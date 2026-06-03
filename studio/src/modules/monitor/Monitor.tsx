import React, { useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useApi, useInterval } from "@shared/hooks";
import {
  listCases,
  listProcesses,
  listInstances,
  getInstance,
  startInstance,
  cancelInstance,
  completeUserTask,
  createProcessSchedule,
} from "@shared/api/client";
import {
  Card,
  Button,
  StatusBadge,
  Spinner,
  EmptyState,
  TimeAgo,
  Stat,
} from "@shared/components";

/* ═══════════════════════════════════════════════════════════════════
   Monitor — Case instances (primary) + BPMN process instances (secondary)
   ═══════════════════════════════════════════════════════════════════ */

export default function Monitor() {
  const { processId, instanceId } = useParams();

  if (instanceId && processId) {
    return <InstanceDetail processId={processId} instanceId={instanceId} />;
  }
  if (processId) {
    return <ProcessInstances processId={processId} />;
  }
  return <MonitorHome />;
}

/* ── Home: Cases tab + Processes tab ──────────────────────────── */

function MonitorHome() {
  const [tab, setTab] = useState<"cases" | "processes">("cases");

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {/* Tabs */}
      <div style={{ display: "flex", gap: 4, marginBottom: "var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", paddingBottom: 0 }}>
        {(["cases", "processes"] as const).map(t => (
          <button key={t} onClick={() => setTab(t)} style={{
            padding: "8px 20px", border: "none", cursor: "pointer", fontSize: 13,
            background: "none", fontFamily: "var(--font-body)",
            color: tab === t ? "var(--accent)" : "var(--text-secondary)",
            fontWeight: tab === t ? 600 : 400,
            borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent",
            marginBottom: -1,
          }}>
            {t === "cases" ? "Cases" : "BPMN Processes"}
          </button>
        ))}
      </div>

      {tab === "cases" && <CasesView />}
      {tab === "processes" && <ProcessPicker />}
    </div>
  );
}

/* ── Cases view (from case service) ───────────────────────────── */

const MON_PAGE_SIZE = 25;

function CasesView() {
  const [page, setPage]               = useState(1);
  const [statusFilter, setStatusFilter]     = useState("");
  const [priorityFilter, setPriorityFilter] = useState("");
  const [titleSearch, setTitleSearch]       = useState("");

  const { data, loading, error, refetch } = useApi(
    () => listCases({ page, page_size: MON_PAGE_SIZE, status: statusFilter || undefined, priority: priorityFilter || undefined }),
    [page, statusFilter, priorityFilter]
  );
  useInterval(refetch, 10000);

  const allCases = (data as any)?.items ?? [];
  const total    = (data as any)?.total ?? 0;
  const totalPages = Math.ceil(total / MON_PAGE_SIZE);

  const resetPage = (fn: () => void) => { fn(); setPage(1); };

  // Unique values for dropdowns (from current page)
  const uniqueStatuses    = Array.from(new Set(allCases.map((c: any) => c.status))).sort() as string[];
  const uniquePriorities  = Array.from(new Set(allCases.map((c: any) => c.priority))).sort() as string[];
  const uniqueTypes       = Array.from(new Set(allCases.map((c: any) => c.case_type_name).filter(Boolean))).sort() as string[];
  const [typeFilter, setTypeFilter] = useState("");

  // Client-side title + type filter (server handles status/priority)
  const cases = allCases.filter((c: any) => {
    if (typeFilter  && c.case_type_name !== typeFilter) return false;
    if (titleSearch && !(c.title || c.id || "").toLowerCase().includes(titleSearch.toLowerCase())) return false;
    return true;
  });

  const hasFilter = statusFilter || priorityFilter || typeFilter || titleSearch;

  const STATUS_COLORS: Record<string, string> = {
    open: "#3b82f6", in_progress: "#f59e0b", pending: "#8b5cf6",
    resolved: "#22c55e", closed: "#6b7280", cancelled: "#ef4444",
  };

  if (loading && allCases.length === 0) {
    return <Center><Spinner size={28} /></Center>;
  }
  if (error) {
    return (
      <Card style={{ borderColor: "var(--status-failed)" }}>
        <p style={{ color: "var(--status-failed)", fontSize: 13 }}>Failed to load cases: {error}</p>
        <Button variant="secondary" size="sm" onClick={refetch} style={{ marginTop: "var(--space-sm)" }}>Retry</Button>
      </Card>
    );
  }

  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-lg)" }}>
        <Card><Stat label="Total" value={total} /></Card>
        <Card><Stat label="Open" value={allCases.filter((c: any) => c.status === "open" || c.status === "in_progress").length} /></Card>
        <Card><Stat label="Resolved" value={allCases.filter((c: any) => c.status === "resolved" || c.status === "closed").length} /></Card>
      </div>

      {/* Filter bar */}
      <div style={{ display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-md)", flexWrap: "wrap", alignItems: "center" }}>
        {/* Status */}
        <select value={statusFilter} onChange={e => resetPage(() => setStatusFilter(e.target.value))}
          style={selStyle}>
          <option value="">All statuses</option>
          {uniqueStatuses.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        {/* Priority */}
        <select value={priorityFilter} onChange={e => resetPage(() => setPriorityFilter(e.target.value))}
          style={selStyle}>
          <option value="">All priorities</option>
          {uniquePriorities.map(p => <option key={p} value={p}>{p}</option>)}
        </select>
        {/* Case Type */}
        {uniqueTypes.length > 0 && (
          <select value={typeFilter} onChange={e => setTypeFilter(e.target.value)} style={selStyle}>
            <option value="">All types</option>
            {uniqueTypes.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        )}
        {/* Title search */}
        <input value={titleSearch} onChange={e => setTitleSearch(e.target.value)}
          placeholder="Search title or ID…"
          style={{ padding: "6px 10px", fontSize: 12, fontFamily: "var(--font-mono)", background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", width: 200 }}
        />
        {hasFilter && (
          <button onClick={() => { setStatusFilter(""); setPriorityFilter(""); setTypeFilter(""); setTitleSearch(""); setPage(1); }}
            style={{ background: "none", border: "none", cursor: "pointer", fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
            ✕ Clear
          </button>
        )}
        <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
          {cases.length}{cases.length !== allCases.length ? ` / ${allCases.length}` : ""} shown
        </span>
      </div>

      {cases.length === 0 ? (
        <EmptyState title="No cases match" description={hasFilter ? "Adjust or clear the filters." : "No cases yet."} />
      ) : (
        <>
          {/* Table header + top pagination */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
            <div style={{ display: "grid", gridTemplateColumns: "2fr 140px 100px 100px 140px", flex: 1, padding: "var(--space-sm) var(--space-md)", fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
              <span>Case</span><span>Type</span><span>Status</span><span>Priority</span><span>Created</span>
            </div>
            {totalPages > 1 && <MonPager page={page} totalPages={totalPages} total={total} onChange={setPage} />}
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-xs)" }}>
            {cases.map((c: any) => (
              <div key={c.id} style={{
                display: "grid", gridTemplateColumns: "2fr 140px 100px 100px 140px",
                alignItems: "center", padding: "var(--space-md)",
                background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
                borderRadius: "var(--radius-md)", borderLeft: `3px solid ${STATUS_COLORS[c.status] || "#0d9488"}`,
              }}>
                <div>
                  <div style={{ fontWeight: 600, fontSize: 14, color: "var(--text-primary)" }}>{c.title || c.id?.slice(0, 8)}</div>
                  <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 2 }}>{c.id}</div>
                </div>
                <div style={{ fontSize: 12, color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}>{c.case_type_name || "—"}</div>
                <StatusBadge status={c.status} />
                <div style={{ fontSize: 11, padding: "2px 8px", borderRadius: 4, width: "fit-content", background: c.priority === "high" || c.priority === "critical" ? "#fef2f2" : "var(--bg-elevated)", color: c.priority === "high" || c.priority === "critical" ? "#ef4444" : "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "capitalize" }}>
                  {c.priority}
                </div>
                <div style={{ fontSize: 12, color: "var(--text-muted)" }}><TimeAgo date={c.created_at} /></div>
              </div>
            ))}
          </div>

          {/* Bottom pagination */}
          {totalPages > 1 && (
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "var(--space-lg)" }}>
              <MonPager page={page} totalPages={totalPages} total={total} onChange={setPage} />
            </div>
          )}
        </>
      )}
    </>
  );
}

/* ── BPMN Process picker ───────────────────────────────────────── */

function ProcessPicker() {
  const { data, loading, error } = useApi(listProcesses);
  const navigate = useNavigate();

  if (loading) return <Center><Spinner size={28} /></Center>;

  if (error) {
    return (
      <Card style={{ borderColor: "var(--status-failed)" }}>
        <p style={{ color: "var(--status-failed)", fontSize: 13 }}>
          BPMN engine unavailable — start the Velaris Engine (port 8100) to view process instances.
        </p>
      </Card>
    );
  }

  const processes = (data as any)?.processes ?? [];

  return processes.length === 0 ? (
    <EmptyState
      title="No BPMN processes deployed"
      description="Deploy a process from the BPMN Modeler to monitor its instances here."
    />
  ) : (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-sm)" }}>
      {processes.map((p: any) => (
        <Card key={p.process_id}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div style={{ flex: 1, cursor: "pointer" }} onClick={() => navigate(`/monitor/${p.process_id}`)}>
              <div style={{ fontWeight: 600, fontSize: 15 }}>{p.name || p.process_id}</div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
                {p.process_id} · v{p.version} · {p.element_count} elements · {p.flow_count} flows
              </div>
            </div>
            <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center" }}>
              <StatusBadge status={p.status} />
              <Button variant="secondary" size="sm" onClick={() => navigate(`/modeler?edit=${p.process_id}`)}>
                Edit
              </Button>
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}

/* ── Instance list for a specific process ─────────────────────── */

function ProcessInstances({ processId }: { processId: string }) {
  const { data, loading, refetch } = useApi(() => listInstances(processId), [processId]);
  const navigate = useNavigate();
  const [starting, setStarting] = useState(false);
  const [businessKey, setBusinessKey] = useState("");
  const [showSchedule, setShowSchedule] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");
  const [bkFilter, setBkFilter] = useState("");

  useInterval(refetch, 3000);

  const handleStart = async () => {
    setStarting(true);
    try {
      await startInstance(processId, {}, businessKey.trim() || undefined);
      setBusinessKey("");
      refetch();
    } catch {}
    setStarting(false);
  };

  const allInstances: any[] = (data as any)?.instances ?? [];

  // Unique status values from the data for the filter dropdown
  const uniqueStatuses = Array.from(new Set(allInstances.map((i: any) => i.status))).sort();

  // Apply client-side filters
  const instances = allInstances.filter((i: any) => {
    if (statusFilter && i.status !== statusFilter) return false;
    if (bkFilter && !(i.business_key || "").toLowerCase().includes(bkFilter.toLowerCase())) return false;
    return true;
  });

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "var(--space-xl)" }}>
        <div>
          <button onClick={() => navigate("/monitor")} style={{ background: "none", border: "none", color: "var(--text-muted)", fontSize: 12, cursor: "pointer", fontFamily: "var(--font-mono)", marginBottom: 8 }}>
            ← All processes
          </button>
          <h1 style={{ fontFamily: "var(--font-display)", fontSize: 24, fontWeight: 700 }}>{processId}</h1>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-sm)", alignItems: "flex-end" }}>
          <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center" }}>
            <input
              value={businessKey}
              onChange={e => setBusinessKey(e.target.value)}
              onKeyDown={e => e.key === "Enter" && handleStart()}
              placeholder="Business key (optional)"
              style={{ padding: "8px 12px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", width: 200, fontFamily: "var(--font-mono)" }}
            />
            <Button onClick={handleStart} disabled={starting}>{starting ? "Starting..." : "▶ Start Instance"}</Button>
          </div>
          <div style={{ display: "flex", gap: "var(--space-sm)" }}>
            <Button variant="secondary" size="sm" onClick={() => navigate(`/modeler?edit=${processId}`)}>Edit in Modeler</Button>
            <Button variant="secondary" size="sm" onClick={() => setShowSchedule(true)}>⏰ Schedule</Button>
          </div>
        </div>
      </div>

      {/* Business Key explainer */}
      <Card style={{ marginBottom: "var(--space-lg)", background: "var(--bg-elevated)" }}>
        <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.7 }}>
          <div style={{ display: "flex", gap: "var(--space-xl)", flexWrap: "wrap" }}>
            <div style={{ flex: 1, minWidth: 200 }}>
              <div style={{ fontWeight: 600, color: "var(--text-primary)", marginBottom: 2 }}>Instance ID (system)</div>
              A UUID auto-generated by the engine for every run. Use it for API calls, audit logs, and technical correlation. It never changes and uniquely identifies one execution.
            </div>
            <div style={{ flex: 1, minWidth: 200 }}>
              <div style={{ fontWeight: 600, color: "var(--text-primary)", marginBottom: 2 }}>Business Key (yours)</div>
              Optional. Set it to the ID from your own system — e.g. <code style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>LOAN-2026-001</code>, <code style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>ORDER-12345</code>, <code style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>CASE-UUID</code>. Makes it easy to find a specific run without knowing the UUID.
            </div>
          </div>
        </div>
      </Card>

      {/* Filter bar */}
      {allInstances.length > 0 && (
        <div style={{ display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-md)", alignItems: "center", flexWrap: "wrap" }}>
          {/* Status filter — unique values from data */}
          <select
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value)}
            style={{ padding: "6px 10px", fontSize: 12, fontFamily: "var(--font-mono)", background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", cursor: "pointer" }}
          >
            <option value="">All statuses</option>
            {uniqueStatuses.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          {/* Business key search */}
          <input
            value={bkFilter}
            onChange={e => setBkFilter(e.target.value)}
            placeholder="Search business key…"
            style={{ padding: "6px 10px", fontSize: 12, fontFamily: "var(--font-mono)", background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", width: 200 }}
          />
          {(statusFilter || bkFilter) && (
            <button onClick={() => { setStatusFilter(""); setBkFilter(""); }} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
              ✕ Clear
            </button>
          )}
          <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
            {instances.length} / {allInstances.length} shown
          </span>
        </div>
      )}

      {loading && allInstances.length === 0 ? (
        <Center><Spinner size={32} /></Center>
      ) : allInstances.length === 0 ? (
        <EmptyState title="No instances" description="Start an instance to begin execution." action={<Button onClick={handleStart}>▶ Start Instance</Button>} />
      ) : instances.length === 0 ? (
        <div style={{ padding: "var(--space-xl)", color: "var(--text-muted)", fontSize: 13 }}>
          No instances match the current filters. <button onClick={() => { setStatusFilter(""); setBkFilter(""); }} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--accent)", fontSize: 13 }}>Clear filters</button>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-sm)" }}>
          <div style={{ display: "grid", gridTemplateColumns: "2fr 120px 140px 140px 140px", padding: "var(--space-sm) var(--space-md)", fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
            <span>Instance ID</span><span>Status</span><span>Started</span><span>Completed</span><span>Business Key</span>
          </div>
          {instances.map((inst: any) => (
            <div key={inst.instance_id} onClick={() => navigate(`/monitor/${processId}/${inst.instance_id}`)}
              style={{ display: "grid", gridTemplateColumns: "2fr 120px 140px 140px 140px", alignItems: "center", padding: "var(--space-md)", background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", cursor: "pointer", transition: "background 0.12s ease" }}
              onMouseEnter={e => (e.currentTarget.style.background = "var(--bg-card-hover)")}
              onMouseLeave={e => (e.currentTarget.style.background = "var(--bg-card)")}
            >
              <div>
                {inst.business_key
                  ? <div style={{ fontWeight: 600, fontSize: 13, color: "var(--accent)" }}>{inst.business_key}</div>
                  : null}
                <div style={{ fontFamily: "var(--font-mono)", fontSize: inst.business_key ? 10 : 13, color: "var(--text-muted)" }}>{inst.instance_id.split("-")[0]}…</div>
              </div>
              <StatusBadge status={inst.status} />
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}><TimeAgo date={inst.started_at} /></div>
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{inst.completed_at ? <TimeAgo date={inst.completed_at} /> : "—"}</div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-muted)" }}>{inst.business_key ? "✓ keyed" : "—"}</div>
            </div>
          ))}
        </div>
      )}

      {showSchedule && (
        <ScheduleModal processId={processId} onClose={() => setShowSchedule(false)} />
      )}
    </div>
  );
}

/* ── Instance detail ──────────────────────────────────────────── */

function InstanceDetail({ processId, instanceId }: { processId: string; instanceId: string }) {
  const { data: inst, loading, refetch } = useApi(() => getInstance(processId, instanceId), [processId, instanceId]);
  const navigate = useNavigate();
  const [showCompleteTask, setShowCompleteTask] = useState(false);

  // Stop refreshing when the form modal is open — prevents input fields from resetting
  const isRunning = (inst as any)?.status === "running";
  useInterval(() => { if (isRunning) refetch(); }, isRunning && !showCompleteTask ? 5000 : null);

  if (loading || !inst) return <Center><Spinner size={32} /></Center>;

  const handleCancel = async () => {
    try { await cancelInstance(processId, instanceId); refetch(); } catch {}
  };

  const i = inst as any;
  const pendingTask = i.pending_user_task;
  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
      <button onClick={() => navigate(`/monitor/${processId}`)} style={{ background: "none", border: "none", color: "var(--text-muted)", fontSize: 12, cursor: "pointer", fontFamily: "var(--font-mono)", marginBottom: "var(--space-md)" }}>
        ← {processId} instances
      </button>

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "var(--space-xl)" }}>
        <div>
          <h1 style={{ fontFamily: "var(--font-display)", fontSize: 22, fontWeight: 700 }}>Instance Detail</h1>
          <div style={{ fontFamily: "var(--font-mono)", fontSize: 13, color: "var(--text-muted)", marginTop: 4 }}>{i.instance_id}</div>
          {i.business_key && (
            <div style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--accent)", marginTop: 2 }}>🔑 {i.business_key}</div>
          )}
        </div>
        <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center" }}>
          <StatusBadge status={i.status} />
          {i.status === "running" && <Button variant="danger" size="sm" onClick={handleCancel}>Cancel</Button>}
        </div>
      </div>

      {/* Pending User Task banner */}
      {pendingTask && (
        <Card style={{ marginBottom: "var(--space-lg)", borderColor: "var(--status-running)", background: "color-mix(in srgb, var(--status-running) 8%, var(--bg-panel))" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <div style={{ fontWeight: 600, fontSize: 14, color: "var(--text-primary)" }}>
                ⏳ Waiting for User Task: <span style={{ color: "var(--accent)" }}>{pendingTask.task_name || pendingTask.task_id}</span>
              </div>
              {pendingTask.form_key && (
                <div style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 4 }}>
                  Form Key: {pendingTask.form_key}
                </div>
              )}
              <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>
                Fill in the form below to resume process execution.
              </div>
            </div>
            <Button onClick={() => setShowCompleteTask(true)}>Fill Form &amp; Resume</Button>
          </div>
        </Card>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "var(--space-md)", marginBottom: "var(--space-xl)" }}>
        <InfoCard label="Process" value={`${processId} v${i.version}`} />
        <InfoCard label="Started" value={new Date(i.started_at).toLocaleString()} />
        <InfoCard label="Duration" value={i.completed_at ? `${((new Date(i.completed_at).getTime() - new Date(i.started_at).getTime()) / 1000).toFixed(1)}s` : "running…"} />
      </div>

      {i.error && (
        <Card style={{ marginBottom: "var(--space-lg)", borderColor: "color-mix(in srgb, var(--status-failed) 40%, transparent)" }}>
          <div style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>Error</div>
          <div style={{ color: "var(--status-failed)", fontFamily: "var(--font-mono)", fontSize: 13 }}>{i.error}</div>
        </Card>
      )}

      <Card style={{ marginBottom: "var(--space-lg)" }}>
        <div style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginBottom: "var(--space-md)", textTransform: "uppercase", letterSpacing: "0.06em" }}>Execution Trace</div>
        {i.visited_elements?.length === 0 ? (
          <div style={{ color: "var(--text-muted)", fontSize: 13 }}>No elements visited yet.</div>
        ) : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {i.visited_elements?.map((el: string, idx: number) => (
              <React.Fragment key={idx}>
                <span style={{ padding: "4px 10px", background: "var(--bg-elevated)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontFamily: "var(--font-mono)", fontSize: 12 }}>{el}</span>
                {idx < i.visited_elements.length - 1 && <span style={{ color: "var(--text-muted)", alignSelf: "center" }}>→</span>}
              </React.Fragment>
            ))}
          </div>
        )}
      </Card>

      <Card>
        <div style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginBottom: "var(--space-md)", textTransform: "uppercase", letterSpacing: "0.06em" }}>Process Variables</div>
        <pre style={{ background: "var(--bg-input)", padding: "var(--space-md)", borderRadius: "var(--radius-sm)", overflow: "auto", maxHeight: 400, fontSize: 13, lineHeight: 1.6, color: "var(--text-primary)" }}>
          {JSON.stringify(i.variables, null, 2)}
        </pre>
      </Card>

      {showCompleteTask && pendingTask && (
        <CompleteTaskModal
          processId={processId}
          instanceId={instanceId}
          task={pendingTask}
          onClose={() => setShowCompleteTask(false)}
          onCompleted={() => { setShowCompleteTask(false); refetch(); }}
        />
      )}
    </div>
  );
}

/* ── Modals ───────────────────────────────────────────────────── */

function CompleteTaskModal({ processId, instanceId, task, onClose, onCompleted }: {
  processId: string; instanceId: string;
  task: { task_id: string; task_name?: string; form_key?: string };
  onClose: () => void; onCompleted: () => void;
}) {
  const [varsJson, setVarsJson] = useState("{}");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const vars = JSON.parse(varsJson);
      await completeUserTask(processId, instanceId, task.task_id, vars);
      onCompleted();
    } catch (e: any) {
      setError(e.message || "Failed to complete task");
      setSubmitting(false);
    }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", width: 520, maxHeight: "80vh", display: "flex", flexDirection: "column" }}>
        <div style={{ padding: "var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <div style={{ fontFamily: "var(--font-display)", fontWeight: 600, fontSize: 15 }}>Complete User Task</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: 2 }}>
              {task.task_name || task.task_id}{task.form_key ? ` · ${task.form_key}` : ""}
            </div>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 18 }}>✕</button>
        </div>
        <div style={{ padding: "var(--space-lg)", flex: 1, overflow: "auto" }}>
          <div style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: "var(--space-md)", lineHeight: 1.5 }}>
            Enter the form submission data as JSON. These variables will be merged into the process and execution will resume.
          </div>
          <label style={{ display: "block", fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>
            Form Variables (JSON)
          </label>
          <textarea
            value={varsJson}
            onChange={e => setVarsJson(e.target.value)}
            rows={8}
            style={{ width: "100%", padding: "10px 12px", fontSize: 13, fontFamily: "var(--font-mono)", background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", resize: "vertical", boxSizing: "border-box" }}
            placeholder='{\n  "approved": true,\n  "amount": 50000\n}'
          />
          {error && (
            <div style={{ color: "var(--status-failed)", fontSize: 12, marginTop: "var(--space-sm)", fontFamily: "var(--font-mono)" }}>{error}</div>
          )}
        </div>
        <div style={{ padding: "var(--space-md) var(--space-lg)", borderTop: "1px solid var(--border-subtle)", display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end" }}>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button onClick={handleSubmit} disabled={submitting}>{submitting ? "Submitting..." : "Submit & Resume"}</Button>
        </div>
      </div>
    </div>
  );
}

function ScheduleModal({ processId, onClose }: { processId: string; onClose: () => void }) {
  const [cron, setCron] = useState("0 9 * * 1-5");
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const PRESETS = [
    { label: "Every weekday at 9am", cron: "0 9 * * 1-5" },
    { label: "Every hour", cron: "0 * * * *" },
    { label: "Daily at midnight", cron: "0 0 * * *" },
    { label: "Every Monday at 8am", cron: "0 8 * * 1" },
    { label: "Every 30 minutes", cron: "*/30 * * * *" },
  ];

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const result = await createProcessSchedule(processId, cron, {}, description || undefined);
      setSaved(result.schedule_id);
    } catch (e: any) {
      setError(e.message || "Failed to create schedule");
    }
    setSaving(false);
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", width: 480 }}>
        <div style={{ padding: "var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ fontFamily: "var(--font-display)", fontWeight: 600, fontSize: 15 }}>Schedule Process Runs</div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 18 }}>✕</button>
        </div>
        <div style={{ padding: "var(--space-lg)" }}>
          {saved ? (
            <div style={{ padding: "var(--space-xl)" }}>
              <div style={{ fontSize: 32, marginBottom: "var(--space-md)" }}>✓</div>
              <div style={{ fontWeight: 600, fontSize: 15, color: "var(--status-completed)" }}>Schedule Created</div>
              <div style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 8 }}>{saved}</div>
              <Button variant="secondary" onClick={onClose} style={{ marginTop: "var(--space-lg)" }}>Close</Button>
            </div>
          ) : (
            <>
              <div style={{ marginBottom: "var(--space-md)" }}>
                <label style={{ display: "block", fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>Quick Presets</label>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {PRESETS.map(p => (
                    <button key={p.cron} onClick={() => setCron(p.cron)}
                      style={{ padding: "4px 10px", fontSize: 11, borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)", background: cron === p.cron ? "var(--accent-dim)" : "transparent", color: "var(--text-secondary)", cursor: "pointer", fontFamily: "var(--font-mono)" }}>
                      {p.label}
                    </button>
                  ))}
                </div>
              </div>
              <div style={{ marginBottom: "var(--space-md)" }}>
                <label style={{ display: "block", fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>Cron Expression</label>
                <input value={cron} onChange={e => setCron(e.target.value)}
                  style={{ width: "100%", padding: "8px 12px", fontSize: 13, fontFamily: "var(--font-mono)", background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", boxSizing: "border-box" }} />
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4, fontFamily: "var(--font-mono)" }}>Format: min hour day-of-month month day-of-week</div>
              </div>
              <div style={{ marginBottom: "var(--space-lg)" }}>
                <label style={{ display: "block", fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>Description (optional)</label>
                <input value={description} onChange={e => setDescription(e.target.value)}
                  placeholder="e.g. Daily morning report"
                  style={{ width: "100%", padding: "8px 12px", fontSize: 13, background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", fontFamily: "var(--font-body)", boxSizing: "border-box" }} />
              </div>
              {error && (
                <div style={{ color: "var(--status-failed)", fontSize: 12, marginBottom: "var(--space-md)", fontFamily: "var(--font-mono)" }}>{error}</div>
              )}
              <div style={{ display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end" }}>
                <Button variant="secondary" onClick={onClose}>Cancel</Button>
                <Button onClick={handleSave} disabled={saving || !cron}>{saving ? "Creating..." : "Create Schedule"}</Button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── Helpers ──────────────────────────────────────────────────── */

function InfoCard({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</div>
      <div style={{ fontSize: 15, fontWeight: 500, marginTop: 4, fontFamily: "var(--font-mono)" }}>{value}</div>
    </Card>
  );
}

function Center({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: "100%", padding: "var(--space-2xl)" }}>
      {children}
    </div>
  );
}

const selStyle: React.CSSProperties = {
  padding: "6px 10px", fontSize: 12, fontFamily: "var(--font-mono)",
  background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", cursor: "pointer",
};

function MonPager({ page, totalPages, total, onChange }: { page: number; totalPages: number; total: number; onChange: (p: number) => void }) {
  const pages: (number | "…")[] = [];
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - page) <= 1) pages.push(i);
    else if (pages[pages.length - 1] !== "…") pages.push("…");
  }
  const btn = (active: boolean, disabled = false): React.CSSProperties => ({
    width: 26, height: 26, border: `1px solid ${active ? "var(--accent)" : "var(--border-default)"}`,
    borderRadius: 4, background: active ? "var(--accent)" : "transparent",
    color: active ? "#fff" : "var(--text-secondary)", fontSize: 11, cursor: disabled ? "default" : "pointer",
    fontFamily: "var(--font-mono)", opacity: disabled ? 0.35 : 1,
    display: "inline-flex", alignItems: "center", justifyContent: "center",
  });
  return (
    <div style={{ display: "flex", gap: 3, alignItems: "center" }}>
      <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginRight: 4 }}>
        {(page - 1) * MON_PAGE_SIZE + 1}–{Math.min(page * MON_PAGE_SIZE, total)} / {total}
      </span>
      <button onClick={() => onChange(page - 1)} disabled={page === 1} style={btn(false, page === 1)}>‹</button>
      {pages.map((p, i) => p === "…"
        ? <span key={`e${i}`} style={{ fontSize: 11, color: "var(--text-muted)" }}>…</span>
        : <button key={p} onClick={() => onChange(p as number)} style={btn(page === p)}>{p}</button>
      )}
      <button onClick={() => onChange(page + 1)} disabled={page >= totalPages} style={btn(false, page >= totalPages)}>›</button>
    </div>
  );
}
