/**
 * nav-data.ts — single source of truth for navigation metadata.
 *
 * Used by: GlobalSearch (Ctrl+K) and the SiteMap page.
 * Update a description here → reflects in both places automatically.
 *
 * Description format: "Feature A | Feature B | Feature C"
 * Keep 5–7 pipe-separated keywords that describe what is on the page.
 */

export type NavRole =
  | "admin" | "manager" | "designer" | "case_worker"
  | "devops" | "integration" | "security" | "viewer" | "developer";

export interface NavEntry {
  path:        string;
  label:       string;
  description: string;
  section:     string;
  roles:       NavRole[];
  featureKey?: string;   // if set, nav item is hidden until this feature has any version enabled in the DB
}

export const NAV_DATA: NavEntry[] = [
  // ── Workspace (everyone) ────────────────────────────────────────────
  {
    path: "/",
    label: "Dashboard",
    description: "Case overview | SLA status | Activity feed | KPI metrics | Recent cases | Team workload",
    section: "Workspace",
    roles: [],
  },
  {
    path: "/cases",
    label: "Cases",
    description: "Case list | Create case | Search & filter | Case timeline | Status tracking | Bulk actions",
    section: "Workspace",
    roles: [],
  },
  {
    path: "/work-center",
    label: "Work Center",
    description: "My tasks | Assigned steps | Queue inbox | Step completion | Pending approvals | Personal workload",
    section: "Workspace",
    roles: [],
  },
  {
    path: "/hxnexus",
    label: "HxNexus",
    description: "AI copilot | Case Q&A | Smart suggestions | Knowledge search | Chat history | RAG context",
    section: "Workspace",
    roles: [],
  },
  {
    path: "/help",
    label: "Knowledge Center",
    description: "Platform guide | Articles | Glossary | AI assistant | Search documentation | How-to guides",
    section: "Workspace",
    roles: [],
  },
  {
    path: "/hxdocs",
    label: "HxDocs",
    description: "Documentation | Knowledge articles | Platform guides | API reference | Release notes",
    section: "Workspace",
    roles: [],
  },
  {
    path: "/hxcanvas",
    label: "HxCanvas",
    description: "Collaborative whiteboard | Diagrams | Sticky notes | Real-time sync | Drawing tools | Export",
    section: "Workspace",
    roles: [],
  },
  {
    path: "/sitemap",
    label: "Site Map",
    description: "All modules | Module index | Search features | Category filter | Quick navigation",
    section: "Workspace",
    roles: ["admin", "manager", "designer", "developer"],
  },

  // ── Cases ───────────────────────────────────────────────────────────
  {
    path: "/analytics",
    label: "Analytics",
    description: "Case volume | SLA compliance | Resolution time | Performance charts | Team metrics | Trends",
    section: "Cases",
    roles: ["case_worker", "admin"],
  },
  {
    path: "/hxanalytics",
    label: "HxAnalytics",
    description: "BI report builder | Natural language queries | Custom dashboards | Data export | Semantic search",
    section: "Cases",
    roles: ["case_worker", "admin"],
  },
  {
    path: "/documents",
    label: "Documents",
    description: "File upload | Document library | Version history | Attachments | Download | Document search",
    section: "Cases",
    roles: ["case_worker", "admin"],
  },
  {
    path: "/inbox",
    label: "Email Inbox",
    description: "Email management | Link to case | Reply | Assign | Thread view | Email templates",
    section: "Cases",
    roles: ["case_worker", "admin"],
  },

  // ── Development ──────────────────────────────────────────────────────
  {
    path: "/case-designer",
    label: "Case Designer",
    description: "Stages & steps | Forms | SLA policies | Routing rules | Commit history | Version bump | Stage pipeline",
    section: "Development",
    roles: ["designer", "admin"],
  },
  {
    path: "/form-builder",
    label: "Form Builder",
    description: "Drag-and-drop fields | Sections | Field types | Validation rules | Form preview | JSON schema",
    section: "Development",
    roles: ["designer", "admin"],
  },
  {
    path: "/nlp-builder",
    label: "NLP Builder",
    description: "Natural language to case type | AI generation | Quick mode | Full mode | Stage config | SLA config",
    section: "Development",
    roles: ["designer", "admin"],
  },
  {
    path: "/modeler",
    label: "BPMN Modeler",
    description: "Process design | Gateways | User tasks | Service tasks | Sequence flows | BPMN export | Deploy to engine",
    section: "Development",
    roles: ["designer", "admin"],
  },
  {
    path: "/app-builder",
    label: "App Builder",
    description: "Mobile app generation | iOS | Android | React Native | Bundle ID | API configuration | Code download",
    section: "Development",
    roles: ["designer", "admin"],
  },
  {
    path: "/hxwork",
    label: "HxWork",
    description: "Kanban board | Sprint planning | Story cards | Velocity tracking | Task relations | Backlog",
    section: "Development",
    roles: ["designer", "admin"],
  },
  {
    path: "/hxbranch",
    label: "HxBranch",
    description: "Version control | Branches | Merge artifacts | Compare configs | Pull from environment | Artifact sync",
    section: "Development",
    roles: ["designer", "admin"],
  },
  {
    path: "/graph",
    label: "HxGraph",
    description: "Knowledge graph | D3 visualisation | Node relationships | Semantic search | Community detection | Browse nodes",
    section: "Development",
    roles: ["designer", "admin"],
  },
  {
    path: "/process-mining",
    label: "Process Mining",
    description: "Case execution patterns | Bottleneck analysis | Flow discovery | Process traces | Variant comparison",
    section: "Development",
    roles: ["designer", "admin"],
  },
  {
    path: "/monitor",
    label: "Monitor",
    description: "Running process instances | Workflow status | BPMN engine | Execution logs | Active tasks",
    section: "Development",
    roles: ["designer", "admin"],
  },
  {
    path: "/escalation",
    label: "Escalation Trees",
    description: "Escalation paths | SLA breach rules | Notification targets | Priority levels | Escalation policy",
    section: "Development",
    roles: ["admin", "designer"],
  },

  // ── DevOps ───────────────────────────────────────────────────────────
  {
    path: "/deploy",
    label: "HxDeploy",
    description: "Environments | Promote releases | Deployment runs | Change windows | Approval gates | Health checks",
    section: "DevOps",
    roles: ["devops", "admin"],
  },
  {
    path: "/hxmigrate",
    label: "HxMigrate",
    description: "BPM import pipeline | Camunda | Pega | Appian | ServiceNow | Power Automate | Apply to Velaris | Migration history",
    section: "DevOps",
    roles: ["devops", "admin"],
  },
  {
    path: "/scout",
    label: "Scout",
    description: "Legacy BPM scanner | Compatibility analysis | Migration assessment | Artifact classification | Effort estimate",
    section: "DevOps",
    roles: ["devops", "admin"],
  },
  {
    path: "/scout-ai",
    label: "Scout AI",
    description: "AI code analysis | Auto-porting | Code generation | Rule conversion | AI-assisted migration",
    section: "DevOps",
    roles: ["devops", "admin"],
  },
  {
    path: "/orchestrator",
    label: "Orchestrator AI",
    description: "Migration projects | Review tasks | Multi-step workflows | Project tracking | Human review queue",
    section: "DevOps",
    roles: ["devops", "admin"],
  },

  // ── Marketplace — hidden until v1.2.0 release (controlled by scheduled_releases flag) ──
  {
    path:        "/marketplace",
    label:       "Marketplace",
    description: "Install connectors | Case templates | Modules | NLP packs | Themes | Sandbox testing | Admin review",
    section:     "Integration",
    roles:       [],
    featureKey:  "marketplace",
  },

  // ── Integration ──────────────────────────────────────────────────────
  {
    path: "/hxconnect",
    label: "HxConnect",
    description: "Payments | Stripe | KYC & Identity | E-Sign | CRM sync | Invoices | SMS | Slack | Document intelligence | Cloud storage",
    section: "Integration",
    roles: ["integration", "admin"],
  },
  {
    path: "/hxbridge",
    label: "HxBridge",
    description: "Connector registry | HTTP connectors | Call history | Dead letter queue | Connector config | API calls",
    section: "Integration",
    roles: ["integration", "admin"],
  },
  {
    path: "/devconn",
    label: "Dev Connectors",
    description: "Custom connectors | Webhook receiver | OpenAPI builder | Inbound webhooks | API explorer | No-code HTTP",
    section: "Integration",
    roles: ["integration", "admin", "designer"],
  },
  {
    path: "/testsuite",
    label: "Test Suite",
    description: "Platform test suites | Component & Security tests | Run history | Marketplace conformance gate | Structural checks | HxTest AI generation (marketplace add-on)",
    section: "Integration",
    roles: ["admin"],
  },
  {
    path: "/hxsync",
    label: "HxSync",
    description: "Data sync jobs | BigQuery | Kafka | DuckDB | Pipeline config | Warehouse bridge | Scheduled sync",
    section: "Integration",
    roles: ["integration", "admin"],
  },
  {
    path: "/hxfusion",
    label: "HxFusion",
    description: "Adaptive execution engine | BPMN orchestration | Rule evaluation | Task routing | Process fusion",
    section: "Integration",
    roles: ["integration", "admin", "designer"],
  },

  // ── Security ─────────────────────────────────────────────────────────
  {
    path: "/hxshield",
    label: "HxShield",
    description: "Fraud detection | Threat scoring | IP allowlists | Abuse prevention | Risk rules | Security policies",
    section: "Security",
    roles: ["security", "admin"],
  },
  {
    path: "/hxstream",
    label: "HxStream",
    description: "Live event stream | Case events | Audit log | Real-time activity | Event replay | Event filter",
    section: "Security",
    roles: ["security", "admin", "designer"],
  },
  {
    path: "/hxlogs",
    label: "HxLogs",
    description: "Structured logs | Filter & search | Log export | Service logs | Error tracking | Log levels",
    section: "Security",
    roles: ["security", "admin", "designer"],
  },
  // ── HxDB Manager — hidden until v1.3.0 release (controlled by scheduled_releases flag) ──
  {
    path:        "/hxdbmanager",
    label:       "HxDB Manager",
    description: "Schema browser | Table viewer | SQL editor | AI SQL assistant | EXPLAIN visualiser | Index advisor | Query history",
    section:     "Security",
    roles:       [],
    featureKey:  "hxdbmanager",
  },
  {
    path: "/compliance",
    label: "Compliance",
    description: "Audit chain integrity | Evidence pack | Compliance reports | Data lineage | GDPR | Regulatory frameworks",
    section: "Security",
    roles: ["security", "admin"],
  },
  {
    path: "/observability",
    label: "Observability",
    description: "Service metrics | p95 latency | Slowest endpoints | Performance traces | KPI dashboard | Auto-refresh",
    section: "Security",
    roles: ["security", "admin"],
  },

  // ── Admin ─────────────────────────────────────────────────────────────
  {
    path: "/portal-admin",
    label: "Customer Portal",
    description: "Portal configuration | Customer submissions | Portal branding | Case type access | Portal settings",
    section: "Admin",
    roles: ["admin"],
  },
  {
    path: "/access-directory",
    label: "Access Directory",
    description: "Access groups | Access roles | User directory | Portal assignments | Permission management",
    section: "Admin",
    roles: ["admin"],
  },
  {
    path: "/admin",
    label: "Admin Console",
    description: "System settings | Audit logs | Queues | Webhooks | Business rules | Calendars | Component permissions",
    section: "Admin",
    roles: ["admin"],
  },
  {
    path: "/tenants",
    label: "Tenants",
    description: "Multi-tenant config | Tenant isolation | Tenant management | Data separation | Tenant settings",
    section: "Admin",
    roles: ["admin"],
  },
  {
    path: "/enterprise",
    label: "Enterprise",
    description: "Feature flags | Enterprise settings | Platform configuration | Global policies | Licence management",
    section: "Admin",
    roles: ["admin"],
  },
  {
    path: "/email-admin",
    label: "Email Admin",
    description: "SMTP accounts | Email templates | Mailbox configuration | Inbox setup | Delivery settings",
    section: "Admin",
    roles: ["admin"],
  },
  {
    path: "/push-admin",
    label: "Push Notifications",
    description: "Push devices | Notification logs | FCM | APNs | Web push | VAPID | Test sends",
    section: "Admin",
    roles: ["admin"],
  },
  {
    path: "/hxglobal",
    label: "HxGlobal",
    description: "Multi-region | Locales | Currencies | Data sovereignty | Region health | Cloud configuration",
    section: "Admin",
    roles: ["admin"],
  },
];
