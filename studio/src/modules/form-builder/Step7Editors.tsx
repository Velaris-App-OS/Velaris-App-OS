/**
 * Form Builder v2 §5 step 7 (2026-10-29), the builder side:
 *  - SecurityEditor   — who sees a field and who may change it (access roles);
 *  - TranslationsPanel — the form's text per language;
 *  - TestsPanel       — sample answers with what should happen, run by the server.
 * The server enforces security and runs the tests; nothing here is trusted.
 */
import React, { useEffect, useState } from "react";
import type { FormDefinition, FormFieldDef } from "./FormBuilder";
import { STATIC_TYPES, fieldKey, validateForm } from "./validate";
import { languageName, languages, type LanguageTexts, type Translations } from "./translate";
import { getFormAccessRoles, runFormTests, type FormTestResult } from "@shared/api/client";

const input: React.CSSProperties = {
  width: "100%", boxSizing: "border-box", padding: "5px 8px", fontSize: 12, background: "var(--bg-input)",
  border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)",
};
const small: React.CSSProperties = { fontSize: 10, color: "var(--text-muted)", marginTop: 3 };
const label: React.CSSProperties = { fontSize: 11, color: "var(--text-secondary)", marginBottom: 3 };
const chip = (on: boolean): React.CSSProperties => ({
  padding: "2px 8px", fontSize: 11, borderRadius: 10, cursor: "pointer",
  border: `1px solid ${on ? "var(--accent)" : "var(--border-default)"}`,
  background: on ? "var(--accent)" : "transparent", color: on ? "#fff" : "var(--text-secondary)",
});
const link: React.CSSProperties = { background: "none", border: "none", padding: 0, color: "var(--accent)", cursor: "pointer", fontSize: 11 };

/** Every field with a value, a group's columns as `group.column`. */
function valueFields(def: FormDefinition): { key: string; field: FormFieldDef; group?: string }[] {
  const out: { key: string; field: FormFieldDef; group?: string }[] = [];
  for (const s of def.sections || []) for (const f of s.fields || []) {
    if (STATIC_TYPES.has(f.type)) continue;
    out.push({ key: fieldKey(f), field: f });
    for (const c of (f as any).fields || []) out.push({ key: `${fieldKey(f)}.${fieldKey(c)}`, field: c, group: f.label });
  }
  return out;
}

function Card({ title, summary, children }: { title: string; summary: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", marginBottom: "var(--space-md)", background: "var(--bg-card)" }}>
      <button onClick={() => setOpen(o => !o)} aria-expanded={open} style={{
        width: "100%", textAlign: "left", background: "none", border: "none", padding: "8px 12px", cursor: "pointer",
        fontSize: 12, fontWeight: 600, color: "var(--text-primary)",
      }}>{open ? "▾" : "▸"} {title}
        <span style={{ fontWeight: 400, color: "var(--text-muted)", marginLeft: 6 }}>{summary}</span>
      </button>
      {open && <div style={{ padding: "0 12px 12px", fontSize: 12 }}>{children}</div>}
    </div>
  );
}

/* ── Field-level security ───────────────────────────────────────── */

let rolesCache: Promise<{ id: string; name: string }[]> | null = null;

export function SecurityEditor({ field, onUpdate }: { field: FormFieldDef; onUpdate: (p: Partial<FormFieldDef>) => void }) {
  const [roles, setRoles] = useState<{ id: string; name: string }[] | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    rolesCache ??= getFormAccessRoles();
    rolesCache.then(setRoles).catch(e => { rolesCache = null; setErr(e?.message || "Access roles could not be read"); });
  }, []);
  const sec = (field as any).security || {};
  const view: string[] = Array.isArray(sec.view) ? sec.view : [];
  const edit: string[] = Array.isArray(sec.edit) ? sec.edit : [];
  const set = (next: { view?: string[]; edit?: string[] }) => {
    const v = next.view ?? view, e = next.edit ?? edit;
    onUpdate({ security: v.length || e.length ? { ...(v.length ? { view: v } : {}), ...(e.length ? { edit: e } : {}) } : undefined } as any);
  };
  // Roles are kept by name, as Admin Studio shows them; the server matches a name or an id.
  const names = roles?.map(r => r.name) ?? [];
  const known = new Set(names);
  const row = (title: string, picked: string[], key: "view" | "edit", hint: string) => (
    <div style={{ marginBottom: 8 }}>
      <div style={label}>{title}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
        {[...names, ...picked.filter(p => !known.has(p))].map(n => {
          const on = picked.includes(n);
          return <button key={n} type="button" aria-pressed={on} style={chip(on)}
            onClick={() => set({ [key]: on ? picked.filter(p => p !== n) : [...picked, n] })}>{n}</button>;
        })}
      </div>
      <div style={small}>{picked.length ? hint : "Everyone who can work on the case."}</div>
    </div>
  );
  return (
    <div>
      <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 6 }}>Who can see and change it</div>
      {err && <div style={{ ...small, color: "var(--status-failed)" }}>{err}</div>}
      {roles && !roles.length && <div style={small}>No access roles yet — create them in Admin Studio &gt; Access Roles.</div>}
      {row("Who can see it", view, "view", `Only ${view.join(", ")} see it; it is left out of the form for everyone else.`)}
      {row("Who can change it", edit, "edit", `Only ${edit.join(", ")} change it; others see it read-only.`)}
      <div style={small}>
        The server applies this when it serves the form and when it accepts it; a value sent for a field the person may
        not change is dropped. Customer portals never show a limited field. The case's stored data is not hidden by this.
      </div>
    </div>
  );
}

/* ── Translations ───────────────────────────────────────────────── */

const LANG = /^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$/;

export function TranslationsPanel({ definition, onChange }: { definition: FormDefinition; onChange: (d: FormDefinition) => void }) {
  const langs = languages(definition);
  const [lang, setLang] = useState<string>(langs[0] ?? "");
  const [adding, setAdding] = useState("");
  const all: Translations = ((definition as any).translations || {}) as Translations;
  const cur: LanguageTexts = all[lang] || {};
  const write = (next: LanguageTexts | undefined, code = lang) => {
    const tr: Translations = { ...all };
    if (next) tr[code] = next; else delete tr[code];
    const d: any = { ...definition, translations: tr };
    if (!Object.keys(tr).length) delete d.translations;
    onChange(d);
  };
  const clean = (o: Record<string, any>) => Object.fromEntries(Object.entries(o).filter(([, v]) =>
    v !== undefined && v !== "" && !(typeof v === "object" && !Object.keys(v).length)));
  const setField = (key: string, k: string, v: string) =>
    write({ ...cur, fields: clean({ ...(cur.fields || {}), [key]: clean({ ...(cur.fields?.[key] || {}), [k]: v || undefined }) }) });
  const setOption = (key: string, value: string, v: string) => {
    const f = cur.fields?.[key] || {};
    write({ ...cur, fields: clean({ ...(cur.fields || {}), [key]: clean({ ...f, options: clean({ ...(f.options || {}), [value]: v || undefined }) }) }) });
  };
  const setSection = (id: string, k: "title" | "description", v: string) =>
    write({ ...cur, sections: clean({ ...(cur.sections || {}), [id]: clean({ ...(cur.sections?.[id] || {}), [k]: v || undefined }) }) });
  const box = (value: string | undefined, original: string | undefined, onSet: (v: string) => void, aria: string) => (
    <input aria-label={aria} value={value ?? ""} placeholder={original || "—"} onChange={e => onSet(e.target.value)} style={{ ...input, marginBottom: 3 }} />
  );
  const fields = valueFields(definition);
  const textOnly = (definition.sections || []).flatMap(s => s.fields).filter(f => STATIC_TYPES.has(f.type) && (f as any).text);

  return (
    <Card title="Translations" summary={langs.length ? langs.join(", ") : "form's own language only"}>
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
        {langs.map(l => <button key={l} type="button" aria-pressed={l === lang} style={chip(l === lang)} onClick={() => setLang(l)}>{languageName(l)}</button>)}
        <input aria-label="New language code" value={adding} placeholder="add: de, fr, pt-BR" onChange={e => setAdding(e.target.value.trim())}
          style={{ ...input, width: 120 }} />
        <button type="button" style={link} disabled={!LANG.test(adding) || langs.includes(adding)} onClick={() => {
          write({}, adding); setLang(adding); setAdding("");
        }}>Add</button>
      </div>
      <div style={small}>The person's browser language picks it; they can switch above the form. Only text changes — answers and rules stay the same. An empty box keeps the original.</div>
      {lang && all[lang] && (
        <div style={{ marginTop: 10 }}>
          <div style={label}>Submit button</div>
          {box(cur.form?.submit_label, definition.settings?.submit_label || "Submit", v => write({ ...cur, form: clean({ ...(cur.form || {}), submit_label: v || undefined }) }), "Submit button translation")}
          {(definition.sections || []).map(s => (
            <div key={s.id} style={{ marginTop: 8 }}>
              <div style={label}>Section</div>
              {box(cur.sections?.[s.id]?.title, s.title, v => setSection(s.id, "title", v), `Section ${s.title} title`)}
              {s.description && box(cur.sections?.[s.id]?.description, s.description, v => setSection(s.id, "description", v), `Section ${s.title} description`)}
            </div>
          ))}
          {fields.map(({ key, field, group }) => {
            const t = cur.fields?.[key] || {};
            const opts = Array.isArray(field.options) ? field.options.map((o: any) => typeof o === "string" ? { label: o, value: o } : o) : [];
            return (
              <div key={key} style={{ marginTop: 8, paddingTop: 6, borderTop: "1px solid var(--border-subtle)" }}>
                <div style={label}>{group ? `${group} › ` : ""}{field.label || key}</div>
                {box(t.label, field.label, v => setField(key, "label", v), `${key} label`)}
                {field.description && box(t.description, field.description, v => setField(key, "description", v), `${key} help`)}
                {field.placeholder && box(t.placeholder, field.placeholder, v => setField(key, "placeholder", v), `${key} placeholder`)}
                {field.required_message && box(t.required_message, field.required_message, v => setField(key, "required_message", v), `${key} required message`)}
                {opts.map((o: any) => (
                  <div key={String(o.value)} style={{ paddingLeft: 10 }}>
                    {box(t.options?.[String(o.value)], o.label, v => setOption(key, String(o.value), v), `${key} option ${o.value}`)}
                  </div>
                ))}
              </div>
            );
          })}
          {textOnly.map(f => {
            const key = fieldKey(f);
            return (
              <div key={key} style={{ marginTop: 8 }}>
                <div style={label}>{f.label || key} (text)</div>
                {box(cur.fields?.[key]?.text, (f as any).text, v => setField(key, "text", v), `${key} text`)}
              </div>
            );
          })}
          <button type="button" style={{ ...link, color: "var(--status-failed)", marginTop: 10 }} onClick={() => {
            write(undefined); setLang(langs.find(l => l !== lang) ?? "");
          }}>Remove {languageName(lang)}</button>
        </div>
      )}
    </Card>
  );
}

/* ── Test scenarios ─────────────────────────────────────────────── */

export type FormTest = {
  name: string; values: Record<string, any>; roles?: string[];
  expect: { valid?: boolean; errors?: string[]; values?: Record<string, any> };
};

export function TestsPanel({ definition, onChange, onRecord }: {
  definition: FormDefinition; onChange: (d: FormDefinition) => void; onRecord?: () => void;
}) {
  const tests: FormTest[] = Array.isArray((definition as any).tests) ? (definition as any).tests : [];
  const [results, setResults] = useState<FormTestResult[] | null>(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState("");
  const write = (next: FormTest[]) => {
    const d: any = { ...definition, tests: next };
    if (!next.length) delete d.tests;
    onChange(d);
    setResults(null);
  };
  const update = (i: number, p: Partial<FormTest>) => write(tests.map((t, j) => j === i ? { ...t, ...p } : t));
  const keys = valueFields(definition).filter(f => !f.group).map(f => ({ key: f.key, label: f.field.label || f.key }));
  const run = async () => {
    setRunning(true); setErr("");
    try {
      const { tests: _drop, ...def } = definition as any;
      setResults((await runFormTests(def, tests)).results);
    } catch (e: any) { setErr(e?.message || "The tests could not run"); }
    finally { setRunning(false); }
  };
  const passed = results?.filter(r => r.passed).length;

  return (
    <Card title="Test scenarios" summary={tests.length ? `${tests.length} scenario${tests.length !== 1 ? "s" : ""}${results ? ` · ${passed}/${results.length} pass` : ""}` : "none"}>
      <div style={small}>
        Sample answers and what should happen, checked by the server's own rules — the ones a real submission meets. Run them before you commit.
      </div>
      <div style={{ display: "flex", gap: 10, margin: "8px 0" }}>
        {onRecord && <button type="button" style={link} onClick={onRecord}>+ Record one in View Form</button>}
        {tests.length > 0 && <button type="button" style={link} disabled={running} onClick={run}>{running ? "Running…" : "▶ Run all"}</button>}
      </div>
      {err && <div style={{ ...small, color: "var(--status-failed)" }}>{err}</div>}
      {tests.map((t, i) => {
        const r = results?.[i];
        const refused = t.expect?.valid === false;
        const filled = Object.entries(t.values || {}).filter(([, v]) => v !== undefined && v !== "" && v !== null);
        return (
          <div key={i} style={{ borderTop: "1px solid var(--border-subtle)", paddingTop: 8, marginTop: 8 }}>
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              {r && <span aria-label={r.passed ? "passes" : "fails"} style={{ color: r.passed ? "var(--status-completed, #16a34a)" : "var(--status-failed)" }}>{r.passed ? "✓" : "✗"}</span>}
              <input aria-label="Scenario name" value={t.name} onChange={e => update(i, { name: e.target.value })} style={{ ...input, flex: 1 }} />
              <button type="button" aria-label="Delete scenario" style={{ ...link, color: "var(--text-muted)" }} onClick={() => write(tests.filter((_, j) => j !== i))}>✕</button>
            </div>
            <div style={{ ...small, marginTop: 4 }}>
              {filled.length ? filled.slice(0, 4).map(([k, v]) => `${k} = ${typeof v === "object" ? JSON.stringify(v).slice(0, 30) : String(v).slice(0, 30)}`).join(" · ") : "no answers"}
              {filled.length > 4 ? ` · +${filled.length - 4} more` : ""}
            </div>
            <div style={{ display: "flex", gap: 4, alignItems: "center", marginTop: 6 }}>
              <span style={{ fontSize: 11, color: "var(--text-secondary)" }}>Should be</span>
              <button type="button" aria-pressed={!refused} style={chip(!refused)} onClick={() => update(i, { expect: { ...t.expect, valid: true, errors: undefined } })}>accepted</button>
              <button type="button" aria-pressed={refused} style={chip(refused)} onClick={() => update(i, { expect: { ...t.expect, valid: false, values: undefined } })}>refused</button>
            </div>
            {refused && (
              <div style={{ marginTop: 6 }}>
                <div style={label}>with a problem in</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                  {keys.map(k => {
                    const on = (t.expect.errors || []).includes(k.key);
                    return <button key={k.key} type="button" aria-pressed={on} style={chip(on)} onClick={() => update(i, {
                      expect: { ...t.expect, errors: on ? (t.expect.errors || []).filter(e => e !== k.key) : [...(t.expect.errors || []), k.key] },
                    })}>{k.label}</button>;
                  })}
                </div>
              </div>
            )}
            {!refused && t.expect?.values && Object.keys(t.expect.values).length > 0 && (
              <div style={small}>and stores {Object.entries(t.expect.values).map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join(", ")}</div>
            )}
            {r && !r.passed && (
              <ul style={{ margin: "4px 0 0", paddingLeft: 16, fontSize: 11, color: "var(--status-failed)" }}>
                {r.problems.map((p, j) => <li key={j}>{p}</li>)}
                {r.errors.slice(0, 5).map((e, j) => <li key={`e${j}`} style={{ color: "var(--text-muted)" }}>server: {e.field_key} — {e.message}</li>)}
              </ul>
            )}
          </div>
        );
      })}
    </Card>
  );
}

/** A scenario from answers in View Form, expecting what the form does now
 *  (Studio's check); calculated values are pinned as expected results. */
export function scenarioFrom(definition: FormDefinition, values: Record<string, any>, n: number): FormTest {
  const { clean, errors } = validateForm(definition.sections || [], values);
  const keys = Object.keys(errors).map(k => k.split("[")[0]);
  const calculated = new Set((definition.sections || []).flatMap(s => s.fields)
    .filter(f => typeof (f as any).calculation === "string" && (f as any).calculation.trim()).map(f => fieldKey(f)));
  return {
    name: `Scenario ${n}`,
    values: Object.fromEntries(Object.entries(values).filter(([k]) => !calculated.has(k))),
    expect: keys.length ? { valid: false, errors: [...new Set(keys)] }
      : { valid: true, ...(calculated.size ? { values: Object.fromEntries([...calculated].map(k => [k, clean[k] ?? null])) } : {}) },
  };
}
