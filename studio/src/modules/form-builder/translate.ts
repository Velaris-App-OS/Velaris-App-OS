/**
 * Form translations (Form Builder v2 §5 step 7, 2026-10-29) — the twin of
 * `velaris_shared/forms/translate.py`. Only text changes: labels, help,
 * placeholders, messages, option labels, section titles, the Submit text.
 * Keys, values and rules never do, so a form filled in German stores what one
 * filled in English stores.
 */
import { fieldKey } from "./validate";

export type FieldTexts = {
  label?: string; description?: string; placeholder?: string; required_message?: string;
  text?: string; add_label?: string; options?: Record<string, string>;
};
export type LanguageTexts = {
  form?: { submit_label?: string };
  sections?: Record<string, { title?: string; description?: string }>;
  /** By field key; a repeating group's column as `group.column`. */
  fields?: Record<string, FieldTexts>;
};
export type Translations = Record<string, LanguageTexts>;

const FIELD_TEXTS = ["label", "description", "placeholder", "required_message", "text", "add_label"] as const;
const LANG = /^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$/;

const has = (v: unknown): v is string => typeof v === "string" && v.trim() !== "";

export function languages(definition: any): string[] {
  const tr = definition?.translations;
  if (!tr || typeof tr !== "object") return [];
  return Object.keys(tr).filter(k => LANG.test(k) && tr[k] && typeof tr[k] === "object").sort();
}

/** The first of the person's languages the form has (exact, then the main part); null = the form's own. */
export function pickLanguage(definition: any, preferred: readonly string[] | undefined): string | null {
  const have = languages(definition);
  if (!have.length || !preferred) return null;
  const lower = new Map(have.map(h => [h.toLowerCase(), h]));
  for (const p of preferred) {
    const tag = p.toLowerCase();
    if (lower.has(tag)) return lower.get(tag)!;
    const main = tag.split("-")[0];
    if (lower.has(main)) return lower.get(main)!;
  }
  return null;
}

function field(f: any, texts: Record<string, FieldTexts>, prefix = ""): any {
  if (!f || typeof f !== "object") return f;
  const key = prefix + fieldKey(f);
  const t = texts[key] && typeof texts[key] === "object" ? texts[key] : {};
  const out: any = { ...f };
  for (const k of FIELD_TEXTS) if (has(t[k])) out[k] = t[k];
  const opts = t.options && typeof t.options === "object" ? t.options : null;
  if (opts && Array.isArray(f.options)) {
    out.options = f.options.map((o: any) =>
      o && typeof o === "object" && has(opts[String(o.value)]) ? { ...o, label: opts[String(o.value)] }
        : typeof o === "string" && has(opts[o]) ? { label: opts[o], value: o } : o);
  }
  if (Array.isArray(f.fields)) out.fields = f.fields.map((c: any) => field(c, texts, `${key}.`));
  return out;
}

/** The form in `lang`; the same object when there is nothing to translate. */
export function translate<T extends { sections?: any[]; settings?: any }>(definition: T, lang: string | null): T {
  const tr: LanguageTexts | undefined = lang ? (definition as any)?.translations?.[lang] : undefined;
  if (!tr || typeof tr !== "object") return definition;
  const fields = tr.fields && typeof tr.fields === "object" ? tr.fields : {};
  const secs = tr.sections && typeof tr.sections === "object" ? tr.sections : {};
  const out: any = { ...definition };
  out.sections = (definition.sections || []).map((s: any) => {
    if (!s || typeof s !== "object") return s;
    const st = secs[String(s.id)] || {};
    return {
      ...s,
      ...(has(st.title) ? { title: st.title } : {}),
      ...(has(st.description) ? { description: st.description } : {}),
      fields: (s.fields || []).map((f: any) => field(f, fields)),
    };
  });
  if (has(tr.form?.submit_label)) out.settings = { ...(definition.settings || {}), submit_label: tr.form!.submit_label };
  return out;
}

/** A language's display name in the person's own language, e.g. "Deutsch (de)". */
export function languageName(code: string): string {
  try {
    const n = new Intl.DisplayNames([code], { type: "language" }).of(code);
    return n && n !== code ? `${n} (${code})` : code;
  } catch { return code; }
}
