/**
 * P60 — HxBranch: Artifact Version Control & Live Environment Sync
 *
 * Three tabs:
 *   Branches       — list, review, and merge all branches in dev
 *   Pull from Env  — connect to staging/UAT, browse artifacts, pull as branch
 *   Connections    — manage API tokens for each registered environment
 */
import React, { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { ReviewerPicker } from "@shared/components";
import { useCurrentUserGroups } from "@shared/hooks";

const API  = "/api/v1/branches";
const DAPI = "/api/v1/deploy";

// ── Types ─────────────────────────────────────────────────────────

type Branch = {
  id: string; name: string; description: string | null;
  branch_type: string; artifact_type: string | null; artifact_id: string | null;
  source_env_id: string | null; source_env_name: string;
  status: string; conflict_detected: boolean;
  created_by: string; reviewed_by: string | null; merged_by: string | null;
  assigned_reviewer_id: string | null;
  assigned_reviewer_name?: string | null;
  created_at: string; merged_at: string | null;
  diff_vs_main?: DiffResult; diff_from_base?: DiffResult;
};

type DiffResult = {
  changed_fields: { field: string; base: unknown; branch: unknown }[];
  added_keys: string[]; removed_keys: string[];
  total_changes: number;
};

type Review = {
  id: string; reviewer_id: string; decision: string;
  comments: string | null; created_at: string;
};

type AuditEvent = {
  id: string; branch_id: string; event_type: string;
  actor_id: string | null; actor_name: string | null;
  metadata: Record<string, unknown>; created_at: string;
};

type Env = {
  id: string; name: string; label: string;
  url: string | null; status: string;
  api_token_enc: unknown; connection_verified_at: string | null;
};

type RemoteItem = { id: string; name: string; version?: string; updated_at?: string; [k: string]: unknown };

// ── Status colours ────────────────────────────────────────────────

const STATUS_COLOR: Record<string, string> = {
  open: "#3b82f6", pending_review: "#f59e0b",
  approved: "#22c55e", merged: "#0d9488",
  rejected: "#ef4444", closed: "#94a3b8",
};

const AUDIT_EVENT_LABEL: Record<string, string> = {
  branch_created:       "Branch created",
  submitted_for_review: "Submitted for review",
  recalled:             "Recalled from review",
  reviewed:             "Review decision",
  merged:               "Merged to main",
  content_saved:        "Content saved",
  reverted_to_base:     "Reverted to base",
  branch_deleted:       "Branch deleted",
};

const AUDIT_EVENT_COLOR: Record<string, string> = {
  branch_created:       "#3b82f6",
  submitted_for_review: "#f59e0b",
  recalled:             "#94a3b8",
  reviewed:             "#8b5cf6",
  merged:               "#0d9488",
  content_saved:        "#6b7280",
  reverted_to_base:     "#f97316",
  branch_deleted:       "#ef4444",
};
const DECISION_COLOR: Record<string, string> = {
  approved: "#22c55e", rejected: "#ef4444", changes_requested: "#f59e0b",
};

const S: Record<string, React.CSSProperties> = {
  page:   { display: "flex", flexDirection: "column", height: "100%", background: "var(--bg-main)", color: "var(--text-primary)", fontFamily: "system-ui, sans-serif" },
  header: { padding: "18px 24px 0", flexShrink: 0 },
  tabBar: { display: "flex", gap: 4, padding: "14px 24px 0", borderBottom: "1px solid var(--border)", flexShrink: 0 },
  tab:    { padding: "8px 20px", border: "none", background: "none", fontSize: 13, cursor: "pointer", borderBottom: "2px solid transparent", fontWeight: 400, color: "var(--text-secondary)" },
  tabA:   { borderBottom: "2px solid var(--accent)", color: "var(--accent)", fontWeight: 700 },
  body:   { flex: 1, overflow: "auto", padding: "20px 28px" },
  card:   { background: "var(--bg-surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "14px 18px", marginBottom: 10 },
  btn:    { padding: "7px 16px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: 700 },
  btnP:   { background: "var(--accent)", color: "#fff" },
  btnS:   { background: "var(--bg-surface)", border: "1px solid var(--border)", color: "var(--text-secondary)" },
  btnD:   { background: "#fee2e2", color: "#ef4444", border: "1px solid #fecaca" },
  input:  { width: "100%", padding: "7px 11px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "var(--bg-main)", color: "var(--text-primary)" },
  select: { padding: "7px 11px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, background: "var(--bg-main)", color: "var(--text-primary)", width: "100%" },
  label:  { fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 4, display: "block" },
  badge:  { fontSize: 10, padding: "2px 8px", borderRadius: 10, fontWeight: 700 },
  mono:   { fontFamily: "monospace", fontSize: 11 },
};

function Badge({ label, color }: { label: string; color: string }) {
  return <span style={{ ...S.badge, background: color + "22", color }}>{label}</span>;
}
function fmtDate(s: string | null) { return s ? new Date(s).toLocaleString() : "—"; }
async function api(method: string, path: string, body?: unknown) {
  const r = await fetch(path, {
    method, headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("helix_token") ?? ""}` },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) { const e = await r.json().catch(() => ({ detail: r.statusText })); throw new Error(e.detail ?? "Request failed"); }
  if (r.status === 204) return null;
  return r.json();
}

// ═══════════════════════════════════════════════════════════════════
// Tab 1 — Branches
// ═══════════════════════════════════════════════════════════════════

function BranchesTab() {
  const [branches, setBranches] = useState<Branch[]>([]);
  const [statusF, setStatusF] = useState("");
  const [typeF, setTypeF] = useState("");
  const [selected, setSelected] = useState<Branch | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null); // branch id pending confirm

  const load = useCallback(async () => {
    const qs = new URLSearchParams();
    if (statusF) qs.set("status", statusF);
    if (typeF) qs.set("artifact_type", typeF);
    const d = await api("GET", `${API}?${qs}`).catch(e => { setMsg(e.message); return null; });
    if (d) setBranches(d.branches ?? []);
  }, [statusF, typeF]);

  useEffect(() => { load(); }, [load]);

  const openDetail = async (b: Branch) => {
    const d = await api("GET", `${API}/${b.id}`).catch(e => { setMsg(e.message); return null; });
    if (d) setSelected(d);
  };

  if (selected) {
    return <BranchDetail branch={selected} onBack={() => { setSelected(null); load(); }} />;
  }

  return (
    <div style={S.body}>
      {msg && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 12 }}>{msg}</div>}

      {/* Filters */}
      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <select style={{ ...S.select, width: 180 }} value={statusF} onChange={e => setStatusF(e.target.value)}>
          <option value="">All statuses</option>
          {["open","pending_review","approved","merged","rejected","closed"].map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select style={{ ...S.select, width: 160 }} value={typeF} onChange={e => setTypeF(e.target.value)}>
          <option value="">All types</option>
          {ARTIFACT_TYPES.filter(t => t.value !== "app").map(t => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
        <button style={{ ...S.btn, ...S.btnS }} onClick={load}>↻ Refresh</button>
      </div>

      <CreateLocalBranchForm onCreated={load} />

      {branches.length === 0 && (
        <div style={{ padding: 48, color: "var(--text-secondary)", fontSize: 13 }}>
          No branches yet. Use "Branch from Local Artifact" above or pull from a remote environment.
        </div>
      )}

      {branches.map(b => (
        <div key={b.id} style={{ ...S.card, cursor: "pointer" }} onClick={() => openDetail(b)}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
            <span style={{ fontSize: 14, fontWeight: 700, flex: 1, fontFamily: "monospace" }}>{b.name}</span>
            <Badge label={b.status.replace("_", " ")} color={STATUS_COLOR[b.status] ?? "#888"} />
            {b.conflict_detected && <Badge label="CONFLICT" color="#ef4444" />}
            {b.artifact_type && <Badge label={b.artifact_type} color="#0d9488" />}
            {b.status === "merged" && confirmDelete !== b.id && (
              <button
                style={{ ...S.btn, ...S.btnD, padding: "3px 10px", fontSize: 11 }}
                onClick={e => { e.stopPropagation(); setConfirmDelete(b.id); }}
                title="Delete merged branch"
              >
                Clean up
              </button>
            )}
            {b.status === "merged" && confirmDelete === b.id && (
              <>
                <span style={{ fontSize: 11, color: "#ef4444" }}>Delete?</span>
                <button style={{ ...S.btn, background: "#ef4444", color: "#fff", border: "none", padding: "3px 10px", fontSize: 11 }}
                  onClick={e => { e.stopPropagation(); setConfirmDelete(null); api("DELETE", `${API}/${b.id}`).then(load); }}>
                  Yes
                </button>
                <button style={{ ...S.btn, ...S.btnS, padding: "3px 8px", fontSize: 11 }}
                  onClick={e => { e.stopPropagation(); setConfirmDelete(null); }}>
                  No
                </button>
              </>
            )}
          </div>
          <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>
            from <b>{b.source_env_name}</b> · pulled by {b.created_by} · {fmtDate(b.created_at)}
            {b.description && ` · ${b.description}`}
            {b.status === "merged" && b.merged_at && (
              <span style={{ marginLeft: 8, color: "#0d9488" }}>· Merged {fmtDate(b.merged_at)}</span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

const EDITOR_URL: Record<string, (b: Branch) => string> = {
  case_type:   b => `/case-designer?caseType=${b.artifact_id}&branch=${b.id}`,
  form:        b => `/form-builder?branch=${b.id}`,
  escalation:  b => `/escalation?branch=${b.id}`,
  integration: b => `/hxbridge?branch=${b.id}`,
  connector:   b => `/hxbridge?branch=${b.id}`,
};

function BranchDetail({ branch: initialBranch, onBack }: { branch: Branch; onBack: () => void }) {
  const navigate = useNavigate();
  const myGroups = useCurrentUserGroups();
  const [branch, setBranch]           = useState(initialBranch);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [activeTab, setActiveTab] = useState<"diff" | "reviews" | "activity">("diff");
  const [decision, setDecision] = useState<"approved" | "rejected" | "changes_requested">("approved");
  const [comments, setComments] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showReviewer, setShowReviewer] = useState(false);
  const [reviewerId, setReviewerId]     = useState("");

  const refreshBranch = useCallback(async () => {
    const d = await api("GET", `${API}/${branch.id}`).catch(() => null);
    if (d) setBranch(d);
  }, [branch.id]);

  useEffect(() => {
    api("GET", `${API}/${branch.id}/reviews`).then(d => setReviews(d?.reviews ?? [])).catch(() => {});
    api("GET", `${API}/${branch.id}/audit`).then(d => setAuditEvents(d?.events ?? [])).catch(() => {});
  }, [branch.id]);

  const act = async (fn: () => Promise<unknown>, stayOnPage = false) => {
    setBusy(true); setMsg(null);
    try { await fn(); if (!stayOnPage) onBack(); else await refreshBranch(); }
    catch (e: any) { setMsg(e.message); }
    finally { setBusy(false); }
  };

  const handleSubmitForReview = () => {
    if (!reviewerId.trim()) return;
    act(() => api("POST", `${API}/${branch.id}/submit`, { assigned_reviewer_id: reviewerId.trim() }));
    setShowReviewer(false); setReviewerId("");
  };

  const diff = branch.diff_vs_main;
  const editorUrl = branch.artifact_type ? EDITOR_URL[branch.artifact_type]?.(branch) : null;

  return (
    <>
    <div style={S.body}>
      <button style={{ ...S.btn, ...S.btnS, marginBottom: 16 }} onClick={onBack}>← Back to Branches</button>

      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
        <span style={{ fontSize: 18, fontWeight: 700, fontFamily: "monospace" }}>{branch.name}</span>
        <Badge label={branch.status.replace("_", " ")} color={STATUS_COLOR[branch.status] ?? "#888"} />
        {branch.conflict_detected && <Badge label="⚠ CONFLICT" color="#ef4444" />}
      </div>
      <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 20, display: "flex", alignItems: "center", gap: 12 }}>
        <span>
          {branch.artifact_type && <><b>{branch.artifact_type}</b> · </>}
          from <b>{branch.source_env_name}</b> · {fmtDate(branch.created_at)}
          {branch.description && ` · ${branch.description}`}
          {branch.assigned_reviewer_id && <> · Reviewer: <b>{branch.assigned_reviewer_name || branch.assigned_reviewer_id}</b></>}
        </span>
        {editorUrl && (
          <button style={{ ...S.btn, ...S.btnP, padding: "4px 12px", fontSize: 11 }}
            onClick={() => navigate(editorUrl)}>
            Open in Native Editor →
          </button>
        )}
      </div>

      {msg && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 12, padding: "8px 12px", background: "#fee2e2", borderRadius: 6 }}>{msg}</div>}

      {/* Actions */}
      <div style={{ ...S.card, marginBottom: 16 }}>
        <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 10, color: "var(--text-secondary)" }}>ACTIONS</div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          {(branch.status === "open" || branch.status === "changes_requested") && !showReviewer && (
            <button style={{ ...S.btn, ...S.btnP }} disabled={busy} onClick={() => setShowReviewer(true)}>
              Submit for Review
            </button>
          )}
          {branch.status === "pending_review" && (
            <button style={{ ...S.btn, ...S.btnS }} disabled={busy}
              onClick={() => act(() => api("POST", `${API}/${branch.id}/recall`), true)}>
              {busy ? "…" : "Recall"}
            </button>
          )}
          {(branch.status === "open" || branch.status === "changes_requested") && (
            <button style={{ ...S.btn, ...S.btnS }} disabled={busy}
              onClick={() => act(() => api("POST", `${API}/${branch.id}/revert-to-base`), true)}>
              {busy ? "…" : "↩ Revert to Base"}
            </button>
          )}
          {branch.status === "approved" && (
            <button style={{ ...S.btn, ...S.btnP }} disabled={busy}
              onClick={() => act(() => api("POST", `${API}/${branch.id}/merge`))}>
              {busy ? "Merging…" : "Merge to Main"}
            </button>
          )}
          {branch.artifact_type === "rule" && (
            <button style={{ ...S.btn, ...S.btnS }} disabled={busy}
              title="Replay recent real cases against this branch's rule change (HxReplay)"
              onClick={() => { window.location.href = `/hxreplay?branch=${branch.id}`; }}>
              Simulate on history
            </button>
          )}
          <button style={{ ...S.btn, ...S.btnD }} disabled={busy}
            onClick={() => setShowDeleteModal(true)}>
            Delete Branch
          </button>
        </div>
        {showReviewer && (
          <div style={{ marginTop: 10, display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 12, color: "var(--text-secondary)", whiteSpace: "nowrap" }}>Reviewer:</span>
            <ReviewerPicker
              autoFocus
              value={reviewerId}
              onChange={setReviewerId}
              accessGroupId={myGroups[0]}
              onKeyDown={e => e.key === "Enter" && handleSubmitForReview()}
              placeholder="Must differ from branch owner"
            />
            <button style={{ ...S.btn, ...S.btnP, padding: "5px 14px", fontSize: 12 }}
              disabled={busy || !reviewerId.trim()} onClick={handleSubmitForReview}>
              {busy ? "…" : "Confirm"}
            </button>
            <button style={{ ...S.btn, ...S.btnS, padding: "5px 10px", fontSize: 12 }}
              onClick={() => { setShowReviewer(false); setReviewerId(""); }}>Cancel</button>
          </div>
        )}
      </div>

      {/* Tab bar: Diff / Reviews / Activity */}
      <div style={{ display: "flex", gap: 2, borderBottom: "1px solid var(--border)", marginBottom: 12 }}>
        {(["diff", "reviews", "activity"] as const).map(t => (
          <button key={t} style={{
            ...S.btn, ...S.btnS, borderBottom: `2px solid ${activeTab === t ? "var(--accent)" : "transparent"}`,
            borderRadius: 0, fontWeight: activeTab === t ? 700 : 400,
            color: activeTab === t ? "var(--accent)" : "var(--text-secondary)",
            background: "transparent", border: "none",
            borderBottomWidth: 2, borderBottomStyle: "solid",
            borderBottomColor: activeTab === t ? "var(--accent)" : "transparent",
            padding: "8px 16px", fontSize: 12,
          }}
            onClick={() => setActiveTab(t)}>
            {t === "diff" ? "Diff" : t === "reviews" ? `Reviews (${reviews.length})` : `Activity (${auditEvents.length})`}
          </button>
        ))}
      </div>

      {/* Diff tab */}
      {activeTab === "diff" && (
        <div style={S.card}>
          <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 10, color: "var(--text-secondary)" }}>DIFF — BRANCH vs DEV MAIN</div>
          {!diff || diff.total_changes === 0
            ? <div style={{ fontSize: 12, color: "#22c55e" }}>✓ No changes vs current main.</div>
            : diff.changed_fields.map((f, i) => (
              <div key={i} style={{ marginBottom: 10, padding: "8px 12px", background: "var(--bg-main)", borderRadius: 6, borderLeft: "3px solid #f59e0b" }}>
                <div style={{ ...S.mono, fontWeight: 700, marginBottom: 4, color: "var(--text-primary)" }}>{f.field}</div>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                  <div>
                    <div style={{ fontSize: 10, color: "#ef4444", fontWeight: 700, marginBottom: 2 }}>MAIN (current)</div>
                    <pre style={{ ...S.mono, color: "#ef4444", margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{JSON.stringify(f.base, null, 2)}</pre>
                  </div>
                  <div>
                    <div style={{ fontSize: 10, color: "#22c55e", fontWeight: 700, marginBottom: 2 }}>BRANCH (incoming)</div>
                    <pre style={{ ...S.mono, color: "#22c55e", margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{JSON.stringify(f.branch, null, 2)}</pre>
                  </div>
                </div>
              </div>
            ))
          }
          {diff && diff.added_keys.length > 0 && <div style={{ fontSize: 11, color: "#22c55e", marginTop: 6 }}>+ New fields: {diff.added_keys.join(", ")}</div>}
          {diff && diff.removed_keys.length > 0 && <div style={{ fontSize: 11, color: "#ef4444", marginTop: 4 }}>− Removed fields: {diff.removed_keys.join(", ")}</div>}
        </div>
      )}

      {/* Reviews tab */}
      {activeTab === "reviews" && (
        <div style={S.card}>
          <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 10, color: "var(--text-secondary)" }}>REVIEWS</div>
          {reviews.length === 0 && <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 12 }}>No reviews yet.</div>}
          {reviews.map(r => (
            <div key={r.id} style={{ marginBottom: 8, padding: "8px 12px", background: "var(--bg-main)", borderRadius: 6, borderLeft: `3px solid ${DECISION_COLOR[r.decision] ?? "#888"}` }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
                <Badge label={r.decision.replace("_", " ")} color={DECISION_COLOR[r.decision] ?? "#888"} />
                <span style={{ fontSize: 11, color: "var(--text-secondary)" }}>{r.reviewer_id} · {fmtDate(r.created_at)}</span>
              </div>
              {r.comments && <div style={{ fontSize: 12, color: "var(--text-primary)" }}>{r.comments}</div>}
            </div>
          ))}

          {branch.status === "pending_review" && (
            <div style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
              <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                {(["approved", "changes_requested", "rejected"] as const).map(d => (
                  <button key={d} style={{
                    ...S.btn, fontSize: 11,
                    background: decision === d ? (DECISION_COLOR[d] + "22") : "transparent",
                    border: `1px solid ${DECISION_COLOR[d]}`,
                    color: DECISION_COLOR[d],
                  }} onClick={() => setDecision(d)}>{d.replace("_", " ")}</button>
                ))}
              </div>
              <textarea placeholder="Comments (optional)…" value={comments} onChange={e => setComments(e.target.value)}
                style={{ ...S.input, height: 60, resize: "vertical", marginBottom: 8 }} />
              <button style={{ ...S.btn, ...S.btnP }} disabled={busy}
                onClick={() => act(() => api("POST", `${API}/${branch.id}/reviews`, { decision, comments: comments || null }))}>
                Submit Review
              </button>
            </div>
          )}
        </div>
      )}

      {/* Activity (audit trail) tab */}
      {activeTab === "activity" && (
        <div style={S.card}>
          <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 14, color: "var(--text-secondary)" }}>BRANCH ACTIVITY</div>
          {auditEvents.length === 0 && (
            <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>No activity recorded yet. Actions from this point forward will appear here.</div>
          )}
          <div style={{ position: "relative" }}>
            {auditEvents.length > 0 && (
              <div style={{ position: "absolute", left: 11, top: 12, bottom: 12, width: 2, background: "var(--border)", zIndex: 0 }} />
            )}
            {auditEvents.map((ev, i) => {
              const color = AUDIT_EVENT_COLOR[ev.event_type] ?? "#6b7280";
              const label = AUDIT_EVENT_LABEL[ev.event_type] ?? ev.event_type.replace(/_/g, " ");
              return (
                <div key={ev.id} style={{ display: "flex", gap: 12, marginBottom: i < auditEvents.length - 1 ? 16 : 0, position: "relative", zIndex: 1 }}>
                  {/* Dot */}
                  <div style={{ width: 24, height: 24, borderRadius: "50%", background: color + "22", border: `2px solid ${color}`, flexShrink: 0, display: "flex", alignItems: "center", justifyContent: "center" }}>
                    <div style={{ width: 8, height: 8, borderRadius: "50%", background: color }} />
                  </div>
                  {/* Content */}
                  <div style={{ flex: 1, paddingTop: 2 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 2 }}>
                      <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)" }}>{label}</span>
                      {ev.event_type === "reviewed" && !!ev.metadata.decision && (
                        <Badge label={String(ev.metadata.decision).replace("_", " ")} color={DECISION_COLOR[String(ev.metadata.decision)] ?? "#888"} />
                      )}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                      {ev.actor_name && <span>{ev.actor_name} · </span>}
                      {fmtDate(ev.created_at)}
                      {ev.event_type === "submitted_for_review" && !!(ev.metadata.assigned_reviewer_name || ev.metadata.assigned_reviewer_id) && (
                        <span> · Reviewer: <strong>{String(ev.metadata.assigned_reviewer_name || ev.metadata.assigned_reviewer_id)}</strong></span>
                      )}
                      {ev.event_type === "merged" && ev.metadata.changes !== undefined && (
                        <span> · {String(ev.metadata.changes)} field(s) changed{ev.metadata.via === "admin_override" ? " (admin override)" : ""}</span>
                      )}
                      {ev.event_type === "reviewed" && !!ev.metadata.comments && (
                        <span> · "{String(ev.metadata.comments)}"</span>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>

    {/* ── Delete confirmation modal ── */}
    {showDeleteModal && (
      <div style={{
        position: "fixed", inset: 0, zIndex: 10000,
        background: "rgba(0,0,0,0.55)", display: "flex", alignItems: "center", justifyContent: "center",
      }} onClick={() => setShowDeleteModal(false)}>
        <div style={{
          background: "var(--bg-panel)", border: "1px solid var(--border)",
          borderRadius: 10, padding: 28, maxWidth: 420, width: "90%",
          boxShadow: "0 16px 48px rgba(0,0,0,0.3)",
        }} onClick={e => e.stopPropagation()}>
          {/* Header */}
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
            <div style={{ width: 36, height: 36, borderRadius: 8, background: "#fee2e2", display: "flex", alignItems: "center", justifyContent: "center" }}>
              <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                <path d="M9 3L16 15H2L9 3Z" stroke="#ef4444" strokeWidth="1.5" strokeLinejoin="round" />
                <path d="M9 8v3" stroke="#ef4444" strokeWidth="1.5" strokeLinecap="round" />
                <circle cx="9" cy="13" r="0.75" fill="#ef4444" />
              </svg>
            </div>
            <div>
              <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)" }}>Delete Branch</div>
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>This action cannot be undone</div>
            </div>
          </div>

          {/* Branch name */}
          <div style={{ background: "var(--bg-elevated)", borderRadius: 6, padding: "8px 12px", marginBottom: 14, fontFamily: "var(--font-mono)", fontSize: 13, color: "var(--text-primary)", border: "1px solid var(--border)" }}>
            {branch.name}
          </div>

          {/* Warning if linked to a story */}
          <div style={{ background: "#fffbeb", border: "1px solid #fde68a", borderRadius: 6, padding: "10px 12px", marginBottom: 20, fontSize: 12, color: "#92400e" }}>
            <strong>⚠ HxWork board impact:</strong> If this branch is linked to a story, the story will be automatically unlinked. You can re-link a new branch from the story panel in HxWork.
          </div>

          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button style={{ ...S.btn, ...S.btnS }} onClick={() => setShowDeleteModal(false)}>
              Cancel
            </button>
            <button style={{ ...S.btn, background: "#ef4444", color: "#fff", border: "none" }} disabled={busy}
              onClick={() => { setShowDeleteModal(false); act(() => api("DELETE", `${API}/${branch.id}`)); }}>
              {busy ? "Deleting…" : "Delete Branch"}
            </button>
          </div>
        </div>
      </div>
    )}
    </>
  );
}

// ── Create a branch from a local artifact ────────────────────────

type ArtifactOption = { id: string; name: string };

async function fetchArtifactOptions(type: string): Promise<ArtifactOption[]> {
  try {
    const AUTH = () => ({ Authorization: `Bearer ${localStorage.getItem("helix_token") ?? ""}` });
    const get = async (url: string) => {
      const r = await fetch(url, { headers: { ...AUTH() } });
      if (!r.ok) return null;
      return r.json();
    };
    if (type === "case_type") {
      const d = await get("/api/v1/case-types?page_size=200");
      return (d?.items ?? []).map((i: any) => ({ id: i.id, name: i.name }));
    }
    if (type === "form") {
      const d = await get("/api/v1/forms?page_size=200");
      return (Array.isArray(d) ? d : (d?.items ?? [])).map((i: any) => ({ id: i.id, name: i.name }));
    }
    if (type === "rule") {
      const d = await get("/api/v1/rules?page_size=200");
      return (Array.isArray(d) ? d : (d?.items ?? [])).map((i: any) => ({ id: i.id, name: i.name }));
    }
    if (type === "integration") {
      const d = await get("/api/v1/hxbridge/connectors");
      return (d?.connectors ?? []).map((i: any) => ({ id: i.id, name: i.name }));
    }
    if (type === "escalation") {
      const d = await get("/api/v1/escalation-trees?active_only=false");
      return (Array.isArray(d) ? d : (d?.items ?? [])).map((i: any) => ({ id: i.id, name: i.name }));
    }
    return [];
  } catch { return []; }
}

function CreateLocalBranchForm({ onCreated }: { onCreated: () => void }) {
  const myGroups = useCurrentUserGroups();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [artifactType, setArtifactType] = useState("case_type");
  const [artifactId, setArtifactId] = useState("");
  const [artifactOptions, setArtifactOptions] = useState<ArtifactOption[]>([]);
  const [loadingArtifacts, setLoadingArtifacts] = useState(false);
  const [description, setDescription] = useState("");
  const [reviewerId, setReviewerId] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const loadArtifacts = useCallback(async (type: string) => {
    setLoadingArtifacts(true);
    setArtifactId("");
    const opts = await fetchArtifactOptions(type);
    setArtifactOptions(opts);
    setLoadingArtifacts(false);
  }, []);

  useEffect(() => { if (open) loadArtifacts(artifactType); }, [open, artifactType, loadArtifacts]);

  const submit = async () => {
    if (!name.trim() || !artifactId.trim()) return;
    setBusy(true); setErr(null);
    try {
      await api("POST", API, {
        name: name.trim(),
        description: description.trim() || undefined,
        artifact_type: artifactType,
        artifact_id: artifactId.trim(),
        assigned_reviewer_id: reviewerId.trim() || undefined,
      });
      setName(""); setArtifactId(""); setDescription(""); setReviewerId("");
      setOpen(false);
      onCreated();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  };

  if (!open) {
    return (
      <button style={{ ...S.btn, ...S.btnP, marginBottom: 16 }} onClick={() => setOpen(true)}>
        + Branch from Local Artifact
      </button>
    );
  }

  const selectedArtifact = artifactOptions.find(o => o.id === artifactId);

  return (
    <div style={{ ...S.card, marginBottom: 16 }}>
      <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 12, color: "var(--text-secondary)" }}>
        NEW BRANCH FROM LOCAL ARTIFACT
      </div>
      {err && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 8 }}>{err}</div>}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 10 }}>
        <div>
          <label style={S.label}>Branch Name</label>
          <input style={S.input} placeholder="e.g. fix/loan-form-fields" value={name}
            onChange={e => setName(e.target.value)} />
        </div>
        <div>
          <label style={S.label}>Artifact Type</label>
          <select style={S.select} value={artifactType}
            onChange={e => { setArtifactType(e.target.value); }}>
            {ARTIFACT_TYPES.filter(t => t.value !== "app").map(t => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
        </div>
      </div>
      <div style={{ marginBottom: 10 }}>
        <label style={S.label}>Artifact</label>
        {loadingArtifacts ? (
          <div style={{ ...S.input, color: "var(--text-muted)", display: "flex", alignItems: "center" }}>Loading…</div>
        ) : artifactOptions.length === 0 ? (
          <div style={{ fontSize: 11, color: "var(--text-muted)", padding: "6px 0" }}>
            No {artifactType.replace("_", " ")}s found — make sure the service is running.
          </div>
        ) : (
          <select style={S.select} value={artifactId}
            onChange={e => setArtifactId(e.target.value)}>
            <option value="">— select {artifactType.replace("_", " ")} —</option>
            {artifactOptions.map(o => (
              <option key={o.id} value={o.id}>{o.name}</option>
            ))}
          </select>
        )}
        {selectedArtifact && (
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 3, fontFamily: "var(--font-mono)" }}>
            ID: {selectedArtifact.id}
          </div>
        )}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 10 }}>
        <div>
          <label style={S.label}>Description (optional)</label>
          <input style={S.input} placeholder="What are you changing?"
            value={description} onChange={e => setDescription(e.target.value)} />
        </div>
        <div>
          <label style={S.label}>Reviewer (optional)</label>
          <ReviewerPicker
            value={reviewerId}
            onChange={setReviewerId}
            accessGroupId={myGroups[0]}
            placeholder="Assign reviewer now or at submit"
          />
        </div>
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <button style={{ ...S.btn, ...S.btnP }}
          disabled={busy || !name.trim() || !artifactId.trim()} onClick={submit}>
          {busy ? "Creating…" : "Create Branch"}
        </button>
        <button style={{ ...S.btn, ...S.btnS }} onClick={() => { setOpen(false); setErr(null); }}>
          Cancel
        </button>
      </div>
    </div>
  );
}


// ═══════════════════════════════════════════════════════════════════
// Tab 2 — Pull from Environment
// ═══════════════════════════════════════════════════════════════════

const ARTIFACT_TYPES = [
  { value: "case_type",  label: "Case Types" },
  { value: "form",       label: "Forms" },
  { value: "rule",       label: "Rules" },
  { value: "integration",label: "Connectors" },
  { value: "escalation", label: "Escalation Trees" },
  { value: "app",        label: "App Packages" },
];

function PullTab() {
  const [envs, setEnvs] = useState<Env[]>([]);
  const [selectedEnvId, setSelectedEnvId] = useState("");
  const [artifactType, setArtifactType] = useState("case_type");
  const [items, setItems] = useState<RemoteItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [pulling, setPulling] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  useEffect(() => {
    api("GET", `${DAPI}/environments`).then(d => setEnvs(d?.environments ?? [])).catch(() => {});
  }, []);

  const browse = async () => {
    if (!selectedEnvId) return;
    setLoading(true); setMsg(null); setItems([]); setSuccess(null);
    try {
      const d = await api("GET", `${API}/remote/${selectedEnvId}/available?artifact_type=${artifactType}`);
      const raw = d?.items;
      const list = Array.isArray(raw) ? raw
        : (raw?.case_types ?? raw?.forms ?? raw?.packages ?? raw?.items ?? []);
      setItems(list);
    } catch (e: any) { setMsg(e.message); }
    finally { setLoading(false); }
  };

  const pull = async (item: RemoteItem) => {
    const name = prompt(`Branch name for "${item.name}":`, `fix/${item.name.toLowerCase().replace(/\s+/g, "-")}`);
    if (!name) return;
    setPulling(item.id); setMsg(null); setSuccess(null);
    try {
      const body: any = {
        env_id: selectedEnvId,
        branch_name: name,
        branch_type: artifactType === "app" ? "app" : "artifact",
        artifact_type: artifactType !== "app" ? artifactType : undefined,
        artifact_id: artifactType !== "app" ? item.id : undefined,
      };
      await api("POST", `${API}/pull`, body);
      setSuccess(`Branch "${name}" created. Go to Branches tab to review and merge.`);
    } catch (e: any) { setMsg(e.message); }
    finally { setPulling(null); }
  };

  const envLabel = (id: string) => envs.find(e => e.id === id)?.label ?? id;

  return (
    <div style={S.body}>
      {msg && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 12, padding: "8px 12px", background: "#fee2e2", borderRadius: 6 }}>{msg}</div>}
      {success && <div style={{ fontSize: 12, color: "#22c55e", marginBottom: 12, padding: "8px 12px", background: "#dcfce7", borderRadius: 6 }}>✓ {success}</div>}

      <div style={{ ...S.card, marginBottom: 20 }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 10, alignItems: "flex-end" }}>
          <div>
            <label style={S.label}>Source Environment</label>
            <select style={S.select} value={selectedEnvId} onChange={e => { setSelectedEnvId(e.target.value); setItems([]); }}>
              <option value="">— select environment —</option>
              {envs.filter(e => e.api_token_enc).map(e => (
                <option key={e.id} value={e.id}>{e.label} ({e.name})</option>
              ))}
            </select>
            {envs.some(e => !e.api_token_enc) && (
              <div style={{ fontSize: 10, color: "#f59e0b", marginTop: 4 }}>
                {envs.filter(e => !e.api_token_enc).length} environment(s) missing API token — configure in Connections tab.
              </div>
            )}
          </div>
          <div>
            <label style={S.label}>Artifact Type</label>
            <select style={S.select} value={artifactType} onChange={e => { setArtifactType(e.target.value); setItems([]); }}>
              {ARTIFACT_TYPES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
            </select>
          </div>
          <button style={{ ...S.btn, ...S.btnP, alignSelf: "flex-end" }}
            disabled={!selectedEnvId || loading} onClick={browse}>
            {loading ? "Loading…" : "Browse →"}
          </button>
        </div>
      </div>

      {items.length === 0 && !loading && selectedEnvId && (
        <div style={{ padding: 32, color: "var(--text-secondary)", fontSize: 13 }}>
          No items found. Try browsing another type.
        </div>
      )}

      {items.map(item => (
        <div key={item.id} style={{ ...S.card, display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 13, fontWeight: 600 }}>{item.name}</div>
            <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>
              {item.version && `v${item.version} · `}
              id: <span style={S.mono}>{String(item.id).slice(0, 16)}…</span>
            </div>
          </div>
          <button style={{ ...S.btn, ...S.btnP }}
            disabled={pulling === item.id}
            onClick={() => pull(item)}>
            {pulling === item.id ? "Pulling…" : "Pull as Branch"}
          </button>
        </div>
      ))}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════
// Tab 3 — Connections
// ═══════════════════════════════════════════════════════════════════

function ConnectionsTab() {
  const [envs, setEnvs] = useState<Env[]>([]);
  const [tokens, setTokens] = useState<Record<string, string>>({});
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; latency_ms: number; message: string }>>({});
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    api("GET", `${DAPI}/environments`).then(d => setEnvs(d?.environments ?? [])).catch(e => setMsg(e.message));
  }, []);

  const saveToken = async (envId: string) => {
    const token = tokens[envId];
    if (!token?.trim()) return;
    try {
      await api("POST", `${API}/envs/${envId}/token`, { api_token: token.trim() });
      setMsg(null);
      setEnvs(prev => prev.map(e => e.id === envId ? { ...e, api_token_enc: true } : e));
      setTokens(prev => ({ ...prev, [envId]: "" }));
    } catch (e: any) { setMsg(e.message); }
  };

  const testConnection = async (envId: string) => {
    try {
      const r = await api("POST", `${API}/envs/${envId}/test-connection`);
      setTestResults(prev => ({ ...prev, [envId]: r }));
    } catch (e: any) { setTestResults(prev => ({ ...prev, [envId]: { ok: false, latency_ms: 0, message: e.message } })); }
  };

  return (
    <div style={S.body}>
      <div style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 16 }}>
        API tokens allow HxBranch to connect live to remote Velaris environments. Generate a token from each environment's admin panel and paste it here. Tokens are stored AES-256 encrypted.
      </div>
      {msg && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 12 }}>{msg}</div>}

      {envs.length === 0 && (
        <div style={{ padding: 32, color: "var(--text-secondary)", fontSize: 13 }}>
          No environments registered. Add environments in HxDeploy first.
        </div>
      )}

      {envs.map(env => {
        const test = testResults[env.id];
        const hasToken = !!env.api_token_enc;
        return (
          <div key={env.id} style={S.card}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
              <span style={{ fontSize: 14, fontWeight: 700, flex: 1 }}>{env.label}</span>
              <Badge label={env.name} color="#0d9488" />
              {hasToken
                ? <Badge label="TOKEN SET" color="#22c55e" />
                : <Badge label="NO TOKEN" color="#f59e0b" />}
            </div>
            {env.url && <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: 10, fontFamily: "monospace" }}>{env.url}</div>}

            {/* Token input */}
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 10 }}>
              <input
                type="password"
                style={{ ...S.input, flex: 1 }}
                placeholder={hasToken ? "●●●●●●●● (set — paste new token to rotate)" : "Paste API token from remote Velaris…"}
                value={tokens[env.id] ?? ""}
                onChange={e => setTokens(prev => ({ ...prev, [env.id]: e.target.value }))}
              />
              <button style={{ ...S.btn, ...S.btnP, whiteSpace: "nowrap" }}
                disabled={!tokens[env.id]?.trim()}
                onClick={() => saveToken(env.id)}>
                {hasToken ? "Rotate" : "Set Token"}
              </button>
            </div>

            {/* Test connection */}
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <button style={{ ...S.btn, ...S.btnS }} disabled={!hasToken}
                onClick={() => testConnection(env.id)}>
                Test Connection
              </button>
              {test && (
                <span style={{ fontSize: 12, color: test.ok ? "#22c55e" : "#ef4444" }}>
                  {test.ok ? "✓" : "✗"} {test.message} {test.latency_ms > 0 && `(${test.latency_ms}ms)`}
                </span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════
// Root component
// ═══════════════════════════════════════════════════════════════════

type TabKey = "branches" | "pull" | "connections";

export default function HxBranch() {
  const [tab, setTab] = useState<TabKey>("branches");

  const tabs: { key: TabKey; label: string }[] = [
    { key: "branches",    label: "Branches" },
    { key: "pull",        label: "Pull from Environment" },
    { key: "connections", label: "Connections" },
  ];

  return (
    <div style={S.page}>
      <div style={S.tabBar}>
        {tabs.map(t => (
          <button key={t.key} style={{ ...S.tab, ...(tab === t.key ? S.tabA : {}) }} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === "branches"    && <BranchesTab />}
      {tab === "pull"        && <PullTab />}
      {tab === "connections" && <ConnectionsTab />}
    </div>
  );
}
