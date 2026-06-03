"""Knowledge Center API — P40.

Serves structured, plain-English platform knowledge for the Help Center UI.
All data is derived live from the DB — no static files.

Endpoints:
  GET /knowledge/overview          platform summary (counts, health, phases)
  GET /knowledge/case-types        all case types with stages/steps in plain English
  GET /knowledge/glossary          Helix concept glossary (static + DB-derived terms)
  GET /knowledge/modules           modules list with descriptions (from sitemap)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import (
    AccessGroupModel,
    CaseInstanceModel,
    CaseTypeModel,
    FormDefinitionModel,
    PortalModel,
)
from case_service.db.session import get_session
from case_service.api.routers.sitemap import MODULES, PHASES

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

# ── Static glossary ───────────────────────────────────────────────────────────

_GLOSSARY_STATIC = [
    {"term": "Case",            "definition": "A single work item tracked through a lifecycle — e.g. a support ticket, insurance claim, or onboarding request."},
    {"term": "Case Type",       "definition": "A template that defines the stages, steps, forms, and SLA rules for a category of cases."},
    {"term": "Stage",           "definition": "A major phase in the case lifecycle (e.g. Intake → Review → Resolution). Cases move forward through stages."},
    {"term": "Step",            "definition": "A task within a stage — can be a user form, an approval, a document upload request, or an automated action."},
    {"term": "Operator",        "definition": "A staff member who works on cases inside Helix Studio."},
    {"term": "Access Group",    "definition": "A bundle of roles and portal assignment for a group of operators — equivalent to Pega's Access Group."},
    {"term": "Portal",          "definition": "The UI layout shown to operators in an access group — Staff Studio, Customer Portal, Manager view, etc."},
    {"term": "Role / Privilege","definition": "A named set of permissions (e.g. 'Claims Adjuster') that controls which case types and actions an operator can perform."},
    {"term": "Work Center",     "definition": "The operator's personal inbox — shows all cases with active steps assigned to them."},
    {"term": "Perform View",    "definition": "The simplified screen shown to the assigned operator — just their step form, nothing else."},
    {"term": "Review View",     "definition": "Read-only screen shown to managers — all completed step forms and audit trail."},
    {"term": "Case 360",        "definition": "Full lifecycle view shown to case owners and admins — all stages, steps, history, and assignments."},
    {"term": "SLA",             "definition": "Service Level Agreement — the deadline by which a case must be resolved. Breaches trigger escalations."},
    {"term": "HxNexus",         "definition": "The AI copilot embedded in Helix — provides case suggestions, document Q&A, and conversational help."},
    {"term": "HxStream",        "definition": "Live execution and interaction stream — shows every event, click, stage transition, and AI call in real time."},
    {"term": "Form Builder",    "definition": "The drag-and-drop tool for creating step forms — supports text, select, rating, signature, file upload, and more."},
    {"term": "Queue",           "definition": "A pool of unassigned cases that operators pick from. Cases can be routed to queues by rules."},
    {"term": "Escalation Tree", "definition": "A visual tree of escalation actions that fire when an SLA is breached — reassignments, notifications, priority bumps."},
    {"term": "Tracking Token",  "definition": "A unique code given to a customer when they submit a request — lets them track status without logging in."},
    {"term": "Tenant",          "definition": "An organisation within Helix. All data (cases, users, portals) is isolated per tenant."},
    {"term": "Migration",       "definition": "A numbered SQL file that evolves the database schema. Applied in order, never via ORM auto-create."},
    {"term": "Phase",           "definition": "A development milestone in Helix. Each phase ships a complete feature from migration → API → tests → Studio UI."},
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _latest_versions(cts: list) -> list:
    """Return only the latest version for each case type name.

    Versions are compared lexicographically after zero-padding each numeric
    segment, which correctly orders 1.0.0 < 1.0.1 < 1.1.0 < 2.0.0.
    """
    def _ver_key(v: str) -> tuple:
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0,)

    best: dict[str, object] = {}
    for ct in cts:
        name = ct.name
        if name not in best or _ver_key(ct.version) > _ver_key(best[name].version):  # type: ignore[attr-defined]
            best[name] = ct
    return list(best.values())


def _step_type_label(step_type: str) -> str:
    return {
        "user_task":        "Form — operator fills in a form",
        "approval":         "Approval — approve or reject with a reason",
        "document_request": "Document — upload required before advancing",
        "automated":        "Automated — runs without operator input",
    }.get(step_type, step_type.replace("_", " ").title())


def _summarise_case_type(ct: CaseTypeModel) -> dict:
    definition = ct.definition_json or {}
    stages = definition.get("stages", [])
    stage_summaries = []
    for stage in stages:
        steps = stage.get("steps", [])
        step_summaries = [
            {
                "id":    s.get("id", ""),
                "name":  s.get("name", s.get("id", "")),
                "type":  _step_type_label(s.get("step_type", "user_task")),
                "required": s.get("required", False),
            }
            for s in steps
        ]
        stage_summaries.append({
            "id":    stage.get("id", ""),
            "name":  stage.get("name", stage.get("id", "")),
            "order": stage.get("order", 0),
            "steps": step_summaries,
            "step_count": len(steps),
            "required_steps": sum(1 for s in steps if s.get("required", False)),
        })

    sla_policies = definition.get("sla_policies", [])

    stage_names = ", ".join(s.get("name", s.get("id", "")) for s in stages)
    auto_summary = (
        f"{len(stages)} stage{'s' if len(stages) != 1 else ''}: {stage_names}."
        + (f" {len(sla_policies)} SLA rule{'s' if len(sla_policies) != 1 else ''} applied." if sla_policies else "")
    )
    return {
        "id":            str(ct.id),
        "name":          ct.name,
        "version":       ct.version,
        "description":   ct.description.strip() if ct.description and ct.description.strip() else "",
        "color":         ct.color or "",
        "portal_enabled": ct.portal_enabled,
        "stage_count":   len(stages),
        "stages":        stage_summaries,
        "sla_count":     len(sla_policies),
        "plain_english": ct.description.strip() if ct.description and ct.description.strip() else auto_summary,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/overview")
async def knowledge_overview(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Platform summary — counts, phase history, module list."""
    case_type_count = (await session.execute(
        select(func.count()).select_from(CaseTypeModel)
    )).scalar_one()

    form_count = (await session.execute(
        select(func.count()).select_from(FormDefinitionModel)
    )).scalar_one()

    active_case_count = (await session.execute(
        select(func.count()).select_from(CaseInstanceModel).where(
            CaseInstanceModel.status.in_(["new", "open", "in_progress", "pending"])
        )
    )).scalar_one()

    portal_count = (await session.execute(
        select(func.count()).select_from(PortalModel)
    )).scalar_one()

    access_group_count = (await session.execute(
        select(func.count()).select_from(AccessGroupModel)
    )).scalar_one()

    completed_phases = [p for p in PHASES if p["complete"]]

    return {
        "platform": "HELIX BPM",
        "stats": {
            "case_types":    case_type_count,
            "forms":         form_count,
            "active_cases":  active_case_count,
            "portals":       portal_count,
            "access_groups": access_group_count,
            "phases_shipped": len(completed_phases),
            "modules":       len(MODULES),
        },
        "phases": completed_phases,
        "quick_start": [
            {"step": 1, "action": "Create a Case Type",    "path": "/case-designer",  "description": "Define the lifecycle stages, steps, and forms for your process."},
            {"step": 2, "action": "Build a Form",          "path": "/form-builder",   "description": "Create the forms that operators fill in at each step."},
            {"step": 3, "action": "Open a Case",           "path": "/cases",          "description": "Submit a case and watch it move through the lifecycle."},
            {"step": 4, "action": "Work your Assignment",  "path": "/work-center",    "description": "Pick up your assigned tasks and complete the step forms."},
            {"step": 5, "action": "Monitor Live Activity", "path": "/hxstream",       "description": "Watch every event and user interaction in real time via HxStream."},
        ],
    }


@router.get("/case-types")
async def knowledge_case_types(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """All case types described in plain English — stages, steps, SLAs (latest version per name)."""
    cts = _latest_versions((await session.execute(select(CaseTypeModel))).scalars().all())
    return {
        "total": len(cts),
        "case_types": [_summarise_case_type(ct) for ct in cts],
    }


@router.get("/glossary")
async def knowledge_glossary(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Helix concept glossary — static definitions + live DB-derived terms."""
    # Augment with live DB terms — one entry per case type name (latest version)
    cts = _latest_versions((await session.execute(select(CaseTypeModel))).scalars().all())
    portals = (await session.execute(select(PortalModel))).scalars().all()
    groups = (await session.execute(select(AccessGroupModel))).scalars().all()

    live_terms = []
    for ct in cts:
        stages = (ct.definition_json or {}).get("stages", [])
        stage_names = ", ".join(s.get("name", s.get("id", "")) for s in stages) if stages else "—"
        auto = (
            f"{len(stages)} stage{'s' if len(stages) != 1 else ''}: {stage_names}."
            + (" Available on the Customer Portal." if ct.portal_enabled else "")
        )
        live_terms.append({
            "term":       ct.name,
            "definition": ct.description.strip() if ct.description and ct.description.strip() else auto,
            "category":   "case_type",
            "meta":       auto,
        })
    for p in portals:
        live_terms.append({
            "term":       p.name,
            "definition": f"Portal type: {p.portal_type}. Modules: {', '.join(p.modules or []) or 'none configured'}.",
            "category":   "portal",
            "meta":       "",
        })
    for g in groups:
        live_terms.append({
            "term":       g.name,
            "definition": f"Access group — Portal: {g.portal_id}.",
            "category":   "access_group",
            "meta":       "",
        })

    static = [{"category": "concept", **t} for t in _GLOSSARY_STATIC]

    return {
        "total": len(static) + len(live_terms),
        "glossary": static + live_terms,
    }


@router.get("/modules")
async def knowledge_modules(
    _: AuthenticatedUser = Depends(get_current_user),
):
    """All Studio modules with descriptions — same source as Sitemap."""
    by_category: dict[str, list] = {}
    for m in MODULES:
        cat = m["category"]
        by_category.setdefault(cat, [])
        by_category[cat].append({
            "label":       m["label"],
            "path":        m["path"],
            "description": m["description"],
            "phase":       m["phase"],
        })
    return {
        "total": len(MODULES),
        "by_category": by_category,
    }
