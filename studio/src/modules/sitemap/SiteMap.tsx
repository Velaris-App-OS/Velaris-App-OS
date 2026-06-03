import React, { useState, useMemo } from "react";
import { Card } from "@shared/components";
import { useNavigate } from "react-router-dom";
import { NAV_DATA } from "@/app/nav-data";

/* ═══════════════════════════════════════════════════════════════════
   Site Map — central index of every Velaris module.

   Single source of truth: reads directly from nav-data.ts so that
   updating a description there reflects here AND in GlobalSearch.
   ═══════════════════════════════════════════════════════════════════ */

// Map NAV_DATA sections to display categories (same concept, same names)
const modules = NAV_DATA.map(e => ({
  path:        e.path,
  label:       e.label,
  description: e.description,
  category:    e.section,
}));

export default function SiteMap() {
  const navigate   = useNavigate();
  const [query, setQuery]       = useState("");
  const [category, setCategory] = useState<string>("");

  const categories = useMemo(() => {
    const cats: Record<string, number> = {};
    modules.forEach(m => { cats[m.category] = (cats[m.category] || 0) + 1; });
    return cats;
  }, []);

  const filtered = useMemo(() => {
    let list = modules;
    if (category) list = list.filter(m => m.category === category);
    if (query) {
      const q = query.toLowerCase();
      list = list.filter(m =>
        m.label.toLowerCase().includes(q) ||
        m.description.toLowerCase().includes(q)
      );
    }
    return list;
  }, [query, category]);

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>

      {/* Search + filter bar */}
      <Card style={{ marginBottom: "var(--space-lg)" }}>
        <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center" }}>
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="🔍 Search modules..."
            style={{
              flex: 1, padding: "10px 14px", fontSize: 14, fontFamily: "var(--font-body)",
              background: "var(--bg-input)", border: "1px solid var(--border-default)",
              borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
            }}
          />
          <select value={category} onChange={e => setCategory(e.target.value)}
            style={{
              padding: "10px 14px", fontSize: 14,
              background: "var(--bg-input)", border: "1px solid var(--border-default)",
              borderRadius: "var(--radius-sm)", color: "var(--text-primary)",
            }}>
            <option value="">All Categories</option>
            {Object.keys(categories).map(c => (
              <option key={c} value={c}>{c} ({categories[c]})</option>
            ))}
          </select>
        </div>
      </Card>

      {/* Modules grid */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: "var(--space-md)" }}>
        {filtered.map(m => (
          <div key={m.path}
            style={{ cursor: "pointer", transition: "transform 0.1s" }}
            onClick={() => navigate(m.path)}
            onMouseEnter={e => { (e.currentTarget as HTMLDivElement).style.transform = "translateY(-2px)"; }}
            onMouseLeave={e => { (e.currentTarget as HTMLDivElement).style.transform = "translateY(0)"; }}
          >
            <Card style={{ height: "100%", display: "flex", flexDirection: "column", justifyContent: "space-between" }}>
              <div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "var(--space-sm)" }}>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {m.label}
                    </div>
                    <code style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--accent)" }}>
                      {m.path}
                    </code>
                  </div>
                  <span style={{
                    fontSize: 9, padding: "2px 6px", borderRadius: 3, flexShrink: 0, marginLeft: 8,
                    background: categoryColor(m.category),
                    color: "white", fontFamily: "var(--font-mono)", textTransform: "uppercase",
                  }}>{m.category}</span>
                </div>
                <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.6 }}>
                  {m.description.split(" | ").map((tag, i) => (
                    <span key={i} style={{ display: "inline-block", marginRight: 4, marginBottom: 3 }}>
                      {i > 0 && <span style={{ color: "var(--border)", marginRight: 4 }}>·</span>}
                      {tag}
                    </span>
                  ))}
                </div>
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", marginTop: "var(--space-sm)", paddingTop: "var(--space-sm)", borderTop: "1px solid var(--border-subtle)" }}>
                <span style={{ fontSize: 10, color: "var(--accent)" }}>Open →</span>
              </div>
            </Card>
          </div>
        ))}
      </div>

      {filtered.length === 0 && (
        <div style={{ padding: "var(--space-2xl)", color: "var(--text-muted)" }}>
          No modules match your search.
        </div>
      )}
    </div>
  );
}

function categoryColor(cat: string): string {
  const colors: Record<string, string> = {
    Workspace:   "#4ecdc4",
    Cases:       "#45b7d1",
    Development: "#2ecc71",
    DevOps:      "#e67e22",
    Integration: "#3498db",
    Security:    "#e74c3c",
    Admin:       "#9b59b6",
  };
  return colors[cat] ?? "#888";
}
