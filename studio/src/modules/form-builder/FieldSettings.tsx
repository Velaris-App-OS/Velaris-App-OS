import React, { useState } from "react";
import type { FormFieldDef } from "./FormBuilder";
import { FIELD_TYPES, renameField } from "./FormBuilder";
import { BehaviourEditor, ChecksEditor, FieldSummary, ValueEditor } from "./LogicEditors";
import ReferenceEditor from "./ReferenceEditor";
import { phoneCountry, safePattern } from "./validate";
import { SecurityEditor } from "./Step7Editors";
import LookupEditor, { ConnectorOperationField } from "./LookupEditor";
import DisplayEditor from "./DisplayEditor";
import { codeOptions } from "./SmallControls";
import { EMBED_PERMISSIONS, embedRefusal, useEmbedAllowsForms, useEmbedOrigins } from "./Embed";
import { safeLink } from "./BatchM";
import FormulaEditor from "./FormulaEditor";
import type { FormVariable } from "@shared/api/client";

/* ═══════════════════════════════════════════════════════════════════
   FieldSettings — the settings panel for the selected field
   (Form Builder redesign part 2, 2026-10-28).

   Tabs General / Validation / Data / Logic / Appearance, each showing only
   what applies to the field's type. Replaces the flat property list. Settings
   the later parts add (per-type rules, option sources, show/hide rules,
   lookups) land in these tabs.
   ═══════════════════════════════════════════════════════════════════ */

type Tab = "general" | "validation" | "data" | "logic" | "appearance";

const TABS: { key: Tab; label: string }[] = [
  { key: "general",    label: "General" },
  { key: "validation", label: "Validation" },
  { key: "data",       label: "Data" },
  { key: "logic",      label: "Logic" },
  { key: "appearance", label: "Appearance" },
];

/** Types that show data rather than take input: no default, no required. */
export const DISPLAY_TYPES = new Set(["connector_lookup", "connector_result", "json_display", "table_display", "record_details",
  "heading", "paragraph", "divider", "callout", "embed", "button"]);
/** Words, a line, a box, an embedded page or a button — no value at all (2026-10-29). */
const STATIC_TYPES = new Set(["heading", "paragraph", "divider", "callout", "embed", "button"]);
const PICKERS = new Set(["user_picker", "case_picker", "document_picker"]);
const CHOICE_TYPES = new Set(["dropdown", "radio", "multi_select"]);
const TEXT_TYPES = new Set(["text", "textarea", "email", "phone", "url"]);
const PLACEHOLDER_TYPES = new Set([...TEXT_TYPES, "number", "currency", "dropdown", "percentage", "tags", "iban", "country", "currency_code",
  "rich_text", "user_picker", "case_picker", "document_picker"]);

/** A field key made from a label: lower case, letters/digits/underscore, unique in the form. */
export function keyFromLabel(label: string, taken: Set<string>): string {
  let base = label.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60);
  if (!base || !/^[a-z]/.test(base)) base = `field_${base || "x"}`;
  let key = base;
  for (let n = 2; taken.has(key); n++) key = `${base}_${n}`;
  return key;
}

export default function FieldSettings({ field, fields = [], sections = [], variables, formId, takenKeys, savedKey, onUpdate, onClose, backLabel }: {
  field: FormFieldDef;
  /** The form's sections — a button can go to one. */
  sections?: { id: string; title: string }[];
  /** A repeating group's column: the close button goes back to the group. */
  backLabel?: string;
  formId?: string;
  /** Case variables of the case types using this form; undefined while loading or unsaved. */
  variables?: { case_types: { id: string; name: string }[]; variables: FormVariable[] } | null;
  /** Every field on the form — show/hide rules pick from them. */
  fields?: FormFieldDef[];
  /** Keys of the form's other fields — a key must be unique. */
  takenKeys: Set<string>;
  /** This field's key in the saved form, if it was saved — data submitted
   *  with the form is stored under it, so the key no longer follows the label. */
  savedKey?: string;
  onUpdate: (partial: Partial<FormFieldDef>) => void;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<Tab>("general");
  const [advanced, setAdvanced] = useState(false);
  // A repeating group's column being edited — the same panel, one level in.
  const [childId, setChildId] = useState<string | null>(null);
  const isGroup = field.type === "repeating_group";
  const children: FormFieldDef[] = isGroup && Array.isArray(field.fields) ? field.fields : [];
  const child = isGroup && childId ? children.find(c => c.id === childId) : undefined;
  if (child) {
    const others = new Set(children.filter(c => c.id !== child.id).map(c => c.field_key));
    return (
      <FieldSettings key={child.id} field={child} formId={formId} backLabel={`← ${field.label || "Group"}`}
        // A row's formulas see its own columns first, then the rest of the form (not the group itself).
        fields={[...children, ...fields.filter(f => f.field_key !== field.field_key && !others.has(f.field_key) && f.field_key !== child.field_key)]}
        takenKeys={others}
        // A renamed column: its siblings' formulas and rules follow it.
        onUpdate={p => {
          const renamed = p.field_key !== undefined && p.field_key !== child.field_key ? p.field_key : null;
          onUpdate({ fields: children.map(c => (c.id === child.id ? { ...c, ...p }
            : renamed ? renameField(c, child.field_key, renamed) : c)) });
        }}
        onClose={() => setChildId(null)} />
    );
  }
  const isDisplay = DISPLAY_TYPES.has(field.type);
  const typeLabel = FIELD_TYPES.find(t => t.type === field.type)?.label ?? field.type;
  const v = field.validation || {};
  const setV = (partial: Record<string, any>) => onUpdate({ validation: { ...v, ...partial } });
  const optNum = (s: string) => (s.trim() === "" || isNaN(Number(s)) ? undefined : Number(s));

  // The key follows the label until somebody sets it by hand.
  const autoKey = field.field_key === keyFromLabel(field.label, takenKeys) || /^field_\d+$/.test(field.field_key);
  const changeLabel = (label: string) =>
    onUpdate(autoKey && !savedKey
      ? { label, field_key: keyFromLabel(label || "field", takenKeys) }
      : { label });
  const keyClash = takenKeys.has(field.field_key);
  const keyRenamedFromSaved = !!savedKey && savedKey !== field.field_key;

  return (
    <div style={{
      background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
      borderRadius: "var(--radius-md)", display: "flex", flexDirection: "column", minHeight: 0,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "10px 12px 0" }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {field.label || "Untitled field"}
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{typeLabel}</div>
        </div>
        {backLabel ? (
          <button onClick={onClose} style={{ background: "transparent", border: "none", color: "var(--accent)", cursor: "pointer", fontSize: 12 }}>
            {backLabel}
          </button>
        ) : (
          <button onClick={onClose} aria-label="Close settings" style={{
            background: "transparent", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 16, padding: 2,
          }}>×</button>
        )}
      </div>

      <FieldSummary field={field} fields={fields} />

      <div role="tablist" style={{ display: "flex", gap: 2, padding: "8px 8px 0", borderBottom: "1px solid var(--border-subtle)", overflowX: "auto" }}>
        {TABS.map(t => (
          <button key={t.key} role="tab" aria-selected={tab === t.key} onClick={() => setTab(t.key)} style={{
            padding: "6px 8px", fontSize: 12, fontWeight: 500, background: "none", border: "none",
            borderBottom: `2px solid ${tab === t.key ? "var(--accent)" : "transparent"}`, marginBottom: -1,
            color: tab === t.key ? "var(--text-primary)" : "var(--text-muted)", cursor: "pointer", whiteSpace: "nowrap",
          }}>{t.label}</button>
        ))}
      </div>

      <div style={{ padding: 12, overflow: "auto" }}>
        {tab === "general" && (
          <>
            <Prop label="Label">
              <TextIn value={field.label} onChange={changeLabel} />
            </Prop>
            <Prop label="Help text" hint="Shown under the label.">
              <TextIn value={field.description || ""} onChange={val => onUpdate({ description: val })} />
            </Prop>
            {PLACEHOLDER_TYPES.has(field.type) && (
              <Prop label="Placeholder" hint={field.type === "dropdown" ? "Shown before anything is chosen." : "Shown in the empty box."}>
                <TextIn value={field.placeholder || ""} onChange={val => onUpdate({ placeholder: val })} />
              </Prop>
            )}
            {(field.type === "paragraph" || field.type === "callout") && (
              <Prop label="Text" hint="Plain text; line breaks are kept.">
                <textarea value={(field as any).text ?? ""} rows={4} onChange={e => onUpdate({ text: e.target.value } as any)}
                  style={{ ...inputStyle, resize: "vertical" }} />
              </Prop>
            )}
            {field.type === "callout" && (
              <Prop label="Kind">
                <Segmented value={(field as any).tone ?? "info"} onChange={t => onUpdate({ tone: t } as any)}
                  options={[["info", "Info"], ["success", "Success"], ["warning", "Warning"], ["error", "Error"]]} />
              </Prop>
            )}
            {field.type === "embed" && <EmbedSettings field={field} onUpdate={onUpdate} />}
            {field.type === "button" && <ButtonSettings field={field} fields={fields} sections={sections} onUpdate={onUpdate} />}
            {field.type === "address" && <Muted>Street, town, region, postcode and country. The postcode is checked for the country where we know its format.</Muted>}
            {field.type === "signature" && <Muted>The person draws a signature; it is stored as a small image with the answers.</Muted>}
            {field.type === "user_picker" && <Muted>Searches the active people of your tenant. The server accepts only one of them.</Muted>}
            {field.type === "case_picker" && <Muted>Searches your tenant's cases by number. The server accepts only one of them.</Muted>}
            {field.type === "document_picker" && <Muted>Lists the documents of the case the form is opened on. Not available on the customer portal.</Muted>}
            {STATIC_TYPES.has(field.type) && field.type !== "embed" && field.type !== "button" && (
              <Muted>{field.type === "divider" ? "A line between parts of the form." : field.type === "heading" ? "The label is the heading."
                : "Shown as written; it holds no value."}</Muted>
            )}
            {field.type === "consent" && (
              <Prop label="Terms" hint="Shown in a box the person can scroll. Plain text.">
                <textarea value={(field as any).terms ?? ""} rows={5} onChange={e => onUpdate({ terms: e.target.value || undefined } as any)}
                  style={{ ...inputStyle, resize: "vertical" }} />
              </Prop>
            )}
            {(field.type === "checkbox" || field.type === "consent") && (
              <Prop label="Text next to the box">
                <TextIn value={field.placeholder || ""} onChange={val => onUpdate({ placeholder: val })} />
              </Prop>
            )}
            {isGroup && <GroupColumns field={field} onUpdate={onUpdate} onEdit={setChildId} />}
            {field.type === "location" && (
              <Muted>A map pin, stored as latitude and longitude. The map shows when this install serves its own map tiles; otherwise the person enters coordinates or uses their device's location.</Muted>
            )}
            {!isDisplay && !isGroup && !NO_DEFAULT.has(field.type) && !CHOICE_TYPES.has(field.type) && (
              <ValueEditor field={field} fields={fields} onUpdate={onUpdate}>
                <Prop label="Default value" hint="Filled in when the form opens.">
                  <DefaultValueInput field={field} onChange={val => onUpdate({ default_value: val })} />
                </Prop>
              </ValueEditor>
            )}
            {CHOICE_TYPES.has(field.type) && <OptionsEditor field={field} fields={fields} onUpdate={onUpdate} />}

            <Prop label="Field type">
              <Select value={field.type} onChange={val => onUpdate({ type: val as FormFieldDef["type"] })}
                options={FIELD_TYPES.map(t => [t.type, t.label])} />
            </Prop>

            <button onClick={() => setAdvanced(a => !a)} style={linkBtn}>
              {advanced ? "▾" : "▸"} Advanced
            </button>
            {advanced && (
              <Prop label="Field key" hint="Where the value is stored in the case. Made from the label until you change it.">
                <TextIn mono value={field.field_key} onChange={val => onUpdate({ field_key: val.replace(/[^A-Za-z0-9_.-]/g, "_") })} />
                {keyClash && <Warn>Another field already uses this key.</Warn>}
                {keyRenamedFromSaved && <Warn>Data already submitted with this form stays under the old key.</Warn>}
              </Prop>
            )}
          </>
        )}

        {tab === "validation" && (
          <>
            {!isDisplay && (
              <>
                <Check label="Required" checked={field.required} onChange={c => onUpdate({ required: c, ...(c ? { required_when: undefined } : {}) } as any)}
                  hint={(field as any).required_when ? "Now required only when its condition holds (Logic tab)." : undefined} />
                {field.required && (
                  <Prop label="Message when empty" hint={`Default: "${field.label || "This field"} is required"`}>
                    <TextIn value={field.required_message || ""} onChange={val => onUpdate({ required_message: val || undefined })} />
                  </Prop>
                )}
              </>
            )}
            {TEXT_TYPES.has(field.type) && (
              <>
                <Row>
                  <Prop label="Min length"><NumIn value={v.min_length} onChange={s => setV({ min_length: optNum(s) })} /></Prop>
                  <Prop label="Max length"><NumIn value={v.max_length} onChange={s => setV({ max_length: optNum(s) })} /></Prop>
                </Row>
                {field.type === "email" && (
                  <>
                    <Check label="Check the full address format" checked={v.format === "strict"}
                      onChange={c => setV({ format: c ? "strict" : undefined })}
                      hint="name@domain.tld — otherwise only the @ is checked, as forms made before October 2026 did." />
                    <Prop label="Allowed domains" hint="Comma separated, e.g. example.com. Sub-domains are included. Empty: any.">
                      <ListIn value={v.allowed_domains} onChange={list => setV({ allowed_domains: list })} />
                    </Prop>
                  </>
                )}
                {field.type === "phone" && (
                  <>
                    <Prop label="Country" hint="Set: the number is checked for this country and saved in international form (+44…), and people see a country selector. Not set: only the format below is checked, as before.">
                      <Select value={(field as any).phone?.default_country || ""}
                        onChange={c => onUpdate({ phone: c ? { ...((field as any).phone || {}), default_country: c } : undefined } as any)}
                        options={[["", "Not set — check the format only"], ...codeOptions("country").filter(o => phoneCountry(o.value)).map(o => [o.value, `${o.label} (${o.value})`] as [string, string])]} />
                    </Prop>
                    {(field as any).phone?.default_country && (
                      <Prop label="Only numbers from" hint="Country codes, comma separated (e.g. GB, IE). Matched by dialling code: GB allows every +44 number. Empty: any country.">
                        <ListIn value={(field as any).phone?.countries}
                          onChange={list => onUpdate({ phone: { ...(field as any).phone, countries: list?.map(c => c.toUpperCase()) } } as any)} />
                      </Prop>
                    )}
                  </>
                )}
                {field.type !== "email" && !(field.type === "phone" && (field as any).phone?.default_country)
                  && <PatternEditor v={v} setV={setV} phone={field.type === "phone"} />}
              </>
            )}
            {field.type === "percentage" && <Muted>0 to 100 unless you set your own limits.</Muted>}
            {["number", "currency", "slider", "percentage"].includes(field.type) && (
              <Row>
                <Prop label="Min"><NumIn value={v.min} onChange={s => setV({ min: optNum(s) })} /></Prop>
                <Prop label="Max"><NumIn value={v.max} onChange={s => setV({ max: optNum(s) })} /></Prop>
                {field.type === "slider"
                  ? <Prop label="Step"><NumIn value={v.step} onChange={s => setV({ step: optNum(s) })} /></Prop>
                  : <Prop label="Decimals"><NumIn value={v.decimals} onChange={s => setV({ decimals: optNum(s) })} /></Prop>}
              </Row>
            )}
            {field.type === "time" && (
              <Row>
                <Prop label="Earliest" hint="HH:MM, e.g. 09:00">
                  <TextIn value={v.earliest ?? ""} onChange={val => setV({ earliest: val.trim() || undefined })} />
                </Prop>
                <Prop label="Latest">
                  <TextIn value={v.latest ?? ""} onChange={val => setV({ latest: val.trim() || undefined })} />
                </Prop>
              </Row>
            )}
            {field.type === "time" && [v.earliest, v.latest].some(b => b && !/^([01]\d|2[0-3]):[0-5]\d$/.test(String(b).trim())) && (
              <Warn>Use a 24-hour time, e.g. 09:00 or 17:30.</Warn>
            )}
            {field.type === "date_range" && (
              <Prop label="At most (days)" hint="Counting the first and the last day.">
                <NumIn value={v.max_days} onChange={s => setV({ max_days: optNum(s) })} />
              </Prop>
            )}
            {field.type === "tags" && (
              <Row>
                <Prop label="At most (tags)"><NumIn value={v.max_tags} onChange={s => setV({ max_tags: optNum(s) })} /></Prop>
                <Prop label="Longest tag" hint="Characters; 50 if empty."><NumIn value={v.max_tag_length} onChange={s => setV({ max_tag_length: optNum(s) })} /></Prop>
              </Row>
            )}
            {field.type === "iban" && <Muted>The country's length and the check digits are always checked. Stored without spaces.</Muted>}
            {(field.type === "country" || field.type === "currency_code") && (
              <Muted>The value is the ISO code ({field.type === "country" ? "DE, FR, GB…" : "EUR, USD, GBP…"}), always one from the built-in list.</Muted>
            )}
            {field.type === "url" && <Muted>Must start with http:// or https://.</Muted>}
            {field.type === "rich_text" && (
              <Prop label="Max length" hint="Characters, at most 10,000.">
                <NumIn value={v.max_length} onChange={s => setV({ max_length: optNum(s) })} />
              </Prop>
            )}
            {field.type === "consent" && <Muted>Required: the form cannot be sent until the person agrees.</Muted>}
            {(field.type === "date" || field.type === "datetime" || field.type === "date_range") && (
              <>
                <Check label="No dates in the past" checked={!!v.no_past} onChange={c => setV({ no_past: c || undefined })} />
                <Check label="No dates in the future" checked={!!v.no_future} onChange={c => setV({ no_future: c || undefined })} />
                <Row>
                  <Prop label="Earliest" hint="2026-01-01, today, today+7, today-30">
                    <TextIn value={v.earliest ?? ""} onChange={val => setV({ earliest: val.trim() || undefined })} />
                  </Prop>
                  <Prop label="Latest">
                    <TextIn value={v.latest ?? ""} onChange={val => setV({ latest: val.trim() || undefined })} />
                  </Prop>
                </Row>
                {[v.earliest, v.latest].some(b => b && !/^(today([+-]\d{1,4})?|\d{4}-\d{2}-\d{2})$/.test(String(b).trim())) && (
                  <Warn>Use a date (2026-01-01) or today, today+N, today-N.</Warn>
                )}
              </>
            )}
            {field.type === "checkbox" && (
              <Check label="Must be ticked" checked={!!v.must_be_ticked} onChange={c => setV({ must_be_ticked: c || undefined })}
                hint="For an agreement: the form cannot be sent unticked." />
            )}
            {field.type === "file_upload" && (
              <>
                <Prop label="Allowed file types" hint="Comma separated, e.g. .pdf, .png. Empty: any type the platform accepts.">
                  <ListIn value={v.accept} onChange={list => setV({ accept: list })} />
                </Prop>
                <Row>
                  <Prop label="Max size (MB)"><NumIn value={v.max_size_mb} onChange={s => setV({ max_size_mb: optNum(s) })} /></Prop>
                  {v.multiple && <Prop label="Max files"><NumIn value={v.max_files} onChange={s => setV({ max_files: optNum(s) })} /></Prop>}
                </Row>
                <Check label="Allow several files" checked={!!v.multiple} onChange={c => setV({ multiple: c || undefined })} />
              </>
            )}
            {isDisplay && <Muted>{STATIC_TYPES.has(field.type) ? "Text only" : "This field shows data"}; there is nothing to check.</Muted>}
            {field.type === "multi_select" && (
              <Row>
                <Prop label="Choose at least"><NumIn value={v.min_selected} onChange={s => setV({ min_selected: optNum(s) })} /></Prop>
                <Prop label="Choose at most"><NumIn value={v.max_selected} onChange={s => setV({ max_selected: optNum(s) })} /></Prop>
              </Row>
            )}
            {["rating", "dropdown", "radio", "multi_select"].includes(field.type) && (
              <Muted>The value must be one of the {field.type === "rating" ? "stars" : "options"}; that is always checked.</Muted>
            )}
            {isGroup && (
              <Row>
                <Prop label="At least (rows)"><NumIn value={field.min_rows} onChange={s => onUpdate({ min_rows: optNum(s) })} /></Prop>
                <Prop label="At most (rows)" hint="Never more than 500."><NumIn value={field.max_rows} onChange={s => onUpdate({ max_rows: optNum(s) })} /></Prop>
              </Row>
            )}
            {!isDisplay && <ChecksEditor field={field} fields={fields} onUpdate={onUpdate} />}
          </>
        )}

        {tab === "data" && (
          <>
            {field.type !== "connector_lookup" && !field.from && !STATIC_TYPES.has(field.type) && (
              <VariablePicker field={field} variables={variables} onChange={val => onUpdate({ variable: val || undefined })} />
            )}
            {field.type === "connector_lookup" && <LookupEditor field={field} fields={fields} formId={formId} onUpdate={onUpdate} />}
            {(field.type === "table_display" || field.type === "record_details") && <DisplayEditor field={field} fields={fields} onUpdate={onUpdate} />}
            {(field.type === "connector_result" || field.type === "json_display") && (
              <>
                <Muted>{field.type === "json_display" ? "JSON Display" : "Connector Result"} is an older field type: it shows raw data.</Muted>
                <button style={{ ...dashedBtn, marginTop: 8 }} onClick={() => onUpdate({ type: "record_details", show_raw: true, source: undefined } as any)}>
                  Convert to Record details
                </button>
                <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
                  Keeps its data and key; you can then choose and label the values it shows.
                </div>
              </>
            )}
          </>
        )}

        {tab === "logic" && (
          <>
            <Check label="Hidden" checked={!!field.hidden} onChange={c => onUpdate({ hidden: c || undefined })}
              hint="Not shown. Its default value is saved with the form." />
            {(field.read_only || field.hidden) && field.required && (
              <Warn>A read-only or hidden field is never required — the person cannot fill it in.</Warn>
            )}
            <div style={{ borderTop: "1px solid var(--border-subtle)", marginTop: 10, paddingTop: 10 }}>
              <BehaviourEditor field={field} fields={fields} onUpdate={onUpdate} />
              <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 6 }}>
                A field that is not shown is not required, and its value is not sent — the server applies the same rules.
              </div>
            </div>
            {!STATIC_TYPES.has(field.type) && (
              <div style={{ borderTop: "1px solid var(--border-subtle)", marginTop: 10, paddingTop: 10 }}>
                <SecurityEditor field={field} onUpdate={onUpdate} />
              </div>
            )}
          </>
        )}

        {tab === "appearance" && (
          <>
            <Prop label="Width" hint="Side by side on wide screens; full width on a phone.">
              <Segmented value={field.width ?? "full"} onChange={w => onUpdate({ width: w === "full" ? undefined : w as FormFieldDef["width"] })}
                options={[["full", "Full"], ["half", "Half"], ["third", "Third"]]} />
            </Prop>
            {(field.type === "radio" || field.type === "multi_select") && (
              <Prop label="Options laid out">
                <Segmented value={field.display?.layout ?? "down"}
                  onChange={l => onUpdate({ display: { ...field.display, layout: l === "down" ? undefined : l as "across" } })}
                  options={[["down", "Down"], ["across", "Across"]]} />
              </Prop>
            )}
            {field.type === "dropdown" && (
              <Prop label="Search box">
                <Segmented value={field.display?.searchable === undefined ? "auto" : field.display.searchable ? "on" : "off"}
                  onChange={k => onUpdate({ display: { ...field.display, searchable: k === "auto" ? undefined : k === "on" } })}
                  options={[["auto", "Above 10 options"], ["on", "Always"], ["off", "Never"]]} />
              </Prop>
            )}
            {(field.type === "text" || field.type === "textarea") && (
              <Check label="Show a character counter" checked={!!field.display?.counter}
                onChange={c => onUpdate({ display: { ...field.display, counter: c || undefined } })} />
            )}
            {field.type === "textarea" && (
              <Prop label="Height (lines)">
                <NumIn value={field.display?.rows} onChange={s => onUpdate({ display: { ...field.display, rows: optNum(s) } })} />
              </Prop>
            )}
            {field.type === "number" && (
              <Prop label="Unit" hint="Shown after the number, e.g. kg, %, km.">
                <TextIn value={v.unit ?? ""} onChange={val => setV({ unit: val || undefined })} />
              </Prop>
            )}
            {field.type === "slider" && (
              <Row>
                <Prop label="Label at the low end"><TextIn value={v.min_label ?? ""} onChange={val => setV({ min_label: val || undefined })} /></Prop>
                <Prop label="Label at the high end"><TextIn value={v.max_label ?? ""} onChange={val => setV({ max_label: val || undefined })} /></Prop>
              </Row>
            )}
            {field.type === "rating" && (
              <>
                <Prop label="Stars">
                  <Select value={String(v.max_stars ?? 5)} onChange={s => setV({ max_stars: Number(s) })}
                    options={[["3", "3 stars"], ["5", "5 stars"], ["10", "10 stars"]]} />
                </Prop>
                <Prop label="Words for each star" hint="Comma separated, low to high, e.g. Poor, Fair, Good, Very good, Excellent.">
                  <ListIn value={v.labels} onChange={list => setV({ labels: list })} />
                </Prop>
              </>
            )}
            {field.type === "currency" && (
              <Prop label="Currency">
                <Select value={v.symbol ?? "$"} onChange={s => setV({ symbol: s })}
                  options={[["$", "$ USD"], ["€", "€ EUR"], ["£", "£ GBP"], ["¥", "¥ JPY/CNY"], ["₹", "₹ INR"], ["₩", "₩ KRW"], ["CHF", "CHF"]]} />
              </Prop>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/* ── Button (batch M, 2026-10-29) ─────────────────────────────── */

const ACTION_LABEL: Record<string, string> = {
  set: "Set a field", clear: "Clear a field", run_lookup: "Run a lookup", open_link: "Open a web page", go_to_section: "Go to a section",
};

function ButtonSettings({ field, fields, sections, onUpdate }: {
  field: FormFieldDef; fields: FormFieldDef[]; sections: { id: string; title: string }[]; onUpdate: (p: Partial<FormFieldDef>) => void;
}) {
  const f = field as any;
  const actions: any[] = Array.isArray(f.actions) ? f.actions : [];
  const set = (next: any[]) => onUpdate({ actions: next } as any);
  const at = (i: number, p: any) => set(actions.map((a, j) => (j === i ? { ...a, ...p } : a)));
  const targets = fields.filter(x => !DISPLAY_TYPES.has(x.type) && x.type !== "consent" && typeof (x as any).calculation !== "string");
  const lookups = fields.filter(x => x.type === "connector_lookup");
  return (
    <>
      <Prop label="Style">
        <Segmented value={f.style ?? "primary"} onChange={s => onUpdate({ style: s === "primary" ? undefined : s } as any)}
          options={[["primary", "Main"], ["secondary", "Plain"]]} />
      </Prop>
      <div style={{ fontSize: 12, color: "var(--text-primary)", margin: "6px 0" }}>When clicked</div>
      {actions.map((a, i) => (
        <div key={i} style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", padding: 8, marginBottom: 6 }}>
          <div style={{ display: "flex", gap: 4, marginBottom: 6 }}>
            <Select value={a.action} onChange={v => at(i, { action: v })} options={Object.entries(ACTION_LABEL) as [string, string][]} />
            <button aria-label="Remove action" onClick={() => set(actions.filter((_, j) => j !== i))} style={{ ...miniBtn, color: "var(--status-failed)" }}>×</button>
          </div>
          {(a.action === "set" || a.action === "clear") && (
            <Select value={a.target ?? ""} onChange={v => at(i, { target: v || undefined })}
              options={[["", "— field —"], ...targets.map(t => [t.field_key, t.label || t.field_key] as [string, string])]} />
          )}
          {a.action === "set" && (
            <div style={{ marginTop: 6 }}>
              <FormulaEditor value={a.expression ?? ""} fields={fields} ariaLabel="Set to" placeholder="e.g. today()"
                onChange={expression => at(i, { expression })} />
            </div>
          )}
          {a.action === "run_lookup" && (
            <Select value={a.target ?? ""} onChange={v => at(i, { target: v || undefined })}
              options={[["", lookups.length ? "— lookup —" : "No lookup field on this form"], ...lookups.map(t => [t.field_key, t.label || t.field_key] as [string, string])]} />
          )}
          {a.action === "open_link" && (
            <>
              <TextIn value={a.url ?? ""} onChange={v => at(i, { url: v.trim() || undefined })} />
              {a.url && !safeLink(a.url) && <Warn>Use an https address; anything else is not opened.</Warn>}
            </>
          )}
          {a.action === "go_to_section" && (
            <Select value={a.section ?? ""} onChange={v => at(i, { section: v || undefined })}
              options={[["", "— section —"], ...sections.map(s => [s.id, s.title] as [string, string])]} />
          )}
        </div>
      ))}
      <button type="button" disabled={actions.length >= 10} onClick={() => set([...actions, { action: "set" }])} style={dashedBtn}>+ Add an action</button>
      <Muted>A button never changes a read-only or calculated field, never ticks a consent, and opens only https pages, in a new tab. Go to a section works with Step by step or Tabs.</Muted>
    </>
  );
}

/* ── Embedded page (2026-10-29) ──────────────────────────────── */

function EmbedSettings({ field, onUpdate }: { field: FormFieldDef; onUpdate: (p: Partial<FormFieldDef>) => void }) {
  const f = field as any;
  const origins = useEmbedOrigins();
  const refusal = f.url ? embedRefusal(f.url, origins) : null;
  const perms: string[] = Array.isArray(f.permissions) ? f.permissions : [];
  const allowsForms = useEmbedAllowsForms();
  return (
    <>
      <Prop label="Page address" hint="https only, from a site your administrator approved.">
        <TextIn value={f.url ?? ""} onChange={val => onUpdate({ url: val.trim() } as any)} />
        {refusal && <Warn>{refusal} The form cannot be saved with it.</Warn>}
      </Prop>
      <Prop label="Height (pixels)" hint="100 to 1200.">
        <NumIn value={f.height} onChange={s => onUpdate({ height: s.trim() === "" || isNaN(Number(s)) ? undefined : Number(s) } as any)} />
      </Prop>
      {Object.keys(EMBED_PERMISSIONS).filter(p => p !== "forms" || allowsForms || perms.includes("forms")).map(p => (
        <Check key={p} label={p === "forms" ? "Let the page send its own forms" : "Let the page open new windows"}
          checked={perms.includes(p)}
          onChange={c => onUpdate({ permissions: c ? [...perms, p] : perms.filter(x => x !== p).length ? perms.filter(x => x !== p) : undefined } as any)} />
      ))}
      {!allowsForms && perms.includes("forms") && <Warn>Typing into embedded pages is switched off on this platform — untick it, or the form cannot be saved.</Warn>}
      <Muted>The page runs in a locked-down frame: it cannot move this page, read it, or use the camera, microphone or location.
        Every viewer sees a label above it naming the site.</Muted>
      {origins && origins.length === 0 && <Muted>No site is approved yet — an administrator adds them under Admin › Security › Allowed sites.</Muted>}
    </>
  );
}

/* ── Default value, typed per field ───────────────────────────── */

/** No default value or calculation: a file, a pin, a list of words, two dates —
 *  and a consent, which the person must give themselves, never a designer. */
const NO_DEFAULT = new Set(["file_upload", "location", "tags", "date_range", "consent",
  "address", "signature", "user_picker", "case_picker", "document_picker"]);

/** A repeating group's columns: add, reorder, remove — and open one to set it up. */
function GroupColumns({ field, onUpdate, onEdit }: {
  field: FormFieldDef; onUpdate: (p: Partial<FormFieldDef>) => void; onEdit: (id: string) => void;
}) {
  const cols: FormFieldDef[] = Array.isArray(field.fields) ? field.fields : [];
  const [type, setType] = useState<FormFieldDef["type"]>("text");
  const set = (next: FormFieldDef[]) => onUpdate({ fields: next });
  const usable = FIELD_TYPES.filter(t => !DISPLAY_TYPES.has(t.type) && t.type !== "repeating_group");
  const add = () => {
    const label = usable.find(t => t.type === type)?.label ?? "Column";
    const id = `col-${Date.now()}`;
    set([...cols, { id, type, label, field_key: keyFromLabel(label, new Set(cols.map(c => c.field_key))), required: false, placeholder: "" }]);
    onEdit(id);
  };
  const move = (i: number, d: number) => { const n = [...cols]; const [c] = n.splice(i, 1); n.splice(i + d, 0, c); set(n); };
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>Columns — one of each per row</div>
      {cols.length === 0 && <Muted>No columns yet.</Muted>}
      {cols.map((c, i) => (
        <div key={c.id} style={{ display: "flex", alignItems: "center", gap: 4, padding: "4px 6px", border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-sm)", marginBottom: 4, fontSize: 12 }}>
          <button onClick={() => onEdit(c.id)} style={{ flex: 1, textAlign: "left", background: "none", border: "none", color: "var(--text-primary)", cursor: "pointer", fontSize: 12, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}
            aria-label={`Edit column ${c.label}`}>
            {c.label || c.field_key}
            <span style={{ color: "var(--text-muted)", fontSize: 10, marginLeft: 6 }}>
              {FIELD_TYPES.find(t => t.type === c.type)?.label}{typeof c.calculation === "string" ? " · ƒ" : ""}{c.required ? " · required" : ""}
            </span>
          </button>
          <button aria-label={`Move ${c.label} up`} disabled={i === 0} onClick={() => move(i, -1)} style={miniBtn}>↑</button>
          <button aria-label={`Move ${c.label} down`} disabled={i === cols.length - 1} onClick={() => move(i, 1)} style={miniBtn}>↓</button>
          <button aria-label={`Remove ${c.label}`} onClick={() => set(cols.filter(x => x.id !== c.id))} style={{ ...miniBtn, color: "var(--status-failed)" }}>×</button>
        </div>
      ))}
      <div style={{ display: "flex", gap: 4, marginTop: 4 }}>
        <Select value={type} onChange={v => setType(v as FormFieldDef["type"])} options={usable.map(t => [t.type, t.label])} />
        <button onClick={add} style={dashedBtn}>+ Column</button>
      </div>
      <Prop label="Add button text">
        <TextIn value={field.add_label || ""} onChange={v => onUpdate({ add_label: v || undefined })} />
      </Prop>
      <Muted>Stored as a list of rows. A total elsewhere on the form: Calculated, e.g. sum({field.field_key}.{cols.find(c => c.type === "number" || c.type === "currency")?.field_key ?? "amount"}).</Muted>
    </div>
  );
}

const miniBtn: React.CSSProperties = {
  background: "none", border: "1px solid var(--border-subtle)", borderRadius: 4, padding: "0 5px",
  fontSize: 11, color: "var(--text-secondary)", cursor: "pointer",
};

function DefaultValueInput({ field, onChange }: { field: FormFieldDef; onChange: (v: any) => void }) {
  const val = field.default_value;
  const clear = (v: any) => onChange(v === "" || v === undefined ? undefined : v);
  switch (field.type) {
    case "number": case "currency": case "slider": case "rating": case "percentage":
      return <NumIn value={val} onChange={s => clear(s.trim() === "" || isNaN(Number(s)) ? undefined : Number(s))} />;
    case "toggle":
      return <Select value={val === true ? "true" : val === false ? "false" : ""} onChange={s => onChange(s === "" ? undefined : s === "true")}
        options={[["", "— none —"], ["true", "Yes"], ["false", "No"]]} />;
    case "time":
      return <input type="time" value={val || ""} onChange={e => clear(e.target.value)} style={inputStyle} />;
    case "country": case "currency_code":
      return <Select value={val ?? ""} onChange={s => clear(s)}
        options={[["", "— none —"], ...codeOptions(field.type).map(o => [o.value, o.label] as [string, string])]} />;
    case "date":
      return <input type="date" value={val || ""} onChange={e => clear(e.target.value)} style={inputStyle} />;
    case "datetime":
      return <input type="datetime-local" value={val || ""} onChange={e => clear(e.target.value)} style={inputStyle} />;
    case "checkbox":
      return <Check label="Ticked" checked={!!val} onChange={c => clear(c || undefined)} />;
    case "dropdown": case "radio":
      return <Select value={val ?? ""} onChange={s => clear(s)}
        options={[["", "— none —"], ...(field.options || []).map(o => [o.value, o.label] as [string, string])]} />;
    case "multi_select": {
      const chosen: string[] = Array.isArray(val) ? val : [];
      return (
        <div>
          {(field.options || []).map(o => (
            <Check key={o.value} label={o.label} checked={chosen.includes(o.value)}
              onChange={c => clear(c ? [...chosen, o.value] : chosen.filter(x => x !== o.value).length ? chosen.filter(x => x !== o.value) : undefined)} />
          ))}
        </div>
      );
    }
    case "textarea":
      return <textarea value={val || ""} onChange={e => clear(e.target.value)} rows={3} style={{ ...inputStyle, resize: "vertical" }} />;
    default:
      return <TextIn value={val ?? ""} onChange={clear} />;
  }
}

/* ── Options ──────────────────────────────────────────────────── */

/** An option value made from its label, like a field key. */
export function valueFromLabel(label: string): string {
  return label.toLowerCase().normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "option";
}

function OptionsEditor({ field, fields, onUpdate }: { field: FormFieldDef; fields: FormFieldDef[]; onUpdate: (p: Partial<FormFieldDef>) => void }) {
  const opts = (field.options || []).map((o: any) => (typeof o === "string" ? { label: o, value: o } : o));
  const src: any = (field as any).options_source;
  const fromConnector = src?.kind === "connector";
  const [pasting, setPasting] = useState(false);
  const [paste, setPaste] = useState("");
  const [drag, setDrag] = useState<number | null>(null);
  const multi = field.type === "multi_select";
  const defaults: string[] = multi ? (Array.isArray(field.default_value) ? field.default_value : []) : [field.default_value];

  const set = (next: { label: string; value: string }[]) => onUpdate({ options: next });
  const setRow = (i: number, p: Partial<{ label: string; value: string }>) => {
    const next = [...opts];
    const row = { ...next[i], ...p };
    // The value follows the label until it is set by hand.
    if (p.label !== undefined && next[i].value === valueFromLabel(next[i].label)) row.value = valueFromLabel(p.label);
    next[i] = row;
    set(next);
  };
  const move = (from: number, to: number) => {
    if (from === to || to < 0 || to >= opts.length) return;
    const next = [...opts];
    const [row] = next.splice(from, 1);
    next.splice(to, 0, row);
    set(next);
  };
  const toggleDefault = (value: string) => {
    if (multi) {
      const next = defaults.includes(value) ? defaults.filter(v => v !== value) : [...defaults, value];
      onUpdate({ default_value: next.length ? next : undefined });
    } else {
      onUpdate({ default_value: field.default_value === value ? undefined : value });
    }
  };
  const values = opts.map(o => o.value);
  const dupes = values.filter((v, i) => values.indexOf(v) !== i);

  return (
    <Prop label="Options">
      <Segmented value={fromConnector ? "connector" : src?.kind === "reference" ? "reference" : "fixed"}
        onChange={k => onUpdate({ options_source: k === "connector" ? { kind: "connector", connector_id: "", input_mapping: {} }
          : k === "reference" ? { kind: "reference" } : undefined } as any)}
        options={[["fixed", "Fixed list"], ["reference", "Reference table"], ["connector", "Connector"]]} />
      <div style={{ height: 8 }} />
      {src?.kind === "reference" ? (
        <ReferenceEditor field={field} fields={fields} onUpdate={onUpdate} />
      ) : fromConnector ? (
        <>
          <Prop label="Connector ID"><TextIn mono value={src.connector_id ?? ""} onChange={v => onUpdate({ options_source: { ...src, connector_id: v } } as any)} /></Prop>
          <ConnectorOperationField connectorId={src.connector_id} value={src.operation}
            onChange={op => onUpdate({ options_source: { ...src, operation: op } } as any)} />
          <Prop label="Where the list is in the response" hint="e.g. data.items — empty if the response is the list itself.">
            <TextIn mono value={src.list_path ?? ""} onChange={v => onUpdate({ options_source: { ...src, list_path: v || undefined } } as any)} />
          </Prop>
          <Row>
            <Prop label="Label from"><TextIn mono value={src.label_path ?? ""} onChange={v => onUpdate({ options_source: { ...src, label_path: v || undefined } } as any)} /></Prop>
            <Prop label="Value from"><TextIn mono value={src.value_path ?? ""} onChange={v => onUpdate({ options_source: { ...src, value_path: v || undefined } } as any)} /></Prop>
          </Row>
          <Prop label="Inputs (JSON: {param: field_key})" hint="The list is fetched again when one of these fields changes.">
            <JsonIn value={src.input_mapping ?? {}} onChange={m => onUpdate({ options_source: { ...src, input_mapping: m } } as any)} />
          </Prop>
          <Muted>Defaults: label from "name", value from "id". The server cannot check a choice against a list it does not hold.</Muted>
        </>
      ) : (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "14px 1fr 1fr 22px 18px", gap: 4, fontSize: 10, color: "var(--text-muted)", marginBottom: 2 }}>
            <span /><span>Label (shown)</span><span>Value (stored)</span><span title="Default">Def.</span><span />
          </div>
          {opts.map((opt, i) => (
            <div key={i} draggable
              onDragStart={() => setDrag(i)}
              onDragOver={e => e.preventDefault()}
              onDrop={() => { if (drag !== null) move(drag, i); setDrag(null); }}
              style={{ display: "grid", gridTemplateColumns: "14px 1fr 1fr 22px 18px", gap: 4, marginBottom: 4, alignItems: "center" }}>
              <span title="Drag to reorder" style={{ cursor: "grab", color: "var(--text-muted)", fontSize: 11 }}>⠿</span>
              <TextIn value={opt.label} onChange={label => setRow(i, { label })} />
              <TextIn mono value={opt.value} onChange={value => setRow(i, { value: value.trim() })} />
              <input type={multi ? "checkbox" : "radio"} aria-label={`Default: ${opt.label}`} checked={defaults.includes(opt.value)}
                onChange={() => toggleDefault(opt.value)} onClick={() => !multi && field.default_value === opt.value && toggleDefault(opt.value)}
                style={{ accentColor: "var(--accent)" }} />
              <button onClick={() => set(opts.filter((_, j) => j !== i))} aria-label={`Remove ${opt.label}`}
                style={{ background: "transparent", border: "none", color: "var(--status-failed)", cursor: "pointer", fontSize: 13 }}>×</button>
            </div>
          ))}
          {dupes.length > 0 && <Warn>Two options share the value "{dupes[0]}" — the stored answer could not tell them apart.</Warn>}
          <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
            <button onClick={() => {
              const n = opts.length + 1;
              set([...opts, { label: `Option ${n}`, value: `option_${n}` }]);
            }} style={dashedBtn}>+ Add option</button>
            <button onClick={() => setPasting(p => !p)} style={dashedBtn}>Paste a list</button>
          </div>
          {pasting && (
            <div style={{ marginTop: 6 }}>
              <textarea value={paste} onChange={e => setPaste(e.target.value)} rows={5}
                placeholder={"One per line.\nGermany\nFrance | FR   (label | value)"}
                style={{ ...inputStyle, resize: "vertical" }} />
              <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
                <button onClick={() => {
                  const added = paste.split("\n").map(l => l.trim()).filter(Boolean).map(l => {
                    const [label, value] = l.split("|").map(x => x.trim());
                    return { label, value: value || valueFromLabel(label) };
                  });
                  set([...opts, ...added]);
                  setPaste(""); setPasting(false);
                }} style={dashedBtn}>Add {paste.split("\n").filter(l => l.trim()).length} options</button>
                <button onClick={() => { setPasting(false); setPaste(""); }} style={dashedBtn}>Cancel</button>
              </div>
            </div>
          )}
        </>
      )}
    </Prop>
  );
}

/** JSON text that keeps what is typed and says when it does not parse —
 *  the old box silently dropped every keystroke that made it invalid. */
function JsonIn({ value, onChange }: { value: any; onChange: (v: any) => void }) {
  const [text, setText] = useState(() => JSON.stringify(value));
  const [bad, setBad] = useState(false);
  return (
    <>
      <TextIn mono value={text} onChange={t => {
        setText(t);
        try { onChange(JSON.parse(t)); setBad(false); } catch { setBad(true); }
      }} />
      {bad && <Warn>Not valid JSON yet — the last valid value is kept.</Warn>}
    </>
  );
}

/* ── Pre-fill from a case variable ────────────────────────────── */

function VariablePicker({ field, variables, onChange }: {
  field: FormFieldDef;
  variables?: { case_types: { id: string; name: string }[]; variables: FormVariable[] } | null;
  onChange: (v: string) => void;
}) {
  const current = field.variable || "";
  const list = variables?.variables ?? [];
  const known = list.some(v => v.full_key === current);
  const [typing, setTyping] = useState(!!current && !known && variables !== undefined);
  const byNs = new Map<string, FormVariable[]>();
  for (const v of list) byNs.set(v.namespace || "other", [...(byNs.get(v.namespace || "other") ?? []), v]);

  return (
    <Prop label="Pre-fill from case variable"
      hint={`When the form opens in a case, the field starts with this variable's value. None chosen: a variable named "${field.field_key}" is used if the case has one.`}>
      {variables === undefined && <Muted>Loading the case variables…</Muted>}
      {variables === null && <Muted>Type the variable's name — e.g. crm.account_status. (The list needs a saved form.)</Muted>}
      {variables && !typing && (
        <Select value={known || !current ? current : "__other"} onChange={v => v === "__type" ? setTyping(true) : onChange(v)}
          options={[
            ["", "— none —"],
            ...Array.from(byNs.entries()).flatMap(([ns, vs]) =>
              vs.map(v => [v.full_key, `${v.label ? `${v.label} — ` : ""}${v.full_key}`] as [string, string])),
            ...(current && !known ? [["__other", `${current} (not defined)`] as [string, string]] : []),
            ["__type", "Type a name…"],
          ]} />
      )}
      {(variables === null || (variables && typing)) && (
        <>
          <TextIn mono value={current} onChange={onChange} />
          {variables && <button onClick={() => setTyping(false)} style={linkBtn}>Choose from the list</button>}
        </>
      )}
      {variables && variables.case_types.length === 0 && (
        <Warn>No step uses this form yet, so there are no case variables to choose from. Attach it to a step in Case Designer.</Warn>
      )}
      {variables && variables.case_types.length > 0 && list.length === 0 && (
        <Muted>{variables.case_types.map(c => c.name).join(", ")} define no case variables.</Muted>
      )}
    </Prop>
  );
}

/* ── Pattern (preset or own) ──────────────────────────────────── */

const PRESETS: [string, string][] = [
  ["", "Any text"], ["digits", "Digits only"], ["alnum", "Letters and digits"],
  ["no_digits", "No digits"], ["email", "Email address"], ["phone", "Phone number"], ["custom", "Own pattern (regex)"],
];

function PatternEditor({ v, setV, phone }: { v: Record<string, any>; setV: (p: Record<string, any>) => void; phone: boolean }) {
  const isPreset = !v.pattern || PRESETS.some(([k]) => k === v.pattern);
  const choice = !v.pattern ? "" : isPreset ? v.pattern : "custom";
  let bad = false, unsafe = false;
  if (choice === "custom" && v.pattern) {
    try { new RegExp(v.pattern, "u"); } catch { bad = true; }
    unsafe = !bad && !safePattern(v.pattern);
  }
  return (
    <>
      <Prop label="Format">
        <Select value={choice} onChange={k => setV({ pattern: k === "custom" ? (isPreset ? ".*" : v.pattern) : k || undefined })}
          options={phone ? PRESETS.filter(([k]) => ["", "phone", "digits", "custom"].includes(k)) : PRESETS} />
      </Prop>
      {choice === "custom" && (
        <Prop label="Pattern" hint="The whole value must match, e.g. [A-Z]{2}-[0-9]{4}">
          <TextIn mono value={v.pattern ?? ""} onChange={val => setV({ pattern: val || undefined })} />
          {bad && <Warn>This pattern is not valid; it is ignored until it is.</Warn>}
          {unsafe && <Warn>This pattern could take very long on some answers (a repeated group that itself repeats, a back-reference, or over 200 characters). It is ignored — simplify it.</Warn>}
        </Prop>
      )}
      {choice && (
        <Prop label="Message when it does not match">
          <TextIn value={v.pattern_message ?? ""} onChange={val => setV({ pattern_message: val || undefined })} />
        </Prop>
      )}
    </>
  );
}

/** A comma separated list, stored as an array; empty = not set. */
function ListIn({ value, onChange }: { value: any; onChange: (v: string[] | undefined) => void }) {
  const [text, setText] = useState(() => (Array.isArray(value) ? value.join(", ") : ""));
  return <TextIn value={text} onChange={t => {
    setText(t);
    const list = t.split(",").map(x => x.trim()).filter(Boolean);
    onChange(list.length ? list : undefined);
  }} />;
}

/* ── Primitives ───────────────────────────────────────────────── */

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "6px 8px", fontSize: 12, fontFamily: "var(--font-body)",
  background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none", boxSizing: "border-box",
};
const linkBtn: React.CSSProperties = {
  background: "none", border: "none", padding: "4px 0", margin: "4px 0", fontSize: 12,
  color: "var(--text-secondary)", cursor: "pointer",
};
const dashedBtn: React.CSSProperties = {
  padding: "3px 8px", fontSize: 11, background: "transparent", border: "1px dashed var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-muted)", cursor: "pointer",
};

function Prop({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 10, flex: 1, minWidth: 0 }}>
      <label style={{ display: "block", fontSize: 11, fontWeight: 500, color: "var(--text-secondary)", marginBottom: 3 }}>{label}</label>
      {children}
      {hint && <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

function Row({ children }: { children: React.ReactNode }) {
  return <div style={{ display: "flex", gap: 8 }}>{children}</div>;
}

function TextIn({ value, onChange, mono }: { value: string; onChange: (v: string) => void; mono?: boolean }) {
  return <input value={value} onChange={e => onChange(e.target.value)}
    style={{ ...inputStyle, fontFamily: mono ? "var(--font-mono)" : inputStyle.fontFamily }} />;
}

function NumIn({ value, onChange }: { value: any; onChange: (v: string) => void }) {
  return <input type="number" value={value ?? ""} onChange={e => onChange(e.target.value)} style={inputStyle} />;
}

function Select({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: [string, string][] }) {
  return (
    <select value={value} onChange={e => onChange(e.target.value)} style={inputStyle}>
      {options.map(([o, l]) => <option key={o} value={o}>{l}</option>)}
    </select>
  );
}

function Check({ label, checked, onChange, hint }: { label: string; checked: boolean; onChange: (c: boolean) => void; hint?: string }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--text-primary)", cursor: "pointer" }}>
        <input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} style={{ accentColor: "var(--accent)" }} />
        {label}
      </label>
      {hint && <div style={{ fontSize: 10, color: "var(--text-muted)", marginLeft: 22 }}>{hint}</div>}
    </div>
  );
}

function Segmented({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: [string, string][] }) {
  return (
    <div style={{ display: "inline-flex", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", overflow: "hidden" }}>
      {options.map(([o, l]) => (
        <button key={o} onClick={() => onChange(o)} aria-pressed={value === o} style={{
          padding: "4px 12px", fontSize: 12, border: "none", cursor: "pointer",
          background: value === o ? "var(--accent)" : "transparent",
          color: value === o ? "#fff" : "var(--text-secondary)",
        }}>{l}</button>
      ))}
    </div>
  );
}

function Warn({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 11, color: "var(--status-warning, #d97706)", marginTop: 4 }}>{children}</div>;
}

function Muted({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{children}</div>;
}
