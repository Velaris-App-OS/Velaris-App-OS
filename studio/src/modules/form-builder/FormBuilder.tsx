import React, { useState, useCallback, useMemo, useEffect } from "react";
import { TestsPanel, TranslationsPanel } from "./Step7Editors";
import { getFormVariables } from "@shared/api/client";
import { Button, EmptyState } from "@shared/components";
import FieldSettings, { keyFromLabel } from "./FieldSettings";
import { FieldRenderer } from "./FormRenderer";
import { renameRef } from "./expr";
import { ReusableSectionEditor, ReusableSectionsPanel, linkedSection, saveAsReusable } from "./ReusableSections";

/* ═══════════════════════════════════════════════════════════════════
   FormBuilder — visual drag-and-drop form designer

   The canvas shows each field as the person filling the form will see it
   (width included), and fields drag within and between sections; the
   panel on the right is the palette, or the selected field's settings
   (redesign part 2, 2026-10-28).
   
   Produces a definition_json with structure:
   {
     sections: [{ id, title, order, fields: [{ id, type, label, ... }] }]
   }
   ═══════════════════════════════════════════════════════════════════ */

export type JSONSchema = Record<string, any>;

export interface ConnectorSourceDef {
  connector_id:           string;
  input_mapping:          Record<string, string>;
  response_path:          string;
  response_normalization?: "raw" | "postgres_safe" | "mongo_safe";
  target_fields?:         Record<string, string>;
  null_policy?:           "skip" | "error" | "default";
}

export interface FormFieldDef {
  id: string;
  type: "text" | "number" | "dropdown" | "checkbox" | "date" | "textarea" | "email" | "phone" | "file_upload"
      | "radio" | "multi_select" | "rating" | "slider" | "currency" | "datetime"
      | "connector_lookup" | "connector_result" | "json_display" | "table_display" | "record_details"
      | "repeating_group" | "location"
      | "toggle" | "time" | "date_range" | "percentage" | "url" | "tags" | "country" | "currency_code"
      | "iban" | "consent" | "heading" | "paragraph" | "divider" | "callout" | "embed"
      | "address" | "signature" | "user_picker" | "case_picker" | "document_picker" | "rich_text" | "button";
  data_type?:    JSONSchema;
  label: string;
  field_key: string;
  required: boolean;
  placeholder?: string;
  description?: string;
  options?: { label: string; value: string }[];
  validation?: Record<string, any>;
  default_value?: any;
  null_policy?:  "skip" | "error" | "default";
  source?:       ConnectorSourceDef;
  /** Case variable to pre-fill from (e.g. "crm.account_status" or "subject").
   *  Falls back to matching the field_key against case variables. */
  variable?: string;
  /** Redesign 2026-10-28 — all optional; absent = as before. See validate.ts. */
  required_message?: string;
  hidden?:    boolean;
  read_only?: boolean;
  show_when?: { match?: "all" | "any"; rules: { field: string; op: string; value?: any }[]; expression?: string };
  /** Form Builder v2 (2026-10-28) — all optional; absent = as before. Formulas: expr.ts. */
  calculation?:    string;
  required_when?:  { match?: "all" | "any"; rules?: { field: string; op: string; value?: any }[]; expression?: string };
  read_only_when?: { match?: "all" | "any"; rules?: { field: string; op: string; value?: any }[]; expression?: string };
  checks?:         { expression: string; message?: string }[];
  /** Field-level security (§5 step 7): access role names; absent = everyone. The server enforces it. */
  security?:       { view?: string[]; edit?: string[] };
  on_change?:      { action: "set" | "clear"; target: string; expression?: string }[];
  /** Repeating group (v2): its own fields, one set per row; stored as a list of rows. */
  fields?:         FormFieldDef[];
  min_rows?:       number;
  max_rows?:       number;
  add_label?:      string;
  /** How it is drawn — never what is accepted (that is `validation`). */
  display?:   { counter?: boolean; rows?: number; layout?: "down" | "across" | "list" | "grid"; searchable?: boolean };
  /** Record details / table (part 8): read the value of another field (e.g. a lookup's whole result). */
  from?:          string;
  /** Table: where the rows are inside that value. */
  rows_path?:     string;
  /** Table columns / record details items, in order. */
  columns?:       { path: string; label?: string; format?: string; currency?: string }[];
  empty_message?: string;
  page_size?:     number;
  sortable?:      boolean;
  /** Record details: also offer the raw data under "All data". */
  show_raw?:      boolean;
  /** Options from a connector instead of `options` (part 4). */
  options_source?: { kind: "connector"; connector_id: string; input_mapping?: Record<string, string>;
                     list_path?: string; label_path?: string; value_path?: string }
                   /** A reference table (v2 step 3): checked by the server, filtered by other fields, fills others. */
                   | { kind: "reference"; table_id?: string; value_column?: string; label_column?: string;
                       filters?: { column: string; field: string }[]; fill?: { column: string; field: string }[] };
  /** Side by side on wide screens; full width on a phone. Absent = full. */
  width?:     "full" | "half" | "third";
  /** Legacy P38b shape, still honoured. */
  conditions?: { field_id: string; equals: any }[];
}

export interface FormSectionDef {
  id: string;
  title: string;
  order: number;
  fields: FormFieldDef[];
  /** Part 9: text under the title; a header that opens / closes it. */
  description?: string;
  collapsible?: boolean;
  /** Starts closed (collapsible sections only). */
  collapsed?: boolean;
  /** Linked to a reusable section (§5 step 6): its fields are the server's copy, not edited here. */
  section_ref?: { id: string; version?: number };
}

/** Form-level settings (part 9). Absent = one page, one column, "Submit". */
export interface FormSettings {
  submit_label?: string;
  /** Fields with no width of their own share the row: 1, 2 or 3 per row. */
  columns?: 1 | 2 | 3;
  /** "steps": each section is a page, with Back / Next. */
  mode?: "single" | "steps" | "tabs";
  /** Steps: a page to check every answer before Submit. */
  review_page?: boolean;
  /** "per_case": each case keeps the version of this form it first used (§5 step 6). */
  versioning?: "per_case";
}

export interface FormDefinition {
  sections: FormSectionDef[];
  settings?: FormSettings;
  /** §5 step 7: text per language (translate.ts) and test scenarios (Step7Editors.tsx). */
  translations?: import("./translate").Translations;
  tests?: import("./Step7Editors").FormTest[];
  definition_version?: number;
}

export const FORM_DEFINITION_VERSION = 2;

interface FormBuilderProps {
  definition: FormDefinition;
  onChange: (def: FormDefinition) => void;
  /** The form as last saved — a saved field's key no longer follows its label. */
  savedDefinition?: FormDefinition;
  /** The saved form's id — used to list the case variables its fields can pre-fill from. */
  formId?: string;
  /** Open View Form to record a test scenario (§5 step 7). */
  onRecordTest?: () => void;
}

/** The palette, in groups (2026-10-29). Order here is the order shown:
 *  Layout always first, then Data, then the inputs. */
export const FIELD_GROUPS = ["Layout", "Data", "Text", "Numbers", "Date & time", "Choices", "Special"] as const;

const FIELD_TYPES: { type: FormFieldDef["type"]; label: string; icon: string; group: typeof FIELD_GROUPS[number] }[] = [
  // ── Text
  { type: "text",             label: "Text",             icon: "Aa", group: "Text" },
  { type: "textarea",         label: "Text Area",        icon: "¶", group: "Text" },
  { type: "email",            label: "Email",            icon: "@", group: "Text" },
  { type: "phone",            label: "Phone",            icon: "☎", group: "Text" },
  { type: "url",              label: "Web Address",      icon: "🔗", group: "Text" },
  { type: "rich_text",        label: "Rich Text",        icon: "𝐓", group: "Text" },
  // ── Numbers
  { type: "number",           label: "Number",           icon: "#", group: "Numbers" },
  { type: "currency",         label: "Currency",         icon: "$", group: "Numbers" },
  { type: "percentage",       label: "Percentage",       icon: "%", group: "Numbers" },
  { type: "slider",           label: "Slider",           icon: "⇔", group: "Numbers" },
  { type: "rating",           label: "Rating",           icon: "★", group: "Numbers" },
  // ── Date & time
  { type: "date",             label: "Date",             icon: "📅", group: "Date & time" },
  { type: "datetime",         label: "Date & Time",      icon: "🕐", group: "Date & time" },
  { type: "time",             label: "Time",             icon: "⏱", group: "Date & time" },
  { type: "date_range",       label: "Date Range",       icon: "↔", group: "Date & time" },
  // ── Choices
  { type: "dropdown",         label: "Dropdown",         icon: "▾", group: "Choices" },
  { type: "radio",            label: "Radio",            icon: "⊙", group: "Choices" },
  { type: "multi_select",     label: "Multi Select",     icon: "☰", group: "Choices" },
  { type: "checkbox",         label: "Checkbox",         icon: "☑", group: "Choices" },
  { type: "toggle",           label: "Toggle",           icon: "⏻", group: "Choices" },
  { type: "tags",             label: "Tags",             icon: "#", group: "Choices" },
  { type: "country",          label: "Country",          icon: "🌐", group: "Choices" },
  { type: "currency_code",    label: "Currency Code",    icon: "¤", group: "Choices" },
  // ── Files, places & money
  { type: "file_upload",      label: "File Upload",      icon: "📎", group: "Special" },
  { type: "location",         label: "Location",         icon: "📍", group: "Special" },
  { type: "iban",             label: "IBAN",             icon: "🏦", group: "Special" },
  { type: "consent",          label: "Consent",          icon: "✔", group: "Special" },
  { type: "address",          label: "Address",          icon: "🏠", group: "Special" },
  { type: "signature",        label: "Signature",        icon: "✍", group: "Special" },
  // ── Data
  { type: "connector_lookup", label: "Connector Lookup", icon: "⚡", group: "Data" },
  { type: "record_details",   label: "Record Details",   icon: "▤", group: "Data" },
  { type: "table_display",    label: "Table",            icon: "⊞", group: "Data" },
  { type: "user_picker",      label: "Person Picker",    icon: "👤", group: "Data" },
  { type: "case_picker",      label: "Case Picker",      icon: "🗂", group: "Data" },
  { type: "document_picker",  label: "Document Picker",  icon: "📄", group: "Data" },
  { type: "connector_result", label: "Connector Result (legacy)", icon: "↩", group: "Data" },
  { type: "json_display",     label: "JSON Display (legacy)",     icon: "{}", group: "Data" },
  // ── Layout
  { type: "heading",          label: "Heading",          icon: "H", group: "Layout" },
  { type: "paragraph",        label: "Paragraph",        icon: "≡", group: "Layout" },
  { type: "divider",          label: "Divider",          icon: "—", group: "Layout" },
  { type: "callout",          label: "Callout",          icon: "ⓘ", group: "Layout" },
  { type: "embed",            label: "Embedded Page",    icon: "▣", group: "Layout" },
  { type: "button",           label: "Button",           icon: "⏎", group: "Layout" },
  { type: "repeating_group",  label: "Repeating Group",  icon: "⊟", group: "Layout" },
];

type DragItem = { kind: "type"; type: FormFieldDef["type"] } | { kind: "field"; sectionId: string; fieldId: string };

const DRAG_MIME = "application/x-velaris-form-field";

/** `f` with every reference to the field key `from` renamed `to` (v2). */
export function renameField(f: FormFieldDef, from: string, to: string): FormFieldDef {
  const r = (src: any) => (typeof src === "string" ? renameRef(src, from, to) : src);
  const when = (w: any) => w && typeof w === "object"
    ? { ...w, ...(Array.isArray(w.rules) ? { rules: w.rules.map((x: any) => x?.field === from ? { ...x, field: to } : x) } : {}),
        ...(typeof w.expression === "string" ? { expression: r(w.expression) } : {}) }
    : w;
  const out: any = { ...f };
  for (const p of ["show_when", "required_when", "read_only_when"]) if ((f as any)[p]) out[p] = when((f as any)[p]);
  if (typeof f.calculation === "string") out.calculation = r(f.calculation);
  if (Array.isArray(f.checks)) out.checks = f.checks.map(c => ({ ...c, expression: r(c.expression) }));
  if (Array.isArray(f.on_change)) out.on_change = f.on_change.map(a => ({ ...a, target: a.target === from ? to : a.target, expression: r(a.expression) }));
  const src: any = (f as any).options_source;
  if (src?.kind === "reference") {
    const fx = (l: any) => (Array.isArray(l) ? l.map((m: any) => (m?.field === from ? { ...m, field: to } : m)) : l);
    out.options_source = { ...src, filters: fx(src.filters), fill: fx(src.fill) };
  }
  return out;
}

function readDrag(e: React.DragEvent): DragItem | null {
  const raw = e.dataTransfer.getData(DRAG_MIME);
  if (raw) { try { return JSON.parse(raw); } catch { return null; } }
  const t = e.dataTransfer.getData("field_type");   // the palette's old payload
  return t ? { kind: "type", type: t as FormFieldDef["type"] } : null;
}

/* Palette groups start closed; the ones a designer opens are remembered in
   this browser only (2026-10-29) — storage failing means all start closed. */
const PALETTE_OPEN_KEY = "velaris.formbuilder.palette.open";

function usePaletteGroups(): [Set<string>, (g: string) => void] {
  const [open, setOpen] = useState<Set<string>>(() => {
    try {
      const raw = JSON.parse(localStorage.getItem(PALETTE_OPEN_KEY) || "[]");
      return new Set(Array.isArray(raw) ? raw.filter((x: unknown) => typeof x === "string") : []);
    } catch { return new Set(); }
  });
  const toggle = (g: string) => setOpen(prev => {
    const next = new Set(prev);
    if (next.has(g)) next.delete(g); else next.add(g);
    try { localStorage.setItem(PALETTE_OPEN_KEY, JSON.stringify([...next])); } catch { /* stays in memory */ }
    return next;
  });
  return [open, toggle];
}

export default function FormBuilder({ definition, onChange, savedDefinition, formId, onRecordTest }: FormBuilderProps) {
  const [openGroups, toggleGroup] = usePaletteGroups();
  // undefined = loading; null = none to show (no saved form, or not a designer)
  const [variables, setVariables] = useState<Awaited<ReturnType<typeof getFormVariables>> | null | undefined>(formId ? undefined : null);
  useEffect(() => {
    if (!formId) { setVariables(null); return; }
    let alive = true;
    setVariables(undefined);
    getFormVariables(formId).then(v => alive && setVariables(v)).catch(() => alive && setVariables(null));
    return () => { alive = false; };
  }, [formId]);
  const [selectedFieldId, setSelectedFieldId] = useState<string | null>(null);
  /** Where a drop would land: before `index` in `sectionId` (index = length → at the end). */
  const [dropAt, setDropAt] = useState<{ sectionId: string; index: number } | null>(null);

  const sections = definition.sections || [];
  const sorted = [...sections].sort((a, b) => a.order - b.order);
  /** Every field's sample / default value — what the canvas draws, so a table
   *  reading a lookup shows the lookup's sample rows. */
  const samples: Record<string, any> = {};
  for (const s of sections) for (const f of s.fields) if (f.default_value !== undefined) samples[f.field_key || f.id] = f.default_value;

  const savedKeyById = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of savedDefinition?.sections || []) for (const f of s.fields || []) if (f.id) m.set(f.id, f.field_key);
    return m;
  }, [savedDefinition]);

  const update = useCallback((sections: FormSectionDef[]) => {
    onChange({ ...definition, sections });
  }, [definition, onChange]);

  const allKeys = (exceptId?: string) => new Set(
    sections.flatMap(s => s.fields).filter(f => f.id !== exceptId).map(f => f.field_key));

  // ── Section ops ──────────────────────────────────────────────
  const addSection = () => {
    const id = `section-${Date.now()}`;
    update([...sections, { id, title: `Section ${sections.length + 1}`, order: sections.length, fields: [] }]);
  };

  const updateSection = (sectionId: string, partial: Partial<FormSectionDef>) => {
    update(sections.map(s => s.id === sectionId ? { ...s, ...partial } : s));
  };

  // Reusable sections (§5 step 6): link one in, edit it everywhere, or unlink a copy.
  const [editingReusable, setEditingReusable] = useState<string | null>(null);
  const [reusableRefresh, setReusableRefresh] = useState(0);
  const locked = (sectionId: string) => !!sections.find(s => s.id === sectionId)?.section_ref;

  const deleteSection = (sectionId: string) => {
    const sec = sections.find(s => s.id === sectionId);
    if (sec && sec.fields.length && !window.confirm(`Delete "${sec.title}" and its ${sec.fields.length} field(s)?`)) return;
    update(sections.filter(s => s.id !== sectionId).map((s, i) => ({ ...s, order: i })));
  };

  const moveSection = (sectionId: string, dir: -1 | 1) => {
    const list = [...sorted];
    const idx = list.findIndex(s => s.id === sectionId);
    const swapIdx = idx + dir;
    if (swapIdx < 0 || swapIdx >= list.length) return;
    [list[idx], list[swapIdx]] = [list[swapIdx], list[idx]];
    update(list.map((s, i) => ({ ...s, order: i })));
  };

  // ── Field ops ────────────────────────────────────────────────
  const newField = (type: FormFieldDef["type"]): FormFieldDef => {
    const id = `field-${Date.now()}`;
    const label = FIELD_TYPES.find(t => t.type === type)?.label || type;
    return {
      id, type, label, field_key: keyFromLabel(label, allKeys()),
      required: false, placeholder: "",
      ...(["dropdown", "radio", "multi_select"].includes(type)
        ? { options: [{ label: "Option 1", value: "option_1" }, { label: "Option 2", value: "option_2" }] }
        : {}),
      ...(type === "slider"   ? { validation: { min: 0, max: 100, step: 1 } } : {}),
      ...(type === "rating"   ? { validation: { max_stars: 5 } } : {}),
      ...(type === "currency" ? { validation: { symbol: "$", decimals: 2 } } : {}),
      // New fields get the stricter checks; forms made before keep theirs.
      ...(type === "email"    ? { validation: { format: "strict" } } : {}),
      ...(type === "phone"    ? { validation: { pattern: "phone" } } : {}),
      // Small controls (2026-10-29): words to start from, and a consent that is asked for.
      ...(type === "paragraph" ? { text: "Write the text here." } : {}),
      ...(type === "callout"   ? { label: "Note", text: "Write the message here.", tone: "info" } : {}),
      ...(type === "consent"   ? { label: "Terms", required: true, terms: "Write or paste the terms here." } : {}),
      ...(type === "embed"     ? { url: "", height: 400 } : {}),
      ...(type === "button"    ? { label: "Fill in", actions: [] } : {}),
      // A repeating group starts as a small line-item table the designer reshapes.
      ...(type === "repeating_group" ? { label: "Items", field_key: keyFromLabel("Items", allKeys()), add_label: "+ Add item", fields: [
        { id: `${id}-c1`, type: "text", label: "Description", field_key: "description", required: true, placeholder: "" },
        { id: `${id}-c2`, type: "number", label: "Amount", field_key: "amount", required: false, placeholder: "" },
      ] as FormFieldDef[] } : {}),
    };
  };

  const insertField = (sectionId: string, field: FormFieldDef, index?: number) => {
    update(sections.map(s => {
      if (s.id !== sectionId) return s;
      const fields = [...s.fields];
      fields.splice(index ?? fields.length, 0, field);
      return { ...s, fields };
    }));
    setSelectedFieldId(field.id);
  };

  const addField = (sectionId: string, type: FormFieldDef["type"], index?: number) =>
    insertField(sectionId, newField(type), index);

  const updateField = (sectionId: string, fieldId: string, partial: Partial<FormFieldDef>) => {
    // A new key: rules on other fields that named the old one follow it.
    const before = sections.flatMap(s => s.fields).find(f => f.id === fieldId);
    const old = before?.field_key;
    const renamed = partial.field_key !== undefined && old && partial.field_key !== old ? partial.field_key : null;
    // A repeating group's renamed columns: `items.qty` elsewhere follows too.
    const group = (partial.field_key ?? old) as string;
    const colRenames: [string, string][] = Array.isArray(partial.fields) && Array.isArray(before?.fields)
      ? partial.fields.flatMap(c => {
          const was = before!.fields!.find(b => b.id === c.id);
          return was && was.field_key !== c.field_key ? [[`${group}.${was.field_key}`, `${group}.${c.field_key}`] as [string, string]] : [];
        })
      : [];
    // Formulas, conditions, checks and on-change targets follow it too (v2).
    const follow = (f: FormFieldDef): FormFieldDef => {
      let out = renamed && old ? renameField(f, old, renamed) : f;
      for (const [a, b] of colRenames) out = renameField(out, a, b);
      return out;
    };
    update(sections.map(s => ({
      ...s,
      fields: s.fields.map(f => f.id === fieldId ? { ...f, ...partial } : follow(f)),
    })));
  };

  const deleteField = (sectionId: string, fieldId: string) => {
    update(sections.map(s =>
      s.id === sectionId ? { ...s, fields: s.fields.filter(f => f.id !== fieldId) } : s
    ));
    if (selectedFieldId === fieldId) setSelectedFieldId(null);
  };

  const duplicateField = (sectionId: string, fieldId: string) => {
    const sec = sections.find(s => s.id === sectionId);
    const idx = sec?.fields.findIndex(f => f.id === fieldId) ?? -1;
    if (!sec || idx < 0) return;
    const src = sec.fields[idx];
    const label = `${src.label} (copy)`;
    const copy: FormFieldDef = {
      ...JSON.parse(JSON.stringify(src)), id: `field-${Date.now()}`, label,
      field_key: keyFromLabel(label, allKeys()),
    };
    insertField(sectionId, copy, idx + 1);
  };

  /** Move a field to `index` in `toSectionId` (index counted before the move). */
  const moveFieldTo = (fromSectionId: string, fieldId: string, toSectionId: string, index: number) => {
    const from = sections.find(s => s.id === fromSectionId);
    const field = from?.fields.find(f => f.id === fieldId);
    if (!from || !field) return;
    const fromIdx = from.fields.indexOf(field);
    const target = fromSectionId === toSectionId && fromIdx < index ? index - 1 : index;
    if (fromSectionId === toSectionId && target === fromIdx) return;
    update(sections.map(s => {
      let fields = s.fields;
      if (s.id === fromSectionId) fields = fields.filter(f => f.id !== fieldId);
      if (s.id === toSectionId) { fields = [...fields]; fields.splice(target, 0, field); }
      return fields === s.fields ? s : { ...s, fields };
    }));
  };

  const moveField = (sectionId: string, fieldId: string, dir: -1 | 1) => {
    const sec = sections.find(s => s.id === sectionId);
    const idx = sec?.fields.findIndex(f => f.id === fieldId) ?? -1;
    if (!sec || idx < 0) return;
    const to = idx + dir;
    if (to < 0 || to >= sec.fields.length) return;
    moveFieldTo(sectionId, fieldId, sectionId, dir === 1 ? to + 1 : to);
  };

  const drop = (e: React.DragEvent, sectionId: string, index: number) => {
    e.preventDefault();
    e.stopPropagation();
    setDropAt(null);
    const item = readDrag(e);
    if (!item || locked(sectionId)) return;          // a linked section's fields come from its source
    if (item.kind === "type") addField(sectionId, item.type, index);
    else moveFieldTo(item.sectionId, item.fieldId, sectionId, index);
  };

  // Find selected field
  let selectedField: FormFieldDef | null = null;
  let selectedSectionId: string | null = null;
  if (selectedFieldId) {
    for (const s of sections) {
      const f = s.fields.find(f => f.id === selectedFieldId);
      if (f) { selectedField = f; selectedSectionId = s.id; break; }
    }
  }

  return (
    <div style={{ display: "flex", gap: "var(--space-lg)", height: "100%" }}>
      {editingReusable && (
        <ReusableSectionEditor sectionId={editingReusable} onClose={() => setEditingReusable(null)}
          onSaved={rs => {
            // The server already updated every saved form linking it; this form's copy follows now too.
            update(sections.map(sec => sec.section_ref?.id === rs.id
              ? { ...sec, fields: JSON.parse(JSON.stringify(rs.section.fields || [])), section_ref: { id: rs.id, version: rs.version } } : sec));
            setReusableRefresh(n => n + 1);
          }} />
      )}
      {/* Left: Canvas */}
      <div style={{ flex: 1, overflow: "auto", minWidth: 0 }} onClick={() => setSelectedFieldId(null)}>
        {sections.length === 0 ? (
          <EmptyState
            title="No sections yet"
            description="Add a section to start building your form."
            action={<Button onClick={addSection}>+ Add Section</Button>}
          />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)", maxWidth: 860 }}>
            {sorted.map((section, si) => (
              <SectionCard
                key={section.id}
                section={section}
                isFirst={si === 0}
                isLast={si === sorted.length - 1}
                selectedFieldId={selectedFieldId}
                samples={samples}
                columns={definition.settings?.columns}
                stepNumber={definition.settings?.mode === "steps" ? si + 1 : undefined}
                onUpdateSection={(p) => updateSection(section.id, p)}
                dropIndex={dropAt?.sectionId === section.id ? dropAt.index : null}
                onSelectField={setSelectedFieldId}
                onUpdateTitle={(title) => updateSection(section.id, { title })}
                onDelete={() => deleteSection(section.id)}
                onMove={(dir) => moveSection(section.id, dir)}
                onAddField={(type) => addField(section.id, type)}
                onDeleteField={(fid) => deleteField(section.id, fid)}
                onDuplicateField={(fid) => duplicateField(section.id, fid)}
                onMoveField={(fid, dir) => moveField(section.id, fid, dir)}
                onDragOverAt={(index) => setDropAt(prev =>
                  prev?.sectionId === section.id && prev.index === index ? prev : { sectionId: section.id, index })}
                onDragLeave={() => setDropAt(null)}
                onDropAt={(e, index) => drop(e, section.id, index)}
                onEditLinked={() => section.section_ref && setEditingReusable(section.section_ref.id)}
                onUnlink={() => updateSection(section.id, { section_ref: undefined })}
                onSaveReusable={async name => {
                  const r = await saveAsReusable(section, name);
                  updateSection(section.id, { section_ref: { id: r.id, version: r.version } });
                  setReusableRefresh(n => n + 1);
                }}
              />
            ))}
            <button onClick={e => { e.stopPropagation(); addSection(); }} style={{
              padding: "var(--space-md)", border: "2px dashed var(--border-default)",
              borderRadius: "var(--radius-md)", background: "transparent",
              color: "var(--text-muted)", cursor: "pointer", fontSize: 13,
              fontFamily: "var(--font-body)", transition: "all 0.15s",
            }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = "var(--accent)"; e.currentTarget.style.color = "var(--accent)"; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = "var(--border-default)"; e.currentTarget.style.color = "var(--text-muted)"; }}
            >+ Add Section</button>
          </div>
        )}
      </div>

      {/* Right: the selected field's settings, or the palette */}
      <div style={{ width: 340, flexShrink: 0, display: "flex", flexDirection: "column", gap: "var(--space-md)", overflow: "auto" }}>
        {selectedField && selectedSectionId ? (
          <FieldSettings
            key={selectedField.id}
            field={selectedField}
            fields={sections.flatMap(s => s.fields)}
            sections={sorted.map(s => ({ id: s.id, title: s.title }))}
            variables={variables}
            formId={formId}
            takenKeys={allKeys(selectedField.id)}
            savedKey={savedKeyById.get(selectedField.id)}
            onUpdate={(partial) => updateField(selectedSectionId!, selectedField!.id, partial)}
            onClose={() => setSelectedFieldId(null)}
          />
        ) : (
          <div>
            <FormSettingsPanel settings={definition.settings ?? {}} sections={sorted.length}
              onChange={settings => onChange({ ...definition, settings: Object.keys(settings).length ? settings : undefined })} />
            <TranslationsPanel definition={definition} onChange={onChange} />
            <TestsPanel definition={definition} onChange={onChange} onRecord={onRecordTest} />
            <div style={{
              fontSize: 10, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase",
              letterSpacing: "0.08em", fontFamily: "var(--font-mono)", marginBottom: "var(--space-sm)",
            }}>Field Types</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>
              Drag onto the form, or click to add to the last section. Click a field to change its settings.
            </div>
            {FIELD_GROUPS.map(group => {
            const types = FIELD_TYPES.filter(ft => ft.group === group && !LEGACY_TYPES.has(ft.type));
            const isOpen = openGroups.has(group);
            return (
            <div key={group} role="group" aria-label={group} style={{ marginBottom: 6 }}>
            <button type="button" onClick={() => toggleGroup(group)} aria-expanded={isOpen} style={{
              display: "flex", alignItems: "center", gap: 6, width: "100%", background: "none", border: "none",
              padding: "4px 0", cursor: "pointer", textAlign: "left",
              fontSize: 10, fontWeight: 600, color: "var(--text-muted)",
            }}>
              <span aria-hidden style={{ display: "inline-block", width: 8, transition: "transform 0.12s", transform: isOpen ? "rotate(90deg)" : "none" }}>›</span>
              <span style={{ flex: 1 }}>{group}</span>
              {!isOpen && <span style={{ fontWeight: 400 }}>{types.length}</span>}
            </button>
            {isOpen && (
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 4, marginTop: 2 }}>
              {types.map(ft => (
                <div
                  key={ft.type}
                  draggable
                  role="button"
                  tabIndex={0}
                  onDragStart={e => {
                    e.dataTransfer.setData(DRAG_MIME, JSON.stringify({ kind: "type", type: ft.type }));
                    e.dataTransfer.setData("field_type", ft.type);
                  }}
                  onClick={() => sorted.length ? addField(sorted[sorted.length - 1].id, ft.type) : undefined}
                  onKeyDown={e => { if ((e.key === "Enter" || e.key === " ") && sorted.length) addField(sorted[sorted.length - 1].id, ft.type); }}
                  style={{
                    padding: "8px 4px", fontSize: 10,
                    fontFamily: "var(--font-mono)", color: "var(--text-secondary)",
                    background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
                    borderRadius: "var(--radius-sm)", cursor: "grab",
                    transition: "all 0.1s",
                  }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = "var(--accent)"; e.currentTarget.style.color = "var(--accent)"; }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = "var(--border-subtle)"; e.currentTarget.style.color = "var(--text-secondary)"; }}
                >
                  <div style={{ fontSize: 16, marginBottom: 2 }}>{ft.icon}</div>
                  {ft.label}
                </div>
              ))}
            </div>
            )}
            </div>
            );
            })}
            <ReusableSectionsPanel refreshKey={reusableRefresh}
              onInsert={rs => update([...sorted, linkedSection(rs, sorted.length)])} />
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Form settings ──────────────────────────────────────────────── */

function FormSettingsPanel({ settings, sections, onChange }: {
  settings: FormSettings; sections: number; onChange: (s: FormSettings) => void;
}) {
  const [open, setOpen] = useState(false);
  const set = (p: Partial<FormSettings>) => {
    const next: any = { ...settings, ...p };
    for (const k of Object.keys(next)) if (next[k] === undefined) delete next[k];
    onChange(next);
  };
  const seg = (value: string, current: string, label: string, onClick: () => void) => (
    <button key={value} onClick={onClick} aria-pressed={value === current} style={{
      padding: "3px 10px", fontSize: 11, border: "none", cursor: "pointer",
      background: value === current ? "var(--accent)" : "transparent", color: value === current ? "#fff" : "var(--text-secondary)",
    }}>{label}</button>
  );
  return (
    <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", marginBottom: "var(--space-md)", background: "var(--bg-card)" }}>
      <button onClick={() => setOpen(o => !o)} style={{
        width: "100%", textAlign: "left", background: "none", border: "none", padding: "8px 12px", cursor: "pointer",
        fontSize: 12, fontWeight: 600, color: "var(--text-primary)",
      }}>{open ? "▾" : "▸"} Form settings
        <span style={{ fontWeight: 400, color: "var(--text-muted)", marginLeft: 6 }}>
          {settings.mode === "steps" ? `step by step · ${sections} page${sections !== 1 ? "s" : ""}` : "one page"}
          {settings.columns && settings.columns > 1 ? ` · ${settings.columns} columns` : ""}
        </span>
      </button>
      {open && (
        <div style={{ padding: "0 12px 12px", fontSize: 12 }}>
          <div style={{ marginBottom: 8 }}>
            <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: 3 }}>Layout</div>
            <div style={{ display: "inline-flex", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", overflow: "hidden" }}>
              {seg("single", settings.mode ?? "single", "One page", () => set({ mode: undefined, review_page: undefined }))}
              {seg("steps", settings.mode ?? "single", "Step by step", () => set({ mode: "steps" }))}
              {seg("tabs", settings.mode ?? "single", "Tabs", () => set({ mode: "tabs", review_page: undefined }))}
            </div>
            {settings.mode === "tabs" && (
              <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 3 }}>Each section is a tab; Submit checks every tab and opens the first with a problem.</div>
            )}
            {settings.mode === "steps" && (
              <>
                <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 3 }}>Each section is a page; Next checks that page.</div>
                <label style={{ display: "flex", gap: 4, alignItems: "center", marginTop: 6 }}>
                  <input type="checkbox" checked={!!settings.review_page} onChange={e => set({ review_page: e.target.checked || undefined })} />
                  End with a page to check the answers
                </label>
              </>
            )}
          </div>
          <div style={{ marginBottom: 8 }}>
            <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: 3 }}>Fields per row</div>
            <div style={{ display: "inline-flex", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", overflow: "hidden" }}>
              {[1, 2, 3].map(n => seg(String(n), String(settings.columns ?? 1), String(n), () => set({ columns: n === 1 ? undefined : n as 2 | 3 })))}
            </div>
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 3 }}>For fields with no width of their own; one per row on a phone.</div>
          </div>
          <div style={{ marginBottom: 8 }}>
            <label style={{ display: "flex", gap: 4, alignItems: "center" }}>
              <input type="checkbox" checked={settings.versioning === "per_case"}
                onChange={e => set({ versioning: e.target.checked ? "per_case" : undefined })} />
              Each case keeps the version of this form it first used
            </label>
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 3 }}>
              Off: every case always sees the form as it is now (as before). On: changes reach only cases that have not used the form yet.
            </div>
          </div>
          <div>
            <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: 3 }}>Submit button text</div>
            <input aria-label="Submit button text" value={settings.submit_label ?? ""} placeholder="Submit"
              onChange={e => set({ submit_label: e.target.value || undefined })}
              style={{ width: "100%", boxSizing: "border-box", padding: "5px 8px", fontSize: 12, background: "var(--bg-input)",
                border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)" }} />
          </div>
        </div>
      )}
    </div>
  );
}

/** Still drawn for forms that have them; replaced by Record details in the palette (part 8). */
export const LEGACY_TYPES = new Set(["connector_result", "json_display"]);

/* ── Section Card ─────────────────────────────────────────────── */

const SPAN: Record<string, number> = { full: 6, half: 3, third: 2 };

function SectionCard({ section, isFirst, isLast, selectedFieldId, samples, columns, stepNumber, onUpdateSection, dropIndex, onSelectField, onUpdateTitle, onDelete, onMove,
  onAddField, onDeleteField, onDuplicateField, onMoveField, onDragOverAt, onDragLeave, onDropAt, onEditLinked, onUnlink, onSaveReusable }: {
  onEditLinked?: () => void;
  onUnlink?: () => void;
  onSaveReusable?: (name: string) => Promise<void>;
  section: FormSectionDef;
  isFirst: boolean;
  isLast: boolean;
  selectedFieldId: string | null;
  samples: Record<string, any>;
  columns?: number;
  stepNumber?: number;
  onUpdateSection: (p: Partial<FormSectionDef>) => void;
  dropIndex: number | null;
  onSelectField: (id: string) => void;
  onUpdateTitle: (title: string) => void;
  onDelete: () => void;
  onMove: (dir: -1 | 1) => void;
  onAddField: (type: FormFieldDef["type"]) => void;
  onDeleteField: (id: string) => void;
  onDuplicateField: (id: string) => void;
  onMoveField: (id: string, dir: -1 | 1) => void;
  onDragOverAt: (index: number) => void;
  onDragLeave: () => void;
  onDropAt: (e: React.DragEvent, index: number) => void;
}) {
  const [editingTitle, setEditingTitle] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [reusableName, setReusableName] = useState("");
  const [reusableError, setReusableError] = useState<string | null>(null);
  const n = section.fields.length;
  const linked = !!section.section_ref;

  return (
    <div
      onDragOver={e => { e.preventDefault(); onDragOverAt(n); }}
      onDragLeave={e => { if (!e.currentTarget.contains(e.relatedTarget as Node)) onDragLeave(); }}
      onDrop={e => onDropAt(e, n)}
      style={{
        background: "var(--bg-card)", border: `1px solid ${dropIndex !== null ? "var(--accent)" : "var(--border-subtle)"}`,
        borderRadius: "var(--radius-md)", overflow: "hidden",
        transition: "border-color 0.15s",
      }}
    >
      {/* Section header */}
      <div style={{
        padding: "var(--space-sm) var(--space-md)",
        background: "var(--bg-elevated)", borderBottom: "1px solid var(--border-subtle)",
        display: "flex", alignItems: "center", justifyContent: "space-between",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)", minWidth: 0 }}>
          <MiniBtn title="Move section up" disabled={isFirst} onClick={e => { e.stopPropagation(); onMove(-1); }}>▲</MiniBtn>
          <MiniBtn title="Move section down" disabled={isLast} onClick={e => { e.stopPropagation(); onMove(1); }}>▼</MiniBtn>
          {editingTitle ? (
            <input
              autoFocus
              value={section.title}
              onClick={e => e.stopPropagation()}
              onChange={e => onUpdateTitle(e.target.value)}
              onBlur={() => setEditingTitle(false)}
              onKeyDown={e => e.key === "Enter" && setEditingTitle(false)}
              style={{
                background: "var(--bg-input)", border: "1px solid var(--border-focus)",
                borderRadius: "var(--radius-sm)", padding: "2px 6px", fontSize: 13,
                fontWeight: 600, color: "var(--text-primary)", outline: "none",
                fontFamily: "var(--font-display)",
              }}
            />
          ) : (
            <span
              onClick={e => { e.stopPropagation(); setEditingTitle(true); }}
              title="Click to rename"
              style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", fontFamily: "var(--font-display)", cursor: "text" }}
            >{section.title}</span>
          )}
          <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
            ({n} field{n !== 1 ? "s" : ""})
          </span>
          {stepNumber && <span style={{ fontSize: 10, color: "var(--accent)" }}>Page {stepNumber}</span>}
          {linked && <span style={{ fontSize: 10, color: "var(--accent)" }} title="Linked to a reusable section">🔗 linked · v{section.section_ref?.version ?? "?"}</span>}
          {section.collapsible && <span style={{ fontSize: 10, color: "var(--text-muted)" }}>· {section.collapsed ? "starts closed" : "can be closed"}</span>}
        </div>
        <div style={{ display: "flex", gap: 2 }}>
          <MiniBtn title="Section settings" onClick={e => { e.stopPropagation(); setSettingsOpen(o => !o); }}>⚙</MiniBtn>
          <MiniBtn title="Delete section" danger onClick={e => { e.stopPropagation(); onDelete(); }}>×</MiniBtn>
        </div>
      </div>
      {settingsOpen && (
        <div onClick={e => e.stopPropagation()} style={{ padding: "8px var(--space-md)", borderBottom: "1px solid var(--border-subtle)", background: "var(--bg-elevated)", fontSize: 12 }}>
          <textarea aria-label="Section description" value={section.description ?? ""} rows={2} placeholder="Text under the section title (optional)"
            onChange={e => onUpdateSection({ description: e.target.value || undefined })}
            style={{ width: "100%", boxSizing: "border-box", padding: "5px 8px", fontSize: 12, background: "var(--bg-input)",
              border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", resize: "vertical" }} />
          <div style={{ display: "flex", gap: 16, marginTop: 6 }}>
            <label style={{ display: "flex", gap: 4, alignItems: "center" }}>
              <input type="checkbox" checked={!!section.collapsible}
                onChange={e => onUpdateSection({ collapsible: e.target.checked || undefined, ...(e.target.checked ? {} : { collapsed: undefined }) })} />
              The person can close it
            </label>
            {section.collapsible && (
              <label style={{ display: "flex", gap: 4, alignItems: "center" }}>
                <input type="checkbox" checked={!!section.collapsed} onChange={e => onUpdateSection({ collapsed: e.target.checked || undefined })} />
                Starts closed
              </label>
            )}
          </div>
          {!linked && onSaveReusable && n > 0 && (
            <div style={{ display: "flex", gap: 6, marginTop: 8, alignItems: "center" }}>
              <input aria-label="Reusable section name" value={reusableName} onChange={e => setReusableName(e.target.value)} maxLength={100}
                placeholder="Name, e.g. Address" style={{ flex: 1, padding: "4px 8px", fontSize: 12, background: "var(--bg-input)",
                  border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)" }} />
              <button type="button" disabled={!reusableName.trim()} onClick={() => onSaveReusable(reusableName)
                .then(() => { setReusableName(""); setReusableError(null); }, err => setReusableError(err?.message || "Not saved"))}
                style={{ padding: "4px 10px", fontSize: 11, border: "1px dashed var(--border-default)", borderRadius: "var(--radius-sm)",
                  background: "transparent", color: "var(--text-secondary)", cursor: "pointer" }}>Save as reusable section</button>
            </div>
          )}
          {reusableError && <div style={{ fontSize: 11, color: "var(--status-failed)", marginTop: 4 }}>{reusableError}</div>}
        </div>
      )}
      {section.description && (
        <div style={{ padding: "6px var(--space-md) 0", fontSize: 12, color: "var(--text-muted)" }}>{section.description}</div>
      )}

      {/* Fields, laid out as the form will be */}
      <div style={{ padding: "var(--space-md)" }}>
        {linked ? (
          <div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
              {section.fields.map(f => (
                <span key={f.id} style={{ fontSize: 11, padding: "3px 8px", borderRadius: 10, border: "1px solid var(--border-subtle)",
                  color: "var(--text-secondary)", background: "var(--bg-elevated)" }}>{f.label || f.field_key}</span>
              ))}
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8, fontSize: 11, color: "var(--text-muted)" }}>
              <span style={{ flex: 1 }}>These fields come from the reusable section; change them there and every linked form follows.</span>
              <button type="button" onClick={e => { e.stopPropagation(); onEditLinked?.(); }} style={linkBtn}>Edit section</button>
              <button type="button" onClick={e => { e.stopPropagation(); onUnlink?.(); }} style={linkBtn}
                title="Keep these fields in this form only, no longer linked">Unlink</button>
            </div>
          </div>
        ) : n === 0 ? (
          <div style={{
            padding: "var(--space-lg)", color: "var(--text-muted)", fontSize: 12, textAlign: "center",
            border: `1px dashed ${dropIndex !== null ? "var(--accent)" : "var(--border-default)"}`, borderRadius: "var(--radius-sm)",
          }}>
            Drag a field type here, or use the buttons below
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(6, minmax(0, 1fr))", gap: "var(--space-sm)" }}>
            {section.fields.map((field, idx) => (
              <FieldCard
                key={field.id}
                field={field}
                span={field.width ? SPAN[field.width] ?? 6 : 6 / (columns && [1, 2, 3].includes(columns) ? columns : 1)}
                samples={samples}
                selected={selectedFieldId === field.id}
                dropBefore={dropIndex === idx}
                isFirst={idx === 0}
                isLast={idx === n - 1}
                onSelect={() => onSelectField(field.id)}
                onDelete={() => onDeleteField(field.id)}
                onDuplicate={() => onDuplicateField(field.id)}
                onMoveUp={() => onMoveField(field.id, -1)}
                onMoveDown={() => onMoveField(field.id, 1)}
                onDragStart={e => {
                  e.dataTransfer.effectAllowed = "move";
                  e.dataTransfer.setData(DRAG_MIME, JSON.stringify({ kind: "field", sectionId: section.id, fieldId: field.id }));
                }}
                onDragOver={e => { e.preventDefault(); e.stopPropagation(); onDragOverAt(idx); }}
                onDrop={e => onDropAt(e, idx)}
              />
            ))}
            {dropIndex === n && <div style={{ gridColumn: "span 6", height: 3, background: "var(--accent)", borderRadius: 2 }} />}
          </div>
        )}

        {/* Quick-add buttons */}
        {!linked && <div style={{ display: "flex", gap: 4, marginTop: "var(--space-sm)", flexWrap: "wrap" }}>
          {(["text", "number", "dropdown", "checkbox", "textarea", "date"] as const).map(type => (
            <button key={type} onClick={e => { e.stopPropagation(); onAddField(type); }} style={{
              padding: "3px 8px", fontSize: 11,
              background: "transparent", border: "1px dashed var(--border-default)",
              borderRadius: "var(--radius-sm)", color: "var(--text-muted)",
              cursor: "pointer",
            }}
              onMouseEnter={e => e.currentTarget.style.borderColor = "var(--accent)"}
              onMouseLeave={e => e.currentTarget.style.borderColor = "var(--border-default)"}
            >+ {FIELD_TYPES.find(t => t.type === type)?.label}</button>
          ))}
        </div>}
      </div>
    </div>
  );
}

const linkBtn: React.CSSProperties = {
  padding: "3px 10px", fontSize: 11, background: "transparent", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", cursor: "pointer",
};

/* ── Field Card — the field as it will look ───────────────────── */

function FieldCard({ field, span, samples, selected, dropBefore, isFirst, isLast, onSelect, onDelete, onDuplicate, onMoveUp, onMoveDown,
  onDragStart, onDragOver, onDrop }: {
  field: FormFieldDef; span: number; samples: Record<string, any>; selected: boolean; dropBefore: boolean;
  isFirst: boolean; isLast: boolean;
  onSelect: () => void; onDelete: () => void; onDuplicate: () => void;
  onMoveUp: () => void; onMoveDown: () => void;
  onDragStart: (e: React.DragEvent) => void;
  onDragOver: (e: React.DragEvent) => void;
  onDrop: (e: React.DragEvent) => void;
}) {
  const [hover, setHover] = useState(false);
  // Required already shows as the red asterisk on the label.
  const badges = [
    typeof field.calculation === "string" && "ƒ Calculated",
    (field.read_only || field.read_only_when) && "Read-only",
    (field.show_when?.rules?.length || field.show_when?.expression || field.conditions?.length
      || field.required_when || field.read_only_when) && "Conditional",
    field.checks?.length && "Checks",
    field.hidden && "Hidden",
  ].filter(Boolean) as string[];

  return (
    <div
      draggable
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDrop={onDrop}
      onClick={e => { e.stopPropagation(); onSelect(); }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      data-testid={`field-card-${field.field_key}`}
      style={{
        gridColumn: `span ${span}`,
        position: "relative", padding: "10px 12px", borderRadius: "var(--radius-sm)",
        background: selected ? "var(--accent-dim)" : hover ? "var(--bg-card-hover)" : "transparent",
        border: `1px solid ${selected ? "var(--accent)" : hover ? "var(--border-default)" : "var(--border-subtle)"}`,
        boxShadow: dropBefore ? "inset 0 3px 0 var(--accent)" : undefined,
        cursor: "grab", opacity: field.hidden ? 0.6 : 1, minWidth: 0,
      }}
    >
      {(hover || selected) && (
        <div style={{ position: "absolute", top: 4, right: 4, display: "flex", gap: 2, zIndex: 1,
          background: "var(--bg-card)", borderRadius: 4 }}>
          {!isFirst && <MiniBtn title="Move up" onClick={e => { e.stopPropagation(); onMoveUp(); }}>▲</MiniBtn>}
          {!isLast && <MiniBtn title="Move down" onClick={e => { e.stopPropagation(); onMoveDown(); }}>▼</MiniBtn>}
          <MiniBtn title="Duplicate" onClick={e => { e.stopPropagation(); onDuplicate(); }}>⧉</MiniBtn>
          <MiniBtn title="Delete field" danger onClick={e => { e.stopPropagation(); onDelete(); }}>×</MiniBtn>
        </div>
      )}
      {badges.length > 0 && (
        <div style={{ display: "flex", gap: 4, marginBottom: 4 }}>
          {badges.map(b => (
            <span key={b} style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3,
              background: "var(--bg-elevated)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.04em" }}>{b}</span>
          ))}
        </div>
      )}
      {/* The real renderer, inert: what the person will see, nothing to click into. */}
      <div style={{ pointerEvents: "none" }} aria-hidden>
        <FieldRenderer field={field} value={field.default_value} allValues={samples} onChange={() => {}} readOnly preview />
      </div>
    </div>
  );
}

function MiniBtn({ children, onClick, danger, title, disabled }: {
  children: React.ReactNode; onClick: (e: React.MouseEvent) => void; danger?: boolean; title?: string; disabled?: boolean;
}) {
  return (
    <button onClick={onClick} title={title} aria-label={title} disabled={disabled} style={{
      width: 20, height: 20, display: "flex", alignItems: "center", justifyContent: "center",
      background: "transparent", border: "none", borderRadius: 2,
      color: danger ? "var(--status-failed)" : "var(--text-muted)",
      cursor: disabled ? "default" : "pointer", fontSize: 11, opacity: disabled ? 0.25 : 0.7,
    }}
      onMouseEnter={e => { if (!disabled) e.currentTarget.style.opacity = "1"; }}
      onMouseLeave={e => { if (!disabled) e.currentTarget.style.opacity = "0.7"; }}
    >{children}</button>
  );
}

export { FIELD_TYPES };
