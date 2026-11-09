/* ═══════════════════════════════════════════════════════════════════
   The form expression language — Studio's twin of the server's
   velaris_shared/forms/expr.py (Form Builder v2, 2026-10-28).

   Calculated values, conditions and cross-field checks. Both sides run
   expression_cases.json, so a formula means the same thing in the
   browser and on the server — and the server recomputes on submit.

   Parsed into a tree, never eval'd; bounded size; evaluation never
   throws (a value it cannot compute is null).
   ═══════════════════════════════════════════════════════════════════ */

export const MAX_LENGTH = 2000;
const MAX_NODES = 500;
const MAX_DEPTH = 64;
const MAX_TEXT = 10000;
const MAX_ROUND = 10;

export const FUNCTIONS: Record<string, [number, number | null]> = {
  if: [2, 3], coalesce: [1, null], is_empty: [1, 1],
  round: [1, 2], floor: [1, 1], ceil: [1, 1], abs: [1, 1],
  min: [1, null], max: [1, null], sum: [1, null], avg: [1, null], count: [1, null],
  len: [1, 1], upper: [1, 1], lower: [1, 1], trim: [1, 1], concat: [1, null],
  contains: [2, 2], number: [1, 1], text: [1, 1],
  today: [0, 0], days_between: [2, 2], years_between: [2, 2], add_days: [2, 2],
};

const NUMERIC = /^\s*-?(?:\d+(?:\.\d*)?|\.\d+)\s*$/;
const DATE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$/;
const IDENT = /[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*/y;
const NUMBER = /(?:\d+(?:\.\d+)?|\.\d+)/y;
const WORDS = new Set(["and", "or", "not", "true", "false", "null"]);

export class ExprError extends Error {}

export type Node =
  | ["lit", any]
  | ["ref", string, boolean]
  | ["call", string, Node[]]
  | ["not", Node] | ["neg", Node]
  | ["and", Node, Node] | ["or", Node, Node]
  | ["bin", string, Node, Node];

type Tok = [string, any];

function tokens(src: string): Tok[] {
  const out: Tok[] = [];
  let i = 0;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    if (/\s/.test(c)) { i++; continue; }
    if (c === '"' || c === "'") {
      let j = i + 1, buf = "";
      for (;;) {
        if (j >= n) throw new ExprError("A piece of text is not closed — add the missing quote.");
        if (src[j] === c) {
          if (j + 1 < n && src[j + 1] === c) { buf += c; j += 2; continue; }
          break;
        }
        buf += src[j]; j++;
      }
      out.push(["str", buf]);
      i = j + 1;
      continue;
    }
    if (c === "{") {
      const j = src.indexOf("}", i + 1);
      if (j < 0 || j === i + 1) throw new ExprError("A field in { } is not closed or is empty.");
      out.push(["bref", src.slice(i + 1, j).trim()]);
      i = j + 1;
      continue;
    }
    if (/[0-9.]/.test(c)) {
      NUMBER.lastIndex = i;
      const m = NUMBER.exec(src);
      if (m) { out.push(["num", parseFloat(m[0])]); i = NUMBER.lastIndex; continue; }
    }
    IDENT.lastIndex = i;
    const m = IDENT.exec(src);
    if (m) {
      const low = m[0].toLowerCase();
      out.push(WORDS.has(low) ? ["word", low] : ["name", m[0]]);
      i = IDENT.lastIndex;
      continue;
    }
    const two = src.slice(i, i + 2);
    if (["==", "!=", "<>", "<=", ">=", "&&", "||"].includes(two)) {
      out.push(["op", ({ "==": "=", "<>": "!=", "&&": "and", "||": "or" } as Record<string, string>)[two] ?? two]);
      i += 2;
      continue;
    }
    if ("+-*/%^&=<>!(),×÷".includes(c)) {
      out.push(["op", ({ "×": "*", "÷": "/", "!": "not" } as Record<string, string>)[c] ?? c]);
      i++;
      continue;
    }
    throw new ExprError(`“${c}” cannot be used here.`);
  }
  return out;
}

class Parser {
  i = 0; nodes = 0; depth = 0;
  constructor(private t: Tok[]) {}
  peek(): Tok | undefined { return this.t[this.i]; }
  take(kind: string, value?: any): boolean {
    const p = this.peek();
    if (p && (p[0] === kind || (kind === "op" && p[0] === "word")) && (value === undefined || p[1] === value)) {
      this.i++;
      return true;
    }
    return false;
  }
  node<T extends Node>(n: T): T {
    if (++this.nodes > MAX_NODES) throw new ExprError("This formula is too long — split it over more fields.");
    return n;
  }
  nest<T>(f: () => T): T {
    if (++this.depth > MAX_DEPTH) throw new ExprError("This formula is nested too deeply.");
    try { return f(); } finally { this.depth--; }
  }
  parse(): Node {
    if (!this.t.length) throw new ExprError("The formula is empty.");
    const n = this.or();
    const p = this.peek();
    if (p) throw new ExprError(`Unexpected “${p[1]}” — is an operator or a comma missing?`);
    return n;
  }
  or(): Node {
    let a = this.and();
    while (this.take("op", "or")) a = this.node(["or", a, this.and()]);
    return a;
  }
  and(): Node {
    let a = this.not();
    while (this.take("op", "and")) a = this.node(["and", a, this.not()]);
    return a;
  }
  not(): Node {
    if (this.take("op", "not")) return this.nest(() => this.node(["not", this.not()]));
    return this.cmp();
  }
  cmp(): Node {
    let a = this.add();
    const p = this.peek();
    if (p && p[0] === "op" && ["=", "!=", "<", "<=", ">", ">="].includes(p[1])) {
      this.i++;
      a = this.node(["bin", p[1], a, this.add()]);
    }
    return a;
  }
  add(): Node {
    let a = this.mul();
    for (;;) {
      const p = this.peek();
      if (p && p[0] === "op" && ["+", "-", "&"].includes(p[1])) { this.i++; a = this.node(["bin", p[1], a, this.mul()]); }
      else return a;
    }
  }
  mul(): Node {
    let a = this.unary();
    for (;;) {
      const p = this.peek();
      if (p && p[0] === "op" && ["*", "/", "%"].includes(p[1])) { this.i++; a = this.node(["bin", p[1], a, this.unary()]); }
      else return a;
    }
  }
  unary(): Node {
    if (this.take("op", "-")) return this.nest(() => this.node(["neg", this.unary()]));
    if (this.take("op", "+")) return this.unary();
    return this.power();
  }
  power(): Node {
    const a = this.primary();
    if (this.take("op", "^")) return this.nest(() => this.node(["bin", "^", a, this.unary()]));
    return a;
  }
  primary(): Node {
    const p = this.peek();
    if (!p) throw new ExprError("The formula ends too early — something is missing at the end.");
    const [kind, v] = p;
    this.i++;
    if (kind === "num" || kind === "str") return this.node(["lit", v]);
    if (kind === "word" && (v === "true" || v === "false" || v === "null"))
      return this.node(["lit", v === "true" ? true : v === "false" ? false : null]);
    if (kind === "bref") return this.node(["ref", v, true]);
    if (kind === "name") {
      if (this.take("op", "(")) {
        const name = String(v).toLowerCase();
        if (!(name in FUNCTIONS)) throw new ExprError(`There is no function called ${v}.`);
        const args: Node[] = [];
        this.nest(() => {
          if (!this.take("op", ")")) {
            for (;;) {
              args.push(this.or());
              if (this.take("op", ")")) break;
              if (!this.take("op", ",")) throw new ExprError(`${v}( is not closed — add “)”.`);
            }
          }
        });
        const [lo, hi] = FUNCTIONS[name];
        if (args.length < lo || (hi !== null && args.length > hi)) {
          const want = lo === hi ? `${lo}` : hi === null ? `at least ${lo}` : `${lo} to ${hi}`;
          throw new ExprError(`${name}() takes ${want} value(s), not ${args.length}.`);
        }
        return this.node(["call", name, args]);
      }
      return this.node(["ref", v, false]);
    }
    if (kind === "op" && v === "(") {
      const n = this.nest(() => this.or());
      if (!this.take("op", ")")) throw new ExprError("A bracket is not closed — add “)”.");
      return n;
    }
    throw new ExprError(`Unexpected “${v}”.`);
  }
}

const cache = new Map<string, Node>();

export function parseExpr(src: any): Node {
  if (typeof src !== "string") throw new ExprError("A formula must be text.");
  const hit = cache.get(src);
  if (hit) return hit;
  if (src.length > MAX_LENGTH) throw new ExprError(`This formula is longer than ${MAX_LENGTH} characters.`);
  let s = src.trim();
  if (s.startsWith("=")) s = s.slice(1);
  const tree = new Parser(tokens(s)).parse();
  if (cache.size > 1024) cache.clear();
  cache.set(src, tree);
  return tree;
}

/** null when `src` parses; else the message to show the designer. */
export function checkExpr(src: any): string | null {
  try { parseExpr(src); return null; }
  catch (e: any) { return e instanceof ExprError ? e.message : "This formula is nested too deeply."; }
}

/** The field keys `src` reads (the first segment of a dotted path). */
export function references(src: any): Set<string> {
  const out = new Set<string>();
  let tree: Node;
  try { tree = parseExpr(src); } catch { return out; }
  const walk = (n: Node) => {
    if (n[0] === "ref") { out.add(n[2] ? n[1] : n[1].split(".")[0]); return; }
    if (n[0] === "lit") return;
    if (n[0] === "call") { n[2].forEach(walk); return; }
    for (const part of n.slice(1)) if (Array.isArray(part)) walk(part as Node);
  };
  walk(tree);
  return out;
}

/** Every field path `src` reads, whole: "items.qty", "rec.city", "total". */
export function referencePaths(src: any): string[] {
  const out = new Set<string>();
  let tree: Node;
  try { tree = parseExpr(src); } catch { return []; }
  const walk = (n: Node) => {
    if (n[0] === "ref") { out.add(n[1]); return; }
    if (n[0] === "lit") return;
    if (n[0] === "call") { n[2].forEach(walk); return; }
    for (const part of n.slice(1)) if (Array.isArray(part)) walk(part as Node);
  };
  walk(tree);
  return [...out];
}

/* ── values ──────────────────────────────────────────────────── */

export function isEmpty(v: any): boolean {
  return v === null || v === undefined || v === "" ||
    (Array.isArray(v) && v.length === 0) ||
    (typeof v === "object" && !Array.isArray(v) && Object.keys(v).length === 0);
}

export function truthy(v: any): boolean {
  if (typeof v === "boolean") return v;
  if (typeof v === "number") return v !== 0 && !Number.isNaN(v);
  return !isEmpty(v);
}

export function toNum(v: any): number | null {
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && NUMERIC.test(v)) return parseFloat(v);
  return null;
}

export function toText(v: any): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isFinite(v) ? (Object.is(v, -0) ? "0" : String(v)) : "";
  if (Array.isArray(v)) return v.map(toText).join(", ");
  if (typeof v === "object") return "";
  return String(v);
}

function out(v: any): any {
  if (typeof v === "number" && !Number.isFinite(v)) return null;
  if (Object.is(v, -0)) return 0;
  if (typeof v === "string" && v.length > MAX_TEXT) return null;
  return v === undefined ? null : v;
}

function toDate(v: any): number | null {
  if (typeof v !== "string") return null;
  const m = DATE.exec(v.trim());
  if (!m) return null;
  const y = +m[1], mo = +m[2], d = +m[3];
  const t = Date.UTC(y, mo - 1, d);
  const back = new Date(t);
  if (back.getUTCFullYear() !== y || back.getUTCMonth() !== mo - 1 || back.getUTCDate() !== d || y < 1) return null;
  return t;
}

function iso(t: number): string {
  const d = new Date(t);
  const y = d.getUTCFullYear();
  if (y < 1 || y > 9999) throw new RangeError("date");
  return `${String(y).padStart(4, "0")}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
}

function flat(args: any[]): any[] {
  return args.flatMap(a => (Array.isArray(a) ? a : [a]));
}

function equal(a: any, b: any): boolean {
  if (Array.isArray(a)) return a.some(x => equal(x, b));
  if (isEmpty(a) || isEmpty(b)) return isEmpty(a) && isEmpty(b);
  const na = toNum(a), nb = toNum(b);
  if (na !== null && nb !== null && typeof a !== "boolean" && typeof b !== "boolean") return na === nb;
  return toText(a) === toText(b);
}

function order(op: string, a: any, b: any): boolean {
  const bad = (v: any) => isEmpty(v) || (typeof v === "object" && v !== null);
  if (bad(a) || bad(b)) return false;
  const na = toNum(a), nb = toNum(b);
  let x: any, y: any;
  if (na !== null && nb !== null) { x = na; y = nb; } else { x = toText(a); y = toText(b); }
  return op === "<" ? x < y : op === "<=" ? x <= y : op === ">" ? x > y : x >= y;
}

function arith(op: string, a: any, b: any): number | null {
  const x = toNum(a), y = toNum(b);
  if (x === null || y === null || isEmpty(a) || isEmpty(b)) return null;
  let r: number;
  switch (op) {
    case "+": r = x + y; break;
    case "-": r = x - y; break;
    case "*": r = x * y; break;
    case "/": if (y === 0) return null; r = x / y; break;
    case "%": if (y === 0) return null; r = x - y * Math.floor(x / y); break;
    default:
      if (x < 0 && !Number.isInteger(y)) return null;
      if (x === 0 && y < 0) return null;
      r = Math.pow(x, y);
  }
  return Number.isFinite(r) ? r : null;
}

function roundHalfAway(x: number, n: number): number {
  const p = Math.pow(10, n);
  const v = parseFloat((Math.abs(x) * p).toPrecision(15));
  return (x < 0 ? -1 : 1) * (Math.floor(v + 0.5) / p);
}

function call(name: string, args: any[], today: string): any {
  switch (name) {
    case "coalesce": return args.find(a => !isEmpty(a)) ?? null;
    case "is_empty": return isEmpty(args[0]);
    case "round": case "floor": case "ceil": case "abs": {
      const x = toNum(args[0]);
      if (x === null || isEmpty(args[0])) return null;
      if (name === "round") {
        const n = args.length > 1 ? toNum(args[1]) : 0;
        if (n === null || !Number.isInteger(n) || n < 0 || n > MAX_ROUND) return null;
        return roundHalfAway(x, n);
      }
      return name === "floor" ? Math.floor(x) : name === "ceil" ? Math.ceil(x) : Math.abs(x);
    }
    case "min": case "max": case "sum": case "avg": {
      const nums = flat(args).filter(a => !isEmpty(a)).map(toNum).filter((n): n is number => n !== null);
      let total = 0;
      for (const x of nums) total += x;
      if (name === "sum") return total;
      if (!nums.length) return null;
      if (name === "avg") return total / nums.length;
      return name === "min" ? Math.min(...nums) : Math.max(...nums);
    }
    case "count": return flat(args).filter(a => !isEmpty(a)).length;
    case "len": return [...toText(args[0])].length;
    case "upper": return toText(args[0]).toUpperCase();
    case "lower": return toText(args[0]).toLowerCase();
    case "trim": return toText(args[0]).trim();
    case "concat": return args.map(toText).join("");
    case "contains": {
      const [hay, needle] = args;
      if (Array.isArray(hay)) return hay.some(x => equal(x, needle));
      return isEmpty(needle) ? false : toText(hay).includes(toText(needle));
    }
    case "number": return isEmpty(args[0]) ? null : toNum(args[0]);
    case "text": return toText(args[0]);
    case "today": return today;
    case "days_between": case "years_between": {
      const a = toDate(args[0]), b = toDate(args[1]);
      if (a === null || b === null) return null;
      if (name === "days_between") return Math.round((b - a) / 86400000);
      const da = new Date(a), db = new Date(b);
      const before = db.getUTCMonth() < da.getUTCMonth() ||
        (db.getUTCMonth() === da.getUTCMonth() && db.getUTCDate() < da.getUTCDate());
      return db.getUTCFullYear() - da.getUTCFullYear() - (before ? 1 : 0);
    }
    case "add_days": {
      const d = toDate(args[0]), n = toNum(args[1]);
      if (d === null || n === null || !Number.isInteger(n) || Math.abs(n) > 100000) return null;
      try { return iso(d + n * 86400000); } catch { return null; }
    }
  }
  return null;
}

/** Follow `rest` into `v`; through a list (a repeating group's rows) it collects every row's value. */
function path(v: any, rest: string[]): any {
  for (let i = 0; i < rest.length; i++) {
    if (Array.isArray(v)) { const tail = rest.slice(i); return v.map(e => path(e, tail) ?? null); }
    v = v && typeof v === "object" ? v[rest[i]] : undefined;
  }
  return v;
}

/** The value of `src`, reading fields through `lookup(key)`. Never throws. */
export function evaluate(src: any, lookup: (key: string) => any, today: string): any {
  let tree: Node;
  try { tree = Array.isArray(src) ? (src as Node) : parseExpr(src); } catch { return null; }

  const ev = (n: Node): any => {
    switch (n[0]) {
      case "lit": return n[1];
      case "ref": {
        const key = n[1];
        if (key.includes(".") && !n[2]) {
          const [head, ...rest] = key.split(".");
          const whole = lookup(key);
          return whole !== undefined && whole !== null ? whole : path(lookup(head), rest) ?? null;
        }
        return lookup(key) ?? null;
      }
      case "not": return !truthy(ev(n[1]));
      case "neg": { const x = ev(n[1]); const y = toNum(x); return y === null || isEmpty(x) ? null : -y; }
      case "and": return truthy(ev(n[1])) && truthy(ev(n[2]));
      case "or": return truthy(ev(n[1])) || truthy(ev(n[2]));
      case "call": {
        const [, name, args] = n;
        if (name === "if") return truthy(ev(args[0])) ? ev(args[1]) : args.length > 2 ? ev(args[2]) : null;
        return call(name, args.map(ev), today);
      }
      case "bin": {
        const [, op, an, bn] = n;
        const a = ev(an), b = ev(bn);
        if (op === "=") return equal(a, b);
        if (op === "!=") return !equal(a, b);
        if (["<", "<=", ">", ">="].includes(op)) return order(op, a, b);
        if (op === "&") { const t = toText(a) + toText(b); return t.length <= MAX_TEXT ? t : null; }
        return arith(op, a, b);
      }
    }
    return null;
  };

  try { return out(ev(tree)); } catch { return null; }
}

/* ── for the builder ─────────────────────────────────────────── */

const RESERVED = new Set([...WORDS]);

/** How a field key is written in a formula: bare when it can be, else in braces. */
export function refFor(key: string): string {
  return /^[A-Za-z_][A-Za-z0-9_]*$/.test(key) && !RESERVED.has(key.toLowerCase()) ? key : `{${key}}`;
}

const FN_TEXT: Record<string, (a: string[]) => string> = {
  if: a => `if ${a[0]} then ${a[1]}${a[2] !== undefined ? ` otherwise ${a[2]}` : ""}`,
  coalesce: a => `the first filled of ${a.join(", ")}`,
  is_empty: a => `${a[0]} is empty`,
  round: a => `${a[0]} rounded${a[1] !== undefined ? ` to ${a[1]} decimals` : ""}`,
  floor: a => `${a[0]} rounded down`, ceil: a => `${a[0]} rounded up`, abs: a => `${a[0]} without its sign`,
  min: a => `the smallest of ${a.join(", ")}`, max: a => `the largest of ${a.join(", ")}`,
  sum: a => `the sum of ${a.join(", ")}`, avg: a => `the average of ${a.join(", ")}`,
  count: a => `how many of ${a.join(", ")} are filled`, len: a => `the length of ${a[0]}`,
  upper: a => `${a[0]} in capitals`, lower: a => `${a[0]} in small letters`, trim: a => `${a[0]} without outer spaces`,
  concat: a => a.join(" + "), contains: a => `${a[0]} contains ${a[1]}`,
  number: a => `${a[0]} as a number`, text: a => `${a[0]} as text`, today: () => "today",
  days_between: a => `days from ${a[0]} to ${a[1]}`, years_between: a => `full years from ${a[0]} to ${a[1]}`,
  add_days: a => `${a[0]} plus ${a[1]} days`,
};
const OP_TEXT: Record<string, string> = {
  "+": "+", "-": "−", "*": "×", "/": "÷", "%": "remainder of dividing by", "^": "to the power of", "&": "followed by",
  "=": "is", "!=": "is not", "<": "is less than", "<=": "is at most", ">": "is more than", ">=": "is at least",
};

/** A formula in words, with field labels: "Quantity × Unit price". null when it does not parse. */
export function describeExpr(src: any, label: (key: string) => string): string | null {
  let tree: Node;
  try { tree = parseExpr(src); } catch { return null; }
  const d = (n: Node, top = false): string => {
    switch (n[0]) {
      case "lit": return n[1] === null ? "nothing" : typeof n[1] === "string" ? `“${n[1]}”` : typeof n[1] === "boolean" ? (n[1] ? "yes" : "no") : toText(n[1]);
      case "ref": {
        if (n[2]) return label(n[1]);
        const [head, ...rest] = n[1].split(".");
        return rest.length ? `${label(head)} › ${rest.join(" › ")}` : label(head);
      }
      case "not": return `not (${d(n[1])})`;
      case "neg": return `−${d(n[1])}`;
      case "and": case "or": { const t = `${d(n[1])} ${n[0]} ${d(n[2])}`; return top ? t : `(${t})`; }
      case "call": return (FN_TEXT[n[1]] ?? (a => `${n[1]}(${a.join(", ")})`))(n[2].map(x => d(x, true)));
      case "bin": {
        const t = `${d(n[2])} ${OP_TEXT[n[1]] ?? n[1]} ${d(n[3])}`;
        return top ? t : `(${t})`;
      }
    }
    return "";
  };
  return d(tree, true);
}

/** `src` with the field `from` renamed `to` — strings and functions untouched.
 *  A dotted `from` ("items.qty") renames that path: a repeating group's column. */
export function renameRef(src: string, from: string, to: string): string {
  if (typeof src !== "string" || !from || from === to) return src;
  if (from.includes(".")) {
    const [fg, ...fr] = from.split("."), [, ...tr] = to.split(".");
    let out = "", i = 0;
    while (i < src.length) {
      const c = src[i];
      if (c === '"' || c === "'") {
        let j = i + 1;
        while (j < src.length) { if (src[j] === c) { if (src[j + 1] === c) { j += 2; continue; } break; } j++; }
        out += src.slice(i, j + 1); i = j + 1; continue;
      }
      IDENT.lastIndex = i;
      const m = /[A-Za-z_]/.test(c) && (i === 0 || !/[A-Za-z0-9_.{]/.test(src[i - 1])) ? IDENT.exec(src) : null;
      if (m) {
        const parts = m[0].split(".");
        const hit = parts[0] === fg && fr.every((p, k) => parts[k + 1] === p);
        out += hit ? [fg, ...tr, ...parts.slice(fr.length + 1)].join(".") : m[0];
        i = IDENT.lastIndex; continue;
      }
      out += c; i++;
    }
    return out;
  }
  let out = "", i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === '"' || c === "'") {
      let j = i + 1;
      while (j < src.length) { if (src[j] === c) { if (src[j + 1] === c) { j += 2; continue; } break; } j++; }
      out += src.slice(i, j + 1); i = j + 1; continue;
    }
    if (c === "{") {
      const j = src.indexOf("}", i + 1);
      if (j < 0) { out += src.slice(i); break; }
      const inner = src.slice(i + 1, j).trim();
      out += inner === from ? `{${to}}` : src.slice(i, j + 1);
      i = j + 1; continue;
    }
    IDENT.lastIndex = i;
    const m = /[A-Za-z_]/.test(c) && (i === 0 || !/[A-Za-z0-9_.]/.test(src[i - 1])) ? IDENT.exec(src) : null;
    if (m) {
      const [head, ...rest] = m[0].split(".");
      const isCall = /^\s*\(/.test(src.slice(IDENT.lastIndex));
      out += head === from && !isCall ? [refFor(to), ...rest].join(".") : m[0];
      i = IDENT.lastIndex; continue;
    }
    out += c; i++;
  }
  return out;
}
