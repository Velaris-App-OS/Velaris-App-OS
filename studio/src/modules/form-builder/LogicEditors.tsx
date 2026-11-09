import React, { useState } from "react";
import type { FormFieldDef } from "./FormBuilder";
import FormulaEditor, { labelOf } from "./FormulaEditor";
import RulesEditor, { whenToFormula, type When } from "./RulesEditor";
import { describeExpr, refFor } from "./expr";
import { fieldKey } from "./validate";

/* ═══════════════════════════════════════════════════════════════════
   Everything a field does, set where the field is (Form Builder v2,
   2026-10-28). Pega spreads one field over a declare expression, a when
   rule, a validate rule and an action set, each its own rule to open;
   here they are four blocks of one panel, each read back in words, with
   the canvas preview recomputing as they change.
   ═══════════════════════════════════════════════════════════════════ */

type Update = (p: Partial<FormFieldDef>) => void;
type F = FormFieldDef & Record<string, any>;

const NUMERIC = new Set(["number", "currency", "slider", "rating", "percentage"]);
const DATES = new Set(["date", "datetime"]);
const INPUTS = (fields: FormFieldDef[], self: string) =>
  fields.filter(f => fieldKey(f) !== self && !["connector_lookup", "connector_result", "json_display", "table_display", "record_details",
    "heading", "paragraph", "divider", "callout", "embed", "button"].includes(f.type));

/* ── the field in words ────────────────────────────────────────── */

function whenWords(w: When | undefined, fields: FormFieldDef[]): string | null {
  if (!w) return null;
  const src = typeof w.expression === "string" ? w.expression : w.rules?.length ? whenToFormula(w) : "";
  return src.trim() ? describeExpr(src, labelOf(fields)) ?? src : null;
}

/** Plain sentences for everything the field does — shown above the tabs. */
export function fieldSentences(field: FormFieldDef, fields: FormFieldDef[]): string[] {
  const f = field as F;
  const out: string[] = [];
  const label = labelOf(fields);
  if (f.type === "repeating_group") {
    const cols = (Array.isArray(f.fields) ? f.fields : []).map((c: any) => c.label || c.field_key);
    const lim = [typeof f.min_rows === "number" && `at least ${f.min_rows}`, typeof f.max_rows === "number" && `at most ${f.max_rows}`].filter(Boolean);
    out.push(`Rows of ${cols.length ? cols.join(", ") : "no columns yet"}${lim.length ? ` — ${lim.join(", ")} rows` : ""}.`);
  }
  if (typeof f.calculation === "string" && f.calculation.trim())
    out.push(`Calculated: ${describeExpr(f.calculation, label) ?? f.calculation}.`);
  else if (f.variable) out.push(`Starts with the case value “${f.variable}”.`);
  const src = f.options_source;
  if (src?.kind === "reference") {
    const filt = (src.filters ?? []).filter((x: any) => x?.field).map((x: any) => `${x.column} is ${label(x.field)}`);
    const fill = (src.fill ?? []).filter((x: any) => x?.field).map((x: any) => label(x.field));
    out.push(`Options from a reference table${filt.length ? `, only rows where ${filt.join(" and ")}` : ""}${fill.length ? `; fills ${fill.join(", ")}` : ""}.`);
  }
  if (f.hidden) out.push("Hidden from the person.");
  else {
    const show = whenWords(f.show_when, fields);
    if (show) out.push(`Shown only when ${show}.`);
    else if (f.conditions?.length) out.push("Shown only when its older-format rules hold.");
  }
  if (f.required) out.push("Required.");
  else { const r = whenWords(f.required_when, fields); if (r) out.push(`Required when ${r}.`); }
  if (f.read_only) out.push("Read-only.");
  else { const r = whenWords(f.read_only_when, fields); if (r) out.push(`Read-only when ${r}.`); }
  for (const c of (Array.isArray(f.checks) ? f.checks : [])) {
    if (c?.expression?.trim()) out.push(`Must pass: ${describeExpr(c.expression, label) ?? c.expression}.`);
  }
  for (const a of (Array.isArray(f.on_change) ? f.on_change : [])) {
    if (!a?.target) continue;
    out.push(a.action === "clear"
      ? `When changed, clears ${label(a.target)}.`
      : `When changed, sets ${label(a.target)} to ${describeExpr(a.expression ?? "", label) ?? "…"}.`);
  }
  return out;
}

export function FieldSummary({ field, fields }: { field: FormFieldDef; fields: FormFieldDef[] }) {
  const lines = fieldSentences(field, fields);
  if (!lines.length) return null;
  return (
    <div aria-label="What this field does" style={{
      margin: "8px 12px 0", padding: "6px 8px", fontSize: 11, lineHeight: 1.5, color: "var(--text-secondary)",
      background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)",
    }}>
      {lines.map(l => <div key={l}>{l}</div>)}
    </div>
  );
}

/* ── Value: typed in, or calculated ────────────────────────────── */

type Preset = "sum" | "multiply" | "subtract" | "percent" | "age" | "days" | "join";
const PRESETS: [Preset, string][] = [
  ["sum", "Add up fields"], ["multiply", "Multiply two fields"], ["subtract", "Subtract one from another"],
  ["percent", "A percentage of a field"], ["age", "Age from a date"], ["days", "Days between two dates"],
  ["join", "Join text fields"],
];

export function ValueEditor({ field, fields, onUpdate, children }: {
  field: FormFieldDef; fields: FormFieldDef[]; onUpdate: Update;
  /** The default-value input, shown when the value is typed in. */
  children?: React.ReactNode;
}) {
  const f = field as F;
  const self = fieldKey(field);
  const calculated = typeof f.calculation === "string";
  // Open while there is nothing to read back yet, and while being typed in.
  const [formula, setFormula] = useState(() => !f.calculation);
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>Value</div>
      <div role="radiogroup" aria-label="Value" style={{ display: "flex", gap: 4, marginBottom: 6 }}>
        {[["typed", "Typed in"], ["calc", "Calculated"]].map(([k, l]) => {
          const on = (k === "calc") === calculated;
          return (
            <button key={k} role="radio" aria-checked={on} type="button"
              onClick={() => onUpdate(k === "calc"
                ? { calculation: f.calculation ?? "", default_value: undefined } as any
                : { calculation: undefined } as any)}
              style={{ ...seg, ...(on ? segOn : {}) }}>{l}</button>
          );
        })}
      </div>
      {!calculated && children}
      {calculated && (
        <>
          <PresetBuilder field={field} fields={fields} onApply={calculation => { onUpdate({ calculation } as any); setFormula(false); }} />
          <button type="button" onClick={() => setFormula(x => !x)} style={link}>{formula ? "▾" : "▸"} Formula</button>
          {formula ? (
            <FormulaEditor value={f.calculation ?? ""} fields={fields} self={self} ariaLabel="Calculation"
              onChange={calculation => onUpdate({ calculation } as any)} />
          ) : (
            <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>= {describeExpr(f.calculation, labelOf(fields)) ?? f.calculation}</div>
          )}
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 6 }}>
            Recalculated as the form is filled in, and again by the server on submit — a value sent by the browser is ignored.
          </div>
        </>
      )}
    </div>
  );
}

function PresetBuilder({ field, fields, onApply }: { field: FormFieldDef; fields: FormFieldDef[]; onApply: (src: string) => void }) {
  const self = fieldKey(field);
  const inputs = INPUTS(fields, self);
  const nums = inputs.filter(f => NUMERIC.has(f.type));
  const dates = inputs.filter(f => DATES.has(f.type));
  const [preset, setPreset] = useState<Preset | "">("");
  const [picked, setPicked] = useState<string[]>([]);
  const [a, setA] = useState(""), [b, setB] = useState(""), [n, setN] = useState("19");
  const pool = preset === "age" || preset === "days" ? dates : preset === "join" ? inputs : nums;
  const one = (val: string, set: (v: string) => void, label: string) => (
    <select aria-label={label} value={val} onChange={e => set(e.target.value)} style={sel}>
      <option value="">{label}…</option>
      {pool.map(f => <option key={fieldKey(f)} value={fieldKey(f)}>{f.label || fieldKey(f)}</option>)}
    </select>
  );
  const build = (): string | null => {
    const r = refFor;
    switch (preset) {
      case "sum": return picked.length ? `sum(${picked.map(r).join(", ")})` : null;
      case "join": return picked.length ? `concat(${picked.map(r).join(', " ", ')})` : null;
      case "multiply": return a && b ? `${r(a)} * ${r(b)}` : null;
      case "subtract": return a && b ? `${r(a)} - ${r(b)}` : null;
      case "percent": return a && n.trim() && !isNaN(Number(n)) ? `round(${r(a)} * ${Number(n)} / 100, 2)` : null;
      case "age": return a ? `years_between(${r(a)}, today())` : null;
      case "days": return a && b ? `days_between(${r(a)}, ${r(b)})` : null;
    }
    return null;
  };
  const src = build();
  return (
    <div style={{ border: "1px dashed var(--border-default)", borderRadius: "var(--radius-sm)", padding: 8, marginBottom: 6 }}>
      <select aria-label="Calculation type" value={preset} onChange={e => { setPreset(e.target.value as Preset); setPicked([]); setA(""); setB(""); }} style={{ ...sel, width: "100%" }}>
        <option value="">Build a calculation…</option>
        {PRESETS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
      </select>
      {(preset === "sum" || preset === "join") && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 6 }}>
          {pool.length === 0 && <span style={{ fontSize: 11, color: "var(--text-muted)" }}>No {preset === "sum" ? "number" : ""} fields on the form yet.</span>}
          {pool.map(f => (
            <label key={fieldKey(f)} style={{ fontSize: 11, display: "flex", gap: 3, alignItems: "center" }}>
              <input type="checkbox" checked={picked.includes(fieldKey(f))}
                onChange={e => setPicked(p => e.target.checked ? [...p, fieldKey(f)] : p.filter(x => x !== fieldKey(f)))} />
              {f.label || fieldKey(f)}
            </label>
          ))}
        </div>
      )}
      {(preset === "multiply" || preset === "subtract" || preset === "days") && (
        <div style={{ display: "flex", gap: 4, marginTop: 6, alignItems: "center" }}>
          {one(a, setA, preset === "days" ? "From" : "First")}
          <span style={{ fontSize: 11 }}>{preset === "multiply" ? "×" : preset === "subtract" ? "−" : "to"}</span>
          {one(b, setB, preset === "days" ? "To" : "Second")}
        </div>
      )}
      {(preset === "percent" || preset === "age") && (
        <div style={{ display: "flex", gap: 4, marginTop: 6, alignItems: "center" }}>
          {preset === "percent" && (<><input aria-label="Percent" value={n} onChange={e => setN(e.target.value)} style={{ ...sel, width: 50 }} /><span style={{ fontSize: 11 }}>% of</span></>)}
          {one(a, setA, preset === "age" ? "Date of birth" : "Field")}
        </div>
      )}
      {preset && (
        <button type="button" disabled={!src} onClick={() => src && onApply(src)} style={{ ...dashed, marginTop: 6 }}>
          Use this calculation
        </button>
      )}
    </div>
  );
}

/* ── Behaviour: shown, required, read-only, on change ──────────── */

function TriState({ label, on, when, onSet, children }: {
  label: string; on: boolean; when: boolean; onSet: (v: "no" | "yes" | "when") => void; children: React.ReactNode;
}) {
  const value = on ? "yes" : when ? "when" : "no";
  return (
    <div style={{ borderTop: "1px solid var(--border-subtle)", marginTop: 10, paddingTop: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
        <span style={{ fontSize: 12, color: "var(--text-primary)", minWidth: 70 }}>{label}</span>
        <div role="radiogroup" aria-label={label} style={{ display: "flex", gap: 2 }}>
          {[["no", "No"], ["yes", "Yes"], ["when", "Only when…"]].map(([k, l]) => (
            <button key={k} role="radio" aria-checked={value === k} type="button" onClick={() => onSet(k as any)}
              style={{ ...seg, ...(value === k ? segOn : {}) }}>{l}</button>
          ))}
        </div>
      </div>
      {value === "when" && children}
    </div>
  );
}

export function BehaviourEditor({ field, fields, onUpdate }: { field: FormFieldDef; fields: FormFieldDef[]; onUpdate: Update }) {
  const f = field as F;
  const calculated = typeof f.calculation === "string";
  const startWhen = (prop: string) => ({ [prop]: { match: "all", rules: [] } });
  return (
    <>
      {!f.hidden && (
        <RulesEditor field={field} fields={fields} onUpdate={onUpdate} />
      )}
      {!calculated && !f.hidden && (
        <TriState label="Required" on={!!f.required} when={!!f.required_when}
          onSet={v => onUpdate({ required: v === "yes", required_when: v === "when" ? (f.required_when ?? startWhen("x").x) : undefined } as any)}>
          <RulesEditor field={field} fields={fields} onUpdate={onUpdate} prop="required_when"
            lead="Required" always="not yet — add a condition" addLabel="+ Required when…" />
        </TriState>
      )}
      {!calculated && !f.hidden && (
        <TriState label="Read-only" on={!!f.read_only} when={!!f.read_only_when}
          onSet={v => onUpdate({ read_only: v === "yes" || undefined, read_only_when: v === "when" ? (f.read_only_when ?? startWhen("x").x) : undefined } as any)}>
          <RulesEditor field={field} fields={fields} onUpdate={onUpdate} prop="read_only_when"
            lead="Read-only" always="not yet — add a condition" addLabel="+ Read-only when…" />
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
            While read-only, what the person typed is not saved — the case's own value stays.
          </div>
        </TriState>
      )}
      {!calculated && <OnChangeEditor field={field} fields={fields} onUpdate={onUpdate} />}
    </>
  );
}

function OnChangeEditor({ field, fields, onUpdate }: { field: FormFieldDef; fields: FormFieldDef[]; onUpdate: Update }) {
  const f = field as F;
  const self = fieldKey(field);
  const actions: any[] = Array.isArray(f.on_change) ? f.on_change : [];
  // A consent is never set for the person (2026-10-29).
  const targets = INPUTS(fields, self).filter(t => typeof (t as any).calculation !== "string" && t.type !== "consent");
  const set = (next: any[]) => onUpdate({ on_change: next.length ? next : undefined } as any);
  const at = (i: number, p: any) => set(actions.map((a, j) => (j === i ? { ...a, ...p } : a)));
  return (
    <div style={{ borderTop: "1px solid var(--border-subtle)", marginTop: 10, paddingTop: 10 }}>
      <div style={{ fontSize: 12, color: "var(--text-primary)", marginBottom: 6 }}>When the person changes it</div>
      {actions.map((a, i) => (
        <div key={i} style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", padding: 8, marginBottom: 6 }}>
          <div style={{ display: "flex", gap: 4, alignItems: "center", marginBottom: a.action === "set" ? 6 : 0 }}>
            <select aria-label="Action" value={a.action} onChange={e => at(i, { action: e.target.value })} style={sel}>
              <option value="set">Set</option>
              <option value="clear">Clear</option>
            </select>
            <select aria-label="Target field" value={a.target ?? ""} onChange={e => at(i, { target: e.target.value })} style={{ ...sel, flex: 1 }}>
              <option value="">— field —</option>
              {targets.map(t => <option key={fieldKey(t)} value={fieldKey(t)}>{t.label || fieldKey(t)}</option>)}
            </select>
            {a.action === "set" && <span style={{ fontSize: 11 }}>to</span>}
            <button aria-label="Remove action" onClick={() => set(actions.filter((_, j) => j !== i))}
              style={{ ...dashed, border: "none", color: "var(--status-failed)" }}>×</button>
          </div>
          {a.action === "set" && (
            <FormulaEditor value={a.expression ?? ""} fields={fields} ariaLabel="Set to" placeholder={`e.g. ${refFor(self)} * 2`}
              onChange={expression => at(i, { expression })} />
          )}
        </div>
      ))}
      <button type="button" disabled={!targets.length} onClick={() => set([...actions, { action: "set", target: "", expression: "" }])} style={dashed}>
        + Set or clear another field
      </button>
      <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
        A convenience: the person can still change that field afterwards. For a value they must not change, make it Calculated.
      </div>
    </div>
  );
}

/* ── Checks against other fields ───────────────────────────────── */

export function ChecksEditor({ field, fields, onUpdate }: { field: FormFieldDef; fields: FormFieldDef[]; onUpdate: Update }) {
  const f = field as F;
  const self = fieldKey(field);
  const checks: any[] = Array.isArray(f.checks) ? f.checks : [];
  const set = (next: any[]) => onUpdate({ checks: next.length ? next : undefined } as any);
  const at = (i: number, p: any) => set(checks.map((c, j) => (j === i ? { ...c, ...p } : c)));
  return (
    <div style={{ borderTop: "1px solid var(--border-subtle)", marginTop: 10, paddingTop: 10 }}>
      <div style={{ fontSize: 12, color: "var(--text-primary)", marginBottom: 2 }}>Checks against other fields</div>
      <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 6 }}>
        Each must be true. Run once every field it names is filled in — in the form and again by the server.
      </div>
      {checks.map((c, i) => (
        <div key={i} style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", padding: 8, marginBottom: 6 }}>
          <FormulaEditor kind="condition" value={c.expression ?? ""} fields={fields} ariaLabel="Check"
            placeholder={`e.g. ${refFor(self)} >= start_date`} onChange={expression => at(i, { expression })} />
          <div style={{ display: "flex", gap: 4, marginTop: 6 }}>
            <input aria-label="Message" value={c.message ?? ""} placeholder="Message when it fails"
              onChange={e => at(i, { message: e.target.value })} style={{ ...sel, flex: 1 }} />
            <button aria-label="Remove check" onClick={() => set(checks.filter((_, j) => j !== i))}
              style={{ ...dashed, border: "none", color: "var(--status-failed)" }}>×</button>
          </div>
        </div>
      ))}
      <button type="button" onClick={() => set([...checks, { expression: "", message: "" }])} style={dashed}>+ Add a check</button>
    </div>
  );
}

const sel: React.CSSProperties = {
  padding: "4px 6px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", minWidth: 0,
};
const dashed: React.CSSProperties = {
  padding: "3px 8px", fontSize: 11, background: "transparent", border: "1px dashed var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", cursor: "pointer",
};
const link: React.CSSProperties = {
  background: "none", border: "none", padding: "4px 0", fontSize: 11, color: "var(--text-muted)", cursor: "pointer",
};
const seg: React.CSSProperties = {
  padding: "3px 8px", fontSize: 11, background: "transparent", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", cursor: "pointer",
};
const segOn: React.CSSProperties = { background: "var(--accent)", borderColor: "var(--accent)", color: "#fff" };
