"""Site Map API — static catalog of all Velaris modules.

Provides a central index of every route in the Studio with
descriptions, categories, and API endpoints. Used by the
Help / Site Map page in the UI.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.config import get_settings

router = APIRouter(prefix="/sitemap", tags=["sitemap"], dependencies=[Depends(get_current_user)])


# The catalogue itself lives in the shared layer: HxGraph's sync reads it
# from its own process since 13d. Re-exported here so this router and every
# existing importer keep working unchanged.
from velaris_shared.core.modules import MODULES  # noqa: E402


PHASES = [
    {"num": 0, "name": "Foundation", "complete": True},
    {"num": 1, "name": "Core Case Management", "complete": True},
    {"num": 2, "name": "BPMN Engine", "complete": True},
    {"num": 3, "name": "Case Types", "complete": True},
    {"num": 4, "name": "Routing & SLAs", "complete": True},
    {"num": 5, "name": "Rules Engine", "complete": True},
    {"num": 6, "name": "Studio UI", "complete": True},
    {"num": 7, "name": "Forms & Calendars", "complete": True},
    {"num": 8, "name": "Analytics & ABAC", "complete": True},
    {"num": 9, "name": "Webhooks", "complete": True},
    {"num": 10, "name": "Admin Console", "complete": True},
    {"num": 11, "name": "Production Hardening", "complete": True},
    {"num": 12, "name": "Auth & SSO", "complete": True},
    {"num": 13, "name": "Dev-mode Login", "complete": True},
    {"num": 14, "name": "Process Mining", "complete": True},
    {"num": 15, "name": "NLP Builder", "complete": True},
    {"num": 16, "name": "Scout", "complete": True},
    {"num": 17, "name": "Multi-tenancy", "complete": True},
    {"num": 18, "name": "React Native Codegen", "complete": True},
    {"num": 19, "name": "AI-Powered Scout", "complete": True},
    {"num": 20, "name": "Enterprise Hardening", "complete": True},
    {"num": 21, "name": "Orchestrator", "complete": True},
    {"num": 22, "name": "Real-time Collaboration", "complete": True},
    {"num": 23, "name": "Observability & Telemetry", "complete": True},
    {"num": 24, "name": "Document Management", "complete": True},
    {"num": 25, "name": "Email Integration", "complete": True},
    {"num": 27, "name": "Mobile Push Notifications", "complete": True},
    {"num": 30, "name": "HxNexus", "complete": True},
    {"num": 33, "name": "Customer Portal", "complete": True},
    {"num": 32, "name": "Load Testing & Horizontal Scaling", "complete": True},
    {"num": 34, "name": "SLA & Escalation Engine v2", "complete": True},
    {"num": 36, "name": "Audit & Compliance Reports", "complete": True},
    {"num": 37, "name": "Operator & Access Group Model", "complete": True},
    {"num": 38,  "name": "Case Lifecycle UI & Assignment Views", "complete": True},
    {"num": 39,  "name": "Intelligent Customer Portal", "complete": True},
    {"num": 40,  "name": "Knowledge Center", "complete": True},
    {"num": 41,  "name": "HxGraph — Velaris Native Knowledge Graph", "complete": True},
    {"num": 42,  "name": "HxNexus Polyglot Intelligence", "complete": True},
    {"num": 43,  "name": "App Export & Environment Pipeline", "complete": True},
    {"num": 44,  "name": "BPM App Importer", "complete": True},
    {"num": 28,  "name": "HxBridge — Connector Protocol Foundation", "complete": True},
    {"num": 26,  "name": "HxAnalytics — Semantic Business Intelligence", "complete": True},
    {"num": 29,  "name": "HxSync — Data Pipeline & Warehouse Bridge", "complete": True},
    {"num": 35,  "name": "HxGlobal — Multi-Region & Data Sovereignty", "complete": True},
    {"num": 46,  "name": "HxStream — Live Execution & Interaction Stream", "complete": True},
    {"num": 59,  "name": "HxShield — Case Fraud & Abuse Detection", "complete": True},
    {"num": 47,  "name": "HxFusion — Adaptive Execution Engine (BPMN + AI Director)", "complete": True},
    {"num": 48,  "name": "HxConnect: Payment & Financial (Stripe)", "complete": True},
    {"num": 49,  "name": "HxConnect: Identity, KYC & E-Sign (Onfido + DocuSign)", "complete": True},
    {"num": 50,  "name": "HxConnect: CRM & Accounting (Salesforce + Xero)", "complete": True},
    {"num": 51,  "name": "HxConnect: Communications (Twilio SMS + Slack)", "complete": True},
    {"num": 52,  "name": "HxConnect: Document Intelligence & Storage (Docling + S3)", "complete": True},
    {"num": 53,  "name": "HxConnect: Developer & Custom Connectors (HTTP Builder + Webhook Receiver + OpenAPI)", "complete": True},
    {"num": 54,  "name": "HxMigrate — Unified Migration Intelligence Pipeline (5-stage: Scout→AI→Generate→Orchestrate→Package)", "complete": True},
    {"num": 55,  "name": "HxDeploy — Intelligent Deployment Governance (risk gate + approval cases + change windows + health checks)", "complete": True},
    {"num": 56,  "name": "HxWork — Kanban + Sprint Board (cards=cases, sprints, velocity, HxNexus board analysis)", "complete": True},
    {"num": 57,  "name": "HxCanvas — Visual Whiteboard (sticky notes, shapes, connectors, freehand, live HxGraph embeds, real-time collab, PNG/BPMN export)", "complete": True},
    {"num": 58,  "name": "HxDocs — Living Documentation (block editor, AI generation, live embeds, versioning, search)", "complete": True},
    {"num": 60,  "name": "HxBranch — Artifact Version Control & Live Environment Sync (branch, diff, review, merge, live env pull)", "complete": True},
    {"num": 61,  "name": "HxWork Redesign + Platform-wide Commit Pattern (dev lifecycle boards, user stories, git-style commit on every save)", "complete": True},
    {"num": 62,  "name": "Story-driven Branches — HxBranch + HxWork integration (auto-branch on story create, auto-merge to done)", "complete": True},
    {"num": 63,  "name": "HxLogs — AI-Driven Log Analyser (unified log view, traceback parsing, HxNexus root-cause analysis)", "complete": True},
    {"num": 64,  "name": "Real Authentication — bcrypt login, account lockout, forgot password OTP, TOTP MFA, Google/GitHub SSO, user management", "complete": True},
]


@router.get("/modules")
async def list_modules():
    """List every module/page in Velaris Studio."""
    return {"modules": MODULES, "total": len(MODULES)}


@router.get("/categories")
async def list_categories():
    """Module categories."""
    categories: dict[str, int] = {}
    for m in MODULES:
        categories[m["category"]] = categories.get(m["category"], 0) + 1
    return [{"name": n, "module_count": c} for n, c in categories.items()]


@router.get("/phases")
async def list_phases():
    """Development phase history."""
    completed = sum(1 for p in PHASES if p["complete"])
    return {
        "phases": PHASES,
        "total": len(PHASES),
        "completed": completed,
        "pending": len(PHASES) - completed,
    }


@router.get("/search")
async def search_modules(q: str = ""):
    """Search modules by label/description."""
    if not q:
        return {"results": MODULES}
    q_lower = q.lower()
    results = [
        m for m in MODULES
        if q_lower in m["label"].lower() or q_lower in m["description"].lower()
    ]
    return {"results": results, "query": q}


@router.get("/ai-status")
async def ai_status():
    """Is the configured AI model ready? Drives Studio's degraded-mode banner.

    Asked only whether Ollama answered until R4 (2026-10-30): a box with a cloud
    model and no Ollama showed "AI unavailable", and a box with Ollama but no
    model pulled showed nothing. Now the same answer as the Status screen's
    "AI model" line (case_service/readiness.py). No session, no tenant data: the
    route is open to every signed-in Studio page.
    """
    from case_service import readiness
    settings = get_settings()
    models = await readiness._ollama_models(settings)
    completion, _embedding = await readiness.check_ai(settings, models)
    available = completion["state"] == readiness.WORKING

    # ai_dependency tri-state (roadmap #11, §4.4): none | optional | required.
    # When AI is down: "required" modules are unavailable, "optional" run degraded.
    unavailable = [m["label"] for m in MODULES if m.get("ai_dependency") == "required" and not available]
    degraded = [m["label"] for m in MODULES if m.get("ai_dependency") == "optional" and not available]
    return {
        "ai_available": available,
        "unavailable_modules": unavailable,
        "degraded_modules": degraded,
    }
