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
  /** The install's own screens (2026-11-04): shown only to the platform
   *  operator (superadmin, or the default tenant). A tenant's admin is refused
   *  their data by the API anyway; this just stops showing an empty page. */
  operatorOnly?: boolean;
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
    description: "Case list | Create case | Search & filter | Case timeline | Status tracking | Bulk actions | Real-time case sessions",
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
    path: "/case-config",
    label: "Case Configuration",
    description: "Statuses | Categories | Transitions | Action labels | Action visibility | Offered-from rules",
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
    path: "/reference-tables",
    label: "Reference Tables",
    description: "Branches | Products | Price bands | CSV import | Options for forms | Cascading lists",
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
    path: "/hxdbmigrate",
    label: "HxDBMigrate",
    description: "Connect a source database | Schema Autobiography | Data-quality scoring | MySQL / MariaDB / PostgreSQL sources",
    section: "DevOps",
    roles: ["admin"],
  },
  {
    path: "/hxreplay",
    label: "HxReplay",
    description: "Counterfactual case replay | Fork real history at a decision point | Cohort what-if deltas | Determinacy coverage",
    section: "DevOps",
    roles: ["admin"],
  },
  {
    path: "/channels",
    label: "Channel Inbox",
    description: "Inbound SMS and WhatsApp | Two-way threads on the case record | STOP/opt-out honoured | WhatsApp 24-hour window",
    section: "Operations",
    roles: ["admin", "manager", "case_worker"],
  },
  {
    path: "/bots",
    label: "Bots",
    description: "RPA bot registry and job queue | Approval by a second admin | Attended jobs propose, the operator commits",
    section: "DevOps",
    roles: ["devops", "admin", "developer"],
  },
  {
    path: "/workforce",
    label: "Agent Workforce",
    description: "AI agents that hold case work | Skills, capacity and hourly token budgets | Why an agent stopped receiving work | Scorecards from the decision ledger | Routing weights a person approves",
    section: "Operations",
    roles: ["admin", "manager", "security"],
  },
  {
    path: "/rep-desk",
    label: "Rep Desk",
    description: "Who is calling, and what you may tell them | A 360 that can only narrow what your own permissions allow | Verification tiers on the call, not the person | One field unmasked at a time, with a reason | Raise a case in the customer's name",
    section: "Operations",
    roles: ["admin", "manager", "case_worker"],
  },
  {
    path: "/case-transfers",
    label: "Case Transfers",
    description: "Move a case into another tenant | Outgoing requests and a second administrator's approval | Incoming requests to accept or refuse | What moved, closed and was severed, once accepted",
    section: "Operations",
    roles: ["admin"],
  },
  {
    path: "/healing",
    label: "Self-Healing",
    description: "What the platform proposed to change about itself | Every rung of the validation ladder, passes included | One approval that applies the change | A pre-signed rollback | Deployed is not healed until the watch window closes",
    section: "Operations",
    roles: ["admin", "manager", "security"],
  },
  {
    path: "/field-ops",
    label: "Field Operations",
    description: "Work that came back from a disconnected node | Conflicts nothing decided for itself | Both values side by side before you choose | Node fleet, charters and staleness lockouts",
    section: "Operations",
    roles: ["admin", "manager", "case_worker"],
  },
  {
    path: "/intent",
    label: "Intent Execution",
    description: "State an outcome and read the plan Velaris composed | Verified fragments only, never free-form generation | Consequential capabilities called out | Confirming binds the hash of exactly what will run",
    section: "Development",
    roles: ["admin", "manager", "developer"],
  },
  {
    path: "/joint-contracts",
    label: "Joint Contracts",
    description: "Shared workflows agreed with another organisation | Every clause the matcher resolved | What the seam carries now vs what was negotiated | Recompute their answer yourself | Close the seam without asking anybody",
    section: "Integration",
    roles: ["admin", "manager", "security"],
  },
  {
    path: "/model-market",
    label: "Model Marketplace",
    description: "Share what a model learned, never the cases it learned from | What a published file actually discloses | A stranger's model scored on your own data before it may be installed | Installed is not active | Uninstall-with-delete destroys the weights and keeps the record",
    section: "Operations",
    roles: ["admin", "security"],
  },
  {
    path: "/calendar",
    label: "Calendar",
    description: "Who is away and who covers | Assignment and escalation skip people on leave | Business calendars for SLA hours | A colleague sees busy, never why",
    section: "Operations",
    roles: ["admin", "manager", "case_worker", "developer"],
  },
  {
    path: "/scheduler",
    label: "Scheduler",
    description: "Timed actions | Nightly email, bot runs, reports, warehouse sync | Approval by a second person | Every firing on the record",
    section: "DevOps",
    roles: ["devops", "admin", "developer", "manager"],
  },
  {
    path: "/risk-review",
    label: "Risk Review",
    description: "HxSentinel supervisor oversight | Per-reviewer score reliability | Outcome-agreement | Dispute rubber-stamped scores",
    section: "Security",
    roles: ["admin"],
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

  // ── Marketplace — visibility controlled by the `marketplace` scheduled_releases flag ──
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
    path: "/hxcheckout",
    label: "HxCheckout",
    description: "Commerce integration | Order intake API | Webhook mode (common shop platforms or a custom field map) | Service tokens | Order board | Returns & complaints | Marketplace app",
    section: "Integration",
    roles: ["integration", "admin"],
  },
  {
    path: "/hxstorefront",
    label: "HxStorefront",
    description: "Hosted store builder | Products & variants | Categories | Inventory | Promotions | Theme & page builder | Public storefront | Orders via HxCheckout | Marketplace app",
    section: "Integration",
    roles: ["integration", "admin"],
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
    path: "/hxstatus",
    label: "HxStatus",
    description: "Public status page | Uptime | SLA reporting | Component health | Incident visibility",
    section: "Security",
    roles: ["security", "admin", "designer"],
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
    path: "/arbiter",
    label: "HxArbiter",
    description: "Sovereignty & security policy engine | One rule, one deterministic evaluator, one provable decision log | Shadow before enforce, with parity as the evidence | Policies written in plain English, approved by a person",
    section: "Security",
    roles: ["admin", "security", "developer"],
  },
  {
    path: "/allowed-sites",
    label: "Allowed sites",
    description: "Sites forms may embed | Platform policy | Blocked sites | Change history",
    section: "Security",
    roles: ["admin"],
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
    label: "Portals Management",
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
    description: "System settings | Audit logs | Queues | Webhooks | Business rules | Calendars | Component permissions | Marketplace sources",
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
    description: "Feature flags | Enterprise settings | Security events | GDPR requests | Retention policies",
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
