import React, { useEffect, useRef, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/auth";
import { usePermissions } from "@/auth/PermissionsContext";
import { NAV_DATA, type NavEntry } from "@/app/nav-data";

/* ═══════════════════════════════════════════════════════════════════
   GlobalSearch — Cmd/Ctrl+K spotlight search across all permitted pages.

   Access control: a page appears in results only if isRouteAllowed()
   returns true for the current user — the same gate used by the sidebar
   and PermRoute. Results are therefore always a subset of what the user
   can actually navigate to.
   ═══════════════════════════════════════════════════════════════════ */

interface Props {
  open: boolean;
  onClose: () => void;
}

const SECTION_ORDER = [
  "Workspace", "Cases", "Development", "DevOps", "Integration", "Security", "Admin",
];

function highlight(text: string, query: string): React.ReactNode {
  if (!query) return text;
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return text;
  return (
    <>
      {text.slice(0, idx)}
      <mark style={{ background: "var(--accent)", color: "#fff", borderRadius: 2, padding: "0 1px" }}>
        {text.slice(idx, idx + query.length)}
      </mark>
      {text.slice(idx + query.length)}
    </>
  );
}

export default function GlobalSearch({ open, onClose }: Props) {
  const navigate = useNavigate();
  const { user } = useAuth();
  const { isRouteAllowed } = usePermissions();
  const inputRef = useRef<HTMLInputElement>(null);

  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);

  // ── Filter by text AND permission ────────────────────────────────
  const permitted: NavEntry[] = NAV_DATA.filter(entry =>
    isRouteAllowed(entry.path, user?.roles ?? [], user?.is_admin ?? false)
  );

  const results: NavEntry[] = query.trim()
    ? permitted.filter(e => {
        const q = query.toLowerCase();
        return (
          e.label.toLowerCase().includes(q) ||
          e.description.toLowerCase().includes(q) ||
          e.path.toLowerCase().includes(q) ||
          e.section.toLowerCase().includes(q)
        );
      })
    : permitted;

  // Group results by section in canonical order
  const grouped: { section: string; items: NavEntry[] }[] = SECTION_ORDER
    .map(sec => ({ section: sec, items: results.filter(r => r.section === sec) }))
    .filter(g => g.items.length > 0);

  // Flat list for cursor tracking
  const flat: NavEntry[] = grouped.flatMap(g => g.items);

  // Reset cursor when results change
  useEffect(() => { setCursor(0); }, [query]);

  // Focus input when opened
  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
      setTimeout(() => inputRef.current?.focus(), 30);
    }
  }, [open]);

  const go = useCallback((entry: NavEntry) => {
    navigate(entry.path);
    onClose();
  }, [navigate, onClose]);

  // ── Keyboard navigation ──────────────────────────────────────────
  const handleKey = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor(c => Math.min(c + 1, flat.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor(c => Math.max(c - 1, 0));
    } else if (e.key === "Enter") {
      if (flat[cursor]) go(flat[cursor]);
    } else if (e.key === "Escape") {
      onClose();
    }
  }, [flat, cursor, go, onClose]);

  if (!open) return null;

  return (
    // Backdrop
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 1000,
        background: "rgba(0,0,0,0.55)", backdropFilter: "blur(3px)",
        display: "flex", alignItems: "flex-start", justifyContent: "center",
        paddingTop: "12vh",
      }}
    >
      {/* Panel — stop backdrop click propagating through */}
      <div
        onClick={e => e.stopPropagation()}
        style={{
          width: "min(640px, 92vw)",
          background: "var(--bg-card)",
          border: "1px solid var(--border)",
          borderRadius: 14,
          boxShadow: "0 24px 60px rgba(0,0,0,0.45)",
          display: "flex", flexDirection: "column",
          maxHeight: "70vh", overflow: "hidden",
        }}
      >
        {/* Search input row */}
        <div style={{
          display: "flex", alignItems: "center", gap: 10,
          padding: "14px 18px",
          borderBottom: "1px solid var(--border)",
          flexShrink: 0,
        }}>
          <span style={{ fontSize: 16, color: "var(--text-muted)" }}>🔍</span>
          <input
            ref={inputRef}
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={handleKey}
            placeholder="Search pages…"
            style={{
              flex: 1, border: "none", outline: "none",
              fontSize: 16, background: "transparent",
              color: "var(--text-primary)", fontFamily: "var(--font-body)",
            }}
          />
          {query && (
            <button
              onClick={() => setQuery("")}
              style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 13 }}
            >
              ✕
            </button>
          )}
          <kbd style={{
            fontSize: 10, padding: "2px 6px", borderRadius: 4,
            border: "1px solid var(--border)", color: "var(--text-muted)",
            fontFamily: "var(--font-mono)", background: "var(--bg-elevated)",
          }}>Esc</kbd>
        </div>

        {/* Results */}
        <div style={{ flex: 1, overflowY: "auto" }}>
          {flat.length === 0 ? (
            <div style={{
              padding: "32px 20px", textAlign: "center",
              color: "var(--text-muted)", fontSize: 13,
            }}>
              {query ? `No pages matching "${query}" in your access scope.` : "No pages available."}
            </div>
          ) : (
            grouped.map(group => {
              // track the absolute cursor index for this group
              const groupStart = flat.indexOf(group.items[0]);
              return (
                <div key={group.section}>
                  <div style={{
                    padding: "10px 18px 4px",
                    fontSize: 10, fontWeight: 700,
                    color: "var(--text-muted)",
                    textTransform: "uppercase", letterSpacing: "0.07em",
                    fontFamily: "var(--font-mono)",
                  }}>
                    {group.section}
                  </div>
                  {group.items.map((entry, i) => {
                    const idx = groupStart + i;
                    const isActive = idx === cursor;
                    return (
                      <div
                        key={entry.path}
                        onClick={() => go(entry)}
                        onMouseEnter={() => setCursor(idx)}
                        style={{
                          display: "flex", alignItems: "center", gap: 12,
                          padding: "9px 18px",
                          cursor: "pointer",
                          background: isActive ? "var(--accent-dim, rgba(78,205,196,0.12))" : "transparent",
                          borderLeft: isActive ? "3px solid var(--accent)" : "3px solid transparent",
                          transition: "background 0.08s",
                        }}
                      >
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{
                            fontSize: 13, fontWeight: 600,
                            color: isActive ? "var(--accent)" : "var(--text-primary)",
                          }}>
                            {highlight(entry.label, query)}
                          </div>
                          <div style={{
                            fontSize: 11, color: "var(--text-muted)",
                            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                          }}>
                            {highlight(entry.description, query)}
                          </div>
                        </div>
                        <code style={{
                          fontSize: 10, color: "var(--text-muted)",
                          fontFamily: "var(--font-mono)", flexShrink: 0,
                        }}>
                          {entry.path}
                        </code>
                        {isActive && (
                          <span style={{ fontSize: 11, color: "var(--accent)", flexShrink: 0 }}>↵</span>
                        )}
                      </div>
                    );
                  })}
                </div>
              );
            })
          )}
        </div>

        {/* Footer */}
        <div style={{
          padding: "8px 18px",
          borderTop: "1px solid var(--border)",
          display: "flex", gap: 16, flexShrink: 0,
          fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)",
        }}>
          <span><kbd style={kbdStyle}>↑</kbd><kbd style={kbdStyle}>↓</kbd> navigate</span>
          <span><kbd style={kbdStyle}>↵</kbd> open</span>
          <span><kbd style={kbdStyle}>Esc</kbd> close</span>
          <span style={{ marginLeft: "auto" }}>
            {flat.length} page{flat.length !== 1 ? "s" : ""} in scope
          </span>
        </div>
      </div>
    </div>
  );
}

const kbdStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "1px 5px", borderRadius: 3,
  border: "1px solid var(--border)",
  background: "var(--bg-elevated)",
  marginRight: 3, fontSize: 10,
};
