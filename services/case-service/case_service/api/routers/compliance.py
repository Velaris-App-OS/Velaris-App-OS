"""Compliance API — audit verification, evidence packs, lineage."""
from __future__ import annotations
import io
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import get_current_user, require_role
from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from case_service.compliance import (
    chain_status, verify_chain, seal_new_entries,
    anchor_chain_tip, list_anchors,
    generate_evidence_pack, FRAMEWORKS,
    get_case_lineage,
)
from velaris_shared.db.models import ComplianceReportModel
from velaris_shared.db.session import get_analytics_session as get_session
# This router's `get_session` is the analytics pool (above): its routed paths
# take the analytics variants, which keep that pool when there is no replica.
from velaris_shared.db.read_routing import (
    get_analytics_cutoff_session as get_cutoff_session,
    get_analytics_read_session as get_read_session,
)

router = APIRouter(prefix="/compliance", tags=["compliance"])


async def _install_chain_operator(session: AsyncSession, user: AuthenticatedUser) -> None:
    """The audit chain is ONE chain for the whole install: its row counts, its
    breaks (other tenants' audit-row ids), a seal, a full verification (61 s at
    1.6M links, vx-load 2026-11-06) and an outside timestamp call are the
    platform operator's, not every tenant admin's (demo sign-ups are tenant
    admins). A tenant reads its own chain result in its evidence pack, which
    already leaves the install's counts and break ids out."""
    from case_service.tenancy.identity import is_platform_operator

    if not await is_platform_operator(session, user):
        raise HTTPException(403, "The install's audit chain is the platform operator's.")


# ── Audit chain ──────────────────────────────────────────────────────

@router.get("/audit/status")
async def get_audit_status(
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.audit.read")),
):
    await _install_chain_operator(session, user)
    return await chain_status(session)


@router.post("/audit/seal")
async def seal_chain(
    max_rows: int = Query(10000, ge=1, le=100000),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.audit.seal")),
):
    await _install_chain_operator(session, user)
    return await seal_new_entries(session, max_rows=max_rows)


@router.get("/audit/verify")
async def verify_audit_chain(
    limit: Optional[int] = Query(None, ge=1, le=1_000_000),
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.audit.read")),
):
    await _install_chain_operator(session, user)
    return await verify_chain(session, limit=limit)


# ── External anchoring (Group I — RFC-3161) ──────────────────────────

@router.get("/audit/anchors")
async def get_audit_anchors(
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.audit.read")),
):
    await _install_chain_operator(session, user)
    return await list_anchors(session, limit=limit)


@router.post("/audit/anchor")
async def trigger_audit_anchor(
    force: bool = Query(False, description="Anchor even if the tip is unchanged"),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.audit.seal")),
):
    await _install_chain_operator(session, user)
    try:
        return await anchor_chain_tip(session, force=force)
    except Exception as e:
        raise HTTPException(502, f"Anchoring failed: {e}")


# ── Evidence packs ───────────────────────────────────────────────────

class GenerateReportBody(BaseModel):
    framework: str
    period_days: int = 30
    period_start: Optional[str] = None
    period_end: Optional[str] = None


@router.get("/frameworks")
async def list_frameworks(
    _user: AuthenticatedUser = Depends(hxguard.guard("compliance.frameworks.read")),
):
    return {k: v["name"] for k, v in FRAMEWORKS.items()}


async def _report_tenant(session: AsyncSession, user: AuthenticatedUser) -> str | None:
    from case_service.tenancy.identity import is_platform_operator

    if await is_platform_operator(session, user):
        return None
    return str(user.tenant_id)


@router.post("/reports/generate")
async def generate_report(
    body: GenerateReportBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.reports.generate")),
):
    if body.framework not in FRAMEWORKS:
        raise HTTPException(400, f"Unknown framework: {body.framework}")

    end = datetime.fromisoformat(body.period_end) if body.period_end else datetime.now(timezone.utc)
    if end.tzinfo is None: end = end.replace(tzinfo=timezone.utc)
    start = (
        datetime.fromisoformat(body.period_start)
        if body.period_start
        else end - timedelta(days=body.period_days)
    )
    if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)

    # Seal anything pending so the report covers all writes up to now
    await seal_new_entries(session)

    # Whose pack this is (2026-10-11). The platform operator's is the whole
    # install, stored with no tenant, exactly as every report was before.
    # Anybody else's is their own tenant's rows and records that tenant — until
    # now a tenant's admin got every tenant's counts and admin e-mail addresses.
    tenant = await _report_tenant(session, user)

    result = await generate_evidence_pack(
        session, framework=body.framework,
        period_start=start, period_end=end,
        generated_by=user.user_id, cadence="on_demand",
        tenant_id=tenant,
    )

    # Persist report metadata. Storage of bytes in document storage is optional
    # (P24 storage backend can be wired in via tenant config); we store
    # the JSON inline in summary if small, otherwise store separately.
    report = ComplianceReportModel(
        id=uuid.uuid4(),
        framework=body.framework,
        period_start=start, period_end=end,
        generated_by=user.user_id,
        summary=result["summary"],
        chain_verified=result["chain_verified"],
        cadence="on_demand",
        tenant_id=tenant,
    )
    session.add(report)
    await session.flush()

    return {
        "report_id": str(report.id),
        "framework": body.framework,
        "summary": result["summary"],
        "chain_verified": result["chain_verified"],
        "json_size_bytes": len(result["json_bytes"]),
        "pdf_size_bytes": len(result["pdf_bytes"]),
        # Bytes are NOT returned in JSON — fetch them via the download endpoints
    }


@router.get("/reports")
async def list_reports(
    framework: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.reports.read")),
):
    q = select(ComplianceReportModel).order_by(ComplianceReportModel.generated_at.desc()).limit(limit)
    if framework:
        q = q.where(ComplianceReportModel.framework == framework)
    # Every tenant's rows until 2026-09-29 (8d). A report with NO tenant is an
    # install-wide pack, and its summary says so in numbers — the operator's.
    from case_service.tenancy.identity import (
        caller_tenant_tokens, is_platform_operator, scope_to_caller,
    )
    if await is_platform_operator(session, user):
        q = await scope_to_caller(q, ComplianceReportModel, session, user)
    else:
        q = q.where(ComplianceReportModel.tenant_id.in_(
            await caller_tenant_tokens(session, user)))
    res = await session.execute(q)
    return [
        {
            "id": str(r.id),
            "framework": r.framework,
            "period_start": r.period_start.isoformat() if r.period_start else None,
            "period_end": r.period_end.isoformat() if r.period_end else None,
            "generated_by": r.generated_by,
            "generated_at": r.generated_at.isoformat() if r.generated_at else None,
            "summary": r.summary,
            "chain_verified": r.chain_verified,
            "cadence": r.cadence,
        }
        for r in res.scalars().all()
    ]


@router.get("/reports/{report_id}/download")
async def download_report(
    report_id: uuid.UUID,
    fmt: str = Query("json", pattern=r"^(json|pdf)$"),
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.reports.read")),
):
    report = await session.get(ComplianceReportModel, report_id)
    if report is None:
        raise HTTPException(404, "Report not found")
    # The path guard already refuses another tenant's report; it passes one
    # with no tenant, which is install-wide and so the operator's alone. Both
    # are checked here as well — MCP tools call this handler directly.
    from case_service.tenancy.identity import is_platform_operator, may_reach_tenant
    if report.tenant_id is None:
        if not await is_platform_operator(session, user):
            raise HTTPException(404, "Report not found")
    elif not await may_reach_tenant(session, user, report.tenant_id):
        raise HTTPException(404, "Report not found")

    # Re-generate fresh bytes for the requested format. Reports are
    # deterministic given period + chain state, so re-generation is safe;
    # this avoids double-storing in DB and works without object storage.
    result = await generate_evidence_pack(
        session, framework=report.framework,
        period_start=report.period_start, period_end=report.period_end,
        generated_by=report.generated_by, cadence=report.cadence,
        tenant_id=report.tenant_id,
    )
    fname = f"helix-{report.framework}-{report.period_start.date()}-to-{report.period_end.date()}.{fmt}"
    if fmt == "json":
        return Response(
            content=result["json_bytes"], media_type="application/json",
            headers={"content-disposition": f'attachment; filename="{fname}"'},
        )
    else:
        return Response(
            content=result["pdf_bytes"], media_type="application/pdf",
            headers={"content-disposition": f'attachment; filename="{fname}"'},
        )


# ── Lineage ──────────────────────────────────────────────────────────

@router.get("/lineage/{case_id}")
async def get_lineage(
    case_id: uuid.UUID,
    limit: int = Query(500, ge=1, le=5000),
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    # Was authentication only until 2026-09-29: another tenant's case now 404s.
    await hxguard.require_case(session, user, "case.read", case_id)
    timeline = await get_case_lineage(session, case_id, limit=limit)
    return {"case_id": str(case_id), "events": timeline, "count": len(timeline)}


# ─── HxConsent — consent & purpose-limitation ledger (§8.1) ────────


@router.post("/consent")
async def record_consent(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.consent.write")),
):
    """Append one consent decision to the ledger (#39 / §8.1).

    Admin/security only: consent records *are* personal data, and the ledger
    is the evidence a regulator would be shown. The portal records a subject's
    own consent through its own submit path, not through here.

    Tenant is taken from the caller, never from the body — a client-supplied
    tenant would let one tenant write into another's ledger.
    """
    from case_service.compliance import consent as consent_svc

    try:
        row = await consent_svc.record(
            session,
            subject_ref=str(body.get("subject_ref") or ""),
            purpose=str(body.get("purpose") or ""),
            action=str(body.get("action") or consent_svc.GRANTED),
            basis=str(body.get("basis") or consent_svc.CONSENT),
            subject_type=str(body.get("subject_type") or "email"),
            source=str(body.get("source") or "admin"),
            evidence=body.get("evidence") or {},
            tenant_id=str(user.tenant_id) if user.tenant_id else None,
            actor_id=user.user_id,
        )
    except consent_svc.ConsentError as exc:
        raise HTTPException(400, str(exc))

    await session.commit()
    return {"id": str(row.id), "purpose": row.purpose, "action": row.action,
            "basis": row.basis, "recorded_at": row.recorded_at.isoformat()}


@router.get("/consent/{subject_ref}")
async def get_consent_state(
    subject_ref: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.consent.read")),
):
    """Current lawful basis per purpose, plus the full ledger (Article 15).

    Tenant-scoped to the caller: a subject may exist in more than one tenant,
    and one tenant must not read another's basis for processing them.
    """
    from case_service.compliance import consent as consent_svc

    tenant = str(user.tenant_id) if user.tenant_id else None
    state = await consent_svc.current_state(session, subject_ref, tenant_id=tenant)
    ledger = await consent_svc.history(session, subject_ref, tenant_id=tenant)

    return {
        "subject_ref": subject_ref.strip().lower(),
        "purposes": state,
        "ledger": [{
            "id": str(r.id),
            "purpose": r.purpose,
            "action": r.action,
            "basis": r.basis,
            "source": r.source,
            "evidence": r.evidence,
            "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
        } for r in ledger],
    }


@router.get("/consent/{subject_ref}/check")
async def check_consent(
    subject_ref: str,
    purpose: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.consent.read")),
):
    """Is processing for `purpose` currently lawful for this subject?

    Fails closed — an unknown purpose or no record at all is False. The absence
    of a recorded basis is not permission.
    """
    from case_service.compliance import consent as consent_svc

    allowed = await consent_svc.has_consent(
        session, subject_ref, purpose,
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )
    return {"subject_ref": subject_ref.strip().lower(), "purpose": purpose,
            "lawful": allowed}


# ─── HxPrivacy — PII discovery & classification (§8.10) ────────────


@router.post("/privacy/scan")
async def run_pii_scan(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.privacy.scan")),
):
    """Scan every form and data model for probable personal data (#39 / §8.10).

    Idempotent: re-running refreshes findings and **never overwrites a human
    verdict**, or each scan would silently re-open every dismissal and the list
    would stop being trusted.
    """
    from case_service.compliance import pii_scan

    out = await pii_scan.scan_all(
        session, tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )
    await session.commit()
    return out


@router.get("/privacy/findings")
async def list_pii_findings(
    status: str | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.privacy.read")),
):
    """Findings awaiting or past review.

    Findings describe schema structure, so this is privileged and
    tenant-scoped — an unscoped list would expose one tenant's field inventory
    to another.
    """
    from sqlalchemy import select as _select

    from case_service.compliance import pii_scan
    from velaris_shared.db.models import PIIFindingModel

    q = _select(PIIFindingModel).order_by(
        PIIFindingModel.special_category.desc(),
        PIIFindingModel.confidence.desc(),
    ).limit(max(1, min(int(limit), 1000)))
    if status:
        q = q.where(PIIFindingModel.status == status)
    # Cross-tenant PII findings are a PLATFORM operator's view: which fields in
    # every tenant's forms hold national ids, health and financial data. Until
    # 2026-09-17 this was `user_allows(compliance.crosstenant.read)` — and
    # `admin` seeds to a wildcard that passes every privilege, so every tenant's
    # admin read every tenant's findings. Superadmin only now.
    from case_service.tenancy.identity import is_superadmin as _is_superadmin
    scope = None
    if not _is_superadmin(user):
        if user.tenant_id:
            # Either spelling of the caller's tenant (slug or id).
            from case_service.tenancy.identity import caller_tenant_tokens
            scope = PIIFindingModel.tenant_id.in_(await caller_tenant_tokens(session, user))
        else:
            # No tenant claim = the default tenant, never every tenant (2026-09-17).
            from sqlalchemy import or_ as _or
            from case_service.tenancy.identity import default_tenant_tokens
            scope = _or(PIIFindingModel.tenant_id.is_(None),
                        PIIFindingModel.tenant_id.in_(await default_tenant_tokens(session)))
        q = q.where(scope)

    rows = (await session.execute(q)).scalars().all()
    return {
        "summary": await pii_scan.summary(session, scope),
        "findings": [{
            "id": str(r.id),
            "resource_type": r.resource_type,
            "resource_name": r.resource_name,
            "field_path": r.field_path,
            "field_label": r.field_label,
            "category": r.category,
            "confidence": r.confidence,
            "reason": r.reason,
            "special_category": r.special_category,
            "status": r.status,
            "reviewed_by": r.reviewed_by,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        } for r in rows],
    }


@router.post("/privacy/findings/{finding_id}/review")
async def review_pii_finding(
    finding_id: uuid.UUID,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.privacy.review")),
):
    """Confirm or dismiss a finding.

    Detection is heuristic and drives retention/residency policy, so a human
    decides before it changes anything — a false positive over-restricts a
    harmless field, a false negative leaves personal data untagged.
    """
    from case_service.compliance import pii_scan

    try:
        row = await pii_scan.review(
            session, finding_id,
            decision=str(body.get("decision") or ""), actor_id=user.user_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    await session.commit()
    return {"id": str(row.id), "status": row.status, "reviewed_by": row.reviewed_by}


# ─── HxLedger — double-entry ledger & reconciliation (§8.4) ────────


@router.post("/ledger/accounts")
async def create_ledger_account(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.ledger.write")),
):
    """Create or fetch a chart-of-accounts entry (#39 / §8.4). Idempotent."""
    from case_service.compliance import ledger

    try:
        acc = await ledger.ensure_account(
            session,
            code=str(body.get("code") or ""),
            name=str(body.get("name") or ""),
            account_type=str(body.get("account_type") or ""),
            currency=str(body.get("currency") or "usd"),
            tenant_id=str(user.tenant_id) if user.tenant_id else None,
        )
    except ledger.LedgerError as exc:
        raise HTTPException(400, str(exc))

    await session.commit()
    return {"id": str(acc.id), "code": acc.code, "account_type": acc.account_type}


@router.post("/ledger/transactions")
async def post_ledger_transaction(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.ledger.write")),
):
    """Post one balanced transaction.

    Rejected outright if debits do not equal credits — an unbalanced ledger is
    worse than none, so it is never stored and corrected later.
    """
    import uuid as _uuid

    from case_service.compliance import ledger

    raw_lines = body.get("lines")
    if not isinstance(raw_lines, list):
        raise HTTPException(400, "lines must be a list")

    lines = []
    for ln in raw_lines:
        if not isinstance(ln, dict):
            raise HTTPException(400, "each line must be an object")
        try:
            lines.append({**ln, "account_id": _uuid.UUID(str(ln.get("account_id")))})
        except (ValueError, TypeError):
            raise HTTPException(400, "each line needs a valid account_id")

    try:
        txn = await ledger.post(
            session, lines=lines,
            description=body.get("description"),
            currency=str(body.get("currency") or "usd"),
            tenant_id=str(user.tenant_id) if user.tenant_id else None,
            posted_by=user.user_id,
        )
    except ledger.LedgerError as exc:
        raise HTTPException(400, str(exc))

    await session.commit()
    return {"transaction_id": str(txn.id), "posted_at": txn.posted_at.isoformat()}


@router.post("/ledger/transactions/{transaction_id}/reverse")
async def reverse_ledger_transaction(
    transaction_id: uuid.UUID,
    body: dict | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.ledger.write")),
):
    """Reverse a transaction by posting its mirror image.

    The original is never edited or deleted, so the books can still be
    reproduced as at any past date.
    """
    from case_service.compliance import ledger

    try:
        txn = await ledger.reverse(
            session, transaction_id, posted_by=user.user_id,
            reason=(body or {}).get("reason"),
        )
    except ledger.LedgerError as exc:
        raise HTTPException(400, str(exc))

    await session.commit()
    return {"reversal_transaction_id": str(txn.id), "reverses": str(transaction_id)}


@router.get("/ledger/trial-balance")
async def get_trial_balance(
    session: AsyncSession = Depends(get_cutoff_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.ledger.read")),
):
    """Per-account totals plus the whole-ledger balance check.

    `balanced: false` means entries were written outside `post()` — the only
    path that guarantees balance — which is worth surfacing loudly.
    """
    from case_service.compliance import ledger

    return await ledger.trial_balance(
        session, tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )


@router.get("/ledger/reconcile")
async def reconcile_ledger(
    session: AsyncSession = Depends(get_cutoff_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.ledger.read")),
):
    """Captured payments vs what the ledger recorded.

    A difference means money moved without a matching book entry — the exact
    failure a ledger exists to catch. Reports; never auto-corrects.
    """
    from case_service.compliance import ledger

    return await ledger.reconcile_payments(
        session, tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )


# ─── HxDR — disaster recovery posture (§8.9) ───────────────────────


@router.post("/dr/backups")
async def record_backup(
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.dr.write")),
):
    """Record a backup that ops has taken (#39 / §8.9).

    A registry, not an executor — taking the dump belongs to the tooling that
    holds the credentials and can block for minutes (`update-velaris.sh` already
    does, before every update). Without the record, "when did we last have a
    good backup" is unanswerable.

    Refuses a residency violation: a backup of a governed region stored
    elsewhere is rejected here, where it is still cheap to fix.
    """
    import uuid as _uuid

    from case_service.compliance import dr

    def _opt_uuid(v):
        try:
            return _uuid.UUID(str(v)) if v else None
        except (ValueError, TypeError):
            raise HTTPException(400, f"invalid uuid: {v!r}")

    try:
        row = await dr.record_backup(
            session,
            location=str(body.get("location") or ""),
            region_id=_opt_uuid(body.get("region_id")),
            stored_region_id=_opt_uuid(body.get("stored_region_id")),
            scope=str(body.get("scope") or "database"),
            status=str(body.get("status") or dr.SUCCEEDED),
            size_bytes=body.get("size_bytes"),
            checksum=body.get("checksum"),
            encrypted=bool(body.get("encrypted", False)),
            error=body.get("error"),
            tenant_id=str(user.tenant_id) if user.tenant_id else None,
            recorded_by=user.user_id,
        )
    except dr.DRError as exc:
        raise HTTPException(400, str(exc))

    await session.commit()
    return {"id": str(row.id), "taken_at": row.taken_at.isoformat(),
            "status": row.status}


@router.get("/dr/backups")
async def list_backups(
    region_id: uuid.UUID | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.dr.read")),
):
    """Recorded backups, newest first.

    `posture` answers "are we covered"; this answers "which backup are we
    talking about", which is what a restore drill has to be recorded against.

    `location` is a filesystem path on the server and is admin-only for the
    same reason HxStatus withholds `detail` from its public route — it
    describes internal layout.
    """
    from sqlalchemy import select as _select

    from velaris_shared.db.models import BackupRecordModel

    q = (
        _select(BackupRecordModel)
        .order_by(BackupRecordModel.taken_at.desc())
        .limit(max(1, min(int(limit), 500)))
    )
    if region_id:
        q = q.where(BackupRecordModel.region_id == region_id)
    # Backup records name storage locations and recovery posture. Another
    # tenant's are none of a tenant admin's business, and a PLATFORM backup (no
    # tenant) describes the whole install's storage — so both are superadmin
    # only. Until 2026-09-17 `admin`'s wildcard privilege passed the cross-tenant
    # check and every tenant's admin read all of them.
    from case_service.tenancy.identity import is_superadmin as _is_superadmin
    if not _is_superadmin(user):
        from case_service.tenancy.identity import default_tenant_tokens
        mine = ([str(user.tenant_id)] if user.tenant_id
                else await default_tenant_tokens(session))
        q = q.where(BackupRecordModel.tenant_id.in_(mine))

    rows = (await session.execute(q)).scalars().all()
    return {
        "backups": [{
            "id": str(r.id),
            "region_id": str(r.region_id) if r.region_id else None,
            "stored_region_id": str(r.stored_region_id) if r.stored_region_id else None,
            "scope": r.scope,
            "status": r.status,
            "location": r.location,
            "size_bytes": r.size_bytes,
            "checksum": r.checksum,
            "encrypted": r.encrypted,
            "error": r.error,
            "taken_at": r.taken_at.isoformat() if r.taken_at else None,
            "restore_tested_at": (
                r.restore_tested_at.isoformat() if r.restore_tested_at else None
            ),
            "restore_test_result": r.restore_test_result,
            "restore_test_duration_seconds": r.restore_test_duration_seconds,
            "recorded_by": r.recorded_by,
        } for r in rows],
        "count": len(rows),
    }


@router.post("/dr/backups/{backup_id}/restore-test")
async def record_restore_test(
    backup_id: uuid.UUID,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.dr.write")),
):
    """Record the outcome of actually restoring a backup.

    This is the column that matters: an untested backup is a hope, not a
    recovery plan. A failed drill is recorded rather than discarded — knowing a
    backup does not restore is worth more than any number of untested ones.
    """
    from case_service.compliance import dr

    raw_duration = body.get("duration_seconds")
    try:
        duration = int(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        raise HTTPException(400, "duration_seconds must be a whole number of seconds")

    try:
        row = await dr.record_restore_test(
            session, backup_id,
            result=str(body.get("result") or ""),
            notes=body.get("notes"), actor_id=user.user_id,
            duration_seconds=duration,
        )
    except dr.DRError as exc:
        raise HTTPException(400, str(exc))

    await session.commit()
    return {"id": str(row.id), "result": row.restore_test_result,
            "tested_at": row.restore_tested_at.isoformat(),
            "duration_seconds": row.restore_test_duration_seconds}


@router.get("/dr/posture")
async def get_dr_posture(
    rpo_hours: int = 24,
    rto_hours: int = 4,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.dr.read")),
):
    """Per-region recovery posture against declared objectives.

    Reports `untested` as its own state rather than folding it into `ok` —
    collapsing them is how organisations discover at the worst possible moment
    that their backups never worked.
    """
    from case_service.compliance import dr
    from case_service.compliance.dr_status import backup_status

    out = await dr.posture(
        session,
        rpo_hours=max(1, min(int(rpo_hours), 24 * 30)),
        rto_hours=max(1, min(int(rto_hours), 24 * 30)),
    )
    # What the backup tooling on this host last did — encryption, off-box copy,
    # timer, proven restore, a private key left on the server. Additive key:
    # existing clients of this response are unaffected.
    out["backup_operations"] = backup_status()
    # The standby server (R5c): link state, lag, last copies, and its history.
    # Additive key, like backup_operations.
    from case_service.compliance.dr_status import standby_status
    standby = standby_status()
    try:
        async with session.begin_nested():
            standby["events"] = await dr.recent_standby_events(session)
    except Exception:   # an install that has not applied migration 222 yet
        standby["events"] = []
    out["standby"] = standby
    return out
