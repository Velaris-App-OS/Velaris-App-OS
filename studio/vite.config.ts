import { defineConfig, Plugin, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { securityHeaders } from "./csp.config";

/**
 * Warn when a non-auth file calls raw fetch() instead of apiClient.
 *
 * Exempt files (raw fetch is intentional):
 *   src/auth/         — login, refresh, SSO flows exist before tokens
 *   src/shared/api/client.ts — the canonical wrapper that wraps fetch itself
 *
 * All other .ts/.tsx files that contain `fetch(` (word-boundary match to
 * exclude `prefetch(`, `fetchData(` etc.) emit a Vite warning at build/dev
 * transform time. This does NOT break the build — it guides developers to
 * use apiClient so auth headers are always included.
 */
function noRawFetchPlugin(): Plugin {
  const RAW_FETCH = /\bfetch\s*\(/;
  const EXEMPT = [
    `${path.sep}src${path.sep}auth${path.sep}`,
    `${path.sep}src${path.sep}shared${path.sep}api${path.sep}client.ts`,
  ];

  return {
    name: "no-raw-fetch",
    transform(code, id) {
      if (!id.endsWith(".ts") && !id.endsWith(".tsx")) return;
      if (EXEMPT.some((seg) => id.includes(seg))) return;
      if (RAW_FETCH.test(code)) {
        this.warn(
          `raw fetch() detected — use apiClient from @shared/api/client so auth headers are always included`
        );
      }
    },
  };
}

// Shared by the dev server and by `vite preview`. **`preview` does NOT inherit
// `server.proxy`** — a production unit running preview without this serves the
// SPA fine and 404s every single API call.
//
// Targets are 127.0.0.1, never `localhost`: the gateway and the engine listen on
// 127.0.0.1 only, and where `localhost` also resolves to ::1 (Debian, RHEL,
// containers) Node tries ::1 first, is refused and does not retry — every API
// call answered 500 on a clean box (R6, 2026-10-30).
const apiProxy = {
    "/api/v1/testsuite": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/hxtest": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/hxdbmigrate": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/hxevolve": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/hxreplay": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/costing": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/cases": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/case-types": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/queues": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/assignments": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/my": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/rules":{ 
      target: "http://127.0.0.1:8200", 
      changeOrigin: true,
    },
   "/api/v1/data-models":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/enterprise":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/realtime":{
      target: "ws://127.0.0.1:8200",
      ws: true,
      changeOrigin: true,
    },
    "/api/v1/orchestrator":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/sitemap":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/scout-ai":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/codegen":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/tenants":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/scout":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/nlp":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/process-mining":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/auth":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // Enterprise identity P2 — the Studio side of this is the token
    // mint/revoke screen under /api/v1/scim/tokens; the /v2 half is called by
    // the customer's IdP, not by the browser.
    "/api/v1/scim":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // Omnichannel — the Channel Inbox and the case Channels tab.
    "/api/v1/channels":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // RPA — the bot registry, job list and attended proposal diff.
    "/api/v1/bots":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // Timed actions — the schedule list, its review gate and run history.
    "/api/v1/scheduler":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/admin":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/webhooks":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/analytics":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/form-submissions":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/observability":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/documents":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/email":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/compliance":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/legal-holds":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/user-directory":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/escalation-trees":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/push":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/hxnexus":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/portal-admin":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/portal":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/portals":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/access-roles":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/access-groups":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/knowledge": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/graph": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/apps": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/importer": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/analytics": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/hxbridge": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/sync": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/global": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/shield": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // Audit streaming to a SIEM (mig 200). Missing until the Enterprise screen
    // was built on 2026-09-10 — the documented trap: without an entry here the
    // browser's calls fall through to the engine on 8100 and 404, while curl
    // against :8200 works perfectly.
    "/api/v1/audit-stream": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/fusion": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/payments": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/identity": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/esign": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/crm": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/invoices": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/comms": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/docintel": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/devconn": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/hxmigrate": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/arbiter": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // F-00 agent identity. No Studio screen yet — the entry is here because a
    // router without one is the trap this allowlist exists to prevent, and the
    // first UI to call it would otherwise hit the catch-all instead.
    "/api/v1/agents": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // F-06 ambient capture. Same reason as the line above: the router exists,
    // so the allowlist entry exists, whether or not a screen calls it yet.
    "/api/v1/capture": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // F-07 sovereign model marketplace.
    "/api/v1/model-market": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // F-08 … F-12 and the Customer Engagement rep desk. None of these has a
    // Studio screen yet, and the entries exist anyway for the same reason
    // F-00's does: a router without an allowlist entry is precisely the trap
    // this list prevents, and the first UI to call one would silently hit the
    // catch-all instead of the gateway.
    "/api/v1/healing": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/handoff": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/field": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/intent": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/negotiate": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/engage": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/calendar": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/deploy": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/hxwork": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/hxcanvas": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/hxdocs": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/intake": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/branches": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/commits": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/hxlogs": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/webhooks": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/hxstream/ws": {
      target: "ws://127.0.0.1:8200",
      ws: true,
      changeOrigin: true,
    },
    "/api/v1/hxstream":{
      target: "http://127.0.0.1:8200",
      ws: true,
      changeOrigin: true,
    },
    "/api/v1/hxdbmanager": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // 198 — case status set and action configuration. `/api/v1/cases` already
    // routes to 8200 above; this is the separate authoring router.
    "/api/v1/case-config": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // HxStatus (§8.8). Must sit BEFORE the "/api" catch-all below, or the
    // browser 404s while curl against :8200 works perfectly — the documented
    // trap for every new case-service router.
    "/api/v1/status": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/releases": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/marketplace": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // HxCheckout + HxStorefront (commerce-service, R5e step 7). Missing until
    // 2026-10-19, so both Studio modules fell through to the engine catch-all.
    "/api/v1/checkout": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/storefront": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/auth/me":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/auth/switch-context":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/sla":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/forms":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/form-sections": {   // Reusable form sections (Form Builder §5 step 6)
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/reference-tables":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/admin":{
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/meet": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
      ws: true,  // P4a-live caption stream
    },
    "/api/v1/stt": {   // Voice input — local speech-to-text
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/voicefill": {   // Voice input P3 — transcript to form-field candidates
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/variables": {
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/risk": {   // HxSentinel (findings, rule config, verify-gate holds)
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/platform": {   // Platform update orchestrator (banner + rollout plans)
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/rollouts": {   // Staged case-type rollouts (HxDeploy → Rollouts tab; was 404 here)
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/map": {   // Map tiles for the Form Builder Location field (gateway, static)
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    "/api/v1/csp-reports": {   // what Studio's Content-Security-Policy blocked / would block
      target: "http://127.0.0.1:8200",
      changeOrigin: true,
    },
    // Catch-all LAST: anything not matched above goes to the engine (8100)
    // with /api stripped. A new case-service router MUST be allowlisted
    // above, or its browser calls silently 404 on the engine.
    "/api": {
      target: "http://127.0.0.1:8100",
      changeOrigin: true,
      rewrite: (path) => path.replace(/^\/api/, ""),
    },
};

// Every route names the browser's address in X-Velaris-Client — set here,
// overwriting anything the browser sent. The gateway keys its per-client limits
// on it (from a loopback peer only) and strips it, so the services see nothing
// new. Without it every Studio user was one client sharing one 20 r/s bucket
// (R6, 2026-10-30). Not X-Forwarded-For: the services read that one, and their
// own per-address limit would start applying where it never has.
//
// Behind a TLS front on this box (deployment guide: nginx → 127.0.0.1:5173) the
// peer is loopback, so the front's right-most X-Forwarded-For entry — the
// address the front itself saw — is used instead; from any other peer the
// header is the browser's own and is ignored.
const LOOPBACK = new Set(["127.0.0.1", "::1", "::ffff:127.0.0.1"]);
const nameTheClient: ProxyOptions["configure"] = (proxy) => {
  const name = (
    proxyReq: { setHeader(k: string, v: string): void },
    req: { socket: { remoteAddress?: string }; headers: Record<string, string | string[] | undefined> },
  ) => {
    const peer = req.socket.remoteAddress ?? "";
    const fwd = LOOPBACK.has(peer) ? String(req.headers["x-forwarded-for"] ?? "").split(",").map((a) => a.trim()).filter(Boolean).pop() : undefined;
    proxyReq.setHeader("X-Velaris-Client", fwd || peer);
  };
  proxy.on("proxyReq", name);
  proxy.on("proxyReqWs", name);
  // F3 (R6 failure tests, 2026-11-03): with the gateway down — its crash, or the
  // ~10 s an update recreates it — Vite answered 500, which reads as a bug in
  // the platform. Not reaching the upstream is a 502 (504 on a timeout), with
  // Retry-After. Registered before Vite's own handler, which then sees the
  // response already sent and only logs.
  proxy.on("error", (err: NodeJS.ErrnoException, _req: unknown, res: unknown) => {
    const r = res as { headersSent?: boolean; writableEnded?: boolean; req?: unknown;
                       writeHead?: (s: number, h: Record<string, string>) => { end(b: string): void } };
    if (!r || !("req" in r) || r.headersSent || r.writableEnded || !r.writeHead) return;
    const timeout = err.code === "ETIMEDOUT" || err.code === "ESOCKETTIMEDOUT";
    r.writeHead(timeout ? 504 : 502, { "Content-Type": "application/json", "Retry-After": "5" })
      .end(JSON.stringify({
        detail: "The platform is not answering right now (restarting or updating). Try again in a moment.",
        code: timeout ? "VEL-STUDIO-UPSTREAM-TIMEOUT" : "VEL-STUDIO-UPSTREAM-DOWN",
      }));
  });
};
for (const route of Object.values(apiProxy)) Object.assign(route, { configure: nameTheClient });

export default defineConfig({
  plugins: [react(), noRawFetchPlugin()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
      "@shared": path.resolve(__dirname, "src/shared"),
      "@modules": path.resolve(__dirname, "src/modules"),
    },
  },
  server: {
    port: 5173,
    allowedHosts: true,
    fs: {
      // Allow serving files from root node_modules (hoisted monorepo deps)
      allow: [path.resolve(__dirname, ".."), "/home/utpal-bhadra/helix/node_modules"],
    },
    proxy: apiProxy,
  },
  preview: {
    port: 5173,
    allowedHosts: true,
    proxy: apiProxy,
    // Security headers on the production server only (csp.config.ts):
    // report-only unless VELARIS_STUDIO_CSP says otherwise.
    headers: securityHeaders(process.env),
  },
});
