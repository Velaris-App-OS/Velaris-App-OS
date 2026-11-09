/* ═══════════════════════════════════════════════════════════════════
   Form values check — the mirror of velaris_shared/forms/validate.py
   (Form Builder redesign part 1, 2026-10-28).

   The server decides; this gives the person the same answer before they
   press Submit. Both run services/_shared/velaris_shared/forms/
   validation_cases.json, so a rule changed on one side only fails a test.
   ═══════════════════════════════════════════════════════════════════ */

import { parsePhoneNumberFromString, getCountryCallingCode, isSupportedCountry, type CountryCode } from "libphonenumber-js/max";
import type { FormFieldDef, FormSectionDef } from "./FormBuilder";
import { evaluate, references, truthy } from "./expr";
import CODES from "./codes.json";

type Values = Record<string, any>;
type Rule = { field?: string; op?: string; value?: any };

const TEXT_TYPES = new Set(["text", "textarea", "email", "phone", "url"]);
const CHOICE_TYPES = new Set(["dropdown", "radio"]);
/** A map pin (2026-10-29): `{lat, lng}`, stored as exactly that. */
export const LOCATION = "location";

/* ── Small controls (2026-10-29) ─────────────────────────────────── */
/** Text, a line or a box the designer writes: no value; one sent is dropped. */
export const STATIC_TYPES = new Set(["heading", "paragraph", "divider", "callout", "embed", "button"]);
export const COUNTRIES: { code: string; name: string }[] = CODES.countries;
export const CURRENCIES: { code: string; name: string }[] = CODES.currencies;
const COUNTRY_CODES = new Set(COUNTRIES.map(c => c.code));
const CURRENCY_CODES = new Set(CURRENCIES.map(c => c.code));
const IBAN_LENGTHS: Record<string, number> = CODES.iban_lengths;
const TIME = /^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$/;
const URL_RE = /^https?:\/\/[^\s/?#.][^\s]*$/i;
const TAG_MAX_LENGTH = 50;

/* ── Controls batch M (2026-10-29) ───────────────────────────────── */
const POSTCODE_PATTERNS: Record<string, string> = (CODES as any).postcode_patterns ?? {};
const ADDRESS_PARTS = ["line1", "line2", "city", "region", "postcode", "country"] as const;
const ADDRESS_PART_MAX = 200;
export const SIGNATURE_PREFIX = "data:image/png;base64,";
export const SIGNATURE_MAX = 200_000;
export const RICH_TEXT_MAX = 10_000;
/** A picker's value is an id; the server checks it is one the caller may choose. */
export const PICKER_TYPES = new Set(["user_picker", "case_picker", "document_picker"]);

function addressError(val: any): string | null {
  if (!val || typeof val !== "object" || Array.isArray(val)) return "Must be an address";
  const parts: Record<string, any> = Object.fromEntries(ADDRESS_PARTS.map(k => [k, val[k]]));
  if (Object.values(parts).some(v => v !== undefined && v !== null && typeof v !== "string")) return "Must be an address";
  if (Object.values(parts).some(v => typeof v === "string" && [...v].length > ADDRESS_PART_MAX)) return `Each part has at most ${ADDRESS_PART_MAX} characters`;
  if (!(parts.line1 ?? "").trim() || !(parts.city ?? "").trim()) return "Enter the street and the town";
  const country = (parts.country ?? "").trim().toUpperCase();
  if (!COUNTRY_CODES.has(country)) return "Must be a country";
  const postcode = (parts.postcode ?? "").trim();
  if (postcode) {
    const pattern = POSTCODE_PATTERNS[country];
    const ok = pattern ? new RegExp(`^(?:${pattern})$`, "i").test(postcode) : /^[A-Za-z0-9][A-Za-z0-9 -]{1,11}$/.test(postcode);
    if (!ok) return `Not a postcode used in ${country}`;
  }
  return null;
}

function signatureError(val: any): string | null {
  if (typeof val !== "string" || !val.startsWith(SIGNATURE_PREFIX) || val.length > SIGNATURE_MAX) return "Not a valid signature";
  const b64 = val.slice(SIGNATURE_PREFIX.length);
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(b64) || b64.length % 4 !== 0) return "Not a valid signature";
  try {
    const head = atob(b64.slice(0, 12));
    return head.startsWith("\x89PNG\r\n\x1a\n") ? null : "Not a valid signature";
  } catch { return "Not a valid signature"; }
}
/** A ceiling whatever the designer sets: tags are typed by hand. */
const TAG_MAX_COUNT = 100;

/** `validation.pattern` is a preset name or the designer's own regex. */
export const PATTERN_PRESETS: Record<string, string> = {
  email:     "[^\\s@]+@[^\\s@]+\\.[^\\s@]{2,}",
  digits:    "[0-9]+",
  alnum:     "[A-Za-z0-9]+",
  no_digits: "[^0-9]+",
  phone:     "\\+?[0-9 ()\\-]{6,20}",
};
const PRESET_MESSAGES: Record<string, string> = {
  email: "Invalid email address", digits: "Use digits only",
  alnum: "Use letters and digits only", no_digits: "Must not contain digits",
  phone: "Invalid phone number",
};
const PATTERN_MAX_INPUT = 1000;
const PATTERN_MAX_LENGTH = 200;
const RULE_MAX_DEPTH = 64;
/** A repeating group never holds more rows than this, whatever it allows. */
export const ROWS_HARD_MAX = 500;
export const GROUP = "repeating_group";

/** A repeating group's own fields — never another group (no nesting). */
export function groupFields(field: any): FormFieldDef[] {
  return (Array.isArray(field?.fields) ? field.fields : []).filter((c: any) => c && typeof c === "object" && c.type !== GROUP);
}

/** Can this pattern run in time proportional to its input? The server's rule
 *  (velaris_shared/forms/validate.py `safe_pattern`): no quantified group that
 *  itself repeats or alternates — `(a+)+`, `(a|ab)*` —, no backreference, at
 *  most 200 characters. A refused pattern is never run. */
export function safePattern(regex: string): boolean {
  if (regex.length > PATTERN_MAX_LENGTH || /\\[1-9]|\\k</.test(regex)) return false;
  const stack: [boolean, boolean][] = [];
  let inClass = false;
  for (let i = 0; i < regex.length; i++) {
    const c = regex[i];
    if (c === "\\") { i++; continue; }
    if (inClass) { inClass = c !== "]"; continue; }
    if (c === "[") inClass = true;
    else if (c === "(") stack.push([false, false]);
    else if (c === "|" && stack.length) stack[stack.length - 1][1] = true;
    else if ("*+?{".includes(c) && stack.length && (c !== "?" || (i > 0 && !"*+?}(".includes(regex[i - 1])))) stack[stack.length - 1][0] = true;
    else if (c === ")" && stack.length) {
      const [quant, alt] = stack.pop()!;
      const nxt = regex[i + 1] ?? "";
      if (nxt && "*+{".includes(nxt) && (quant || alt)) return false;
      if (stack.length && (quant || (nxt && "*+?{".includes(nxt)))) stack[stack.length - 1][0] = true;
    }
  }
  return true;
}

/** Today as YYYY-MM-DD in the person's own time zone. */
export function localToday(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** A date bound: a fixed YYYY-MM-DD, or today, today+N, today-N. */
export function dateBound(spec: any, today: string): string | null {
  if (typeof spec !== "string" || !spec) return null;
  const m = /^today(?:([+-])(\d{1,4}))?$/.exec(spec.trim());
  if (m) {
    const [y, mo, d] = today.split("-").map(Number);
    const n = Number(m[2] || 0) * (m[1] === "-" ? -1 : 1);
    const t = new Date(Date.UTC(y, mo - 1, d + n));
    return t.toISOString().slice(0, 10);
  }
  return spec.slice(0, 10);
}

function decimals(val: any): number {
  let text = s(val);
  if (/e/i.test(text)) text = Number(text).toFixed(20).replace(/0+$/, "");
  const i = text.indexOf(".");
  return i < 0 ? 0 : text.length - i - 1;
}

function patternError(v: Record<string, any>, val: string): string | null {
  const pattern = v.pattern;
  if (!pattern || typeof pattern !== "string") return null;
  const source = PATTERN_PRESETS[pattern] ?? pattern;
  if (!(pattern in PATTERN_PRESETS) && !safePattern(source)) return null;   // refused in the builder; never run
  const message = v.pattern_message || PRESET_MESSAGES[pattern] || "Not in the expected format";
  if ([...val].length > PATTERN_MAX_INPUT) return message;
  let re: RegExp;
  try { re = new RegExp(`^(?:${source})$`, "u"); } catch { return null; }
  return re.test(val) ? null : message;
}

function fileError(v: Record<string, any>, val: any): string | null {
  const files = Array.isArray(val) ? val : [val];
  if (Array.isArray(val) && !v.multiple) return "Only one file is allowed";
  const max = num(v.max_files);
  if (v.multiple && max !== null && files.length > max) return `At most ${v.max_files} files`;
  const accept = (v.accept || []).filter((a: any) => typeof a === "string" && a)
    .map((a: string) => (a.startsWith(".") ? a.toLowerCase() : `.${a.toLowerCase()}`));
  if (accept.length) {
    for (const f of files) {
      const name = String((f && typeof f === "object" ? f.filename : "") || "").toLowerCase();
      if (!accept.some((a: string) => name.endsWith(a))) return `Allowed file types: ${accept.join(", ")}`;
    }
  }
  return null;
}

function allFields(sections: FormSectionDef[] | undefined): FormFieldDef[] {
  // A malformed definition yields what it can, never throws.
  if (!Array.isArray(sections)) return [];
  return sections.flatMap(s => (s && Array.isArray(s.fields) ? s.fields : []).filter(f => f && typeof f === "object"));
}

/** The key a field's value is stored under. `key` is HxMigrate's spelling:
 *  its forms carry neither `field_key` nor `id`. Same rule as the server. */
export function fieldKey(f: { field_key?: string; id?: string; key?: string }): string {
  return f.field_key || f.key || f.id || "";
}

function isEmpty(v: any): boolean {
  return v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0);
}

/** One spelling for a value in a comparison — the same as the server's. */
function s(v: any): string {
  if (v === undefined || v === null) return "";
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

function num(v: any): number | null {
  if (typeof v === "boolean") return v ? 1 : 0;
  if (v === null || v === undefined || (typeof v === "string" && v.trim() === "")) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function equals(actual: any, expected: any): boolean {
  if (Array.isArray(actual)) return actual.some(a => s(a) === s(expected));
  return s(actual) === s(expected);
}

function ruleHolds(rule: Rule, actual: any): boolean {
  const op = rule.op ?? "equals";
  const expected = rule.value;
  switch (op) {
    case "equals":     return equals(actual, expected);
    case "not_equals": return !equals(actual, expected);
    case "in":         return (Array.isArray(expected) ? expected : [expected]).some(e => equals(actual, e));
    case "is_empty":   return isEmpty(actual);
    case "not_empty":  return !isEmpty(actual);
    case "gt": case "lt": {
      const a = num(actual), e = num(expected);
      if (a === null || e === null || isEmpty(actual)) return false;
      return op === "gt" ? a > e : a < e;
    }
    case "before": case "after": {
      const a = s(actual), e = s(expected);
      if (!a || !e) return false;
      return op === "before" ? a < e : a > e;
    }
    default: return false;   // an operator this version does not know never shows a field by accident
  }
}

const hasWhen = (w: any): boolean =>
  !!w && typeof w === "object" &&
  ((typeof w.expression === "string" && w.expression.trim() !== "") || (Array.isArray(w.rules) && w.rules.length > 0));

// Agreeing is never computed (consent, 2026-10-29).
const calcOf = (f: any): string | null =>
  f?.type !== "consent" && typeof f?.calculation === "string" && f.calculation.trim() ? f.calculation : null;

export type FormState = {
  shown: (key: string) => boolean;
  locked: (key: string) => boolean;
  required: (key: string) => boolean;
  value: (key: string) => any;
  reads: (key: string, formula?: boolean) => any;
  keys: string[];
  order: (FormFieldDef & Record<string, any>)[];
  /** A repeating group's row errors, by group key (filled as its rows are read). */
  rowErrors: Record<string, Record<string, string>>;
  /** Each row's own state, by group key — for drawing the rows. */
  rowStates: Record<string, FormState[]>;
};

/** What each field is, given these values: shown, locked (read-only), required,
 *  and its value — a calculated field's computed one. Twin of the server's
 *  FormState (velaris_shared/forms/validate.py); a circle reads as "before rules". */
export function formState(sections: FormSectionDef[] | undefined, values: Values, today: string = localToday(),
                          outer?: (key: string) => any): FormState {
  const order = allFields(sections) as (FormFieldDef & Record<string, any>)[];
  const fields = new Map<string, FormFieldDef & Record<string, any>>();
  for (const f of order) fields.set(fieldKey(f), f as any);
  const rowErrors: Record<string, Record<string, string>> = {};
  const rowStates: Record<string, FormState[]> = {};
  const byId = new Map<string, string>();
  for (const f of fields.values()) if (f.id) byId.set(f.id, fieldKey(f));
  const memo = new Map<string, any>();
  const busy = new Set<string>();

  const once = <T,>(kind: string, key: string, depth: number, fallback: T, compute: () => T): T => {
    const k = `${kind}\u0000${key}`;
    if (memo.has(k)) return memo.get(k);
    if (depth > RULE_MAX_DEPTH || busy.has(k)) return fallback;
    busy.add(k);
    let out: T;
    try { out = compute(); } finally { busy.delete(k); }
    memo.set(k, out);
    return out;
  };

  const holds = (when: any, depth: number): boolean => {
    if (typeof when.expression === "string" && when.expression.trim())
      return truthy(evaluate(when.expression, k => reads(k, depth + 1, true), today));
    const results = when.rules.filter((r: any) => r && typeof r === "object")
      .map((r: Rule) => ruleHolds(r, reads(String(r.field ?? ""), depth + 1)));
    return when.match === "any" ? results.some(Boolean) : results.every(Boolean);
  };

  const shown = (key: string, depth = 0): boolean => {
    const field = fields.get(key);
    if (!field) return true;
    if (field.hidden) return false;
    return once("shown", key, depth, true, () => {
      if (hasWhen(field.show_when)) return holds(field.show_when, depth);
      if (Array.isArray(field.conditions) && field.conditions.length) {
        // Legacy P38b shape: [{field_id, equals}], all must hold.
        return field.conditions.filter((c: any) => c && typeof c === "object")
          .every((c: any) => equals(reads(String(byId.get(c.field_id) ?? c.field_id ?? ""), depth + 1), c.equals));
      }
      return true;
    });
  };

  const locked = (key: string, depth = 0): boolean => {
    const field = fields.get(key);
    if (!field) return false;
    if (field.hidden || field.read_only || calcOf(field)) return true;
    if (!hasWhen(field.read_only_when)) return false;
    return once("locked", key, depth, false, () => holds(field.read_only_when, depth));
  };

  const required = (key: string, depth = 0): boolean => {
    const field = fields.get(key);
    if (!field) return false;
    if (field.required) return true;
    if (!hasWhen(field.required_when)) return false;
    return once("required", key, depth, false, () => holds(field.required_when, depth));
  };

  const value = (key: string, depth = 0): any => {
    const field = fields.get(key);
    const calc = field ? calcOf(field) : null;
    if (calc) return once("value", key, depth, null, () => evaluate(calc, k => reads(k, depth + 1, true), today));
    if (field?.type === GROUP) return once("value", key, depth, null, () => rows(field, key, depth));
    return values[key];
  };

  /** A repeating group's rows, each checked as a small form of its own; the
   *  twin of FormState._rows on the server. */
  function rows(field: any, key: string, depth: number): Values[] | null {
    const children = groupFields(field);
    const errs: Record<string, string> = {};
    rowErrors[key] = errs;
    rowStates[key] = [];
    // Hidden: the form's own rows. Read-only: what the form holds (pre-filled
    // from the case), else its default — the server reads the case itself.
    let raw = values[key];
    if (field.hidden) raw = field.default_value;
    else if (locked(key, depth)) raw = values[key] ?? field.default_value;
    if (raw === undefined || raw === null) return null;
    if (!Array.isArray(raw)) { errs[key] = "Must be a list of rows"; return null; }
    if (raw.length > ROWS_HARD_MAX) { errs[key] = `At most ${ROWS_HARD_MAX} rows`; return null; }
    const sub = [{ id: "row", title: "", order: 0, fields: children }] as FormSectionDef[];
    const keys = new Set(children.map(fieldKey));
    const out: Values[] = [];
    raw.forEach((row: any, i: number) => {
      if (!row || typeof row !== "object" || Array.isArray(row)) { errs[`${key}[${i}]`] = `Row ${i + 1} is not valid`; return; }
      const rst = formState(sub, { ...row }, today, k => reads(k, depth + 1, true));
      rowStates[key][i] = rst;
      const { clean, errors } = check(rst, { ...row }, today);
      out.push(Object.fromEntries(Object.entries(clean).filter(([k]) => keys.has(k))));
      for (const [k, m] of Object.entries(errors)) errs[`${key}[${i}].${k}`] = m;
    });
    return out;
  }

  function reads(key: string, depth: number, formula = false): any {
    const field = fields.get(key);
    if (!field) {
      if (outer) return outer(key);            // a row: the rest of the form
      return formula ? null : values[key];
    }
    if (field.hidden && calcOf(field)) return value(key, depth);
    if (!shown(key, depth)) return undefined;
    return value(key, depth);
  }

  return {
    shown: k => shown(k), locked: k => locked(k), required: k => required(k), value: k => value(k),
    reads: (k, formula) => reads(k, 0, formula), keys: [...fields.keys()], order, rowErrors, rowStates,
  };
}

/** Field key -> is it shown, given these values. */
export function visibleFields(sections: FormSectionDef[] | undefined, values: Values, today?: string): Record<string, boolean> {
  const st = formState(sections, values, today);
  const out: Record<string, boolean> = {};
  for (const key of st.keys) out[key] = st.shown(key);
  return out;
}

const coord = (v: any): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** A pin is two numbers, latitude -90..90 and longitude -180..180. */
export function locationError(val: any): string | null {
  if (!val || typeof val !== "object" || Array.isArray(val)) return "Must be a location";
  const lat = coord(val.lat), lng = coord(val.lng);
  if (lat === null || lng === null) return "Enter both latitude and longitude";
  if (lat < -90 || lat > 90) return "Latitude must be between -90 and 90";
  if (lng < -180 || lng > 180) return "Longitude must be between -180 and 180";
  return null;
}

function day(val: any): string | null {
  if (typeof val !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(val)) return null;
  const d = new Date(`${val}T00:00:00Z`);
  return !isNaN(d.getTime()) && d.toISOString().slice(0, 10) === val ? val : null;
}

/** `{start, end}`, both days, end on or after start, inside the field's bounds. */
function dateRangeError(v: Record<string, any>, val: any, today: string): string | null {
  const ok = val && typeof val === "object" && !Array.isArray(val);
  const start = ok ? day(val.start) : null, end = ok ? day(val.end) : null;
  if (!start || !end) return "Enter a start and an end date";
  if (end < start) return "The end must be on or after the start";
  const lo = v.no_past ? today : dateBound(v.earliest, today);
  const hi = v.no_future ? today : dateBound(v.latest, today);
  if (lo && start < lo) return `Must be on or after ${lo}`;
  if (hi && end > hi) return `Must be on or before ${hi}`;
  const most = num(v.max_days);
  const days = (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86400000 + 1;
  if (most !== null && days > most) return `At most ${v.max_days} days`;
  return null;
}

export const ibanCompact = (val: string) => val.replace(/\s+/g, "").toUpperCase();

/** Country length and the ISO 13616 mod-97 check digits. */
export function ibanError(val: any): string | null {
  if (typeof val !== "string") return "Not a valid IBAN";
  const iban = ibanCompact(val);
  if (!/^[A-Z]{2}\d{2}[A-Z0-9]{8,30}$/.test(iban)) return "Not a valid IBAN";
  const want = IBAN_LENGTHS[iban.slice(0, 2)];
  if (want === undefined) return `${iban.slice(0, 2)} does not use IBANs`;
  if (iban.length !== want) return `An IBAN from ${iban.slice(0, 2)} has ${want} characters`;
  let rem = 0;
  for (const ch of iban.slice(4) + iban.slice(0, 4)) {
    for (const d of String(parseInt(ch, 36))) rem = (rem * 10 + Number(d)) % 97;
  }
  return rem === 1 ? null : "Not a valid IBAN (check digits)";
}

function tagsError(v: Record<string, any>, val: any): string | null {
  if (!Array.isArray(val) || val.some(t => typeof t !== "string" || !t.trim())) return "Must be a list of tags";
  const set = num(v.max_tags);
  const most = set === null ? TAG_MAX_COUNT : Math.min(set, TAG_MAX_COUNT);
  if (val.length > most) return `At most ${most} tags`;
  const longest = num(v.max_tag_length) || TAG_MAX_LENGTH;
  if (val.some((t: string) => [...t.trim()].length > longest)) return `A tag has at most ${longest} characters`;
  if (new Set(val.map((t: string) => t.trim().toLowerCase())).size !== val.length) return "Each tag only once";
  return null;
}

/** A phone field's country settings (`{default_country, countries}`); null =
 *  none set, which keeps the format check every phone field had before. */
export function phoneConfig(field: Record<string, any>): { default_country: string; countries?: string[] } | null {
  if (field.type !== "phone") return null;
  const cfg = field.phone;
  if (!cfg || typeof cfg !== "object" || !COUNTRY_CODES.has(cfg.default_country)) return null;
  // A country with no dialling data (Antarctica…) cannot parse a number: the
  // plain format check, as with no country — the server does the same.
  if (!isSupportedCountry(cfg.default_country)) return null;
  return cfg;
}

/** Countries a phone field can offer: listed AND known to the phone data. */
export function phoneCountry(code: string): boolean {
  return COUNTRY_CODES.has(code) && isSupportedCountry(code);
}

/** [E.164, null] for a valid number with an allowed dialling code, else [null, why]
 *  — the same rule as velaris_shared/forms/validate.py `parse_phone` (full data on
 *  both sides; allowed countries matched by dialling code). */
export function parsePhone(cfg: { default_country: string; countries?: string[] }, val: string): [string | null, string | null] {
  if (val.length > 40) return [null, "Invalid phone number"];
  const num = parsePhoneNumberFromString(val, cfg.default_country as CountryCode);
  if (!num) return [null, "Invalid phone number"];
  if (!num.isValid()) return [null, "Not a valid phone number"];
  const allowed = (cfg.countries || []).filter(c => typeof c === "string" && phoneCountry(c));
  if (allowed.length) {
    const codes = new Set(allowed.map(c => getCountryCallingCode(c as CountryCode)));
    if (!codes.has(num.countryCallingCode)) return [null, `Use a number from ${allowed.join(", ")}`];
  }
  return [num.number, null];
}

/** The value as it is kept: a pin is its two numbers, a range its two days,
 *  tags trimmed, an IBAN without spaces, a country-aware phone number in
 *  international form — nothing else rides along. */
function stored(type: string | undefined, val: any, field?: Record<string, any>): any {
  if (type === "phone" && field && typeof val === "string") {
    const cfg = phoneConfig(field);
    if (cfg) return parsePhone(cfg, val)[0] ?? val;
  }
  if (type === LOCATION) return { lat: val.lat, lng: val.lng };
  if (type === "date_range") return { start: val.start, end: val.end };
  if (type === "tags") return val.map((t: string) => t.trim());
  if (type === "iban") return ibanCompact(val);
  if (type === "address") {
    const out: Record<string, string> = {};
    for (const k of ADDRESS_PARTS) if (typeof val[k] === "string" && val[k].trim()) out[k] = val[k].trim();
    out.country = out.country.toUpperCase();
    return out;
  }
  return val;
}

function typeError(field: FormFieldDef & Record<string, any>, val: any, today: string): string | null {
  const type = field.type || "text";
  let v: Record<string, any> = field.validation && typeof field.validation === "object" ? field.validation : {};

  if (type === "phone" && typeof val === "string") {
    const cfg = phoneConfig(field);
    if (cfg) return parsePhone(cfg, val)[1];
  }

  if (TEXT_TYPES.has(type) && typeof val === "string") {
    if (type === "url" && !URL_RE.test(val)) return "Must be a web address (https://…)";
    if (type === "email") {
      if (!val.includes("@")) return "Invalid email address";
      if (v.format === "strict" && !new RegExp(`^(?:${PATTERN_PRESETS.email})$`, "u").test(val)) return "Invalid email address";
      const domains = (v.allowed_domains || []).filter((d: any) => typeof d === "string" && d)
        .map((d: string) => d.toLowerCase().replace(/^@/, ""));
      if (domains.length) {
        const host = val.slice(val.lastIndexOf("@") + 1).toLowerCase();
        if (!domains.some((d: string) => host === d || host.endsWith("." + d))) return `Use an address at ${domains.join(", ")}`;
      }
    }
    const n = [...val].length;   // characters, as the server counts them
    const loLen = num(v.min_length), hiLen = num(v.max_length);
    if (loLen && n < loLen) return `Must be at least ${v.min_length} chars`;
    if (hiLen && n > hiLen) return `Must be at most ${v.max_length} chars`;
    const msg = patternError(v, val);
    if (msg) return msg;
  }

  if (type === "number" || type === "currency" || type === "slider" || type === "rating" || type === "percentage") {
    const n = num(val);
    if (n === null) return "Must be a number";
    if (type === "percentage") {   // 0–100 unless the designer says otherwise
      v = { min: 0, max: 100, ...Object.fromEntries(Object.entries(v).filter(([, x]) => x !== null && x !== undefined)) };
    }
    const hiRaw = type === "rating" ? v.max_stars : v.max;
    const lo = num(v.min), hi = num(hiRaw);
    if (lo !== null && n < lo) return `Must be >= ${v.min}`;
    if (hi !== null && n > hi) return `Must be <= ${hiRaw}`;
    const dec = num(v.decimals);
    if (dec !== null && decimals(val) > dec) return dec === 0 ? "Use whole numbers" : `Use at most ${v.decimals} decimals`;
  }

  if ((type === "date" || type === "datetime") && typeof val === "string") {
    const day = val.slice(0, 10);
    const lo = v.no_past ? today : dateBound(v.earliest, today);
    const hi = v.no_future ? today : dateBound(v.latest, today);
    if (lo && day < lo) return `Must be on or after ${lo}`;
    if (hi && day > hi) return `Must be on or before ${hi}`;
  }

  if (type === "file_upload") {
    const msg = fileError(v, val);
    if (msg) return msg;
  }

  if (type === LOCATION) return locationError(val);

  if ((type === "toggle" || type === "consent") && typeof val !== "boolean") return "Must be yes or no";

  if (type === "time") {
    if (typeof val !== "string" || !TIME.test(val)) return "Must be a time (HH:MM)";
    if (typeof v.earliest === "string" && TIME.test(v.earliest) && val < v.earliest) return `Must be at or after ${v.earliest}`;
    if (typeof v.latest === "string" && TIME.test(v.latest) && val > v.latest) return `Must be at or before ${v.latest}`;
  }

  if (type === "date_range") return dateRangeError(v, val, today);
  if (type === "tags") return tagsError(v, val);
  if (type === "country" && !COUNTRY_CODES.has(val)) return "Must be a country";
  if (type === "currency_code" && !CURRENCY_CODES.has(val)) return "Must be a currency";
  if (type === "iban") return ibanError(val);
  if (type === "address") return addressError(val);
  if (type === "signature") return signatureError(val);
  if (PICKER_TYPES.has(type) && (typeof val !== "string" || !val.trim() || val.length > 255)) return "Choose from the list";
  if (type === "rich_text") {
    if (typeof val !== "string") return "Must be text";
    const most = Math.min(num(v.max_length) || RICH_TEXT_MAX, RICH_TEXT_MAX);
    if ([...val].length > most) return `Must be at most ${most} chars`;
  }

  if (CHOICE_TYPES.has(type) || type === "multi_select") {
    const chosen = type === "multi_select" ? val : [val];
    if (!Array.isArray(chosen)) return "Must be a list of options";
    // Options a connector supplies at run time are not in the definition.
    const dynamic = field.options_source?.kind === "connector";
    const options = (Array.isArray(field.options) ? field.options : []).map((o: any) => (o && typeof o === "object" ? o.value : o));
    if (options.length && !dynamic && chosen.some((c: any) => !options.includes(c))) return "Must be one of the options";
    if (type === "multi_select") {
      const lo = num(v.min_selected), hi = num(v.max_selected);
      if (lo !== null && chosen.length < lo) return `Choose at least ${v.min_selected}`;
      if (hi !== null && chosen.length > hi) return `Choose at most ${v.max_selected}`;
    }
  }
  return null;
}

/** The first cross-field check that fails. A check waits until every field it
 *  names has a value; one that cannot be computed passes. */
function checkError(field: Record<string, any>, st: FormState, today: string): string | null {
  if (!Array.isArray(field.checks)) return null;
  for (const c of field.checks) {
    if (!c || typeof c !== "object" || typeof c.expression !== "string" || !c.expression.trim()) continue;
    if ([...references(c.expression)].some(k => isEmpty(st.reads(k, true)))) continue;
    const r = evaluate(c.expression, k => st.reads(k, true), today);
    if (r !== null && r !== undefined && !truthy(r)) return String(c.message || "This value does not pass a check");
  }
  return null;
}

/** The values to send and the errors by field key. No errors = submit. */
export function validateForm(
  sections: FormSectionDef[] | undefined, values: Values, today: string = localToday(),
): { clean: Values; errors: Record<string, string> } {
  return check(formState(sections, values, today), values, today);
}

/** validateForm for one form — or one row of a repeating group (its own state). */
function check(st: FormState, values: Values, today: string): { clean: Values; errors: Record<string, string> } {
  const clean: Values = { ...values };
  const errors: Record<string, string> = {};

  for (const field of st.order) {
    const key = fieldKey(field);
    if (STATIC_TYPES.has(field.type)) { delete clean[key]; continue; }
    // Agreeing is the person's own act: a hidden or read-only consent never
    // takes a designer's default, so it is simply not given.
    if (field.type === "consent" && st.locked(key)) { delete clean[key]; continue; }
    const calculated = calcOf(field) !== null;
    const group = field.type === GROUP;
    if (field.hidden && !calculated && !group) {
      delete clean[key];
      if (field.default_value !== undefined && field.default_value !== null) clean[key] = field.default_value;
      continue;
    }
    if (!field.hidden && !st.shown(key)) { delete clean[key]; continue; }
    if (group) {
      const rows = st.value(key);
      delete clean[key];
      if (rows !== null && rows !== undefined) clean[key] = rows;
      const rowErr = st.rowErrors[key] ?? {};
      if (rowErr[key]) { Object.assign(errors, rowErr); continue; }
      const n = Array.isArray(rows) ? rows.length : 0;
      const lo = num(field.min_rows), hi = num(field.max_rows);
      const label = field.label || key;
      if (n === 0 && st.required(key)) errors[key] = String(field.required_message || `${label} is required`);
      else if (lo !== null && n < lo) errors[key] = `Add at least ${field.min_rows} rows`;
      else if (hi !== null && n > hi) errors[key] = `At most ${field.max_rows} rows`;
      else if (n) { const m = checkError(field, st, today); if (m) errors[key] = m; }
      Object.assign(errors, rowErr);
      continue;
    }
    let val: any;
    if (calculated) {
      delete clean[key];
      val = st.value(key);
      if (val !== null && val !== undefined) clean[key] = val;
      if (field.hidden) continue;
    } else if (st.locked(key)) {
      delete clean[key];
      if (field.default_value !== undefined && field.default_value !== null) clean[key] = field.default_value;
      continue;
    } else {
      val = values[key];
    }
    if (field.type === "checkbox" && field.validation?.must_be_ticked && val !== true) {
      errors[key] = field.required_message || "Must be ticked";
      continue;
    }
    if (field.type === "consent" && st.required(key) && val !== true) {
      errors[key] = String(field.required_message || "Please agree to continue");
      continue;
    }
    if (isEmpty(val)) {
      if (st.required(key)) errors[key] = String(field.required_message || `${field.label || key} is required`);
      continue;
    }
    const msg = typeError(field, val, today) ?? checkError(field, st, today);
    if (msg) { errors[key] = msg; continue; }
    clean[key] = stored(field.type, val, field);
  }
  return { clean, errors };
}
