/**
 * CommitModal — shown whenever any component is saved across the platform.
 * Requires a commit message before the save proceeds (git-style).
 * Shows a pending-changes preview when diff data is available.
 */
import React, { useState, useEffect, useRef } from "react";
import type { ChangeEntry } from "@shared/hooks/useCommit";

interface Props {
  componentName: string;
  componentType: string;
  open: boolean;
  onCommit: (message: string) => void;
  onCancel: () => void;
  saving?: boolean;
  pendingChanges?: ChangeEntry[];
  /** Pre-fill the message field (e.g. for restore operations). User can still edit it. */
  prefilledMessage?: string;
}

const SYMBOL_STYLE: Record<string, React.CSSProperties> = {
  "+": { color: "var(--status-completed)", fontWeight: 700 },
  "-": { color: "var(--status-failed)",    fontWeight: 700 },
  "~": { color: "#f59e0b",                  fontWeight: 700 },
};

export function CommitModal({
  componentName, componentType, open, onCommit, onCancel, saving,
  pendingChanges = [], prefilledMessage,
}: Props) {
  const [message, setMessage] = useState("");
  const [touched, setTouched] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (open) {
      setMessage(prefilledMessage ?? "");
      setTouched(!!prefilledMessage);
      setTimeout(() => textareaRef.current?.focus(), 60);
    }
  }, [open, prefilledMessage]);

  if (!open) return null;

  const valid   = message.trim().length >= 10;
  const tooLong = message.length > 500;

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && valid && !saving) {
      onCommit(message.trim());
    }
    if (e.key === "Escape") onCancel();
  };

  const hasChanges = pendingChanges.length > 0;
  const added   = pendingChanges.filter(c => c.symbol === "+").length;
  const removed = pendingChanges.filter(c => c.symbol === "-").length;
  const modified = pendingChanges.filter(c => c.symbol === "~").length;

  return (
    <>
      {/* Backdrop */}
      <div
        onClick={onCancel}
        style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)",
          zIndex: 999, backdropFilter: "blur(2px)",
        }}
      />
      {/* Modal */}
      <div style={{
        position: "fixed", top: "50%", left: "50%",
        transform: "translate(-50%,-50%)",
        width: 520, background: "var(--bg-card)",
        border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-md)",
        boxShadow: "0 20px 60px rgba(0,0,0,0.3)",
        zIndex: 1000,
        fontFamily: "var(--font-body)",
        overflow: "hidden",
      }}>
        {/* Header */}
        <div style={{ padding: "var(--space-lg) var(--space-xl)", borderBottom: "1px solid var(--border-subtle)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
            <span style={{
              fontSize: 13,
              background: prefilledMessage ? "color-mix(in srgb, #f59e0b 15%, var(--bg-elevated))" : "var(--accent-dim)",
              color: prefilledMessage ? "#d97706" : "var(--accent)",
              padding: "3px 9px",
              borderRadius: "var(--radius-sm)", fontFamily: "var(--font-mono)",
              fontWeight: 700, letterSpacing: "0.03em",
            }}>
              {prefilledMessage ? "↩ restore" : "commit"}
            </span>
            <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
              {componentType}
            </span>
            {prefilledMessage && (
              <span style={{ fontSize: 11, color: "#d97706", fontFamily: "var(--font-mono)" }}>
                — version restore (audited)
              </span>
            )}
          </div>
          <div style={{ fontSize: 16, fontWeight: 700, color: "var(--text-primary)" }}>
            {componentName}
          </div>
        </div>

        {/* Pending changes preview */}
        {hasChanges && (
          <div style={{ borderBottom: "1px solid var(--border-subtle)", background: "var(--bg-elevated)" }}>
            {/* Summary chips */}
            <div style={{ padding: "10px var(--space-xl)", display: "flex", gap: 10, alignItems: "center" }}>
              <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                Changes:
              </span>
              {added   > 0 && <Chip color="var(--status-completed)" label={`+${added} added`}   />}
              {removed > 0 && <Chip color="var(--status-failed)"    label={`−${removed} removed`} />}
              {modified > 0 && <Chip color="#f59e0b"                 label={`~${modified} modified`} />}
            </div>
            {/* Change list */}
            <div style={{
              maxHeight: 160, overflowY: "auto",
              padding: "0 var(--space-xl) 10px",
              display: "flex", flexDirection: "column", gap: 3,
            }}>
              {pendingChanges.map((c, i) => (
                <div key={i} style={{
                  display: "flex", gap: 8, alignItems: "flex-start",
                  fontSize: 12, lineHeight: 1.4,
                }}>
                  <span style={{ ...SYMBOL_STYLE[c.symbol], flexShrink: 0, width: 12, fontFamily: "var(--font-mono)" }}>
                    {c.symbol}
                  </span>
                  <span style={{ color: "var(--text-secondary)" }}>{c.text}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {!hasChanges && (
          <div style={{
            padding: "10px var(--space-xl)",
            borderBottom: "1px solid var(--border-subtle)",
            background: "var(--bg-elevated)",
            fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)",
          }}>
            No structural changes detected (metadata or property edit)
          </div>
        )}

        {/* Message input */}
        <div style={{ padding: "var(--space-lg) var(--space-xl)" }}>
          <label style={{
            display: "block", fontSize: 12, fontWeight: 600,
            color: "var(--text-secondary)", marginBottom: 6,
          }}>
            What did you change? <span style={{ color: "var(--status-failed)" }}>*</span>
          </label>
          <textarea
            ref={textareaRef}
            value={message}
            onChange={e => { setMessage(e.target.value); setTouched(true); }}
            onKeyDown={handleKeyDown}
            placeholder="e.g. Added payment step to Stage 3 and updated SLA from 48h to 24h for high-priority claims"
            rows={3}
            style={{
              width: "100%", boxSizing: "border-box",
              padding: "10px 12px", fontSize: 13,
              fontFamily: "var(--font-body)", lineHeight: 1.5,
              background: "var(--bg-input)",
              border: `1px solid ${touched && !valid ? "var(--status-failed)" : "var(--border-default)"}`,
              borderRadius: "var(--radius-sm)",
              color: "var(--text-primary)", outline: "none",
              resize: "none",
            }}
          />
          <div style={{
            display: "flex", justifyContent: "space-between",
            marginTop: 4, fontSize: 11,
          }}>
            <span style={{ color: tooLong ? "var(--status-failed)" : "var(--text-muted)" }}>
              {touched && !valid && !tooLong ? "Minimum 10 characters" : tooLong ? "Maximum 500 characters" : "⌘+Enter to commit"}
            </span>
            <span style={{ color: tooLong ? "var(--status-failed)" : "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
              {message.length} / 500
            </span>
          </div>
        </div>

        {/* Actions */}
        <div style={{
          padding: "var(--space-md) var(--space-xl)",
          borderTop: "1px solid var(--border-subtle)",
          display: "flex", justifyContent: "flex-end", gap: 10,
          background: "var(--bg-elevated)",
        }}>
          <button
            onClick={onCancel}
            disabled={saving}
            style={{
              padding: "8px 20px", border: "1px solid var(--border-default)",
              borderRadius: "var(--radius-sm)", background: "transparent",
              color: "var(--text-secondary)", cursor: "pointer", fontSize: 13,
            }}
          >
            Cancel
          </button>
          <button
            onClick={() => onCommit(message.trim())}
            disabled={!valid || tooLong || saving}
            style={{
              padding: "8px 24px", border: "none",
              borderRadius: "var(--radius-sm)",
              background: valid && !tooLong ? "var(--accent)" : "var(--bg-elevated)",
              color: valid && !tooLong ? "#fff" : "var(--text-muted)",
              cursor: valid && !tooLong && !saving ? "pointer" : "not-allowed",
              fontSize: 13, fontWeight: 700,
              display: "flex", alignItems: "center", gap: 7,
              transition: "background 0.15s",
            }}
          >
            {saving ? "Committing…" : "Commit →"}
          </button>
        </div>
      </div>
    </>
  );
}

function Chip({ color, label }: { color: string; label: string }) {
  return (
    <span style={{
      fontSize: 11, padding: "2px 8px", borderRadius: 10, fontFamily: "var(--font-mono)", fontWeight: 600,
      background: `color-mix(in srgb, ${color} 12%, transparent)`,
      color,
      border: `1px solid color-mix(in srgb, ${color} 30%, transparent)`,
    }}>
      {label}
    </span>
  );
}
