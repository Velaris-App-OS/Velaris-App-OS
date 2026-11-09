import { AsYouType, getCountryCallingCode, parsePhoneNumberFromString, type CountryCode } from "libphonenumber-js/max";
import React, { useState, useCallback, useEffect, useMemo, useRef } from "react";
import { Button, Spinner, VoiceFillPanel, VoiceInput, appendTranscript } from "@shared/components";
import type { FormFieldDef, FormSectionDef, FormSettings } from "./FormBuilder";
import { getAccessToken } from "@/auth/tokenManager";
import { STATIC_TYPES, dateBound, fieldKey, formState, localToday, phoneConfig, phoneCountry, validateForm, type FormState } from "./validate";
import { evaluate } from "./expr";
import { languageName, languages, pickLanguage, translate } from "./translate";
import RepeatingGroup from "./RepeatingGroup";
import { fillsFor, GroupKeyContext, RefFetcherContext, staffFetcher, type RefOption } from "./refOptions";
import { useTheme } from "@/theme/ThemeContext";
import { DataTable, RecordDetails, tableRows } from "./display";
import LocationField, { locationText } from "./LocationField";
import { Consent, DateRange, Iban, StaticBlock, Tags, Toggle, codeOptions, smallText } from "./SmallControls";
import { AddressInput, ButtonBlock, FormActionsContext, LookupRegistry, PickerInput, RichText, RichTextInput, SignaturePad,
  addressText, pickerLabel } from "./BatchM";

function _authHdr(): Record<string, string> {
  const t = getAccessToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

function useConnectorLookup(
  field: FormFieldDef,
  values: Record<string, any>,
  setValues: (fn: (prev: Record<string, any>) => Record<string, any>) => void,
  caseId?: string,
  formId?: string,
) {
  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState<{ text: string; ok: boolean } | null>(null);
  /** Several records came back and the person is to pick one (`on_many: "choose"`). */
  const [choices, setChoices] = useState<any[] | null>(null);
  const lastResult = useRef<any>(null);
  const src: any = field.source;

  /** Copy a record's values into the form, and the whole result into this field. */
  const apply = useCallback((record: any, result: any) => {
    const filled: Record<string, any> = {};
    for (const [path, target] of Object.entries(src?.target_fields ?? {})) {
      if (!target) continue;
      const val = path ? atPath(record, path) : record;
      if (val !== undefined) filled[target as string] = val;
    }
    filled[fieldKey(field)] = result;
    setValues(prev => ({ ...prev, ...filled }));
    setChoices(null);
    const n = Object.keys(filled).length - 1;
    setToast({ text: n ? `${n} field${n !== 1 ? "s" : ""} filled in` : "Done", ok: true });
    setTimeout(() => setToast(null), 4000);
  }, [field, src, setValues]);

  const trigger = useCallback(async (auto = false) => {
    if (!src?.connector_id) {
      if (!auto) setToast({ text: "No connector configured for this field", ok: false });
      return;
    }
    setLoading(true);
    setToast(null);
    setChoices(null);
    try {
      const inputData: Record<string, any> = {};
      for (const [param, key] of Object.entries(src.input_mapping ?? {})) {
        inputData[param] = values[key as string] ?? "";
      }
      // The server runs only what the saved form's field says (2026-10-28),
      // so it needs to know which form and field this is.
      const body: any = { input_data: inputData, form_id: formId, field_key: fieldKey(field) };
      if (caseId) body.case_id = caseId;
      if (src.operation) body.operation = src.operation;   // previews only; the server reads the saved field
      const r = await fetch(`/api/v1/hxbridge/connectors/${src.connector_id}/form-lookup`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ..._authHdr() },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok || !data.ok) {
        // A coded error carries its text in `detail` and an object in `error`.
        const msg = typeof data.detail === "string" ? data.detail
          : typeof data.error === "string" ? data.error : "Lookup failed";
        setToast({ text: msg, ok: false });
        return;
      }
      const result = data.result ?? {};
      // `response_path` names the record inside the result (empty = the result).
      const record = atPath(result, src.response_path);
      if (Array.isArray(record)) {
        if (record.length === 0) { setToast({ text: "No match found", ok: false }); return; }
        if (record.length > 1 && src.on_many === "error") { setToast({ text: `${record.length} matches — narrow the search`, ok: false }); return; }
        if (record.length > 1 && src.on_many === "choose") { lastResult.current = result; setChoices(record); return; }
        apply(record[0], result);
      } else {
        apply(record, result);
      }
    } catch (e: any) {
      setToast({ text: e.message || "Lookup error", ok: false });
    } finally {
      setLoading(false);
    }
  }, [field, src, values, caseId, formId, apply]);

  return { trigger, loading, toast, choices, choose: (rec: any) => apply(rec, lastResult.current), cancelChoice: () => setChoices(null) };
}

/** Every field's default value, by key. */
function defaultValues(sections: FormSectionDef[] | undefined): Record<string, any> {
  const out: Record<string, any> = {};
  for (const s of sections || []) for (const f of s.fields || []) {
    if (f.default_value !== undefined && f.default_value !== null && !DISPLAY_FIELDS.has(f.type) && f.type !== "consent") out[fieldKey(f)] = f.default_value;
  }
  return out;
}
const DISPLAY_FIELDS = new Set(["connector_lookup", "connector_result", "json_display", "table_display", "record_details"]);

function atPath(obj: any, path: string | undefined): any {
  if (!path) return obj;
  return path.split(".").reduce((o: any, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
}

/** One line for a record in "choose a match": the values the lookup copies, else its first few. */
function summarise(rec: any, targets?: Record<string, string>): string {
  if (rec === null || typeof rec !== "object") return String(rec);
  const paths = Object.keys(targets ?? {}).filter(Boolean);
  const vals = (paths.length ? paths.map(p => atPath(rec, p)) : Object.values(rec))
    .filter(v => v !== undefined && v !== null && typeof v !== "object").slice(0, 4);
  return vals.length ? vals.join(" · ") : JSON.stringify(rec).slice(0, 80);
}

type Opt = { label: string; value: string };

/** A choice field's options: its own list, or — `options_source.kind ===
 *  "connector"` — rows a connector returns, fetched when the form opens and
 *  again when a mapped input changes. Runs only what the saved form says. */
function useChoiceOptions(field: FormFieldDef, values: Record<string, any>, formId?: string, caseId?: string, enabled = true) {
  const src: any = (field as any).options_source;
  const ref = useReferenceOptions(field, values, formId, enabled);
  const dynamic = src?.kind === "connector" && !!src.connector_id;
  const inputs: Record<string, any> = {};
  if (dynamic) for (const [param, key] of Object.entries(src.input_mapping ?? {})) inputs[param] = values[key as string] ?? "";
  const inputsKey = JSON.stringify(inputs);
  const [state, setState] = useState<{ options: Opt[]; loading: boolean; error: string | null }>(
    { options: [], loading: false, error: null });

  useEffect(() => {
    if (!dynamic || !enabled) return;
    let alive = true;
    setState(s => ({ ...s, loading: true, error: null }));
    fetch(`/api/v1/hxbridge/connectors/${src.connector_id}/form-lookup`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ..._authHdr() },
      // `operation` matters only for an integration administrator's preview; for
      // everyone else the server runs the operation the saved field names.
      body: JSON.stringify({ input_data: inputs, form_id: formId, field_key: fieldKey(field), case_id: caseId,
                             operation: src.operation }),
    }).then(r => r.json().then(d => ({ r, d }))).then(({ r, d }) => {
      if (!alive) return;
      if (!r.ok || !d.ok) {
        setState({ options: [], loading: false, error: typeof d.detail === "string" ? d.detail : d.error || "Could not load the options" });
        return;
      }
      const rows = atPath(d.result, src.list_path);
      const options = (Array.isArray(rows) ? rows : []).map((row: any) => {
        const value = typeof row === "object" && row !== null ? atPath(row, src.value_path || "id") : row;
        const label = typeof row === "object" && row !== null ? atPath(row, src.label_path || "name") : row;
        return { value: String(value ?? ""), label: String(label ?? value ?? "") };
      }).filter((o: Opt) => o.value !== "");
      setState({ options, loading: false, error: null });
    }).catch(e => alive && setState({ options: [], loading: false, error: e?.message || "Could not load the options" }));
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dynamic, enabled, src?.connector_id, inputsKey, formId, caseId]);

  const own: Opt[] = (field.options || []).map((o: any) =>
    typeof o === "string" ? { label: o, value: o } : { label: String(o.label ?? o.value), value: o.value });
  if (ref) return ref;
  return dynamic ? { ...state, options: state.options } : { options: own, loading: false, error: null };
}

/** `options_source.kind === "reference"` (v2 step 3): the rows of a reference
 *  table, through the saved field — filtered by the fields its filters name
 *  (a child list follows its parent), searched on the server as the person
 *  types. Each option keeps the columns it fills other fields from. */
function useReferenceOptions(field: FormFieldDef, values: Record<string, any>, formId?: string, enabled = true) {
  const src: any = (field as any).options_source;
  const isRef = src?.kind === "reference";
  const fetcher = React.useContext(RefFetcherContext) ?? staffFetcher(formId);
  const group = React.useContext(GroupKeyContext);
  const filterVals: Record<string, any> = {};
  for (const f of Array.isArray(src?.filters) ? src.filters : []) if (f?.field) filterVals[f.field] = values[f.field];
  const filterKey = JSON.stringify(filterVals);
  const [q, setQ] = useState("");
  const [state, setState] = useState<{ options: RefOption[]; loading: boolean; error: string | null }>(
    { options: [], loading: false, error: null });
  useEffect(() => {
    if (!isRef || !enabled) return;
    let alive = true;
    setState(s => ({ ...s, loading: true, error: null }));
    const t = setTimeout(() => {
      fetcher({ field_key: fieldKey(field), group_key: group, q, values: filterVals })
        .then(options => alive && setState({ options, loading: false, error: null }))
        .catch(e => alive && setState({ options: [], loading: false, error: e?.message || "Could not load the options" }));
    }, q ? 250 : 0);
    return () => { alive = false; clearTimeout(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isRef, enabled, filterKey, q, formId, group]);
  if (!isRef) return null;
  return { ...state, options: state.options.map(o => ({ label: String(o.label ?? o.value ?? ""), value: o.value === null || o.value === undefined ? "" : String(o.value) })),
           search: setQ, raw: state.options };
}

/** A dropdown with a search box — used for long lists. */
function SearchableSelect({ options, value, onChange, placeholder, disabled, style, onQuery }: {
  options: Opt[]; value: any; onChange: (v: any) => void; placeholder?: string; disabled?: boolean; style: React.CSSProperties;
  /** Search on the server too (a reference table can be long). */
  onQuery?: (q: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ0] = useState("");
  const setQ = (v: string) => { setQ0(v); onQuery?.(v); };
  const selected = options.find(o => o.value === value);
  const shown = q ? options.filter(o => o.label.toLowerCase().includes(q.toLowerCase())) : options;
  return (
    <div style={{ position: "relative" }}>
      <input
        role="combobox" aria-expanded={open}
        value={open ? q : selected?.label ?? ""}
        placeholder={placeholder || "Search…"}
        disabled={disabled}
        onFocus={() => { setOpen(true); setQ(""); }}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onChange={e => setQ(e.target.value)}
        onKeyDown={e => { if (e.key === "Enter" && shown[0]) { onChange(shown[0].value); setOpen(false); (e.target as HTMLInputElement).blur(); } }}
        style={style}
      />
      {open && (
        <div role="listbox" style={{
          position: "absolute", zIndex: 20, left: 0, right: 0, top: "100%", marginTop: 2, maxHeight: 240, overflow: "auto",
          background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
          boxShadow: "0 6px 20px rgba(0,0,0,.15)",
        }}>
          {shown.length === 0 && <div style={{ padding: "8px 12px", fontSize: 12, color: "var(--text-muted)" }}>No match</div>}
          {shown.map(o => (
            <div key={o.value} role="option" aria-selected={o.value === value}
              onMouseDown={e => { e.preventDefault(); onChange(o.value); setOpen(false); }}
              style={{ padding: "7px 12px", fontSize: 13, cursor: "pointer", color: "var(--text-primary)",
                background: o.value === value ? "var(--accent-dim)" : "transparent" }}>
              {o.label}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   FormRenderer — runtime form rendering for step assignments

   Given a form definition_json, renders a dynamic form, validates
   inputs, and calls onSubmit with the collected values.
   ═══════════════════════════════════════════════════════════════════ */

interface FormRendererProps {
  formName: string;
  definition: { sections: FormSectionDef[]; settings?: FormSettings };
  initialValues?: Record<string, any>;
  onSubmit: (values: Record<string, any>) => Promise<void>;
  onCancel: () => void;
  readOnly?: boolean;
  caseId?: string;   // when provided, file_upload fields upload to this case
  formId?: string;   // scopes voice fill to this form's fields
  /** A second button beside Submit that takes the answers as they are, unchecked
   *  — "Save draft" in a case, "Save as test" in View Form (§5 step 7). */
  secondaryAction?: { label: string; run: (values: Record<string, any>) => Promise<string | void> | string | void };
  /** A line above the form (e.g. "Draft from … restored"). */
  notice?: React.ReactNode;
}

export default function FormRenderer({
  formName,
  definition,
  initialValues = {},
  onSubmit,
  onCancel,
  readOnly = false,
  caseId,
  formId,
  secondaryAction,
  notice,
}: FormRendererProps) {
  // A field's default value fills it when the form opens; what the case
  // already holds (pre-fill) wins. A read-only view shows only what was saved.
  const [values, setValues] = useState<Record<string, any>>(() =>
    readOnly ? initialValues : { ...defaultValues(definition.sections), ...initialValues });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");

  // Translations (§5 step 7): the person's language when the form has it; text only.
  const langs = languages(definition);
  const [lang, setLang] = useState<string | null>(() =>
    pickLanguage(definition, typeof navigator !== "undefined" ? navigator.languages : undefined));
  const shownDef = useMemo(() => translate(definition, lang), [definition, lang]);
  const [secondaryBusy, setSecondaryBusy] = useState(false);
  const [secondaryNote, setSecondaryNote] = useState("");

  const allSections = shownDef.sections || [];
  const settings: FormSettings = (shownDef as any).settings || {};
  // Step-by-step (part 9): each section is a page. A read-only view shows everything at once.
  const stepMode = settings.mode === "steps" && !readOnly;
  // Tabs (batch M): each section is a tab; everything is checked on Submit.
  const tabMode = settings.mode === "tabs" && !readOnly;
  const [step, setStep] = useState(0);
  const [tab, setTab] = useState(0);
  const lookupRegistry = useRef(new Map<string, () => void>()).current;
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(allSections.filter(s => s.collapsible && s.collapsed).map(s => [s.id, true])));

  const setValue = (key: string, val: any, also?: Record<string, any>) => {
    setValues(prev => applyOnChange(allSections, key, { ...prev, ...(also ?? {}), [key]: val }));
    // Clear error on change — a group's row errors too (`key[i].child`).
    if (Object.keys(errors).some(k => k === key || k.startsWith(`${key}[`))) {
      setErrors(prev => Object.fromEntries(Object.entries(prev).filter(([k]) => k !== key && !k.startsWith(`${key}[`))));
    }
  };

  // Width settings apply side by side only when there is room for them.
  const [narrow, setNarrow] = useState(false);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = bodyRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) => setNarrow(entry.contentRect.width < 520));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // The same check the server runs (validate.ts mirrors velaris_shared/forms).
  const sections = allSections;
  // Shown, read-only now, required now, and calculated values — one resolver,
  // the server's twin (Form Builder v2). A calculated field shows its result.
  const st = formState(sections, values);
  const shown: Record<string, boolean> = Object.fromEntries(st.keys.map(k => [k, st.shown(k)]));
  const view: Record<string, any> = { ...values };
  for (const f of sections.flatMap(sec => sec.fields)) {
    if (typeof (f as any).calculation === "string" && (f as any).calculation.trim()) view[fieldKey(f)] = st.value(fieldKey(f));
  }
  // A page whose fields are all hidden by rules is skipped.
  const pages = sections.filter(s => !s.fields.length || s.fields.some(f => shown[fieldKey(f)] !== false));
  const reviewing = stepMode && !!settings.review_page && step === pages.length;
  const pageIndex = Math.min(step, Math.max(0, pages.length - 1));
  const lastPage = step >= pages.length - 1;
  const spanFor = (f: FormFieldDef) => narrow ? 6
    : f.width ? SPAN[f.width] ?? 6 : 6 / (settings.columns && [1, 2, 3].includes(settings.columns) ? settings.columns : 1);

  const validate = (): Record<string, any> | null => {
    const { clean, errors: newErrors } = validateForm(sections, values);
    setErrors(newErrors);
    return Object.keys(newErrors).length === 0 ? clean : null;
  };

  /** Next page: only this page's fields are checked. */
  const next = () => {
    const { errors: all } = validateForm(sections, values);
    const here = new Set(pages[pageIndex]?.fields.map(f => fieldKey(f)) ?? []);
    const pageErrors = Object.fromEntries(Object.entries(all).filter(([k]) => here.has(baseKey(k))));
    setErrors(pageErrors);
    if (Object.keys(pageErrors).length) return;
    setStep(s => s + 1);
    bodyRef.current?.scrollIntoView?.({ block: "start" });
  };

  const handleSubmit = async () => {
    const clean = validate();
    if (!clean) {
      const { errors: raw } = validateForm(sections, values);
      // A repeating group's row errors (`items[0].qty`) count for the group.
      const all: Record<string, string> = {};
      for (const k of Object.keys(raw)) all[baseKey(k)] = raw[k];
      // A closed section with a problem opens, so the message can be seen.
      setCollapsed(c => Object.fromEntries(Object.entries(c).map(([id, v]) =>
        [id, v && !sections.find(sec => sec.id === id)?.fields.some(f => all[fieldKey(f)])])));
      // In steps or tabs, go to the first page with a problem.
      if (stepMode || tabMode) {
        const first = pages.findIndex(p => p.fields.some(f => all[fieldKey(f)]));
        if (first >= 0) (stepMode ? setStep : setTab)(first);
      }
      return;
    }
    setSubmitting(true);
    setSubmitError("");
    try {
      await onSubmit(clean);
    } catch (e: any) {
      setSubmitError(e?.message || "Submission failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{
      background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
      borderRadius: "var(--radius-md)", overflow: "hidden",
    }}>
      {/* Header */}
      <div style={{
        padding: "var(--space-md) var(--space-lg)",
        borderBottom: "1px solid var(--border-subtle)",
        background: "var(--bg-elevated)",
      }}>
        <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text-primary)", fontFamily: "var(--font-display)" }}>
          {formName}
        </div>
        {readOnly && (
          <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase" }}>
            read only
          </span>
        )}
        {langs.length > 0 && (
          <select aria-label="Language" value={lang ?? ""} onChange={e => setLang(e.target.value || null)} style={{
            float: "right", marginTop: -20, fontSize: 11, padding: "2px 6px", background: "var(--bg-input)",
            border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-secondary)",
          }}>
            <option value="">Original</option>
            {langs.map(l => <option key={l} value={l}>{languageName(l)}</option>)}
          </select>
        )}
      </div>
      {notice && (
        <div style={{ padding: "6px var(--space-lg)", fontSize: 12, background: "var(--bg-elevated)",
          borderBottom: "1px solid var(--border-subtle)", color: "var(--text-secondary)" }}>{notice}</div>
      )}

      {/* Sections */}
      <div ref={bodyRef} style={{ padding: "var(--space-lg)" }}>
        {/* Voice fill proposes values into the fields below; it never submits. */}
        {caseId && !readOnly && (
          <VoiceFillPanel
            caseId={caseId}
            formId={formId}
            disabled={submitting}
            onAccept={(key, value) => { if (!st.locked(key)) setValue(key, value); }}
          />
        )}
        {stepMode && pages.length > 1 && (
          <div style={{ marginBottom: "var(--space-lg)" }} aria-label="Progress">
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
              {reviewing ? "Check your answers" : `Step ${pageIndex + 1} of ${pages.length}${settings.review_page ? " (then a review)" : ""} · ${pages[pageIndex]?.title ?? ""}`}
            </div>
            <div style={{ height: 4, background: "var(--bg-elevated)", borderRadius: 2, overflow: "hidden" }}>
              <div style={{ height: "100%", background: "var(--accent)", transition: "width .2s",
                width: `${((reviewing ? pages.length : pageIndex + 1) / pages.length) * 100}%` }} />
            </div>
          </div>
        )}
        {tabMode && pages.length > 1 && (
          <div role="tablist" aria-label="Form sections" style={{ display: "flex", gap: 2, borderBottom: "1px solid var(--border-subtle)",
            marginBottom: "var(--space-lg)", overflowX: "auto" }}>
            {pages.map((p, i) => {
              const bad = p.fields.some(f => Object.keys(errors).some(k => baseKey(k) === fieldKey(f)));
              const on = i === Math.min(tab, pages.length - 1);
              return (
                <button key={p.id} type="button" role="tab" aria-selected={on} onClick={() => setTab(i)} style={{
                  padding: "8px 12px", fontSize: 12, fontWeight: 500, background: "none", border: "none", whiteSpace: "nowrap",
                  borderBottom: `2px solid ${on ? "var(--accent)" : "transparent"}`, marginBottom: -1, cursor: "pointer",
                  color: on ? "var(--text-primary)" : "var(--text-muted)",
                }}>{p.title}{bad && <span aria-label="has a problem" style={{ color: "var(--status-failed)", marginLeft: 4 }}>●</span>}</button>
              );
            })}
          </div>
        )}
        <FormActionsContext.Provider value={{ sections, setValues,
          goToSection: id => { const i = pages.findIndex(p => p.id === id); if (i < 0) return; if (stepMode) setStep(i); else if (tabMode) setTab(i); } }}>
        <LookupRegistry.Provider value={lookupRegistry}>
        {reviewing ? (
          <Review pages={pages} values={view} shown={shown} onEdit={i => setStep(i)} />
        ) : (stepMode ? [pages[pageIndex]].filter(Boolean) : tabMode && pages.length > 1 ? [pages[Math.min(tab, pages.length - 1)]] : sections).map((section, si, list) => {
          const closed = !stepMode && section.collapsible && collapsed[section.id];
          return (
          <div key={section.id} style={{ marginBottom: si < list.length - 1 ? "var(--space-xl)" : 0 }}>
            <div
              onClick={() => !stepMode && section.collapsible && setCollapsed(c => ({ ...c, [section.id]: !c[section.id] }))}
              role={!stepMode && section.collapsible ? "button" : undefined}
              aria-expanded={!stepMode && section.collapsible ? !closed : undefined}
              style={{
                fontSize: 12, fontWeight: 600, color: "var(--text-primary)",
                fontFamily: "var(--font-display)", marginBottom: closed ? 0 : section.description ? 4 : "var(--space-md)",
                paddingBottom: "var(--space-xs)", borderBottom: "1px solid var(--border-subtle)",
                cursor: !stepMode && section.collapsible ? "pointer" : "default", display: "flex", gap: 6, alignItems: "center",
              }}>
              {!stepMode && section.collapsible && <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{closed ? "▸" : "▾"}</span>}
              {section.title}
            </div>
            {!closed && section.description && (
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: "var(--space-md)" }}>{section.description}</div>
            )}

            {/* Six columns: full = 6, half = 3, third = 2. Below 520 px every
                field is full width, so a phone never gets a squeezed column. */}
            {!closed && (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(6, minmax(0, 1fr))", gap: "var(--space-md)" }}>
              {section.fields.filter(field => shown[fieldKey(field)] !== false).map(field => (
                <div key={field.id || fieldKey(field)} style={{ gridColumn: `span ${spanFor(field)}`, minWidth: 0 }}>
                <FieldRenderer
                  field={field}
                  value={view[fieldKey(field)]}
                  error={errors[fieldKey(field)]}
                  onChange={(val, also) => setValue(fieldKey(field), val, also)}
                  readOnly={readOnly || st.locked(fieldKey(field))}
                  requiredNow={st.required(fieldKey(field))}
                  errors={errors}
                  rowStates={st.rowStates[fieldKey(field)]}
                  caseId={caseId}
                  formId={formId}
                  allValues={view}
                  setAllValues={setValues}
                />
                </div>
              ))}
            </div>
            )}
          </div>
          );
        })}
        </LookupRegistry.Provider>
        </FormActionsContext.Provider>
      </div>

      {/* Actions */}
      {!readOnly && (
        <div style={{
          padding: "var(--space-md) var(--space-lg)",
          borderTop: "1px solid var(--border-subtle)",
          display: "flex", justifyContent: "space-between", alignItems: "center",
        }}>
          {submitError && (
            <span style={{ fontSize: 12, color: "var(--status-failed)" }}>{submitError}</span>
          )}
          {!submitError && secondaryNote && (
            <span role="status" style={{ fontSize: 12, color: "var(--text-muted)" }}>{secondaryNote}</span>
          )}
          <div style={{ display: "flex", gap: "var(--space-sm)", marginLeft: "auto" }}>
            <Button variant="secondary" onClick={onCancel}>Cancel</Button>
            {secondaryAction && (
              <Button variant="secondary" disabled={secondaryBusy} onClick={async () => {
                setSecondaryBusy(true); setSubmitError(""); setSecondaryNote("");
                try { setSecondaryNote((await secondaryAction.run(values)) || "Saved"); }
                catch (e: any) { setSubmitError(e?.message || "That did not work"); }
                finally { setSecondaryBusy(false); }
              }}>{secondaryAction.label}</Button>
            )}
            {stepMode && step > 0 && <Button variant="secondary" onClick={() => setStep(s => Math.max(0, Math.min(s, pages.length) - 1))}>Back</Button>}
            {stepMode && !reviewing && !(lastPage && !settings.review_page) ? (
              <Button onClick={next}>{lastPage ? "Review" : "Next"}</Button>
            ) : (
              <Button onClick={handleSubmit} disabled={submitting}>
                {submitting ? "Submitting…" : settings.submit_label || "Submit"}
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** The answers, page by page, before Submit (`settings.review_page`). */
function Review({ pages, values, shown, onEdit }: {
  pages: FormSectionDef[]; values: Record<string, any>; shown: Record<string, boolean>; onEdit: (page: number) => void;
}) {
  const show = (f: FormFieldDef, v: any): string => {
    if (v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length)) return "—";
    const label = (x: any) => (f.options || []).map((o: any) => (typeof o === "string" ? { label: o, value: o } : o))
      .find((o: any) => o.value === x)?.label ?? String(x);
    if (Array.isArray(v)) return v.map(x => (x && typeof x === "object" ? x.filename ?? JSON.stringify(x) : label(x))).join(", ");
    if (typeof v === "boolean") return v ? "Yes" : "No";
    if (f.type === "location") return locationText(v) || "—";
    if (f.type === "address") return addressText(v) || "—";
    if (f.type === "signature") return typeof v === "string" ? "Signed" : "—";
    if (["user_picker", "case_picker", "document_picker"].includes(f.type)) return typeof v === "string" ? pickerLabel(v) : "—";
    if (f.type === "rich_text") return typeof v === "string" ? (v.length > 120 ? `${v.slice(0, 120)}…` : v) : "—";
    const small = smallText(f, v);
    if (small !== null) return small;
    if (f.type === "repeating_group" && Array.isArray(v)) return `${v.length} ${v.length === 1 ? "row" : "rows"}`;
    if (typeof v === "object") return v.filename ?? JSON.stringify(v).slice(0, 80);
    return label(v);
  };
  return (
    <div>
      {pages.map((p, i) => (
        <div key={p.id} style={{ marginBottom: "var(--space-lg)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", borderBottom: "1px solid var(--border-subtle)", marginBottom: 6 }}>
            <span style={{ fontSize: 12, fontWeight: 600 }}>{p.title}</span>
            <button type="button" onClick={() => onEdit(i)} style={{ background: "none", border: "none", color: "var(--accent)", fontSize: 12, cursor: "pointer" }}>Change</button>
          </div>
          <dl style={{ display: "grid", gridTemplateColumns: "minmax(120px, 35%) 1fr", gap: "4px 12px", margin: 0, fontSize: 13 }}>
            {p.fields.filter(f => shown[fieldKey(f)] !== false && !DISPLAY_FIELDS.has(f.type)).map(f => (
              <React.Fragment key={f.id}>
                <dt style={{ color: "var(--text-muted)" }}>{f.label}</dt>
                <dd style={{ margin: 0 }}>{show(f, values[fieldKey(f)])}</dd>
              </React.Fragment>
            ))}
          </dl>
        </div>
      ))}
    </div>
  );
}

/* ── Individual Field Renderer ────────────────────────────────── */

/** The field an error belongs to: `items[0].qty` is the group `items`'s. */
const baseKey = (k: string) => k.split("[")[0];

/** A field's `on_change` actions, run when the person changes it: set another
 *  field to a formula's result, or clear it. The person can still change the
 *  target afterwards — this is a convenience, the server checks the result. */
export function applyOnChange(sections: FormSectionDef[], key: string, next: Record<string, any>): Record<string, any> {
  const field = sections.flatMap(sec => sec.fields).find(f => fieldKey(f) === key) as any;
  const actions = Array.isArray(field?.on_change) ? field.on_change : [];
  if (!actions.length) return next;
  const out = { ...next };
  for (const a of actions) {
    if (!a || typeof a !== "object" || typeof a.target !== "string" || !a.target || a.target === key) continue;
    if (formState(sections, out).locked(a.target)) continue;     // never a calculated or read-only field
    const target = sections.flatMap(sec => sec.fields).find(f => fieldKey(f) === a.target);
    if (target?.type === "consent" && a.action === "set") continue;   // only the person agrees
    if (a.action === "clear") { delete out[a.target]; continue; }
    if (a.action === "set" && typeof a.expression === "string") {
      const st = formState(sections, out);
      const v = evaluate(a.expression, k => st.reads(k, true), localToday());
      if (v === null || v === undefined) delete out[a.target]; else out[a.target] = v;
    }
  }
  return out;
}

export function FieldRenderer({ field, value, error, onChange, readOnly, caseId, formId, allValues, setAllValues, preview, requiredNow,
  bare, errors, rowStates }: {
  field: FormFieldDef;
  /** A cell of a repeating group's table: no label, no help text. */
  bare?: boolean;
  /** Repeating group: every error of the form (its rows' are `key[i].child`). */
  errors?: Record<string, string>;
  /** Repeating group: each row's state from the form's resolver. */
  rowStates?: FormState[];
  /** Required at this moment (`required`, or `required_when` holds). */
  requiredNow?: boolean;
  /** Drawn on the builder's canvas: no dictation button. */
  preview?: boolean;
  value: any;
  error?: string;
  /** `also`: other fields to set in the same change (a reference row's fill). */
  onChange: (val: any, also?: Record<string, any>) => void;
  readOnly: boolean;
  caseId?: string;
  formId?: string;
  allValues?: Record<string, any>;
  setAllValues?: (fn: (prev: Record<string, any>) => Record<string, any>) => void;
}) {
  const connectorLookup = useConnectorLookup(field, allValues ?? {}, setAllValues ?? (() => {}), caseId, formId);
  // `trigger: "change"`: run by itself once every mapped input has a value,
  // and again when one of them changes (after a short pause in typing).
  const lookupInputs = field.type === "connector_lookup" && (field.source as any)?.trigger === "change"
    ? Object.values((field.source as any)?.input_mapping ?? {}).map(k => (allValues ?? {})[k as string]) : null;
  const lookupKey = lookupInputs ? JSON.stringify(lookupInputs) : "";
  // A button may run this lookup (batch M): register it under the field key.
  const registry = React.useContext(LookupRegistry);
  useEffect(() => {
    if (!registry || field.type !== "connector_lookup" || readOnly || preview) return;
    const k = fieldKey(field);
    registry.set(k, () => connectorLookup.trigger());
    return () => { registry.delete(k); };
  });
  useEffect(() => {
    if (!lookupInputs || readOnly || preview || !lookupInputs.length) return;
    if (lookupInputs.some(v => v === undefined || v === null || v === "")) return;
    const t = setTimeout(() => connectorLookup.trigger(true), 600);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lookupKey, readOnly, preview]);
  // The builder's canvas (preview + read-only) loads nothing; the portal (preview only) does.
  const choice: any = useChoiceOptions(field, allValues ?? {}, formId, caseId,
    ["dropdown", "radio", "multi_select"].includes(field.type) && !(preview && readOnly));
  const opts: Opt[] = choice.options;
  // A reference option fills other fields from its row, in the same change (v2 step 3).
  const choose = (v: any) => {
    const src: any = (field as any).options_source;
    if (src?.kind !== "reference" || !Array.isArray(src.fill) || !src.fill.length) return onChange(v);
    onChange(v, fillsFor(src, (choice.raw ?? []).find((o: RefOption) => String(o.value) === String(v))));
  };
  const across = (field as any).display?.layout === "across";
  const groupKeyForPicker = React.useContext(GroupKeyContext);
  // The browser's own date picker follows the app's theme (it was always dark).
  const { resolvedTheme } = useTheme();
  const v = field.validation || {};
  const today = localToday();
  const step = (dec: any, fallback: any) =>
    v.step ?? (typeof dec === "number" ? (dec === 0 ? 1 : Number((10 ** -dec).toFixed(dec))) : fallback);
  const inputStyle: React.CSSProperties = {
    width: "100%", padding: "8px 12px", fontSize: 13,
    fontFamily: "var(--font-body)",
    background: readOnly ? "var(--bg-elevated)" : "var(--bg-input)",
    border: `1px solid ${error ? "var(--status-failed)" : "var(--border-default)"}`,
    borderRadius: "var(--radius-sm)", color: "var(--text-primary)",
    outline: "none", boxSizing: "border-box" as const,
  };

  const renderInput = () => {
    // A phone field with a country set (2026-11-04); without one it is the
    // plain text box below, as before.
    if (field.type === "phone" && phoneConfig(field as any)) {
      return <PhoneInput field={field as any} value={value} onChange={onChange} readOnly={readOnly} style={inputStyle} />;
    }
    switch (field.type) {
      case "text":
      case "email":
      case "phone":
      case "url":
        return (
          <>
            <input
              type={field.type === "email" ? "email" : field.type === "phone" ? "tel" : field.type === "url" ? "url" : "text"}
              value={value || ""}
              onChange={e => onChange(e.target.value)}
              placeholder={field.placeholder}
              disabled={readOnly}
              maxLength={v.max_length || undefined}
              style={inputStyle}
              onFocus={e => !error && (e.target.style.borderColor = "var(--border-focus)")}
              onBlur={e => !error && (e.target.style.borderColor = "var(--border-default)")}
            />
            <Counter field={field} value={value} />
          </>
        );

      case "number":
        return (
          <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
            <input
              type="number"
              value={value ?? ""}
              onChange={e => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
              placeholder={field.placeholder}
              disabled={readOnly}
              style={{ ...inputStyle, paddingRight: v.unit ? 12 + 8 * String(v.unit).length : undefined }}
              min={v.min}
              max={v.max}
              step={step(v.decimals, "any")}
              onFocus={e => !error && (e.target.style.borderColor = "var(--border-focus)")}
              onBlur={e => !error && (e.target.style.borderColor = "var(--border-default)")}
            />
            {v.unit && (
              <span style={{ position: "absolute", right: 10, fontSize: 12, color: "var(--text-muted)", pointerEvents: "none" }}>{v.unit}</span>
            )}
          </div>
        );

      case "textarea":
        return (
          <>
            <textarea
              value={value || ""}
              onChange={e => onChange(e.target.value)}
              placeholder={field.placeholder}
              disabled={readOnly}
              rows={(field as any).display?.rows || 4}
              maxLength={v.max_length || undefined}
              style={{ ...inputStyle, resize: "vertical" as const }}
            />
            <Counter field={field} value={value} />
          </>
        );

      case "dropdown": {
        // Long lists get a search box; a designer can also ask for one.
        const searchable = (field as any).display?.searchable ?? opts.length > 10;
        return (
          <>
            {searchable && !readOnly ? (
              <SearchableSelect options={opts} value={value} onChange={choose} onQuery={choice.search}
                placeholder={field.placeholder} disabled={readOnly} style={inputStyle} />
            ) : (
              <select
                value={value ?? ""}
                onChange={e => choose(e.target.value)}
                disabled={readOnly}
                style={inputStyle}
              >
                <option value="">{choice.loading ? "Loading…" : field.placeholder || "Select…"}</option>
                {opts.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
              </select>
            )}
            <ChoiceStatus loading={choice.loading} error={choice.error} />
          </>
        );
      }

      case "checkbox":
        return (
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: readOnly ? "default" : "pointer" }}>
            <input
              type="checkbox"
              checked={!!value}
              onChange={e => onChange(e.target.checked)}
              disabled={readOnly}
              style={{ accentColor: "var(--accent)", width: 16, height: 16 }}
            />
            <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
              {field.placeholder || field.label}
            </span>
          </label>
        );

      case "date":
        return (
          <input
            type="date"
            value={value || ""}
            onChange={e => onChange(e.target.value)}
            disabled={readOnly}
            min={(v.no_past ? today : dateBound(v.earliest, today)) ?? undefined}
            max={(v.no_future ? today : dateBound(v.latest, today)) ?? undefined}
            style={{ ...inputStyle, colorScheme: resolvedTheme }}
          />
        );

      case "toggle":
        return <Toggle value={value} onChange={onChange} readOnly={readOnly} label={field.label} />;

      case "time":
        return <input type="time" value={value || ""} onChange={e => onChange(e.target.value || undefined)} disabled={readOnly}
          min={v.earliest} max={v.latest} style={{ ...inputStyle, colorScheme: resolvedTheme }} />;

      case "date_range":
        return <DateRange value={value} onChange={onChange} readOnly={readOnly}
          inputStyle={{ ...inputStyle, colorScheme: resolvedTheme }}
          min={(v.no_past ? today : dateBound(v.earliest, today)) ?? undefined}
          max={(v.no_future ? today : dateBound(v.latest, today)) ?? undefined} />;

      case "percentage":
        return (
          <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
            <input type="number" value={value ?? ""} disabled={readOnly} placeholder={field.placeholder}
              onChange={e => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
              min={v.min ?? 0} max={v.max ?? 100} step={step(v.decimals, "any")} style={{ ...inputStyle, paddingRight: 28 }} />
            <span style={{ position: "absolute", right: 10, fontSize: 12, color: "var(--text-muted)", pointerEvents: "none" }}>%</span>
          </div>
        );

      case "tags":
        return <Tags value={value} onChange={onChange} readOnly={readOnly} inputStyle={inputStyle} placeholder={field.placeholder} />;

      case "country":
      case "currency_code": {
        const codes = codeOptions(field.type);
        return readOnly
          ? <input value={codes.find(o => o.value === value)?.label ?? value ?? ""} disabled style={inputStyle} />
          : <SearchableSelect options={codes} value={value} onChange={c => onChange(c || undefined)}
              placeholder={field.placeholder || (field.type === "country" ? "Choose a country…" : "Choose a currency…")} style={inputStyle} />;
      }

      case "iban":
        return <Iban value={value} onChange={onChange} readOnly={readOnly} inputStyle={inputStyle} placeholder={field.placeholder} />;

      case "consent":
        return <Consent field={field} value={value} onChange={onChange} readOnly={readOnly} />;

      case "address":
        return <AddressInput value={value} onChange={onChange} readOnly={readOnly} inputStyle={inputStyle} />;

      case "signature":
        return <SignaturePad value={value} onChange={onChange} readOnly={readOnly} />;

      case "user_picker":
      case "case_picker":
      case "document_picker":
        return <PickerInput field={field} value={value} onChange={onChange} readOnly={readOnly} inputStyle={inputStyle}
          formId={formId} caseId={caseId} groupKey={groupKeyForPicker} />;

      case "rich_text":
        return <RichTextInput value={value} onChange={onChange} readOnly={readOnly} inputStyle={inputStyle}
          placeholder={field.placeholder} maxLength={v.max_length || undefined} />;

      case "location":
        return <LocationField value={value} onChange={onChange} readOnly={readOnly} inputStyle={inputStyle} error={error} />;

      case "file_upload":
        return <FileUploadField value={value} onChange={onChange} readOnly={readOnly} caseId={caseId}
          accept={v.accept} maxSizeMb={v.max_size_mb} multiple={!!v.multiple} maxFiles={v.max_files} />;

      case "datetime":
        return (
          <input type="datetime-local" value={value || ""} onChange={e => onChange(e.target.value)}
            disabled={readOnly}
            min={(() => { const b = v.no_past ? today : dateBound(v.earliest, today); return b ? `${b}T00:00` : undefined; })()}
            max={(() => { const b = v.no_future ? today : dateBound(v.latest, today); return b ? `${b}T23:59` : undefined; })()}
            style={{ ...inputStyle, colorScheme: resolvedTheme }} />
        );

      case "radio": {
        return (
          <div style={{ display: "flex", flexDirection: across ? "row" : "column", flexWrap: "wrap", gap: across ? 16 : 8 }}>
            <ChoiceStatus loading={choice.loading} error={choice.error} />
            {opts.map(opt => (
              <label key={opt.value} style={{ display: "flex", alignItems: "center", gap: 8, cursor: readOnly ? "default" : "pointer", fontSize: 13, color: "var(--text-primary)" }}>
                <input type="radio"
                  name={fieldKey(field)}
                  value={opt.value}
                  checked={value === opt.value}
                  onChange={() => !readOnly && choose(opt.value)}
                  disabled={readOnly}
                  style={{ accentColor: "var(--accent)", width: 15, height: 15 }}
                />
                {opt.label}
              </label>
            ))}
          </div>
        );
      }

      case "multi_select": {
        const msValue: string[] = Array.isArray(value) ? value : [];
        return (
          <div style={{ display: "flex", flexDirection: across ? "row" : "column", flexWrap: "wrap", gap: across ? 16 : 8 }}>
            <ChoiceStatus loading={choice.loading} error={choice.error} />
            {opts.map(opt => (
              <label key={opt.value} style={{ display: "flex", alignItems: "center", gap: 8, cursor: readOnly ? "default" : "pointer", fontSize: 13, color: "var(--text-primary)" }}>
                <input type="checkbox"
                  checked={msValue.includes(opt.value)}
                  onChange={e => {
                    if (readOnly) return;
                    onChange(e.target.checked ? [...msValue, opt.value] : msValue.filter(v => v !== opt.value));
                  }}
                  disabled={readOnly}
                  style={{ accentColor: "var(--accent)", width: 15, height: 15 }}
                />
                {opt.label}
              </label>
            ))}
          </div>
        );
      }

      case "rating": {
        const maxStars = field.validation?.max_stars || 5;
        const ratingValue = Number(value) || 0;
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 2 }}>
            {Array.from({ length: maxStars }, (_, i) => i + 1).map(star => (
              <button key={star} type="button" disabled={readOnly}
                onClick={() => !readOnly && onChange(star === ratingValue ? 0 : star)}
                style={{
                  background: "none", border: "none",
                  cursor: readOnly ? "default" : "pointer",
                  fontSize: 26, lineHeight: 1, padding: "0 1px",
                  color: star <= ratingValue ? "#f59e0b" : "var(--border-default)",
                  transition: "color 0.1s",
                }}>★</button>
            ))}
            {ratingValue > 0 && (
              <span style={{ fontSize: 12, color: "var(--text-muted)", marginLeft: 6 }}>
                {Array.isArray(v.labels) && v.labels[ratingValue - 1] ? v.labels[ratingValue - 1] : `${ratingValue}/${maxStars}`}
              </span>
            )}
          </div>
        );
      }

      case "slider": {
        const min = field.validation?.min ?? 0;
        const max = field.validation?.max ?? 100;
        const step = field.validation?.step ?? 1;
        const sliderVal = value !== undefined && value !== null && value !== "" ? Number(value) : min;
        return (
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
              <span>{v.min_label || min}</span>
              <span style={{ fontWeight: 700, color: "var(--text-primary)", fontFamily: "var(--font-mono)" }}>{sliderVal}{v.unit ? ` ${v.unit}` : ""}</span>
              <span>{v.max_label || max}</span>
            </div>
            <input type="range" min={min} max={max} step={step} value={sliderVal}
              onChange={e => !readOnly && onChange(Number(e.target.value))}
              disabled={readOnly}
              style={{ width: "100%", accentColor: "var(--accent)", cursor: readOnly ? "default" : "pointer" }}
            />
          </div>
        );
      }

      case "currency": {
        const symbol = v.symbol || v.currency || "$";
        return (
          <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
            <span style={{
              position: "absolute", left: 10, fontSize: 13, fontWeight: 600,
              color: "var(--text-muted)", fontFamily: "var(--font-mono)", pointerEvents: "none",
            }}>{symbol}</span>
            <input type="number" step={step(v.decimals ?? 2, "0.01")} min={v.min ?? 0} max={v.max}
              value={value ?? ""}
              onChange={e => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
              disabled={readOnly}
              placeholder={field.placeholder || "0.00"}
              style={{ ...inputStyle, paddingLeft: symbol.length > 2 ? 48 : 30 }}
              onFocus={e => !readOnly && !error && (e.target.style.borderColor = "var(--border-focus)")}
              onBlur={e => !error && (e.target.style.borderColor = "var(--border-default)")}
            />
          </div>
        );
      }

      case "connector_lookup": {
        const { trigger, loading: lookupLoading, toast, choices, choose, cancelChoice } = connectorLookup;
        const src: any = field.source ?? {};
        const auto = src.trigger === "change";
        return (
          <div>
            {!auto && (
              <button
                type="button"
                disabled={lookupLoading || readOnly}
                onClick={() => trigger()}
                style={{
                  padding: "7px 16px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
                  background: "var(--bg-elevated)", color: "var(--text-primary)", cursor: lookupLoading ? "wait" : "pointer",
                  fontSize: 12, fontWeight: 600, display: "flex", alignItems: "center", gap: 6,
                }}
              >
                {lookupLoading ? "Looking up…" : `⚡ ${src.button_label || (src.connector_id ? "Look up" : "Look up (no connector set)")}`}
              </button>
            )}
            {auto && (
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                {lookupLoading ? "Looking up…" : "Filled in by itself once the fields it needs are filled."}
              </div>
            )}
            {choices && (
              <div role="listbox" aria-label="Choose a match" style={{ marginTop: 6, border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", maxHeight: 220, overflow: "auto" }}>
                <div style={{ padding: "6px 10px", fontSize: 11, color: "var(--text-muted)", display: "flex", justifyContent: "space-between" }}>
                  {choices.length} matches — choose one
                  <button type="button" onClick={cancelChoice} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)" }}>✕</button>
                </div>
                {choices.map((rec, i) => (
                  <div key={i} role="option" aria-selected={false} onClick={() => choose(rec)}
                    style={{ padding: "6px 10px", fontSize: 12, cursor: "pointer", borderTop: "1px solid var(--border-subtle)" }}>
                    {summarise(rec, src.target_fields)}
                  </div>
                ))}
              </div>
            )}
            {toast && (
              <div style={{
                marginTop: 6, fontSize: 11, padding: "5px 10px", borderRadius: 4,
                background: toast.ok ? "#22c55e22" : "#ef444422",
                color: toast.ok ? "#22c55e" : "#ef4444",
              }}>{toast.text}</div>
            )}
          </div>
        );
      }

      case "connector_result": {
        if (!value) return <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic" }}>No data yet — trigger a connector lookup above.</div>;
        return (
          <ConnectorResultDisplay data={value} />
        );
      }

      case "json_display": {
        if (!value) return <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic" }}>—</div>;
        return <JsonTreeDisplay data={value} />;
      }

      case "table_display": {
        // `from`: another field's value (e.g. a lookup's whole result); else its own (pre-fill, sample).
        const data = field.from ? (allValues ?? {})[field.from] : value;
        return <DataTable rows={tableRows(data, field.rows_path)} columns={field.columns as any}
          emptyMessage={field.empty_message} pageSize={field.page_size} sortable={field.sortable}
          total={field.rows_path ? undefined : data?.total} />;
      }

      case "repeating_group":
        return <RepeatingGroup field={field} value={value} onChange={onChange} readOnly={readOnly} errors={errors}
          states={rowStates} preview={preview} caseId={caseId} formId={formId} outer={allValues} />;

      case "record_details": {
        const data = field.from ? (allValues ?? {})[field.from] : value;
        const record = field.rows_path ? atPath(data, field.rows_path) : data;
        return <RecordDetails data={Array.isArray(record) ? record[0] : record} items={field.columns as any}
          layout={(field as any).display?.layout === "grid" ? "grid" : "list"} emptyMessage={field.empty_message}
          showRaw={field.show_raw} />;
      }

      default:
        return <input type="text" value={value || ""} onChange={e => onChange(e.target.value)} style={inputStyle} />;
    }
  };

  if (field.type === "button") return <ButtonBlock field={field} readOnly={readOnly || preview} />;
  if (STATIC_TYPES.has(field.type)) return <StaticBlock field={field} />;

  return (
    <div>
      {field.type !== "checkbox" && !bare && (
        <label style={{
          display: "block", fontSize: 12, fontWeight: 500, color: "var(--text-secondary)",
          marginBottom: 4,
        }}>
          {field.label}
          {(requiredNow ?? field.required) && <span style={{ color: "var(--status-failed)", marginLeft: 2 }}>*</span>}
        </label>
      )}
      {field.description && !bare && (
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>{field.description}</div>
      )}
      {/* Dictation on the free-text fields (voice input P2). Only the kinds a
          person would actually speak into — a dropdown or a date picker gains
          nothing from a mic. The control hides itself when the flag is off or
          no engine is installed, so this is opt-in by capability, and it
          APPENDS rather than replacing half-typed text.
          Distinct from the VoiceFillPanel above the form: that one talks
          through the model to propose values across many fields; this puts
          your words in one box. */}
      {DICTATABLE_FIELDS.has(field.type) && !readOnly && !preview ? (
        <div style={{ display: "flex", alignItems: "flex-start", gap: 6 }}>
          <div style={{ flex: 1, minWidth: 0 }}>{renderInput()}</div>
          <VoiceInput
            size={30}
            caseId={caseId}
            onText={t => onChange(appendTranscript(typeof value === "string" ? value : "", t))}
          />
        </div>
      ) : renderInput()}
      {error && (
        <div style={{ fontSize: 11, color: "var(--status-failed)", marginTop: 3 }}>{error}</div>
      )}
    </div>
  );
}

const SPAN: Record<string, number> = { full: 6, half: 3, third: 2 };

function ChoiceStatus({ loading, error }: { loading: boolean; error: string | null }) {
  if (error) return <div style={{ fontSize: 11, color: "var(--status-failed)", width: "100%" }}>{error}</div>;
  if (loading) return <div style={{ fontSize: 11, color: "var(--text-muted)", width: "100%" }}>Loading options…</div>;
  return null;
}

/** "12 / 200" under a text box, when the designer turned the counter on. */
function Counter({ field, value }: { field: FormFieldDef; value: any }) {
  const display = (field as any).display || {};
  if (!display.counter) return null;
  const n = [...(typeof value === "string" ? value : "")].length;
  const max = field.validation?.max_length;
  return (
    <div style={{ fontSize: 10, color: "var(--text-muted)", textAlign: "right", marginTop: 2 }}>
      {max ? `${n} / ${max}` : n}
    </div>
  );
}

/** Field kinds worth a mic: free text a person would dictate. A dropdown, a
 *  date picker or a rating gains nothing from one. */
const DICTATABLE_FIELDS = new Set(["text", "textarea", "email", "phone"]);

/* ── File Upload Field ────────────────────────────────────────── */

function FileUploadField({ value, onChange, readOnly, caseId, accept, maxSizeMb, multiple, maxFiles }: {
  value: any;
  onChange: (val: any) => void;
  readOnly: boolean;
  caseId?: string;
  /** Allowed extensions, e.g. [".pdf", ".png"]; empty = any the platform accepts. */
  accept?: string[];
  maxSizeMb?: number;
  multiple?: boolean;
  maxFiles?: number;
}) {
  const [uploading, setUploading] = useState(false);
  const [uploadErr, setUploadErr] = useState<string | null>(null);

  // Value: one { document_id, filename } (or { filename, local } with no case),
  // or a list of them when the field takes several files.
  const files: any[] = (Array.isArray(value) ? value : value ? [value] : [])
    .filter(f => f && typeof f === "object" && f.filename);
  const exts = (accept || []).map(a => (a.startsWith(".") ? a : `.${a}`).toLowerCase());
  const room = multiple ? (maxFiles ? Math.max(0, maxFiles - files.length) : Infinity) : (files.length ? 0 : 1);

  async function upload(file: File): Promise<any> {
    if (!caseId) return { filename: file.name, local: true };   // no case: keep the name so the form can submit
    const fd = new FormData();
    fd.append("case_id", caseId);
    fd.append("file", file);
    const r = await fetch("/api/v1/documents/upload", {
      method: "POST", body: fd,
      headers: getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {},
    });
    if (!r.ok) throw new Error(`Upload failed: ${r.status}`);
    const doc = await r.json();
    return { document_id: doc.id, filename: file.name };
  }

  async function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(e.target.files || []);
    e.target.value = "";
    if (!picked.length) return;
    setUploadErr(null);
    // Checked before anything leaves the browser; the server checks the type again on submit.
    for (const f of picked) {
      if (exts.length && !exts.some(x => f.name.toLowerCase().endsWith(x))) {
        setUploadErr(`${f.name}: allowed file types are ${exts.join(", ")}`); return;
      }
      if (maxSizeMb && f.size > maxSizeMb * 1024 * 1024) {
        setUploadErr(`${f.name} is larger than ${maxSizeMb} MB`); return;
      }
    }
    if (picked.length > room) {
      setUploadErr(multiple ? `At most ${maxFiles} files` : "Only one file is allowed"); return;
    }
    setUploading(true);
    try {
      const done: any[] = [];
      for (const f of picked) done.push(await upload(f));
      onChange(multiple ? [...files, ...done] : done[0]);
    } catch (err: any) {
      setUploadErr(err.message || "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  function remove(i: number) {
    const next = files.filter((_, j) => j !== i);
    onChange(multiple ? (next.length ? next : null) : null);
    setUploadErr(null);
  }

  if (readOnly) {
    return (
      <div style={{ fontSize: 13, color: files.length ? "var(--accent)" : "var(--text-muted)" }}>
        {files.length ? files.map(f => `📎 ${f.filename}`).join("  ") : "No file uploaded"}
      </div>
    );
  }

  return (
    <div>
      {files.map((f, i) => (
        <div key={`${f.filename}-${i}`} style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
          <div style={{
            display: "flex", alignItems: "center", gap: 6,
            padding: "6px 12px", border: "1px solid var(--status-completed)",
            borderRadius: "var(--radius-sm)", background: "color-mix(in srgb, var(--status-completed) 8%, transparent)",
          }}>
            <span style={{ fontSize: 14 }}>📎</span>
            <span style={{ fontSize: 12, color: "var(--status-completed)", fontWeight: 500 }}>{f.filename}</span>
            {f.document_id && (
              <span style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>· saved</span>
            )}
          </div>
          <button onClick={() => remove(i)} style={{
            background: "none", border: "none", cursor: "pointer", fontSize: 12, color: "var(--text-muted)",
          }}>✕ Remove</button>
        </div>
      ))}
      {room > 0 && (
        <label style={{
          display: "inline-flex", alignItems: "center", gap: 8, cursor: uploading ? "wait" : "pointer",
          padding: "8px 16px", border: "2px dashed var(--border-default)",
          borderRadius: "var(--radius-sm)", fontSize: 12, color: "var(--text-secondary)",
          background: "var(--bg-elevated)", transition: "border-color 0.15s",
          width: "100%", boxSizing: "border-box" as const,
        }}
          onMouseEnter={(e) => (e.currentTarget.style.borderColor = "var(--accent)")}
          onMouseLeave={(e) => (e.currentTarget.style.borderColor = "var(--border-default)")}
        >
          <span style={{ fontSize: 18 }}>📎</span>
          {uploading ? "Uploading…" : multiple ? "Click to choose files" : "Click to choose file"}
          <span style={{ fontSize: 10, color: "var(--text-muted)", marginLeft: "auto" }}>
            {[exts.length ? exts.join(" ") : "", maxSizeMb ? `≤ ${maxSizeMb} MB` : "", !caseId ? "(no case linked)" : ""]
              .filter(Boolean).join(" · ")}
          </span>
          <input type="file" style={{ display: "none" }} onChange={handleFile} disabled={uploading}
            accept={exts.length ? exts.join(",") : undefined} multiple={!!multiple} />
        </label>
      )}
      {uploadErr && (
        <div style={{ fontSize: 11, color: "var(--status-failed)", marginTop: 4 }}>{uploadErr}</div>
      )}
    </div>
  );
}

/* ── Connector result display components ─────────────────────────── */

function ConnectorResultDisplay({ data }: { data: any }) {
  const [collapsed, setCollapsed] = useState(false);
  return (
    <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", overflow: "hidden" }}>
      <div
        onClick={() => setCollapsed(c => !c)}
        style={{ padding: "6px 10px", background: "var(--bg-elevated)", cursor: "pointer", fontSize: 11, fontWeight: 600, display: "flex", justifyContent: "space-between" }}
      >
        <span>Connector Result</span>
        <span>{collapsed ? "▶" : "▼"}</span>
      </div>
      {!collapsed && (
        <pre style={{ margin: 0, padding: "8px 12px", fontSize: 11, fontFamily: "var(--font-mono)", whiteSpace: "pre-wrap", maxHeight: 300, overflow: "auto", background: "var(--bg-card)", color: "var(--text-primary)" }}>
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  );
}

function JsonTreeDisplay({ data }: { data: any }) {
  const [collapsed, setCollapsed] = useState(false);
  return (
    <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", overflow: "hidden" }}>
      <div
        onClick={() => setCollapsed(c => !c)}
        style={{ padding: "5px 10px", background: "var(--bg-elevated)", cursor: "pointer", fontSize: 11, fontWeight: 600, display: "flex", justifyContent: "space-between" }}
      >
        <span>{Array.isArray(data) ? `Array [${data.length}]` : "Object {…}"}</span>
        <span>{collapsed ? "▶" : "▼"}</span>
      </div>
      {!collapsed && (
        <pre style={{ margin: 0, padding: "8px 12px", fontSize: 11, fontFamily: "var(--font-mono)", whiteSpace: "pre-wrap", maxHeight: 400, overflow: "auto", background: "var(--bg-card)", color: "var(--text-primary)" }}>
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  );
}




/** A country-aware phone number (2026-11-04): a country selector, the number
 *  formatted as it is typed, and — once it is valid — the value in
 *  international form (+447911123456), which is also how the server stores it.
 *  Allowed countries, when the designer limited them, are the selector's list. */
function PhoneInput({ field, value, onChange, readOnly, style }: {
  field: Record<string, any>; value: any; onChange: (v: any) => void; readOnly?: boolean;
  style: React.CSSProperties;
}) {
  const cfg = phoneConfig(field)!;
  const allowed: string[] = (cfg.countries || []).filter(Boolean);
  const choices = codeOptions("country").filter(o => phoneCountry(o.value) && (!allowed.length || allowed.includes(o.value)));
  const initial = (() => {
    const p = typeof value === "string" && value.startsWith("+") ? parsePhoneNumberFromString(value) : undefined;
    return p?.country && (!allowed.length || allowed.includes(p.country)) ? p.country : cfg.default_country;
  })();
  const [country, setCountry] = React.useState<string>(initial);
  const [text, setText] = React.useState<string>(() => {
    const p = typeof value === "string" ? parsePhoneNumberFromString(value, initial as CountryCode) : undefined;
    return p ? (p.country === initial ? p.formatNational() : p.formatInternational()) : (value || "");
  });
  const emit = (raw: string, c: string) => {
    const p = parsePhoneNumberFromString(raw, c as CountryCode);
    onChange(raw.trim() === "" ? "" : p?.isValid() ? p.number : raw);
  };
  return (
    <div style={{ display: "flex", gap: 6 }}>
      <select aria-label={`${field.label || "Phone"} country`} value={country} disabled={readOnly}
        onChange={e => { setCountry(e.target.value); emit(text, e.target.value); }}
        style={{ ...style, width: 210, flex: "0 0 auto" }}>
        {choices.map(o => (
          <option key={o.value} value={o.value}>{o.label} +{getCountryCallingCode(o.value as CountryCode)}</option>
        ))}
      </select>
      <input type="tel" inputMode="tel" autoComplete="tel" value={text} disabled={readOnly}
        placeholder={field.placeholder}
        onChange={e => {
          const raw = e.target.value;
          // Format as typed, but never fight the person deleting characters.
          const next = raw.length < text.length ? raw : new AsYouType(country as CountryCode).input(raw);
          setText(next);
          emit(raw, country);
        }}
        style={{ ...style, flex: 1 }} />
    </div>
  );
}
