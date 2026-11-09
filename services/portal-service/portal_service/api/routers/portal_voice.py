"""Voice input P3b — speech on the customer portal.

The worker-side mic (``stt.py``) and form-fill (``voicefill.py``) are gated by
HxGuard case actions on an ``AuthenticatedUser``. A portal customer has none of
that: a 24 h HS256 customer JWT, no directory row, no role, no PDP path. So the
portal gets its own endpoints authorized the portal's own way — the
account↔case link, with a uniform 404 — rather than a widened worker route.

Consent inverts here too. A worker dictating is their own voice, transcribed
and discarded, which is why P1/P3 carry no per-tenant opt-in. A customer
dictating is the tenant capturing an *outside person's* voice, so this path
adds the gate that was deferred: ``settings.portal.voice_input``, default OFF,
plus an explicit in-UI acknowledgement recorded once per case as an audit row.

Still transcribe-and-discard: the clip dies with the request, nothing is
stored, and the fill endpoint cannot write a case value — it proposes, the
customer accepts into their own form, and the existing action-complete path
saves. Sealed HxMeet transcripts are NOT reachable from here (no ``session_id``
input): there is no customer equivalent of ``meet.recording.view``.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from portal_service.api.routers.portal_customers import (
    _require_customer,
    _require_customer_accounts,
)
from velaris_shared.db.models import (
    CaseTypeModel,
    PortalCustomerModel,
    TenantModel,
)
from velaris_shared.core.error_codes import CONSENT_REFUSED_403, coded
from velaris_shared.db.session import get_session
from velaris_shared.llm.guard import _RateLimiter

#: Voice on the portal rides customer accounts — a customer JWT is the only
#: way in, so the same release gate applies as to every other account route.
from portal_service.tenant_open import refuse_closed_portal

router = APIRouter(prefix="/portal", tags=["portal-voice"],
                   dependencies=[Depends(refuse_closed_portal), Depends(_require_customer_accounts)])

#: Lower than the worker's 30/min: a customer answering one card is not
#: dictating all day, and transcription serializes behind one process lock.
_stt_limiter = _RateLimiter(max_calls=15, window_seconds=60)
_fill_limiter = _RateLimiter(max_calls=10, window_seconds=60)

#: 45 s, not the worker's 60. A customer answering one field does not need
#: more, and 45 s of PCM16 @16 kHz is ~1.4 MB — a quarter off the worst-case
#: upload on a phone's uplink. Enforced here, not only in the browser.
MAX_CLIP_SECONDS = 45
#: Speech recognition and form-fill run in case-service (one ASR model, on its
#: GPU — R5e step 8); 16 kHz mono PCM16 is what it takes.
SAMPLE_RATE = 16_000
MAX_PCM_BYTES = MAX_CLIP_SECONDS * SAMPLE_RATE * 2
MIN_PCM_BYTES = int(SAMPLE_RATE * 2 * 0.2)

MAX_TEXT_CHARS = 8_000


class FillRequest(BaseModel):
    """A dictation the portal mic already transcribed, and the card it targets.

    There is deliberately no ``session_id``: sealed session transcripts stay on
    the worker side, behind ``meet.recording.view``.
    """
    text:    str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    step_id: str = Field(min_length=1, max_length=256)


def _require_flag(name: str) -> None:
    from velaris_shared.core.feature_flags import is_feature_enabled

    if not is_feature_enabled(name):
        raise HTTPException(404, "Not found")


async def _tenant_for(session: AsyncSession, slug: str) -> TenantModel:
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "Not found")
    return tenant


def _voice_opted_in(tenant: TenantModel) -> bool:
    """Per-tenant consent gate — silence means OFF.

    Recording a customer is the tenant's decision to make and defend, so it is
    never on by default the way worker dictation is. A portal that has been
    switched off entirely cannot be talked to either, whatever this flag says.
    """
    portal = (tenant.settings or {}).get("portal") or {}
    return bool(portal.get("enabled", False)) and bool(portal.get("voice_input", False))


def _consent_text(tenant: TenantModel, external_ai: bool) -> str:
    """What the customer is told before their first recording.

    The default has to track ``egress``: transcription is always local, but
    with ``external_allowed`` the *text* is then sent to a third-party model
    for form-fill. Promising "your own servers" in that configuration would be
    a false statement made on the tenant's behalf, so the wording changes with
    the setting rather than staying comfortable.
    """
    portal = (tenant.settings or {}).get("portal") or {}
    if custom := portal.get("voice_consent_text"):
        return custom
    base = (f"Your voice is sent to {tenant.name}'s own servers, turned into text there, "
            "and the recording is discarded immediately.")
    if external_ai:
        return (base + " The text is then processed by an external AI provider, with "
                "personal details removed first, to suggest answers. Only what you "
                "accept is added to your answer.")
    return base + " Nothing is kept, and only the text you accept is added to your answer."


async def _customer_case(slug: str, case_id: uuid.UUID,
                         customer: PortalCustomerModel, session: AsyncSession,
                         tenant: TenantModel):
    """The customer's own, still-open case — uniform 404 for anything else.

    Two checks beyond the account↔case link:

    * **the case must belong to the portal's tenant.** The opt-in and consent
      wording are read off the portal's tenant while sovereignty and content
      belong to the case's — so a link that ever spanned tenants would let one
      tenant's consent decision govern another tenant's data. Auto-linking is
      tenant-scoped today; this makes that a guarantee rather than a habit.
    * **the case must be open.** A closed case shows no action cards, so voice
      on one is spend with nothing to fill.
    """
    from portal_service.api.routers.portal import _linked_case
    from velaris_shared.tenancy.identity import same_tenant

    case = await _linked_case(slug, case_id, customer, session)
    if not await same_tenant(session, case.tenant_id, tenant.id):
        raise HTTPException(404, "Case not found")
    from velaris_shared.case_actions import statuses as _st
    if await _st.is_terminal_for_case(session, case):
        raise HTTPException(404, "Case not found")
    return case


async def _customer_step(session: AsyncSession, case, step_id: str) -> dict:
    """The pending customer step this dictation is for.

    Same lookup ``account_complete_action`` uses, so voice can never reach a
    card the customer is not actually being shown.
    """
    from portal_service.api.routers.portal import _customer_steps
    ct = await session.get(CaseTypeModel, case.case_type_id)
    step = next((s for s in _customer_steps(ct, case.current_stage_id)
                 if s.get("id") == step_id), None)
    if step is None:
        raise HTTPException(404, "Action not found")
    return step


# ── status ───────────────────────────────────────────────────────────────────

@router.get("/{slug}/account/voice/status")
async def portal_voice_status(
    slug: str,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Should the portal offer a mic, and may it offer form-fill too?

    The browser has its own veto (secure context, AudioWorklet, permission) —
    this answers only the half the server owns.
    """
    _require_flag("voice_input")
    from portal_service.api.routers.portal import _internal

    tenant = await _tenant_for(session, slug)
    opted_in = _voice_opted_in(tenant)
    got = await _internal("portal/voice/status", {"slug": slug, "opted_in": opted_in}, session)

    return {
        "available":        opted_in and got["asr_available"],
        "fill_available":   got["fill_available"],
        "consent_text":     _consent_text(tenant, got["external"]) if opted_in else None,
        "engine":           got["engine"],
        "realtime":         got["realtime"],
        "max_clip_seconds": MAX_CLIP_SECONDS,
    }


# ── transcribe ───────────────────────────────────────────────────────────────

@router.post("/{slug}/account/cases/{case_id}/voice/transcribe")
async def portal_transcribe(
    slug: str,
    case_id: uuid.UUID,
    request: Request,
    consent: bool = False,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One PCM16 clip in, text out. The clip is never stored.

    ``consent=true`` on the first call of a case records the acknowledgement
    the UI just showed. The browser's localStorage only suppresses re-asking;
    this row is the record the tenant can actually stand behind.
    """
    _require_flag("voice_input")

    # Ahead of any DB work: a caller over quota must not be able to spend the
    # server's lookups getting told so.
    allowed, retry_after = _stt_limiter.is_allowed(str(customer.id))
    if not allowed:
        raise HTTPException(429, f"Too many voice requests — retry in {retry_after}s")

    tenant = await _tenant_for(session, slug)
    if not _voice_opted_in(tenant):
        raise HTTPException(404, "Not found")

    case = await _customer_case(slug, case_id, customer, session, tenant)

    pcm = await request.body()
    if len(pcm) > MAX_PCM_BYTES:
        raise HTTPException(413, f"Clip exceeds the {MAX_CLIP_SECONDS}s limit")
    if len(pcm) < MIN_PCM_BYTES or len(pcm) % 2:
        raise HTTPException(400, "Body must be at least 0.2s of 16 kHz mono PCM16 audio")

    # Transcribed in case-service (internal_api/portal.py: voice_transcribe),
    # which records the consent and the dictation — never a word of what was
    # said: the text is the customer's content until they submit it. A server
    # without speech recognition answers 501 from there, as before.
    import base64

    from portal_service.api.routers.portal import _internal

    pcm_b64 = base64.b64encode(pcm).decode("ascii")
    del pcm  # the clip dies with the request — nothing is stored
    got = await _internal("portal/voice/transcribe", {
        "case_id": case.id, "customer_id": customer.id, "pcm_b64": pcm_b64,
        "consent": consent, "clip_seconds_max": MAX_CLIP_SECONDS}, session)
    return {"text": got["text"], "engine": got["engine"], "realtime": got["realtime"]}


# ── form-fill ────────────────────────────────────────────────────────────────

@router.post("/{slug}/account/cases/{case_id}/voice/fill")
async def portal_voice_fill(
    slug: str,
    case_id: uuid.UUID,
    body: FillRequest,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Propose values for ONE customer action card. Writes nothing.

    The catalogue is that card's own visible fields and nothing else, so this
    cannot be used to enumerate the tenant's internal variables.
    """
    _require_flag("voice_fill")

    allowed, retry_after = _fill_limiter.is_allowed(str(customer.id))
    if not allowed:
        raise HTTPException(429, f"Too many extraction requests — retry in {retry_after}s")

    tenant = await _tenant_for(session, slug)
    if not _voice_opted_in(tenant):
        raise HTTPException(404, "Not found")

    case = await _customer_case(slug, case_id, customer, session, tenant)
    await _customer_step(session, case, body.step_id)   # 404 for a card the customer is not shown

    if not body.text.strip():
        raise HTTPException(400, "Nothing to extract from")

    # Extracted in case-service (internal_api/portal.py: voice_fill — HxNexus,
    # the consent gate, the egress audit). 400 / 503 come back as they were.
    from portal_service.api.routers.portal import _internal

    try:
        result = await _internal("portal/voice/fill", {
            "case_id": case.id, "customer_id": customer.id,
            "step_id": body.step_id, "transcript": body.text}, session)
    except HTTPException as e:
        if e.status_code == 403 and str(e.detail).startswith("consent:"):
            # No lawful basis for ai_processing. The portal is the path where a
            # subject is always present, so this is the surface where the gate
            # actually bites. The wording stays neutral: a customer is told the
            # platform will not process, not that their own consent record is
            # the reason a colleague's screen is empty.
            purpose = str(e.detail).split(":", 1)[1]
            raise coded(
                403, CONSENT_REFUSED_403,
                f"Refused: no lawful basis for {purpose!r} on this case.",
            ) from None
        raise

    return {"case_id": str(case_id), "step_id": body.step_id,
            "candidates": result["candidates"],
            "fields_considered": result["fields_considered"],
            "external_ai": result["external_ai"]}
