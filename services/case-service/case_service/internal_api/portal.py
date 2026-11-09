"""The portal's internal case API — `/internal/v1/portal/*` (R5e step 8).

What the customer portal asks case-service to do, because only case-service may:
open a portal case (rollouts, consent), complete a customer step (audit,
outbound rules, auto-advance), read case variables through the portal's
redaction context, hand out a meeting token, export or erase a customer (legal
hold is enforced here), run speech recognition and voice form-fill (one ASR
model, on this process's GPU), and send a sign-in code by email.

Each handler is the code the portal router ran in-process before the split,
moved verbatim. The router checks the customer and the portal first — and the
handler checks again what it can (the account↔case link, the customer step,
the portal's case types): every holder of the internal secret reaches these
routes, not only portal-service, so a handler never takes the caller's word for
who acted or what they may touch. The actor is derived, never sent. Every route
carries its tenant or its case explicitly and is safe to repeat.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import binascii
import logging
import uuid
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)
logger = log

#: 45 s of 16 kHz mono PCM16 (the portal's own limit), base64 — plus slack.
_MAX_PCM_B64 = (45 * 16_000 * 2) * 4 // 3 + 16


async def _linked_case(session: AsyncSession, customer_id: uuid.UUID, case_id: uuid.UUID):
    """The case, only when this customer's account is linked to it — the same
    rule the portal applies. Uniform 404 otherwise (missing, deleted, unlinked)."""
    from velaris_shared.db.models import CaseInstanceModel, PortalCustomerCaseLinkModel

    link = (await session.execute(select(PortalCustomerCaseLinkModel).where(
        PortalCustomerCaseLinkModel.customer_id == customer_id,
        PortalCustomerCaseLinkModel.case_id == case_id))).scalar_one_or_none()
    case = await session.get(CaseInstanceModel, case_id) if link else None
    if case is None or bool(getattr(case, "is_deleted", False)):
        raise HTTPException(404, "Case not found")
    return case


async def _customer_step(session: AsyncSession, case, step_id: str) -> dict:
    """A customer step of the case's CURRENT stage, read from the case type."""
    from velaris_shared.case_actions.customer_steps import customer_steps
    from velaris_shared.db.models import CaseTypeModel

    ct = await session.get(CaseTypeModel, case.case_type_id) if case.case_type_id else None
    step = next((s for s in customer_steps(ct, case.current_stage_id)
                 if s.get("id") == step_id), None)
    if step is None:
        raise HTTPException(404, "Action not found")
    return step


# ── 1. submit: open a portal case ─────────────────────────────────────────────

class PortalCaseIn(BaseModel):
    tenant_id: uuid.UUID
    slug: str = Field(min_length=1, max_length=200)
    case_type_id: uuid.UUID
    submitter_name: str
    submitter_email: str
    subject: str
    description: str
    priority: str = "medium"
    extra_data: dict[str, Any] = Field(default_factory=dict)
    client_ref: uuid.UUID | None = None


def _replay(original) -> dict:
    return {"case_id": str(original.id) if original else None,
            "tracking_token": str(original.portal_tracking_token)
            if original and original.portal_tracking_token else "",
            "replayed": True}


async def open_case(session: AsyncSession, body: PortalCaseIn) -> dict:
    """Portal submit. Idempotent on `client_ref`: a repeat answers the original case."""
    from sqlalchemy.exc import IntegrityError

    from case_service.rollouts import service as _rollouts
    from velaris_shared.db.models import (
        CaseInstanceModel,
        CaseTypeModel,
        PortalCustomerCaseLinkModel as _Link,
        PortalCustomerModel as _Cust,
        PortalSubmissionRefModel as _Ref,
    )

    if body.client_ref is not None:
        seen = (await session.execute(
            select(_Ref).where(_Ref.client_ref == body.client_ref))).scalar_one_or_none()
        if seen is not None:
            if seen.tenant_slug != body.slug:
                raise HTTPException(400, "Invalid client reference")
            return _replay(await session.get(CaseInstanceModel, seen.case_id))

    # The portal's own rule, again: the slug's tenant, its portal switched on,
    # a portal-enabled case type of that tenant (or platform-shared), and in
    # the portal's allowed list when it has one.
    from velaris_shared.db.models import TenantModel
    from velaris_shared.tenancy.identity import same_tenant

    tenant = await session.get(TenantModel, body.tenant_id)
    portal = ((tenant.settings or {}).get("portal") or {}) if tenant else {}
    ct = await session.get(CaseTypeModel, body.case_type_id)
    allowed = portal.get("allowed_case_type_ids", [])
    if (tenant is None or tenant.slug != body.slug or not portal.get("enabled", False)
            or ct is None or not ct.portal_enabled
            or not await same_tenant(session, ct.tenant_id, tenant.id)
            or (allowed and str(ct.id) not in allowed)):
        raise HTTPException(400, "Case type not available for portal submission")

    token = uuid.uuid4()
    ct, _rollout = await _rollouts.resolve_case_type(
        session, case_type=ct, tenant=body.tenant_id,
        subject=_rollouts.subject_of(body.submitter_email),
        data={"subject": body.subject, **(body.extra_data or {})})
    case = CaseInstanceModel(
        case_type_id=ct.id,
        case_type_version=ct.version,
        # Found 2026-09-29 (item 18a): a portal case was stored with NO tenant,
        # and a NULL-tenant case is readable by every tenant. It belongs to the
        # tenant whose portal it came through.
        tenant_id=body.tenant_id,
        status="new",
        priority=body.priority,
        created_by=f"portal:{body.submitter_email}",
        portal_tracking_token=token,
        portal_submitter_name=body.submitter_name,
        portal_submitter_email=body.submitter_email,
        # The platform's own keys come LAST (2026-10-28): before, a customer's
        # extra_data could overwrite source, tenant_slug, subject and description.
        data={
            **body.extra_data,
            "subject": body.subject,
            "description": body.description,
            "source": "customer_portal",
            "tenant_slug": body.slug,
        },
        extra_metadata={"portal_slug": body.slug},
    )
    session.add(case)
    await session.flush()
    await _rollouts.record(session, case_id=case.id, res=_rollout, tenant=body.tenant_id,
                           via="portal")

    # HxConsent (§8.1): submitting is the consent act for processing this
    # case, so the basis is recorded at the moment it arises rather than
    # reconstructed later. Best-effort — a ledger write must never lose a
    # customer's submission, and the case itself remains lawful under the
    # same act whether or not the row lands.
    try:
        from case_service.compliance import consent as _consent

        await _consent.record(
            session,
            subject_ref=body.submitter_email,
            purpose=_consent.CASE_PROCESSING,
            action=_consent.GRANTED,
            basis=_consent.CONSENT,
            source="portal_submit",
            evidence={"tenant_slug": body.slug, "subject": body.subject[:200]},
            case_id=case.id,
            tenant_id=str(ct.tenant_id) if ct.tenant_id else None,
            actor_id=f"portal:{body.submitter_email}",
        )
    except Exception as _exc:
        log.warning("consent not recorded for portal submit: %s", _exc)

    # Portal v2 (P1): link the case to an existing customer account at submit
    # time — registration only back-links historical cases, so without this a
    # logged-in customer's NEW submissions never appear under My Cases.
    account = (await session.execute(
        select(_Cust).where(
            _Cust.tenant_id == body.tenant_id,
            _Cust.primary_email == body.submitter_email.lower(),
        )
    )).scalar_one_or_none()
    if account:
        session.add(_Link(customer_id=account.id, case_id=case.id))

    if body.client_ref is not None:
        session.add(_Ref(client_ref=body.client_ref, tenant_slug=body.slug, case_id=case.id))

    # HxShield producer (roadmap #25). duplicate_case_flood, dos_submission and
    # replay_attack all match on `portal_submit`, and nothing had ever written
    # it — so the platform's most-attacked surface was the one HxShield could
    # say least about. Recorded BEFORE the commit so it shares the submission's
    # transaction: a submission that rolls back did not happen, and must not
    # leave a detection event claiming it did.
    try:
        from velaris_shared.db.security_events import log_security_event
        await log_security_event(
            session, event_type="portal_submit", severity="info",
            user_id=f"portal:{(body.submitter_email or 'anonymous')}",
            resource_type="case", resource_id=str(case.id),
            action="submit", outcome="success",
            details={"tenant_id": body.slug, "case_type_id": str(case.case_type_id),
                     "client_ref": str(body.client_ref) if body.client_ref else None},
        )
    except Exception as _e:  # noqa: BLE001
        logger.warning("portal_submit security event not recorded: %s", _e)

    try:
        await session.commit()
    except IntegrityError:
        # Two racing replays: the loser re-reads the winner's ref row.
        await session.rollback()
        seen = (await session.execute(
            select(_Ref).where(_Ref.client_ref == body.client_ref))).scalar_one()
        return _replay(await session.get(CaseInstanceModel, seen.case_id))

    return {"case_id": str(case.id), "tracking_token": str(token), "replayed": False}


# ── 2. a customer completes a step ────────────────────────────────────────────

class StepCompleteIn(BaseModel):
    case_id: uuid.UUID
    customer_id: uuid.UUID
    step_id: str = Field(min_length=1, max_length=256)
    status: str = Field(pattern="^(completed|rejected)$")
    data: dict[str, Any] = Field(default_factory=dict)


async def complete_step(session: AsyncSession, body: StepCompleteIn) -> dict:
    """Completion row + audit + `step_complete` outbound rules + auto-advance.

    The router has already refused a closed case, a step that is not the
    customer's, and a step already done; this checks the link, the open case and
    the step again, and records the CUSTOMER as the actor. A repeat finds the
    row and does nothing.
    """
    from case_service.api.routers.cases import (
        _audit,
        _auto_advance_if_complete,
        fire_outbound_rules,
    )
    from case_service.case_vars import service as case_vars
    from velaris_shared.case_actions import statuses as _st
    from velaris_shared.db.models import CaseStepCompletionModel, CaseTypeModel

    case = await _linked_case(session, body.customer_id, body.case_id)
    if await _st.is_terminal_for_case(session, case):
        raise HTTPException(400, "This case is closed")
    step = await _customer_step(session, case, body.step_id)
    actor = f"customer:{body.customer_id}"
    stage_id = case.current_stage_id
    existing = (await session.execute(
        select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == case.id,
            CaseStepCompletionModel.step_id == body.step_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return {"step_id": body.step_id, "status": existing.status,
                "auto_advanced": False, "advance_held_by_risk": False, "replayed": True}

    session.add(CaseStepCompletionModel(
        case_id=case.id, stage_id=stage_id, step_id=body.step_id,
        step_type=step.get("step_type", "user_task"), status=body.status,
        data={**body.data, "source": "customer_portal"},
        completed_by=actor,
    ))
    await session.flush()
    await _audit(session, case.id, "step_completed", actor_id=actor,
                 details={"step_id": body.step_id, "stage_id": stage_id,
                          "status": body.status, "source": "customer_portal"})

    # Parity with the worker complete_step path: fire step_complete connector
    # rules. (Stage-level SLA start + stage_enter/exit rules are handled INSIDE
    # _auto_advance_if_complete below, so both callers get them.) We do NOT nudge
    # the Temporal SLA companion here — that _signal_lifecycle hop needs the
    # worker request context; a customer action that advances the stage still
    # (re)starts the next stage's SLA via auto-advance, which is what matters.
    if body.status == "completed":
        try:
            await fire_outbound_rules(
                session, trigger_event="step_complete",
                case_id=case.id, case_type_id=case.case_type_id,
                case_data=await case_vars.get_all(
                    session,
                    case_vars.CallerContext(kind="rules", actor_id="outbound-rules"),
                    case.id),
                tenant_id="default",
            )
        except Exception:
            log.warning("step_complete outbound rules failed for customer action %s/%s",
                        case.id, body.step_id)

    auto_advanced = False
    held_by_risk = False
    if body.status == "completed" and case.status in ("open", "new", "reopened"):
        ct = await session.get(CaseTypeModel, case.case_type_id)
        new_stage, held_by_risk = await _auto_advance_if_complete(
            session, case.id, case.current_stage_id, (ct.definition_json if ct else None) or {})
        auto_advanced = new_stage is not None
    await session.commit()

    return {"step_id": body.step_id, "status": body.status, "auto_advanced": auto_advanced,
            "advance_held_by_risk": held_by_risk, "replayed": False}


# ── 3. case variables, portal context ─────────────────────────────────────────

class CaseVarsIn(BaseModel):
    case_ids: list[uuid.UUID] = Field(max_length=500)


async def case_vars_for(session: AsyncSession, body: CaseVarsIn) -> dict:
    """Case data via the case_vars façade (portal context: pii/secret masked)."""
    from case_service.case_vars import service as case_vars

    ctx = case_vars.CallerContext(kind="portal", actor_id="portal")
    got = await case_vars.get_all_bulk(session, ctx, body.case_ids)
    return {"vars": {str(k): v for k, v in got.items()}}


# ── 4. a meeting token for an invited customer ────────────────────────────────

class MeetJoinIn(BaseModel):
    session_id: uuid.UUID
    case_id: uuid.UUID
    customer_id: uuid.UUID
    display_name: str | None = Field(default=None, max_length=500)


async def meet_join(session: AsyncSession, body: MeetJoinIn) -> dict:
    from case_service.meet import service as meet_service
    from velaris_shared.db.models import CaseSessionModel

    await _linked_case(session, body.customer_id, body.case_id)
    row = (await session.execute(
        select(CaseSessionModel).where(
            CaseSessionModel.id == body.session_id,
            CaseSessionModel.case_id == body.case_id,
        )
    )).scalar_one_or_none()
    if row is None or row.status != "active" or row.driver != "embedded":
        raise HTTPException(404, "Session not found")
    try:
        return await meet_service.join_customer(
            session, row=row, customer_id=body.customer_id,
            display_name=body.display_name)
    except ValueError:
        raise HTTPException(404, "Session not found") from None   # not invited — same 404


# ── 5. GDPR export / erasure of a portal customer ─────────────────────────────

class CustomerIn(BaseModel):
    tenant_id: uuid.UUID
    customer_id: uuid.UUID


class EraseIn(CustomerIn):
    #: "self": a customer erasing their own account (a legal hold is recorded for
    #: staff, never told); "admin": an administrator (a hold refuses with 409).
    mode: str = Field(pattern="^(self|admin)$")


async def _customer(session: AsyncSession, body: CustomerIn):
    from velaris_shared.db.models import PortalCustomerModel

    customer = await session.get(PortalCustomerModel, body.customer_id)
    if customer is None or str(customer.tenant_id) != str(body.tenant_id):
        raise HTTPException(404, "Customer not found")
    return customer


async def customer_export(session: AsyncSession, body: CustomerIn) -> dict:
    """GDPR Art. 15 for a customer's own account. Staff notes never included."""
    from case_service.privacy import customer_export as _export
    from velaris_shared.db import security_events

    customer = await _customer(session, body)
    data = await _export.build(session, tenant=customer.tenant_id,
                               portal_customer_ids=[customer.id],
                               include_internal=False)
    await security_events.log_security_event(
        session, event_type="gdpr.customer_self_export", severity="info",
        user_id=f"portal:{customer.id}", resource_type="customer",
        resource_id=str(customer.id), action="export", outcome="success",
        details={"counts": _export.counts(data)})
    await session.commit()
    return {"data": data}


async def _record_refused_self_erasure(session: AsyncSession, customer) -> None:
    """An erasure request staff must answer by hand. One open request per subject."""
    from case_service.enterprise import gdpr
    from velaris_shared.db import security_events
    from velaris_shared.db.models import GDPRRequestModel

    subject = str(customer.primary_email or customer.id)
    already = (await session.execute(select(GDPRRequestModel.id).where(
        GDPRRequestModel.tenant_id == customer.tenant_id,
        GDPRRequestModel.subject_id == subject,
        GDPRRequestModel.request_type == "erasure",
        GDPRRequestModel.status == "pending"))).first()
    if already is None:
        await gdpr.create_request(
            session, subject_id=subject, request_type="erasure",
            requested_by=f"portal:{customer.id}", tenant_id=customer.tenant_id,
            reason="Self-service erasure refused: a case of this customer is under a "
                   "legal hold. Review and answer the customer.")
    await security_events.log_security_event(
        session, event_type="gdpr.erasure_refused_legal_hold", severity="warning",
        user_id=f"portal:{customer.id}", resource_type="customer",
        resource_id=str(customer.id), action="erase", outcome="denied", details={})


async def customer_erase(session: AsyncSession, body: EraseIn) -> dict:
    """GDPR Art. 17 — the rep desk's copy goes, then the record is anonymised.

    Answers `{"erased": False, "held": {...}}` when a legal hold refuses, so the
    router can answer exactly as before in either mode.
    """
    from case_service.engage import erasure as _engage_erasure
    from case_service.legal_hold.service import LegalHoldError

    customer = await _customer(session, body)
    # The rep desk's copy of this person goes too — profile, identifiers, call
    # numbers and backfill proposals (2026-09-29; erasure reached none of it).
    try:
        await _engage_erasure.forget(
            session, tenant_uuid=customer.tenant_id,
            emails=[customer.primary_email, customer.alt_email], phones=[customer.phone],
            portal_customer_ids=[str(customer.id)],
            reason="portal self-erasure" if body.mode == "self" else "portal admin erasure")
    except LegalHoldError as exc:
        if body.mode == "self":
            # One of their cases is under a legal hold (Oct item 13). The customer
            # is NOT told that — a hold can concern an investigation of the very
            # person asking. The request is recorded for staff instead of lost.
            await _record_refused_self_erasure(session, customer)
            await session.commit()
        return {"erased": False, "held": {"status": exc.status_code, "code": exc.code,
                                          "message": str(exc)}}

    customer.display_name    = "Deleted User"
    customer.primary_email   = f"deleted_{customer.id}@deleted"
    customer.alt_email       = None
    customer.phone           = None
    customer.otp_code        = None
    customer.otp_expires_at  = None
    session.add(customer)
    await session.commit()
    return {"erased": True, "held": None}


# ── 6. voice: speech recognition + form-fill stay here (one ASR model) ────────

class VoiceStatusIn(BaseModel):
    slug: str = Field(min_length=1, max_length=200)
    opted_in: bool


async def voice_status(session: AsyncSession, body: VoiceStatusIn) -> dict:
    from case_service.meet import asr as meet_asr
    from velaris_shared.core.feature_flags import is_feature_enabled

    info = meet_asr.info()
    fill_available = False
    external = False
    if body.opted_in:
        from case_service.hxnexus.case_qa import tenant_ai_settings

        ai_cfg = await tenant_ai_settings(session, body.slug)
        external = (ai_cfg.get("egress") or "local_only").lower() == "external_allowed"
        if is_feature_enabled("voice_fill"):
            fill_available = bool(ai_cfg.get("case_qa", False))
    return {"asr_available": meet_asr.available(),
            "fill_available": fill_available,
            "external": external,
            "engine": info.engine if info else None,
            "realtime": bool(info.realtime) if info else None}


class _VoiceCaseIn(BaseModel):
    case_id: uuid.UUID
    customer_id: uuid.UUID


async def _audit_customer(session: AsyncSession, case_id, action: str,
                          customer_id, details: dict) -> None:
    from case_service.api.routers.cases import _audit
    await _audit(session, case_id, action, actor_id=f"customer:{customer_id}",
                 details={**details, "source": "customer_portal"})


class TranscribeIn(_VoiceCaseIn):
    pcm_b64: str = Field(min_length=1, max_length=_MAX_PCM_B64)
    consent: bool = False
    clip_seconds_max: int = Field(default=45, ge=1, le=45)


async def voice_transcribe(session: AsyncSession, body: TranscribeIn) -> dict:
    """One PCM16 clip in, text out. The clip is never stored; the audit names
    that a dictation happened and how long it was, never a word of it."""
    from case_service.meet import asr as meet_asr

    await _linked_case(session, body.customer_id, body.case_id)
    if not meet_asr.available():
        raise HTTPException(501, "Speech recognition is not installed on this server")
    try:
        pcm = base64.b64decode(body.pcm_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "pcm_b64 is not base64") from None
    if len(pcm) > body.clip_seconds_max * meet_asr.SAMPLE_RATE * 2:
        raise HTTPException(413, f"Clip exceeds the {body.clip_seconds_max}s limit")
    if len(pcm) < int(meet_asr.SAMPLE_RATE * 2 * 0.2) or len(pcm) % 2:
        raise HTTPException(400, "Body must be at least 0.2s of 16 kHz mono PCM16 audio")

    if body.consent:
        await _audit_customer(session, body.case_id, "portal_voice_consent", body.customer_id,
                              {"clip_seconds_max": body.clip_seconds_max})
    text = await meet_asr.transcribe_pcm16(pcm)
    del pcm  # the clip dies with the request — nothing is stored
    await _audit_customer(session, body.case_id, "portal_voice_transcribed", body.customer_id,
                          {"characters": len(text or "")})
    await session.commit()

    info = meet_asr.info()
    return {"text": text, "engine": info.engine if info else None,
            "realtime": bool(info.realtime) if info else None}


class FillIn(_VoiceCaseIn):
    step_id: str = Field(min_length=1, max_length=256)
    transcript: str = Field(min_length=1, max_length=8_000)


class _CustomerActor:
    """Actor shim for the shared extractor's egress audit: a portal customer is
    not an ``AuthenticatedUser``, and ``customer:`` is the portal's convention."""

    def __init__(self, customer_id) -> None:
        self.user_id = f"customer:{customer_id}"


async def voice_fill(session: AsyncSession, body: FillIn) -> dict:
    """Propose values for ONE customer card. Writes nothing but the audit.
    Refusals come back as 403 (`consent`), 400, 503."""
    from case_service.compliance.consent import ProcessingRefused
    from case_service.voicefill import service as voicefill

    # The card's own fields, read from the case type — never a catalogue the
    # caller sends, so this cannot be pointed at the tenant's other variables.
    case = await _linked_case(session, body.customer_id, body.case_id)
    step = await _customer_step(session, case, body.step_id)
    catalogue = voicefill.catalogue_from_customer_step(step)
    transcript = body.transcript.strip()[:voicefill.MAX_TRANSCRIPT_CHARS]
    if not transcript:
        raise HTTPException(400, "Nothing to extract from")
    try:
        result = await voicefill.extract(
            session, case=case, user=_CustomerActor(body.customer_id),
            transcript=transcript, speakers=[], catalogue=catalogue)
    except ProcessingRefused as e:        # no lawful basis for ai_processing
        raise HTTPException(403, f"consent:{e.purpose}") from None
    except PermissionError as e:          # tenant has not opted into case AI
        raise HTTPException(400, str(e)) from None
    except RuntimeError as e:             # no backend — honest, never a silent []
        raise HTTPException(503, str(e)) from None

    await _audit_customer(session, case.id, "portal_voicefill_extracted", body.customer_id,
                          {"step_id": body.step_id,
                           "fields_considered": result["fields_considered"],
                           "proposed_fields": [c["field_key"] for c in result["candidates"]],
                           "external_ai": result["external_ai"]})
    await session.commit()
    return {"candidates": result["candidates"],
            "fields_considered": result["fields_considered"],
            "external_ai": result["external_ai"]}


# ── 7. a sign-in / verification code by email ─────────────────────────────────

class OtpEmailIn(BaseModel):
    #: register / login: a customer account (10 min); bank_details: a case (15 min).
    purpose: str = Field(pattern="^(register|login|bank_details)$")
    to: str = Field(min_length=3, max_length=320)
    code: str = Field(pattern=r"^\d{6}$")
    case_id: uuid.UUID | None = None


async def otp_email(session: AsyncSession, body: OtpEmailIn) -> dict:
    """Composed here, never by the caller: there is no route that sends what a
    caller wrote. Delivery failures are logged, never raised — as before."""
    from case_service.mail import EmailService
    from velaris_shared.db.models import EmailAccountModel

    try:
        account = (await session.execute(
            select(EmailAccountModel).where(
                EmailAccountModel.is_default_outbound.is_(True),
                EmailAccountModel.is_active.is_(True),
            ).limit(1)
        )).scalar_one_or_none()
        if account is None:
            if body.purpose == "bank_details":
                # No SMTP account configured — log OTP for development visibility.
                log.warning(
                    "SD-5: No default outbound email account configured. "
                    "OTP for case %s: %s (development fallback — do not use in production)",
                    body.case_id, body.code)
            else:
                log.warning("P65 OTP (no SMTP configured) for %s: %s", body.to, body.code)
            return {"sent": False}
        if body.purpose == "bank_details":
            subject = "Your secure verification code"
            text = (f"Your one-time verification code is: {body.code}\n\n"
                    "This code expires in 15 minutes and can only be used once.\n"
                    "Do not share this code with anyone.")
        else:
            action = "verify your account" if body.purpose == "register" else "log in to your account"
            subject = "Your verification code"
            text = (f"Your one-time code to {action} is: {body.code}\n\n"
                    "This code expires in 10 minutes and can only be used once.\n"
                    "Do not share this code with anyone.")
        await EmailService().send(
            session, case_id=body.case_id if body.purpose == "bank_details" else None,
            account=account, to_addresses=[body.to], subject=subject, body_text=text,
            secret_body=True)
        if body.purpose == "bank_details":
            await session.commit()
        return {"sent": True}
    except Exception as exc:  # noqa: BLE001
        log.warning("OTP email delivery failed for %s: %s", body.to, exc)
        return {"sent": False}


#: path (under /internal/v1/) -> (request model, handler)
ROUTES: dict[str, tuple[type[BaseModel], Any]] = {
    "portal/cases":              (PortalCaseIn, open_case),
    "portal/steps/complete":     (StepCompleteIn, complete_step),
    "portal/case-vars":          (CaseVarsIn, case_vars_for),
    "portal/meet/join":          (MeetJoinIn, meet_join),
    "portal/customers/export":   (CustomerIn, customer_export),
    "portal/customers/erase":    (EraseIn, customer_erase),
    "portal/voice/status":       (VoiceStatusIn, voice_status),
    "portal/voice/transcribe":   (TranscribeIn, voice_transcribe),
    "portal/voice/fill":         (FillIn, voice_fill),
    "portal/otp-email":          (OtpEmailIn, otp_email),
}
