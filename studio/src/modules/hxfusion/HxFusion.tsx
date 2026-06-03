/**
 * P47 — HxFusion: Adaptive Execution Engine
 * Tabs: Definitions · Instances · Bindings · AI Director · Stats
 */
import React, { useState, useEffect, useCallback } from "react";

const API = "/api/v1/fusion";

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem("helix_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function fusionFetch(url: string, options?: RequestInit): Promise<Response> {
  return fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(options?.headers || {}) },
  });
}

async function apiFetch(url: string): Promise<Response> {
  return fetch(url, { headers: authHeaders() });
}

type ProcessDefinition = {
  id: string; name: string; version: number; description: string | null;
  bpmn_xml: string; case_type_id: string | null; status: string; created_by: string | null;
  tenant_id: string | null; created_at: string; updated_at: string;
};
type ProcessInstance = {
  id: string; definition_id: string; case_id: string | null;
  status: string; current_node: string | null; context: Record<string, unknown>;
  error_node: string | null; error_message: string | null;
  started_at: string; ended_at: string | null; tenant_id: string | null;
};
type ProcessBinding = {
  id: string; case_id: string; instance_id: string;
  binding_type: string; direction: string; status: string;
  stage_id: string | null; step_id: string | null;
  created_at: string; resolved_at: string | null;
};
type TaskLog = {
  id: string; instance_id: string; node_id: string; node_name: string | null;
  node_type: string; status: string; input_context: Record<string, unknown>;
  result: Record<string, unknown> | null; error: string | null;
  started_at: string; ended_at: string | null;
};
type Stats = {
  total_definitions: number; total_tasks_executed: number;
  instances_by_status: Record<string, number>;
};
type DirectorResponse = {
  can_automate: boolean; confidence: number; suggestion: string;
  recommended_definition_id: string | null; reasoning: string;
};
type ValidationResult = {
  valid: boolean; error?: string; process_id?: string; process_name?: string;
  node_count?: number; flow_count?: number; node_types?: string[];
};
type CaseType = { id: string; name: string; definition_json?: any };
type Stage    = { id: string; name: string };

const STATUS_COLOR: Record<string, string> = {
  running: "#3b82f6", completed: "#22c55e", failed: "#ef4444",
  suspended: "#f59e0b", cancelled: "#94a3b8", active: "#22c55e",
  inactive: "#94a3b8", draft: "#0d9488",
};
const NODE_COLOR: Record<string, string> = {
  startEvent: "#22c55e", endEvent: "#ef4444", serviceTask: "#3b82f6",
  userTask: "#f59e0b", exclusiveGateway: "#9333ea", parallelGateway: "#9333ea",
  scriptTask: "#0d9488", spawnCase: "#f97316", subProcess: "#0891b2",
};

const MINIMAL_BPMN = `<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="Process_1" name="My Process">
    <startEvent id="start" name="Start" />
    <sequenceFlow id="f1" sourceRef="start" targetRef="task1" />
    <serviceTask id="task1" name="Service Task" />
    <sequenceFlow id="f2" sourceRef="task1" targetRef="end" />
    <endEvent id="end" name="End" />
  </process>
</definitions>`;

const S: Record<string, React.CSSProperties> = {
  page:     { display: "flex", flexDirection: "column", height: "100%", background: "var(--bg-main)", color: "var(--text-primary)", fontFamily: "system-ui, sans-serif" },
  header:   { padding: "18px 24px 0", flexShrink: 0 },
  title:    { fontSize: 20, fontWeight: 700, margin: 0 },
  sub:      { fontSize: 13, color: "var(--text-secondary)", marginTop: 4 },
  tabBar:   { display: "flex", gap: 4, padding: "14px 24px 0", borderBottom: "1px solid var(--border)", flexShrink: 0 },
  tab:      { padding: "8px 20px", border: "none", background: "none", fontSize: 13, cursor: "pointer", borderBottom: "2px solid transparent", fontWeight: 400, color: "var(--text-secondary)" },
  tabActive:{ borderBottom: "2px solid var(--accent)", color: "var(--accent)", fontWeight: 700 },
  body:     { flex: 1, overflow: "auto", padding: "20px 28px" },
  card:     { background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "16px 20px", marginBottom: 12 },
  btn:      { padding: "7px 16px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 700 },
  btnP:     { background: "var(--accent)", color: "#fff" },
  btnS:     { background: "var(--bg-surface)", border: "1px solid var(--border)", color: "var(--text-secondary)" },
  btnD:     { background: "#fee2e2", color: "#ef4444", border: "1px solid #fecaca" },
  btnG:     { background: "#d1fae5", color: "#059669", border: "1px solid #a7f3d0" },
  btnY:     { background: "#fef9c3", color: "#d97706", border: "1px solid #fde68a" },
  input:    { width: "100%", padding: "7px 11px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "var(--bg-main)", color: "var(--text-primary)" },
  inputRO:  { width: "100%", padding: "7px 11px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "var(--bg-main)", color: "var(--text-secondary)", opacity: 0.7 },
  select:   { padding: "7px 11px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, background: "var(--bg-main)", color: "var(--text-primary)", width: "100%" },
  label:    { fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 4, display: "block" },
  badge:    { fontSize: 10, padding: "2px 8px", borderRadius: 10, fontWeight: 700 },
  tbl:      { width: "100%", borderCollapse: "collapse" as const, fontSize: 12 },
  th:       { textAlign: "left" as const, padding: "7px 10px", color: "var(--text-secondary)", fontWeight: 600, borderBottom: "1px solid var(--border)" },
  td:       { padding: "7px 10px", borderBottom: "1px solid var(--border)", verticalAlign: "middle" as const },
  row:      { display: "flex", gap: 10, marginBottom: 10 },
  textarea: { width: "100%", padding: "8px 11px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 11, fontFamily: "monospace", boxSizing: "border-box" as const, background: "var(--bg-main)", color: "var(--text-primary)", resize: "vertical" as const },
  statBox:  { background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "18px 22px", flex: 1, minWidth: 140 },
  statNum:  { fontSize: 28, fontWeight: 800 },
  statLbl:  { fontSize: 12, color: "var(--text-secondary)", marginTop: 4 },
};

function Badge({ label, color }: { label: string; color: string }) {
  return <span style={{ ...S.badge, background: color + "22", color }}>{label}</span>;
}
function fmtDate(s: string | null) { return s ? new Date(s).toLocaleString() : "—"; }
function fmtDuration(start: string, end: string | null) {
  if (!end) return "running…";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 60000)}m`;
}

function truncate(s: string, n = 52) { return s.length > n ? s.slice(0, n) + "…" : s; }

// ── Shared hooks ──────────────────────────────────────────────────────────────

function useCaseTypes() {
  const [caseTypes, setCaseTypes] = useState<CaseType[]>([]);
  useEffect(() => {
    apiFetch("/api/v1/case-types?page_size=200").then(r => r.ok ? r.json() : null).then(d => {
      if (d) setCaseTypes(d.items ?? []);
    });
  }, []);
  return caseTypes;
}

function useCurrentUser() {
  const [user, setUser] = useState<{ username: string; display_name?: string } | null>(null);
  useEffect(() => {
    apiFetch("/api/v1/auth/real/me/profile").then(r => r.ok ? r.json() : null).then(d => {
      if (d) setUser(d);
      else apiFetch("/api/v1/auth/me").then(r => r.ok ? r.json() : null).then(d2 => { if (d2) setUser(d2); });
    });
  }, []);
  return user;
}

function stagesFromCaseType(ct: CaseType | undefined): Stage[] {
  if (!ct) return [];
  const defn = typeof ct.definition_json === "string"
    ? JSON.parse(ct.definition_json)
    : (ct.definition_json ?? {});
  return defn.stages ?? [];
}

// ── Case Type Select ──────────────────────────────────────────────────────────

function CaseTypeSelect({ value, onChange, caseTypes, placeholder = "— Any case type —" }: {
  value: string; onChange: (v: string) => void;
  caseTypes: CaseType[]; placeholder?: string;
}) {
  return (
    <select style={S.select} value={value} onChange={e => onChange(e.target.value)}>
      <option value="">{placeholder}</option>
      {caseTypes.map(ct => (
        <option key={ct.id} value={ct.id} title={ct.id}>{truncate(ct.name)}</option>
      ))}
    </select>
  );
}


// ── Definitions Tab ───────────────────────────────────────────────────────────

type DefForm = { name: string; description: string; bpmn_xml: string; case_type_id: string; created_by: string };

function DefinitionsTab() {
  const caseTypes  = useCaseTypes();
  const currentUser = useCurrentUser();

  const [defs, setDefs]           = useState<ProcessDefinition[]>([]);
  const [creating, setCreating]   = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm]           = useState<DefForm>({ name: "", description: "", bpmn_xml: MINIMAL_BPMN, case_type_id: "", created_by: "" });
  const [editForm, setEditForm]   = useState<Omit<DefForm, "bpmn_xml" | "created_by">>({ name: "", description: "", case_type_id: "" });
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [loading, setLoading]     = useState(false);
  const [expanded, setExpanded]   = useState<string | null>(null);
  const [err, setErr]             = useState<string | null>(null);
  const [viewingXml, setViewingXml] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await fusionFetch(`${API}/definitions`);
    if (r.ok) setDefs(await r.json());
  }, []);

  useEffect(() => { load(); }, [load]);

  // Auto-fill created_by from profile when opening create form
  useEffect(() => {
    if (creating && currentUser) {
      setForm(f => ({ ...f, created_by: f.created_by || currentUser.username || "" }));
    }
  }, [creating, currentUser]);

  const validate = async () => {
    const r = await fusionFetch(`${API}/definitions/validate`, {
      method: "POST", body: JSON.stringify({ bpmn_xml: form.bpmn_xml }),
    });
    setValidation(await r.json());
  };

  const save = async () => {
    if (!form.name.trim()) { setErr("Name is required"); return; }
    if (!form.created_by.trim()) { setErr("Created By is required"); return; }
    setLoading(true); setErr(null);
    const r = await fusionFetch(`${API}/definitions`, {
      method: "POST", body: JSON.stringify({
        name: form.name, description: form.description || null,
        bpmn_xml: form.bpmn_xml,
        case_type_id: form.case_type_id || null,
        created_by: form.created_by,
      }),
    });
    if (r.ok) {
      setCreating(false); setValidation(null);
      setForm({ name: "", description: "", bpmn_xml: MINIMAL_BPMN, case_type_id: "", created_by: "" });
      await load();
    } else {
      const d = await r.json().catch(() => ({}));
      setErr(d.detail ?? "Create failed");
    }
    setLoading(false);
  };

  const saveEdit = async () => {
    if (!editingId || !editForm.name.trim()) return;
    setLoading(true); setErr(null);
    const r = await fusionFetch(`${API}/definitions/${editingId}`, {
      method: "PATCH", body: JSON.stringify({
        name: editForm.name,
        description: editForm.description || null,
        case_type_id: editForm.case_type_id || null,
      }),
    });
    if (r.ok) { setEditingId(null); await load(); }
    else { const d = await r.json().catch(() => ({})); setErr(d.detail ?? "Update failed"); }
    setLoading(false);
  };

  const archive = async (id: string) => {
    await fusionFetch(`${API}/definitions/${id}`, {
      method: "PATCH", body: JSON.stringify({ status: "inactive" }),
    });
    await load();
  };

  const ctName = (id: string | null) => id
    ? (caseTypes.find(ct => ct.id === id)?.name ?? id.slice(0, 14))
    : null;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16 }}>
        <span style={{ fontWeight: 600 }}>{defs.length} process definition(s)</span>
        <button style={{ ...S.btn, ...S.btnP }} onClick={() => { setCreating(true); setEditingId(null); setErr(null); }}>
          + New Definition
        </button>
      </div>

      {err && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 10, padding: "7px 12px", background: "#fef2f2", border: "1px solid #fecaca", borderRadius: 6 }}>{err}</div>}

      {/* Create form */}
      {creating && (
        <div style={{ ...S.card, borderColor: "var(--accent)", marginBottom: 16 }}>
          <div style={{ fontWeight: 600, marginBottom: 12, color: "var(--accent)" }}>New Process Definition</div>
          <div style={S.row}>
            <div style={{ flex: 2 }}>
              <label style={S.label}>Name *</label>
              <input style={S.input} value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} placeholder="e.g. Payment Processing" />
            </div>
            <div style={{ flex: 1 }}>
              <label style={S.label}>Created By *</label>
              <input style={{ ...S.input, background: "var(--bg-main)" }} value={form.created_by}
                onChange={e => setForm(f => ({ ...f, created_by: e.target.value }))}
                placeholder={currentUser?.username ?? "auto-filled from profile"} />
              <div style={{ fontSize: 10, color: "var(--text-secondary)", marginTop: 2 }}>Auto-filled from your profile</div>
            </div>
          </div>
          <div style={S.row}>
            <div style={{ flex: 1 }}>
              <label style={S.label}>Case Type <span style={{ fontWeight: 400 }}>(optional — restricts to this type)</span></label>
              <CaseTypeSelect value={form.case_type_id} onChange={v => setForm(f => ({ ...f, case_type_id: v }))} caseTypes={caseTypes} />
            </div>
            <div style={{ flex: 1 }}>
              <label style={S.label}>Description</label>
              <input style={S.input} value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} />
            </div>
          </div>
          <div style={{ marginBottom: 10 }}>
            <label style={S.label}>BPMN 2.0 XML</label>
            <textarea style={{ ...S.textarea, minHeight: 220 }} value={form.bpmn_xml}
              onChange={e => { setForm(f => ({ ...f, bpmn_xml: e.target.value })); setValidation(null); }} />
          </div>

          {validation && (
            <div style={{ marginBottom: 10, padding: "8px 12px", borderRadius: 6, background: validation.valid ? "#d1fae5" : "#fee2e2", fontSize: 12 }}>
              {validation.valid
                ? <span style={{ color: "#059669" }}>✓ Valid — {validation.node_count} nodes, {validation.flow_count} flows {validation.node_types && `(${validation.node_types.join(", ")})`}</span>
                : <span style={{ color: "#ef4444" }}>✗ {validation.error}</span>}
            </div>
          )}
          <div style={{ display: "flex", gap: 8 }}>
            <button style={{ ...S.btn, ...S.btnS }} onClick={validate}>Validate BPMN</button>
            <button style={{ ...S.btn, ...S.btnP }} onClick={save} disabled={loading || !form.name}>Commit</button>
            <button style={{ ...S.btn, ...S.btnS }} onClick={() => { setCreating(false); setValidation(null); setErr(null); }}>Cancel</button>
          </div>
        </div>
      )}

      <table style={S.tbl}>
        <thead>
          <tr>
            <th style={S.th}>Name</th>
            <th style={S.th}>Version</th>
            <th style={S.th}>Case Type</th>
            <th style={S.th}>Created By</th>
            <th style={S.th}>Status</th>
            <th style={S.th}>Created</th>
            <th style={S.th}></th>
          </tr>
        </thead>
        <tbody>
          {defs.map(d => (
            <React.Fragment key={d.id}>
              <tr style={{ background: editingId === d.id ? "#ede9fe" : "transparent" }}>
                <td style={S.td}>
                  <span style={{ fontWeight: 600, cursor: "pointer", color: "var(--accent)" }}
                    onClick={() => setExpanded(expanded === d.id ? null : d.id)}>
                    {d.name}
                  </span>
                  {d.description && <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>{d.description}</div>}
                </td>
                <td style={S.td}>v{d.version}</td>
                <td style={S.td}>
                  {d.case_type_id
                    ? <Badge label={truncate(ctName(d.case_type_id) ?? d.case_type_id, 24)} color="#0d9488" />
                    : <span style={{ color: "var(--text-secondary)", fontSize: 11 }}>any</span>}
                </td>
                <td style={{ ...S.td, fontSize: 11, color: "var(--text-secondary)" }}>{d.created_by ?? "—"}</td>
                <td style={S.td}><Badge label={d.status} color={STATUS_COLOR[d.status] ?? "#94a3b8"} /></td>
                <td style={S.td}>{fmtDate(d.created_at)}</td>
                <td style={{ ...S.td, whiteSpace: "nowrap" as const }}>
                  <button style={{ ...S.btn, ...S.btnS, fontSize: 11, marginRight: 6 }}
                    onClick={() => {
                      setEditingId(editingId === d.id ? null : d.id);
                      setEditForm({ name: d.name, description: d.description ?? "", case_type_id: d.case_type_id ?? "" });
                      setErr(null); setCreating(false);
                    }}>
                    {editingId === d.id ? "Cancel" : "Edit"}
                  </button>
                  {d.status === "active" && (
                    <button style={{ ...S.btn, ...S.btnD, fontSize: 11 }} onClick={() => archive(d.id)}>Archive</button>
                  )}
                </td>
              </tr>

              {/* Inline edit row */}
              {editingId === d.id && (
                <tr>
                  <td colSpan={7} style={{ padding: "0 0 4px", background: "var(--bg-surface)" }}>
                    <div style={{ padding: "14px 16px", borderTop: "2px solid var(--accent)", borderBottom: "1px solid var(--border)" }}>
                      <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 10 }}>EDIT DEFINITION</div>
                      <div style={S.row}>
                        <div style={{ flex: 2 }}>
                          <label style={S.label}>Name *</label>
                          <input style={S.input} value={editForm.name} onChange={e => setEditForm(f => ({ ...f, name: e.target.value }))} />
                        </div>
                        <div style={{ flex: 2 }}>
                          <label style={S.label}>Description</label>
                          <input style={S.input} value={editForm.description} onChange={e => setEditForm(f => ({ ...f, description: e.target.value }))} />
                        </div>
                        <div style={{ flex: 2 }}>
                          <label style={S.label}>Case Type</label>
                          <CaseTypeSelect value={editForm.case_type_id} onChange={v => setEditForm(f => ({ ...f, case_type_id: v }))} caseTypes={caseTypes} />
                        </div>
                      </div>
                      <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: 10 }}>
                        Note: BPMN XML cannot be edited on an existing definition (would break running instances). Archive and create a new version instead.
                      </div>
                      <div style={{ display: "flex", gap: 8 }}>
                        <button style={{ ...S.btn, ...S.btnP }} onClick={saveEdit} disabled={loading}>Save Changes</button>
                        <button style={{ ...S.btn, ...S.btnS }} onClick={() => { setEditingId(null); setErr(null); }}>Cancel</button>
                      </div>
                    </div>
                  </td>
                </tr>
              )}

              {/* BPMN XML expand */}
              {expanded === d.id && editingId !== d.id && (
                <tr>
                  <td colSpan={7} style={{ ...S.td, background: "var(--bg-main)", padding: "12px 16px" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                      <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)" }}>BPMN 2.0 XML</span>
                      <button
                        onClick={() => setViewingXml(viewingXml === d.id ? null : d.id)}
                        style={{ fontSize: 11, padding: "2px 8px", border: "1px solid var(--border)", borderRadius: 4, background: "var(--bg-surface)", cursor: "pointer", color: "var(--text-secondary)" }}
                      >
                        {viewingXml === d.id ? "Hide" : "Show"}
                      </button>
                    </div>
                    {viewingXml === d.id && (
                      <pre style={{ fontSize: 10, margin: 0, padding: 10, overflow: "auto", maxHeight: 260, background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 4 }}>{d.bpmn_xml}</pre>
                    )}
                  </td>
                </tr>
              )}
            </React.Fragment>
          ))}
          {defs.length === 0 && (
            <tr><td colSpan={7} style={{ ...S.td, color: "var(--text-secondary)", padding: 40 }}>No process definitions yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}


// ── Instances Tab ─────────────────────────────────────────────────────────────

function InstancesTab() {
  const [instances, setInstances] = useState<ProcessInstance[]>([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [expanded, setExpanded]   = useState<string | null>(null);
  const [taskLog, setTaskLog]     = useState<Record<string, TaskLog[]>>({});
  const [resumeId, setResumeId]   = useState<string | null>(null);
  const [resumeData, setResumeData] = useState("{}");

  const load = useCallback(async () => {
    const params = new URLSearchParams();
    if (statusFilter) params.set("status", statusFilter);
    const r = await fusionFetch(`${API}/instances?${params}&limit=50`);
    if (r.ok) setInstances(await r.json());
  }, [statusFilter]);
  useEffect(() => { load(); }, [load]);

  const loadLog = async (id: string) => {
    const r = await fusionFetch(`${API}/instances/${id}/log`);
    if (r.ok) { const data = await r.json(); setTaskLog(prev => ({ ...prev, [id]: data })); }
    setExpanded(id);
  };

  const cancel = async (id: string) => {
    await fusionFetch(`${API}/instances/${id}/cancel`, { method: "POST" });
    await load();
  };

  const resume = async (id: string) => {
    let resolution = {};
    try { resolution = JSON.parse(resumeData); } catch { /* ignore */ }
    await fusionFetch(`${API}/instances/${id}/resume`, {
      method: "POST", body: JSON.stringify({ resolution }),
    });
    setResumeId(null);
    await load();
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 10, marginBottom: 16, alignItems: "center" }}>
        <select style={{ ...S.select, width: 180 }} value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
          <option value="">All statuses</option>
          {["running", "completed", "failed", "suspended", "cancelled"].map(s =>
            <option key={s} value={s}>{s}</option>
          )}
        </select>
        <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{instances.length} instance(s)</span>
      </div>

      {instances.map(inst => (
        <div key={inst.id} style={S.card}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                <Badge label={inst.status} color={STATUS_COLOR[inst.status] ?? "#94a3b8"} />
                {inst.current_node && <Badge label={inst.current_node} color="#0d9488" />}
              </div>
              <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>
                Instance: <code>{inst.id.slice(0, 8)}…</code>
                {inst.case_id && <span> · Case: <code>{inst.case_id.slice(0, 8)}…</code></span>}
                · Started: {fmtDate(inst.started_at)}
                · Duration: {fmtDuration(inst.started_at, inst.ended_at)}
              </div>
              {inst.error_message && (
                <div style={{ marginTop: 6, fontSize: 11, color: "#ef4444", padding: "6px 10px", background: "#fee2e2", borderRadius: 4 }}>
                  Error at <strong>{inst.error_node}</strong>: {inst.error_message}
                </div>
              )}
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <button style={{ ...S.btn, ...S.btnS, fontSize: 11 }}
                onClick={() => expanded === inst.id ? setExpanded(null) : loadLog(inst.id)}>
                {expanded === inst.id ? "Hide Log" : "Execution Log"}
              </button>
              {(inst.status === "suspended" || inst.status === "failed") && (
                <button style={{ ...S.btn, ...S.btnY, fontSize: 11 }}
                  onClick={() => setResumeId(resumeId === inst.id ? null : inst.id)}>Resume</button>
              )}
              {inst.status === "running" && (
                <button style={{ ...S.btn, ...S.btnD, fontSize: 11 }} onClick={() => cancel(inst.id)}>Cancel</button>
              )}
            </div>
          </div>

          {resumeId === inst.id && (
            <div style={{ marginTop: 10, padding: "10px 12px", background: "var(--bg-main)", borderRadius: 6 }}>
              <label style={S.label}>Resolution context (JSON)</label>
              <textarea style={{ ...S.textarea, minHeight: 80 }} value={resumeData}
                onChange={e => setResumeData(e.target.value)} />
              <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                <button style={{ ...S.btn, ...S.btnP }} onClick={() => resume(inst.id)}>Resume</button>
                <button style={{ ...S.btn, ...S.btnS }} onClick={() => setResumeId(null)}>Cancel</button>
              </div>
            </div>
          )}

          {expanded === inst.id && taskLog[inst.id] && (
            <div style={{ marginTop: 10 }}>
              <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 6, color: "var(--text-secondary)" }}>EXECUTION LOG</div>
              {taskLog[inst.id].map(log => (
                <div key={log.id} style={{ display: "flex", gap: 10, alignItems: "center", padding: "5px 0", borderBottom: "1px solid var(--border)", fontSize: 11 }}>
                  <span style={{ minWidth: 80 }}><Badge label={log.node_type} color={NODE_COLOR[log.node_type] ?? "#0d9488"} /></span>
                  <span style={{ flex: 1, fontWeight: 500 }}>{log.node_name ?? log.node_id}</span>
                  <Badge label={log.status} color={STATUS_COLOR[log.status] ?? "#94a3b8"} />
                  <span style={{ color: "var(--text-secondary)", minWidth: 60 }}>{fmtDuration(log.started_at, log.ended_at)}</span>
                  {log.error && <span style={{ color: "#ef4444" }}>{log.error}</span>}
                </div>
              ))}
              {taskLog[inst.id].length === 0 && <div style={{ color: "var(--text-secondary)", fontSize: 11 }}>No log entries yet.</div>}
            </div>
          )}
        </div>
      ))}
      {instances.length === 0 && (
        <div style={{ color: "var(--text-secondary)", padding: 60 }}>No instances.</div>
      )}
    </div>
  );
}


// ── Bindings Tab ──────────────────────────────────────────────────────────────

function BindingsTab() {
  const [bindings, setBindings] = useState<ProcessBinding[]>([]);

  const load = useCallback(async () => {
    const r = await fusionFetch(`${API}/bindings?limit=100`);
    if (r.ok) setBindings(await r.json());
  }, []);
  useEffect(() => { load(); }, [load]);

  return (
    <div>
      <div style={{ ...S.card, background: "#f0f9ff", border: "1px solid #bae6fd", marginBottom: 16 }}>
        <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 6, color: "#0369a1" }}>What is a Binding?</div>
        <div style={{ fontSize: 12, color: "#0c4a6e", lineHeight: 1.6 }}>
          A binding links a <b>Case</b> to a running <b>Process Instance</b> at a specific stage/step.
          Bindings are created automatically when automation triggers, or manually for custom orchestration.
          <br /><b>How to bind:</b> Start a process from the Definitions tab (launch from a definition), or trigger via the API:
          <code style={{ display: "block", marginTop: 6, padding: "4px 8px", background: "#e0f2fe", borderRadius: 4, fontSize: 11 }}>
            POST /api/v1/fusion/instances {"{"} definition_id, case_id, context {"}"}
          </code>
          HxFusion automatically creates the binding record when an instance is launched with a case_id.
        </div>
      </div>

      <div style={{ fontWeight: 600, marginBottom: 14 }}>{bindings.length} case ↔ process binding(s)</div>
      <table style={S.tbl}>
        <thead>
          <tr>
            <th style={S.th}>Case</th>
            <th style={S.th}>Instance</th>
            <th style={S.th}>Type</th>
            <th style={S.th}>Direction</th>
            <th style={S.th}>Status</th>
            <th style={S.th}>Stage / Step</th>
            <th style={S.th}>Created</th>
          </tr>
        </thead>
        <tbody>
          {bindings.map(b => (
            <tr key={b.id}>
              <td style={S.td}><code>{b.case_id.slice(0, 8)}…</code></td>
              <td style={S.td}><code>{b.instance_id.slice(0, 8)}…</code></td>
              <td style={S.td}><Badge label={b.binding_type} color="#0d9488" /></td>
              <td style={S.td}><Badge label={b.direction} color="#3b82f6" /></td>
              <td style={S.td}><Badge label={b.status} color={STATUS_COLOR[b.status] ?? "#94a3b8"} /></td>
              <td style={S.td}>{b.stage_id ?? "—"}{b.step_id ? ` / ${b.step_id}` : ""}</td>
              <td style={S.td}>{fmtDate(b.created_at)}</td>
            </tr>
          ))}
          {bindings.length === 0 && (
            <tr><td colSpan={7} style={{ ...S.td, color: "var(--text-secondary)", padding: 32 }}>
              No bindings yet. Launch a process instance with a case_id to create one.
            </td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}


// ── AI Director Tab ───────────────────────────────────────────────────────────

function DirectorTab() {
  const caseTypes = useCaseTypes();
  const [form, setForm]     = useState({ case_type_id: "", stage_id: "", case_id: "", context: "{}" });
  const [result, setResult] = useState<DirectorResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError]   = useState<string | null>(null);

  // Stages filtered by selected case type
  const selectedCT = caseTypes.find(ct => ct.id === form.case_type_id);
  const stages: Stage[] = stagesFromCaseType(selectedCT);

  const advise = async () => {
    let context = {};
    try { context = JSON.parse(form.context); } catch { /* ignore */ }
    setLoading(true); setError(null);
    try {
      const r = await fusionFetch(`${API}/director/advise`, {
        method: "POST", body: JSON.stringify({
          case_id: form.case_id || "00000000-0000-0000-0000-000000000000",
          stage_id: form.stage_id,
          case_type_id: form.case_type_id || null,
          context,
        }),
      });
      if (r.ok) setResult(await r.json());
      else setError(`Error ${r.status}: ${await r.text()}`);
    } catch (e) { setError(String(e)); }
    setLoading(false);
  };

  return (
    <div>
      {/* Explanation card */}
      <div style={{ ...S.card, background: "#fefce8", border: "1px solid #fde68a", marginBottom: 16 }}>
        <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 6, color: "#92400e" }}>How the AI Director works</div>
        <div style={{ fontSize: 12, color: "#78350f", lineHeight: 1.6 }}>
          <b>Case Type</b> — which type of case (e.g. Insurance Claim). Select first; Stage list is then filtered to that type's stages.<br />
          <b>Stage ID</b> — the current stage the case is at (e.g. "investigation"). The Director reasons about what to automate at this stage.<br />
          <b>Case ID</b> — <i>optional</i>. If provided, the Director reads the live case record (fields, history) for richer context. Without it, reasoning uses only type + stage + the JSON context you pass.<br />
          <b>Context</b> — extra data you know right now: <code>{`{"amount": 50000, "risk_score": 0.8}`}</code>. Merged with case data when deciding.
        </div>
      </div>

      <div style={{ ...S.card, borderColor: "var(--accent)" }}>
        <div style={{ fontWeight: 600, marginBottom: 14 }}>Ask HxFusion Director</div>

        {/* Step 1: Case Type */}
        <div style={{ ...S.row }}>
          <div style={{ flex: 2 }}>
            <label style={S.label}>1. Case Type <span style={{ fontWeight: 400, color: "var(--text-secondary)" }}>(select first to load stages)</span></label>
            <CaseTypeSelect
              value={form.case_type_id}
              onChange={v => setForm(f => ({ ...f, case_type_id: v, stage_id: "" }))}
              caseTypes={caseTypes}
              placeholder="— Select case type —"
            />
          </div>
          {/* Step 2: Stage — only shown after case type is selected */}
          <div style={{ flex: 1 }}>
            <label style={S.label}>2. Stage {!form.case_type_id && <span style={{ fontWeight: 400, color: "#f59e0b" }}>(select case type first)</span>}</label>
            <select style={{ ...S.select, opacity: form.case_type_id ? 1 : 0.5 }}
              value={form.stage_id}
              onChange={e => setForm(f => ({ ...f, stage_id: e.target.value }))}
              disabled={!form.case_type_id}>
              <option value="">— select stage —</option>
              {stages.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
              {form.case_type_id && stages.length === 0 && <option disabled>No stages defined for this case type</option>}
            </select>
          </div>
        </div>

        {/* Case ID — optional, step 3 */}
        <div style={{ marginBottom: 10 }}>
          <label style={S.label}>
            3. Case ID <span style={{ fontWeight: 400, color: "var(--text-secondary)" }}>(optional — gives Director access to live case data)</span>
          </label>
          <input style={S.input} value={form.case_id}
            onChange={e => setForm(f => ({ ...f, case_id: e.target.value }))}
            placeholder="UUID of an existing case — leave blank to reason from type+stage+context only" />
        </div>

        {/* Context JSON */}
        <div style={{ marginBottom: 10 }}>
          <label style={S.label}>
            4. Context (JSON) <span style={{ fontWeight: 400, color: "var(--text-secondary)" }}>— extra data for the Director</span>
          </label>
          <textarea style={{ ...S.textarea, minHeight: 80 }} value={form.context}
            onChange={e => setForm(f => ({ ...f, context: e.target.value }))}
            placeholder={`{\n  "amount": 50000,\n  "risk_score": 0.8,\n  "is_repeat_customer": false\n}`} />
        </div>

        <button style={{ ...S.btn, ...S.btnP }} onClick={advise} disabled={loading || !form.stage_id}>
          {loading ? "Asking HxNexus…" : "Get Recommendation"}
        </button>
        {!form.stage_id && <span style={{ marginLeft: 10, fontSize: 11, color: "var(--text-secondary)" }}>Select a case type and stage first</span>}
      </div>

      {error && <div style={{ color: "#ef4444", fontSize: 12, marginTop: 8, padding: "8px 12px", background: "#fef2f2", borderRadius: 6 }}>{error}</div>}

      {result && (
        <div style={{ ...S.card, borderColor: result.can_automate ? "#22c55e" : "#f59e0b" }}>
          <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12 }}>
            <div style={{ fontSize: 28 }}>{result.can_automate ? "🤖" : "👤"}</div>
            <div>
              <div style={{ fontWeight: 700, fontSize: 15 }}>{result.suggestion}</div>
              <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 3 }}>
                Confidence: <strong>{Math.round(result.confidence * 100)}%</strong>
                {result.recommended_definition_id && (
                  <span> · Definition: <code>{result.recommended_definition_id.slice(0, 8)}…</code></span>
                )}
              </div>
            </div>
          </div>
          <div style={{ fontSize: 12, padding: "8px 12px", background: "var(--bg-main)", borderRadius: 6, borderLeft: "3px solid var(--accent)" }}>
            {result.reasoning}
          </div>
        </div>
      )}
    </div>
  );
}


// ── Stats Tab ─────────────────────────────────────────────────────────────────

function StatsTab() {
  const [stats, setStats] = useState<Stats | null>(null);

  const load = useCallback(async () => {
    const r = await fusionFetch(`${API}/stats`);
    if (r.ok) setStats(await r.json());
  }, []);
  useEffect(() => { load(); }, [load]);

  if (!stats) return <div style={{ padding: 40, color: "var(--text-secondary)" }}>Loading…</div>;

  const total      = Object.values(stats.instances_by_status).reduce((a, b) => a + b, 0);
  const completed  = stats.instances_by_status.completed ?? 0;
  const failed     = stats.instances_by_status.failed ?? 0;
  const running    = stats.instances_by_status.running ?? 0;
  const successRate = total > 0 ? Math.round((completed / total) * 100) : null;
  const failRate    = total > 0 ? Math.round((failed / total) * 100) : null;

  return (
    <div>
      {/* KPI row */}
      <div style={{ display: "flex", gap: 14, marginBottom: 24, flexWrap: "wrap" as const }}>
        <div style={S.statBox}>
          <div style={S.statNum}>{stats.total_definitions}</div>
          <div style={S.statLbl}>Process Definitions</div>
        </div>
        <div style={S.statBox}>
          <div style={S.statNum}>{total}</div>
          <div style={S.statLbl}>Total Instances</div>
        </div>
        <div style={S.statBox}>
          <div style={{ ...S.statNum, color: "#22c55e" }}>{running}</div>
          <div style={S.statLbl}>Currently Running</div>
        </div>
        <div style={S.statBox}>
          <div style={{ ...S.statNum, color: successRate !== null && successRate >= 80 ? "#22c55e" : "#f59e0b" }}>
            {successRate !== null ? `${successRate}%` : "—"}
          </div>
          <div style={S.statLbl}>Success Rate</div>
        </div>
        <div style={S.statBox}>
          <div style={{ ...S.statNum, color: "#ef4444" }}>{failed}</div>
          <div style={S.statLbl}>Failed {failRate !== null ? `(${failRate}%)` : ""}</div>
        </div>
        <div style={S.statBox}>
          <div style={S.statNum}>{stats.total_tasks_executed}</div>
          <div style={S.statLbl}>Nodes Executed</div>
        </div>
      </div>

      {/* Status breakdown */}
      {Object.keys(stats.instances_by_status).length > 0 && (
        <div style={S.card}>
          <div style={{ fontWeight: 600, marginBottom: 14 }}>Instances by Status</div>
          {Object.entries(stats.instances_by_status)
            .sort(([, a], [, b]) => b - a)
            .map(([status, count]) => (
              <div key={status} style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 10 }}>
                <div style={{ minWidth: 80 }}><Badge label={status} color={STATUS_COLOR[status] ?? "#94a3b8"} /></div>
                <div style={{ flex: 1, height: 10, background: "var(--border)", borderRadius: 5 }}>
                  <div style={{ width: total ? `${(count / total) * 100}%` : "0%", height: "100%", background: STATUS_COLOR[status] ?? "#94a3b8", borderRadius: 5 }} />
                </div>
                <span style={{ fontSize: 13, fontWeight: 700, minWidth: 28, textAlign: "right" as const }}>{count}</span>
                <span style={{ fontSize: 11, color: "var(--text-secondary)", minWidth: 36 }}>
                  {total ? `${Math.round((count / total) * 100)}%` : ""}
                </span>
              </div>
            ))}
        </div>
      )}

      {/* Node types */}
      <div style={S.card}>
        <div style={{ fontWeight: 600, marginBottom: 10 }}>Supported BPMN Node Types</div>
        <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 6 }}>
          {Object.entries(NODE_COLOR).map(([type, color]) => <Badge key={type} label={type} color={color} />)}
        </div>
      </div>
    </div>
  );
}


// ── Root ──────────────────────────────────────────────────────────────────────

const TABS = ["Definitions", "Instances", "Bindings", "AI Director", "Stats"] as const;

export default function HxFusion() {
  const [tab, setTab] = useState<typeof TABS[number]>("Definitions");
  return (
    <div style={S.page}>
      <div style={S.tabBar}>
        {TABS.map(t => (
          <button key={t} style={{ ...S.tab, ...(tab === t ? S.tabActive : {}) }} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>
      <div style={S.body}>
        {tab === "Definitions" && <DefinitionsTab />}
        {tab === "Instances"   && <InstancesTab />}
        {tab === "Bindings"    && <BindingsTab />}
        {tab === "AI Director" && <DirectorTab />}
        {tab === "Stats"       && <StatsTab />}
      </div>
    </div>
  );
}
