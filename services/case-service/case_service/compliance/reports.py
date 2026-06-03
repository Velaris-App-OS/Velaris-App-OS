"""Compliance evidence pack generators (SOC2 + ISO27001).

Generates a JSON evidence pack and a structured PDF summary.
PDF generation uses reportlab (BSD licensed).
"""
from __future__ import annotations
import io
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func
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


# ── Collectors ───────────────────────────────────────────────────────

async def _collect_audit_integrity(session: AsyncSession) -> dict:
    status = await chain_status(session)
    verify = await verify_chain(session)
    return {
        "control": "Audit log integrity",
        "status": status,
        "verification": {
            "verified": verify["verified"],
            "chain_length": verify["chain_length"],
            "breaks_count": len(verify["breaks"]),
            "breaks": verify["breaks"][:50],  # truncate for report
        },
    }


async def _collect_security_events(
    session: AsyncSession, period_start: datetime, period_end: datetime,
) -> dict:
    """Aggregated counts of security events in the period."""
    try:
        from case_service.db.models import SecurityEventModel
    except ImportError:
        return {"control": "Security event monitoring", "available": False}

    q_total = select(func.count()).select_from(SecurityEventModel).where(
        SecurityEventModel.timestamp.between(period_start, period_end),
    )
    total = (await session.execute(q_total)).scalar_one()

    q_failed = select(func.count()).select_from(SecurityEventModel).where(
        SecurityEventModel.timestamp.between(period_start, period_end),
        SecurityEventModel.outcome.in_(["denied", "error", "failure"]),
    )
    failed = (await session.execute(q_failed)).scalar_one()

    q_by_type = select(
        SecurityEventModel.event_type, func.count().label("c"),
    ).where(
        SecurityEventModel.timestamp.between(period_start, period_end),
    ).group_by(SecurityEventModel.event_type)
    by_type = {r.event_type: int(r.c) for r in (await session.execute(q_by_type)).all()}

    return {
        "control": "Security event monitoring",
        "available": True,
        "total_events": int(total),
        "failed_events": int(failed),
        "by_type": by_type,
    }


async def _collect_access_review(session: AsyncSession) -> dict:
    """Snapshot of users + roles + active assignments — for access review."""
    try:
        from case_service.db.models import UserDirectoryModel
    except ImportError:
        return {"control": "Access review", "available": False}

    q = select(UserDirectoryModel).where(UserDirectoryModel.is_active.is_(True))
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
) -> dict:
    try:
        from case_service.db.models import GDPRRequestModel
    except ImportError:
        return {"control": "GDPR requests", "available": False}

    q = (
        select(GDPRRequestModel)
        .where(GDPRRequestModel.created_at.between(period_start, period_end))
        .order_by(GDPRRequestModel.created_at.desc())
    )
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
) -> dict:
    from case_service.db.models import CaseAuditLogModel
    q = select(
        CaseAuditLogModel.action, func.count().label("c"),
    ).where(
        CaseAuditLogModel.timestamp.between(period_start, period_end),
    ).group_by(CaseAuditLogModel.action)
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
    """Generate a JSON + PDF evidence pack. Returns metadata + bytes."""
    if framework not in FRAMEWORKS:
        raise ValueError(f"unknown framework: {framework}")

    fw = FRAMEWORKS[framework]
    integrity = await _collect_audit_integrity(session)
    sec = await _collect_security_events(session, period_start, period_end)
    access = await _collect_access_review(session)
    gdpr = await _collect_gdpr_log(session, period_start, period_end)
    audit = await _collect_audit_summary(session, period_start, period_end)

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
