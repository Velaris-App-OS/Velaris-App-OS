import React, { useState, useEffect, useRef, useCallback } from "react";
import ReactDOM from "react-dom";
import { searchUsers, UserDirectoryEntry } from "@shared/api/client";

interface ReviewerPickerProps {
  value: string;
  onChange: (value: string) => void;
  accessGroupId?: string;
  placeholder?: string;
  disabled?: boolean;
  style?: React.CSSProperties;
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement>) => void;
  autoFocus?: boolean;
}

export function ReviewerPicker({
  value,
  onChange,
  accessGroupId,
  placeholder = "Search by name, email, or ID…",
  disabled,
  style,
  onKeyDown,
  autoFocus,
}: ReviewerPickerProps) {
  const [query, setQuery]             = useState(value);
  const [results, setResults]         = useState<UserDirectoryEntry[]>([]);
  const [open, setOpen]               = useState(false);
  const [loading, setLoading]         = useState(false);
  const [activeIdx, setActiveIdx]     = useState(-1);
  const [groupMismatch, setGroupMismatch] = useState(false);
  const [dropPos, setDropPos]     = useState<{ top: number; left: number; width: number } | null>(null);

  const inputRef    = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!value) setQuery("");
  }, [value]);

  const runSearch = useCallback((q: string, force = false) => {
    if (!q.trim() && !force) { setResults([]); setOpen(false); return; }
    setLoading(true);
    searchUsers(q, undefined, 20)
      .then((r) => { setResults(r); setOpen(r.length > 0); setActiveIdx(-1); updateDropPos(); })
      .catch(() => setResults([]))
      .finally(() => setLoading(false));
  }, []);

  const updateDropPos = () => {
    if (!inputRef.current) return;
    const rect = inputRef.current.getBoundingClientRect();
    setDropPos({
      top:   rect.bottom + window.scrollY + 2,
      left:  rect.left  + window.scrollX,
      width: rect.width,
    });
  };

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const q = e.target.value;
    setQuery(q);
    onChange(q);
    setGroupMismatch(false);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => runSearch(q), 250);
  };

  const handleDropdownClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (open) { setOpen(false); return; }
    runSearch(query, true);
  };

  const select = (u: UserDirectoryEntry) => {
    setQuery(u.user_id);
    onChange(u.user_id);
    setOpen(false);
    setResults([]);
    // Warn if user is from a different group than expected
    if (accessGroupId) {
      const groups: string[] = u.access_group_ids ?? [];
      setGroupMismatch(!groups.includes(accessGroupId));
    } else {
      setGroupMismatch(false);
    }
    inputRef.current?.focus();
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open || results.length === 0) { runSearch(query, true); return; }
      setActiveIdx(i => Math.min(i + 1, results.length - 1));
      return;
    }
    if (open && results.length > 0) {
      if (e.key === "ArrowUp")   { e.preventDefault(); setActiveIdx(i => Math.max(i - 1, 0)); return; }
      if (e.key === "Enter" && activeIdx >= 0) { e.preventDefault(); select(results[activeIdx]); return; }
      if (e.key === "Escape") { setOpen(false); return; }
    }
    onKeyDown?.(e);
  };

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        containerRef.current && !containerRef.current.contains(target) &&
        !(document.getElementById("reviewer-picker-portal")?.contains(target))
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  // Update drop position on scroll/resize
  useEffect(() => {
    if (!open) return;
    const update = () => updateDropPos();
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => { window.removeEventListener("scroll", update, true); window.removeEventListener("resize", update); };
  }, [open]);

  const dropdown = open && results.length > 0 && dropPos ? ReactDOM.createPortal(
    <div
      id="reviewer-picker-portal"
      style={{
        position: "absolute",
        top: dropPos.top,
        left: dropPos.left,
        width: dropPos.width,
        zIndex: 99999,
        background: "var(--bg-panel)",
        border: "1px solid var(--border-default)",
        borderRadius: 6,
        boxShadow: "0 8px 24px rgba(0,0,0,0.2)",
        maxHeight: 220,
        overflowY: "auto",
      }}
    >
      {results.map((u, i) => (
        <div
          key={u.user_id}
          onMouseDown={(e) => { e.preventDefault(); select(u); }}
          style={{
            padding: "7px 12px",
            cursor: "pointer",
            fontSize: 12,
            background: i === activeIdx
              ? "color-mix(in srgb, var(--accent) 15%, var(--bg-panel))"
              : "transparent",
            borderBottom: i < results.length - 1
              ? "1px solid color-mix(in srgb, var(--border-default) 40%, transparent)"
              : undefined,
          }}
          onMouseEnter={() => setActiveIdx(i)}
        >
          <div style={{ fontWeight: 600, color: "var(--text-primary)", fontFamily: "var(--font-mono)" }}>
            {u.user_id}
          </div>
          {(u.display_name || u.email) && (
            <div style={{ color: "var(--text-muted)", fontSize: 11, marginTop: 1 }}>
              {[u.display_name, u.email].filter(Boolean).join(" · ")}
            </div>
          )}
        </div>
      ))}
    </div>,
    document.body
  ) : null;

  return (
    <div ref={containerRef} style={{ position: "relative", flex: 1, ...style }}>
      <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
      <input
        ref={inputRef}
        autoFocus={autoFocus}
        value={query}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onFocus={() => { updateDropPos(); query.trim() && results.length > 0 && setOpen(true); }}
        placeholder={placeholder}
        disabled={disabled}
        style={{
          flex: 1,
          padding: "4px 28px 4px 8px",
          border: "1px solid var(--border-default)",
          borderRadius: 4,
          fontSize: 12,
          fontFamily: "var(--font-mono)",
          background: "var(--bg-input)",
          color: "var(--text-primary)",
          boxSizing: "border-box",
          width: "100%",
        }}
      />
      {/* Dropdown chevron button */}
      <button
        type="button"
        onMouseDown={handleDropdownClick}
        disabled={disabled}
        style={{
          position: "absolute",
          right: 4,
          top: "50%",
          transform: "translateY(-50%)",
          background: "none",
          border: "none",
          cursor: disabled ? "not-allowed" : "pointer",
          padding: "2px 4px",
          color: loading ? "var(--accent)" : "var(--text-muted)",
          fontSize: 10,
          lineHeight: 1,
          display: "flex",
          alignItems: "center",
        }}
        tabIndex={-1}
      >
        {loading ? "…" : open ? "▲" : "▼"}
      </button>
      </div>
      {dropdown}
      {groupMismatch && accessGroupId && (
        <div style={{
          fontSize: 11, color: "#92400e", marginTop: 4,
          display: "flex", alignItems: "center", gap: 6,
          background: "#fef3c7", border: "1px solid #fde68a",
          borderRadius: 4, padding: "4px 8px",
        }}>
          <span style={{ fontSize: 13 }}>⚠</span>
          <span>This user is not in your access group — review is allowed but unusual</span>
        </div>
      )}
    </div>
  );
}
