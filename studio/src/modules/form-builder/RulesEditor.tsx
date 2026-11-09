import React from "react";
import type { FormFieldDef } from "./FormBuilder";
import { fieldKey } from "./validate";
import FormulaEditor from "./FormulaEditor";
import { references, refFor } from "./expr";
import { codeOptions } from "./SmallControls";

/* ═══════════════════════════════════════════════════════════════════
   RulesEditor — "show this field only when …", built from pickers
   (Form Builder redesign part 5, 2026-10-28).

   Writes `show_when: {match, rules: [{field, op, value}]}`, which the
   renderer and the server (velaris_shared/forms/validate.py) evaluate:
   a field that is not shown is not required and not submitted.
   ═══════════════════════════════════════════════════════════════════ */

type Rule = { field: string; op: string; value?: any };
export type When = { match?: "all" | "any"; rules?: Rule[]; expression?: string };

/** Every field a field's own logic reads: its rules, conditions and formulas. */
export function fieldDeps(f: any): string[] {
  const out = new Set<string>();
  for (const prop of ["show_when", "required_when", "read_only_when"]) {
    const w = f?.[prop];
    for (const r of (w?.rules ?? []) as Rule[]) if (r?.field) out.add(r.field);
    if (typeof w?.expression === "string") references(w.expression).forEach(k => out.add(k));
  }
  if (typeof f?.calculation === "string") references(f.calculation).forEach(k => out.add(k));
  return [...out];
}

const OP_LABEL: Record<string, string> = {
  equals: "is", not_equals: "is not", in: "is one of",
  gt: "is more than", lt: "is less than", before: "is before", after: "is after",
  is_empty: "is empty", not_empty: "is filled in",
};

/** Fields whose value is true / false. */
const BOOLEAN_TYPES = new Set(["checkbox", "toggle", "consent"]);

/** The operators that make sense for a field of this type. */
export function opsFor(type: string): string[] {
  switch (type) {
    case "number": case "currency": case "slider": case "rating": case "percentage":
      return ["equals", "not_equals", "gt", "lt", "is_empty", "not_empty"];
    case "date": case "datetime":
      return ["before", "after", "equals", "is_empty", "not_empty"];
    case "dropdown": case "radio": case "country": case "currency_code":
      return ["equals", "not_equals", "in", "is_empty", "not_empty"];
    case "multi_select":
      return ["equals", "in", "is_empty", "not_empty"];
    case "checkbox": case "toggle": case "consent":
      return ["equals", "not_equals"];
    case "file_upload": case "location": case "tags": case "date_range":
    case "address": case "signature": case "user_picker": case "case_picker": case "document_picker": case "rich_text":
      return ["is_empty", "not_empty"];
    default:
      return ["equals", "not_equals", "is_empty", "not_empty"];
  }
}

function opLabel(op: string, type: string): string {
  if (type === "multi_select") return op === "equals" ? "includes" : op === "in" ? "includes any of" : OP_LABEL[op];
  return OP_LABEL[op] ?? op;
}

/** Does `from` (directly or through others' rules and formulas) depend on `target`? */
export function dependsOn(fields: FormFieldDef[], from: string, target: string, seen = new Set<string>()): boolean {
  if (from === target) return true;
  if (seen.has(from)) return false;
  seen.add(from);
  const f = fields.find(x => fieldKey(x) === from);
  return fieldDeps(f).some(k => dependsOn(fields, k, target, seen));
}

/** Problems a designer should see: a rule on a field that is gone, or a loop. */
export function ruleProblems(field: FormFieldDef, fields: FormFieldDef[], prop: string = "show_when"): string[] {
  const out: string[] = [];
  const self = fieldKey(field);
  for (const r of ((field as any)[prop]?.rules ?? []) as Rule[]) {
    if (!r.field) continue;
    if (!fields.some(f => fieldKey(f) === r.field)) out.push(`A rule uses "${r.field}", which is no longer on the form.`);
    else if (dependsOn(fields, r.field, self)) out.push(`A rule uses "${r.field}", whose own rules depend on this field — the rules go round in a circle.`);
  }
  const expr = (field as any)[prop]?.expression;
  if (typeof expr === "string") {
    for (const k of references(expr)) {
      if (k !== self && fields.some(f => fieldKey(f) === k) && dependsOn(fields, k, self))
        out.push(`The formula uses "${k}", whose own logic depends on this field — it goes round in a circle.`);
    }
  }
  return out;
}

const DISPLAY = new Set(["connector_lookup", "connector_result", "json_display", "table_display", "record_details"]);

export default function RulesEditor({ field, fields, onUpdate, prop = "show_when", lead = "Show this field", always = "always", addLabel = "+ Show only when…" }: {
  field: FormFieldDef;
  /** Every field on the form, this one included. */
  fields: FormFieldDef[];
  onUpdate: (p: Partial<FormFieldDef>) => void;
  /** Which condition this edits (Form Builder v2): show, required or read-only. */
  prop?: "show_when" | "required_when" | "read_only_when";
  lead?: string;
  always?: string;
  addLabel?: string;
}) {
  const self = fieldKey(field);
  const when: When | undefined = (field as any)[prop];
  const legacy: any[] = prop === "show_when" && Array.isArray((field as any).conditions) ? (field as any).conditions : [];
  const candidates = fields.filter(f => fieldKey(f) !== self && !DISPLAY.has(f.type));
  const byKey = (k: string) => fields.find(f => fieldKey(f) === k);
  const set = (w: When | undefined) => onUpdate({ [prop]: w as any, ...(legacy.length ? { conditions: undefined } : {}) } as any);

  const newRule = (): Rule => {
    const first = candidates[0];
    return { field: first ? fieldKey(first) : "", op: first ? opsFor(first.type)[0] : "equals",
             ...(BOOLEAN_TYPES.has(first?.type ?? "") ? { value: true } : {}) };
  };
  const setRule = (i: number, p: Partial<Rule>) => {
    const rules = [...(when?.rules ?? [])];
    rules[i] = { ...rules[i], ...p };
    set({ match: when?.match ?? "all", rules });
  };

  // Advanced: the condition as a formula (Form Builder v2).
  if (typeof when?.expression === "string") {
    return (
      <div>
        <div style={{ fontSize: 12, color: "var(--text-primary)", marginBottom: 6 }}>{lead} only when this formula is true:</div>
        <FormulaEditor kind="condition" value={when.expression} fields={fields} self={self} ariaLabel={`${lead} formula`}
          onChange={expression => set({ expression })} />
        {ruleProblems(field, fields, prop).map(p => (
          <div key={p} style={{ fontSize: 11, color: "var(--status-warning, #d97706)", marginTop: 4 }}>{p}</div>
        ))}
        <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
          <button onClick={() => set(undefined)} style={btn}>Remove</button>
          <button onClick={() => set({ match: "all", rules: [newRule()] })} style={{ ...btn, border: "none" }}>Use simple conditions</button>
        </div>
      </div>
    );
  }

  if (!when?.rules?.length) {
    return (
      <div>
        <div style={{ fontSize: 12, color: "var(--text-primary)", marginBottom: 6 }}>{lead}: <b>{always}</b></div>
        {legacy.length > 0 && (
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
            This field has {legacy.length} rule(s) in the old format; they still apply. Adding a rule here replaces them.
          </div>
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button disabled={!candidates.length} onClick={() => set({ match: "all", rules: [newRule()] })} style={btn}>
            {addLabel}
          </button>
          <button onClick={() => set({ expression: "" })} style={{ ...btn, border: "none" }} title="For anything the pickers cannot say">
            ƒ formula
          </button>
        </div>
        {!candidates.length && <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>Add another field first.</div>}
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--text-primary)", marginBottom: 8, flexWrap: "wrap" }}>
        {lead} only when
        <select aria-label="Match" value={when.match ?? "all"} onChange={e => set({ ...when, match: e.target.value as "all" | "any" })} style={sel}>
          <option value="all">all</option>
          <option value="any">any</option>
        </select>
        of these are true:
      </div>
      {(when.rules ?? []).map((rule, i) => {
        const src = byKey(rule.field);
        const type = src?.type ?? "text";
        const ops = opsFor(type);
        const needsValue = !["is_empty", "not_empty"].includes(rule.op);
        return (
          <div key={i} style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", padding: 8, marginBottom: 6 }}>
            <div style={{ display: "flex", gap: 4, marginBottom: 4 }}>
              <select aria-label="Field" value={rule.field} style={{ ...sel, flex: 1 }} onChange={e => {
                const f = byKey(e.target.value);
                const op = f ? (opsFor(f.type).includes(rule.op) ? rule.op : opsFor(f.type)[0]) : rule.op;
                setRule(i, { field: e.target.value, op, value: BOOLEAN_TYPES.has(f?.type ?? "") ? true : undefined });
              }}>
                {!src && <option value={rule.field}>{rule.field || "— choose a field —"} (missing)</option>}
                {candidates.map(f => <option key={fieldKey(f)} value={fieldKey(f)}>{f.label || fieldKey(f)}</option>)}
              </select>
              <button aria-label="Remove rule" onClick={() => {
                const rules = (when.rules ?? []).filter((_, j) => j !== i);
                set(rules.length ? { ...when, rules } : undefined);
              }} style={{ ...btn, border: "none", color: "var(--status-failed)" }}>×</button>
            </div>
            <div style={{ display: "flex", gap: 4 }}>
              <select aria-label="Condition" value={rule.op} style={{ ...sel, flex: "0 0 auto" }}
                onChange={e => setRule(i, { op: e.target.value, ...(["is_empty", "not_empty"].includes(e.target.value) ? { value: undefined } : {}) })}>
                {ops.map(op => <option key={op} value={op}>{opLabel(op, type)}</option>)}
              </select>
              {needsValue && <RuleValue source={src} rule={rule} onChange={value => setRule(i, { value })} />}
            </div>
          </div>
        );
      })}
      <div style={{ display: "flex", gap: 8 }}>
        <button onClick={() => set({ ...when, rules: [...(when.rules ?? []), newRule()] })} style={btn}>+ Add a condition</button>
        <button onClick={() => set({ expression: whenToFormula(when) })} style={{ ...btn, border: "none" }}
          title="Turn these conditions into a formula you can extend">ƒ as a formula</button>
      </div>
      {ruleProblems(field, fields, prop).map(p => (
        <div key={p} style={{ fontSize: 11, color: "var(--status-warning, #d97706)", marginTop: 4 }}>{p}</div>
      ))}
    </div>
  );
}

/** The picked conditions as the same formula — so "as a formula" keeps them. */
export function whenToFormula(when: When): string {
  const lit = (v: any) => typeof v === "number" || typeof v === "boolean" ? String(v) : `"${String(v ?? "").replace(/"/g, '""')}"`;
  const one = (r: Rule): string => {
    const f = refFor(r.field);
    switch (r.op) {
      case "equals": return `${f} = ${lit(r.value)}`;
      case "not_equals": return `${f} != ${lit(r.value)}`;
      case "in": return `(${(Array.isArray(r.value) ? r.value : [r.value]).map(v => `${f} = ${lit(v)}`).join(" or ") || "false"})`;
      case "is_empty": return `is_empty(${f})`;
      case "not_empty": return `not is_empty(${f})`;
      case "gt": case "after": return `${f} > ${lit(r.value)}`;
      case "lt": case "before": return `${f} < ${lit(r.value)}`;
    }
    return "false";
  };
  return (when.rules ?? []).map(one).join(when.match === "any" ? " or " : " and ");
}

/** The value box, typed by the field the rule looks at. */
function RuleValue({ source, rule, onChange }: { source?: FormFieldDef; rule: Rule; onChange: (v: any) => void }) {
  const type = source?.type ?? "text";
  const options = source?.type === "country" || source?.type === "currency_code" ? codeOptions(source.type)
    : (source?.options || []).map((o: any) => (typeof o === "string" ? { label: o, value: o } : o));
  if (BOOLEAN_TYPES.has(type)) {
    const [yes, no] = type === "toggle" ? ["yes", "no"] : type === "consent" ? ["agreed", "not agreed"] : ["ticked", "not ticked"];
    return (
      <select aria-label="Value" value={rule.value === false ? "false" : "true"} onChange={e => onChange(e.target.value === "true")} style={{ ...sel, flex: 1 }}>
        <option value="true">{yes}</option>
        <option value="false">{no}</option>
      </select>
    );
  }
  if (options.length && rule.op === "in") {
    const chosen: string[] = Array.isArray(rule.value) ? rule.value : [];
    return (
      <div style={{ flex: 1, display: "flex", flexWrap: "wrap", gap: 6 }}>
        {options.map((o: any) => (
          <label key={o.value} style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 3 }}>
            <input type="checkbox" checked={chosen.includes(o.value)}
              onChange={e => onChange(e.target.checked ? [...chosen, o.value] : chosen.filter(v => v !== o.value))} />
            {o.label}
          </label>
        ))}
      </div>
    );
  }
  if (options.length) {
    return (
      <select aria-label="Value" value={rule.value ?? ""} onChange={e => onChange(e.target.value)} style={{ ...sel, flex: 1 }}>
        <option value="">— choose —</option>
        {options.map((o: any) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    );
  }
  if (type === "date" || type === "datetime") {
    return <input aria-label="Value" type="date" value={rule.value ?? ""} onChange={e => onChange(e.target.value)} style={{ ...sel, flex: 1 }} />;
  }
  if (["number", "currency", "slider", "rating", "percentage"].includes(type)) {
    return <input aria-label="Value" type="number" value={rule.value ?? ""}
      onChange={e => onChange(e.target.value === "" ? undefined : Number(e.target.value))} style={{ ...sel, flex: 1 }} />;
  }
  return <input aria-label="Value" value={rule.value ?? ""} onChange={e => onChange(e.target.value)} style={{ ...sel, flex: 1 }} />;
}

const sel: React.CSSProperties = {
  padding: "4px 6px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", minWidth: 0,
};
const btn: React.CSSProperties = {
  padding: "3px 8px", fontSize: 11, background: "transparent", border: "1px dashed var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", cursor: "pointer",
};
