import React, { useState } from "react";
import { Button } from "./index";
import { ReviewerPicker } from "./ReviewerPicker";
import { useNavigate } from "react-router-dom";

function HxBranchIcon({ locked }: { locked: boolean }) {
  const c = locked ? "#f59e0b" : "var(--accent)";
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
      <circle cx="4.5" cy="13" r="1.8" stroke={c} strokeWidth="1.3" />
      <circle cx="4.5" cy="3.5" r="1.8" stroke={c} strokeWidth="1.3" />
      <circle cx="12"  cy="6"   r="1.8" stroke={c} strokeWidth="1.3" />
      <path d="M4.5 11.2V5.3" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
      <path d="M4.5 8C4.5 8 4.5 6 10.2 6" stroke={c} strokeWidth="1.3" strokeLinecap="round" />
      {locked && <path d="M11 9.5h2v2.5h-2z" stroke={c} strokeWidth="1" strokeLinejoin="round" />}
      {locked && <path d="M11.5 9.5V8.5a.5.5 0 011 0v1" stroke={c} strokeWidth="1" strokeLinecap="round" />}
    </svg>
  );
}

const STATUS_COLOR: Record<string, string> = {
  open:               "#3b82f6",
  pending_review:     "#f59e0b",
  changes_requested:  "#ef4444",
  approved:           "#22c55e",
  merged:             "#8b5cf6",
  rejected:           "#ef4444",
  closed:             "#6b7280",
};

const STATUS_LABEL: Record<string, string> = {
  open:               "Open",
  pending_review:     "Pending Review",
  changes_requested:  "Changes Requested",
  approved:           "Approved",
  merged:             "Merged",
  rejected:           "Rejected",
  closed:             "Closed",
};

const READ_ONLY_STATUSES = new Set(["merged", "rejected", "closed"]);

const READ_ONLY_MSG: Record<string, string> = {
  merged:   "Changes are live in main — this branch is read-only.",
  rejected: "Branch was rejected — this branch is read-only.",
  closed:   "Branch is closed — this branch is read-only.",
};

interface BranchModeBannerProps {
  branch:            any;
  saving:            boolean;
  error:             string | null;
  accessGroupId?:    string;
  onSubmitForReview: (reviewerId: string) => void;
  onRecall:          () => void;
}

export function BranchModeBanner({
  branch,
  saving,
  error,
  accessGroupId,
  onSubmitForReview,
  onRecall,
}: BranchModeBannerProps) {
  const navigate = useNavigate();
  const [showReviewer, setShowReviewer] = useState(false);
  const [reviewerId, setReviewerId]     = useState("");

  if (!branch) return null;

  const status    = branch.status as string;
  const color     = STATUS_COLOR[status] ?? "#6b7280";
  const isLocked  = status === "pending_review";
  const isReadOnly = READ_ONLY_STATUSES.has(status);
  const canSubmit = status === "open" || status === "changes_requested";
  const canRecall = status === "pending_review";

  const handleSubmit = () => {
    if (!reviewerId.trim()) return;
    onSubmitForReview(reviewerId.trim());
    setShowReviewer(false);
    setReviewerId("");
  };

  // Read-only banner for merged/rejected/closed
  if (isReadOnly) {
    return (
      <div style={{
        borderBottom: "1px solid color-mix(in srgb, #8b5cf6 25%, transparent)",
        background: "color-mix(in srgb, #8b5cf6 5%, var(--bg-panel))",
        padding: "9px var(--space-xl)",
        display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <HxBranchIcon locked={false} />
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontSize: 12, fontWeight: 700, color: "var(--text-primary)", fontFamily: "var(--font-mono)" }}>
                BRANCH MODE
              </span>
              <span style={{
                fontSize: 11, padding: "2px 8px", borderRadius: 4, fontFamily: "var(--font-mono)",
                fontWeight: 700, textTransform: "uppercase",
                background: `color-mix(in srgb, ${color} 15%, transparent)`,
                color, border: `1px solid color-mix(in srgb, ${color} 30%, transparent)`,
              }}>
                {STATUS_LABEL[status] ?? status}
              </span>
              <span style={{
                fontSize: 10, padding: "2px 7px", borderRadius: 4,
                background: "#fef3c7", color: "#d97706",
                fontFamily: "var(--font-mono)", fontWeight: 700, textTransform: "uppercase",
              }}>
                READ ONLY
              </span>
            </div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 1 }}>
              {branch.name}
              {branch.merged_by && status === "merged" && (
                <span style={{ marginLeft: 8 }}>· Merged by <strong>{branch.merged_by}</strong></span>
              )}
              <span style={{ marginLeft: 8, color: "#d97706" }}>· {READ_ONLY_MSG[status]}</span>
            </div>
          </div>
        </div>
        <Button variant="secondary" size="sm" onClick={() => navigate("/hxbranch")}>
          View in HxBranch →
        </Button>
      </div>
    );
  }

  return (
    <div style={{
      borderBottom: "1px solid color-mix(in srgb, var(--accent) 25%, transparent)",
      background: "color-mix(in srgb, var(--accent) 6%, var(--bg-panel))",
    }}>
      <div style={{ padding: "9px var(--space-xl)", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <HxBranchIcon locked={isLocked} />
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontSize: 12, fontWeight: 700, color: "var(--text-primary)", fontFamily: "var(--font-mono)" }}>
                BRANCH MODE
              </span>
              <span style={{
                fontSize: 11, padding: "2px 8px", borderRadius: 4, fontFamily: "var(--font-mono)",
                fontWeight: 700, textTransform: "uppercase",
                background: `color-mix(in srgb, ${color} 15%, transparent)`,
                color, border: `1px solid color-mix(in srgb, ${color} 30%, transparent)`,
              }}>
                {STATUS_LABEL[status] ?? status}
              </span>
            </div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 1 }}>
              {branch.name}
              {branch.assigned_reviewer_id && (
                <span style={{ marginLeft: 8 }}>· Reviewer: <strong>{branch.assigned_reviewer_id}</strong></span>
              )}
              {isLocked && <span style={{ marginLeft: 8, color: "#f59e0b" }}>· Locked — recall to edit</span>}
              {status === "changes_requested" && <span style={{ marginLeft: 8, color: "#ef4444" }}>· Reviewer requested changes</span>}
            </div>
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {error && <span style={{ fontSize: 11, color: "var(--status-failed)", fontFamily: "var(--font-mono)" }}>{error}</span>}
          {canSubmit && !showReviewer && (
            <Button size="sm" onClick={() => setShowReviewer(true)} disabled={saving}>
              Submit for Review
            </Button>
          )}
          {canRecall && (
            <Button variant="secondary" size="sm" onClick={onRecall} disabled={saving}>
              {saving ? "Recalling…" : "Recall"}
            </Button>
          )}
        </div>
      </div>

      {showReviewer && (
        <div style={{ padding: "8px var(--space-xl)", borderTop: "1px solid color-mix(in srgb, var(--accent) 15%, transparent)", display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 11, color: "var(--text-secondary)", whiteSpace: "nowrap" }}>Reviewer user ID:</span>
          <ReviewerPicker
            autoFocus
            value={reviewerId}
            onChange={setReviewerId}
            accessGroupId={accessGroupId}
            onKeyDown={e => e.key === "Enter" && handleSubmit()}
            placeholder="Must be different from branch owner"
          />
          <Button size="sm" onClick={handleSubmit} disabled={saving || !reviewerId.trim()}>
            {saving ? "Submitting…" : "Confirm"}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => { setShowReviewer(false); setReviewerId(""); }}>Cancel</Button>
        </div>
      )}
    </div>
  );
}
