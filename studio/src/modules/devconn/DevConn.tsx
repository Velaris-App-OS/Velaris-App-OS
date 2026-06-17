/**
 * P53 — HxConnect: Developer & Custom Connectors
 * Tabs: Builder · Webhook Rules · Events · OpenAPI
 */
import React, { useState, useEffect, useCallback } from "react";
import { Button } from "@shared/components";

const API = "/api/v1/devconn";
function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
}


// ── Shared styles ─────────────────────────────────────────────────────────────

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
  value:   { fontSize: 13, color: "var(--text-primary)", marginBottom: 12 },
  input:   { width: "100%", padding: "8px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const, marginBottom: 10 },
  row:     { display: "flex", gap: 8, alignItems: "center", padding: "10px 14px", borderBottom: "1px solid var(--border-subtle)", cursor: "pointer" },
  badge:   { fontSize: 10, padding: "2px 8px", borderRadius: 4, fontWeight: 700, textTransform: "uppercase" as const },
  grid2:   { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 },
  section: { marginBottom: "var(--space-lg)" },
  mono:    { fontFamily: "var(--font-mono)", fontSize: 11 },
};

const sc = (s: string) => ({
  matched: "#22c55e", received: "#3b82f6", no_match: "#94a3b8", error: "#ef4444"
}[s] ?? "#94a3b8");

// ── Builder Tab ───────────────────────────────────────────────────────────────

function BuilderTab() {
  const [name, setName]           = useState("");
  const [method, setMethod]       = useState("POST");
  const [url, setUrl]             = useState("");
  const [authType, setAuthType]   = useState("none");
  const [token, setToken]         = useState("");
  const [username, setUsername]   = useState("");
  const [password, setPassword]   = useState("");
  const [bodyTpl, setBodyTpl]     = useState("");
  const [respMap, setRespMap]     = useState("");
  const [headers, setHeaders]     = useState("");
  const [varNamespace, setVarNamespace] = useState("");
  const [connectors, setConnectors] = useState<any[]>([]);
  const [msg, setMsg]             = useState<{ text: string; ok: boolean } | null>(null);
  const [loading, setLoading]     = useState(false);

  useEffect(() => {
    authFetch(`${API}/connectors`).then(r => r.ok ? r.json() : []).then(setConnectors).catch(() => {});
  }, []);

  const handleBuild = async () => {
    if (!name.trim() || !url.trim()) { setMsg({ text: "Name and URL are required.", ok: false }); return; }
    setLoading(true); setMsg(null);
    try {
      let parsedHeaders: Record<string,string> = {};
      let parsedRespMap: Record<string,string> = {};
      if (headers.trim()) parsedHeaders = JSON.parse(headers);
      if (respMap.trim()) parsedRespMap = JSON.parse(respMap);

      const creds: Record<string,string> = {};
      if (authType === "bearer" && token) creds.token = token;
      if (authType === "basic") { creds.username = username; creds.password = password; }

      const r = await authFetch(`${API}/connectors/build`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, method, url, headers: parsedHeaders, auth_type: authType, body_template: bodyTpl, response_mapping: parsedRespMap, credentials: creds, variable_namespace: varNamespace.trim() || null }),
      });
      if (r.ok) {
        const c = await r.json();
        setConnectors(cs => [c, ...cs]);
        setMsg({ text: `Connector "${name}" created (${c.id})`, ok: true });
        setName(""); setUrl(""); setBodyTpl(""); setRespMap(""); setHeaders(""); setVarNamespace("");
      } else {
        const err = await r.json();
        setMsg({ text: err.detail || "Failed", ok: false });
      }
    } catch (e: any) {
      setMsg({ text: e.message || "Invalid JSON", ok: false });
    }
    setLoading(false);
  };

  return (
    <div style={S.grid2}>
      {/* Form */}
      <div>
        <div style={S.card}>
          <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 16 }}>Build HTTP Connector</div>

          <div style={S.label}>Connector Name</div>
          <input style={S.input} placeholder="e.g. Notify Slack" value={name} onChange={e => setName(e.target.value)} />

          <div style={S.grid2}>
            <div>
              <div style={S.label}>Method</div>
              <select value={method} onChange={e => setMethod(e.target.value)}
                style={{ ...S.input, marginBottom: 10 }}>
                {["GET","POST","PUT","PATCH","DELETE"].map(m => <option key={m}>{m}</option>)}
              </select>
            </div>
            <div>
              <div style={S.label}>Auth Type</div>
              <select value={authType} onChange={e => setAuthType(e.target.value)}
                style={{ ...S.input, marginBottom: 10 }}>
                {["none","bearer","basic"].map(a => <option key={a}>{a}</option>)}
              </select>
            </div>
          </div>

          <div style={S.label}>URL (use &#123;var&#125; for placeholders)</div>
          <input style={S.input} placeholder="https://api.example.com/endpoint/{case_id}" value={url} onChange={e => setUrl(e.target.value)} />

          {authType === "bearer" && (
            <>
              <div style={S.label}>Bearer Token</div>
              <input style={S.input} type="password" placeholder="sk-..." value={token} onChange={e => setToken(e.target.value)} />
            </>
          )}
          {authType === "basic" && (
            <div style={S.grid2}>
              <div>
                <div style={S.label}>Username</div>
                <input style={S.input} value={username} onChange={e => setUsername(e.target.value)} />
              </div>
              <div>
                <div style={S.label}>Password</div>
                <input style={S.input} type="password" value={password} onChange={e => setPassword(e.target.value)} />
              </div>
            </div>
          )}

          <div style={S.label}>Headers (JSON, optional)</div>
          <textarea style={{ ...S.input, fontFamily: "var(--font-mono)", fontSize: 11, resize: "vertical" }} rows={2}
            placeholder='{"X-Api-Key": "my-key"}' value={headers} onChange={e => setHeaders(e.target.value)} />

          <div style={S.label}>Body Template (JSON with &#123;var&#125; placeholders)</div>
          <textarea style={{ ...S.input, fontFamily: "var(--font-mono)", fontSize: 11, resize: "vertical" }} rows={3}
            placeholder='{"case_id": "{case_id}", "message": "{summary}"}' value={bodyTpl} onChange={e => setBodyTpl(e.target.value)} />

          <div style={S.label}>Response Field Mapping (JSON: case_field → response.json.path)</div>
          <textarea style={{ ...S.input, fontFamily: "var(--font-mono)", fontSize: 11, resize: "vertical" }} rows={2}
            placeholder='{"external_id": "data.id"}' value={respMap} onChange={e => setRespMap(e.target.value)} />

          <div style={S.label}>Variable Namespace (optional — case variables this connector writes)</div>
          <input style={S.input} placeholder="e.g. payment_gw" value={varNamespace} onChange={e => setVarNamespace(e.target.value)} />

          {msg && <div style={{ fontSize: 12, padding: "8px 12px", borderRadius: 4, marginBottom: 12, background: msg.ok ? "#22c55e22" : "#ef444422", color: msg.ok ? "#22c55e" : "#ef4444" }}>{msg.text}</div>}
          <Button disabled={loading || !name || !url} onClick={handleBuild}>{loading ? "Creating…" : "Create Connector"}</Button>
        </div>
      </div>

      {/* Existing custom connectors */}
      <div>
        <div style={S.card}>
          <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 12 }}>Custom Connectors ({connectors.length})</div>
          {connectors.length === 0 && <div style={{ fontSize: 12, color: "var(--text-muted)" }}>No custom connectors yet.</div>}
          {connectors.map(c => (
            <div key={c.id} style={{ padding: "10px 0", borderBottom: "1px solid var(--border-subtle)" }}>
              <div style={{ fontWeight: 600, fontSize: 13 }}>{c.name}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{c.id}</div>
              <span style={{ ...S.badge, background: c.enabled ? "#22c55e22" : "#94a3b822", color: c.enabled ? "#22c55e" : "#94a3b8" }}>
                {c.enabled ? "enabled" : "disabled"}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── OpenAPI Tab ───────────────────────────────────────────────────────────────

function OpenAPITab() {
  const [spec, setSpec]       = useState("");
  const [connName, setConnName] = useState("Generated Connector");
  const [result, setResult]   = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [msg, setMsg]         = useState<string | null>(null);

  const handleGenerate = async () => {
    if (!spec.trim()) { setMsg("Paste an OpenAPI spec first."); return; }
    setLoading(true); setMsg(null); setResult(null);
    const r = await authFetch(`${API}/connectors/from-openapi`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec, connector_name: connName }),
    });
    if (r.ok) setResult(await r.json());
    else setMsg("Generation failed.");
    setLoading(false);
  };

  return (
    <div>
      <div style={S.card}>
        <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 4 }}>OpenAPI → Connector</div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 14 }}>
          Paste any OpenAPI / Swagger spec. HxNexus analyses it and suggests connector operations ready to deploy.
        </div>

        <div style={S.label}>Connector Name</div>
        <input style={{ ...S.input, maxWidth: 320 }} value={connName} onChange={e => setConnName(e.target.value)} />

        <div style={S.label}>OpenAPI Spec (JSON or YAML)</div>
        <textarea style={{ ...S.input, fontFamily: "var(--font-mono)", fontSize: 11, resize: "vertical" }} rows={10}
          placeholder='{"openapi": "3.0.0", "info": {...}, "paths": {...}}'
          value={spec} onChange={e => setSpec(e.target.value)} />

        {msg && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 10 }}>{msg}</div>}
        <Button disabled={loading || !spec} onClick={handleGenerate}>{loading ? "Analysing…" : "Generate Connector Config"}</Button>
      </div>

      {result && (
        <div style={S.card}>
          <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 4 }}>{result.name}</div>
          {result.base_url && <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4 }}>Base URL: <code>{result.base_url}</code></div>}
          {result.auth_notes && <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>Auth: {result.auth_notes}</div>}

          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>Suggested Operations ({result.suggested_operations?.length ?? 0})</div>
          {(result.suggested_operations ?? []).map((op: any, i: number) => (
            <div key={i} style={{ border: "1px solid var(--border-subtle)", borderRadius: 6, padding: "10px 14px", marginBottom: 8 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                <span style={{ ...S.badge, background: "#3b82f622", color: "#3b82f6" }}>{op.method}</span>
                <span style={{ fontWeight: 600, fontSize: 13 }}>{op.operation_id}</span>
                {op.step_type_suggestion && <span style={{ ...S.badge, background: "#0f766e22", color: "#0f766e" }}>{op.step_type_suggestion}</span>}
              </div>
              {op.summary && <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4 }}>{op.summary}</div>}
              <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>{op.url}</div>
              {Object.keys(op.response_mapping ?? {}).length > 0 && (
                <div style={{ fontSize: 11, marginTop: 6, color: "var(--text-muted)" }}>
                  Maps: {Object.entries(op.response_mapping).map(([k,v]) => `${k} ← ${v}`).join(" · ")}
                </div>
              )}
            </div>
          ))}

          <div style={{ marginTop: 12 }}>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>
              Review and use the Builder tab to deploy individual operations as custom connectors.
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Webhook Rules Tab ─────────────────────────────────────────────────────────

function WebhookRulesTab() {
  const [rules, setRules]         = useState<any[]>([]);
  const [connectors, setConnectors] = useState<any[]>([]);
  const [showForm, setShowForm]   = useState(false);
  const [form, setForm]           = useState({ name: "", connector_id: "", case_id_field: "", match_case_field: "", match_payload_field: "", field_updates: "", advance_stage: false });
  const [msg, setMsg]             = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [rr, rc] = await Promise.all([
        authFetch(`${API}/connectors/inbound`).then(r => r.ok ? r.json() : []).catch(() => []),
        authFetch(`/api/v1/hxbridge/connectors`).then(r => r.ok ? r.json() : {}).catch(() => {}),
      ]);
      setRules(Array.isArray(rr) ? rr : []);
      const connList = Array.isArray(rc) ? rc : ((rc as any)?.connectors ?? []);
      setConnectors(connList);
    } catch { setRules([]); setConnectors([]); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleCreate = async () => {
    if (!form.name) { setMsg("Name is required."); return; }
    let field_updates: Record<string,string> = {};
    try { if (form.field_updates.trim()) field_updates = JSON.parse(form.field_updates); } catch { setMsg("Field updates must be valid JSON."); return; }
    const r = await authFetch(`${API}/connectors/inbound`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...form, connector_id: form.connector_id || null, field_updates, advance_stage: form.advance_stage }),
    });
    if (r.ok) { await load(); setShowForm(false); setMsg(null); } else setMsg("Failed to create rule.");
  };

  const handleDelete = async (id: string) => {
    await authFetch(`${API}/connectors/inbound/${id}`, { method: "DELETE" });
    setRules(rs => rs.filter(r => r.id !== id));
  };

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <div style={{ fontSize: 14, fontWeight: 700 }}>Inbound Connectors</div>
        <Button size="sm" onClick={() => setShowForm(s => !s)}>{showForm ? "Cancel" : "+ New Rule"}</Button>
      </div>

      {showForm && (
        <div style={S.card}>
          <div style={S.label}>Rule Name</div>
          <input style={S.input} placeholder="e.g. Route Stripe webhooks" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} />

          <div style={S.label}>Connector (optional — leave blank to match any)</div>
          <select style={S.input} value={form.connector_id} onChange={e => setForm(f => ({ ...f, connector_id: e.target.value }))}>
            <option value="">Any connector</option>
            {connectors.map((c: any) => <option key={c.id} value={c.id}>{c.name} ({c.connector_type})</option>)}
          </select>

          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8, marginTop: 4 }}>Case Routing</div>
          <div style={S.label}>Payload field containing Case UUID (dot-path, e.g. <code>data.case_id</code>)</div>
          <input style={S.input} placeholder="data.case_id" value={form.case_id_field} onChange={e => setForm(f => ({ ...f, case_id_field: e.target.value }))} />

          <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 8 }}>— or match by value —</div>
          <div style={S.grid2}>
            <div>
              <div style={S.label}>Case field to match on</div>
              <input style={S.input} placeholder="reference_number" value={form.match_case_field} onChange={e => setForm(f => ({ ...f, match_case_field: e.target.value }))} />
            </div>
            <div>
              <div style={S.label}>Payload field with match value</div>
              <input style={S.input} placeholder="metadata.ref" value={form.match_payload_field} onChange={e => setForm(f => ({ ...f, match_payload_field: e.target.value }))} />
            </div>
          </div>

          <div style={S.label}>Field Updates (JSON: &#123;"case_field": "payload.dotpath"&#125;)</div>
          <textarea style={{ ...S.input, fontFamily: "var(--font-mono)", fontSize: 11 }} rows={2}
            placeholder='{"status": "data.status", "external_ref": "id"}'
            value={form.field_updates} onChange={e => setForm(f => ({ ...f, field_updates: e.target.value }))} />

          <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, marginBottom: 12 }}>
            <input type="checkbox" checked={form.advance_stage} onChange={e => setForm(f => ({ ...f, advance_stage: e.target.checked }))} />
            Auto-advance case stage after update
          </label>

          {msg && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <Button onClick={handleCreate}>Create Rule</Button>
        </div>
      )}

      {rules.length === 0 && !showForm && <div style={{ fontSize: 13, color: "var(--text-muted)" }}>No routing rules yet. Create one to route inbound webhooks to cases.</div>}

      {rules.map(rule => (
        <div key={rule.id} style={S.card}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 4 }}>{rule.name}</div>
              {rule.case_id_field && <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Route by: <code>{rule.case_id_field}</code></div>}
              {rule.match_case_field && <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Match: case.<code>{rule.match_case_field}</code> = payload.<code>{rule.match_payload_field}</code></div>}
              {Object.keys(rule.field_updates || {}).length > 0 && (
                <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>
                  Updates: {Object.entries(rule.field_updates).map(([k,v]) => `${k}←${v}`).join(", ")}
                </div>
              )}
              {rule.advance_stage && <div style={{ fontSize: 11, color: "#22c55e", marginTop: 4 }}>↑ Auto-advances stage</div>}
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", wordBreak: "break-all" }}>
                POST /api/v1/devconn/webhooks/receive/{rule.connector_id ?? "any"}
              </div>
              <button onClick={() => handleDelete(rule.id)}
                style={{ fontSize: 11, color: "#ef4444", background: "none", border: "1px solid #ef444444", borderRadius: 4, padding: "3px 8px", cursor: "pointer" }}>Delete</button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Outbound Rules Tab ────────────────────────────────────────────────────────

const TRIGGER_EVENTS = ["case_created", "stage_enter", "stage_exit", "step_complete", "field_change"];

function OutboundRulesTab() {
  const [rules, setRules]         = useState<any[]>([]);
  const [connectors, setConnectors] = useState<any[]>([]);
  const [showForm, setShowForm]   = useState(false);
  const [form, setForm]           = useState({
    name: "", trigger_event: "stage_enter", connector_id: "",
    case_type_id: "", input_mapping: "", condition_expr: "", enabled: true,
  });
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [rr, rc] = await Promise.all([
        authFetch(`${API}/connectors/outbound`).then(r => r.ok ? r.json() : []).catch(() => []),
        authFetch(`/api/v1/hxbridge/connectors`).then(r => r.ok ? r.json() : {}).catch(() => {}),
      ]);
      setRules(Array.isArray(rr) ? rr : []);
      setConnectors(Array.isArray(rc) ? rc : ((rc as any)?.connectors ?? []));
    } catch { setRules([]); setConnectors([]); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleCreate = async () => {
    if (!form.name) { setMsg("Name is required."); return; }
    let input_mapping: Record<string, string> = {};
    let condition_expr: Record<string, any> | null = null;
    try { if (form.input_mapping.trim()) input_mapping = JSON.parse(form.input_mapping); } catch { setMsg("Input mapping must be valid JSON."); return; }
    try { if (form.condition_expr.trim()) condition_expr = JSON.parse(form.condition_expr); } catch { setMsg("Condition must be valid JSON."); return; }
    const body: any = {
      name: form.name, trigger_event: form.trigger_event,
      connector_id: form.connector_id || null,
      case_type_id: form.case_type_id || null,
      input_mapping, condition_expr, enabled: form.enabled,
    };
    const r = await authFetch(`${API}/connectors/outbound`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (r.ok) { await load(); setShowForm(false); setMsg(null); }
    else { const d = await r.json(); setMsg(d.detail || "Failed to create rule."); }
  };

  const handleDelete = async (id: string) => {
    await authFetch(`${API}/connectors/outbound/${id}`, { method: "DELETE" });
    setRules(rs => rs.filter(r => r.id !== id));
  };

  const handleToggle = async (rule: any) => {
    await authFetch(`${API}/connectors/outbound/${rule.id}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...rule, enabled: !rule.enabled }),
    });
    await load();
  };

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <div style={{ fontSize: 14, fontWeight: 700 }}>Outbound Connectors</div>
        <Button size="sm" onClick={() => setShowForm(s => !s)}>{showForm ? "Cancel" : "+ New Rule"}</Button>
      </div>

      {showForm && (
        <div style={S.card}>
          <div style={S.label}>Rule Name</div>
          <input style={S.input} placeholder="e.g. Notify Salesforce on stage advance" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} />

          <div style={S.grid2}>
            <div>
              <div style={S.label}>Trigger Event</div>
              <select style={S.input} value={form.trigger_event} onChange={e => setForm(f => ({ ...f, trigger_event: e.target.value }))}>
                {TRIGGER_EVENTS.map(ev => <option key={ev} value={ev}>{ev}</option>)}
              </select>
            </div>
            <div>
              <div style={S.label}>Connector</div>
              <select style={S.input} value={form.connector_id} onChange={e => setForm(f => ({ ...f, connector_id: e.target.value }))}>
                <option value="">— select connector —</option>
                {connectors.map((c: any) => <option key={c.id} value={c.id}>{c.name} ({c.connector_type})</option>)}
              </select>
            </div>
          </div>

          <div style={S.label}>Case Type ID (optional — leave blank to apply to all)</div>
          <input style={S.input} placeholder="UUID of a specific case type" value={form.case_type_id} onChange={e => setForm(f => ({ ...f, case_type_id: e.target.value }))} />

          <div style={S.label}>Input Mapping (JSON: &#123;"connector_param": "case_field_key"&#125;)</div>
          <textarea style={{ ...S.input, fontFamily: "var(--font-mono)", fontSize: 11 }} rows={2}
            placeholder='{"customer_id": "ref_number"}' value={form.input_mapping} onChange={e => setForm(f => ({ ...f, input_mapping: e.target.value }))} />

          <div style={S.label}>Condition (JSON, optional — only fire if this is true)</div>
          <textarea style={{ ...S.input, fontFamily: "var(--font-mono)", fontSize: 11 }} rows={2}
            placeholder='{"status": "approved"}' value={form.condition_expr} onChange={e => setForm(f => ({ ...f, condition_expr: e.target.value }))} />

          <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, marginBottom: 12 }}>
            <input type="checkbox" checked={form.enabled} onChange={e => setForm(f => ({ ...f, enabled: e.target.checked }))} />
            Rule enabled
          </label>

          {msg && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <Button onClick={handleCreate}>Create Rule</Button>
        </div>
      )}

      {rules.length === 0 && !showForm && (
        <div style={{ fontSize: 13, color: "var(--text-muted)" }}>No outbound connectors yet. Create one to fire a connector when a case event occurs.</div>
      )}

      {rules.map(rule => (
        <div key={rule.id} style={S.card}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 4 }}>
                {rule.name}
                {!rule.enabled && <span style={{ ...S.badge, background: "#94a3b822", color: "#94a3b8", marginLeft: 8 }}>disabled</span>}
              </div>
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                On: <code>{rule.trigger_event}</code>
                {rule.case_type_id && <> · Case type: <code>{rule.case_type_id.slice(0, 8)}…</code></>}
              </div>
              {Object.keys(rule.input_mapping || {}).length > 0 && (
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                  Mapping: {Object.entries(rule.input_mapping).map(([k, v]) => `${k}←${v}`).join(", ")}
                </div>
              )}
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button onClick={() => handleToggle(rule)}
                style={{ fontSize: 11, background: "none", border: "1px solid var(--border)", borderRadius: 4, padding: "3px 8px", cursor: "pointer", color: "var(--text-muted)" }}>
                {rule.enabled ? "Disable" : "Enable"}
              </button>
              <button onClick={() => handleDelete(rule.id)}
                style={{ fontSize: 11, color: "#ef4444", background: "none", border: "1px solid #ef444444", borderRadius: 4, padding: "3px 8px", cursor: "pointer" }}>Delete</button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Root ──────────────────────────────────────────────────────────────────────

type Tab = "builder" | "openapi" | "inbound_rules" | "outbound_rules";

export default function DevConn() {
  const [tab, setTab] = useState<Tab>("builder");

  const TABS: { key: Tab; label: string }[] = [
    { key: "builder",        label: "HTTP Connector Builder" },
    { key: "openapi",        label: "OpenAPI → Connector" },
    { key: "inbound_rules",  label: "Inbound Connectors" },
    { key: "outbound_rules", label: "Outbound Connectors" },
  ];

  return (
    <div style={S.page}>
      <div style={S.tabs}>
        {TABS.map(({ key, label }) => (
          <button key={key} style={{ ...S.tab, ...(tab === key ? S.tabActive : {}) }} onClick={() => setTab(key)}>{label}</button>
        ))}
      </div>

      {tab === "builder"        && <BuilderTab />}
      {tab === "openapi"        && <OpenAPITab />}
      {tab === "inbound_rules"  && <WebhookRulesTab />}
      {tab === "outbound_rules" && <OutboundRulesTab />}
    </div>
  );
}
