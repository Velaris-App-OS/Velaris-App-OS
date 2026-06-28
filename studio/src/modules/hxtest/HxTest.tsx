/**
 * #27 — Test Suite (platform) + HxTest (marketplace add-on)
 * Run · History · Conformance hit the always-on core Test Suite
 * (/api/v1/testsuite). Generated is the HxTest marketplace layer
 * (/api/v1/hxtest) — install-gated: when HxTest is not installed the API 404s
 * and the tab shows an "install from Marketplace" notice instead.
 */
import React, { useState, useEffect, useCallback } from "react";
import { Button } from "@shared/components";

const TS = "/api/v1/testsuite";
const HX = "/api/v1/hxtest";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { "Content-Type": "application/json", ..._authHdr(), ...opts.headers } });
}

const S: Record<string, React.CSSProperties> = {
  page:    { padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" as const },
  header:  { marginBottom: "var(--space-lg)" },
  title:   { fontSize: 24, fontWeight: 700, margin: 0, color: "var(--text-primary)" },
  sub:     { fontSize: 13, color: "var(--text-muted)", marginTop: 4 },
  tabs:    { display: "flex", gap: 2, marginBottom: "var(--space-lg)", borderBottom: "1px solid var(--border)" },
  tab:     { padding: "8px 18px", fontSize: 13, fontWeight: 500, background: "none", border: "none", borderBottom: "2px solid transparent", cursor: "pointer", color: "var(--text-muted)", marginBottom: -1 },
  tabActive: { borderBottomColor: "var(--accent)", color: "var(--accent)", fontWeight: 700 },
  card:    { background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8, padding: "var(--space-lg)", marginBottom: "var(--space-md)" },
  label:   { fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase" as const, letterSpacing: "0.05em", marginBottom: 4 },
  input:   { width: "100%", padding: "8px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const, marginBottom: 10 },
  row:     { display: "flex", gap: 10, alignItems: "center", padding: "10px 14px", borderBottom: "1px solid var(--border-subtle)" },
  badge:   { fontSize: 10, padding: "2px 8px", borderRadius: 4, fontWeight: 700, textTransform: "uppercase" as const, color: "#fff" },
  mono:    { fontFamily: "var(--font-mono)", fontSize: 11, whiteSpace: "pre-wrap" as const },
};

const statusColor = (s: string) => ({
  passed: "#22c55e", failed: "#ef4444", partial: "#f59e0b", error: "#ef4444",
  running: "#3b82f6", skipped: "#94a3b8",
}[s] ?? "#94a3b8");

function StatusBadge({ s }: { s: string }) {
  return <span style={{ ...S.badge, background: statusColor(s) }}>{s}</span>;
}

// ── Run tab ───────────────────────────────────────────────────────────────────
function RunTab() {
  const [suites, setSuites] = useState<any[]>([]);
  const [running, setRunning] = useState<string | null>(null);
  const [result, setResult] = useState<any>(null);

  useEffect(() => {
    // Builtin suites run by name; stored (e.g. generated) run by their uuid —
    // the /run endpoint accepts either (see _resolve_suite). Merge both so
    // generated suites are runnable here, not just visible in the Generated tab.
    authFetch(`${TS}/suites`).then(r => r.json()).then(d => {
      const builtin = (d.builtin || []).map((s: any) => ({ ...s, ref: s.name }));
      const stored  = (d.stored  || []).map((s: any) => ({ ...s, ref: s.id }));
      setSuites([...builtin, ...stored]);
    }).catch(() => {});
  }, []);

  const run = async (ref: string, isolate: boolean) => {
    setRunning(ref); setResult(null);
    try {
      const r = await authFetch(`${TS}/run`, { method: "POST", body: JSON.stringify({ suite: ref, isolate }) });
      setResult(await r.json());
    } finally { setRunning(null); }
  };

  return (
    <div>
      {suites.map(s => (
        <div key={s.ref} style={S.card}>
          <div style={S.row}>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 600, fontSize: 14 }}>{s.name}</div>
              <div style={S.sub}>{s.suite_type} · {s.count} test{s.count === 1 ? "" : "s"}</div>
            </div>
            <Button onClick={() => run(s.ref, false)} disabled={!!running}>
              {running === s.ref ? "Running…" : "Run"}
            </Button>
            <Button onClick={() => run(s.ref, true)} disabled={!!running} variant="secondary">
              Run isolated
            </Button>
          </div>
        </div>
      ))}
      {result && (
        <div style={S.card}>
          <div style={S.label}>Last run</div>
          <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
            <StatusBadge s={result.status} />
            <span>{result.passed}/{result.total} passed · {result.failed} failed · {result.skipped} skipped</span>
          </div>
        </div>
      )}
    </div>
  );
}

// ── History tab ─────────────────────────────────────────────────────────────
function HistoryTab() {
  const [runs, setRuns] = useState<any[]>([]);
  const [detail, setDetail] = useState<any>(null);

  const load = useCallback(() => {
    authFetch(`${TS}/runs`).then(r => r.json())
      .then(d => setRuns(Array.isArray(d) ? d : []))   // never feed a non-array to .map()
      .catch(() => setRuns([]));
  }, []);
  useEffect(load, [load]);

  const open = async (id: string) => {
    const r = await authFetch(`${TS}/runs/${id}`);
    setDetail(await r.json());
  };

  return (
    <div>
      <div style={{ marginBottom: 10 }}><Button onClick={load} variant="secondary">Refresh</Button></div>
      {runs.map(r => (
        <div key={r.id} style={{ ...S.row, cursor: "pointer" }} onClick={() => open(r.id)}>
          <StatusBadge s={r.status} />
          <div style={{ flex: 1 }}>{r.suite_name}</div>
          <span style={S.sub}>{r.passed}/{r.total} · {new Date(r.started_at).toLocaleString()}</span>
        </div>
      ))}
      {detail && (
        <div style={S.card}>
          <div style={S.label}>{detail.suite_name} — {detail.status}</div>
          {(detail.results || []).map((t: any, i: number) => (
            <div key={i} style={S.row}>
              <StatusBadge s={t.status} />
              <div style={{ flex: 1 }}>{t.test_name || t.test_id}</div>
              {t.error_detail && <span style={{ ...S.mono, color: "#ef4444" }}>{t.error_detail}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Generated tab ─────────────────────────────────────────────────────────────
// Structural generation is CORE (always available, /testsuite/generate). AI
// scenarios are the HxTest add-on (/hxtest/generate) — shown only when installed.
function GeneratedTab() {
  const [gen, setGen] = useState<any[]>([]);
  const [caseTypes, setCaseTypes] = useState<any[]>([]);
  const [ctId, setCtId] = useState("");
  const [scenarios, setScenarios] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [hxInstalled, setHxInstalled] = useState<boolean | null>(null);  // HxTest add-on present?

  const load = useCallback(() => {
    // Generated suites come from the core endpoint — works without HxTest.
    authFetch(`${TS}/suites`).then(r => r.json())
      .then(d => setGen(((d && d.stored) || []).filter((s: any) => s.suite_type === "generated")))
      .catch(() => setGen([]));
    // Probe whether the HxTest AI add-on is installed (gates the AI checkbox).
    authFetch(`${HX}/generated`).then(r => setHxInstalled(r.status !== 404)).catch(() => setHxInstalled(false));
  }, []);
  useEffect(load, [load]);

  // Case types for the picker (same source as the New Case form).
  useEffect(() => {
    authFetch("/api/v1/case-types?page_size=200").then(r => r.ok ? r.json() : null)
      .then(d => setCaseTypes(Array.isArray(d?.items) ? d.items : []))
      .catch(() => setCaseTypes([]));
  }, []);

  const generate = async () => {
    setMsg(null);
    const useAi = hxInstalled && scenarios;
    const url = useAi ? `${HX}/generate` : `${TS}/generate`;
    const body = useAi ? { case_type_id: ctId, include_scenarios: true } : { case_type_id: ctId };
    const r = await authFetch(url, { method: "POST", body: JSON.stringify(body) });
    const d = await r.json();
    setMsg(r.ok
      ? `Generated ${d.total} test(s): ${d.structural} structural${useAi ? ` + ${d.scenario} AI scenario` : ""}`
      : (d.detail || "failed"));
    load();
  };

  return (
    <div>
      <div style={S.card}>
        <div style={S.label}>Generate tests for a case type</div>
        <select style={S.input} value={ctId} onChange={e => setCtId(e.target.value)}>
          <option value="">Select a case type…</option>
          {caseTypes.map(ct => (
            <option key={ct.id} value={ct.id}>{ct.name}{ct.version ? ` (v${ct.version})` : ""}</option>
          ))}
        </select>
        {hxInstalled ? (
          <label style={{ fontSize: 13, display: "flex", gap: 6, marginBottom: 10 }}>
            <input type="checkbox" checked={scenarios} onChange={e => setScenarios(e.target.checked)} />
            Include AI scenario tests (HxTest; advisory, skipped if AI unavailable)
          </label>
        ) : (
          <div style={{ ...S.sub, marginBottom: 10 }}>
            Structural tests only. Install the <code>velaris/hxtest</code> add-on from the Marketplace
            to also generate AI scenario tests.
          </div>
        )}
        <Button onClick={generate} disabled={!ctId}>Generate</Button>
        {msg && <div style={{ ...S.sub, marginTop: 8 }}>{msg}</div>}
      </div>
      {gen.map(s => (
        <div key={s.id} style={S.row}>
          <div style={{ flex: 1 }}>{s.name}</div>
          {s.ai_stale && (
            <span style={{ ...S.badge, background: "#f59e0b" }}
              title={hxInstalled
                ? "The case type / rules / integration changed — regenerate with AI scenarios checked to refresh."
                : "AI scenarios are out of date. Install HxTest to regenerate them."}>
              AI stale
            </span>
          )}
          <span style={S.sub}>v{s.version} · {s.count} tests · {s.source}</span>
        </div>
      ))}
    </div>
  );
}

// ── Conformance tab ───────────────────────────────────────────────────────────
function ConformanceTab() {
  const [pkg, setPkg] = useState('{\n  "manifest": {"name": "My App", "version": "1.0.0"},\n  "case_types": [],\n  "forms": [],\n  "rules": []\n}');
  const [wsId, setWsId] = useState("");
  const [result, setResult] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);

  const run = async () => {
    setErr(null); setResult(null);
    let parsed: any;
    try { parsed = JSON.parse(pkg); } catch { setErr("Package JSON is invalid"); return; }
    const r = await authFetch(`${TS}/conformance`, {
      method: "POST", body: JSON.stringify({ package: parsed, workspace_id: wsId || null }),
    });
    const d = await r.json();
    if (!r.ok) { setErr(d.detail || "failed"); return; }
    setResult(d);
  };

  return (
    <div>
      <div style={S.card}>
        <div style={S.label}>Package contents</div>
        <textarea style={{ ...S.input, height: 180, ...S.mono }} value={pkg} onChange={e => setPkg(e.target.value)} />
        <div style={S.label}>Workspace id (optional — attaches the result for the submit gate)</div>
        <input style={S.input} placeholder="workspace uuid" value={wsId} onChange={e => setWsId(e.target.value)} />
        <Button onClick={run}>Run structural conformance</Button>
        {err && <div style={{ ...S.sub, color: "#ef4444", marginTop: 8 }}>{err}</div>}
      </div>
      {result && (
        <div style={S.card}>
          <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
            <StatusBadge s={result.passed ? "passed" : "failed"} />
            <span>{result.total - result.failed}/{result.total} checks passed</span>
          </div>
          <div style={{ ...S.sub, marginTop: 8 }}>
            {result.passed ? "Workspace is submittable." : "Fix the failing checks before submission."}
          </div>
        </div>
      )}
    </div>
  );
}

const TABS: Record<string, () => JSX.Element> = {
  Run: RunTab, History: HistoryTab, Generated: GeneratedTab, Conformance: ConformanceTab,
};

export default function HxTest() {
  const [tab, setTab] = useState("Run");
  const Active = TABS[tab];
  return (
    <div style={S.page}>
      <div style={S.tabs}>
        {Object.keys(TABS).map(t => (
          <button key={t} style={{ ...S.tab, ...(tab === t ? S.tabActive : {}) }} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>
      <Active />
    </div>
  );
}
