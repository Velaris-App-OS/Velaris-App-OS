/**
 * nav-icons.tsx — single source of truth for all sidebar and page-header icons.
 *
 * HOW TO USE:
 *   - To change an icon: find the path entry in NAV_ICONS below and swap the component.
 *   - To add a new icon component: define it at the bottom, then add it to NAV_ICONS.
 *   - NAV_ICONS is keyed by route path (must match nav-data.ts paths exactly).
 *
 * The icon component type is:
 *   NavIconComponent = React.FC<{ active?: boolean; size?: number }>
 *
 * Used by:
 *   - AppLayout.tsx sidebar nav items
 *   - PageHeader.tsx (top-of-page title bar)
 */

import React from "react";

/** Single source of truth for icon size — change here to update sidebar + page header together. */
export const NAV_ICON_SIZE = 25;

export interface NavIconProps {
  active?: boolean;
  size?:   number;
}

export type NavIconComponent = React.FC<NavIconProps>;

/**
 * withSparkleBox — wraps any base icon in an open-corner rounded box with 3 sparkle
 * stars bursting from the top-right gap. Applied to all AI-powered Hx pages and
 * NLP Builder / Scout AI / Orchestrator AI.
 */
function withSparkleBox(Icon: NavIconComponent): NavIconComponent {
  const Wrapped: NavIconComponent = ({ active, size = 16 }) => {
    const c = active ? "var(--accent)" : "var(--text-muted)";
    return (
      <svg width={size} height={size} viewBox="0 0 16 16" fill="none" overflow="visible">
        {/* Rounded box — open at top-right so stars can emerge */}
        <path
          d="M9 3 L3.5 3 Q1.5 3 1.5 5 L1.5 13.5 Q1.5 15.5 3.5 15.5 L12.5 15.5 Q14.5 15.5 14.5 13.5 L14.5 6"
          stroke={c} strokeWidth="1.5" strokeLinecap="round" fill="none"
        />
        {/* Inner base icon centred in the box */}
        <svg x={3} y={4.5} width={10} height={10} overflow="visible">
          <Icon active={active} size={10} />
        </svg>
        {/* Large 4-pointed sparkle star */}
        <path d="M13 1 L13.55 2.45 L15 3 L13.55 3.55 L13 5 L12.45 3.55 L11 3 L12.45 2.45 Z" fill={c} />
        {/* Medium star — upper-right */}
        <path d="M15.5 -0.3 L15.85 0.75 L16.9 1.1 L15.85 1.45 L15.5 2.5 L15.15 1.45 L14.1 1.1 L15.15 0.75 Z" fill={c} />
        {/* Small star — lower-right */}
        <path d="M16 4.5 L16.25 5.2 L17 5.5 L16.25 5.8 L16 6.5 L15.75 5.8 L15 5.5 L15.75 5.2 Z" fill={c} />
      </svg>
    );
  };
  Wrapped.displayName = `SparkleBox(${Icon.displayName ?? Icon.name ?? "Icon"})`;
  return Wrapped;
}

// ── Icon-to-route mapping ─────────────────────────────────────────────────────
// Every route has its own dedicated icon — no two routes share a function.
// withSparkleBox() marks AI-powered pages.

export const NAV_ICONS: Record<string, NavIconComponent> = {
  // Workspace
  "/":              DashboardIcon,
  "/cases":         CasesIcon,
  "/work-center":   WorkCenterIcon,
  "/hxnexus":       withSparkleBox(HxNexusIcon),
  "/help":          KnowledgeIcon,
  "/hxdocs":        withSparkleBox(HxDocsIcon),
  "/hxcanvas":      withSparkleBox(HxCanvasIcon),
  "/sitemap":       SiteMapIcon,

  // Cases
  "/analytics":     AnalyticsIcon,
  "/hxanalytics":   withSparkleBox(HxAnalyticsIcon),
  "/documents":     DocumentsIcon,
  "/inbox":         InboxIcon,

  // Development
  "/case-designer":  CaseDesignerIcon,
  "/form-builder":   FormBuilderIcon,
  "/nlp-builder":    withSparkleBox(NLPBuilderIcon),
  "/modeler":        ModelerIcon,
  "/app-builder":    AppBuilderIcon,
  "/hxwork":         withSparkleBox(HxWorkIcon),
  "/testsuite":      withSparkleBox(TestSuiteIcon),
  "/hxbranch":       withSparkleBox(HxBranchIcon),
  "/graph":          withSparkleBox(HxGraphIcon),
  "/process-mining": ProcessMiningIcon,
  "/monitor":        MonitorIcon,
  "/escalation":     EscalationIcon,

  // DevOps
  "/deploy":         withSparkleBox(HxDeployIcon),
  "/hxmigrate":      withSparkleBox(BpmImporterIcon),
  "/scout":          ScoutIcon,
  "/scout-ai":       withSparkleBox(ScoutAIIcon),
  "/orchestrator":   withSparkleBox(OrchestratorIcon),

  // Integration
  "/marketplace": MarketplaceIcon,
  "/hxconnect":  withSparkleBox(HxConnectIcon),
  "/hxbridge":   withSparkleBox(HxBridgeIcon),
  "/devconn":    DevConnIcon,
  "/hxsync":     withSparkleBox(HxSyncIcon),
  "/hxfusion":   withSparkleBox(HxFusionIcon),

  // Security
  "/hxshield":      withSparkleBox(HxShieldIcon),
  "/hxstream":      withSparkleBox(HxStreamIcon),
  "/hxlogs":        withSparkleBox(HxLogsIcon),
  "/hxdbmanager":   withSparkleBox(HxDBManagerIcon),
  "/compliance":    ComplianceIcon,
  "/observability": ObservabilityIcon,

  // Admin
  "/portal-admin":     PortalAdminIcon,
  "/access-directory": AccessGroupIcon,
  "/admin":            AdminIcon,
  "/tenants":          TenantsIcon,
  "/enterprise":       EnterpriseIcon,
  "/email-admin":      EmailAdminIcon,
  "/push-admin":       PushAdminIcon,
  "/hxglobal":         withSparkleBox(HxGlobalIcon),
};

/** Retrieve an icon component by route path; falls back to a generic box icon. */
export function getNavIcon(path: string): NavIconComponent {
  return NAV_ICONS[path] ?? DefaultIcon;
}

// ══════════════════════════════════════════════════════════════════════════════
// Base icon components — one function per route, no sharing.
// ══════════════════════════════════════════════════════════════════════════════

function DefaultIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="2" y="2" width="12" height="12" rx="2" stroke={c} strokeWidth="1.5" />
    </svg>
  );
}

// ── Workspace ─────────────────────────────────────────────────────────────────

function DashboardIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="1" y="1" width="6" height="6" rx="1" stroke={c} strokeWidth="1.5" />
      <rect x="9" y="1" width="6" height="6" rx="1" stroke={c} strokeWidth="1.5" />
      <rect x="1" y="9" width="6" height="6" rx="1" stroke={c} strokeWidth="1.5" />
      <rect x="9" y="9" width="6" height="6" rx="1" stroke={c} strokeWidth="1.5" />
    </svg>
  );
}

function CasesIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="1" y="4" width="14" height="10" rx="1.5" stroke={c} strokeWidth="1.5" />
      <path d="M5 4V3a1 1 0 011-1h4a1 1 0 011 1v1" stroke={c} strokeWidth="1.5" />
      <path d="M1 8h14" stroke={c} strokeWidth="1.2" />
    </svg>
  );
}

function WorkCenterIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="1" y="2" width="14" height="3" rx="1" stroke={c} strokeWidth="1.5" />
      <rect x="1" y="7" width="14" height="3" rx="1" stroke={c} strokeWidth="1.5" />
      <rect x="1" y="12" width="8" height="3" rx="1" stroke={c} strokeWidth="1.5" />
    </svg>
  );
}

function HxNexusIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Left lobe */}
      <path d="M8 4C7 3.2 5.5 3.2 4.5 4C3.5 4.8 3 6 3 7C2.5 7.4 2.5 8.4 3 9C3 10 3.5 11 4.5 11.5C5.5 12 7 12 8 11.5" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
      {/* Right lobe */}
      <path d="M8 4C9 3.2 10.5 3.2 11.5 4C12.5 4.8 13 6 13 7C13.5 7.4 13.5 8.4 13 9C13 10 12.5 11 11.5 11.5C10.5 12 9 12 8 11.5" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
      {/* Center divide */}
      <path d="M8 4V11.5" stroke={c} strokeWidth="0.8" strokeLinecap="round" />
      {/* Wrinkles */}
      <path d="M4.5 7C5.2 7.5 6.2 7 7 7.5" stroke={c} strokeWidth="0.9" strokeLinecap="round" />
      <path d="M4.5 9C5.2 9.5 6.2 9 7 9.5" stroke={c} strokeWidth="0.9" strokeLinecap="round" />
      <path d="M11.5 7C10.8 7.5 9.8 7 9 7.5" stroke={c} strokeWidth="0.9" strokeLinecap="round" />
      <path d="M11.5 9C10.8 9.5 9.8 9 9 9.5" stroke={c} strokeWidth="0.9" strokeLinecap="round" />
    </svg>
  );
}

function KnowledgeIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Lightbulb — floating above the book */}
      <circle cx="8" cy="4" r="2.2" stroke={c} strokeWidth="1.2" />
      <path d="M7 6.5h2M7.3 7.3h1.4" stroke={c} strokeWidth="0.9" strokeLinecap="round" />
      {/* Book — lower portion */}
      <rect x="2" y="9" width="12" height="6" rx="1" stroke={c} strokeWidth="1.3" />
      <path d="M8 9v6" stroke={c} strokeWidth="1" />
      <path d="M2 9Q8 7.5 14 9" stroke={c} strokeWidth="1" strokeLinecap="round" />
    </svg>
  );
}

function HxDocsIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <path d="M3 2h7l3 3v9H3V2Z" stroke={c} strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M10 2v3h3" stroke={c} strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M5.5 7h5M5.5 9.5h3.5" stroke={c} strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

function HxCanvasIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Palette shape */}
      <path d="M8 2C5 2 2 4.5 2 7.5C2 10 3.5 12 5.5 13L7 14h2c.5 0 1-.4 1-1v-.5c0-.5.5-1 1-1h.5C13.5 11.5 14.5 10 14.5 8.5 14.5 4.5 11.5 2 8 2Z" stroke={c} strokeWidth="1.3" strokeLinejoin="round" />
      {/* Paint dots */}
      <circle cx="5.5" cy="7" r="1" fill={c} />
      <circle cx="7" cy="5" r="1" fill={c} />
      <circle cx="9.5" cy="4.5" r="1" fill={c} />
      <circle cx="11.5" cy="6.5" r="1" fill={c} />
    </svg>
  );
}

function SiteMapIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="2" y="2" width="4" height="4" stroke={c} strokeWidth="1.5" />
      <rect x="10" y="2" width="4" height="4" stroke={c} strokeWidth="1.5" />
      <rect x="2" y="10" width="4" height="4" stroke={c} strokeWidth="1.5" />
      <rect x="10" y="10" width="4" height="4" stroke={c} strokeWidth="1.5" />
      <path d="M6 4h4M6 12h4M4 6v4M12 6v4" stroke={c} strokeWidth="1" strokeDasharray="1 1" />
    </svg>
  );
}

// ── Cases ─────────────────────────────────────────────────────────────────────

function AnalyticsIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="2" y="9" width="3" height="5" rx="0.5" stroke={c} strokeWidth="1.5" />
      <rect x="7" y="5" width="3" height="9" rx="0.5" stroke={c} strokeWidth="1.5" />
      <rect x="12" y="2" width="3" height="12" rx="0.5" stroke={c} strokeWidth="1.5" />
    </svg>
  );
}

function HxAnalyticsIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <path d="M2 13V9M5.5 13V7M9 13V5M12.5 13V2" stroke={c} strokeWidth="1.4" strokeLinecap="round" />
      <path d="M1 13.5h14" stroke={c} strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

function DocumentsIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* LEFT half: briefcase */}
      <path d="M1 7Q1 5.5 2.5 5.5H8V14.5H2.5Q1 14.5 1 13Z" stroke={c} strokeWidth="1.3" strokeLinejoin="round" />
      {/* Briefcase handle */}
      <path d="M3.5 5.5V4a1.5 1.5 0 013 0v1.5" stroke={c} strokeWidth="1.2" strokeLinecap="round" />
      {/* Briefcase clasp band */}
      <path d="M1 9.5h7" stroke={c} strokeWidth="0.9" />
      {/* RIGHT half: document with folded corner */}
      <path d="M8 5.5h3.5L15 9V13Q15 14.5 13.5 14.5H8V5.5Z" stroke={c} strokeWidth="1.3" strokeLinejoin="round" />
      {/* Fold */}
      <path d="M11.5 5.5V9H15" stroke={c} strokeWidth="1.3" strokeLinejoin="round" />
      {/* Document lines */}
      <path d="M9 11h4M9 12.5h2.5" stroke={c} strokeWidth="0.9" strokeLinecap="round" />
    </svg>
  );
}

function InboxIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Envelope — slightly shallower flap to give @ more room */}
      <rect x="1.5" y="3" width="13" height="10" rx="1.5" stroke={c} strokeWidth="1.3" />
      <path d="M1.5 4.5L8 7.5 14.5 4.5" stroke={c} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
      {/* @ symbol — large, centered in lower half */}
      {/* Inner 'a' circle */}
      <circle cx="8" cy="9.5" r="1.5" stroke={c} strokeWidth="1.1" />
      {/* Outer ring arc with tail — full @ shape */}
      <path d="M9.5 9C9.5 7 6.5 6.8 5.5 8.8C4.5 10.8 5.8 12.8 8.2 12.8C9.5 12.8 10.5 12 10.5 10.5" stroke={c} strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

// ── Development ───────────────────────────────────────────────────────────────

function CaseDesignerIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" overflow="visible">
      {/* Person silhouette (compliance icon base) */}
      <circle cx="7" cy="4.5" r="2.5" stroke={c} strokeWidth="1.3" />
      <path d="M2.5 13c0-2.5 2-4.5 4.5-4.5s4.5 2 4.5 4.5" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
      {/* Glowing bulb — top-right corner */}
      <circle cx="13" cy="4" r="1.6" stroke={c} strokeWidth="1" />
      <path d="M13 2V1.5M14.5 2.8l.4-.4M15 4h.5M14.5 5.2l.4.4M13 6V6.5M11.5 5.2l-.4.4M11 4h-.5M11.5 2.8l-.4-.4" stroke={c} strokeWidth="0.7" strokeLinecap="round" />
    </svg>
  );
}

function FormBuilderIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" overflow="visible">
      {/* Original form — rounded rect */}
      <rect x="2" y="2" width="12" height="12" rx="2" stroke={c} strokeWidth="1.3" />
      {/* Field lines */}
      <path d="M5 5.5h6M5 8h6M5 10.5h4" stroke={c} strokeWidth="1.1" strokeLinecap="round" />
      {/* + badge — bottom-right corner */}
      <circle cx="12.5" cy="12.5" r="2" fill="var(--bg-panel)" stroke={c} strokeWidth="1" />
      <path d="M12.5 11v3M11 12.5h3" stroke={c} strokeWidth="1.1" strokeLinecap="round" />
    </svg>
  );
}

function NLPBuilderIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Message bubble 1 — top left */}
      <path d="M1 2h5v3H3.8L2.8 6V5H1V2Z" stroke={c} strokeWidth="1.1" strokeLinejoin="round" />
      {/* Message bubble 2 — bottom left */}
      <path d="M1 10h5v3H3.8L2.8 14V13H1V10Z" stroke={c} strokeWidth="1.1" strokeLinejoin="round" />
      {/* Lines (no arrow heads) converging into processor */}
      <path d="M6 3.5L9 7" stroke={c} strokeWidth="1" strokeLinecap="round" />
      <path d="M6 11.5L9 9" stroke={c} strokeWidth="1" strokeLinecap="round" />
      {/* Processor chip — right, vertically centered */}
      <rect x="9" y="5.5" width="6" height="5" rx="0.5" stroke={c} strokeWidth="1.1" />
      {/* Top & bottom pins */}
      <path d="M10.5 5.5V4.5M13 5.5V4.5M10.5 10.5V11.5M13 10.5V11.5" stroke={c} strokeWidth="0.9" strokeLinecap="round" />
      {/* Inner chip detail */}
      <rect x="10" y="6.5" width="3.5" height="3" rx="0.3" stroke={c} strokeWidth="0.7" />
    </svg>
  );
}

function ModelerIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <circle cx="3" cy="8" r="2" stroke={c} strokeWidth="1.5" />
      <rect x="8" y="5" width="6" height="6" rx="1" stroke={c} strokeWidth="1.5" />
      <path d="M5 8h3" stroke={c} strokeWidth="1.5" />
    </svg>
  );
}

function AppBuilderIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="4" y="1" width="8" height="14" rx="1.5" stroke={c} strokeWidth="1.5" />
      <path d="M7 13h2" stroke={c} strokeWidth="1.5" strokeLinecap="round" />
      <rect x="6" y="3" width="4" height="8" rx="0.5" stroke={c} strokeWidth="1" />
    </svg>
  );
}

function HxWorkIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Kanban board */}
      <rect x="1" y="2" width="14" height="12" rx="1.5" stroke={c} strokeWidth="1.3" />
      {/* Header bar */}
      <path d="M1 5h14" stroke={c} strokeWidth="1" />
      {/* Column dividers */}
      <path d="M5.5 2v12M10.5 2v12" stroke={c} strokeWidth="1" />
      {/* Col 1 cards */}
      <rect x="2" y="6.5" width="2.5" height="1.8" rx="0.3" stroke={c} strokeWidth="0.8" />
      <rect x="2" y="9.5" width="2.5" height="1.8" rx="0.3" stroke={c} strokeWidth="0.8" />
      {/* Col 2 card */}
      <rect x="6.5" y="6.5" width="3" height="1.8" rx="0.3" stroke={c} strokeWidth="0.8" />
      {/* Col 3 cards */}
      <rect x="11.5" y="6.5" width="2.5" height="1.8" rx="0.3" stroke={c} strokeWidth="0.8" />
      <rect x="11.5" y="9.5" width="2.5" height="1.8" rx="0.3" stroke={c} strokeWidth="0.8" />
    </svg>
  );
}

function HxBranchIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Main branch — bottom node */}
      <circle cx="4.5" cy="13" r="1.8" stroke={c} strokeWidth="1.3" />
      {/* Main branch — top node */}
      <circle cx="4.5" cy="3.5" r="1.8" stroke={c} strokeWidth="1.3" />
      {/* Feature branch node */}
      <circle cx="12" cy="6" r="1.8" stroke={c} strokeWidth="1.3" />
      {/* Main line */}
      <path d="M4.5 11.2V5.3" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
      {/* Branch-off curve */}
      <path d="M4.5 8C4.5 8 4.5 6 10.2 6" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}

function HxGraphIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <circle cx="8"  cy="3"  r="2" stroke={c} strokeWidth="1.3" />
      <circle cx="2"  cy="13" r="2" stroke={c} strokeWidth="1.3" />
      <circle cx="14" cy="13" r="2" stroke={c} strokeWidth="1.3" />
      <path d="M8 5L2 11M8 5L14 11M2 11h12" stroke={c} strokeWidth="1.1" strokeLinecap="round" />
    </svg>
  );
}

function ProcessMiningIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Magnifying glass */}
      <circle cx="7" cy="7" r="5" stroke={c} strokeWidth="1.4" />
      <path d="M11 11L14.5 14.5" stroke={c} strokeWidth="1.5" strokeLinecap="round" />
      {/* Eye inside */}
      <path d="M4 7C4 7 5.2 5.5 7 5.5C8.8 5.5 10 7 10 7C10 7 8.8 8.5 7 8.5C5.2 8.5 4 7 4 7Z" stroke={c} strokeWidth="1" />
      <circle cx="7" cy="7" r="1.2" fill={c} />
    </svg>
  );
}

function MonitorIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Monitor */}
      <rect x="1" y="2" width="14" height="10" rx="1.5" stroke={c} strokeWidth="1.3" />
      <path d="M5.5 13.5h5M8 12v2" stroke={c} strokeWidth="1.2" strokeLinecap="round" />
      {/* Magnifier inside screen */}
      <circle cx="7.5" cy="6.5" r="2.8" stroke={c} strokeWidth="1" />
      <path d="M9.5 9L11 10.5" stroke={c} strokeWidth="1.2" strokeLinecap="round" />
      {/* Eye inside magnifier */}
      <path d="M5.5 6.5C5.5 6.5 6.4 5.5 7.5 5.5C8.6 5.5 9.5 6.5 9.5 6.5C9.5 6.5 8.6 7.5 7.5 7.5C6.4 7.5 5.5 6.5 5.5 6.5Z" stroke={c} strokeWidth="0.8" />
      <circle cx="7.5" cy="6.5" r="0.8" fill={c} />
    </svg>
  );
}

function EscalationIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" overflow="visible">
      {/* Inverted pyramid */}
      <path d="M1.5 3H14.5L8 10Z" stroke={c} strokeWidth="1.3" strokeLinejoin="round" />
      {/* Level lines */}
      <path d="M4.5 5.5h7M6.5 7.5h3" stroke={c} strokeWidth="0.9" strokeLinecap="round" />
      {/* Gear at bottom */}
      <circle cx="8" cy="13" r="1.6" stroke={c} strokeWidth="1" />
      <circle cx="8" cy="13" r="0.55" fill={c} />
      {/* Gear teeth */}
      <path d="M8 10.9V10.4M8 15.1V15.6" stroke={c} strokeWidth="1" strokeLinecap="round" />
      <path d="M10.1 11.4l.4-.4M5.5 14.6l-.4.4M5.9 11.4l-.4-.4M10.5 14.6l.4.4" stroke={c} strokeWidth="1" strokeLinecap="round" />
      <path d="M11.1 13H11.6M4.4 13H4.9" stroke={c} strokeWidth="1" strokeLinecap="round" />
    </svg>
  );
}

// ── DevOps ────────────────────────────────────────────────────────────────────

function HxDeployIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="2" y="2" width="12" height="4" rx="1.5" stroke={c} strokeWidth="1.3" />
      <rect x="2" y="7" width="12" height="4" rx="1.5" stroke={c} strokeWidth="1.3" />
      <path d="M11.5 13l1.5-1.5L11.5 10" stroke={c} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function BpmImporterIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* --> top arrow (right) */}
      <path d="M2 5.5H13" stroke={c} strokeWidth="1.4" strokeLinecap="round" />
      <path d="M11 3.5L13.5 5.5L11 7.5" stroke={c} strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      {/* <-- bottom arrow (left) — more spacing below */}
      <path d="M14 10.5H3" stroke={c} strokeWidth="1.4" strokeLinecap="round" />
      <path d="M5 8.5L2.5 10.5L5 12.5" stroke={c} strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ScoutIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <circle cx="7" cy="7" r="5" stroke={c} strokeWidth="1.5" />
      <path d="M11 11l3 3" stroke={c} strokeWidth="1.5" strokeLinecap="round" />
      <path d="M5 7h4M7 5v4" stroke={c} strokeWidth="1" strokeLinecap="round" />
    </svg>
  );
}

function ScoutAIIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <circle cx="7" cy="7" r="5" stroke={c} strokeWidth="1.5" />
      <path d="M11 11l3 3" stroke={c} strokeWidth="1.5" strokeLinecap="round" />
      <path d="M5 7l1 1 2-2" stroke={c} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function OrchestratorIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <circle cx="3" cy="3" r="1.5" stroke={c} strokeWidth="1.2" />
      <circle cx="13" cy="3" r="1.5" stroke={c} strokeWidth="1.2" />
      <circle cx="8" cy="8" r="1.5" stroke={c} strokeWidth="1.2" />
      <circle cx="3" cy="13" r="1.5" stroke={c} strokeWidth="1.2" />
      <circle cx="13" cy="13" r="1.5" stroke={c} strokeWidth="1.2" />
      <path d="M4 4l3 3M12 4l-3 3M7 9l-3 3M9 9l3 3" stroke={c} strokeWidth="1" />
    </svg>
  );
}

// ── Integration ───────────────────────────────────────────────────────────────

function MarketplaceIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Storefront awning */}
      <path d="M1 5h14v2.5C15 8.3 14.3 9 13.5 9S12 8.3 12 7.5C12 8.3 11.3 9 10.5 9S9 8.3 9 7.5C9 8.3 8.3 9 7.5 9S6 8.3 6 7.5C6 8.3 5.3 9 4.5 9S3 8.3 3 7.5C3 8.3 2.3 9 1.5 9S0 8.3 0 7.5V5z" stroke={c} strokeWidth="1.2" strokeLinejoin="round" fill="none"/>
      {/* Storefront body */}
      <rect x="1" y="9" width="14" height="6" rx="0.5" stroke={c} strokeWidth="1.2"/>
      {/* Door */}
      <rect x="6" y="11" width="4" height="4" rx="0.3" stroke={c} strokeWidth="1"/>
      {/* Window */}
      <rect x="2" y="11" width="3" height="2.5" rx="0.3" stroke={c} strokeWidth="1"/>
      <rect x="11" y="11" width="3" height="2.5" rx="0.3" stroke={c} strokeWidth="1"/>
      {/* Roof / banner bar */}
      <rect x="1" y="3" width="14" height="2" rx="0.5" stroke={c} strokeWidth="1.2"/>
    </svg>
  );
}

function HxConnectIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="1" y="5" width="14" height="8" rx="2" stroke={c} strokeWidth="1.3" />
      <path d="M5 9h2M9 9h2" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
      <path d="M8 5V3M6 3h4" stroke={c} strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

function HxBridgeIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <path d="M1 8h3M12 8h3" stroke={c} strokeWidth="1.4" strokeLinecap="round" />
      <rect x="4" y="5" width="8" height="6" rx="2" stroke={c} strokeWidth="1.3" />
      <path d="M7 8h2" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}

function DevConnIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Same connector body + side pins */}
      <path d="M1 8h3M12 8h3" stroke={c} strokeWidth="1.4" strokeLinecap="round" />
      <rect x="4" y="5" width="8" height="6" rx="2" stroke={c} strokeWidth="1.3" />
      {/* </> code symbol inside instead of plain dash */}
      <path d="M6.5 9.5L5.5 8l1-1.5" stroke={c} strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M9.5 9.5L10.5 8l-1-1.5" stroke={c} strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M8.5 6.5L7.5 9.5" stroke={c} strokeWidth="0.9" strokeLinecap="round" />
    </svg>
  );
}

function TestSuiteIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* "{TS}" monogram — rendered inside the withSparkleBox() box */}
      <text x="8" y="10.8" textAnchor="middle" fontSize="6.5" fontWeight="700"
            fontFamily="var(--font-mono, ui-monospace, monospace)" fill={c}>{"{TS}"}</text>
    </svg>
  );
}

function HxSyncIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <path d="M2 8h12M10.5 5.5L14 8l-3.5 2.5" stroke={c} strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M2 4.5C2 3.1 3.1 2 4.5 2h7C12.9 2 14 3.1 14 4.5" stroke={c} strokeWidth="1.2" strokeLinecap="round" />
      <path d="M14 11.5c0 1.4-1.1 2.5-2.5 2.5h-7C3.1 14 2 12.9 2 11.5" stroke={c} strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

function HxFusionIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="1" y="1" width="6" height="6" rx="1.5" stroke={c} strokeWidth="1.3" />
      <rect x="9" y="1" width="6" height="6" rx="1.5" stroke={c} strokeWidth="1.3" />
      <rect x="5" y="9" width="6" height="6" rx="1.5" stroke={c} strokeWidth="1.3" />
      <path d="M7 4h2M8 4v5" stroke={c} strokeWidth="1.1" strokeLinecap="round" />
    </svg>
  );
}

// ── Security ──────────────────────────────────────────────────────────────────

function HxShieldIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <path d="M8 2L2.5 4.5v4c0 3 2.5 5 5.5 5.5C11 13.5 13.5 11.5 13.5 8.5v-4L8 2z" stroke={c} strokeWidth="1.3" strokeLinejoin="round" />
      <path d="M5.5 8l1.8 2L10.5 6" stroke={c} strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function HxStreamIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <path d="M1 8h2.5l2-4 2 8 2-4 2 3H15" stroke={c} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="13.5" cy="3.5" r="1.5" fill={c} />
    </svg>
  );
}

function HxDBManagerIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Database cylinder */}
      <ellipse cx="8" cy="4" rx="5" ry="1.8" stroke={c} strokeWidth="1.3" />
      <path d="M3 4v4c0 1 2.2 1.8 5 1.8s5-.8 5-1.8V4" stroke={c} strokeWidth="1.3" />
      <path d="M3 8v4c0 1 2.2 1.8 5 1.8s5-.8 5-1.8V8" stroke={c} strokeWidth="1.3" />
    </svg>
  );
}

function HxLogsIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Document shifted up — folded top-right corner */}
      <path d="M2 1h8l4 4v10H2V1Z" stroke={c} strokeWidth="1.3" strokeLinejoin="round" />
      <path d="M10 1v4h4" stroke={c} strokeWidth="1.3" strokeLinejoin="round" />
      {/* </> pushed down for vertical centering */}
      <path d="M5.5 11.5L4 10l1.5-1.5" stroke={c} strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M10.5 11.5L12 10l-1.5-1.5" stroke={c} strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M9 8L7 12" stroke={c} strokeWidth="1" strokeLinecap="round" />
    </svg>
  );
}

function ComplianceIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Person — right side */}
      <circle cx="10.5" cy="4" r="2" stroke={c} strokeWidth="1.3" />
      <path d="M6.5 14c0-2.5 2-4 4-4s4 1.5 4 4" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
      {/* Gavel — left side, rotated diagonal */}
      <g transform="rotate(35, 4, 10)">
        <rect x="2" y="8" width="4" height="2" rx="0.4" stroke={c} strokeWidth="1" fill="none" />
        <rect x="3.2" y="10" width="1.6" height="1.3" rx="0.3" stroke={c} strokeWidth="0.9" fill="none" />
        <path d="M4 11.3V15" stroke={c} strokeWidth="1.1" strokeLinecap="round" />
        <circle cx="4" cy="15" r="0.65" stroke={c} strokeWidth="0.9" fill="none" />
      </g>
    </svg>
  );
}

function ObservabilityIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Magnifying glass */}
      <circle cx="7" cy="7" r="5.5" stroke={c} strokeWidth="1.3" />
      <path d="M11 11L14.5 14.5" stroke={c} strokeWidth="1.5" strokeLinecap="round" />
      {/* Line chart inside */}
      <path d="M3 9.5l2-2.5 2 1.5 2.5-4 1.5 2" stroke={c} strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ── Admin ─────────────────────────────────────────────────────────────────────

function PortalAdminIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="1.5" y="3.5" width="13" height="9" rx="1.5" stroke={c} strokeWidth="1.4" />
      <path d="M4.5 7h7M4.5 9.5h4.5" stroke={c} strokeWidth="1.2" strokeLinecap="round" />
      <path d="M1.5 6h13" stroke={c} strokeWidth="1.2" />
    </svg>
  );
}

function AccessGroupIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <circle cx="5.5" cy="5" r="2" stroke={c} strokeWidth="1.3" />
      <circle cx="10.5" cy="5" r="2" stroke={c} strokeWidth="1.3" />
      <path d="M1.5 13c0-2.21 1.79-4 4-4s4 1.79 4 4" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
      <path d="M10.5 9c1.93 0 3.5 1.57 3.5 3.5" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}

function AdminIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Lock shackle */}
      <path d="M5 8.5V6a3 3 0 016 0v2.5" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
      {/* Lock body */}
      <rect x="3.5" y="8.5" width="9" height="6" rx="1.5" stroke={c} strokeWidth="1.3" />
      {/* >_ terminal prompt — no circle, just text in lock body */}
      <path d="M5.5 11l1.5.5L5.5 12" stroke={c} strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M8.5 12.5h2" stroke={c} strokeWidth="1.1" strokeLinecap="round" />
    </svg>
  );
}

function TenantsIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <rect x="1" y="5" width="6" height="9" rx="1" stroke={c} strokeWidth="1.5" />
      <rect x="9" y="2" width="6" height="12" rx="1" stroke={c} strokeWidth="1.5" />
      <path d="M3 8h2M3 10h2M11 5h2M11 7h2M11 9h2" stroke={c} strokeWidth="1" strokeLinecap="round" />
    </svg>
  );
}

function EnterpriseIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Cloud — clearly above the building with gap */}
      <path d="M4 4C4 2.6 5.1 1.5 6.5 1.5C7.5 1.5 8.4 2.1 8.8 2.9C9.1 2.6 9.6 2.4 10.1 2.4C11.2 2.4 12 3.2 12 4.1C12 5 11.2 5.6 10.2 5.6H5C4.4 5.6 4 4.9 4 4Z" stroke={c} strokeWidth="1.1" strokeLinejoin="round" />
      {/* Office building — starts below cloud with clear gap */}
      <rect x="3" y="7" width="10" height="8.5" stroke={c} strokeWidth="1.3" />
      {/* Floor lines */}
      <path d="M3 10.5h10M3 13h10" stroke={c} strokeWidth="0.8" />
      {/* Window grid */}
      <rect x="4.5" y="8" width="1.5" height="1.5" stroke={c} strokeWidth="0.8" />
      <rect x="7.5" y="8" width="1.5" height="1.5" stroke={c} strokeWidth="0.8" />
      <rect x="10" y="8" width="1.5" height="1.5" stroke={c} strokeWidth="0.8" />
      <rect x="4.5" y="11.2" width="1.5" height="1.5" stroke={c} strokeWidth="0.8" />
      <rect x="10" y="11.2" width="1.5" height="1.5" stroke={c} strokeWidth="0.8" />
      {/* Door */}
      <rect x="7" y="13.2" width="2" height="2.3" stroke={c} strokeWidth="0.8" />
    </svg>
  );
}

function EmailAdminIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Envelope */}
      <rect x="1.5" y="4" width="13" height="9.5" rx="1.5" stroke={c} strokeWidth="1.3" />
      <path d="M1.5 5.5L8 9 14.5 5.5" stroke={c} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
      {/* Lock in envelope center */}
      <rect x="6.5" y="9" width="3" height="2.5" rx="0.5" stroke={c} strokeWidth="1" />
      <path d="M7.2 9V8.2a.8.8 0 011.6 0V9" stroke={c} strokeWidth="0.9" strokeLinecap="round" />
    </svg>
  );
}

function PushAdminIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      {/* Bell body */}
      <path d="M8 2.5C5.5 2.5 4 4.5 4 7C4 9 3 10.5 2.5 11.5H13.5C13 10.5 12 9 12 7C12 4.5 10.5 2.5 8 2.5Z" stroke={c} strokeWidth="1.3" strokeLinejoin="round" />
      {/* Bell base bar */}
      <path d="M2.5 11.5h11" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
      {/* Clapper */}
      <path d="M6.5 11.5c0 .83.67 1.5 1.5 1.5s1.5-.67 1.5-1.5" stroke={c} strokeWidth="1.2" strokeLinecap="round" />
      {/* Top stem */}
      <path d="M8 2.5V1.5" stroke={c} strokeWidth="1.1" strokeLinecap="round" />
    </svg>
  );
}

function HxGlobalIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <circle cx="8" cy="8" r="6" stroke={c} strokeWidth="1.3" />
      <path d="M8 2C8 2 6 5 6 8s2 6 2 6" stroke={c} strokeWidth="1.1" strokeLinecap="round" />
      <path d="M8 2c0 0 2 3 2 6s-2 6-2 6" stroke={c} strokeWidth="1.1" strokeLinecap="round" />
      <path d="M2 8h12" stroke={c} strokeWidth="1.1" strokeLinecap="round" />
      <path d="M2.5 5.5h11M2.5 10.5h11" stroke={c} strokeWidth="0.9" strokeLinecap="round" strokeDasharray="2 1" />
    </svg>
  );
}

export function LiveActivityIcon({ active, size = 16 }: NavIconProps) {
  const c = active ? "var(--accent)" : "var(--text-muted)";
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <path d="M2 8h3l2-5 2 10 2-5h3" stroke={c} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
