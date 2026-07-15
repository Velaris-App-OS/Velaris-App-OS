/**
 * HxStorefront — hosted store builder (marketplace app `velaris/hxstorefront`).
 * Install-gated: when not installed the API 404s and the whole page shows an
 * "install from Marketplace" notice. Multi-store: pick or create a store, then
 * manage its catalogue, inventory, promotions, and theme.
 */
import React, { useState, useEffect, useCallback } from "react";
import { Button } from "@shared/components";

const API = "/api/v1/storefront";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { "Content-Type": "application/json", ..._authHdr(), ...opts.headers } });
}

const S: Record<string, React.CSSProperties> = {
  page:   { padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" as const },
  tabs:   { display: "flex", gap: 2, marginBottom: "var(--space-lg)", borderBottom: "1px solid var(--border)", flexWrap: "wrap" as const },
  tab:    { padding: "8px 16px", fontSize: 13, fontWeight: 500, background: "none", border: "none", borderBottom: "2px solid transparent", cursor: "pointer", color: "var(--text-muted)", marginBottom: -1 },
  tabOn:  { borderBottomColor: "var(--accent)", color: "var(--accent)", fontWeight: 700 },
  card:   { background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8, padding: "var(--space-lg)", marginBottom: "var(--space-md)" },
  label:  { fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase" as const, letterSpacing: "0.05em", marginBottom: 4 },
  input:  { width: "100%", padding: "8px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const, marginBottom: 10 },
  row:    { display: "flex", gap: 10, alignItems: "center", padding: "9px 12px", borderBottom: "1px solid var(--border-subtle)", fontSize: 13 },
  badge:  { fontSize: 10, padding: "2px 8px", borderRadius: 4, fontWeight: 700, color: "#fff" },
  select: { padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)" },
};

const money = (c: number) => (c / 100).toFixed(2);

function NotInstalled() {
  return (
    <div style={S.card}>
      <h3 style={{ marginTop: 0 }}>HxStorefront is not installed</h3>
      <p style={{ color: "var(--text-muted)", fontSize: 13 }}>
        HxStorefront is a marketplace app. Install <code>velaris/hxstorefront</code> from the
        Marketplace to turn on the store builder for your tenant. It also needs
        <code> velaris/hxcheckout</code> installed to accept orders.
      </p>
    </div>
  );
}

// ── Products ───────────────────────────────────────────────────────────────────
function ProductsTab({ slug }: { slug: string }) {
  const [products, setProducts] = useState<any[]>([]);
  const [name, setName] = useState(""); const [price, setPrice] = useState("");
  const load = useCallback(() => {
    authFetch(`${API}/stores/${slug}/products`).then(r => r.json()).then(d => setProducts(d.products || [])).catch(() => {});
  }, [slug]);
  useEffect(() => { load(); }, [load]);
  const create = async () => {
    await authFetch(`${API}/stores/${slug}/products`, { method: "POST",
      body: JSON.stringify({ name, price_cents: Math.round(parseFloat(price || "0") * 100), status: "active" }) });
    setName(""); setPrice(""); load();
  };
  return (
    <div>
      <div style={S.card}>
        <div style={S.label}>New product</div>
        <input style={S.input} placeholder="Name" value={name} onChange={e => setName(e.target.value)} />
        <input style={S.input} placeholder="Price (e.g. 19.99)" value={price} onChange={e => setPrice(e.target.value)} />
        <Button onClick={create}>Add product</Button>
      </div>
      <div style={S.card}>
        {products.length === 0 && <div style={{ color: "var(--text-muted)" }}>No products yet.</div>}
        {products.map(p => (
          <div key={p.id} style={S.row}>
            <span>{p.name}</span>
            <span style={{ ...S.badge, background: p.status === "active" ? "#22c55e" : "#94a3b8" }}>{p.status}</span>
            <span style={{ flex: 1 }} />
            <span>{money(p.price_cents)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Categories ─────────────────────────────────────────────────────────────────
function CategoriesTab({ slug }: { slug: string }) {
  const [cats, setCats] = useState<any[]>([]); const [name, setName] = useState("");
  const load = useCallback(() => {
    authFetch(`${API}/stores/${slug}/categories`).then(r => r.json()).then(d => setCats(d.categories || [])).catch(() => {});
  }, [slug]);
  useEffect(() => { load(); }, [load]);
  const create = async () => { await authFetch(`${API}/stores/${slug}/categories`, { method: "POST", body: JSON.stringify({ name }) }); setName(""); load(); };
  return (
    <div>
      <div style={S.card}>
        <div style={S.label}>New category</div>
        <input style={S.input} placeholder="Name" value={name} onChange={e => setName(e.target.value)} />
        <Button onClick={create}>Add category</Button>
      </div>
      <div style={S.card}>
        {cats.map(c => <div key={c.id} style={S.row}><span>{c.name}</span><span style={{ color: "var(--text-muted)" }}>{c.slug}</span></div>)}
      </div>
    </div>
  );
}

// ── Inventory ──────────────────────────────────────────────────────────────────
function InventoryTab({ slug }: { slug: string }) {
  const [inv, setInv] = useState<any[]>([]);
  const load = useCallback(() => {
    authFetch(`${API}/stores/${slug}/inventory`).then(r => r.json()).then(d => setInv(d.inventory || [])).catch(() => {});
  }, [slug]);
  useEffect(() => { load(); }, [load]);
  const adjust = async (vid: string, change: number) => {
    await authFetch(`${API}/stores/${slug}/inventory/${vid}`, { method: "PATCH", body: JSON.stringify({ change }) });
    load();
  };
  return (
    <div style={S.card}>
      {inv.length === 0 && <div style={{ color: "var(--text-muted)" }}>No variants yet.</div>}
      {inv.map(i => (
        <div key={i.variant_id} style={S.row}>
          <span>{i.product_name}</span>
          <span style={{ color: "var(--text-muted)" }}>{i.sku}</span>
          {i.low_stock && <span style={{ ...S.badge, background: "#f59e0b" }}>low</span>}
          <span style={{ flex: 1 }} />
          <span>{i.stock_quantity ?? "∞"}</span>
          <Button variant="secondary" onClick={() => adjust(i.variant_id, 1)}>+1</Button>
          <Button variant="secondary" onClick={() => adjust(i.variant_id, -1)}>-1</Button>
        </div>
      ))}
    </div>
  );
}

// ── Promotions ─────────────────────────────────────────────────────────────────
function PromotionsTab({ slug }: { slug: string }) {
  const [promos, setPromos] = useState<any[]>([]);
  const [code, setCode] = useState(""); const [pct, setPct] = useState("");
  const load = useCallback(() => {
    authFetch(`${API}/stores/${slug}/promotions`).then(r => r.json()).then(d => setPromos(d.promotions || [])).catch(() => {});
  }, [slug]);
  useEffect(() => { load(); }, [load]);
  const create = async () => {
    await authFetch(`${API}/stores/${slug}/promotions`, { method: "POST",
      body: JSON.stringify({ code, discount_type: "percentage", config: { percent: parseFloat(pct || "0") } }) });
    setCode(""); setPct(""); load();
  };
  return (
    <div>
      <div style={S.card}>
        <div style={S.label}>New discount code</div>
        <input style={S.input} placeholder="Code (e.g. SUMMER10)" value={code} onChange={e => setCode(e.target.value)} />
        <input style={S.input} placeholder="Percent off (e.g. 10)" value={pct} onChange={e => setPct(e.target.value)} />
        <Button onClick={create}>Create promotion</Button>
      </div>
      <div style={S.card}>
        {promos.map(p => (
          <div key={p.id} style={S.row}>
            <span style={{ fontFamily: "var(--font-mono)" }}>{p.code || "(automatic)"}</span>
            <span style={{ color: "var(--text-muted)" }}>{p.discount_type}</span>
            {!p.active && <span style={{ ...S.badge, background: "#94a3b8" }}>inactive</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Theme (JSON editor + CSS preview) ───────────────────────────────────────────
function ThemeTab({ slug }: { slug: string }) {
  const [json, setJson] = useState("{}"); const [css, setCss] = useState(""); const [version, setVersion] = useState(0);
  useEffect(() => {
    authFetch(`${API}/stores/${slug}/theme`).then(r => r.json()).then(d => { setJson(JSON.stringify(d.config || {}, null, 2)); setVersion(d.version || 0); }).catch(() => {});
  }, [slug]);
  const save = async () => {
    try {
      const config = JSON.parse(json);
      const r = await authFetch(`${API}/stores/${slug}/theme`, { method: "PUT", body: JSON.stringify({ config }) });
      if (r.ok) setVersion((await r.json()).version);
    } catch { alert("Invalid JSON"); }
  };
  const preview = async () => {
    try {
      const config = JSON.parse(json);
      const r = await authFetch(`${API}/stores/${slug}/theme/preview`, { method: "POST", body: JSON.stringify({ config }) });
      setCss((await r.json()).css);
    } catch { alert("Invalid JSON"); }
  };
  return (
    <div style={S.card}>
      <div style={S.label}>Theme config (version {version}) — non-destructive, keeps last 10</div>
      <textarea style={{ ...S.input, fontFamily: "var(--font-mono)", minHeight: 200 }} value={json} onChange={e => setJson(e.target.value)} />
      <div style={{ display: "flex", gap: 8 }}><Button onClick={save}>Save version</Button><Button variant="secondary" onClick={preview}>Preview CSS</Button></div>
      {css && <pre style={{ marginTop: 12, fontSize: 11, background: "var(--bg-input)", padding: 12, borderRadius: 6, overflow: "auto" }}>{css}</pre>}
    </div>
  );
}

const STORE_TABS = ["Products", "Categories", "Inventory", "Promotions", "Theme"];

export default function HxStorefront() {
  const [installed, setInstalled] = useState<boolean | null>(null);
  const [stores, setStores] = useState<any[]>([]);
  const [slug, setSlug] = useState<string>("");
  const [tab, setTab] = useState("Products");
  const [newName, setNewName] = useState("");

  const loadStores = useCallback(() => {
    authFetch(`${API}/stores`).then(r => {
      if (r.status === 404) { setInstalled(false); return null; }
      setInstalled(true); return r.json();
    }).then(d => { if (d) { setStores(d.stores || []); if (d.stores?.[0] && !slug) setSlug(d.stores[0].slug); } }).catch(() => setInstalled(false));
  }, [slug]);
  useEffect(() => { loadStores(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const createStore = async () => {
    const r = await authFetch(`${API}/stores`, { method: "POST", body: JSON.stringify({ name: newName }) });
    if (r.ok) { const s = await r.json(); setNewName(""); setSlug(s.slug); loadStores(); }
  };

  if (installed === false) return <div style={S.page}><NotInstalled /></div>;

  return (
    <div style={S.page}>
      <div style={{ ...S.card, display: "flex", gap: 10, alignItems: "center" }}>
        <span style={S.label}>Store</span>
        <select style={S.select} value={slug} onChange={e => setSlug(e.target.value)}>
          {stores.map(s => <option key={s.id} value={s.slug}>{s.name} ({s.slug})</option>)}
        </select>
        <span style={{ flex: 1 }} />
        <input style={{ ...S.input, marginBottom: 0, width: 180 }} placeholder="New store name" value={newName} onChange={e => setNewName(e.target.value)} />
        <Button onClick={createStore}>Create store</Button>
      </div>

      {slug ? (
        <>
          <div style={S.tabs}>
            {STORE_TABS.map(t => <button key={t} style={{ ...S.tab, ...(tab === t ? S.tabOn : {}) }} onClick={() => setTab(t)}>{t}</button>)}
          </div>
          {tab === "Products" && <ProductsTab slug={slug} />}
          {tab === "Categories" && <CategoriesTab slug={slug} />}
          {tab === "Inventory" && <InventoryTab slug={slug} />}
          {tab === "Promotions" && <PromotionsTab slug={slug} />}
          {tab === "Theme" && <ThemeTab slug={slug} />}
        </>
      ) : <div style={S.card}><div style={{ color: "var(--text-muted)" }}>Create a store to begin.</div></div>}
    </div>
  );
}
