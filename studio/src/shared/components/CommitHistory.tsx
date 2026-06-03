/**
 * CommitHistory — shows the audit trail of commits for any component.
 *
 * Usage:
 *   <CommitHistory componentType="case_type" componentId={ct.id} />
 *
 * Features:
 *  - Human-readable diff (stage/step/SLA changes)
 *  - Restore to a previous version (only for case_type commits with an "after" snapshot)
 *  - Git-log-style timeline
 */
import React, { useEffect, useState, useCallback } from "react";
import { computeCaseTypeDiff } from "@shared/hooks/useCommit";
import type { ChangeEntry } from "@shared/hooks/useCommit";

export interface CommitRecord {
  id: string;
  component_type: string;
  component_id: string;
  component_name: string;
  commit_message: string;
  committed_by: string;
  diff_snapshot: { before?: unknown; after?: unknown } | null;
  story_matches: Array<{ story_id: string; title: string; from_status: string; to_status: string }> | null;
  committed_at: string;
}

interface Props {
  componentType: string;
  componentId: string;
  limit?: number;
  compact?: boolean;
  /** Called when user wants to restore to a commit's state. The caller owns the save+commit flow. */
  onRestoreRequest?: (commit: CommitRecord) => void;
}

const STATUS_COLOR: Record<string, string> = {
  in_design: "#a78bfa", in_development: "#3b82f6",
  in_review: "#f59e0b", done: "#22c55e",
};

const SYMBOL_COLOR: Record<string, string> = {
  "+": "var(--status-completed)",
  "-": "var(--status-failed)",
  "~": "#f59e0b",
};

function timeAgo(iso: string): string {
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60)    return `${secs}s ago`;
  if (secs < 3600)  return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function initials(name: string): string { return name.slice(0, 2).toUpperCase(); }

function getDiff(commit: CommitRecord): ChangeEntry[] {
  if (!commit.diff_snapshot) return [];
  const snap = commit.diff_snapshot;
  // New format: { before, after }
  if (snap.before !== undefined || snap.after !== undefined) {
    return computeCaseTypeDiff(
      (snap.before as any) ?? null,
      (snap.after  as any) ?? null,
    );
  }
  return [];
}

function hasRestorableState(commit: CommitRecord): boolean {
  return !!(commit.diff_snapshot as any)?.after;
}

function getSnapshotVersion(commit: CommitRecord): string | null {
  return (commit.diff_snapshot as any)?.after?._version ?? null;
}

export function CommitHistory({ componentType, componentId, limit = 50, compact = false, onRestoreRequest }: Props) {
  const [commits, setCommits]   = useState<CommitRecord[]>([]);
  const [loading, setLoading]   = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const qs = new URLSearchParams({
        component_type: componentType,
        component_id: componentId,
        limit: String(limit),
      });
      const r = await fetch(`/api/v1/commits?${qs}`, {
        headers: { Authorization: `Bearer ${localStorage.getItem("helix_token") ?? ""}` },
      });
      if (r.ok) {
        const d = await r.json();
        setCommits(d.commits ?? []);
      }
    } finally {
      setLoading(false);
    }
  }, [componentType, componentId, limit]);

  useEffect(() => { load(); }, [load]);

  if (loading) {
    return (
      <div style={{ padding: compact ? "12px 0" : 24, color: "var(--text-muted)", fontSize: 12 }}>
        Loading history…
      </div>
    );
  }

  if (commits.length === 0) {
    return (
      <div style={{ padding: compact ? "12px 0" : 24, textAlign: "center" }}>
        <div style={{ fontSize: 24, marginBottom: 8 }}>📝</div>
        <div style={{ fontSize: 13, color: "var(--text-secondary)", fontWeight: 600 }}>No commits yet</div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>
          Every time you save this component, a commit is recorded here.
        </div>
      </div>
    );
  }

  return (
    <div style={{ padding: compact ? 0 : "16px 0" }}>
      {/* Toolbar */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        <span style={{ fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
          {commits.length} commit{commits.length !== 1 ? "s" : ""}
        </span>
        <button
          onClick={load}
          style={{ background: "none", border: "none", cursor: "pointer", fontSize: 11, color: "var(--accent)", padding: 0 }}
        >
          ↻ Refresh
        </button>
      </div>

      {/* Commit list */}
      <div style={{ position: "relative" }}>
        {/* Vertical rail */}
        <div style={{
          position: "absolute", left: 14, top: 14, bottom: 14,
          width: 2, background: "var(--border-subtle)", borderRadius: 1,
        }} />

        {commits.map((commit, idx) => {
          const isExpanded = expanded === commit.id;
          const diff = getDiff(commit);
          const hasDiff = diff.length > 0;
          const hasStories = !!(commit.story_matches?.length);
          const isForm = commit.component_type === "form";
          // case_type: never restore to latest (it's already current). forms: always allow loading.
          const canRestore = hasRestorableState(commit) && (isForm || idx > 0);
          const snapshotVersion = getSnapshotVersion(commit);

          return (
            <div key={commit.id} style={{ display: "flex", gap: 12, marginBottom: idx < commits.length - 1 ? 20 : 0 }}>
              {/* Avatar dot */}
              <div style={{
                width: 30, height: 30, borderRadius: "50%", flexShrink: 0,
                background: idx === 0 ? "var(--accent)" : "var(--accent-dim)",
                color: idx === 0 ? "#fff" : "var(--accent)",
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 10, fontWeight: 700, fontFamily: "var(--font-mono)",
                border: `2px solid var(--bg-card)`, zIndex: 1,
              }}>
                {initials(commit.committed_by)}
              </div>

              {/* Content */}
              <div style={{ flex: 1, minWidth: 0, paddingBottom: 4 }}>
                {/* Meta row */}
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 12, fontWeight: 700, color: "var(--text-primary)" }}>
                    {commit.committed_by}
                  </span>
                  <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                    {timeAgo(commit.committed_at)}
                  </span>
                  {idx === 0 && (
                    <span style={{
                      fontSize: 9, padding: "1px 6px", borderRadius: 4,
                      background: "var(--accent-dim)", color: "var(--accent)",
                      fontFamily: "var(--font-mono)", fontWeight: 700,
                    }}>
                      HEAD
                    </span>
                  )}
                  <span style={{
                    marginLeft: "auto", fontSize: 10, color: "var(--text-muted)",
                    fontFamily: "var(--font-mono)", opacity: 0.6,
                  }}>
                    {commit.id.slice(0, 8)}
                  </span>
                </div>

                {/* Commit message card */}
                <div style={{
                  background: idx === 0 ? "color-mix(in srgb, var(--accent) 5%, var(--bg-card))" : "var(--bg-card)",
                  border: `1px solid ${idx === 0 ? "color-mix(in srgb, var(--accent) 20%, var(--border-subtle))" : "var(--border-subtle)"}`,
                  borderRadius: "var(--radius-sm)", padding: "8px 12px",
                  fontSize: 13, color: "var(--text-primary)", lineHeight: 1.5,
                }}>
                  {commit.commit_message}
                </div>

                {/* Diff summary chips */}
                {hasDiff && (
                  <div style={{ marginTop: 6, display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                    {["+" as const, "-" as const, "~" as const].map(sym => {
                      const count = diff.filter(d => d.symbol === sym).length;
                      if (!count) return null;
                      const labels = { "+": "added", "-": "removed", "~": "changed" };
                      return (
                        <span key={sym} style={{
                          fontSize: 10, padding: "1px 7px", borderRadius: 10,
                          fontFamily: "var(--font-mono)", fontWeight: 600,
                          background: `color-mix(in srgb, ${SYMBOL_COLOR[sym]} 12%, transparent)`,
                          color: SYMBOL_COLOR[sym],
                          border: `1px solid color-mix(in srgb, ${SYMBOL_COLOR[sym]} 25%, transparent)`,
                        }}>
                          {sym}{count} {labels[sym]}
                        </span>
                      );
                    })}
                    <button
                      onClick={() => setExpanded(isExpanded ? null : commit.id)}
                      style={{
                        background: "none", border: "none", cursor: "pointer",
                        fontSize: 11, color: "var(--accent)", padding: 0,
                        fontFamily: "var(--font-mono)",
                      }}
                    >
                      {isExpanded ? "▲ hide" : "▼ details"}
                    </button>
                  </div>
                )}

                {/* Expanded diff */}
                {isExpanded && hasDiff && (
                  <div style={{
                    marginTop: 8, padding: "8px 12px",
                    background: "var(--bg-elevated)", border: "1px solid var(--border-subtle)",
                    borderRadius: "var(--radius-sm)",
                  }}>
                    {diff.map((c, i) => (
                      <div key={i} style={{ display: "flex", gap: 8, fontSize: 12, lineHeight: 1.6 }}>
                        <span style={{ color: SYMBOL_COLOR[c.symbol], fontFamily: "var(--font-mono)", fontWeight: 700, flexShrink: 0 }}>
                          {c.symbol}
                        </span>
                        <span style={{ color: "var(--text-secondary)" }}>{c.text}</span>
                      </div>
                    ))}
                  </div>
                )}

                {/* Story advancement */}
                {hasStories && (
                  <div style={{ marginTop: 6, display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {commit.story_matches!.map((m, i) => (
                      <div key={i} style={{
                        fontSize: 10, padding: "2px 8px", borderRadius: 10,
                        background: (STATUS_COLOR[m.to_status] ?? "#888") + "22",
                        color: STATUS_COLOR[m.to_status] ?? "#888",
                        border: `1px solid ${(STATUS_COLOR[m.to_status] ?? "#888")}44`,
                        display: "flex", alignItems: "center", gap: 4,
                      }}>
                        <span style={{ fontWeight: 600 }}>↑</span>
                        <span style={{ maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {m.title}
                        </span>
                        <span style={{ opacity: 0.7 }}>→ {m.to_status.replace("_", " ")}</span>
                      </div>
                    ))}
                  </div>
                )}

                {/* Version badge + load button row */}
                {(snapshotVersion || canRestore || (!hasRestorableState(commit) && onRestoreRequest && isForm)) && (
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
                    {snapshotVersion && (
                      <span style={{
                        fontSize: 11, padding: "2px 10px", borderRadius: 10,
                        background: "var(--accent-dim)", color: "var(--accent)",
                        fontFamily: "var(--font-mono)", fontWeight: 700, border: "1px solid color-mix(in srgb, var(--accent) 30%, transparent)",
                      }}>
                        v{snapshotVersion}
                      </span>
                    )}

                    {canRestore && onRestoreRequest && (
                      <button
                        onClick={() => onRestoreRequest(commit)}
                        style={{
                          background: "var(--accent)", color: "#fff",
                          border: "none", borderRadius: "var(--radius-sm)",
                          cursor: "pointer", fontSize: 12, fontWeight: 600,
                          padding: "4px 12px", display: "inline-flex", alignItems: "center", gap: 5,
                        }}
                      >
                        {isForm
                          ? `↩ Load${snapshotVersion ? ` v${snapshotVersion}` : ""} into editor`
                          : "↩ Restore to this version"}
                      </button>
                    )}

                    {!hasRestorableState(commit) && onRestoreRequest && isForm && (
                      <span style={{ fontSize: 11, color: "var(--text-muted)", fontStyle: "italic" }}>
                        No snapshot (committed before version tracking)
                      </span>
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
