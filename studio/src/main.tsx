import React, { Suspense, lazy } from "react";
import { attemptRefresh, getAccessToken, getRefreshToken } from "@/auth/tokenManager";
import { AuthProvider } from "@/auth";
import SamlHandoffGate from "@/auth/SamlHandoffGate";
import { PermissionsProvider, usePermissions } from "@/auth/PermissionsContext";
import { ThemeProvider } from "@/theme/ThemeContext";
import ProtectedRoute from "@/auth/ProtectedRoute";
import RequireRole from "@/auth/RequireRole";
import { NAV_DATA } from "@/app/nav-data";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route, Navigate, Link } from "react-router-dom";
import { fetchWithBusyRetry } from "@shared/api/busyRetry";
import { noteDataAsOf } from "@shared/api/dataAsOf";
import BusyBanner from "@shared/components/BusyBanner";

// ── Global 401 interceptor ────────────────────────────────────────────────────
// Monkey-patch fetch once at boot. Access tokens are short-lived (15 min) by
// design, so a 401 mid-session is NORMAL — it means "refresh me", not "log me
// out". On a non-excluded 401 with an active session we run the (deduplicated)
// refresh first and dispatch "velaris:unauthorized" only when the refresh
// chain is actually dead (tokens cleared). Callers going through
// request()/caseRequest() retry with the fresh token; other callers fail this
// one call but the session survives.
// Excluded paths: auth endpoints that legitimately return 401 to anonymous
// users. /auth/real/refresh MUST stay excluded — attemptRefresh() posts to it
// through this patched fetch, and re-entering would deadlock on its own
// in-flight promise.
const _EXCLUDED_401 = [
  "/auth/login",
  "/auth/real/login",
  "/auth/real/refresh",   // attemptRefresh's own call — re-entry would deadlock
  "/auth/real/sso",       // SSO provider discovery — called from login page
  "/auth/real/forgot-password",
  "/auth/real/reset-password",
  "/auth/real/webauthn/", // Group J: passkey ceremonies 401 on failed assertions, not expired sessions
  "/auth/me",             // session-restore probe on page load
  "/portal/",            // portal endpoints — public, may return 401
  "/platform/update/",   // PUO step-up: a wrong password/passkey is 401 but the session is fine
];
const _origFetch = window.fetch.bind(window);
window.fetch = async (...args: Parameters<typeof fetch>): Promise<Response> => {
  // A "server busy" refusal of the sign-in check is retried here, for every
  // caller, before anything below sees it (shared/api/busyRetry.ts).
  const response = await fetchWithBusyRetry(_origFetch, args);
  // R5d 11b: a read served from a replica says how current it is.
  noteDataAsOf(response);
  if (response.status === 401) {
    const url = typeof args[0] === "string" ? args[0] : (args[0] as Request).url;
    const isExcluded = _EXCLUDED_401.some(p => url.includes(p));
    // Only treat as "session expired" when the user actually had a token
    const hasSession = !!getAccessToken();
    if (!isExcluded && hasSession) {
      const refreshed = await attemptRefresh();
      // Logout when the chain is dead (tokens cleared) or there is nothing to
      // refresh with (legacy token-only session). A failed refresh with the
      // chain still intact is a transient (network/429) — keep the session.
      if (!refreshed &&
          (!getAccessToken() || !getRefreshToken())) {
        window.dispatchEvent(new CustomEvent("velaris:unauthorized"));
      }
    }
  }
  return response;
};
import AppLayout from "@/app/AppLayout";
import { FeatureFlagsProvider } from "@/app/FeatureFlagsContext";
const Dashboard = lazy(() => import("@modules/dashboard/Dashboard"));
const Modeler = lazy(() => import("@modules/modeler/Modeler"));
const Monitor = lazy(() => import("@modules/monitor/Monitor"));
const CaseDesigner = lazy(() => import("@modules/case-designer/CaseDesigner"));
const CaseConfig = lazy(() => import("@modules/case-config/CaseConfig"));
const CaseManager = lazy(() => import("@modules/case-manager/CaseManager"));
const WorkCenter = lazy(() => import("@modules/work-center/WorkCenter"));
const Analytics = lazy(() => import("@modules/analytics/Analytics"));
const AdminConsole = lazy(() => import("@modules/admin/AdminConsole"));
const ProcessMining = lazy(() => import("@modules/process-mining/ProcessMining"));
const NLPBuilder = lazy(() => import("@modules/nlp-builder/NLPBuilder"));
const Scout = lazy(() => import("@modules/scout/Scout"));
const ScoutAI = lazy(() => import("@modules/scout-ai/ScoutAI"));
const Orchestrator = lazy(() => import("@modules/orchestrator/Orchestrator"));
const LiveActivity = lazy(() => import("@modules/live-activity/LiveActivity"));
const Enterprise = lazy(() => import("@modules/enterprise/Enterprise"));
const SiteMap = lazy(() => import("@modules/sitemap/SiteMap"));
const Tenants = lazy(() => import("@modules/tenants/Tenants"));
const AppBuilder = lazy(() => import("@modules/app-builder/AppBuilder"));
const ObservabilityDashboard = lazy(() => import("@modules/observability/ObservabilityDashboard"));
const DocumentManager = lazy(() => import("@modules/documents/DocumentManager"));
const EscalationEditor = lazy(() => import("@modules/escalation/EscalationEditor"));
const ComplianceDashboard = lazy(() => import("@modules/compliance/ComplianceDashboard"));
const EmailInbox = lazy(() => import("@modules/email/EmailInbox"));
const EmailAdmin = lazy(() => import("@modules/email-admin/EmailAdmin"));
const PushAdmin = lazy(() => import("@modules/push-admin/PushAdmin"));
const HxNexus = lazy(() => import("@modules/hxnexus/HxNexus"));
const Portal = lazy(() => import("@modules/portal/Portal"));
const PortalLogin = lazy(() => import("@modules/portal/Portal").then(m => ({ default: m.PortalLogin })));
const PortalAdmin = lazy(() => import("@modules/portal/PortalAdmin"));
const MeetJoin = lazy(() => import("@modules/portal/MeetJoin"));
const SsoCallback = lazy(() => import("@/auth/SsoCallback"));
const AccessGroupAdmin = lazy(() => import("@modules/access-groups/AccessGroupAdmin"));
const HxStream = lazy(() => import("@modules/hxstream/HxStream"));
const HxStatus = lazy(() => import("@modules/hxstatus/HxStatus"));
const HelpCenter = lazy(() => import("@modules/help/HelpCenter"));
const HxGraph = lazy(() => import("@modules/hxgraph/HxGraph"));
const FormBuilderPage = lazy(() => import("@modules/form-builder/FormBuilderPage"));
const ReferenceTablesPage = lazy(() => import("@modules/form-builder/ReferenceTablesPage"));
const HxBranch = lazy(() => import("@modules/hxbranch/HxBranch"));
const HxLogs = lazy(() => import("@modules/hxlogs/HxLogs"));
const AllowedSites = lazy(() => import("@modules/allowed-sites/AllowedSites"));
const HxDBManager = lazy(() => import("@modules/hxdbmanager/HxDBManager"));
const HxBridge = lazy(() => import("@modules/hxbridge/HxBridge"));
const HxAnalytics = lazy(() => import("@modules/hxanalytics/HxAnalytics"));
const HxSync = lazy(() => import("@modules/hxsync/HxSync"));
const HxGlobal = lazy(() => import("@modules/hxglobal/HxGlobal"));
const HxShield = lazy(() => import("@modules/hxshield/HxShield"));
const HxFusion = lazy(() => import("@modules/hxfusion/HxFusion"));
const Marketplace = lazy(() => import("@modules/marketplace/Marketplace"));
const HxConnect = lazy(() => import("@modules/hxconnect/HxConnect"));
const DevConn = lazy(() => import("@modules/devconn/DevConn"));
const HxTest = lazy(() => import("@modules/hxtest/HxTest"));
const HxCheckout = lazy(() => import("@modules/hxcheckout/HxCheckout"));
const HxStorefront = lazy(() => import("@modules/hxstorefront/HxStorefront"));
const HxMigrate = lazy(() => import("@modules/hxmigrate/HxMigrate"));
const HxDBMigrate = lazy(() => import("@modules/hxdbmigrate/HxDBMigrate"));
const HxReplay = lazy(() => import("@modules/hxreplay/HxReplay"));
const RiskReview = lazy(() => import("@modules/hxsentinel/RiskReview"));
const ChannelInbox = lazy(() => import("@modules/channels/ChannelInbox"));
const BotConsole = lazy(() => import("@modules/bots/BotConsole"));
const Calendar = lazy(() => import("@modules/calendar/Calendar"));
const Arbiter = lazy(() => import("@modules/arbiter/Arbiter"));
const Workforce = lazy(() => import("@modules/workforce/Workforce"));
const ModelMarket = lazy(() => import("@modules/modelmarket/ModelMarket"));
const RepDesk = lazy(() => import("@modules/engage/RepDesk"));
const Healing = lazy(() => import("@modules/healing/Healing"));
const TenantTransfers = lazy(() => import("@modules/case-manager/TenantTransfer"));
const FieldOps = lazy(() => import("@modules/field/FieldOps"));
const Intent = lazy(() => import("@modules/intent/Intent"));
const Negotiate = lazy(() => import("@modules/negotiate/Negotiate"));
const Scheduler = lazy(() => import("@modules/scheduler/Scheduler"));
const HxDeploy = lazy(() => import("@modules/hxdeploy/HxDeploy"));
const HxWork = lazy(() => import("@modules/hxwork/HxWork"));
const HxCanvas = lazy(() => import("@modules/hxcanvas/HxCanvas"));
const HxDocs = lazy(() => import("@modules/hxdocs/HxDocs"));
import "@shared/styles/global.css";
/**
 * Every screen below is `lazy()`, and this is the boundary that covers them.
 *
 * Studio used to build as ONE chunk — 2.78 MB, zero lazy routes — so opening
 * the login page downloaded the BPMN modeler, the D3 graph screen and every
 * admin console with it. That is slow on a first visit anywhere and it is the
 * named prerequisite for a kiosk image, which has to work on a LAN.
 *
 * The fallback is deliberately quiet: a chunk arrives in milliseconds on a
 * local network, and a full-page spinner that flashes on every navigation
 * reads as a bug. Nothing here changes what a screen does once it is loaded.
 */
function PageLoading() {
  return <div style={{ padding: 24, color: "var(--text-muted, #6b7280)" }}>Loading…</div>;
}

function UnknownPage() {
  return (
    <div style={{ padding: "var(--space-xl, 32px)", color: "var(--text-primary)" }}>
      <h2 style={{ margin: "0 0 8px", fontSize: 18 }}>Page not found</h2>
      <p style={{ margin: "0 0 16px", fontSize: 13, color: "var(--text-secondary)" }}>
        There is nothing at <code>{window.location.pathname}</code>. It may have moved, or the link is mistyped.
      </p>
      <Link to="/">Go to the dashboard</Link>
    </div>
  );
}

function PermRoute({ path, pageName, children }: { path: string; pageName: string; children: React.ReactNode }) {
  const { getRouteRoles, isOperator } = usePermissions();
  // The install's own screens (nav-data `operatorOnly`): a tenant's admin gets
  // the denied page rather than an empty screen of refused calls (2026-11-04).
  const operatorOnly = NAV_DATA.some(e => e.path === path && e.operatorOnly);
  if (operatorOnly && !isOperator) {
    return <RequireRole allowedRoles={["platform_operator"]} pageName={pageName} deny>{children}</RequireRole>;
  }
  return <RequireRole allowedRoles={getRouteRoles(path)} pageName={pageName}>{children}</RequireRole>;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider><AuthProvider><FeatureFlagsProvider><BrowserRouter><PermissionsProvider>
     <BusyBanner />
     <SamlHandoffGate>
      <Suspense fallback={<PageLoading />}>
      <Routes>
        <Route element={<ProtectedRoute><AppLayout /></ProtectedRoute>}>
          {/* ── Open to all authenticated users ── */}
          <Route path="/"            element={<Dashboard />} />
          <Route path="/cases"       element={<CaseManager />} />
          <Route path="/work-center" element={<WorkCenter />} />
          <Route path="/hxnexus"     element={<HxNexus />} />
          <Route path="/help"        element={<HelpCenter />} />
          <Route path="/hxdocs"      element={<HxDocs />} />
          <Route path="/hxcanvas"    element={<HxCanvas />} />

          {/* ── Case Worker / Staff ── */}
          <Route path="/analytics"   element={<PermRoute path="/analytics"   pageName="Analytics">  <Analytics />       </PermRoute>} />
          <Route path="/hxanalytics" element={<PermRoute path="/hxanalytics" pageName="HxAnalytics"><HxAnalytics />     </PermRoute>} />
          <Route path="/documents"   element={<PermRoute path="/documents"   pageName="Documents">  <DocumentManager /> </PermRoute>} />
          <Route path="/inbox"       element={<PermRoute path="/inbox"       pageName="Email Inbox"><EmailInbox />      </PermRoute>} />

          {/* ── Designer / Developer ── */}
          <Route path="/case-designer"  element={<PermRoute path="/case-designer"  pageName="Case Designer"> <CaseDesigner />    </PermRoute>} />
          <Route path="/case-config"    element={<PermRoute path="/case-config"    pageName="Case Configuration"> <CaseConfig />  </PermRoute>} />
          <Route path="/form-builder"   element={<PermRoute path="/form-builder"   pageName="Form Builder">  <FormBuilderPage /> </PermRoute>} />
          <Route path="/reference-tables" element={<PermRoute path="/reference-tables" pageName="Reference Tables"> <ReferenceTablesPage /> </PermRoute>} />
          <Route path="/nlp-builder"    element={<PermRoute path="/nlp-builder"    pageName="NLP Builder">   <NLPBuilder />      </PermRoute>} />
          <Route path="/modeler"        element={<PermRoute path="/modeler"        pageName="BPMN Modeler">  <Modeler />         </PermRoute>} />
          <Route path="/app-builder"    element={<PermRoute path="/app-builder"    pageName="App Builder">   <AppBuilder />      </PermRoute>} />
          <Route path="/hxwork"         element={<PermRoute path="/hxwork"         pageName="HxWork">        <HxWork />          </PermRoute>} />
          <Route path="/hxbranch"       element={<PermRoute path="/hxbranch"       pageName="HxBranch">      <HxBranch />        </PermRoute>} />
          <Route path="/graph"          element={<PermRoute path="/graph"          pageName="HxGraph">       <HxGraph />         </PermRoute>} />
          <Route path="/process-mining" element={<PermRoute path="/process-mining" pageName="Process Mining"><ProcessMining />   </PermRoute>} />
          <Route path="/live-activity"  element={<PermRoute path="/live-activity"  pageName="Live Activity"> <LiveActivity />    </PermRoute>} />
          <Route path="/monitor"                       element={<PermRoute path="/monitor" pageName="Monitor"><Monitor /></PermRoute>} />
          <Route path="/monitor/:processId"            element={<PermRoute path="/monitor" pageName="Monitor"><Monitor /></PermRoute>} />
          <Route path="/monitor/:processId/:instanceId" element={<PermRoute path="/monitor" pageName="Monitor"><Monitor /></PermRoute>} />
          <Route path="/escalation" element={<PermRoute path="/escalation" pageName="Escalation Trees"><EscalationEditor /></PermRoute>} />
          <Route path="/sitemap"    element={<PermRoute path="/sitemap"    pageName="Site Map">        <SiteMap />          </PermRoute>} />

          {/* ── DevOps ── */}
          <Route path="/deploy"        element={<PermRoute path="/deploy"       pageName="HxDeploy">    <HxDeploy />    </PermRoute>} />
          <Route path="/hxmigrate"    element={<PermRoute path="/hxmigrate"    pageName="HxMigrate">   <HxMigrate />   </PermRoute>} />
          <Route path="/hxdbmigrate"  element={<PermRoute path="/hxdbmigrate"  pageName="HxDBMigrate"> <HxDBMigrate /> </PermRoute>} />
          <Route path="/hxreplay"     element={<PermRoute path="/hxreplay"     pageName="HxReplay">    <HxReplay />    </PermRoute>} />
          <Route path="/risk-review"  element={<PermRoute path="/risk-review"  pageName="Risk Review"> <RiskReview />  </PermRoute>} />
          <Route path="/channels"     element={<PermRoute path="/channels"     pageName="Channel Inbox"><ChannelInbox /></PermRoute>} />
          <Route path="/bots"         element={<PermRoute path="/bots"         pageName="Bots">         <BotConsole />   </PermRoute>} />
          <Route path="/calendar"     element={<PermRoute path="/calendar"     pageName="Calendar">   <Calendar />  </PermRoute>} />
          <Route path="/arbiter"      element={<PermRoute path="/arbiter"      pageName="HxArbiter">  <Arbiter />   </PermRoute>} />
          <Route path="/workforce"    element={<PermRoute path="/workforce"    pageName="Agent Workforce"><Workforce /></PermRoute>} />
          <Route path="/model-market" element={<PermRoute path="/model-market" pageName="Model Marketplace"><ModelMarket /></PermRoute>} />
          <Route path="/rep-desk"    element={<PermRoute path="/rep-desk"    pageName="Rep Desk"><RepDesk /></PermRoute>} />
          <Route path="/healing"     element={<PermRoute path="/healing"     pageName="Self-Healing"><Healing /></PermRoute>} />
          <Route path="/case-transfers" element={<PermRoute path="/case-transfers" pageName="Case Transfers"><TenantTransfers /></PermRoute>} />
          <Route path="/field-ops"   element={<PermRoute path="/field-ops"   pageName="Field Operations"><FieldOps /></PermRoute>} />
          <Route path="/intent"      element={<PermRoute path="/intent"      pageName="Intent Execution"><Intent /></PermRoute>} />
          <Route path="/joint-contracts" element={<PermRoute path="/joint-contracts" pageName="Joint Contracts"><Negotiate /></PermRoute>} />
          <Route path="/scheduler"    element={<PermRoute path="/scheduler"    pageName="Scheduler">    <Scheduler />    </PermRoute>} />
          <Route path="/scout"        element={<PermRoute path="/scout"        pageName="Scout">        <Scout />        </PermRoute>} />
          <Route path="/scout-ai"     element={<PermRoute path="/scout-ai"     pageName="Scout AI">     <ScoutAI />      </PermRoute>} />
          <Route path="/orchestrator" element={<PermRoute path="/orchestrator" pageName="Orchestrator"> <Orchestrator /> </PermRoute>} />

          {/* ── Integration ── */}
          <Route path="/marketplace" element={<PermRoute path="/marketplace" pageName="Marketplace"><Marketplace /></PermRoute>} />
          <Route path="/hxconnect" element={<PermRoute path="/hxconnect" pageName="HxConnect">      <HxConnect /> </PermRoute>} />
          <Route path="/hxbridge"  element={<PermRoute path="/hxbridge"  pageName="HxBridge">       <HxBridge />  </PermRoute>} />
          <Route path="/devconn"   element={<PermRoute path="/devconn"   pageName="Dev Connectors">  <DevConn />   </PermRoute>} />
          <Route path="/testsuite" element={<PermRoute path="/testsuite" pageName="Test Suite">       <HxTest />   </PermRoute>} />
          <Route path="/hxcheckout" element={<PermRoute path="/hxcheckout" pageName="HxCheckout">      <HxCheckout /></PermRoute>} />
          <Route path="/hxstorefront" element={<PermRoute path="/hxstorefront" pageName="HxStorefront"><HxStorefront /></PermRoute>} />
          <Route path="/hxsync"    element={<PermRoute path="/hxsync"    pageName="HxSync">          <HxSync />    </PermRoute>} />
          <Route path="/hxfusion"  element={<PermRoute path="/hxfusion"  pageName="HxFusion">        <HxFusion />  </PermRoute>} />

          {/* ── Security ── */}
          <Route path="/hxshield"      element={<PermRoute path="/hxshield"      pageName="HxShield">     <HxShield />              </PermRoute>} />
          <Route path="/hxstream"      element={<PermRoute path="/hxstream"      pageName="HxStream">     <HxStream />              </PermRoute>} />
          <Route path="/hxstatus"      element={<PermRoute path="/hxstatus"      pageName="HxStatus">     <HxStatus />              </PermRoute>} />
          <Route path="/allowed-sites" element={<PermRoute path="/allowed-sites" pageName="Allowed sites"><AllowedSites /></PermRoute>} />
          <Route path="/hxlogs"        element={<PermRoute path="/hxlogs"        pageName="HxLogs">       <HxLogs />                </PermRoute>} />
          <Route path="/hxdbmanager"   element={<PermRoute path="/hxdbmanager"   pageName="HxDBManager">  <HxDBManager />           </PermRoute>} />
          <Route path="/compliance"    element={<PermRoute path="/compliance"    pageName="Compliance">   <ComplianceDashboard />   </PermRoute>} />
          <Route path="/observability" element={<PermRoute path="/observability" pageName="Observability"><ObservabilityDashboard /></PermRoute>} />

          {/* ── Admin only ── */}
          <Route path="/portal-admin"   element={<PermRoute path="/portal-admin"   pageName="Portals Management"> <PortalAdmin />      </PermRoute>} />
          {/* /access-directory is the unified page; old paths redirect there */}
          <Route path="/access-directory" element={<PermRoute path="/access-directory" pageName="Access Directory"><AccessGroupAdmin /></PermRoute>} />
          <Route path="/access-groups"    element={<PermRoute path="/access-directory" pageName="Access Directory"><AccessGroupAdmin /></PermRoute>} />
          <Route path="/user-directory"   element={<PermRoute path="/access-directory" pageName="Access Directory"><AccessGroupAdmin /></PermRoute>} />
          <Route path="/admin"          element={<PermRoute path="/admin"          pageName="Admin Console">       <AdminConsole />     </PermRoute>} />
          <Route path="/tenants"        element={<PermRoute path="/tenants"        pageName="Tenants">             <Tenants />          </PermRoute>} />
          <Route path="/enterprise"     element={<PermRoute path="/enterprise"     pageName="Enterprise">          <Enterprise />       </PermRoute>} />
          <Route path="/email-admin"    element={<PermRoute path="/email-admin"    pageName="Email Admin">         <EmailAdmin />       </PermRoute>} />
          <Route path="/push-admin"     element={<PermRoute path="/push-admin"     pageName="Push Notifications">  <PushAdmin />        </PermRoute>} />
          <Route path="/hxglobal"       element={<PermRoute path="/hxglobal"       pageName="HxGlobal">            <HxGlobal />         </PermRoute>} />
          {/* Signed in already: /login (a bookmark, a link in a mail) goes home. */}
          <Route path="/login" element={<Navigate to="/" replace />} />
          {/* Any other unknown address said nothing — a blank page. */}
          <Route path="*" element={<UnknownPage />} />
        </Route>
        {/* Public portal — no auth, no AppLayout */}
        <Route path="/portal/:slug/login" element={<PortalLogin />} />
        <Route path="/portal/:slug" element={<Portal />} />
        <Route path="/portal/:slug/*" element={<Portal />} />
        {/* Public HxMeet guest join — single-use invite link, no auth */}
        <Route path="/meet/join" element={<MeetJoin />} />
        {/* SSO landing — the user has no session yet, so it cannot be protected */}
        <Route path="/auth/sso/callback" element={<SsoCallback />} />
</Routes>
      </Suspense>
     </SamlHandoffGate>
    </PermissionsProvider></BrowserRouter></FeatureFlagsProvider></AuthProvider></ThemeProvider>
  </React.StrictMode>
);
