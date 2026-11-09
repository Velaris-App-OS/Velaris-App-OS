import React, { useEffect, useState } from "react";
import type { FormFieldDef } from "./FormBuilder";
import { fieldKey } from "./validate";
import { listFormConnectors, runFormLookup } from "@shared/api/client";
import type { ConnectorSummary } from "@shared/api/client";

/* ═══════════════════════════════════════════════════════════════════
   LookupEditor — a connector lookup set up with pickers
   (Form Builder redesign part 7, 2026-10-28).

   Connector from a list; its inputs mapped from form fields by dropdown;
   a Test run in the builder shows what comes back, and the result's
   paths become the choices for "put this into that field". No JSON.
   Stored in `source` exactly as before (connector_id, input_mapping,
   response_path, target_fields) plus trigger / on_many / button_label.
   ═══════════════════════════════════════════════════════════════════ */

type Src = {
  connector_id?: string; input_mapping?: Record<string, string>; response_path?: string;
  target_fields?: Record<string, string>; trigger?: "button" | "change";
  on_many?: "first" | "choose" | "error"; button_label?: string; [k: string]: any;
};

/** Every path in a value, depth-first, for the pickers ("" = the whole value). */
export function pathsOf(value: any, prefix = "", depth = 0, out: { path: string; kind: "list" | "record" | "value" }[] = []) {
  if (depth > 5 || value === null || value === undefined) return out;
  if (Array.isArray(value)) {
    out.push({ path: prefix, kind: "list" });   // its records are offered once it is chosen as the record
    return out;
  }
  if (typeof value === "object") {
    if (prefix) out.push({ path: prefix, kind: "record" });
    for (const [k, v] of Object.entries(value)) pathsOf(v, prefix ? `${prefix}.${k}` : k, depth + 1, out);
    return out;
  }
  out.push({ path: prefix, kind: "value" });
  return out;
}

export function atPath(obj: any, path?: string): any {
  if (!path) return obj;
  return path.split(".").reduce((o: any, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
}

export default function LookupEditor({ field, fields, formId, onUpdate }: {
  field: FormFieldDef;
  fields: FormFieldDef[];
  /** The saved form — a Test of a saved lookup runs as any user may. */
  formId?: string;
  onUpdate: (p: Partial<FormFieldDef>) => void;
}) {
  const src: Src = (field.source as any) ?? {};
  const set = (p: Partial<Src>) => onUpdate({ source: { input_mapping: {}, response_path: "", ...src, ...p } as any });
  const [connectors, setConnectors] = useState<ConnectorSummary[] | null>(null);
  const [testInputs, setTestInputs] = useState<Record<string, string>>({});
  const [test, setTest] = useState<{ running?: boolean; result?: any; error?: string } | null>(null);

  useEffect(() => {
    listFormConnectors().then(setConnectors).catch(() => setConnectors([]));
  }, []);

  const self = fieldKey(field);
  const formFields = fields.filter(f => fieldKey(f) !== self);
  const inputFields = formFields.filter(f => !["connector_lookup", "connector_result", "json_display", "table_display"].includes(f.type));
  const mapping = Object.entries(src.input_mapping ?? {});
  const targets = Object.entries(src.target_fields ?? {});
  const record = test?.result !== undefined ? atPath(test.result, src.response_path) : undefined;
  const sample = Array.isArray(record) ? record[0] : record;
  const allPaths = test?.result !== undefined ? pathsOf(test.result) : [];
  const recordPaths = sample !== undefined ? pathsOf(sample).filter(p => p.path) : [];
  const known = (c: string) => connectors?.find(x => x.id === c);

  const runTest = async () => {
    if (!src.connector_id) return;
    setTest({ running: true });
    try {
      const r = await runFormLookup(src.connector_id, {
        input_data: Object.fromEntries(mapping.map(([param]) => [param, testInputs[param] ?? ""])),
        form_id: formId, field_key: self, operation: src.operation,
      });
      setTest(r.ok ? { result: r.result } : { error: r.error || "The connector returned an error" });
    } catch (e: any) {
      setTest({ error: e?.code === "VEL-CONN-FORM-LOOKUP-403"
        ? "Save the form first — a lookup that is not saved can only be tested by an integration administrator."
        : e?.message || "The test failed" });
    }
  };

  return (
    <div>
      <L>Connector</L>
      <select aria-label="Connector" value={src.connector_id ?? ""} onChange={e => set({ connector_id: e.target.value })} style={sel}>
        <option value="">{connectors === null ? "Loading…" : "— choose a connector —"}</option>
        {(connectors ?? []).map(c => <option key={c.id} value={c.id}>{c.name} · {c.connector_type}</option>)}
        {src.connector_id && connectors && !known(src.connector_id) && <option value={src.connector_id}>{src.connector_id} (not found or disabled)</option>}
      </select>
      <OperationPicker connector={src.connector_id ? known(src.connector_id) : undefined}
        value={src.operation} onChange={op => set({ operation: op })} />

      <L top>Send to the connector</L>
      <Hint>Each row: the name the connector expects ← the form field whose value is sent.</Hint>
      {mapping.map(([param, key], i) => (
        <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
          <input aria-label="Parameter" value={param} placeholder="customer_id" style={{ ...sel, flex: 1, fontFamily: "var(--font-mono)" }}
            onChange={e => {
              const next = mapping.map((m, j) => (j === i ? [e.target.value, m[1]] : m));
              set({ input_mapping: Object.fromEntries(next) });
            }} />
          <span style={{ color: "var(--text-muted)", fontSize: 12 }}>←</span>
          <select aria-label="From field" value={key} style={{ ...sel, flex: 1 }}
            onChange={e => set({ input_mapping: { ...src.input_mapping, [param]: e.target.value } })}>
            <option value="">— field —</option>
            {inputFields.map(f => <option key={fieldKey(f)} value={fieldKey(f)}>{f.label || fieldKey(f)}</option>)}
          </select>
          <X onClick={() => set({ input_mapping: Object.fromEntries(mapping.filter((_, j) => j !== i)) })} />
        </div>
      ))}
      <button style={btn} onClick={() => set({ input_mapping: { ...src.input_mapping, [`param_${mapping.length + 1}`]: "" } })}>+ Add an input</button>

      <L top>Test</L>
      {mapping.map(([param]) => (
        <div key={param} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
          <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", minWidth: 80 }}>{param}</span>
          <input aria-label={`Test value ${param}`} value={testInputs[param] ?? ""} style={{ ...sel, flex: 1 }}
            onChange={e => setTestInputs(t => ({ ...t, [param]: e.target.value }))} />
        </div>
      ))}
      <button style={btn} disabled={!src.connector_id || test?.running} onClick={runTest}>
        {test?.running ? "Running…" : "▶ Run a test"}
      </button>
      {test?.error && <div style={{ fontSize: 11, color: "var(--status-failed)", marginTop: 4 }}>{test.error}</div>}
      {test?.result !== undefined && (
        <pre style={{ fontSize: 10, maxHeight: 140, overflow: "auto", background: "var(--bg-elevated)", padding: 6, borderRadius: 4, marginTop: 4 }}>
          {JSON.stringify(test.result, null, 2).slice(0, 4000)}
        </pre>
      )}
      {test?.result !== undefined && (
        <button style={btn} onClick={() => onUpdate({ default_value: test.result })}>
          Keep this result as sample data
        </button>
      )}
      {field.default_value !== undefined && (
        <Hint>Sample data is kept: tables and record details that show this lookup use it in the builder and View Form (never in a case).</Hint>
      )}

      <L top>The record to use</L>
      <select aria-label="Record" value={src.response_path ?? ""} onChange={e => set({ response_path: e.target.value })} style={sel}>
        <option value="">The whole result</option>
        {allPaths.filter(p => p.kind !== "value" && p.path).map(p => (
          <option key={p.path} value={p.path}>{p.path}{p.kind === "list" ? " (list)" : ""}</option>
        ))}
        {src.response_path && !allPaths.some(p => p.path === src.response_path) && <option value={src.response_path}>{src.response_path}</option>}
      </select>
      {(Array.isArray(record) || src.on_many) && (
        <>
          <L top>When several records come back</L>
          <select aria-label="When several" value={src.on_many ?? "first"} onChange={e => set({ on_many: e.target.value as Src["on_many"] })} style={sel}>
            <option value="first">Use the first</option>
            <option value="choose">Let the person choose</option>
            <option value="error">Say there is more than one match</option>
          </select>
        </>
      )}

      <L top>Put into the form</L>
      <Hint>{recordPaths.length ? "Pick from what the test returned." : "Run a test to pick from what comes back, or type a path."}</Hint>
      {targets.map(([path, key], i) => (
        <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4, alignItems: "center" }}>
          {recordPaths.length ? (
            <select aria-label="Result path" value={path} style={{ ...sel, flex: 1 }}
              onChange={e => set({ target_fields: Object.fromEntries(targets.map((t, j) => (j === i ? [e.target.value, t[1]] : t))) })}>
              {!recordPaths.some(p => p.path === path) && <option value={path}>{path || "— value —"}</option>}
              {recordPaths.map(p => <option key={p.path} value={p.path}>{p.path}{p.kind === "list" ? " (list)" : ""}</option>)}
            </select>
          ) : (
            <input aria-label="Result path" value={path} placeholder="name" style={{ ...sel, flex: 1, fontFamily: "var(--font-mono)" }}
              onChange={e => set({ target_fields: Object.fromEntries(targets.map((t, j) => (j === i ? [e.target.value, t[1]] : t))) })} />
          )}
          <span style={{ color: "var(--text-muted)", fontSize: 12 }}>→</span>
          <select aria-label="Into field" value={key} style={{ ...sel, flex: 1 }}
            onChange={e => set({ target_fields: { ...src.target_fields, [path]: e.target.value } })}>
            <option value="">— field —</option>
            {formFields.map(f => <option key={fieldKey(f)} value={fieldKey(f)}>{f.label || fieldKey(f)}</option>)}
          </select>
          <X onClick={() => set({ target_fields: Object.fromEntries(targets.filter((_, j) => j !== i)) })} />
        </div>
      ))}
      <button style={btn} onClick={() => {
        const free = recordPaths.find(p => !(p.path in (src.target_fields ?? {})))?.path ?? `path_${targets.length + 1}`;
        set({ target_fields: { ...src.target_fields, [free]: "" } });
      }}>+ Add a value</button>

      <L top>Runs</L>
      <select aria-label="Runs" value={src.trigger ?? "button"} onChange={e => set({ trigger: e.target.value as Src["trigger"] })} style={sel}>
        <option value="button">When the person clicks the button</option>
        <option value="change">By itself, once every input field is filled</option>
      </select>
      {(src.trigger ?? "button") === "button" && (
        <>
          <L top>Button text</L>
          <input aria-label="Button text" value={src.button_label ?? ""} placeholder="Look up" style={sel}
            onChange={e => set({ button_label: e.target.value || undefined })} />
        </>
      )}
    </div>
  );
}

function L({ children, top }: { children: React.ReactNode; top?: boolean }) {
  return <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-secondary)", margin: `${top ? 12 : 0}px 0 4px` }}>{children}</div>;
}
function Hint({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 4 }}>{children}</div>;
}
function X({ onClick }: { onClick: () => void }) {
  return <button aria-label="Remove" onClick={onClick} style={{ background: "none", border: "none", color: "var(--status-failed)", cursor: "pointer" }}>×</button>;
}
const sel: React.CSSProperties = {
  width: "100%", padding: "5px 6px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", boxSizing: "border-box", minWidth: 0,
};
const btn: React.CSSProperties = {
  padding: "3px 8px", fontSize: 11, background: "transparent", border: "1px dashed var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", cursor: "pointer", marginTop: 2,
};


/** Which of a connector's named operations to call (2026-11-04). Shown only for
 *  a connector that has some; empty = the connector's own request, as before. */
export function OperationPicker({ connector, value, onChange }: {
  connector?: ConnectorSummary; value?: string; onChange: (op: string | undefined) => void;
}) {
  const ops = connector?.operations ?? [];
  if (!ops.length && !value) return null;
  return (
    <>
      <L top>Operation</L>
      <select aria-label="Operation" value={value ?? ""} onChange={e => onChange(e.target.value || undefined)} style={sel}>
        <option value="">The connector's own request</option>
        {ops.map(o => <option key={o.key} value={o.key}>{o.label} · {o.method}</option>)}
        {value && !ops.some(o => o.key === value) && <option value={value}>{value} (no longer on this connector)</option>}
      </select>
    </>
  );
}

/** The operation picker for a connector named only by id (options from a
 *  connector): loads the connector list itself. */
export function ConnectorOperationField({ connectorId, value, onChange }: {
  connectorId?: string; value?: string; onChange: (op: string | undefined) => void;
}) {
  const [connectors, setConnectors] = useState<ConnectorSummary[] | null>(null);
  useEffect(() => { listFormConnectors().then(setConnectors).catch(() => setConnectors([])); }, []);
  const c = connectorId ? connectors?.find(x => x.id === connectorId) : undefined;
  return <OperationPicker connector={c} value={value} onChange={onChange} />;
}
