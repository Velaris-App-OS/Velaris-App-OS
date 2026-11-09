"""High-level email service."""
from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .parser import parse_rfc822, quoted_message_ids
from .smtp_client import build_mime_message, send_via_smtp
from .templates import render_template
from .threader import (
    build_message_id, build_subject_tag, build_references_chain,
    resolve_case_id_from_message,
    detect_case_types_from_content, find_open_case_for_sender,
)

log = logging.getLogger(__name__)

#: What a stored message says in place of a body that held a one-time code.
SECRET_BODY_PLACEHOLDER = "[not stored: this message carried a one-time code]"


class ConsentRefused(Exception):
    """No recipient of a consent-gated send has a lawful basis.

    Raised rather than returning an empty result so a caller cannot mistake
    "refused on consent grounds" for "sent successfully to nobody".
    """

    def __init__(self, purpose: str, dropped: list[str]) -> None:
        self.purpose = purpose
        self.dropped = dropped
        super().__init__(
            f"no recipient has a lawful basis for {purpose!r} "
            f"({len(dropped)} address(es) dropped)"
        )


class EmailService:
    async def _consent_filter(
        self, session: AsyncSession, addresses: list[str], purpose: str,
        *, tenant_id: str | None,
    ) -> tuple[list[str], list[str]]:
        """Split recipients into (allowed, dropped) for a gated purpose.

        **Fails closed, per address.** `has_consent` already returns False for
        an unknown subject or an unrecognised purpose — the absence of a
        recorded basis is not permission — and an *error* during the check is
        treated the same way. A consent gate that opens when the ledger is
        unreachable is not a gate.
        """
        from case_service.compliance import consent as consent_svc

        allowed: list[str] = []
        dropped: list[str] = []
        for addr in addresses:
            try:
                ok = await consent_svc.has_consent(
                    session, addr, purpose, tenant_id=tenant_id,
                )
            except Exception:
                # Deliberately broad: any failure to establish lawfulness means
                # we have not established it.
                log.exception(
                    "consent check failed for %s/%s — refusing this recipient",
                    addr, purpose,
                )
                ok = False
            (allowed if ok else dropped).append(addr)
        return allowed, dropped

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
        purpose: str | None = None,
        tenant_id: str | None = None,
        calendar_ics: str | None = None,
        secret_body: bool = False,
    ):
        """Send an email, enforcing purpose limitation for gated purposes.

        `secret_body`: the message carries a one-time code (sign-in, password
        reset, bank-details verification). It is sent as written, but the stored
        row keeps no body — `email_messages` is read through the mail API by
        anyone holding `platform.email.read`, and a stored code is a way into the
        account it was sent to (found R5e step 10, 2026-10-22).

        `purpose` defaults to `case_processing`, which is **never gated**: case
        correspondence is lawful under contract or legal obligation, and making
        it depend on a consent record would stop the platform doing the job it
        was engaged to do. Only purposes in `consent.GATED_PURPOSES` — today
        `marketing` alone — are filtered.

        Gating happens **before** the message row is written, so a refused
        recipient leaves no queued artefact claiming a send that must not
        happen, and the surviving row's `to_addresses` is the truthful record
        of who was actually written to.
        """
        from case_service.compliance import consent as consent_svc
        from velaris_shared.db.models import EmailMessageModel, EmailTemplateModel

        purpose = purpose or consent_svc.CASE_PROCESSING
        cc_addresses = cc_addresses or []

        if purpose in consent_svc.GATED_PURPOSES:
            to_addresses, dropped_to = await self._consent_filter(
                session, to_addresses, purpose, tenant_id=tenant_id,
            )
            cc_addresses, dropped_cc = await self._consent_filter(
                session, cc_addresses, purpose, tenant_id=tenant_id,
            )
            dropped = dropped_to + dropped_cc
            if dropped:
                # The provable part. Without this, "we stopped marketing to
                # them" is an assertion; with it, it is a record.
                from velaris_shared.db.security_events import log_security_event

                await log_security_event(
                    session,
                    event_type="consent.send_filtered",
                    severity="info",
                    resource_type="email",
                    resource_id=str(case_id) if case_id else None,
                    action=f"send:{purpose}",
                    outcome="blocked" if not to_addresses else "partial",
                    details={
                        "purpose": purpose,
                        "dropped": dropped,
                        "dropped_count": len(dropped),
                        "delivered_count": len(to_addresses) + len(cc_addresses),
                    },
                )
                log.info(
                    "consent gate: dropped %d recipient(s) for purpose %r",
                    len(dropped), purpose,
                )
            if not to_addresses:
                # A cc-only send is not a send: with every `to` dropped there
                # is no lawful primary recipient left to address.
                raise ConsentRefused(purpose, dropped)

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
            calendar_ics=calendar_ics,
        )

        row = EmailMessageModel(
            id=uuid.uuid4(), case_id=case_id, direction="outbound",
            account_id=account.id, message_id=msg_id, in_reply_to=in_reply_to,
            references=refs, from_address=account.address,
            to_addresses=to_addresses, cc_addresses=cc_addresses or [],
            subject=rendered_subject,
            body_text=SECRET_BODY_PLACEHOLDER if secret_body else rendered_text,
            body_html=None if secret_body else rendered_html,
            status="queued", is_read=True,
        )
        session.add(row)
        await session.flush()

        if dry_run:
            row.status = "dry_run"
            await session.flush()
            return row

        try:
            from .egress import connect_host
            smtp_host = await connect_host(account.smtp_host, account.tenant_id,
                                           verified_tls=bool(account.smtp_use_tls),
                                           session=session)
            await send_via_smtp(
                mime_message=mime,
                host=smtp_host, port=account.smtp_port,
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
        from velaris_shared.db.models import EmailMessageModel

        parsed = parse_rfc822(raw)

        # Detect direction: if the message was sent FROM this account's own address
        # it is outbound (e.g. pulled from [Gmail]/Sent Mail or All Mail).
        from_addr = (parsed["from_address"] or "").lower().strip()
        own_addr  = (account_address or "").lower().strip()
        direction = "outbound" if (own_addr and from_addr == own_addr) else "inbound"

        # Idempotency — check both directions to avoid duplicates across folder switches
        if parsed["message_id"]:
            # Per tenant: the same message delivered to two tenants' mailboxes is
            # two rows, and one tenant's copy must not swallow the other's.
            q = select(EmailMessageModel).where(
                EmailMessageModel.message_id == parsed["message_id"],
                (EmailMessageModel.tenant_id == tenant_id) if tenant_id is not None
                else EmailMessageModel.tenant_id.is_(None),
            ).limit(1)
            existing = (await session.execute(q)).scalar_one_or_none()
            if existing is not None:
                return existing
            # A message we sent with a one-time code (`secret_body`) comes back
            # from a polled Sent / All Mail folder tagged with the account's
            # tenant, so the per-tenant check above misses the row we wrote
            # without one — and the copy would store the code after all.
            withheld = (await session.execute(select(EmailMessageModel).where(
                EmailMessageModel.message_id == parsed["message_id"],
                EmailMessageModel.body_text == SECRET_BODY_PLACEHOLDER,
            ).limit(1))).scalar_one_or_none()
            if withheld is not None:
                return withheld

        # A reply to, or a bounce of, a message we sent with a one-time code
        # (`secret_body`) usually quotes it — a bounce encloses the original
        # whole. Recorded, but without its body. Closed 2026-10-22 (R5e step 10).
        quoted = quoted_message_ids(raw)
        if quoted and (await session.execute(select(EmailMessageModel.id).where(
                EmailMessageModel.message_id.in_(quoted),
                EmailMessageModel.body_text == SECRET_BODY_PLACEHOLDER,
        ).limit(1))).first() is not None:
            parsed["body_text"] = SECRET_BODY_PLACEHOLDER
            parsed["body_html"] = None

        case_id = await resolve_case_id_from_message(
            session,
            in_reply_to=parsed["in_reply_to"],
            references=parsed["references"],
            subject=parsed["subject"],
            tenant_id=tenant_id,
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
                open_case = await find_open_case_for_sender(
                    session, from_addr, ct.id, tenant_id=tenant_id)
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
