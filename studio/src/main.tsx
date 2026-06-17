import React from "react";
import { AuthProvider } from "@/auth";
import { PermissionsProvider, usePermissions } from "@/auth/PermissionsContext";
import { ThemeProvider } from "@/theme/ThemeContext";
import ProtectedRoute from "@/auth/ProtectedRoute";
import RequireRole from "@/auth/RequireRole";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route } from "react-router-dom";

// ── Global 401 interceptor ────────────────────────────────────────────────────
// Monkey-patch fetch once at boot. Any API response with status 401 dispatches
// a "velaris:unauthorized" event ONLY when there is an active session token,
// so unauthenticated probes (SSO discovery, portal, etc.) are silently ignored.
// Excluded paths: auth endpoints that legitimately return 401 to anonymous users.
const _EXCLUDED_401 = [
  "/auth/login",
  "/auth/real/login",
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
  const response = await _origFetch(...args);
  if (response.status === 401) {
    const url = typeof args[0] === "string" ? args[0] : (args[0] as Request).url;
    const isExcluded = _EXCLUDED_401.some(p => url.includes(p));
    // Only treat as "session expired" when the user actually had a token
    const hasSession = !!localStorage.getItem("helix_token");
    if (!isExcluded && hasSession) {
      window.dispatchEvent(new CustomEvent("velaris:unauthorized"));
    }
  }
  return response;
};
import AppLayout from "@/app/AppLayout";
import { FeatureFlagsProvider } from "@/app/FeatureFlagsContext";
import Dashboard from "@modules/dashboard/Dashboard";
import Modeler from "@modules/modeler/Modeler";
import Monitor from "@modules/monitor/Monitor";
import CaseDesigner from "@modules/case-designer/CaseDesigner";
import CaseManager from "@modules/case-manager/CaseManager";
import WorkCenter from "@modules/work-center/WorkCenter";
import Analytics from "@modules/analytics/Analytics";
import AdminConsole from "@modules/admin/AdminConsole";
import ProcessMining from "@modules/process-mining/ProcessMining";
import NLPBuilder from "@modules/nlp-builder/NLPBuilder";
import Scout from "@modules/scout/Scout";
import ScoutAI from "@modules/scout-ai/ScoutAI";
import Orchestrator from "@modules/orchestrator/Orchestrator";
import LiveActivity from "@modules/live-activity/LiveActivity";
import Enterprise from "@modules/enterprise/Enterprise";
import SiteMap from "@modules/sitemap/SiteMap";
import Tenants from "@modules/tenants/Tenants";
import AppBuilder from "@modules/app-builder/AppBuilder";
import ObservabilityDashboard from "@modules/observability/ObservabilityDashboard";
import DocumentManager from "@modules/documents/DocumentManager";
import EscalationEditor from "@modules/escalation/EscalationEditor";
import UserDirectory from "@modules/user-directory/UserDirectory";
import ComplianceDashboard from "@modules/compliance/ComplianceDashboard";
import EmailInbox from "@modules/email/EmailInbox";
import EmailAdmin from "@modules/email-admin/EmailAdmin";
import PushAdmin from "@modules/push-admin/PushAdmin";
import HxNexus from "@modules/hxnexus/HxNexus";
import Portal, { PortalLogin } from "@modules/portal/Portal";
import PortalAdmin from "@modules/portal/PortalAdmin";
import AccessGroupAdmin from "@modules/access-groups/AccessGroupAdmin";
import HxStream from "@modules/hxstream/HxStream";
import HelpCenter from "@modules/help/HelpCenter";
import HxGraph from "@modules/hxgraph/HxGraph";
import FormBuilderPage from "@modules/form-builder/FormBuilderPage";
import HxBranch from "@modules/hxbranch/HxBranch";
import HxLogs from "@modules/hxlogs/HxLogs";
import HxDBManager from "@modules/hxdbmanager/HxDBManager";
import HxBridge from "@modules/hxbridge/HxBridge";
import HxAnalytics from "@modules/hxanalytics/HxAnalytics";
import HxSync from "@modules/hxsync/HxSync";
import HxGlobal from "@modules/hxglobal/HxGlobal";
import HxShield from "@modules/hxshield/HxShield";
import HxFusion from "@modules/hxfusion/HxFusion";
import Marketplace from "@modules/marketplace/Marketplace";
import HxConnect from "@modules/hxconnect/HxConnect";
import DevConn from "@modules/devconn/DevConn";
import HxTest from "@modules/hxtest/HxTest";
import HxMigrate from "@modules/hxmigrate/HxMigrate";
import HxDeploy from "@modules/hxdeploy/HxDeploy";
import HxWork from "@modules/hxwork/HxWork";
import HxCanvas from "@modules/hxcanvas/HxCanvas";
import HxDocs from "@modules/hxdocs/HxDocs";
import "@shared/styles/global.css";
function PermRoute({ path, pageName, children }: { path: string; pageName: string; children: React.ReactNode }) {
  const { getRouteRoles } = usePermissions();
  return <RequireRole allowedRoles={getRouteRoles(path)} pageName={pageName}>{children}</RequireRole>;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider><AuthProvider><FeatureFlagsProvider><BrowserRouter><PermissionsProvider>
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
          <Route path="/form-builder"   element={<PermRoute path="/form-builder"   pageName="Form Builder">  <FormBuilderPage /> </PermRoute>} />
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
          <Route path="/scout"        element={<PermRoute path="/scout"        pageName="Scout">        <Scout />        </PermRoute>} />
          <Route path="/scout-ai"     element={<PermRoute path="/scout-ai"     pageName="Scout AI">     <ScoutAI />      </PermRoute>} />
          <Route path="/orchestrator" element={<PermRoute path="/orchestrator" pageName="Orchestrator"> <Orchestrator /> </PermRoute>} />

          {/* ── Integration ── */}
          <Route path="/marketplace" element={<PermRoute path="/marketplace" pageName="Marketplace"><Marketplace /></PermRoute>} />
          <Route path="/hxconnect" element={<PermRoute path="/hxconnect" pageName="HxConnect">      <HxConnect /> </PermRoute>} />
          <Route path="/hxbridge"  element={<PermRoute path="/hxbridge"  pageName="HxBridge">       <HxBridge />  </PermRoute>} />
          <Route path="/devconn"   element={<PermRoute path="/devconn"   pageName="Dev Connectors">  <DevConn />   </PermRoute>} />
          <Route path="/testsuite" element={<PermRoute path="/testsuite" pageName="Test Suite">       <HxTest />   </PermRoute>} />
          <Route path="/hxsync"    element={<PermRoute path="/hxsync"    pageName="HxSync">          <HxSync />    </PermRoute>} />
          <Route path="/hxfusion"  element={<PermRoute path="/hxfusion"  pageName="HxFusion">        <HxFusion />  </PermRoute>} />

          {/* ── Security ── */}
          <Route path="/hxshield"      element={<PermRoute path="/hxshield"      pageName="HxShield">     <HxShield />              </PermRoute>} />
          <Route path="/hxstream"      element={<PermRoute path="/hxstream"      pageName="HxStream">     <HxStream />              </PermRoute>} />
          <Route path="/hxlogs"        element={<PermRoute path="/hxlogs"        pageName="HxLogs">       <HxLogs />                </PermRoute>} />
          <Route path="/hxdbmanager"   element={<PermRoute path="/hxdbmanager"   pageName="HxDBManager">  <HxDBManager />           </PermRoute>} />
          <Route path="/compliance"    element={<PermRoute path="/compliance"    pageName="Compliance">   <ComplianceDashboard />   </PermRoute>} />
          <Route path="/observability" element={<PermRoute path="/observability" pageName="Observability"><ObservabilityDashboard /></PermRoute>} />

          {/* ── Admin only ── */}
          <Route path="/portal-admin"   element={<PermRoute path="/portal-admin"   pageName="Customer Portal">     <PortalAdmin />      </PermRoute>} />
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
        </Route>
        {/* Public portal — no auth, no AppLayout */}
        <Route path="/portal/:slug/login" element={<PortalLogin />} />
        <Route path="/portal/:slug" element={<Portal />} />
        <Route path="/portal/:slug/*" element={<Portal />} />
</Routes>
    </PermissionsProvider></BrowserRouter></FeatureFlagsProvider></AuthProvider></ThemeProvider>
  </React.StrictMode>
);
