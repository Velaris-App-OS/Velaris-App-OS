"""Email API — accounts, templates, send, inbox."""
from __future__ import annotations
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import get_current_user, require_role
from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    EmailAccountModel, EmailTemplateModel, EmailMessageModel, CaseInstanceModel,
)
from velaris_shared.db.session import get_session
from velaris_shared.core.error_codes import CONSENT_REFUSED_403, coded
from case_service.mail import EmailService
from case_service.mail.service import ConsentRefused
from case_service.mail.templates import render_template, TemplateError
from case_service.mail import tenancy as mail_tenancy
from velaris_shared.db.read_routing import get_read_session

router = APIRouter(prefix="/email", tags=["email"])
service = EmailService()


class AccountIn(BaseModel):
    name: str
    address: str
    smtp_host: str
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: bool = True
    imap_host: Optional[str] = None
    imap_port: int = 993
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    imap_use_ssl: bool = True
    imap_folder: str = "INBOX"
    poll_interval_seconds: int = Field(15, ge=5, le=3600)
    is_active: bool = True
    is_default_outbound: bool = False
    tenant_id: Optional[str] = None


class TemplateIn(BaseModel):
    name: str
    description: str = ""
    subject: str
    body_text: str
    body_html: Optional[str] = None
    engine: str = Field("jinja2", pattern=r"^(jinja2|fstring)$")
    scope: str = Field("global", pattern=r"^(global|case_type)$")
    case_type_id: Optional[uuid.UUID] = None
    tenant_id: Optional[str] = None
    is_active: bool = True


class SendIn(BaseModel):
    case_id: Optional[uuid.UUID] = None
    account_id: Optional[uuid.UUID] = None
    to_addresses: list[str]
    cc_addresses: list[str] = []
    template_id: Optional[uuid.UUID] = None
    subject: Optional[str] = None
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    ctx: dict = {}
    in_reply_to: Optional[str] = None
    references: list[str] = []
    dry_run: bool = False
    #: Lawful purpose for this send. Defaults to `case_processing` — never
    #: gated. A gated purpose (today `marketing`) filters recipients against
    #: the HxConsent ledger and refuses when none has a lawful basis.
    purpose: Optional[str] = None


class TemplateRenderIn(BaseModel):
    subject: str
    body_text: str
    body_html: Optional[str] = None
    engine: str = "jinja2"
    ctx: dict = {}


def _where(q, scope, column):
    c = mail_tenancy.clause(scope, column)
    return q if c is None else q.where(c)


async def _own_account(session: AsyncSession, user, account_id: uuid.UUID):
    """The mailbox, if it is the caller's tenant's. Another tenant's mailbox is a
    404 — a 403 would confirm it exists."""
    scope = await mail_tenancy.scope_for(session, user)
    a = await session.get(EmailAccountModel, account_id)
    if a is None or not mail_tenancy.owns(scope, a.tenant_id):
        raise HTTPException(404, "Account not found")
    return a, scope


async def _own_message(session: AsyncSession, user, msg_id: uuid.UUID):
    scope = await mail_tenancy.scope_for(session, user)
    m = await session.get(EmailMessageModel, msg_id)
    if m is None or not mail_tenancy.owns(scope, m.tenant_id):
        raise HTTPException(404, "Message not found")
    return m, scope


async def _tenant_for_write(session: AsyncSession, scope, requested: Optional[str]) -> Optional[str]:
    """The tenant a new mailbox or template is written under.

    Anyone but a superadmin writes into their own tenant, whatever the body says.
    A superadmin may name a tenant, which must exist.
    """
    if not scope.everything:
        return scope.slug
    if requested in (None, ""):
        return None
    from case_service.tenancy.identity import resolve_tenant
    resolved = await resolve_tenant(session, requested)
    if resolved is None:
        raise HTTPException(400, f"{requested!r} is not a tenant on this platform")
    return resolved[1]


async def _clear_other_defaults(session: AsyncSession, keep_id, tenant_value) -> None:
    """One default outbound mailbox PER TENANT. Until 2026-09-17 marking a
    mailbox default cleared every other tenant's default too."""
    scope = await mail_tenancy.scope_for_token(session, tenant_value)
    q = select(EmailAccountModel).where(EmailAccountModel.is_default_outbound.is_(True))
    if keep_id is not None:
        q = q.where(EmailAccountModel.id != keep_id)
    q = _where(q, scope, EmailAccountModel.tenant_id)
    for existing in (await session.execute(q)).scalars().all():
        if existing.tenant_id in (None, "") and tenant_value not in (None, ""):
            # An unassigned mailbox is the default tenant's; a named tenant's
            # default must not clear it unless that tenant IS the default.
            if not scope.owns_unassigned:
                continue
        existing.is_default_outbound = False


def _account_to_dict(a):
    return {
        "id": str(a.id), "name": a.name, "address": a.address,
        "smtp_host": a.smtp_host, "smtp_port": a.smtp_port,
        "smtp_username": a.smtp_username, "smtp_use_tls": a.smtp_use_tls,
        "smtp_password_set": bool(a.smtp_password),
        "imap_host": a.imap_host, "imap_port": a.imap_port,
        "imap_username": a.imap_username, "imap_use_ssl": a.imap_use_ssl,
        "imap_folder": a.imap_folder,
        "imap_password_set": bool(a.imap_password),
        "poll_interval_seconds": a.poll_interval_seconds,
        "is_active": a.is_active, "is_default_outbound": a.is_default_outbound,
        "tenant_id": a.tenant_id,
    }


def _tpl_to_dict(t):
    return {
        "id": str(t.id), "name": t.name, "description": t.description,
        "subject": t.subject, "body_text": t.body_text, "body_html": t.body_html,
        "engine": t.engine, "scope": t.scope,
        "case_type_id": str(t.case_type_id) if t.case_type_id else None,
        "tenant_id": t.tenant_id, "is_active": t.is_active,
    }


def _msg_to_dict(m):
    return {
        "id": str(m.id),
        "case_id": str(m.case_id) if m.case_id else None,
        "direction": m.direction, "from_address": m.from_address,
        "to_addresses": m.to_addresses or [], "cc_addresses": m.cc_addresses or [],
        "subject": m.subject, "body_text": m.body_text, "body_html": m.body_html,
        "message_id": m.message_id, "in_reply_to": m.in_reply_to,
        "references": m.references or [], "status": m.status,
        "is_read": m.is_read, "error_message": m.error_message,
        "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        "received_at": m.received_at.isoformat() if m.received_at else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


# Accounts
async def _require_public_mail_hosts(session, user, smtp_host, imap_host) -> None:
    """A tenant's SMTP/IMAP servers must be on the internet, never this
    install's own network (2026-11-04): a tenant admin — a website demo account
    among them — could point the poller and sender at 127.0.0.1 or the LAN.
    The platform operator's LAN mail servers are unchanged."""
    from velaris_shared.hxbridge.security import validate_outbound_host
    from velaris_shared.tenancy.identity import is_platform_operator
    hosts = [h for h in (smtp_host, imap_host) if h]
    if not hosts or await is_platform_operator(session, user):
        return
    for h in hosts:
        try:
            await validate_outbound_host(h)
        except ValueError:
            raise HTTPException(400, f"Mail host {h!r} is not reachable from this platform: "
                                     "private and internal addresses are refused.")


@router.post("/accounts", status_code=201)
async def create_account(
    body: AccountIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("platform.email.configure")),
):
    scope = await mail_tenancy.scope_for(session, user)
    await _require_public_mail_hosts(session, user, body.smtp_host, body.imap_host)
    values = body.model_dump()
    values["tenant_id"] = await _tenant_for_write(session, scope, body.tenant_id)
    a = EmailAccountModel(**values)
    if body.is_default_outbound:
        await _clear_other_defaults(session, None, a.tenant_id)
    session.add(a)
    await session.flush()
    return _account_to_dict(a)


@router.get("/accounts")
async def list_accounts(
    active_only: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.email.read")),
):
    scope = await mail_tenancy.scope_for(session, _user)
    q = _where(select(EmailAccountModel).order_by(EmailAccountModel.created_at.desc()),
               scope, EmailAccountModel.tenant_id)
    if active_only:
        q = q.where(EmailAccountModel.is_active.is_(True))
    res = await session.execute(q)
    return [_account_to_dict(a) for a in res.scalars().all()]


@router.patch("/accounts/{account_id}")
async def update_account(
    account_id: uuid.UUID, body: AccountIn,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("platform.email.configure")),
):
    a, scope = await _own_account(session, user, account_id)
    await _require_public_mail_hosts(session, user, body.smtp_host, body.imap_host)
    for k, v in body.model_dump().items():
        if k in ("smtp_password", "imap_password") and v is None:
            continue  # None means "keep existing" — never wipe a stored password
        if k == "tenant_id":
            # Only a superadmin moves a mailbox between tenants.
            if scope.everything:
                a.tenant_id = await _tenant_for_write(session, scope, v)
            continue
        setattr(a, k, v)
    if body.is_default_outbound:
        await _clear_other_defaults(session, account_id, a.tenant_id)
    await session.flush()
    # #27 Part B: email-account change can affect intake AI scenarios → flag
    # generated suites' AI layer stale (manual regen).
    from case_service.testsuite import regen
    background_tasks.add_task(regen.bg_scenario_source_changed, None)
    return _account_to_dict(a)


@router.patch("/accounts/{account_id}/deactivate", status_code=200)
async def deactivate_account(
    account_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("platform.email.configure")),
):
    a, _ = await _own_account(session, user, account_id)
    a.is_active = False
    await session.flush()
    return _account_to_dict(a)


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_account(
    account_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("platform.email.configure")),
):
    a, _ = await _own_account(session, user, account_id)
    await session.delete(a)
    await session.flush()
    from starlette.responses import Response
    return Response(status_code=204)


# Templates
@router.post("/templates", status_code=201)
async def create_template(
    body: TemplateIn, session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.email.configure")),
):
    if body.scope == "case_type" and body.case_type_id is None:
        raise HTTPException(400, "case_type_id required when scope='case_type'")
    scope = await mail_tenancy.scope_for(session, _user)
    values = body.model_dump()
    values["tenant_id"] = await _tenant_for_write(session, scope, body.tenant_id)
    t = EmailTemplateModel(**values)
    session.add(t)
    await session.flush()
    return _tpl_to_dict(t)


@router.get("/templates")
async def list_templates(
    case_type_id: Optional[uuid.UUID] = Query(None),
    active_only: bool = Query(True),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.email.read")),
):
    scope = await mail_tenancy.scope_for(session, _user)
    q = _where(select(EmailTemplateModel).order_by(EmailTemplateModel.updated_at.desc()),
               scope, EmailTemplateModel.tenant_id)
    if active_only:
        q = q.where(EmailTemplateModel.is_active.is_(True))
    if case_type_id:
        q = q.where(
            (EmailTemplateModel.case_type_id == case_type_id)
            | (EmailTemplateModel.scope == "global")
        )
    res = await session.execute(q)
    return [_tpl_to_dict(t) for t in res.scalars().all()]


@router.patch("/templates/{tpl_id}")
async def update_template(
    tpl_id: uuid.UUID, body: TemplateIn,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.email.configure")),
):
    scope = await mail_tenancy.scope_for(session, _user)
    t = await session.get(EmailTemplateModel, tpl_id)
    if t is None or not mail_tenancy.owns(scope, t.tenant_id):
        raise HTTPException(404, "Template not found")
    for k, v in body.model_dump().items():
        if k == "tenant_id":
            if scope.everything:
                t.tenant_id = await _tenant_for_write(session, scope, v)
            continue
        setattr(t, k, v)
    await session.flush()
    return _tpl_to_dict(t)


@router.delete("/templates/{tpl_id}", status_code=204)
async def delete_template(
    tpl_id: uuid.UUID, session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.email.configure")),
):
    scope = await mail_tenancy.scope_for(session, _user)
    t = await session.get(EmailTemplateModel, tpl_id)
    if t is not None and mail_tenancy.owns(scope, t.tenant_id):
        t.is_active = False
        await session.flush()
    from starlette.responses import Response
    return Response(status_code=204)


@router.post("/templates/render-preview")
async def render_preview(
    body: TemplateRenderIn,
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.email.configure")),
):
    try:
        return {
            "subject": render_template(body.subject, body.ctx, body.engine),
            "body_text": render_template(body.body_text, body.ctx, body.engine),
            "body_html": render_template(body.body_html, body.ctx, body.engine) if body.body_html else None,
        }
    except TemplateError as e:
        raise HTTPException(400, f"Template error: {e}")


# Send
@router.post("/send", status_code=201)
async def send_email(
    body: SendIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("comms.email.send")),
):
    # A mailbox is its tenant's identity to the outside world: a caller sends
    # only through their own tenant's. Naming another tenant's mailbox is a 404,
    # and the default is this tenant's default, not the platform's.
    scope = await mail_tenancy.scope_for(session, user)
    if body.account_id:
        account = await session.get(EmailAccountModel, body.account_id)
        if account is None or not mail_tenancy.owns(scope, account.tenant_id):
            raise HTTPException(404, "Account not found")
    else:
        account = await mail_tenancy.default_outbound(session, scope)
    if account is None:
        raise HTTPException(400, "No outbound email account configured")

    if body.case_id:
        from case_service.tenancy.identity import case_visible_to
        case = await session.get(CaseInstanceModel, body.case_id)
        if case is None or not await case_visible_to(
                session, case.tenant_id, getattr(user, "tenant_id", None),
                is_superadmin_caller=scope.everything):
            raise HTTPException(404, "Case not found")

    try:
        msg = await service.send(
            session, case_id=body.case_id, account=account,
            to_addresses=body.to_addresses, cc_addresses=body.cc_addresses,
            template_id=body.template_id,
            subject=body.subject, body_text=body.body_text, body_html=body.body_html,
            ctx=body.ctx, in_reply_to=body.in_reply_to, references=body.references,
            dry_run=body.dry_run, purpose=body.purpose,
            tenant_id=str(user.tenant_id) if getattr(user, "tenant_id", None) else None,
        )
    except ConsentRefused as e:
        # 403, not 400: the request is well-formed and the caller is
        # authenticated — the platform is refusing to process on legal grounds.
        # The dropped addresses come back so an operator can see who and why
        # without going to the audit log first.
        raise coded(
            403, CONSENT_REFUSED_403,
            f"Refused: no recipient has a lawful basis for {e.purpose!r}. "
            f"{len(e.dropped)} address(es) were dropped.",
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except TemplateError as e:
        raise HTTPException(400, f"Template error: {e}")

    return {
        "id": str(msg.id), "status": msg.status,
        "message_id": msg.message_id, "subject": msg.subject,
        "case_id": str(msg.case_id) if msg.case_id else None,
        "error": msg.error_message,
    }


# Messages
@router.get("/messages")
async def list_messages(
    case_id: Optional[uuid.UUID] = Query(None),
    direction: Optional[str] = Query(None, pattern=r"^(inbound|outbound)$"),
    unread_only: bool = Query(False),
    unmatched_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.email.read")),
):
    scope = await mail_tenancy.scope_for(session, _user)
    q = _where(select(EmailMessageModel).order_by(EmailMessageModel.created_at.desc()).limit(limit),
               scope, EmailMessageModel.tenant_id)
    if case_id:
        q = q.where(EmailMessageModel.case_id == case_id)
    if direction:
        q = q.where(EmailMessageModel.direction == direction)
    if unread_only:
        q = q.where(EmailMessageModel.is_read.is_(False))
    if unmatched_only:
        q = q.where(EmailMessageModel.case_id.is_(None))
    res = await session.execute(q)
    return [_msg_to_dict(m) for m in res.scalars().all()]


@router.get("/messages/{msg_id}")
async def get_message(
    msg_id: uuid.UUID, session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.email.read")),
):
    m, _ = await _own_message(session, _user, msg_id)
    return _msg_to_dict(m)


@router.post("/messages/{msg_id}/mark-read")
async def mark_read(
    msg_id: uuid.UUID, read: bool = Query(True),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.email.configure")),
):
    m, _ = await _own_message(session, _user, msg_id)
    m.is_read = read
    await session.flush()
    return {"id": str(m.id), "is_read": m.is_read}


@router.post("/messages/{msg_id}/assign-case")
async def assign_to_case(
    msg_id: uuid.UUID, case_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.email.configure")),
):
    m, scope = await _own_message(session, _user, msg_id)
    from case_service.tenancy.identity import case_visible_to
    case = await session.get(CaseInstanceModel, case_id)
    if case is None or not await case_visible_to(
            session, case.tenant_id, getattr(_user, "tenant_id", None),
            is_superadmin_caller=scope.everything):
        raise HTTPException(404, "Case not found")
    m.case_id = case_id
    if m.status == "unmatched":
        m.status = "received"
    await session.flush()
    return _msg_to_dict(m)


@router.get("/inbox/stats")
async def inbox_stats(
    session: AsyncSession = Depends(get_read_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.email.read")),
):
    scope = await mail_tenancy.scope_for(session, _user)
    own = mail_tenancy.clause(scope, EmailMessageModel.tenant_id)
    q_unread = select(func.count()).select_from(EmailMessageModel).where(
        EmailMessageModel.direction == "inbound",
        EmailMessageModel.is_read.is_(False),
    )
    q_unmatched = select(func.count()).select_from(EmailMessageModel).where(
        EmailMessageModel.direction == "inbound",
        EmailMessageModel.case_id.is_(None),
    )
    q_failed = select(func.count()).select_from(EmailMessageModel).where(
        EmailMessageModel.direction == "outbound",
        EmailMessageModel.status == "failed",
    )
    if own is not None:
        q_unread, q_unmatched, q_failed = (q.where(own) for q in (q_unread, q_unmatched, q_failed))
    return {
        "unread_inbound": int((await session.execute(q_unread)).scalar_one()),
        "unmatched_inbound": int((await session.execute(q_unmatched)).scalar_one()),
        "failed_outbound": int((await session.execute(q_failed)).scalar_one()),
    }


@router.post("/simulate-inbound", status_code=201)
async def simulate_inbound(
    body: dict = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("platform.email.operate")),
):
    """Inject a fake inbound email through the real ingest pipeline for testing.

    Gated on `platform.email.operate` since 2026-09-17 — it was open to any
    signed-in user, and it writes mail into the caller's tenant's inbox and can
    attach it to a case. The message lands in the caller's own tenant.
    """
    import email as _email_lib
    from email.mime.text import MIMEText
    from datetime import datetime, timezone
    payload = body or {}

    from_address = payload.get("from_address", "test-sender@example.com")
    to_address = payload.get("to_addresses", ["inbox@yourcompany.com"])
    if isinstance(to_address, list):
        to_address = ", ".join(to_address)
    subject = payload.get("subject", "Test inbound email")
    body_text = payload.get("body_text", "This is a simulated inbound email for testing purposes.")

    mime = MIMEText(body_text, "plain", "utf-8")
    mime["From"] = from_address
    mime["To"] = to_address
    mime["Subject"] = subject
    mime["Message-ID"] = f"<simulate-{uuid.uuid4().hex}@helix.test>"
    mime["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    if payload.get("in_reply_to"):
        mime["In-Reply-To"] = payload["in_reply_to"]

    raw = mime.as_bytes()

    from case_service.mail.service import EmailService
    svc = EmailService()
    msg = await svc.ingest_raw(
        session, raw,
        account_id=None,
        account_address=to_address,
        tenant_id=(await mail_tenancy.scope_for(session, user)).slug,
    )
    await session.commit()
    await session.refresh(msg)
    return _msg_to_dict(msg)


@router.post("/poll-now")
async def poll_now(
    account_id: Optional[uuid.UUID] = Query(None),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("platform.email.operate")),
):
    from case_service.mail.worker import poll_one_account
    from velaris_shared.db.session import get_session_factory

    scope = await mail_tenancy.scope_for(session, user)
    q = _where(select(EmailAccountModel).where(EmailAccountModel.is_active.is_(True)),
               scope, EmailAccountModel.tenant_id)
    if account_id:
        q = q.where(EmailAccountModel.id == account_id)
    accounts = (await session.execute(q)).scalars().all()
    if not accounts:
        raise HTTPException(404, "No active accounts to poll")

    factory = get_session_factory()
    total = 0
    for a in accounts:
        try:
            total += await poll_one_account(a, factory, service)
        except Exception:
            pass
    return {"polled_accounts": len(accounts), "ingested": total}


@router.post("/accounts/{account_id}/test-connection")
async def test_connection(
    account_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("platform.email.operate")),
):
    """Test SMTP and IMAP connectivity for an account and return a plain-English status."""
    import smtplib
    import imaplib as _imap

    a, _ = await _own_account(session, user, account_id)

    result: dict = {"smtp": None, "imap": None}

    # SMTP
    try:
        s = smtplib.SMTP(a.smtp_host, a.smtp_port, timeout=8)
        if a.smtp_use_tls:
            s.starttls()
        if a.smtp_username and a.smtp_password:
            s.login(a.smtp_username, a.smtp_password)
        s.quit()
        result["smtp"] = "ok"
    except Exception as e:
        result["smtp"] = str(e)

    # IMAP
    if a.imap_host and a.imap_username:
        try:
            M = _imap.IMAP4_SSL(a.imap_host, a.imap_port) if a.imap_use_ssl else _imap.IMAP4(a.imap_host, a.imap_port)
            typ, _ = M.login(a.imap_username, a.imap_password or "")
            if typ != "OK":
                result["imap"] = f"login failed: {typ}"
            else:
                # List available folders first — always useful for diagnostics
                folder_names: list[str] = []
                try:
                    _, folders_raw = M.list()
                    for f in (folders_raw or []):
                        if isinstance(f, bytes):
                            parts = f.decode(errors="replace").split('"/"')
                            name = parts[-1].strip().strip('"')
                            folder_names.append(name)
                except Exception:
                    pass

                # Try the configured folder (quote names with spaces — imaplib on this
                # platform does not auto-quote, so [Gmail]/All Mail must become
                # "[Gmail]/All Mail" before being sent over the wire).
                from case_service.mail.imap_client import _imap_mailbox
                try:
                    typ2, _ = M.select(_imap_mailbox(a.imap_folder))
                    if typ2 == "OK":
                        result["imap"] = "ok"
                        result["available_folders"] = folder_names[:30]
                    else:
                        result["imap"] = f"folder {a.imap_folder!r} not found"
                        result["available_folders"] = folder_names[:30]
                except Exception as sel_err:
                    result["imap"] = f"folder {a.imap_folder!r} error: {sel_err}"
                    result["available_folders"] = folder_names[:30]
                try:
                    M.logout()
                except Exception:
                    pass
        except Exception as e:
            result["imap"] = str(e)
    else:
        result["imap"] = "not configured"

    return result
