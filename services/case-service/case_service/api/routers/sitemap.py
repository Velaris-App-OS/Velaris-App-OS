"""Site Map API — static catalog of all HELIX modules.

Provides a central index of every route in the Studio with
descriptions, categories, and API endpoints. Used by the
Help / Site Map page in the UI.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends

from case_service.auth.dependencies import get_current_user
from case_service.config import get_settings

router = APIRouter(prefix="/sitemap", tags=["sitemap"], dependencies=[Depends(get_current_user)])


MODULES = [
    # ── Process ──
    {
        "category": "Process",
        "path": "/",
        "label": "Dashboard",
        "description": "Overview of active processes, recent cases, and system health.",
        "phase": 0,
        "dev_time": "1 day",
        "api_endpoints": ["/api/v1/cases", "/api/v1/analytics"],
        "ai_dependency": "none",
    },
    {
        "category": "Process",
        "path": "/modeler",
        "label": "BPMN Modeler",
        "description": "Visual process designer — drag-and-drop flows, user tasks with forms, service integrations, gateway routing, and one-click deploy to the execution engine.",
        "phase": 2,
        "dev_time": "5 days",
        "api_endpoints": ["/api/v1/processes"],
        "ai_dependency": "none",
    },
    {
        "category": "Process",
        "path": "/monitor",
        "label": "Monitor",
        "description": "Real-time monitoring of running case instances and workflows.",
        "phase": 2,
        "dev_time": "2 days",
        "api_endpoints": ["/api/v1/cases"],
        "ai_dependency": "none",
    },
    # ── Cases ──
    {
        "category": "Cases",
        "path": "/case-designer",
        "label": "Case Designer",
        "description": "Define case types with stages, steps, routing, SLAs, and forms.",
        "phase": 3,
        "dev_time": "5 days",
        "api_endpoints": ["/api/v1/case-types"],
        "ai_dependency": "none",
    },
    {
        "category": "Cases",
        "path": "/form-builder",
        "label": "Form Builder",
        "description": "Drag-and-drop form designer for building step forms — supports text, select, rating, signature, file upload, and more.",
        "phase": 7,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/forms", "/api/v1/documents/upload"],
        "ai_dependency": "none",
    },
    {
        "category": "Cases",
        "path": "/app-builder",
        "label": "App Builder",
        "description": "Generate a React Native mobile app from your case types.",
        "phase": 18,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/codegen"],
        "ai_dependency": "none",
    },
    {
        "category": "Cases",
        "path": "/nlp-builder",
        "label": "NLP Builder",
        "description": "Create case types from plain English descriptions using AI. Quick mode builds the structure; Full mode generates forms, SLA policies, data model, and notifications.",
        "phase": 15,
        "dev_time": "4 days",
        "api_endpoints": [
            "/api/v1/nlp/status",
            "/api/v1/nlp/generate-case-type",
            "/api/v1/nlp/generate-full",
            "/api/v1/nlp/preview",
        ],
        "ai_dependency": "required",
    },
    {
        "category": "Cases",
        "path": "/cases",
        "label": "Cases",
        "description": "Browse, create, and manage case instances.",
        "phase": 3,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/cases"],
        "ai_dependency": "none",
    },
    {
        "category": "Cases",
        "path": "/documents",
        "label": "Documents",
        "description": "Attach PDFs, images, contracts to cases — with versioning and preview.",
        "phase": 24,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/documents"],
        "ai_dependency": "none",
    },
    {
        "category": "Cases",
        "path": "/inbox",
        "label": "Email Inbox",
        "description": "Inbound email queue, outbound delivery status, and templates.",
        "phase": 25,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/email"],
        "ai_dependency": "none",
    },
    {
        "category": "Cases",
        "path": "/work-center",
        "label": "Work Center",
        "description": "Your active assignments and worklist.",
        "phase": 3,
        "dev_time": "5 days",
        "api_endpoints": ["/api/v1/my"],
        "ai_dependency": "none",
    },
    {
        "category": "Cases",
        "path": "/analytics",
        "label": "Analytics",
        "description": "Case analytics, trends, and KPI dashboards.",
        "phase": 8,
        "dev_time": "4 days",
        "api_endpoints": ["/api/v1/analytics"],
        "ai_dependency": "none",
    },
    {
        "category": "Cases",
        "path": "/process-mining",
        "label": "Process Mining",
        "description": "Discover actual flows, find bottlenecks, and check conformance.",
        "phase": 14,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/process-mining"],
        "ai_dependency": "none",
    },

    # ── Admin ──
    {
        "category": "Admin",
        "path": "/admin",
        "label": "Admin Console",
        "description": "System administration, users, and configuration.",
        "phase": 10,
        "dev_time": "2 days",
        "api_endpoints": ["/api/v1/admin"],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/tenants",
        "label": "Tenants",
        "description": "Multi-tenant organization and membership management.",
        "phase": 17,
        "dev_time": "2 days",
        "api_endpoints": ["/api/v1/tenants"],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/scout",
        "label": "Scout",
        "description": "Scan Pega, Appian, or Camunda exports and generate migration plans.",
        "phase": 16,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/scout"],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/orchestrator",
        "label": "Orchestrator",
        "description": "End-to-end automated migration pipeline — scan, analyze, generate, export.",
        "phase": 21,
        "dev_time": "4 days",
        "api_endpoints": ["/api/v1/orchestrator"],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/scout-ai",
        "label": "Scout AI",
        "description": "AI-powered deep code analysis. Understands legacy code and auto-generates HELIX ports.",
        "phase": 19,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/scout-ai"],
        "ai_dependency": "required",
    },
    {
        "category": "Admin",
        "path": "/enterprise",
        "label": "Enterprise",
        "description": "GDPR requests, security events, retention policies, and compliance.",
        "phase": 20,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/enterprise"],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/observability",
        "label": "Observability",
        "description": "System telemetry — request latency, error rates, traces, and health history.",
        "phase": 23,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/observability/metrics", "/api/v1/observability/traces/recent", "/health/deep"],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/escalation",
        "label": "Escalation Trees",
        "description": "Visual editor for SLA escalation trees with business-hour awareness and auto-reassignment.",
        "phase": 34,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/escalation-trees", "/api/v1/cases/{id}/sla"],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/access-directory",
        "label": "Access Directory",
        "description": "Unified access management — groups, roles, user directory, and portal configuration.",
        "phase": 37,
        "dev_time": "4 days",
        "api_endpoints": ["/api/v1/user-directory", "/api/v1/access-roles", "/api/v1/access-groups", "/api/v1/portals"],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/compliance",
        "label": "Compliance",
        "description": "Audit chain integrity, SOC2/ISO27001 evidence packs, and case data lineage.",
        "phase": 36,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/compliance"],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/email-admin",
        "label": "Email Admin",
        "description": "Manage SMTP/IMAP accounts and email templates with live render preview.",
        "phase": 25,
        "dev_time": "2 days",
        "api_endpoints": ["/api/v1/email/accounts", "/api/v1/email/templates"],
        "ai_dependency": "none",
    },
    {
        "category": "Cases",
        "path": "/hxnexus",
        "label": "HxNexus",
        "description": "AI-powered case suggestions, document Q&A, and conversational assistant.",
        "phase": 30,
        "dev_time": "5 days",
        "api_endpoints": ["/api/v1/hxnexus/chat", "/api/v1/hxnexus/cases/{id}/suggest", "/api/v1/hxnexus/cases/{id}/qa"],
        "ai_dependency": "required",
    },
    {
        "category": "Admin",
        "path": "/push-admin",
        "label": "Push Notifications",
        "description": "Manage device tokens, notification preferences, and delivery logs.",
        "phase": 27,
        "dev_time": "2 days",
        "api_endpoints": ["/api/v1/push/devices", "/api/v1/push/preferences", "/api/v1/push/admin/logs"],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/portal-admin",
        "label": "Customer Portal",
        "description": "Configure tenant portals, enable case types for public submission, view submissions.",
        "phase": 33,
        "dev_time": "4 days",
        "api_endpoints": ["/api/v1/portal-admin/tenants", "/api/v1/portal-admin/submissions", "/api/v1/portal/{slug}"],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/hxanalytics",
        "label": "HxAnalytics",
        "description": "Semantic business intelligence — ask questions in plain English, get live charts, save reports, and export to PowerBI/Tableau via OData.",
        "phase": 26,
        "dev_time": "5 days",
        "api_endpoints": [
            "/api/v1/analytics/metrics/snapshot",
            "/api/v1/analytics/metrics/time-series",
            "/api/v1/analytics/metrics/sla-performance",
            "/api/v1/analytics/query",
            "/api/v1/analytics/reports",
            "/api/v1/analytics/odata",
        ],
        "ai_dependency": "optional",
    },
    {
        "category": "Admin",
        "path": "/hxglobal",
        "label": "HxGlobal",
        "description": "Multi-region deployment, data sovereignty rules (GDPR/HIPAA/CCPA), tenant region assignments, health monitoring, and cross-region access audit log.",
        "phase": 35,
        "dev_time": "5 days",
        "api_endpoints": [
            "/api/v1/global/regions",
            "/api/v1/global/health",
            "/api/v1/global/sovereignty-rules",
            "/api/v1/global/sovereignty-rules/resolve",
            "/api/v1/global/tenant-assignments",
            "/api/v1/global/migrate-tenant",
            "/api/v1/global/access-log",
        ],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/hxsync",
        "label": "HxSync",
        "description": "Data pipeline & warehouse bridge — sync live case data to BigQuery, Snowflake, DuckDB, Kafka and more with GDPR-aware per-field redaction.",
        "phase": 29,
        "dev_time": "5 days",
        "api_endpoints": [
            "/api/v1/sync/destinations",
            "/api/v1/sync/run/{dest_id}/sync",
            "/api/v1/sync/runs",
            "/api/v1/sync/health",
            "/api/v1/sync/destinations/{id}/field-mappings",
            "/api/v1/sync/destinations/{id}/redaction-rules",
        ],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/hxbridge",
        "label": "HxBridge",
        "description": "Connector registry, integration call history, dead-letter queue, and webhook receiver for all external integrations.",
        "phase": 28,
        "dev_time": "4 days",
        "api_endpoints": [
            "/api/v1/hxbridge/connector-types",
            "/api/v1/hxbridge/connectors",
            "/api/v1/hxbridge/connectors/{id}/test",
            "/api/v1/hxbridge/connectors/{id}/execute",
            "/api/v1/hxbridge/calls",
            "/api/v1/hxbridge/dlq",
            "/api/v1/webhooks/{connector_id}/receive",
        ],
        "ai_dependency": "none",
    },
    {
        "category": "Security",
        "path": "/hxshield",
        "label": "HxShield",
        "description": "Real-time fraud and abuse detection for cases. Configurable detection rules, incident management, and behavioural scoring.",
        "phase": 59,
        "dev_time": "5 days",
        "api_endpoints": [
            "/api/v1/shield/rules",
            "/api/v1/shield/incidents",
            "/api/v1/shield/events",
            "/api/v1/shield/score",
            "/api/v1/shield/stats",
        ],
        "ai_dependency": "none",
    },
    {
        "category": "Integrations",
        "path": "/hxconnect",
        "label": "HxConnect",
        "description": "Universal integration hub — payments (Stripe), KYC (Onfido), e-sign (DocuSign), CRM (Salesforce), accounting (Xero) — all as native case step types.",
        "phase": 50,
        "dev_time": "10 days",
        "api_endpoints": [
            "/api/v1/payments/cases/{id}/charge",
            "/api/v1/payments/cases/{id}/disburse",
            "/api/v1/identity/cases/{id}/verify",
            "/api/v1/identity/webhooks/onfido",
            "/api/v1/esign/cases/{id}/send",
            "/api/v1/esign/webhooks/docusign",
            "/api/v1/crm/cases/{id}/sync",
            "/api/v1/crm/cases/{id}/records",
            "/api/v1/invoices/cases/{id}/generate",
            "/api/v1/invoices/cases/{id}/records",
            "/api/v1/comms/sms/cases/{id}/send",
            "/api/v1/comms/sms/cases/{id}/messages",
            "/api/v1/comms/slack/cases/{id}/send",
            "/api/v1/comms/slack/cases/{id}/notifications",
            "/api/v1/comms/connectors",
            "/api/v1/docintel/cases/{id}/extract",
            "/api/v1/docintel/cases/{id}/extractions",
            "/api/v1/docintel/cases/{id}/store",
            "/api/v1/docintel/cases/{id}/storage",
            "/api/v1/docintel/connectors",
            "/api/v1/devconn/webhooks/receive/{connector_id}",
            "/api/v1/devconn/rules",
            "/api/v1/devconn/events",
            "/api/v1/devconn/connectors/build",
            "/api/v1/devconn/connectors/from-openapi",
            "/api/v1/devconn/connectors",
        ],
        "ai_dependency": "none",
    },
    {
        "category": "Automation",
        "path": "/hxfusion",
        "label": "HxFusion",
        "description": "Adaptive execution engine — run BPMN process automation and adaptive case management on the same case, with AI-directed mode switching.",
        "phase": 47,
        "dev_time": "5 days",
        "api_endpoints": [
            "/api/v1/fusion/definitions",
            "/api/v1/fusion/instances",
            "/api/v1/fusion/bindings",
            "/api/v1/fusion/director/advise",
            "/api/v1/fusion/stats",
        ],
        "ai_dependency": "optional",
    },
    {
        "category": "Planning",
        "path": "/hxwork",
        "label": "HxWork",
        "description": "Kanban and Sprint boards backed by Helix cases — every card is a real case with stages, SLAs, and assignees. Sprints, velocity tracking, blocking relationships, and HxNexus board analysis.",
        "phase": 56,
        "dev_time": "4 days",
        "api_endpoints": [
            "/api/v1/hxwork/boards",
            "/api/v1/hxwork/boards/{id}/cards",
            "/api/v1/hxwork/boards/{id}/sprints",
            "/api/v1/hxwork/boards/{id}/analytics",
            "/api/v1/hxwork/boards/{id}/ask",
            "/api/v1/hxwork/boards/{id}/relations",
            "/api/v1/hxwork/sprints/{id}/start",
            "/api/v1/hxwork/sprints/{id}/complete",
            "/api/v1/hxwork/sprints/{id}/cards",
        ],
        "ai_dependency": "optional",
    },
    {
        "category": "Documentation",
        "path": "/hxdocs",
        "label": "HxDocs",
        "description": "Living documentation — block-based articles that auto-update from HxGraph. AI-generated articles via HxNexus, live data embeds (case counts, SLA stats, graph nodes), version history, full-text search, and public-facing mode for the Customer Portal.",
        "phase": 58,
        "dev_time": "3 days",
        "api_endpoints": [
            "/api/v1/hxdocs/spaces",
            "/api/v1/hxdocs/spaces/{id}/articles",
            "/api/v1/hxdocs/articles/{id}",
            "/api/v1/hxdocs/articles/{id}/publish",
            "/api/v1/hxdocs/articles/{id}/versions",
            "/api/v1/hxdocs/search",
            "/api/v1/hxdocs/generate",
        ],
        "ai_dependency": "optional",
    },
    {
        "category": "Planning",
        "path": "/hxcanvas",
        "label": "HxCanvas",
        "description": "Infinite visual whiteboard — sticky notes, shapes (rect/circle/diamond), text, connector arrows, freehand drawing, live HxGraph node embeds (auto-updating), real-time multi-user collaboration via HxStream, PNG export and LLM-generated BPMN XML export.",
        "phase": 57,
        "dev_time": "4 days",
        "api_endpoints": [
            "/api/v1/hxcanvas/boards",
            "/api/v1/hxcanvas/boards/{id}",
            "/api/v1/hxcanvas/boards/{id}/items",
            "/api/v1/hxcanvas/boards/{id}/items/{item_id}",
            "/api/v1/hxcanvas/boards/{id}/items/bulk",
            "/api/v1/hxcanvas/boards/{id}/export/bpmn",
            "/api/v1/hxcanvas/graph-nodes/search",
        ],
        "ai_dependency": "optional",
    },
    {
        "category": "DevOps",
        "path": "/deploy",
        "label": "HxDeploy",
        "description": "Context-aware deployment governance — risk-classified promotions (low→auto-approve, high→compliance review), approval cases in Work Center, change-window enforcement, post-deploy health checks, environment swimlane view.",
        "phase": 55,
        "dev_time": "4 days",
        "api_endpoints": [
            "/api/v1/deploy/environments",
            "/api/v1/deploy/environments/{id}/status",
            "/api/v1/deploy/promote",
            "/api/v1/deploy/runs",
            "/api/v1/deploy/runs/{id}",
            "/api/v1/deploy/runs/{id}/approve",
            "/api/v1/deploy/runs/{id}/reject",
            "/api/v1/deploy/runs/{id}/health-check",
            "/api/v1/deploy/windows",
            "/api/v1/deploy/analyse-risk",
        ],
        "ai_dependency": "none",
    },
    {
        "category": "Migration",
        "path": "/hxmigrate",
        "label": "HxMigrate",
        "description": "Unified migration pipeline — upload a Pega/Camunda/Appian/ServiceNow export once and get a deployable Helix application out. Five automatic stages: Scout scan → AI analysis → BPM generation → Orchestrator project → App package.",
        "phase": 54,
        "dev_time": "3 days",
        "api_endpoints": [
            "/api/v1/hxmigrate/run",
            "/api/v1/hxmigrate/runs",
            "/api/v1/hxmigrate/runs/{id}",
            "/api/v1/hxmigrate/runs/{id}/result",
            "/api/v1/hxmigrate/platforms",
        ],
        "ai_dependency": "optional",
    },
    {
        "category": "Integrations",
        "path": "/devconn",
        "label": "Dev Connectors",
        "description": "Build custom HTTP connectors without code, receive inbound webhooks and route them to cases, and auto-generate connectors from OpenAPI/Swagger specs using HxNexus.",
        "phase": 53,
        "dev_time": "3 days",
        "api_endpoints": [
            "/api/v1/devconn/connectors/build",
            "/api/v1/devconn/connectors/from-openapi",
            "/api/v1/devconn/connectors",
            "/api/v1/devconn/rules",
            "/api/v1/devconn/webhooks/receive/{connector_id}",
            "/api/v1/devconn/events",
        ],
        "ai_dependency": "none",
    },
    {
        "category": "DevOps",
        "path": "/hxbranch",
        "label": "HxBranch",
        "description": "Artifact version control & live environment sync — pull case types, forms, or full app packages from any environment (staging, UAT) into dev as a named branch, review the diff, and merge to main. Dev is always the source of truth.",
        "phase": 60,
        "dev_time": "2 days",
        "api_endpoints": [
            "/api/v1/branches",
            "/api/v1/branches/pull",
            "/api/v1/branches/{id}/diff",
            "/api/v1/branches/{id}/reviews",
            "/api/v1/branches/{id}/merge",
            "/api/v1/branches/remote/{env_id}/available",
            "/api/v1/branches/envs/{env_id}/token",
            "/api/v1/branches/envs/{env_id}/test-connection",
        ],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/graph",
        "label": "HxGraph",
        "description": "Helix native knowledge graph — live semantic graph of all case types, stages, forms, modules, and their relationships. Replaces graphify.",
        "phase": 41,
        "dev_time": "4 days",
        "api_endpoints": [
            "/api/v1/graph/sync", "/api/v1/graph/nodes", "/api/v1/graph/query",
            "/api/v1/graph/path", "/api/v1/graph/explain", "/api/v1/graph/report",
            "/api/v1/graph/visualize", "/api/v1/graph/export",
        ],
        "ai_dependency": "none",
    },
    {
        "category": "Admin",
        "path": "/help",
        "label": "Knowledge Center",
        "description": "Platform guide, concept glossary, case type explorer, and HxNexus Q&A for new users and business stakeholders.",
        "phase": 40,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/knowledge/overview", "/api/v1/knowledge/case-types", "/api/v1/knowledge/glossary", "/api/v1/knowledge/modules"],
        "ai_dependency": "none",
    },
    {
        "category": "Security",
        "path": "/hxstream",
        "label": "HxStream",
        "description": "Live execution & interaction stream — observe every event, click, stage transition, and AI call in real time. Always-on singleton WebSocket with session gap markers. Admin/dev access only.",
        "phase": 46,
        "dev_time": "3 days",
        "api_endpoints": ["/api/v1/hxstream/events", "/api/v1/hxstream/ws", "/api/v1/hxstream/replay/{case_id}"],
        "ai_dependency": "none",
    },
    {
        "category": "Security",
        "path": "/hxlogs",
        "label": "HxLogs",
        "description": "AI-driven unified log analyser — reads structlog output from all Helix services, groups multi-line tracebacks with innermost frame highlighted, and sends selected errors to HxNexus for root-cause analysis, suggested fix, and related module identification.",
        "phase": 63,
        "dev_time": "1 day",
        "api_endpoints": [
            "/api/v1/hxlogs/services",
            "/api/v1/hxlogs/entries",
            "/api/v1/hxlogs/analyse",
            "/api/v1/hxlogs/correlate",
        ],
        "ai_dependency": "optional",
    },
    {
        "category": "Admin",
        "path": "/sitemap",
        "label": "Site Map",
        "description": "This page — catalog of every module in HELIX.",
        "phase": 20,
        "dev_time": "1 day",
        "api_endpoints": ["/api/v1/sitemap"],
        "ai_dependency": "none",
    },
]


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
    {"num": 41,  "name": "HxGraph — Helix Native Knowledge Graph", "complete": True},
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
    """List every module/page in HELIX Studio."""
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
    """Probe whether the AI backend (Ollama) is reachable.

    Returns {ai_available, degraded_modules} so the frontend can show a
    degraded-mode banner in AI-dependent modules without blocking their
    non-AI features.
    """
    settings = get_settings()
    ollama_url = settings.ai_ollama_url.rstrip("/")
    available = False

    try:
        async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
            available = resp.status_code < 400
    except Exception:
        available = False

    # ai_dependency tri-state (roadmap #11, §4.4): none | optional | required.
    # When AI is down: "required" modules are unavailable, "optional" run degraded.
    unavailable = [m["label"] for m in MODULES if m.get("ai_dependency") == "required" and not available]
    degraded = [m["label"] for m in MODULES if m.get("ai_dependency") == "optional" and not available]
    return {
        "ai_available": available,
        "unavailable_modules": unavailable,
        "degraded_modules": degraded,
    }
