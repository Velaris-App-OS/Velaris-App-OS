"""Compliance evidence pack generators (SOC2 + ISO27001).

Generates a JSON evidence pack and a structured PDF summary.
PDF generation uses reportlab (BSD licensed).
"""
from __future__ import annotations
import io
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import false, or_, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.compliance.audit_chain import verify_chain, chain_status

log = logging.getLogger(__name__)


FRAMEWORKS = {
    "soc2": {
        "name": "SOC 2 Type II",
        "controls": [
            "CC6.1 Logical access controls",
            "CC6.2 Authentication",
            "CC6.3 Authorization",
            "CC7.2 System monitoring (audit log integrity)",
            "CC7.3 Anomaly detection (failed events)",
            "CC8.1 Change management (data lineage)",
            "P5.2 Privacy disclosure (GDPR requests)",
        ],
    },
    "iso27001": {
        "name": "ISO/IEC 27001:2022",
        "controls": [
            "A.5.15 Access control",
            "A.5.34 Privacy and protection of PII",
            "A.8.15 Logging",
            "A.8.16 Monitoring activities",
            "A.8.32 Change management",
            "A.5.30 ICT readiness for business continuity",
        ],
    },
}


# ── Tenant scope ─────────────────────────────────────────────────────
#
# Until 2026-10-11 every collector below read the whole install, whoever asked:
# a tenant's admin got a pack naming every tenant's admins by email. `None` is
# still the whole install — the platform operator's pack, and what every direct
# caller got before the scope existed.

@dataclass(frozen=True)
class _Scope:
    #: Every spelling of the tenant a string `tenant_id` column may hold.
    tokens: tuple[str, ...]
    #: The tenant's id for GUID columns; None when the token names no tenant
    #: row, and such a scope matches nothing in them.
    tenant_uuid: uuid.UUID | None


async def _scope_for(session: AsyncSession, tenant_id: str | None) -> _Scope | None:
    if not tenant_id:
        return None
    from velaris_shared.tenancy.identity import resolve_tenant

    resolved = await resolve_tenant(session, str(tenant_id))
    tokens = {str(tenant_id)}
    if resolved is not None:
        tokens |= {str(resolved[0]), str(resolved[1])}
    return _Scope(tuple(sorted(tokens)), resolved[0] if resolved else None)


def _tenant_user_ids(scope: _Scope):
    """The tenant's people: its directory rows, and its memberships."""
    from velaris_shared.db.models import TenantMembershipModel, UserDirectoryModel

    mine = UserDirectoryModel.tenant_id.in_(scope.tokens)
    if scope.tenant_uuid is not None:
        mine = or_(mine, UserDirectoryModel.user_id.in_(
            select(TenantMembershipModel.user_id).where(
                TenantMembershipModel.tenant_id == scope.tenant_uuid)))
    return mine


# ── Collectors ───────────────────────────────────────────────────────

async def _collect_audit_integrity(session: AsyncSession, scope: _Scope | None = None) -> dict:
    if scope is not None:
        # One chain per install. A tenant is told whether it verifies; the
        # install's row counts and the broken rows' ids are not its to read.
        # A shared result at most 5 min old (2026-11-06): any tenant admin can
        # generate packs, and a full verification reads the whole install.
        from case_service.compliance.audit_chain import verify_chain_recent

        verify = await verify_chain_recent(session)
        return {
            "control": "Audit log integrity",
            "verification": {
                "verified": verify["verified"],
                "chain_length": verify["chain_length"],
                "breaks_count": len(verify["breaks"]),
                "pruned_by_retention": verify.get("pruned", 0),
                "verified_at": verify.get("verified_at"),
            },
        }
    verify = await verify_chain(session)
    status = await chain_status(session)
    return {
        "control": "Audit log integrity",
        "status": status,
        "verification": {
            "verified": verify["verified"],
            "chain_length": verify["chain_length"],
            "breaks_count": len(verify["breaks"]),
            "breaks": verify["breaks"][:50],  # truncate for report
            # Rows retention removed on the record — not breaks.
            "pruned_by_retention": verify.get("pruned", 0),
            "prune_records": verify.get("prunes", [])[:50],
        },
    }


async def _collect_security_events(
    session: AsyncSession, period_start: datetime, period_end: datetime,
    scope: _Scope | None = None,
) -> dict:
    """Aggregated counts of security events in the period."""
    try:
        from velaris_shared.db.models import SecurityEventModel
    except ImportError:
        return {"control": "Security event monitoring", "available": False}

    in_period = [SecurityEventModel.timestamp.between(period_start, period_end)]
    if scope is not None:
        # `security_events` has no tenant column: a tenant's events are the ones
        # its own people caused. An event with no actor is the platform's.
        from velaris_shared.db.models import UserDirectoryModel
        in_period.append(SecurityEventModel.user_id.in_(
            select(UserDirectoryModel.user_id).where(_tenant_user_ids(scope))))

    # SUM(repeat_count), not COUNT(*): one row can stand for many refused
    # repeats (migration 231). Every older row is 1.
    n = func.coalesce(func.sum(SecurityEventModel.repeat_count), 0)
    q_total = select(n).select_from(SecurityEventModel).where(*in_period)
    total = (await session.execute(q_total)).scalar_one()

    q_failed = select(n).select_from(SecurityEventModel).where(
        *in_period,
        SecurityEventModel.outcome.in_(["denied", "error", "failure"]),
    )
    failed = (await session.execute(q_failed)).scalar_one()

    q_by_type = select(
        SecurityEventModel.event_type, n.label("c"),
    ).where(*in_period).group_by(SecurityEventModel.event_type)
    by_type = {r.event_type: int(r.c) for r in (await session.execute(q_by_type)).all()}

    return {
        "control": "Security event monitoring",
        "available": True,
        "total_events": int(total),
        "failed_events": int(failed),
        "by_type": by_type,
    }


async def _collect_access_review(session: AsyncSession, scope: _Scope | None = None) -> dict:
    """Snapshot of users + roles + active assignments — for access review."""
    try:
        from velaris_shared.db.models import UserDirectoryModel
    except ImportError:
        return {"control": "Access review", "available": False}

    q = select(UserDirectoryModel).where(UserDirectoryModel.is_active.is_(True))
    if scope is not None:
        q = q.where(_tenant_user_ids(scope))
    users = (await session.execute(q)).scalars().all()

    roles_dist: dict[str, int] = {}
    admin_users: list[dict] = []
    for u in users:
        for role in (u.roles or []):
            roles_dist[role] = roles_dist.get(role, 0) + 1
            if role == "admin":
                admin_users.append({
                    "user_id": u.user_id,
                    "email": u.email,
                    "display_name": u.display_name,
                })

    return {
        "control": "Access review",
        "available": True,
        "active_users": len(users),
        "roles_distribution": roles_dist,
        "admin_users": admin_users,
    }


async def _collect_gdpr_log(
    session: AsyncSession, period_start: datetime, period_end: datetime,
    scope: _Scope | None = None,
) -> dict:
    try:
        from velaris_shared.db.models import GDPRRequestModel
    except ImportError:
        return {"control": "GDPR requests", "available": False}

    q = (
        select(GDPRRequestModel)
        .where(GDPRRequestModel.created_at.between(period_start, period_end))
        .order_by(GDPRRequestModel.created_at.desc())
    )
    if scope is not None:
        q = q.where(GDPRRequestModel.tenant_id == scope.tenant_uuid
                    if scope.tenant_uuid is not None else false())
    rows = (await session.execute(q)).scalars().all()
    return {
        "control": "GDPR requests",
        "available": True,
        "total": len(rows),
        "by_type": _group_count(rows, "request_type"),
        "by_status": _group_count(rows, "status"),
    }


async def _collect_audit_summary(
    session: AsyncSession, period_start: datetime, period_end: datetime,
    scope: _Scope | None = None,
) -> dict:
    from velaris_shared.db.models import CaseAuditLogModel, CaseInstanceModel
    q = select(
        CaseAuditLogModel.action, func.count().label("c"),
    ).where(
        CaseAuditLogModel.timestamp.between(period_start, period_end),
    )
    if scope is not None:
        # The tenant's own cases. A case with no tenant is not counted for it.
        q = q.where(CaseAuditLogModel.case_id.in_(
            select(CaseInstanceModel.id).where(
                CaseInstanceModel.tenant_id == scope.tenant_uuid))
            if scope.tenant_uuid is not None else false())
    q = q.group_by(CaseAuditLogModel.action)
    by_action = {r.action: int(r.c) for r in (await session.execute(q)).all()}
    return {
        "control": "Change management (audit summary)",
        "by_action": by_action,
        "total_actions": sum(by_action.values()),
    }


def _group_count(rows, attr: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        v = getattr(r, attr, None) or "unknown"
        out[v] = out.get(v, 0) + 1
    return out


# ── Generation ───────────────────────────────────────────────────────

async def generate_evidence_pack(
    session: AsyncSession,
    *,
    framework: str,
    period_start: datetime,
    period_end: datetime,
    generated_by: str | None = None,
    cadence: str = "on_demand",
    tenant_id: str | None = None,
) -> dict:
    """Generate a JSON + PDF evidence pack. Returns metadata + bytes.

    `tenant_id` (slug or id) builds the pack from that tenant's rows only.
    None is the whole install.
    """
    if framework not in FRAMEWORKS:
        raise ValueError(f"unknown framework: {framework}")

    fw = FRAMEWORKS[framework]
    scope = await _scope_for(session, tenant_id)
    integrity = await _collect_audit_integrity(session, scope)
    sec = await _collect_security_events(session, period_start, period_end, scope)
    access = await _collect_access_review(session, scope)
    gdpr = await _collect_gdpr_log(session, period_start, period_end, scope)
    audit = await _collect_audit_summary(session, period_start, period_end, scope)

    pack = {
        "framework": framework,
        "framework_name": fw["name"],
        "controls_covered": fw["controls"],
        "period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": generated_by,
        "tenant_id": tenant_id,
        "evidence": {
            "audit_integrity": integrity,
            "security_events": sec,
            "access_review": access,
            "gdpr_requests": gdpr,
            "audit_summary": audit,
        },
    }

    summary = {
        "chain_verified": integrity["verification"]["verified"],
        "chain_length": integrity["verification"]["chain_length"],
        "audit_actions": audit.get("total_actions", 0),
        "security_events": sec.get("total_events", 0),
        "failed_security_events": sec.get("failed_events", 0),
        "active_users": access.get("active_users", 0),
        "admin_count": len(access.get("admin_users", [])),
        "gdpr_requests": gdpr.get("total", 0),
    }

    json_bytes = json.dumps(pack, indent=2, default=str).encode("utf-8")
    pdf_bytes = _render_pdf(framework, fw, period_start, period_end, summary, pack)

    return {
        "pack": pack,
        "summary": summary,
        "json_bytes": json_bytes,
        "pdf_bytes": pdf_bytes,
        "chain_verified": integrity["verification"]["verified"],
    }


def _render_pdf(
    framework: str, fw: dict,
    period_start: datetime, period_end: datetime,
    summary: dict, pack: dict,
) -> bytes:
    """Simple structured PDF using reportlab."""
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
    except ImportError:
        log.warning("reportlab not available — returning empty PDF")
        return b"%PDF-1.4\n%%EOF\n"

    PAGE_W, PAGE_H = letter  # 612 x 792 pts
    MARGIN = 50
    # reportlab y=0 is bottom; we track from top downward
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = PAGE_H - MARGIN

    def write(text: str, size: int = 10, bold: bool = False, color: tuple = (0, 0, 0)) -> None:
        nonlocal y
        line_h = size + 6
        if y < MARGIN + size:
            c.showPage()
            nonlocal_reset()
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.setFillColorRGB(*color)
        c.drawString(MARGIN, y, text)
        y -= line_h

    def nonlocal_reset() -> None:
        nonlocal y
        y = PAGE_H - MARGIN

    write("Velaris Compliance Evidence Pack", size=18, bold=True)
    write(fw["name"], size=14)
    write(f"Period: {period_start.date()} to {period_end.date()}", size=10)
    write(f"Generated: {datetime.now(timezone.utc).isoformat()}", size=9, color=(0.4, 0.4, 0.4))
    y -= 12

    integrity_color = (0, 0.5, 0) if summary["chain_verified"] else (0.7, 0, 0)
    write(
        f"Audit Chain: {'VERIFIED' if summary['chain_verified'] else 'BREAKS DETECTED'}",
        size=12, bold=True, color=integrity_color,
    )
    write(f"  Chain length: {summary['chain_length']} sealed rows", size=9)
    y -= 10

    write("Summary", size=13, bold=True)
    for k, v in summary.items():
        write(f"  {k.replace('_', ' ').title()}: {v}", size=10)
    y -= 8

    write("Controls covered", size=13, bold=True)
    for ctrl in fw["controls"]:
        write(f"  - {ctrl}", size=9)
    y -= 8

    for section_name, section in pack["evidence"].items():
        write(section_name.replace("_", " ").title(), size=12, bold=True)
        if section.get("available") is False:
            write("  (not available - module not enabled)", size=9, color=(0.5, 0.5, 0.5))
            continue
        for k, v in section.items():
            if k in ("control", "available"):
                continue
            display = json.dumps(v, default=str) if not isinstance(v, (str, int, float, bool)) else str(v)
            if len(display) > 100:
                display = display[:97] + "..."
            write(f"  {k}: {display}", size=9)
        y -= 6

    c.save()
    return buf.getvalue()
