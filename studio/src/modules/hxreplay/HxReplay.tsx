/**
 * HxReplay — counterfactual case replay ("what-if on real history").
 * Single Case: fork one real case against a candidate rule change, side-by-side.
 * Cohort: replay N historical cases, aggregate deltas + determinacy coverage.
 * Runs: history. Honesty first: coverage, exclusions, and the bias caveat ship
 * next to every number.
 *
 * Built on the shared Studio components (2026-10-20): it had hand-rolled tabs,
 * raw inputs and a dozen hex colours that ignored the theme, so it looked like
 * a different product from every module around it.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  Banner, Button, Card, EmptyState, ErrorNote, FIELD_STYLE, MONO, Pill, SectionTitle,
  Select, Spinner, Stat, SubPanel, TABLE, Tabs, TextInput, TimeAgo,
} from "@shared/components";
import type { PillTone } from "@shared/components";
import { getAccessToken } from "@/auth/tokenManager";
import { listCaseTypes } from "@shared/api/client";
import type { CaseTypeSummary } from "@shared/types";

const API = "/api/v1/hxreplay";

function authHdr(): Record<string, string> {
  const t = getAccessToken();
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

type TabKey = "single" | "cohort" | "runs";
const TABS = [
  { key: "single" as const, label: "Single case" },
  { key: "cohort" as const, label: "Cohort" },
  { key: "runs"   as const, label: "Runs" },
];

const LABEL: React.CSSProperties = {
  fontSize: 12, color: "var(--text-secondary)", marginBottom: 4, display: "block",
};
const HINT: React.CSSProperties = { fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.5 };
const ROW: React.CSSProperties = {
  display: "flex", gap: "var(--space-md)", flexWrap: "wrap", alignItems: "center",
};
const GRID: React.CSSProperties = {
  display: "grid", gap: "var(--space-md)",
  gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
};
const SCROLL: React.CSSProperties = {
  ...MONO, whiteSpace: "pre-wrap", background: "var(--bg-input)", padding: "var(--space-md)",
  borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)",
  maxHeight: 300, overflow: "auto", margin: 0,
};
const GAP: React.CSSProperties = { marginBottom: "var(--space-md)" };

const DETERMINACY_TONE: Record<string, PillTone> = {
  determinate: "ok", estimated: "info", indeterminate: "warn",
};
const STATUS_TONE: Record<string, PillTone> = {
  completed: "ok", done: "ok", running: "warn", queued: "muted", pending: "muted", failed: "bad",
};
const CLASS_COLOR: Record<string, string> = {
  copied: "var(--text-secondary)", elided: "var(--text-muted)",
  synthetic: "var(--status-completed)", recomputed: "var(--accent-text)",
};

function fmtSecs(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}
function fmtPct(v: number | null | undefined): string {
  return `${((v ?? 0) * 100).toFixed(1)}%`;
}
function fmtMoney(v: number | null | undefined, currency: string): string {
  if (v == null) return "—";
  return `${currency} ${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function Field({ label, children, grow }: { label: string; children: React.ReactNode; grow?: boolean }) {
  return (
    <div style={{ flex: grow ? 1 : undefined, minWidth: grow ? 240 : undefined }}>
      <label style={LABEL}>{label}</label>
      {children}
    </div>
  );
}

function Caveat({ children }: { children: React.ReactNode }) {
  if (!children) return null;
  return <div style={{ marginTop: "var(--space-sm)" }}><Banner tone="warn">{children}</Banner></div>;
}

function CostCard({ cost }: { cost: any }) {
  if (!cost) return null;
  return (
    <SubPanel tone="ok" style={{ marginTop: "var(--space-md)" }}>
      <Stat label="Cost delta · determinate cases only" value={`${fmtMoney(cost.savings, cost.currency)} saved`} />
      <div style={{ ...HINT, marginTop: 6 }}>
        baseline {fmtMoney(cost.baseline_cost, cost.currency)} · counterfactual{" "}
        {fmtMoney(cost.counterfactual_cost, cost.currency)} · rate{" "}
        {fmtMoney(cost.hourly_rate, cost.currency)}/h · {cost.cases} case(s)
      </div>
      <div style={{ ...HINT, fontSize: 11, marginTop: 4 }}>{cost.basis}</div>
    </SubPanel>
  );
}

function RateCardEditor() {
  const [rate, setRate] = useState<string>("");
  const [currency, setCurrency] = useState("USD");
  const [configured, setConfigured] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
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
    setMsg(null); setErr(null);
    const r = await fetch("/api/v1/costing/rate-card", {
      method: "PUT", headers: authHdr(),
      body: JSON.stringify({ hourly_rate: Number(rate) || 0, currency }) });
    if (r.ok) { setConfigured(true); setMsg("Saved."); }
    else setErr((await r.json()).detail ?? "Save failed");
  }
  return (
    <Card style={GAP}>
      <SectionTitle right={<Pill tone={configured ? "ok" : "muted"}>{configured ? "configured" : "not configured"}</Pill>}>
        Rate card
      </SectionTitle>
      <div style={HINT}>
        Tenant default hourly rate for manual work — powers the cost delta on replay
        results. {configured ? "" : "Until it is set, replays show no cost block."}
      </div>
      <div style={{ ...ROW, marginTop: "var(--space-md)", alignItems: "flex-end" }}>
        <Field label="Hourly rate">
          <TextInput style={{ width: 130 }} value={rate} placeholder="e.g. 45"
                     inputMode="decimal" onChange={(e) => setRate(e.target.value)} />
        </Field>
        <Field label="Currency">
          <TextInput style={{ width: 90 }} value={currency} maxLength={8}
                     onChange={(e) => setCurrency(e.target.value)} />
        </Field>
        <Button onClick={save} disabled={!rate.trim()}>Save</Button>
        {msg && <Pill tone="ok">{msg}</Pill>}
      </div>
      {err && <ErrorNote style={{ marginTop: "var(--space-sm)" }}>{err}</ErrorNote>}
    </Card>
  );
}

function MetricsBlock({ title, m }: { title: string; m: Metrics | null }) {
  if (!m) {
    return (
      <SubPanel>
        <Stat label={title} value="excluded" />
      </SubPanel>
    );
  }
  return (
    <SubPanel>
      <Stat label={`${title} · cycle time`} value={fmtSecs(m.cycle_time_seconds)} />
      <div style={{ ...HINT, marginTop: 6 }}>
        {m.event_count} events · {m.manual_count} manual · {m.auto_count} auto
        {m.auto_ratio != null && <> · auto-ratio {(m.auto_ratio * 100).toFixed(0)}%</>}
        {!!m.elided_wall_seconds && <> · <b>{fmtSecs(m.elided_wall_seconds)} of work elided</b></>}
      </div>
    </SubPanel>
  );
}

function TraceView({ nodes }: { nodes: TraceNode[] }) {
  return (
    <div style={{ marginTop: "var(--space-md)" }}>
      <label style={LABEL}>Replayed trace</label>
      <div style={SCROLL}>
        {nodes.map((n, i) => (
          <div key={i} style={{ color: CLASS_COLOR[n._class ?? "copied"],
                                textDecoration: n._elided ? "line-through" : undefined }}>
            [{n._class ?? "copied"}] {n.timestamp?.slice(0, 19) ?? "—"}  {n.activity}
            {n.stage_id ? ` (stage ${n.stage_id})` : ""} {n.actor_type ? `· ${n.actor_type}` : ""}
            {n.outcome ? ` > ${n.outcome}` : ""}
          </div>
        ))}
      </div>
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
    <div style={{ marginTop: "var(--space-md)" }}>
      <label style={LABEL}>Decision-time input coverage</label>
      <table style={TABLE.table}>
        <thead><tr><th style={TABLE.th}>Input</th><th style={TABLE.th}>Coverage</th></tr></thead>
        <tbody>
          {entries.map(([k, v]) => (
            <tr key={k} style={TABLE.tr}>
              <td style={{ ...TABLE.td, ...MONO }}>{k}</td>
              <td style={TABLE.td}>
                <Pill tone={v === "unknown" ? "warn" : v === "lineage" ? "ok" : "muted"}>
                  {COVERAGE_LABEL[v] ?? v}
                </Pill>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const CANDIDATE_PLACEHOLDER = JSON.stringify(
  { rules: [{ id: "auto-approve", rule_type: "when", enabled: true,
      definition_json: { conditions: [{ field_path: "claim.amount", operator: "lt", value: 500 }],
                         actions: [{ action_type: "auto_approve" }] } }] }, null, 2);

export default function HxReplay() {
  const [tab, setTab] = useState<TabKey>("single");
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
  const [caseTypes, setCaseTypes] = useState<CaseTypeSummary[] | null>(null);
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [maxCases, setMaxCases] = useState("500");
  const [cohortRun, setCohortRun] = useState<Run | null>(null);

  // runs
  const [runs, setRuns] = useState<Run[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);

  const loadRuns = useCallback(async () => {
    setRunsLoading(true);
    try {
      const r = await apiFetch("/runs");
      if (r.ok) setRuns((await r.json()).runs);
    } finally { setRunsLoading(false); }
  }, []);
  useEffect(() => { if (tab === "runs") void loadRuns(); }, [tab, loadRuns]);

  // The cohort picker. A list that cannot be read falls back to typing the id,
  // as before — the picker is a convenience, not a gate.
  useEffect(() => {
    if (tab !== "cohort" || caseTypes !== null) return;
    listCaseTypes().then((r) => setCaseTypes(r.items ?? []), () => setCaseTypes([]));
  }, [tab, caseTypes]);

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
    <div style={{ marginTop: "var(--space-md)", display: "grid", gap: "var(--space-md)" }}>
      <Field label="Candidate rule change (JSON) — or give an HxBranch id below">
        <textarea style={{ ...FIELD_STYLE, ...MONO, width: "100%", minHeight: 160,
                           boxSizing: "border-box", resize: "vertical" }}
                  value={candidateText} onChange={(e) => setCandidateText(e.target.value)} />
      </Field>
      <Field label="HxBranch id (optional — a branch of a rule)">
        <TextInput style={{ width: "100%", maxWidth: 420, boxSizing: "border-box" }} value={branchId}
                   onChange={(e) => setBranchId(e.target.value)} placeholder="branch uuid" />
      </Field>
      <label style={{ ...HINT, display: "flex", gap: 8, alignItems: "flex-start", cursor: "pointer" }}>
        <input type="checkbox" checked={estimate} onChange={(e) => setEstimate(e.target.checked)}
               style={{ marginTop: 2 }} />
        <span>
          Estimate excluded cases (policy substitution / historical simulation — labelled,
          kept separate from hard metrics)
        </span>
      </label>
    </div>
  );

  const selectedCaseTypeKnown = !!caseTypes?.some((c) => c.id === caseTypeId);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* No PageHeader here: `AppLayout` already renders one from `nav-data`. */}
      <Tabs tabs={TABS} value={tab} onChange={(k) => setTab(k as TabKey)} />

      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg) var(--space-2xl)",
                    boxSizing: "border-box" }}>
        {error && <ErrorNote style={GAP}>{error}</ErrorNote>}

        {tab === "single" && (
          <>
            <Card style={GAP}>
              <SectionTitle>Fork a real case</SectionTitle>
              <div style={HINT}>
                Replays the recorded case against the candidate config. Human and external
                events are held fixed from the record; unprovable inputs exclude the case
                rather than guessing.
              </div>
              <div style={{ marginTop: "var(--space-md)" }}>
                <Field label="Case id">
                  <TextInput style={{ width: "100%", maxWidth: 420, boxSizing: "border-box" }}
                             value={caseId} onChange={(e) => setCaseId(e.target.value)}
                             placeholder="case uuid" />
                </Field>
              </div>
              {candidateEditor}
              <div style={{ marginTop: "var(--space-lg)" }}>
                <Button onClick={runSingle} disabled={busy || !caseId.trim()}>
                  {busy ? "Replaying…" : "Replay"}
                </Button>
              </div>
            </Card>

            {single?.result && (
              <Card style={GAP}>
                <SectionTitle right={
                  <div style={ROW}>
                    <Pill tone={DETERMINACY_TONE[single.result.determinacy] ?? "muted"}>
                      {single.result.determinacy}
                    </Pill>
                    {single.anchored && <Pill tone="info" title="Recorded in the audit chain">anchored</Pill>}
                  </div>
                }>
                  Result
                </SectionTitle>
                <div style={HINT}>
                  {single.result.divergence_point
                    ? <>Diverges at <b style={{ color: "var(--text-primary)" }}>{single.result.divergence_point}</b></>
                    : "No divergence on this case."}
                </div>
                <Caveat>{single.result.exclusion_reason}</Caveat>
                <div style={{ ...GRID, marginTop: "var(--space-md)" }}>
                  <MetricsBlock title="Reality (recorded)" m={single.result.baseline_metrics} />
                  <MetricsBlock title="Counterfactual" m={single.result.counterfactual_metrics} />
                </div>
                <CostCard cost={single.summary?.cost} />
                {single.result.trace?.nodes && <TraceView nodes={single.result.trace.nodes} />}
                <Caveat>{single.result.trace?.note}</Caveat>
                <CoverageView coverage={single.result.trace?.input_coverage} />
              </Card>
            )}
          </>
        )}

        {tab === "cohort" && (
          <>
            <Card style={GAP}>
              <SectionTitle>Replay a cohort</SectionTitle>
              <div style={HINT}>
                Replays historical cases of one case type against the candidate config and
                aggregates the deltas. Runs in the background.
              </div>
              <div style={{ ...ROW, marginTop: "var(--space-md)", alignItems: "flex-end" }}>
                <Field label="Case type" grow>
                  {caseTypes === null ? <Spinner size={16} /> : caseTypes.length > 0 ? (
                    <Select style={{ width: "100%" }} value={caseTypeId}
                            onChange={(e) => setCaseTypeId(e.target.value)}>
                      <option value="">Choose a case type…</option>
                      {caseTypeId && !selectedCaseTypeKnown && <option value={caseTypeId}>{caseTypeId}</option>}
                      {caseTypes.map((c) => (
                        <option key={c.id} value={c.id}>{c.name}{c.version ? ` · v${c.version}` : ""}</option>
                      ))}
                    </Select>
                  ) : (
                    <TextInput style={{ width: "100%", boxSizing: "border-box" }} value={caseTypeId}
                               onChange={(e) => setCaseTypeId(e.target.value)} placeholder="case-type uuid" />
                  )}
                </Field>
                <Field label="From">
                  <TextInput type="date" value={fromDate} onChange={(e) => setFromDate(e.target.value)} />
                </Field>
                <Field label="To">
                  <TextInput type="date" value={toDate} onChange={(e) => setToDate(e.target.value)} />
                </Field>
                <Field label="Max cases">
                  <TextInput style={{ width: 100 }} inputMode="numeric" value={maxCases}
                             onChange={(e) => setMaxCases(e.target.value)} />
                </Field>
              </div>
              {candidateEditor}
              <div style={{ marginTop: "var(--space-lg)" }}>
                <Button onClick={runCohort} disabled={busy || !caseTypeId.trim()}>
                  {busy ? "Starting…" : "Run cohort replay"}
                </Button>
              </div>
            </Card>

            {cohortRun && (
              <Card style={GAP}>
                <SectionTitle right={
                  <div style={ROW}>
                    <Pill tone={STATUS_TONE[cohortRun.status] ?? "muted"}>{cohortRun.status}</Pill>
                    {cohortRun.anchored && <Pill tone="info" title="Recorded in the audit chain">anchored</Pill>}
                    <Button variant="secondary" size="sm" onClick={refreshCohort}>Refresh</Button>
                  </div>
                }>
                  Run <span style={MONO}>{cohortRun.id.slice(0, 8)}</span>
                </SectionTitle>
                {cohortRun.error && <ErrorNote>{cohortRun.error}</ErrorNote>}
                {!cohortRun.summary && !cohortRun.error && (
                  <div style={{ ...HINT, display: "flex", gap: 8, alignItems: "center" }}>
                    <Spinner size={14} /> Replaying in the background — refresh to see the result.
                  </div>
                )}
                {cohortRun.summary && (
                  <>
                    <div style={GRID}>
                      <SubPanel><Stat label="Cases" value={cohortRun.summary.cases} /></SubPanel>
                      <SubPanel>
                        <Stat label="Determinate coverage" value={fmtPct(cohortRun.summary.coverage_ratio)} />
                        <div style={{ ...HINT, marginTop: 4 }}>{cohortRun.summary.indeterminate} excluded</div>
                      </SubPanel>
                      <SubPanel>
                        <Stat label="Diverge" value={fmtPct(cohortRun.summary.divergence_rate)} />
                        <div style={{ ...HINT, marginTop: 4 }}>of determinate cases</div>
                      </SubPanel>
                      <SubPanel>
                        <Stat label="Manual work elided"
                              value={fmtSecs(cohortRun.summary.counterfactual?.elided_wall_seconds_total)} />
                      </SubPanel>
                    </div>
                    <div style={{ ...GRID, marginTop: "var(--space-md)" }}>
                      {(["baseline", "counterfactual"] as const).map((k) => (
                        <SubPanel key={k}>
                          <Stat label={`${k === "baseline" ? "Baseline" : "Counterfactual"} cycle time · mean`}
                                value={fmtSecs(cohortRun.summary[k]?.cycle_time?.mean)} />
                          <div style={{ ...HINT, marginTop: 4 }}>
                            p50 {fmtSecs(cohortRun.summary[k]?.cycle_time?.p50)} · p90{" "}
                            {fmtSecs(cohortRun.summary[k]?.cycle_time?.p90)}
                          </div>
                        </SubPanel>
                      ))}
                    </div>
                    <CostCard cost={cohortRun.summary.cost} />
                    {cohortRun.summary.estimated_block && (
                      <SubPanel tone="info" style={{ marginTop: "var(--space-md)" }}>
                        <Stat label={`Estimated cases (${cohortRun.summary.estimated_block.cases}) · cycle mean`}
                              value={fmtSecs(cohortRun.summary.estimated_block.cycle_time?.mean)} />
                        <div style={{ ...HINT, marginTop: 4 }}>
                          p50 {fmtSecs(cohortRun.summary.estimated_block.cycle_time?.p50)} · p90{" "}
                          {fmtSecs(cohortRun.summary.estimated_block.cycle_time?.p90)} · sources:{" "}
                          {Object.entries(cohortRun.summary.estimated_block.sources ?? {}).map(([k, v]) => `${k}×${String(v)}`).join(" · ")}
                          {Object.keys(cohortRun.summary.estimated_block.outcome_distribution ?? {}).length > 0 &&
                            <> · outcomes: {Object.entries(cohortRun.summary.estimated_block.outcome_distribution).map(([k, v]) => `${k} ${(Number(v) * 100).toFixed(0)}%`).join(", ")}</>}
                        </div>
                        <Caveat>{cohortRun.summary.estimated_block.label}</Caveat>
                      </SubPanel>
                    )}
                    {Object.keys(cohortRun.summary.exclusion_profile?.reasons ?? {}).length > 0 && (
                      <div style={{ marginTop: "var(--space-md)" }}>
                        <label style={LABEL}>Why cases were excluded</label>
                        <table style={TABLE.table}>
                          <thead><tr><th style={TABLE.th}>Reason</th><th style={TABLE.th}>Cases</th></tr></thead>
                          <tbody>
                            {Object.entries(cohortRun.summary.exclusion_profile.reasons).map(([k, v]) => (
                              <tr key={k} style={TABLE.tr}>
                                <td style={TABLE.td}>{k}</td><td style={{ ...TABLE.td, ...MONO }}>{String(v)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    <Caveat>{cohortRun.summary.bias_caveat}</Caveat>
                    {cohortRun.summary.assumption && <Caveat>Assumption: {cohortRun.summary.assumption}</Caveat>}
                  </>
                )}
              </Card>
            )}
          </>
        )}

        {tab === "runs" && (
          <>
            <RateCardEditor />
            <Card>
              <SectionTitle right={
                <Button variant="secondary" size="sm" onClick={loadRuns} disabled={runsLoading}>Refresh</Button>
              }>
                Replay runs
              </SectionTitle>
              {runsLoading && runs.length === 0 ? <Spinner /> : runs.length === 0 ? (
                <EmptyState title="No runs yet"
                  description="Replay a single case or a cohort and it will be listed here." />
              ) : (
                <table style={TABLE.table}>
                  <thead>
                    <tr>
                      <th style={TABLE.th}>Run</th><th style={TABLE.th}>Kind</th>
                      <th style={TABLE.th}>Status</th><th style={TABLE.th}>Anchored</th>
                      <th style={TABLE.th}>Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((r) => (
                      <tr key={r.id} style={TABLE.tr}>
                        <td style={{ ...TABLE.td, ...MONO }}>{r.id.slice(0, 8)}</td>
                        <td style={TABLE.td}>{r.kind === "single" ? "Single case" : "Cohort"}</td>
                        <td style={TABLE.td}><Pill tone={STATUS_TONE[r.status] ?? "muted"}>{r.status}</Pill></td>
                        <td style={TABLE.td}>{r.anchored ? <Pill tone="info">anchored</Pill> : "—"}</td>
                        <td style={TABLE.td}>{r.created_at ? <TimeAgo date={r.created_at} /> : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Card>
          </>
        )}
      </div>
    </div>
  );
}
