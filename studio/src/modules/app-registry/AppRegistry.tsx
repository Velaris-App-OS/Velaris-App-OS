/**
 * P43 — App Registry: package, version, promote, diff Helix applications.
 * Tabs: Packages · Deployments · Diff
 */
import React, { useState, useEffect, useCallback } from "react";

const API = "/api/v1/apps";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
}

type Pkg = {
  id: string; name: string; version: string;
  description: string | null; status: string;
  created_by: string | null; created_at: string | null;
};
type Deployment = {
  id: string; package_id: string; package: string;
  environment: string; status: string;
  deployed_by: string | null; deployed_at: string; notes: string | null;
};
type DiffSection = {
  added: string[]; removed: string[]; changed: { id: string; label: string }[];
  unchanged: number; total_old: number; total_new: number;
};
type DiffResult = {
  has_changes: boolean;
  summary: { added: number; removed: number; changed: number };
  package_a: { name: string; version: string };
  package_b: { name: string; version: string };
  sections: Record<string, DiffSection>;
};

const STATUS_COLOR: Record<string, string> = {
  draft: "#f59e0b",
  published: "#22c55e",
  deprecated: "#94a3b8",
};
const ENV_COLOR: Record<string, string> = {
  dev: "#0d9488",
  staging: "#3b82f6",
  uat: "#f59e0b",
  prod: "#22c55e",
};

const S: Record<string, React.CSSProperties> = {
  page:       { display: "flex", flexDirection: "column", height: "100%", background: "var(--bg-main)", color: "var(--text-primary)", fontFamily: "system-ui, sans-serif" },
  header:     { padding: "18px 24px 0", flexShrink: 0 },
  title:      { fontSize: 20, fontWeight: 700, margin: 0 },
  sub:        { fontSize: 13, color: "var(--text-secondary)", marginTop: 4 },
  tabs:       { display: "flex", gap: 4, padding: "14px 24px 0", borderBottom: "1px solid var(--border)", flexShrink: 0 },
  tab:        { padding: "8px 20px", border: "none", background: "none", fontSize: 13, cursor: "pointer", borderBottom: "2px solid transparent", fontWeight: 400, color: "var(--text-secondary)" },
  tabActive:  { borderBottom: "2px solid var(--accent)", color: "var(--accent)", fontWeight: 700 },
  body:       { flex: 1, overflow: "hidden", display: "flex" },
  sidebar:    { width: 260, borderRight: "1px solid var(--border)", display: "flex", flexDirection: "column", background: "var(--bg-surface)", flexShrink: 0 },
  sideHead:   { padding: "12px 14px", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", gap: 8, flexShrink: 0 },
  list:       { flex: 1, overflow: "auto" },
  listItem:   { padding: "10px 14px", cursor: "pointer", borderBottom: "1px solid var(--border)" },
  detail:     { flex: 1, overflow: "auto", padding: "24px 28px" },
  btn:        { padding: "7px 16px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 700 },
  btnPrimary: { background: "var(--accent)", color: "#fff" },
  btnSecond:  { background: "var(--bg-surface)", border: "1px solid var(--border)", color: "var(--text-secondary)" },
  badge:      { display: "inline-block", padding: "2px 8px", borderRadius: 10, fontSize: 10, fontWeight: 700, textTransform: "uppercase" as const },
  input:      { width: "100%", padding: "7px 10px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "var(--bg-main)", color: "var(--text-primary)" },
  label:      { fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 4, display: "block" },
  row:        { display: "flex", gap: 12, marginBottom: 16 },
  sectionCard:{ background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "12px 16px", marginBottom: 10 },
};


// ── Packages tab ──────────────────────────────────────────────────────────────

function PackagesTab() {
  const [packages, setPackages] = useState<Pkg[]>([]);
  const [selected, setSelected] = useState<Pkg | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", version: "", description: "" });
  const [promoting, setPromoting] = useState<string | null>(null);
  const [promoteNotes, setPromoteNotes] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await authFetch(`${API}/packages`);
    if (r.ok) setPackages((await r.json()).packages);
  }, []);

  useEffect(() => { load(); }, [load]);

  async function loadDetail(pkg: Pkg) {
    setSelected(pkg); setDetail(null);
    const r = await authFetch(`${API}/packages/${pkg.id}`);
    if (r.ok) setDetail(await r.json());
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault(); setLoading(true); setErr(null);
    try {
      const r = await authFetch(`${API}/package`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (!r.ok) { setErr((await r.json()).detail || "Error"); return; }
      setForm({ name: "", version: "", description: "" });
      setCreating(false);
      await load();
    } finally { setLoading(false); }
  }

  async function handleStatusChange(pkg: Pkg, status: string) {
    await authFetch(`${API}/packages/${pkg.id}/status`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    });
    await load();
    if (selected?.id === pkg.id) loadDetail({ ...pkg, status });
  }

  async function handleDownload(pkg: Pkg) {
    const r = await authFetch(`${API}/packages/${pkg.id}/download`);
    if (!r.ok) return;
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${pkg.name.replace(/ /g, "_")}_v${pkg.version}.zip`;
    a.click();
  }

  async function handlePromote(pkg: Pkg, env: string) {
    setPromoting(env);
    try {
      await authFetch(`${API}/packages/${pkg.id}/promote/${env}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes: promoteNotes || null }),
      });
      setPromoteNotes("");
    } finally { setPromoting(null); }
  }

  return (
    <div style={S.body}>
      {/* Sidebar */}
      <div style={S.sidebar}>
        <div style={S.sideHead}>
          <span style={{ flex: 1, fontSize: 12, fontWeight: 700, color: "var(--text-secondary)" }}>PACKAGES</span>
          <button style={{ ...S.btn, ...S.btnPrimary, padding: "5px 10px", fontSize: 11 }}
                  onClick={() => setCreating(c => !c)}>+ New</button>
        </div>
        {creating && (
          <form onSubmit={handleCreate} style={{ padding: 12, borderBottom: "1px solid var(--border)" }}>
            <div style={{ marginBottom: 8 }}>
              <span style={S.label}>App name</span>
              <input style={S.input} value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} required placeholder="MyApp" />
            </div>
            <div style={{ marginBottom: 8 }}>
              <span style={S.label}>Version</span>
              <input style={S.input} value={form.version} onChange={e => setForm(f => ({ ...f, version: e.target.value }))} required placeholder="1.0.0" />
            </div>
            <div style={{ marginBottom: 10 }}>
              <span style={S.label}>Description (optional)</span>
              <input style={S.input} value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} placeholder="Release notes…" />
            </div>
            {err && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{err}</div>}
            <button type="submit" disabled={loading} style={{ ...S.btn, ...S.btnPrimary, width: "100%" }}>
              {loading ? "Packaging…" : "⬡ Package Now"}
            </button>
          </form>
        )}
        <div style={S.list}>
          {packages.map(p => (
            <div key={p.id} onClick={() => loadDetail(p)} style={{
              ...S.listItem,
              background: selected?.id === p.id ? "var(--accent-light, #ede9fe)" : "transparent",
            }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>{p.name}</div>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 3 }}>
                <span style={{ fontSize: 11, color: "var(--text-secondary)" }}>v{p.version}</span>
                <span style={{ ...S.badge, background: (STATUS_COLOR[p.status] || "#6b7280") + "22", color: STATUS_COLOR[p.status] || "#6b7280" }}>{p.status}</span>
              </div>
            </div>
          ))}
          {packages.length === 0 && (
            <div style={{ padding: 16, fontSize: 12, color: "var(--text-secondary)" }}>No packages yet.</div>
          )}
        </div>
      </div>

      {/* Detail */}
      <div style={S.detail}>
        {!selected && (
          <div style={{ color: "var(--text-secondary)", fontSize: 13, paddingTop: 40 }}>
            Select a package to view details, or create a new one.
          </div>
        )}
        {selected && detail && (
          <>
            <div style={{ display: "flex", alignItems: "flex-start", gap: 16, marginBottom: 20 }}>
              <div style={{ flex: 1 }}>
                <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700 }}>{detail.name}</h2>
                <div style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 4 }}>
                  v{detail.version} · {detail.created_at ? new Date(detail.created_at).toLocaleString() : "—"}
                </div>
                {detail.description && <div style={{ fontSize: 13, marginTop: 8 }}>{detail.description}</div>}
              </div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <button style={{ ...S.btn, ...S.btnSecond }} onClick={() => handleDownload(selected)}>↓ Download ZIP</button>
                {detail.status === "draft" && (
                  <button style={{ ...S.btn, ...S.btnPrimary }} onClick={() => handleStatusChange(selected, "published")}>✓ Publish</button>
                )}
                {detail.status !== "deprecated" && (
                  <button style={{ ...S.btn, background: "#f3f4f6", border: "1px solid var(--border)", color: "#6b7280" }}
                          onClick={() => handleStatusChange(selected, "deprecated")}>Archive</button>
                )}
              </div>
            </div>

            {/* Manifest */}
            {detail.manifest && (
              <div style={S.sectionCard}>
                <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 10 }}>MANIFEST</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
                  {Object.entries(detail.manifest).filter(([k]) => !["checksum", "packaged_at"].includes(k)).map(([k, v]) => (
                    <div key={k}>
                      <div style={{ fontSize: 10, color: "var(--text-secondary)", textTransform: "uppercase" }}>{k.replace(/_/g, " ")}</div>
                      <div style={{ fontSize: 18, fontWeight: 700, color: "var(--accent)" }}>{String(v)}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Promote */}
            <div style={S.sectionCard}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 10 }}>PROMOTE TO ENVIRONMENT</div>
              <div style={{ marginBottom: 8 }}>
                <input style={S.input} value={promoteNotes}
                       onChange={e => setPromoteNotes(e.target.value)}
                       placeholder="Optional promotion notes…" />
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                {["dev", "staging", "uat", "prod"].map(env => (
                  <button key={env} disabled={!!promoting}
                          onClick={() => handlePromote(selected, env)}
                          style={{
                            ...S.btn,
                            background: ENV_COLOR[env] + "22",
                            color: ENV_COLOR[env],
                            border: `1px solid ${ENV_COLOR[env]}44`,
                          }}>
                    {promoting === env ? "…" : env}
                  </button>
                ))}
              </div>
            </div>
          </>
        )}
        {selected && !detail && (
          <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>Loading…</div>
        )}
      </div>
    </div>
  );
}


// ── Deployments tab ───────────────────────────────────────────────────────────

function DeploymentsTab() {
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [envFilter, setEnvFilter] = useState("");

  useEffect(() => {
    const url = envFilter ? `${API}/deployments?environment=${envFilter}` : `${API}/deployments`;
    authFetch(url).then(r => r.ok ? r.json() : null).then(d => setDeployments(d?.deployments ?? []));
  }, [envFilter]);

  return (
    <div style={{ flex: 1, overflow: "auto", padding: "20px 28px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>Filter:</span>
        {["", "dev", "staging", "uat", "prod"].map(env => (
          <button key={env} onClick={() => setEnvFilter(env)} style={{
            ...S.btn,
            background: envFilter === env ? "var(--accent)" : "var(--bg-surface)",
            color: envFilter === env ? "#fff" : "var(--text-secondary)",
            border: "1px solid var(--border)",
          }}>{env || "All"}</button>
        ))}
      </div>
      {deployments.length === 0 ? (
        <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>No deployments yet.</div>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border)" }}>
              {["Package", "Environment", "Status", "Deployed by", "Deployed at", "Notes"].map(h => (
                <th key={h} style={{ textAlign: "left", padding: "8px 12px", fontWeight: 600, fontSize: 11, color: "var(--text-secondary)", textTransform: "uppercase" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {deployments.map(d => (
              <tr key={d.id} style={{ borderBottom: "1px solid var(--border)" }}>
                <td style={{ padding: "10px 12px", fontWeight: 600 }}>{d.package}</td>
                <td style={{ padding: "10px 12px" }}>
                  <span style={{ ...S.badge, background: (ENV_COLOR[d.environment] || "#6b7280") + "22", color: ENV_COLOR[d.environment] || "#6b7280" }}>{d.environment}</span>
                </td>
                <td style={{ padding: "10px 12px" }}>
                  <span style={{ ...S.badge, background: "#22c55e22", color: "#22c55e" }}>{d.status}</span>
                </td>
                <td style={{ padding: "10px 12px", color: "var(--text-secondary)" }}>{d.deployed_by || "—"}</td>
                <td style={{ padding: "10px 12px", color: "var(--text-secondary)", whiteSpace: "nowrap" }}>{new Date(d.deployed_at).toLocaleString()}</td>
                <td style={{ padding: "10px 12px", color: "var(--text-secondary)" }}>{d.notes || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}


// ── Diff tab ──────────────────────────────────────────────────────────────────

function DiffTab() {
  const [packages, setPackages] = useState<Pkg[]>([]);
  const [pkgA, setPkgA] = useState("");
  const [pkgB, setPkgB] = useState("");
  const [result, setResult] = useState<DiffResult | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    authFetch(`${API}/packages`).then(r => r.ok ? r.json() : null).then(d => setPackages(d?.packages ?? []));
  }, []);

  async function handleDiff(e: React.FormEvent) {
    e.preventDefault(); setLoading(true); setResult(null);
    try {
      const r = await authFetch(`${API}/packages/${pkgA}/diff/${pkgB}`);
      if (r.ok) setResult(await r.json());
    } finally { setLoading(false); }
  }

  const sections = result ? Object.entries(result.sections) : [];
  const changedSections = sections.filter(([, d]) => d.added.length || d.removed.length || d.changed.length);

  return (
    <div style={{ flex: 1, overflow: "auto", padding: "20px 28px" }}>
      <form onSubmit={handleDiff} style={{ display: "flex", gap: 12, alignItems: "flex-end", marginBottom: 24 }}>
        <div style={{ flex: 1 }}>
          <span style={S.label}>Base (older)</span>
          <select value={pkgA} onChange={e => setPkgA(e.target.value)} required
                  style={{ ...S.input }}>
            <option value="">Select package…</option>
            {packages.map(p => <option key={p.id} value={p.id}>{p.name} v{p.version}</option>)}
          </select>
        </div>
        <div style={{ flex: 1 }}>
          <span style={S.label}>Compare (newer)</span>
          <select value={pkgB} onChange={e => setPkgB(e.target.value)} required
                  style={{ ...S.input }}>
            <option value="">Select package…</option>
            {packages.map(p => <option key={p.id} value={p.id}>{p.name} v{p.version}</option>)}
          </select>
        </div>
        <button type="submit" disabled={loading || !pkgA || !pkgB}
                style={{ ...S.btn, ...S.btnPrimary, flexShrink: 0 }}>
          {loading ? "Diffing…" : "Compare"}
        </button>
      </form>

      {result && (
        <>
          <div style={{ ...S.sectionCard, marginBottom: 20 }}>
            <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 8 }}>
              {result.package_a.name} v{result.package_a.version} → {result.package_b.name} v{result.package_b.version}
            </div>
            {!result.has_changes ? (
              <div style={{ color: "#22c55e", fontSize: 13 }}>✓ No changes between these packages.</div>
            ) : (
              <div style={{ display: "flex", gap: 20 }}>
                <div><span style={{ color: "#22c55e", fontWeight: 700 }}>+{result.summary.added}</span> <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>added</span></div>
                <div><span style={{ color: "#ef4444", fontWeight: 700 }}>-{result.summary.removed}</span> <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>removed</span></div>
                <div><span style={{ color: "#f59e0b", fontWeight: 700 }}>{result.summary.changed}</span> <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>changed</span></div>
              </div>
            )}
          </div>

          {changedSections.map(([section, d]) => (
            <div key={section} style={S.sectionCard}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 8, textTransform: "uppercase" }}>{section.replace(/_/g, " ")}</div>
              {d.added.map((name, i) => <div key={i} style={{ fontSize: 12, color: "#22c55e" }}>+ {name}</div>)}
              {d.removed.map((name, i) => <div key={i} style={{ fontSize: 12, color: "#ef4444" }}>− {name}</div>)}
              {d.changed.map((c, i) => <div key={i} style={{ fontSize: 12, color: "#f59e0b" }}>~ {c.label}</div>)}
            </div>
          ))}

          {result.has_changes && changedSections.length === 0 && (
            <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>All changes are in metadata fields.</div>
          )}
        </>
      )}
    </div>
  );
}


// ── Root component ────────────────────────────────────────────────────────────

export default function AppRegistry() {
  const [tab, setTab] = useState<"packages" | "deployments" | "diff">("packages");

  const tabs: { key: typeof tab; label: string }[] = [
    { key: "packages",    label: "Packages" },
    { key: "deployments", label: "Deployments" },
    { key: "diff",        label: "Diff" },
  ];

  return (
    <div style={S.page}>
      <div style={S.tabs}>
        {tabs.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
                  style={{ ...S.tab, ...(tab === t.key ? S.tabActive : {}) }}>
            {t.label}
          </button>
        ))}
      </div>
      <div style={S.body}>
        {tab === "packages"    && <PackagesTab />}
        {tab === "deployments" && <DeploymentsTab />}
        {tab === "diff"        && <DiffTab />}
      </div>
    </div>
  );
}
