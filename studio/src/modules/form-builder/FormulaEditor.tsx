import React, { useRef } from "react";
import type { FormFieldDef } from "./FormBuilder";
import { checkExpr, describeExpr, FUNCTIONS, referencePaths, references, refFor } from "./expr";
import { fieldKey } from "./validate";

/* ═══════════════════════════════════════════════════════════════════
   FormulaEditor — one box for every formula on a field (Form Builder v2,
   2026-10-28): a calculation, a "when" condition, a check, an on-change
   value. Fields and functions are inserted by picking, the formula is
   checked as it is typed, and it is read back in words with the fields'
   labels — so the designer never has to leave the field to understand it.
   ═══════════════════════════════════════════════════════════════════ */

export const FUNCTION_HELP: [string, string, string][] = [
  ["sum", "sum(a, b, …)", "Adds numbers; empty ones are skipped"],
  ["round", "round(x, 2)", "Rounds half away from zero"],
  ["if", "if(condition, then, otherwise)", "Picks one of two values"],
  ["coalesce", "coalesce(a, b, …)", "The first value that is filled in"],
  ["years_between", "years_between(date_of_birth, today())", "Full years — an age"],
  ["days_between", "days_between(start, end)", "Days from one date to another"],
  ["add_days", "add_days(date, 30)", "A date some days later"],
  ["today", "today()", "Today's date"],
  ["min", "min(a, b, …)", "The smallest number"],
  ["max", "max(a, b, …)", "The largest number"],
  ["avg", "avg(a, b, …)", "The average"],
  ["count", "count(a, b, …)", "How many are filled in"],
  ["abs", "abs(x)", "Without its sign"],
  ["floor", "floor(x)", "Rounded down"],
  ["ceil", "ceil(x)", "Rounded up"],
  ["concat", "concat(a, \" \", b)", "Joins text"],
  ["upper", "upper(text)", "In capitals"],
  ["lower", "lower(text)", "In small letters"],
  ["trim", "trim(text)", "Without outer spaces"],
  ["len", "len(text)", "Number of characters"],
  ["contains", "contains(text_or_list, x)", "Whether it contains x"],
  ["is_empty", "is_empty(x)", "Whether it is empty"],
  ["number", "number(x)", "Text as a number"],
  ["text", "text(x)", "A number as text"],
];

/** Problems the designer should see: does not parse, names a field that is gone, or itself. */
export function formulaProblems(src: string, fields: FormFieldDef[], self?: string): string[] {
  if (!src?.trim()) return [];
  const err = checkExpr(src);
  if (err) return [err];
  const keys = new Set(fields.map(fieldKey));
  const out: string[] = [];
  for (const k of references(src)) {
    if (k === self) out.push("It uses this field itself.");
    else if (!keys.has(k)) out.push(`“${k}” is not a field on this form — it will be empty.`);
  }
  // A repeating group's column that is not there reads as empty in every row.
  for (const p of referencePaths(src)) {
    const [head, col] = p.split(".");
    const g = fields.find(f => fieldKey(f) === head && f.type === "repeating_group");
    if (g && col && !(g.fields ?? []).some(c => fieldKey(c) === col)) out.push(`“${g.label || head}” has no column “${col}” — it will be empty.`);
  }
  return out;
}

export function labelOf(fields: FormFieldDef[]) {
  return (k: string) => fields.find(f => fieldKey(f) === k)?.label || k;
}

export default function FormulaEditor({ value, onChange, fields, self, placeholder, ariaLabel = "Formula", kind = "value" }: {
  value: string;
  onChange: (v: string) => void;
  /** Every field on the form. */
  fields: FormFieldDef[];
  /** This field's key — it cannot use itself. */
  self?: string;
  placeholder?: string;
  ariaLabel?: string;
  /** "condition" reads back as a yes/no question. */
  kind?: "value" | "condition";
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const insert = (text: string) => {
    const el = ref.current;
    const at = el ? el.selectionStart ?? value.length : value.length;
    const end = el ? el.selectionEnd ?? at : at;
    const before = value.slice(0, at), after = value.slice(end);
    const pad = before && !/[\s(,]$/.test(before) ? " " : "";
    const next = `${before}${pad}${text}${after}`;
    onChange(next);
    requestAnimationFrame(() => {
      if (!el) return;
      const pos = (before + pad + text).length;
      el.focus();
      el.setSelectionRange(pos, pos);
    });
  };
  const others = fields.filter(f => fieldKey(f) && fieldKey(f) !== self);
  // A repeating group's columns, as every row's value: "Items › Amount" → items.amount.
  const picks: [string, string][] = others.flatMap(f => [
    [refFor(fieldKey(f)), f.label || fieldKey(f)] as [string, string],
    ...(f.type === "repeating_group" && Array.isArray(f.fields) && /^[A-Za-z_]\w*$/.test(fieldKey(f))
      ? f.fields.filter(c => /^[A-Za-z_]\w*$/.test(fieldKey(c)))
          .map(c => [`${fieldKey(f)}.${fieldKey(c)}`, `${f.label || fieldKey(f)} › ${c.label || fieldKey(c)}`] as [string, string])
      : []),
  ]);
  const problems = formulaProblems(value, fields, self);
  const words = value.trim() && !checkExpr(value) ? describeExpr(value, labelOf(fields)) : null;

  return (
    <div>
      <textarea ref={ref} aria-label={ariaLabel} value={value} rows={2} spellCheck={false}
        placeholder={placeholder ?? (kind === "condition" ? "e.g. amount > 1000 and country = \"DE\"" : "e.g. quantity * unit_price")}
        onChange={e => onChange(e.target.value)}
        style={{
          width: "100%", boxSizing: "border-box", padding: "6px 8px", fontSize: 12, fontFamily: "var(--font-mono)",
          background: "var(--bg-input)", color: "var(--text-primary)", resize: "vertical",
          border: `1px solid ${problems.length && checkExpr(value) ? "var(--status-failed)" : "var(--border-default)"}`,
          borderRadius: "var(--radius-sm)",
        }} />
      <div style={{ display: "flex", gap: 4, marginTop: 4, flexWrap: "wrap" }}>
        <select aria-label="Insert a field" value="" onChange={e => e.target.value && insert(e.target.value)} style={pick}>
          <option value="">+ Field</option>
          {picks.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <select aria-label="Insert a function" value="" onChange={e => {
          const fn = e.target.value;
          if (!fn) return;
          insert(FUNCTIONS[fn][0] === 0 ? `${fn}()` : `${fn}(`);
        }} style={pick}>
          <option value="">+ Function</option>
          {FUNCTION_HELP.map(([fn, sig, help]) => <option key={fn} value={fn} title={help}>{sig} — {help}</option>)}
        </select>
        {["+", "−", "×", "÷", "=", ">", "<", "and", "or"].map(op => (
          <button key={op} type="button" onClick={() => insert(op === "−" ? "-" : op)} style={chip}>{op}</button>
        ))}
      </div>
      {words && (
        <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 6 }}>
          <span style={{ color: "var(--text-muted)" }}>{kind === "condition" ? "When " : "= "}</span>{words}
        </div>
      )}
      {problems.map(p => (
        <div key={p} role="alert" style={{ fontSize: 11, color: checkExpr(value) ? "var(--status-failed)" : "var(--status-warning, #d97706)", marginTop: 4 }}>{p}</div>
      ))}
    </div>
  );
}

const pick: React.CSSProperties = {
  padding: "2px 4px", fontSize: 11, background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", maxWidth: 150,
};
const chip: React.CSSProperties = {
  padding: "1px 7px", fontSize: 11, background: "transparent", border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", cursor: "pointer", fontFamily: "var(--font-mono)",
};
