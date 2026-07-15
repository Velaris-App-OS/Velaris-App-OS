/**
 * HxReplay — counterfactual case replay ("what-if on real history").
 * Single Case: fork one real case against a candidate rule change, side-by-side.
 * Cohort: replay N historical cases, aggregate deltas + determinacy coverage.
 * Runs: history. Honesty first: coverage, exclusions, and the bias caveat ship
 * next to every number.
 */
import React, { useCallback, useEffect, useState } from "react";
import { Button } from "@shared/components";

const API = "/api/v1/hxreplay";

function authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t
    ? { Authorization: `Bearer ${t}`, "Content-Type": "application/json" }
    : { "Content-Type": "application/json" };
}
async function apiFetch(path: string, opts: RequestInit = {}) {
  return fetch(`${API}${path}`, { ...opts, headers: { ...authHdr(), ...(opts.headers ?? {}) } });
}

interface Metrics {
  cycle_time_seconds: number | null; event_count: number; auto_count: number;
  manual_count: number; auto_ratio: number | null; resolved: boolean;
  elided_wall_seconds?: number;
}
interface TraceNode {
  activity: string; activity_type: string; stage_id: string | null;
  actor_type: string | null; timestamp: string | null; outcome: string | null;
  _class?: string; _elided?: boolean;
}
interface RunResult {
  case_id: string; determinacy: string; exclusion_reason: string | null;
  divergence_point: string | null; baseline_metrics: Metrics;
  counterfactual_metrics: Metrics | null;
  trace?: { nodes: TraceNode[]; note?: string | null;
            input_coverage?: Record<string, string> };
}
interface Run {
  id: string; kind: string; status: string; case_id: string | null;
  anchored: boolean; error: string | null; created_at: string | null;
  summary?: any; result?: RunResult;
}

function fmtMoney(v: number | null | undefined, currency: string): string {
  if (v == null) return "—";
  return `${currency} ${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function CostCard({ cost }: { cost: any }) {
  if (!cost) return null;
  return (
    <div style={{ border: "1px solid #22c55e", borderRadius: 8, padding: "var(--space-lg)", marginBottom: "var(--space-md)" }}>
      <b style={{ color: "#22c55e" }}>Cost delta (determinate cases only)</b>
      <div style={{ fontSize: 30, fontWeight: 700 }}>{fmtMoney(cost.savings, cost.currency)} saved</div>
      <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
        baseline {fmtMoney(cost.baseline_cost, cost.currency)} · counterfactual {fmtMoney(cost.counterfactual_cost, cost.currency)}
        {" "}· rate {fmtMoney(cost.hourly_rate, cost.currency)}/h · {cost.cases} case(s)
      </div>
      <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 4 }}>{cost.basis}</div>
    </div>
  );
}

function RateCardEditor() {
  const [rate, setRate] = useState<string>("");
  const [currency, setCurrency] = useState("USD");
  const [configured, setConfigured] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    void (async () => {
      const r = await fetch("/api/v1/costing/rate-card", { headers: authHdr() });
      if (r.ok) {
        const b = await r.json();
        setConfigured(b.configured);
        if (b.configured) { setRate(String(b.hourly_rate)); setCurrency(b.currency); }
      }
    })();
  }, []);
  async function save() {
    setMsg(null);
    const r = await fetch("/api/v1/costing/rate-card", {
      method: "PUT", headers: authHdr(),
      body: JSON.stringify({ hourly_rate: Number(rate) || 0, currency }) });
    if (r.ok) { setConfigured(true); setMsg("Saved."); }
    else setMsg((await r.json()).detail ?? "Save failed");
  }
  return (
    <div style={S.card}>
      <b>Rate card</b>
      <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
        Tenant default hourly rate for manual work — powers the cost delta on replay
        results. {configured ? "" : "Not configured yet — replays show no cost block."}
      </div>
      <div style={{ ...S.row, marginTop: 8 }}>
        <input style={{ ...S.input, minWidth: 110 }} value={rate} placeholder="hourly rate"
               onChange={(e) => setRate(e.target.value)} />
        <input style={{ ...S.input, minWidth: 70 }} value={currency} maxLength={8}
               onChange={(e) => setCurrency(e.target.value)} />
        <Button onClick={save} disabled={!rate.trim()}>Save</Button>
        {msg && <span style={{ fontSize: 12 }}>{msg}</span>}
      </div>
    </div>
  );
}

const S: Record<string, React.CSSProperties> = {
  page:  { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  tabs:  { display: "flex", gap: "var(--space-sm)", padding: "var(--space-md) var(--space-2xl) 0" },
  tab:   { padding: "8px 14px", borderRadius: 8, cursor: "pointer", fontSize: 13, fontWeight: 600, border: "1px solid var(--border-subtle)" },
  body:  { flex: 1, overflow: "auto", padding: "var(--space-xl) var(--space-2xl)" },
  card:  { border: "1px solid var(--border-subtle)", borderRadius: 8, padding: "var(--space-lg)", marginBottom: "var(--space-md)" },
  row:   { display: "flex", gap: "var(--space-md)", alignItems: "center", flexWrap: "wrap" },
  input: { padding: "8px 10px", borderRadius: 6, border: "1px solid var(--border-subtle)", background: "var(--surface-2)", color: "var(--text-primary)", minWidth: 240 },
  label: { fontSize: 12, color: "var(--text-secondary)", marginBottom: 4, display: "block" },
  mono:  { fontFamily: "var(--font-mono, monospace)", whiteSpace: "pre-wrap", fontSize: 12, background: "var(--surface-2)", padding: "var(--space-md)", borderRadius: 6, maxHeight: 300, overflow: "auto" },
  pill:  { padding: "2px 8px", borderRadius: 999, fontSize: 11, fontWeight: 600 },
  half:  { flex: 1, minWidth: 320 },
  caveat:{ fontSize: 12, color: "var(--text-secondary)", borderLeft: "3px solid #f59e0b", paddingLeft: 10, marginTop: 8 },
  big:   { fontSize: 30, fontWeight: 700 },
};

const CLASS_COLOR: Record<string, string> = {
  copied: "var(--text-secondary)", elided: "#94a3b8", synthetic: "#22c55e", recomputed: "#3b82f6",
};

function fmtSecs(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

function MetricsBlock({ title, m }: { title: string; m: Metrics | null }) {
  if (!m) return <div style={S.card}><b>{title}</b><div>excluded</div></div>;
  return (
    <div style={{ ...S.card, ...S.half }}>
      <b>{title}</b>
      <div style={S.big}>{fmtSecs(m.cycle_time_seconds)}</div>
      <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
        {m.event_count} events · {m.manual_count} manual · {m.auto_count} auto
        {m.auto_ratio != null && <> · auto-ratio {(m.auto_ratio * 100).toFixed(0)}%</>}
        {!!m.elided_wall_seconds && <> · <b>{fmtSecs(m.elided_wall_seconds)} of work elided</b></>}
      </div>
    </div>
  );
}

function TraceView({ nodes }: { nodes: TraceNode[] }) {
  return (
    <div style={S.mono}>
      {nodes.map((n, i) => (
        <div key={i} style={{ color: CLASS_COLOR[n._class ?? "copied"], textDecoration: n._elided ? "line-through" : undefined }}>
          [{n._class ?? "copied"}] {n.timestamp?.slice(0, 19) ?? "—"}  {n.activity}
          {n.stage_id ? ` (stage ${n.stage_id})` : ""} {n.actor_type ? `· ${n.actor_type}` : ""}
          {n.outcome ? ` > ${n.outcome}` : ""}
        </div>
      ))}
    </div>
  );
}

const COVERAGE_LABEL: Record<string, string> = {
  lineage: "reconstructed from lineage",
  constant_redacted: "pii/secret — rules see “***” (parity)",
  absent: "did not exist at decision time",
  unknown: "NOT reconstructable",
};

function CoverageView({ coverage }: { coverage?: Record<string, string> }) {
  const entries = Object.entries(coverage ?? {});
  if (entries.length === 0) return null;
  return (
    <div style={{ marginTop: 8 }}>
      <b style={{ fontSize: 12 }}>Decision-time input coverage</b>
      <div style={S.mono}>
        {entries.map(([k, v]) => (
          <div key={k} style={{ color: v === "unknown" ? "#f59e0b" : undefined }}>
            {k} — {COVERAGE_LABEL[v] ?? v}
          </div>
        ))}
      </div>
    </div>
  );
}

const CANDIDATE_PLACEHOLDER = JSON.stringify(
  { rules: [{ id: "auto-approve", rule_type: "when", enabled: true,
      definition_json: { conditions: [{ field_path: "claim.amount", operator: "lt", value: 500 }],
                         actions: [{ action_type: "auto_approve" }] } }] }, null, 2);

export default function HxReplay() {
  const [tab, setTab] = useState<"single" | "cohort" | "runs">("single");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // single
  const [caseId, setCaseId] = useState("");
  const [candidateText, setCandidateText] = useState(CANDIDATE_PLACEHOLDER);
  const [branchId, setBranchId] = useState("");
  const [estimate, setEstimate] = useState(false);
  const [single, setSingle] = useState<Run | null>(null);

  // cohort
  const [caseTypeId, setCaseTypeId] = useState("");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [maxCases, setMaxCases] = useState("500");
  const [cohortRun, setCohortRun] = useState<Run | null>(null);

  // runs
  const [runs, setRuns] = useState<Run[]>([]);

  const loadRuns = useCallback(async () => {
    const r = await apiFetch("/runs");
    if (r.ok) setRuns((await r.json()).runs);
  }, []);
  useEffect(() => { if (tab === "runs") void loadRuns(); }, [tab, loadRuns]);

  // deep-link prefill (HxBranch review: "Simulate on history" → /hxreplay?branch=…)
  useEffect(() => {
    const qs = new URLSearchParams(window.location.search);
    const b = qs.get("branch");
    const ct = qs.get("case_type");
    if (b) { setBranchId(b); setCandidateText(""); setTab("cohort"); }
    if (ct) setCaseTypeId(ct);
  }, []);

  function parseCandidate(): any | null {
    if (!candidateText.trim()) return {};
    try { return JSON.parse(candidateText); } catch { setError("Candidate is not valid JSON"); return null; }
  }

  async function runSingle() {
    setError(null); setSingle(null);
    const candidate = parseCandidate();
    if (candidate === null) return;
    setBusy(true);
    try {
      const r = await apiFetch("/runs", { method: "POST", body: JSON.stringify({
        kind: "single", case_id: caseId.trim(), candidate, estimate,
        branch_id: branchId.trim() || null }) });
      const body = await r.json();
      if (!r.ok) { setError(body.detail ?? "Replay failed"); return; }
      setSingle(body);
    } finally { setBusy(false); }
  }

  async function runCohort() {
    setError(null); setCohortRun(null);
    const candidate = parseCandidate();
    if (candidate === null) return;
    setBusy(true);
    try {
      const r = await apiFetch("/runs", { method: "POST", body: JSON.stringify({
        kind: "cohort", candidate, estimate, branch_id: branchId.trim() || null,
        cohort_filter: { case_type_id: caseTypeId.trim(), from: fromDate || undefined,
                         to: toDate || undefined, max_cases: Number(maxCases) || 500 } }) });
      const body = await r.json();
      if (!r.ok) { setError(body.detail ?? "Cohort replay failed"); return; }
      setCohortRun(body);
    } finally { setBusy(false); }
  }

  async function refreshCohort() {
    if (!cohortRun) return;
    const r = await apiFetch(`/runs/${cohortRun.id}`);
    if (r.ok) setCohortRun(await r.json());
  }

  const candidateEditor = (
    <div style={{ marginTop: 8 }}>
      <label style={S.label}>Candidate rule change (JSON) — or an HxBranch id below</label>
      <textarea style={{ ...S.input, width: "100%", minHeight: 140, fontFamily: "var(--font-mono, monospace)", fontSize: 12 }}
                value={candidateText} onChange={(e) => setCandidateText(e.target.value)} />
      <label style={S.label}>HxBranch id (optional, a branch of a rule)</label>
      <input style={S.input} value={branchId} onChange={(e) => setBranchId(e.target.value)} placeholder="branch uuid" />
      <label style={{ ...S.label, marginTop: 8, cursor: "pointer" }}>
        <input type="checkbox" checked={estimate} onChange={(e) => setEstimate(e.target.checked)} />{" "}
        Estimate excluded cases (policy substitution / historical simulation — labelled,
        kept separate from hard metrics)
      </label>
    </div>
  );

  return (
    <div style={S.page}>
      <div style={S.tabs}>
        {(["single", "cohort", "runs"] as const).map((t) => (
          <div key={t} style={{ ...S.tab, background: tab === t ? "var(--surface-2)" : "transparent" }}
               onClick={() => setTab(t)}>
            {t === "single" ? "Single Case" : t === "cohort" ? "Cohort" : "Runs"}
          </div>
        ))}
      </div>
      <div style={S.body}>
        {error && <div style={{ ...S.card, borderColor: "#ef4444", color: "#ef4444" }}>{error}</div>}

        {tab === "single" && (
          <>
            <div style={S.card}>
              <b>Fork a real case</b>
              <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                Replays the recorded case against the candidate config. Human and external
                events are held fixed from the record; unprovable inputs exclude the case
                rather than guessing.
              </div>
              <label style={{ ...S.label, marginTop: 8 }}>Case id</label>
              <input style={S.input} value={caseId} onChange={(e) => setCaseId(e.target.value)} placeholder="case uuid" />
              {candidateEditor}
              <div style={{ marginTop: 10 }}>
                <Button onClick={runSingle} disabled={busy || !caseId.trim()}>{busy ? "Replaying…" : "Replay"}</Button>
              </div>
            </div>

            {single?.result && (
              <div style={S.card}>
                <div style={S.row}>
                  <b>Result</b>
                  <span style={{ ...S.pill,
                                 background: single.result.determinacy === "determinate" ? "#22c55e22"
                                   : single.result.determinacy === "estimated" ? "#a855f722" : "#f59e0b22",
                                 color: single.result.determinacy === "determinate" ? "#22c55e"
                                   : single.result.determinacy === "estimated" ? "#a855f7" : "#f59e0b" }}>
                    {single.result.determinacy}
                  </span>
                  {single.result.divergence_point
                    ? <span>diverges at <b>{single.result.divergence_point}</b></span>
                    : <span>no divergence on this case</span>}
                  {single.anchored && <span style={{ ...S.pill, background: "#3b82f622", color: "#3b82f6" }}>anchored</span>}
                </div>
                {single.result.exclusion_reason && (
                  <div style={S.caveat}>{single.result.exclusion_reason}</div>
                )}
                <div style={{ ...S.row, marginTop: 10, alignItems: "stretch" }}>
                  <MetricsBlock title="Reality (recorded)" m={single.result.baseline_metrics} />
                  <MetricsBlock title="Counterfactual" m={single.result.counterfactual_metrics} />
                </div>
                <CostCard cost={single.summary?.cost} />
                {single.result.trace?.nodes && <TraceView nodes={single.result.trace.nodes} />}
                {single.result.trace?.note && <div style={S.caveat}>{single.result.trace.note}</div>}
                <CoverageView coverage={single.result.trace?.input_coverage} />
              </div>
            )}
          </>
        )}

        {tab === "cohort" && (
          <>
            <div style={S.card}>
              <b>Replay a cohort</b>
              <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                Replays historical cases of one case-type against the candidate config and
                aggregates the deltas. Runs in the background.
              </div>
              <div style={{ ...S.row, marginTop: 8 }}>
                <div><label style={S.label}>Case-type id</label>
                  <input style={S.input} value={caseTypeId} onChange={(e) => setCaseTypeId(e.target.value)} placeholder="case-type uuid" /></div>
                <div><label style={S.label}>From</label>
                  <input style={{ ...S.input, minWidth: 150 }} type="date" value={fromDate} onChange={(e) => setFromDate(e.target.value)} /></div>
                <div><label style={S.label}>To</label>
                  <input style={{ ...S.input, minWidth: 150 }} type="date" value={toDate} onChange={(e) => setToDate(e.target.value)} /></div>
                <div><label style={S.label}>Max cases</label>
                  <input style={{ ...S.input, minWidth: 90 }} value={maxCases} onChange={(e) => setMaxCases(e.target.value)} /></div>
              </div>
              {candidateEditor}
              <div style={{ marginTop: 10 }}>
                <Button onClick={runCohort} disabled={busy || !caseTypeId.trim()}>{busy ? "Starting…" : "Run cohort replay"}</Button>
              </div>
            </div>

            {cohortRun && (
              <div style={S.card}>
                <div style={S.row}>
                  <b>Run {cohortRun.id.slice(0, 8)}</b>
                  <span style={S.pill}>{cohortRun.status}</span>
                  {cohortRun.anchored && <span style={{ ...S.pill, background: "#3b82f622", color: "#3b82f6" }}>anchored</span>}
                  <Button onClick={refreshCohort}>Refresh</Button>
                </div>
                {cohortRun.error && <div style={S.caveat}>{cohortRun.error}</div>}
                {cohortRun.summary && (
                  <>
                    <div style={{ ...S.row, marginTop: 8 }}>
                      <div style={S.card}><div style={S.big}>{cohortRun.summary.cases}</div><div>cases</div></div>
                      <div style={S.card}>
                        <div style={S.big}>{((cohortRun.summary.coverage_ratio ?? 0) * 100).toFixed(1)}%</div>
                        <div>determinate coverage ({cohortRun.summary.indeterminate} excluded)</div>
                      </div>
                      <div style={S.card}>
                        <div style={S.big}>{((cohortRun.summary.divergence_rate ?? 0) * 100).toFixed(1)}%</div>
                        <div>of determinate cases diverge</div>
                      </div>
                      <div style={S.card}>
                        <div style={S.big}>{fmtSecs(cohortRun.summary.counterfactual?.elided_wall_seconds_total)}</div>
                        <div>manual work elided</div>
                      </div>
                    </div>
                    <div style={{ ...S.row, alignItems: "stretch" }}>
                      <div style={{ ...S.card, ...S.half }}>
                        <b>Baseline cycle time</b>
                        <div>mean {fmtSecs(cohortRun.summary.baseline?.cycle_time?.mean)} · p50 {fmtSecs(cohortRun.summary.baseline?.cycle_time?.p50)} · p90 {fmtSecs(cohortRun.summary.baseline?.cycle_time?.p90)}</div>
                      </div>
                      <div style={{ ...S.card, ...S.half }}>
                        <b>Counterfactual cycle time</b>
                        <div>mean {fmtSecs(cohortRun.summary.counterfactual?.cycle_time?.mean)} · p50 {fmtSecs(cohortRun.summary.counterfactual?.cycle_time?.p50)} · p90 {fmtSecs(cohortRun.summary.counterfactual?.cycle_time?.p90)}</div>
                      </div>
                    </div>
                    <CostCard cost={cohortRun.summary.cost} />
                    {cohortRun.summary.estimated_block && (
                      <div style={{ ...S.card, borderColor: "#a855f7" }}>
                        <b style={{ color: "#a855f7" }}>Estimated cases ({cohortRun.summary.estimated_block.cases})</b>
                        <div>cycle mean {fmtSecs(cohortRun.summary.estimated_block.cycle_time?.mean)} · p50 {fmtSecs(cohortRun.summary.estimated_block.cycle_time?.p50)} · p90 {fmtSecs(cohortRun.summary.estimated_block.cycle_time?.p90)}</div>
                        <div style={{ fontSize: 12 }}>
                          sources: {Object.entries(cohortRun.summary.estimated_block.sources ?? {}).map(([k, v]) => `${k}×${String(v)}`).join(" · ")}
                          {Object.keys(cohortRun.summary.estimated_block.outcome_distribution ?? {}).length > 0 &&
                            <> · outcomes: {Object.entries(cohortRun.summary.estimated_block.outcome_distribution).map(([k, v]) => `${k} ${(Number(v) * 100).toFixed(0)}%`).join(", ")}</>}
                        </div>
                        <div style={S.caveat}>{cohortRun.summary.estimated_block.label}</div>
                      </div>
                    )}
                    {Object.keys(cohortRun.summary.exclusion_profile?.reasons ?? {}).length > 0 && (
                      <div style={S.mono}>
                        {Object.entries(cohortRun.summary.exclusion_profile.reasons).map(([k, v]) => (
                          <div key={k}>{String(v)} × {k}</div>
                        ))}
                      </div>
                    )}
                    <div style={S.caveat}>{cohortRun.summary.bias_caveat}</div>
                    <div style={S.caveat}>Assumption: {cohortRun.summary.assumption}</div>
                  </>
                )}
              </div>
            )}
          </>
        )}

        {tab === "runs" && (
          <>
          <RateCardEditor />
          <div style={S.card}>
            <div style={S.row}><b>Replay runs</b><Button onClick={loadRuns}>Refresh</Button></div>
            <table style={{ width: "100%", fontSize: 13, marginTop: 8 }}>
              <thead><tr style={{ textAlign: "left", color: "var(--text-secondary)" }}>
                <th>id</th><th>kind</th><th>status</th><th>anchored</th><th>created</th></tr></thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id}>
                    <td style={{ fontFamily: "var(--font-mono, monospace)" }}>{r.id.slice(0, 8)}</td>
                    <td>{r.kind}</td><td>{r.status}</td>
                    <td>{r.anchored ? "yes" : "no"}</td>
                    <td>{r.created_at?.slice(0, 19) ?? "—"}</td>
                  </tr>
                ))}
                {runs.length === 0 && <tr><td colSpan={5} style={{ color: "var(--text-secondary)" }}>No runs yet.</td></tr>}
              </tbody>
            </table>
          </div>
          </>
        )}
      </div>
    </div>
  );
}
