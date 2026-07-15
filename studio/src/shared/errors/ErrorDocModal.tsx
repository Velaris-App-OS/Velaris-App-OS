/**
 * ErrorDocModal — the Error Documentation modal, opened by the red octagon in PageHeader.
 *
 * Default view: the current component's errors.
 * Typing in the search: global — searches every component's errors and shows a
 * Component column so each result's origin is visible.
 *
 * One modal, reused on every page; the page just passes its componentKey.
 */
import React, { useState, useEffect, useMemo, useRef } from "react";
import { ERROR_CATALOG, searchErrors, type FlatErrorEntry } from "./catalog";
import ErrorTable from "./ErrorTable";

interface Props {
  open:           boolean;
  onClose:        () => void;
  /** Nav-derived key, e.g. "documents". May be undefined for unknown pages. */
  componentKey?:  string;
  /** Fallback label when the key isn't in the catalog (e.g. the page's nav label). */
  componentLabel?: string;
}

export default function ErrorDocModal({ open, onClose, componentKey, componentLabel }: Props) {
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setTimeout(() => inputRef.current?.focus(), 60);
    }
  }, [open]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    if (open) window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);

  const comp = componentKey ? ERROR_CATALOG[componentKey] : undefined;
  const title = comp?.label ?? componentLabel ?? "Errors";

  const searching = query.trim().length > 0;

  // Default: this component's own entries (flattened to the table's shape).
  const ownEntries: FlatErrorEntry[] = useMemo(() => {
    if (!comp) return [];
    return comp.entries.map(e => ({ ...e, componentKey: componentKey!, componentLabel: comp.label }));
  }, [comp, componentKey]);

  const results = useMemo(() => (searching ? searchErrors(query) : ownEntries), [searching, query, ownEntries]);

  if (!open) return null;

  return (
    <>
      {/* Backdrop */}
      <div
        onClick={onClose}
        style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)", zIndex: 1200, backdropFilter: "blur(2px)" }}
      />
      {/* Modal */}
      <div
        role="dialog"
        aria-label="Error Documentation"
        style={{
          position: "fixed", top: "50%", left: "50%", transform: "translate(-50%,-50%)",
          width: "min(1040px, 94vw)", maxHeight: "86vh",
          background: "var(--bg-card)", border: "1px solid var(--border-default)",
          borderRadius: "var(--radius-md)", boxShadow: "0 20px 60px rgba(0,0,0,0.35)",
          zIndex: 1201, fontFamily: "var(--font-body)",
          display: "flex", flexDirection: "column", overflow: "hidden",
        }}
      >
        {/* Header */}
        <div style={{ padding: "16px 20px", borderBottom: "1px solid var(--border-subtle)", display: "flex", alignItems: "center", gap: 12, flexShrink: 0 }}>
          <Octagon size={20} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)" }}>
              Error Documentation
            </div>
            <div style={{ fontSize: 11.5, color: "var(--text-muted)", marginTop: 1 }}>
              {searching ? "Searching all components" : `${title} — errors, root cause & resolution`}
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            style={{
              width: 28, height: 28, borderRadius: "var(--radius-sm)",
              border: "1px solid var(--border-subtle)", background: "var(--bg-elevated)",
              color: "var(--text-muted)", cursor: "pointer", fontSize: 15, lineHeight: 1, flexShrink: 0,
            }}
          >
            ×
          </button>
        </div>

        {/* Search */}
        <div style={{ padding: "12px 20px", borderBottom: "1px solid var(--border-subtle)", background: "var(--bg-elevated)", flexShrink: 0 }}>
          <input
            ref={inputRef}
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search all errors — code, symptom, cause… (e.g. 503, upload, permission)"
            style={{
              width: "100%", boxSizing: "border-box", padding: "9px 12px", fontSize: 13,
              fontFamily: "var(--font-body)", background: "var(--bg-input)",
              border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
              color: "var(--text-primary)", outline: "none",
            }}
          />
          <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-muted)", display: "flex", justifyContent: "space-between" }}>
            <span>{searching ? `${results.length} match${results.length === 1 ? "" : "es"} across all components` : `${results.length} error${results.length === 1 ? "" : "s"} on this page`}</span>
            <span style={{ fontFamily: "var(--font-mono)" }}>Esc to close</span>
          </div>
        </div>

        {/* Table */}
        <div style={{ overflow: "auto", flex: 1 }}>
          {!searching && !comp ? (
            <div style={{ padding: "28px 16px", textAlign: "center", color: "var(--text-muted)", fontSize: 13 }}>
              No errors documented for this page yet. Use the search above to look across all components.
            </div>
          ) : (
            <ErrorTable entries={results} showComponent={searching} highlight={searching ? query : undefined} />
          )}
        </div>
      </div>
    </>
  );
}

/** Red octagonal icon — the shared Error Documentation glyph. */
export function Octagon({ size = 18, title }: { size?: number; title?: string }) {
  // Regular octagon path on a 24×24 viewbox.
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden={!title} role={title ? "img" : undefined}>
      {title && <title>{title}</title>}
      <polygon
        points="8,2 16,2 22,8 22,16 16,22 8,22 2,16 2,8"
        style={{
          fill: "var(--status-failed, #dc2626)",
          stroke: "color-mix(in srgb, var(--status-failed, #dc2626) 70%, #000)",
          strokeWidth: 1,
        }}
      />
      <text x="12" y="16.5" textAnchor="middle" fontSize="12" fontWeight="700" fill="#fff" fontFamily="var(--font-body)">!</text>
    </svg>
  );
}
