"""High-level email service."""
from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .parser import parse_rfc822
from .smtp_client import build_mime_message, send_via_smtp
from .templates import render_template
from .threader import (
    build_message_id, build_subject_tag, build_references_chain,
    resolve_case_id_from_message,
    detect_case_types_from_content, find_open_case_for_sender,
)

log = logging.getLogger(__name__)


class EmailService:
    async def send(
        self, session: AsyncSession, *,
        case_id: Optional[uuid.UUID], account,
        to_addresses: list[str], cc_addresses: list[str] | None = None,
        template_id: Optional[uuid.UUID] = None,
        subject: Optional[str] = None,
        body_text: Optional[str] = None,
        body_html: Optional[str] = None,
        ctx: dict[str, Any] | None = None,
        in_reply_to: Optional[str] = None,
        references: list[str] | None = None,
        dry_run: bool = False,
    ):
        from case_service.db.models import EmailMessageModel, EmailTemplateModel

        if template_id:
            tmpl = await session.get(EmailTemplateModel, template_id)
            if tmpl is None:
                raise ValueError(f"Template {template_id} not found")
            engine = tmpl.engine
            ctx = ctx or {}
            rendered_subject = render_template(tmpl.subject, ctx, engine)
            rendered_text = render_template(tmpl.body_text, ctx, engine)
            rendered_html = render_template(tmpl.body_html, ctx, engine) if tmpl.body_html else None
        else:
            if not subject or body_text is None:
                raise ValueError("subject + body_text required when no template_id")
            rendered_subject = subject
            rendered_text = body_text
            rendered_html = body_html

        if case_id:
            tag = build_subject_tag(case_id)
            if tag not in rendered_subject:
                rendered_subject = f"{tag} {rendered_subject}"

        msg_id = build_message_id(case_id)
        refs = build_references_chain(in_reply_to, references)

        mime = build_mime_message(
            from_address=account.address,
            to_addresses=to_addresses, cc_addresses=cc_addresses or [],
            subject=rendered_subject, body_text=rendered_text, body_html=rendered_html,
            message_id=msg_id, in_reply_to=in_reply_to, references=refs,
        )

        row = EmailMessageModel(
            id=uuid.uuid4(), case_id=case_id, direction="outbound",
            account_id=account.id, message_id=msg_id, in_reply_to=in_reply_to,
            references=refs, from_address=account.address,
            to_addresses=to_addresses, cc_addresses=cc_addresses or [],
            subject=rendered_subject, body_text=rendered_text, body_html=rendered_html,
            status="queued", is_read=True,
        )
        session.add(row)
        await session.flush()

        if dry_run:
            row.status = "dry_run"
            await session.flush()
            return row

        try:
            await send_via_smtp(
                mime_message=mime,
                host=account.smtp_host, port=account.smtp_port,
                username=account.smtp_username, password=account.smtp_password,
                use_tls=account.smtp_use_tls,
            )
            row.status = "sent"
            row.sent_at = datetime.now(timezone.utc)
        except Exception as e:
            row.status = "failed"
            row.error_message = str(e)[:1000]
            log.warning("SMTP send failed: %s", e)
        await session.flush()
        return row

    async def ingest_raw(
        self, session: AsyncSession, raw: bytes,
        account_id: Optional[uuid.UUID] = None,
        account_address: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ):
        from case_service.db.models import EmailMessageModel

        parsed = parse_rfc822(raw)

        # Detect direction: if the message was sent FROM this account's own address
        # it is outbound (e.g. pulled from [Gmail]/Sent Mail or All Mail).
        from_addr = (parsed["from_address"] or "").lower().strip()
        own_addr  = (account_address or "").lower().strip()
        direction = "outbound" if (own_addr and from_addr == own_addr) else "inbound"

        # Idempotency — check both directions to avoid duplicates across folder switches
        if parsed["message_id"]:
            q = select(EmailMessageModel).where(
                EmailMessageModel.message_id == parsed["message_id"],
            )
            existing = (await session.execute(q)).scalar_one_or_none()
            if existing is not None:
                return existing

        case_id = await resolve_case_id_from_message(
            session,
            in_reply_to=parsed["in_reply_to"],
            references=parsed["references"],
            subject=parsed["subject"],
        )

        # Fallback: fuzzy case-type routing for unmatched inbound messages.
        # Tries all matching case types in score order; attaches to first open case found.
        matched_via_type = False
        if case_id is None and direction == "inbound":
            candidates = await detect_case_types_from_content(
                session,
                subject=parsed["subject"] or "",
                body=parsed["body_text"] or "",
            )
            from_addr = (parsed["from_address"] or "").lower().strip()
            for score, ct in candidates:
                open_case = await find_open_case_for_sender(session, from_addr, ct.id)
                if open_case is not None:
                    case_id = open_case.id
                    matched_via_type = True
                    log.info(
                        "fuzzy-routed inbound from %s → case %s (type: %s, score: %.2f)",
                        from_addr, case_id, ct.name, score,
                    )
                    break

        if direction == "outbound":
            status = "sent"
        elif case_id:
            status = "received"
        else:
            status = "unmatched"

        row = EmailMessageModel(
            id=uuid.uuid4(), case_id=case_id, direction=direction,
            account_id=account_id, message_id=parsed["message_id"],
            in_reply_to=parsed["in_reply_to"], references=parsed["references"],
            from_address=parsed["from_address"],
            to_addresses=parsed["to_addresses"], cc_addresses=parsed["cc_addresses"],
            subject=parsed["subject"], body_text=parsed["body_text"],
            body_html=parsed["body_html"], raw_headers=parsed["raw_headers"],
            status=status,
            sent_at=parsed["received_at"] if direction == "outbound" else None,
            received_at=parsed["received_at"] if direction == "inbound" else None,
            tenant_id=tenant_id, is_read=(direction == "outbound"),
        )
        session.add(row)
        await session.flush()
        return row
