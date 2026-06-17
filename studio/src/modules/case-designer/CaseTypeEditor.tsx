import React, { useState, useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { useApi, useCommit, useBranchMode, useCurrentUserGroups } from "@shared/hooks";
import { getCaseType, listForms, createForm, updateForm as updateFormApi } from "@shared/api/client";
import { Button, Spinner, CommitModal, CommitHistory, BranchModeBanner } from "@shared/components";
import type { CommitRecord } from "@shared/components/CommitHistory";

// ── Version Bump Modal ────────────────────────────────────────────────────────
function VersionBumpModal({ caseType, onClose, onBumped }: { caseType: any; onClose: () => void; onBumped: (newId: string, newVersion: string) => void }) {
  const [bumpType, setBumpType] = useState<"patch" | "minor" | "major">("patch");
  const [changelog, setChangelog] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const bumpedVersion = (v: string, t: string) => {
    const [maj, min, pat] = v.split(".").map(p => parseInt(p) || 0);
    if (t === "major") return `${maj + 1}.0.0`;
    if (t === "minor") return `${maj}.${min + 1}.0`;
    return `${maj}.${min}.${pat + 1}`;
  };

  const BUMP_DESC: Record<string, string> = {
    patch: "Bug fixes, label changes, optional field adjustments",
    minor: "New stages, new steps, new optional fields, new connectors",
    major: "Breaking changes — SLA changes, required field additions, stage deletions",
  };

  const handleBump = async () => {
    setLoading(true); setErr(null);
    const r = await fetch(`/api/v1/apps/case-types/${caseType.id}/bump-version`, {
      method: "POST", headers: {
        "Content-Type": "application/json",
        ...(localStorage.getItem("helix_token") ? { Authorization: `Bearer ${localStorage.getItem("helix_token")}` } : {}),
      },
      body: JSON.stringify({ bump_type: bumpType, changelog }),
    });
    if (r.ok) {
      const data = await r.json();
      onBumped(data.id, data.version);
    } else {
      const e = await r.json().catch(() => ({}));
      setErr(e.detail || "Failed");
    }
    setLoading(false);
  };

  const overlay: React.CSSProperties = { position: "fixed", inset: 0, background: "#00000066", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center" };
  const modal:   React.CSSProperties = { background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 12, padding: 28, width: 480, maxWidth: "90vw" };

  return (
    <div style={overlay} onClick={e => e.target === e.currentTarget && onClose()}>
      <div style={modal}>
        <div style={{ fontSize: 17, fontWeight: 700, marginBottom: 4 }}>Bump Version</div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 20 }}>
          Current: <strong>v{caseType.version}</strong> → New: <strong>v{bumpedVersion(caseType.version, bumpType)}</strong>
        </div>

        {(["patch","minor","major"] as const).map(t => (
          <label key={t} onClick={() => setBumpType(t)} style={{ display: "flex", gap: 12, padding: "10px 14px", borderRadius: 8, cursor: "pointer", marginBottom: 8, border: `2px solid ${bumpType === t ? "var(--accent)" : "var(--border-subtle)"}`, background: bumpType === t ? "color-mix(in srgb, var(--accent) 6%, transparent)" : "var(--bg-elevated)" }}>
            <input type="radio" checked={bumpType === t} onChange={() => setBumpType(t)} style={{ marginTop: 2 }} />
            <div>
              <div style={{ fontWeight: 700, fontSize: 13 }}>{t.charAt(0).toUpperCase() + t.slice(1)} — v{bumpedVersion(caseType.version, t)}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{BUMP_DESC[t]}</div>
            </div>
          </label>
        ))}

        <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 4, marginTop: 16 }}>Changelog (optional)</div>
        <textarea value={changelog} onChange={e => setChangelog(e.target.value)} rows={2} placeholder="What changed in this version?"
          style={{ width: "100%", padding: "8px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box", resize: "vertical", marginBottom: 16 }} />

        {err && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 12 }}>{err}</div>}
        <div style={{ display: "flex", gap: 10 }}>
          <Button onClick={handleBump} disabled={loading}>{loading ? "Creating…" : `Create v${bumpedVersion(caseType.version, bumpType)}`}</Button>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
        </div>
      </div>
    </div>
  );
}
import StagePipeline from "./StagePipeline";
import PropertyPanel from "./PropertyPanel";
import { FormBuilder } from "../form-builder";
import type { FormDefinition } from "../form-builder";
import type { StageDef, StepDef, SLAPolicyDef } from "./StagePipeline";

/* ═══════════════════════════════════════════════════════════════════
   CaseTypeEditor — visual case lifecycle designer
   
   Composes the horizontal stage pipeline with a right-side property
   panel.  All edits are local state until the user clicks Save,
   which writes the full definition back to the API.
   ═══════════════════════════════════════════════════════════════════ */

interface CaseTypeEditorProps {
  caseTypeId: string;
  onBack: () => void;
  readOnly?: boolean;
  onOpenVersion?: (id: string, readOnly: boolean) => void;
}

export default function CaseTypeEditor({ caseTypeId, onBack, readOnly = false, onOpenVersion }: CaseTypeEditorProps) {
  const [searchParams] = useSearchParams();
  const branchId = searchParams.get("branch");

  const { data: caseType, loading } = useApi(() => getCaseType(caseTypeId), [caseTypeId]);
  const branchMode = useBranchMode(branchId);
  const myGroups = useCurrentUserGroups();

  // A branch is only valid for this editor if it actually belongs to this case type.
  // If the user navigates to a different case type while the ?branch= param is still in
  // the URL, we must not show that branch's banner or load its snapshot here.
  const branchBelongsHere =
    branchMode.branch !== null &&
    branchMode.branch.artifact_id === caseTypeId;

  // Local editable state — initialised from the case type definition
  const [stages, setStages]           = useState<StageDef[]>([]);
  const [slaPolicies, setSlaPolicies] = useState<SLAPolicyDef[]>([]);
  const [showBumpModal, setShowBumpModal] = useState(false);
  const [exportMsg, setExportMsg]     = useState<string | null>(null);
  const [selectedStageId, setSelectedStageId] = useState<string | null>(null);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formEditorOpen, setFormEditorOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [intakeOpen, setIntakeOpen] = useState(false);
  const [variablesOpen, setVariablesOpen] = useState(false);
  const [historyTab, setHistoryTab]   = useState<"commits" | "migrations">("commits");
  const [initialized, setInitialized] = useState(false);
  const [restorePrefilledMsg, setRestorePrefilledMsg] = useState<string | undefined>(undefined);

  // Reset when navigating to a different case type (prop change without unmount)
  React.useEffect(() => {
    setInitialized(false);
    setStages([]);
    setSlaPolicies([]);
    setSelectedStageId(null);
    setSelectedStepId(null);
    setDirty(false);
  }, [caseTypeId]);

  // Initialise from API response
  React.useEffect(() => {
    if (!caseType || initialized) return;
    // In branch mode (and branch belongs here), wait for it to load first
    if (branchId && branchBelongsHere === false && !branchMode.error) {
      // branch loaded but belongs to a different artifact — fall through to case type data
    } else if (branchId && branchMode.branch === null && !branchMode.error) {
      return; // still loading the branch
    }
    // Use branch snapshot only if it belongs to this case type AND is not read-only
    // (merged branches have already been applied to main; load from main instead)
    const useSnapshot = branchBelongsHere && !branchMode.isReadOnly;
    const branchDef = useSnapshot ? branchMode.branch?.content_snapshot?.definition_json : undefined;
    const def = branchDef ?? (caseType as any).definition_json ?? {};
    setStages(def.stages || []);
    setSlaPolicies(def.sla_policies || []);
    setInitialized(true);
  }, [caseType, initialized, branchId, branchBelongsHere, branchMode.branch, branchMode.error, branchMode.isReadOnly]);

  // ── Stage operations ───────────────────────────────────────────

  const addStage = useCallback(() => {
    const id = `stage-${Date.now()}`;
    const newStage: StageDef = {
      id,
      name: `Stage ${stages.length + 1}`,
      stage_type: "linear",
      order: stages.length,
      steps: [],
    };
    setStages([...stages, newStage]);
    setSelectedStageId(id);
    setSelectedStepId(null);
    setDirty(true);
  }, [stages]);

  const updateStage = useCallback((updated: StageDef) => {
    setStages((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
    setDirty(true);
  }, []);

  const deleteStage = useCallback((stageId: string) => {
    if (!confirm("Delete this stage and all its steps?")) return;
    setStages((prev) => prev.filter((s) => s.id !== stageId).map((s, i) => ({ ...s, order: i })));
    if (selectedStageId === stageId) { setSelectedStageId(null); setSelectedStepId(null); }
    setDirty(true);
  }, [selectedStageId]);

  const reorderStage = useCallback((stageId: string, direction: "left" | "right") => {
    setStages((prev) => {
      const sorted = [...prev].sort((a, b) => a.order - b.order);
      const idx = sorted.findIndex((s) => s.id === stageId);
      if (idx < 0) return prev;
      const swapIdx = direction === "left" ? idx - 1 : idx + 1;
      if (swapIdx < 0 || swapIdx >= sorted.length) return prev;
      [sorted[idx], sorted[swapIdx]] = [sorted[swapIdx], sorted[idx]];
      return sorted.map((s, i) => ({ ...s, order: i }));
    });
    setDirty(true);
  }, []);

  // ── Step operations ────────────────────────────────────────────

  const addStep = useCallback((stageId: string) => {
    const stepId = `step-${Date.now()}`;
    const newStep: StepDef = {
      id: stepId,
      name: "New Step",
      step_type: "user_task",
      bpmn_element_id: `task_${stepId}`,
      required: true,
      assignment: { strategy: "queue_based" },
    };
    setStages((prev) => prev.map((s) =>
      s.id === stageId ? { ...s, steps: [...s.steps, newStep] } : s
    ));
    setSelectedStageId(stageId);
    setSelectedStepId(stepId);
    setDirty(true);
  }, []);

  const updateStep = useCallback((stageId: string, updated: StepDef) => {
    setStages((prev) => prev.map((s) =>
      s.id === stageId
        ? { ...s, steps: s.steps.map((st) => (st.id === updated.id ? updated : st)) }
        : s
    ));
    setDirty(true);
  }, []);

  const deleteStep = useCallback((stageId: string, stepId: string) => {
    setStages((prev) => prev.map((s) =>
      s.id === stageId ? { ...s, steps: s.steps.filter((st) => st.id !== stepId) } : s
    ));
    if (selectedStepId === stepId) setSelectedStepId(null);
    setDirty(true);
  }, [selectedStepId]);

  // ── Commit (replaces Save) ─────────────────────────────────────

  const { commitOpen, commitSaving, requestCommit, handleCommit, cancelCommit, pendingChanges } =
    useCommit("case_type", caseTypeId, (caseType as any)?.name ?? "Case Type");

  const doSave = async () => {
    if (!caseType) return;
    const definition_json = {
      ...((caseType as any).definition_json || {}),
      stages,
      sla_policies: slaPolicies,
    };
    if (branchMode.isBranchMode && branchBelongsHere) {
      await branchMode.patchContent({ definition_json });
    } else {
      await fetch(`/api/v1/case-types/${caseTypeId}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("helix_token") ?? ""}`,
        },
        body: JSON.stringify({ definition_json }),
      });
    }
    setDirty(false);
  };

  const handleSave = () => {
    if (!dirty || !caseType) return;
    setRestorePrefilledMsg(undefined);
    if (branchMode.isBranchMode && branchBelongsHere) {
      doSave();
      return;
    }
    const beforeDef = (caseType as any).definition_json || {};
    requestCommit(doSave, {
      before: { stages: beforeDef.stages || [], sla_policies: beforeDef.sla_policies || [] },
      after:  { stages, sla_policies: slaPolicies },
    });
  };

  const handleRestoreRequest = useCallback((commit: CommitRecord) => {
    const after = (commit.diff_snapshot as any)?.after;
    if (!after) return;
    // Load old state into editor — marks it as dirty without saving
    const restoredStages = after.stages ?? [];
    const restoredSLAs   = after.sla_policies ?? [];
    const currentDef = (caseType as any)?.definition_json || {};
    setStages(restoredStages);
    setSlaPolicies(restoredSLAs);
    setDirty(true);
    // Pre-fill the commit message so the audit trail explains who restored what
    const when = new Date(commit.committed_at).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
    const msg = `Restored to state from ${when} — "${commit.commit_message}"`;
    setRestorePrefilledMsg(msg);
    // Open CommitModal immediately with the current state as "before" and restored as "after"
    requestCommit(async () => {
      const definition_json = {
        ...(currentDef),
        stages: restoredStages,
        sla_policies: restoredSLAs,
      };
      await fetch(`/api/v1/case-types/${caseTypeId}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("helix_token") ?? ""}`,
        },
        body: JSON.stringify({ definition_json }),
      });
      setDirty(false);
    }, {
      before: { stages: currentDef.stages || [], sla_policies: currentDef.sla_policies || [] },
      after:  { stages: restoredStages, sla_policies: restoredSLAs },
    });
  }, [caseType, caseTypeId, requestCommit]);

  // ── Resolve selected items ─────────────────────────────────────

  const selectedStage = stages.find((s) => s.id === selectedStageId) || null;
  const selectedStep = selectedStage?.steps.find((st) => st.id === selectedStepId) || null;

  // ── Render ─────────────────────────────────────────────────────

  if (loading || !caseType) {
    return (
      <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}>
        <Spinner size={28} />
      </div>
    );
  }

  return (
    <div style={{ display: "flex", height: "100%", overflow: "hidden" }}>
      {/* Main area */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", minWidth: 0 }}>
        {/* Read-only banner */}
        {readOnly && (
          <div style={{
            padding: "10px var(--space-xl)",
            background: "color-mix(in srgb, #f59e0b 10%, var(--bg-panel))",
            borderBottom: "1px solid color-mix(in srgb, #f59e0b 30%, transparent)",
            display: "flex", alignItems: "center", justifyContent: "space-between",
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span style={{ fontSize: 13 }}>🔒</span>
              <div>
                <span style={{ fontSize: 12, fontWeight: 700, color: "#d97706" }}>VIEW ONLY</span>
                <span style={{ fontSize: 12, color: "var(--text-secondary)", marginLeft: 8 }}>
                  v{(caseType as any).version} is not the latest version — editing is disabled.
                </span>
              </div>
            </div>
            <Button size="sm" onClick={onBack}>
              ← Back to list
            </Button>
          </div>
        )}

        {/* Branch mode banner — only when the branch belongs to this case type */}
        {branchMode.isBranchMode && branchBelongsHere && (
          <BranchModeBanner
            branch={branchMode.branch}
            saving={branchMode.saving}
            error={branchMode.error}
            accessGroupId={myGroups[0]}
            onSubmitForReview={branchMode.submitForReview}
            onRecall={branchMode.recall}
          />
        )}

        {/* Toolbar */}
        <div style={{
          padding: "var(--space-md) var(--space-xl)",
          borderBottom: "1px solid var(--border-subtle)",
          display: "flex", justifyContent: "space-between", alignItems: "center",
          background: "var(--bg-panel)",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-md)" }}>
            <button onClick={onBack} style={{
              background: "transparent", border: "none", color: "var(--text-secondary)",
              cursor: "pointer", fontSize: 13, display: "flex", alignItems: "center", gap: 6,
              fontFamily: "var(--font-body)",
            }}>
              ← Back
            </button>
            <div style={{ width: 1, height: 20, background: "var(--border-subtle)" }} />
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <div style={{ fontSize: 16, fontWeight: 600, color: "var(--text-primary)", fontFamily: "var(--font-display)" }}>
                  {(caseType as any).name}
                </div>
                {readOnly && (
                  <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 4, background: "#fef3c7", color: "#d97706", fontFamily: "var(--font-mono)", fontWeight: 700 }}>
                    READ ONLY
                  </span>
                )}
              </div>
              <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
                v{(caseType as any).version} · {stages.length} stages · {stages.reduce((n, s) => n + s.steps.length, 0)} steps
              </div>
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
            {!readOnly && dirty && (
              <span style={{ fontSize: 11, color: "var(--status-running)", fontFamily: "var(--font-mono)" }}>
                ● unsaved changes
              </span>
            )}
            {exportMsg && <span style={{ fontSize: 11, color: "#22c55e" }}>{exportMsg}</span>}
            <Button variant="secondary" size="sm" onClick={async () => {
              const r = await fetch(`/api/v1/apps/package/case-type/${caseTypeId}`, {
                method: "POST",
                headers: localStorage.getItem("helix_token") ? { Authorization: `Bearer ${localStorage.getItem("helix_token")}` } : {},
              });
              if (r.ok) {
                const blob = new Blob([JSON.stringify(await r.json(), null, 2)], { type: "application/json" });
                const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
                a.download = `${(caseType as any).name}-v${(caseType as any).version}.bundle.json`;
                a.click(); setExportMsg("Exported ✓");
                setTimeout(() => setExportMsg(null), 3000);
              }
            }}>⬇ Export</Button>
            {!readOnly && !(branchMode.isBranchMode && branchBelongsHere) && (
              <Button variant="secondary" size="sm" onClick={() => setShowBumpModal(true)}>
                🔖 Bump Version
              </Button>
            )}
            <Button variant="secondary" size="sm" onClick={() => setFormEditorOpen(!formEditorOpen)}>
              {formEditorOpen ? "← Pipeline" : "📋 Forms"}
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setVariablesOpen(!variablesOpen)}>
              {variablesOpen ? "← Pipeline" : "🔣 Variables"}
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setIntakeOpen(!intakeOpen)}>
              {intakeOpen ? "← Close" : "⚡ Intake"}
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setHistoryOpen(!historyOpen)}>
              {historyOpen ? "← Close" : "🕐 History"}
            </Button>
            {!readOnly && !(branchBelongsHere && branchMode.isLocked) && (
              <Button variant="secondary" size="sm" onClick={() => {
                const snapDef = branchBelongsHere ? branchMode.branch?.content_snapshot?.definition_json : undefined;
                const fallbackDef = (caseType as any).definition_json || {};
                const def = snapDef ?? fallbackDef;
                setStages(def.stages || []);
                setSlaPolicies(def.sla_policies || []);
                setDirty(false);
              }}>
                Reset
              </Button>
            )}
            {!readOnly && (
              <Button
                size="sm"
                onClick={handleSave}
                disabled={!dirty || commitSaving || branchMode.saving
                  || (branchBelongsHere && branchMode.isLocked)
                  || (branchBelongsHere && branchMode.isReadOnly)}
              >
                {(commitSaving || branchMode.saving) ? "Saving…"
                  : (branchBelongsHere && branchMode.isLocked) ? "Locked"
                  : (branchBelongsHere && branchMode.isReadOnly) ? "Read Only"
                  : (branchBelongsHere && branchMode.isBranchMode) ? "Save to Branch"
                  : "Commit"}
              </Button>
            )}
          </div>
          {showBumpModal && caseType && (
            <VersionBumpModal
              caseType={caseType}
              onClose={() => setShowBumpModal(false)}
              onBumped={(newId, newVersion) => {
                setShowBumpModal(false);
                if (onOpenVersion) {
                  onOpenVersion(newId, false);
                } else {
                  onBack();
                }
              }}
            />
          )}
        </div>

        {/* Stats bar */}
        <div style={{
          padding: "var(--space-sm) var(--space-xl)",
          display: "flex", gap: "var(--space-lg)",
          borderBottom: "1px solid var(--border-subtle)",
          background: "var(--bg-card)",
        }}>
          <MiniStat label="Stages" value={stages.length} />
          <MiniStat label="Steps" value={stages.reduce((n, s) => n + s.steps.length, 0)} />
          <MiniStat label="SLA Policies" value={slaPolicies.length} />
          <MiniStat label="With Assignments" value={stages.reduce((n, s) => n + s.steps.filter((st) => st.assignment).length, 0)} />
        </div>

        {/* Pipeline or Form Editor or Intake */}
        <div style={{ flex: 1, overflow: "auto", padding: "var(--space-lg) var(--space-xl)" }}>
          {intakeOpen ? (
            <IntakeTriggerPanel caseType={caseType} caseTypeId={caseTypeId} />
          ) : variablesOpen ? (
            <VariablesPanel caseTypeId={caseTypeId} readOnly={readOnly} />
          ) : formEditorOpen ? (
            <FormEditorPanel caseTypeId={caseTypeId} />
          ) : (<>
          {stages.length === 0 && !readOnly ? (
            <div style={{
              display: "flex", flexDirection: "column", alignItems: "center",
              justifyContent: "center", height: "100%", gap: "var(--space-md)",
            }}>
              <div style={{ fontSize: 40, opacity: 0.3 }}>📋</div>
              <div style={{ fontSize: 15, fontWeight: 500, color: "var(--text-secondary)" }}>
                No stages defined yet
              </div>
              <div style={{ fontSize: 12, color: "var(--text-muted)", maxWidth: 320 }}>
                Add stages to define the case lifecycle. Each stage contains steps that represent units of work.
              </div>
              <Button onClick={addStage}>+ Add First Stage</Button>
            </div>
          ) : (
            <StagePipeline
              stages={stages}
              sla_policies={slaPolicies}
              selectedStageId={selectedStageId}
              selectedStepId={selectedStepId}
              readOnly={readOnly}
              onSelectStage={(id) => { setSelectedStageId(id); setSelectedStepId(null); }}
              onSelectStep={(stageId, stepId) => {
                setSelectedStageId(stageId);
                setSelectedStepId(stepId);
              }}
              onAddStage={readOnly ? () => {} : addStage}
              onAddStep={readOnly ? () => {} : addStep}
              onReorderStage={readOnly ? () => {} : reorderStage}
              onDeleteStage={readOnly ? () => {} : deleteStage}
              onDeleteStep={readOnly ? () => {} : deleteStep}
            />
          )}

          {/* SLA Policies summary */}
          {slaPolicies.length > 0 && (
            <div style={{ marginTop: "var(--space-xl)" }}>
              <div style={{
                fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase",
                letterSpacing: "0.08em", fontFamily: "var(--font-mono)", marginBottom: "var(--space-sm)",
              }}>
                SLA Policies ({slaPolicies.length})
              </div>
              <div style={{ display: "flex", gap: "var(--space-sm)", flexWrap: "wrap" }}>
                {slaPolicies.map((sla) => (
                  <div key={sla.id} style={{
                    padding: "8px 12px", background: "var(--bg-card)", borderRadius: "var(--radius-sm)",
                    border: "1px solid var(--border-subtle)", fontSize: 11,
                  }}>
                    <div style={{ fontWeight: 500, color: "var(--text-primary)" }}>{sla.name}</div>
                    <div style={{ fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 2 }}>
                      ⏱ goal: {sla.goal_duration} · deadline: {sla.deadline_duration}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* SLA Policies summary - already exists above */}
          </>
          )}
        </div>
      </div>

      {/* Property panel */}
      <PropertyPanel
        selectedStage={selectedStep ? selectedStage : selectedStage}
        selectedStep={selectedStep}
        sla_policies={slaPolicies}
        readOnly={readOnly}
        onUpdateStage={readOnly ? () => {} : updateStage}
        onUpdateStep={readOnly ? () => {} : updateStep}
        onUpdateSLAPolicies={readOnly ? () => {} : setSlaPolicies}
        onClose={() => { setSelectedStageId(null); setSelectedStepId(null); }}
      />
      <CommitModal
        open={commitOpen}
        saving={commitSaving}
        componentType="case_type"
        componentName={(caseType as any)?.name ?? "Case Type"}
        pendingChanges={pendingChanges}
        prefilledMessage={restorePrefilledMsg}
        onCommit={handleCommit}
        onCancel={() => { cancelCommit(); setRestorePrefilledMsg(undefined); }}
      />

      {/* History slide-out panel */}
      {historyOpen && (
        <div style={{
          width: 400, flexShrink: 0, borderLeft: "1px solid var(--border-subtle)",
          background: "var(--bg-panel)", display: "flex", flexDirection: "column",
          overflow: "hidden",
        }}>
          {/* Header */}
          <div style={{
            padding: "14px 16px 0", borderBottom: "1px solid var(--border-subtle)",
            flexShrink: 0,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 10 }}>
              <div>
                <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)" }}>History</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  {(caseType as any)?.name}
                </div>
              </div>
              <Button variant="ghost" size="sm" onClick={() => setHistoryOpen(false)}>✕</Button>
            </div>
            {/* Tab switcher */}
            <div style={{ display: "flex", gap: 2 }}>
              {(["commits", "migrations"] as const).map(tab => (
                <button key={tab} onClick={() => setHistoryTab(tab)} style={{
                  padding: "6px 14px", border: "none", cursor: "pointer", fontSize: 12,
                  fontWeight: historyTab === tab ? 700 : 400,
                  borderBottom: `2px solid ${historyTab === tab ? "var(--accent)" : "transparent"}`,
                  background: "transparent",
                  color: historyTab === tab ? "var(--text-primary)" : "var(--text-muted)",
                  transition: "all 0.15s",
                }}>
                  {tab === "commits" ? "Commit History" : "Import History"}
                </button>
              ))}
            </div>
          </div>

          {/* Tab content */}
          <div style={{ flex: 1, overflow: "auto", padding: "12px 16px" }}>
            {historyTab === "commits" ? (
              <CommitHistory
                componentType="case_type"
                componentId={caseTypeId}
                compact
                onRestoreRequest={readOnly ? undefined : handleRestoreRequest}
              />
            ) : (
              <MigrationHistoryPanel caseTypeId={caseTypeId} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}


function FormEditorPanel({ caseTypeId }: { caseTypeId: string }) {
  const { data: formsData, refetch } = useApi(listForms);
  const forms = formsData?.items ?? [];
  const [selectedFormId, setSelectedFormId] = React.useState<string | null>(null);
  const [creating, setCreating] = React.useState(false);
  const [newName, setNewName] = React.useState("");
  const [editDef, setEditDef] = React.useState<FormDefinition | null>(null);
  const [saving, setSaving] = React.useState(false);

  const selectedForm = forms.find((f: any) => f.id === selectedFormId);

  React.useEffect(() => {
    if (selectedForm && !editDef) {
      setEditDef(selectedForm.definition_json || { sections: [] });
    }
  }, [selectedFormId]);

  const handleCreate = async () => {
    if (!newName) return;
    try {
      const form = await createForm({
        name: newName,
        version: "1.0.0",
        definition_json: { sections: [] },
      });
      setCreating(false);
      setNewName("");
      refetch();
      setSelectedFormId(form.id);
      setEditDef({ sections: [] });
    } catch (e) {
      console.error("Failed to create form:", e);
    }
  };

  const handleSave = async () => {
    if (!selectedFormId || !editDef) return;
    setSaving(true);
    try {
      await updateFormApi(selectedFormId, { definition_json: editDef as any });
      refetch();
    } catch (e) {
      console.error("Failed to save form:", e);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)", marginBottom: "var(--space-lg)" }}>
        <select
          value={selectedFormId || ""}
          onChange={e => { setSelectedFormId(e.target.value || null); setEditDef(null); }}
          style={{
            flex: 1, padding: "8px 12px", fontSize: 13, fontFamily: "var(--font-body)",
            background: "var(--bg-input)", border: "1px solid var(--border-default)",
            borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
          }}
        >
          <option value="">Select a form to edit…</option>
          {forms.map((f: any) => (
            <option key={f.id} value={f.id}>{f.name} v{f.version}</option>
          ))}
        </select>
        <Button size="sm" onClick={() => setCreating(true)}>+ New Form</Button>
        {selectedFormId && editDef && (
          <Button size="sm" onClick={handleSave} disabled={saving}>
            {saving ? "Committing…" : "Commit Form"}
          </Button>
        )}
      </div>

      {creating && (
        <div style={{
          display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-md)",
          padding: "var(--space-sm)", background: "var(--bg-card)",
          borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
        }}>
          <input
            autoFocus value={newName} onChange={e => setNewName(e.target.value)}
            placeholder="Form name…"
            onKeyDown={e => e.key === "Enter" && handleCreate()}
            style={{
              flex: 1, padding: "6px 10px", fontSize: 12, background: "var(--bg-input)",
              border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
              color: "var(--text-primary)", outline: "none",
            }}
          />
          <Button size="sm" onClick={handleCreate}>Create</Button>
          <Button size="sm" variant="ghost" onClick={() => { setCreating(false); setNewName(""); }}>Cancel</Button>
        </div>
      )}

      {selectedFormId && editDef ? (
        <div style={{ flex: 1, overflow: "auto" }}>
          <FormBuilder definition={editDef} onChange={setEditDef} />
        </div>
      ) : (
        <div style={{
          flex: 1, display: "flex", flexDirection: "column",
          alignItems: "center", justifyContent: "center", color: "var(--text-muted)",
        }}>
          <div style={{ fontSize: 40, opacity: 0.3, marginBottom: "var(--space-md)" }}>📋</div>
          <div style={{ fontSize: 14, fontWeight: 500, color: "var(--text-secondary)" }}>
            Select or create a form to edit
          </div>
          <div style={{ fontSize: 12, marginTop: 4, maxWidth: 320 }}>
            Forms can be linked to steps in the pipeline. When a user completes a step, the form will be rendered for data entry.
          </div>
        </div>
      )}
    </div>
  );
}
function MiniStat({ label, value }: { label: string; value: number }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <span style={{ fontSize: 16, fontWeight: 600, color: "var(--text-primary)", fontFamily: "var(--font-display)" }}>
        {value}
      </span>
      <span style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase" }}>
        {label}
      </span>
    </div>
  );
}


// ── Migration History Panel ───────────────────────────────────────────────────

/**
 * Derive a human-readable display name from a migration record.
 * Priority: email field (which now stores username), user_id, fallback.
 * Strips "@domain" suffixes left over from older records.
 */
function _displayName(record: any): string {
  const raw = record.imported_by_email || record.imported_by_user_id || "";
  if (!raw) return "system";
  // If it looks like a UUID (no spaces, 36 chars with dashes), show "User" + first 8
  const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (uuidPattern.test(raw)) return `User ${raw.slice(0, 8)}`;
  // Strip @domain suffix from legacy UUID@helix.local entries
  const atIdx = raw.indexOf("@");
  if (atIdx > 0) {
    const localPart = raw.slice(0, atIdx);
    if (uuidPattern.test(localPart)) return `User ${localPart.slice(0, 8)}`;
    return localPart;  // e.g. "admin@company.com" → "admin"
  }
  return raw;  // plain username, return as-is
}

const PLATFORM_ICONS: Record<string, string> = {
  camunda:        "⚙️",
  flowable:       "🌊",
  pega:           "🔷",
  servicenow:     "❄️",
  appian:         "🔵",
  power_automate: "⚡",
  salesforce:     "☁️",
  nintex:         "🔶",
  jbpm:           "🔴",
  ibm:            "🔵",
  oracle:         "🔴",
  bizagi:         "🟣",
};

function MigrationHistoryPanel({ caseTypeId }: { caseTypeId: string }) {
  const [records, setRecords] = React.useState<any[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError]     = React.useState<string | null>(null);

  React.useEffect(() => {
    const token = localStorage.getItem("helix_token");
    fetch(`/api/v1/case-types/${caseTypeId}/migration-history`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => { setRecords(data); setLoading(false); })
      .catch(() => { setError("Failed to load migration history"); setLoading(false); });
  }, [caseTypeId]);

  if (loading) return (
    <div style={{ textAlign: "center", padding: 32, color: "var(--text-muted)", fontSize: 13 }}>
      Loading…
    </div>
  );

  if (error) return (
    <div style={{ color: "#ef4444", fontSize: 12, padding: 16 }}>{error}</div>
  );

  if (records.length === 0) return (
    <div style={{ textAlign: "center", padding: 32 }}>
      <div style={{ fontSize: 32, marginBottom: 8 }}>📦</div>
      <div style={{ fontSize: 13, color: "var(--text-muted)" }}>No migration imports yet</div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
        Imports via HxMigrate will appear here
      </div>
    </div>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {records.map((r: any) => {
        const platform = (r.source_platform || "").toLowerCase();
        const icon     = PLATFORM_ICONS[platform] || "📥";
        const label    = platform.replace(/_/g, " ").replace(/\b\w/g, (c: string) => c.toUpperCase());
        const date     = new Date(r.imported_at);
        const dateStr  = date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
        const timeStr  = date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });

        return (
          <div key={r.id} style={{
            border: "1px solid var(--border-subtle)",
            borderRadius: 10,
            background: "var(--bg-elevated)",
            padding: "12px 14px",
            fontSize: 12,
          }}>
            {/* Provider badge + date */}
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 8 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontSize: 16 }}>{icon}</span>
                <span style={{
                  padding: "2px 8px", borderRadius: 20, fontSize: 11, fontWeight: 700,
                  background: "color-mix(in srgb, var(--accent) 12%, transparent)",
                  color: "var(--accent)",
                }}>
                  {label}
                </span>
              </div>
              <div style={{ textAlign: "right", color: "var(--text-muted)", fontSize: 11 }}>
                <div>{dateStr}</div>
                <div>{timeStr}</div>
              </div>
            </div>

            {/* Source file */}
            {r.source_filename && (
              <div style={{ color: "var(--text-secondary)", marginBottom: 6, fontFamily: "var(--font-mono)", fontSize: 11 }}>
                📄 {r.source_filename}
              </div>
            )}

            {/* Counts row */}
            <div style={{ display: "flex", gap: 12, marginBottom: 8, flexWrap: "wrap" }}>
              {[
                { label: "Stages", v: r.stages_count },
                { label: "Steps",  v: r.steps_count },
                { label: "Forms",  v: r.forms_count },
                { label: "Rules",  v: r.rules_count },
                { label: "SLAs",   v: r.slas_count },
              ].filter(x => x.v > 0).map(({ label: lbl, v }) => (
                <div key={lbl} style={{ display: "flex", flexDirection: "column", alignItems: "center", minWidth: 36 }}>
                  <span style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)" }}>{v}</span>
                  <span style={{ fontSize: 9, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.05em" }}>{lbl}</span>
                </div>
              ))}
            </div>

            {/* Imported by */}
            <div style={{
              display: "flex", alignItems: "center", gap: 6,
              padding: "6px 10px", borderRadius: 6,
              background: "var(--bg-panel)", fontSize: 11,
              color: "var(--text-secondary)",
            }}>
              <span style={{ fontSize: 13 }}>👤</span>
              <span>Imported by <strong>{_displayName(r)}</strong></span>
            </div>

            {/* Run ID link */}
            {r.run_id && (
              <div style={{ marginTop: 6, fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                Run: {r.run_id.slice(0, 8)}…
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}


// ── Intake Trigger Panel ───────────────────────────────────────────────────────

function IntakeTriggerPanel({ caseType, caseTypeId }: { caseType: any; caseTypeId: string }) {
  const authHdr = (): Record<string, string> => {
    const t = localStorage.getItem("helix_token");
    const h: Record<string, string> = { "Content-Type": "application/json" };
    if (t) h["Authorization"] = `Bearer ${t}`;
    return h;
  };

  const [trigger,      setTrigger]      = React.useState<string>((caseType as any)?.intake_trigger ?? "manual");
  const [connectorId,  setConnectorId]  = React.useState<string>((caseType as any)?.trigger_connector_id ?? "");
  const [procDefId,    setProcDefId]    = React.useState<string>((caseType as any)?.process_definition_id ?? "");
  const [conditions,   setConditions]   = React.useState<string>(
    JSON.stringify((caseType as any)?.filter_conditions ?? { logic: "and", rules: [] }, null, 2)
  );
  const [fieldMapping, setFieldMapping] = React.useState<string>(
    JSON.stringify((caseType as any)?.field_mapping ?? {}, null, 2)
  );
  const [connectors,   setConnectors]   = React.useState<any[]>([]);
  const [processDefs,  setProcessDefs]  = React.useState<any[]>([]);
  const [saving,       setSaving]       = React.useState(false);
  const [msg,          setMsg]          = React.useState<string | null>(null);

  React.useEffect(() => {
    fetch("/api/v1/hxbridge/connectors?enabled=true", { headers: authHdr() })
      .then(r => r.ok ? r.json() : { connectors: [] })
      .then(d => setConnectors(d.connectors ?? []));
    fetch("/api/v1/fusion/definitions", { headers: authHdr() })
      .then(r => r.ok ? r.json() : [])
      .then(d => setProcessDefs(Array.isArray(d) ? d : []));
  }, []);

  const save = async () => {
    setSaving(true); setMsg(null);
    try {
      let fc = {}, fm = {};
      try { fc = JSON.parse(conditions); } catch { throw new Error("Filter conditions: invalid JSON"); }
      try { fm = JSON.parse(fieldMapping); } catch { throw new Error("Field mapping: invalid JSON"); }
      const r = await fetch(`/api/v1/case-types/${caseTypeId}`, {
        method: "PATCH",
        headers: authHdr(),
        body: JSON.stringify({
          intake_trigger: trigger,
          trigger_connector_id: connectorId || null,
          filter_conditions: fc,
          field_mapping: fm,
          process_definition_id: procDefId || null,
        }),
      });
      if (!r.ok) throw new Error((await r.json()).detail ?? "Save failed");
      setMsg("Saved ✓");
    } catch (e: any) { setMsg(`Error: ${e.message}`); }
    finally { setSaving(false); }
  };

  const webhookUrl = `${window.location.protocol}//${window.location.hostname}:8200/api/v1/intake/webhook/${caseTypeId}`;

  const inp: React.CSSProperties = { width: "100%", padding: "7px 10px", fontSize: 13, background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", boxSizing: "border-box" };
  const lbl: React.CSSProperties = { fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 4, display: "block", textTransform: "uppercase" as const, letterSpacing: "0.05em" };
  const ta: React.CSSProperties = { ...inp, fontFamily: "var(--font-mono)", fontSize: 11, resize: "vertical" as const };

  return (
    <div style={{ maxWidth: 700 }}>
      <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 4 }}>⚡ Intake Trigger</div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 20 }}>
        Configure how new case instances are created — manually by a user or automatically from an external source.
      </div>

      {/* Trigger type */}
      <div style={{ marginBottom: 16 }}>
        <label style={lbl}>Trigger Type</label>
        <div style={{ display: "flex", gap: 8 }}>
          {[
            { v: "manual",   label: "👤 Manual",   desc: "User creates case via Studio or Portal" },
            { v: "webhook",  label: "🔗 Webhook",  desc: "External system POSTs a payload" },
            { v: "schedule", label: "⏰ Schedule",  desc: "Cron-based batch intake (future)" },
          ].map(opt => (
            <div key={opt.v} onClick={() => setTrigger(opt.v)} style={{
              flex: 1, padding: "10px 14px", borderRadius: 8, cursor: "pointer",
              border: `2px solid ${trigger === opt.v ? "var(--accent)" : "var(--border-default)"}`,
              background: trigger === opt.v ? "color-mix(in srgb, var(--accent) 6%, transparent)" : "var(--bg-card)",
            }}>
              <div style={{ fontWeight: 700, fontSize: 13 }}>{opt.label}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>{opt.desc}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Webhook config */}
      {trigger === "webhook" && (<>
        <div style={{ marginBottom: 14 }}>
          <label style={lbl}>Inbound Connector / Webhook Source</label>
          <select value={connectorId} onChange={e => setConnectorId(e.target.value)} style={{ ...inp }}>
            <option value="">— any source (no connector filter) —</option>
            {connectors.map((c: any) => <option key={c.id} value={c.id}>{c.name} ({c.connector_type})</option>)}
          </select>
        </div>

        <div style={{ marginBottom: 14, padding: "10px 14px", background: "var(--bg-elevated)", borderRadius: 6, fontFamily: "var(--font-mono)", fontSize: 11 }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 4 }}>WEBHOOK ENDPOINT</div>
          <div style={{ wordBreak: "break-all", color: "var(--accent)" }}>{webhookUrl}</div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>POST JSON payload to this URL to trigger case creation</div>
        </div>

        <div style={{ marginBottom: 14 }}>
          <label style={lbl}>Filter Conditions (JSON)</label>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
            Define rules that the payload must pass before a case is created.
            Logic: "and" | "or". Operators: eq, neq, gt, gte, lt, lte, contains, regex, exists.
          </div>
          <textarea rows={8} value={conditions} onChange={e => setConditions(e.target.value)} style={ta}
            placeholder={'{\n  "logic": "and",\n  "rules": [\n    {"field": "event.type", "operator": "eq", "value": "claim_submitted"},\n    {"field": "data.amount", "operator": "gte", "value": 100}\n  ]\n}'} />
        </div>

        <div style={{ marginBottom: 14 }}>
          <label style={lbl}>Field Mapping (JSON)</label>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
            Map incoming payload fields to case data fields. Use dot-notation for nested paths.
          </div>
          <textarea rows={6} value={fieldMapping} onChange={e => setFieldMapping(e.target.value)} style={ta}
            placeholder={'{\n  "data.claimant_name": "applicant_name",\n  "data.amount": "claim_amount",\n  "event.source": "intake_channel"\n}'} />
        </div>
      </>)}

      {/* Linked process definition */}
      <div style={{ marginBottom: 20 }}>
        <label style={lbl}>Auto-Start Process (optional)</label>
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
          When a case is created, this HxFusion process starts automatically and binds to the case.
        </div>
        <select value={procDefId} onChange={e => setProcDefId(e.target.value)} style={{ ...inp }}>
          <option value="">— no auto-process —</option>
          {processDefs.map((d: any) => <option key={d.id} value={d.id}>{d.name} v{d.version}</option>)}
        </select>
      </div>

      {msg && <div style={{ fontSize: 12, color: msg.startsWith("Error") ? "#ef4444" : "#22c55e", marginBottom: 12 }}>{msg}</div>}
      <button onClick={save} disabled={saving} style={{ padding: "8px 20px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: 6, fontWeight: 700, fontSize: 13, cursor: "pointer", opacity: saving ? 0.6 : 1 }}>
        {saving ? "Saving…" : "Save Intake Config"}
      </button>
    </div>
  );
}


// ── Variables Panel (Case Variables Phase 1 — spec v2) ──────────────────────────
interface VarNamespace {
  id: string; name: string; owner_type: string; sensitivity: string;
  status: string; reserved: boolean;
}
interface VarDef {
  id: string; namespace: string | null; name: string; full_key: string;
  var_type: string; definition_status: string; sensitivity_override: string | null;
  label: string | null; required: boolean; indexed: boolean;
}

const VAR_TYPES = ["str", "int", "float", "bool", "date", "datetime", "list", "dict", "any"];

function vhdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

function VariablesPanel({ caseTypeId, readOnly }: { caseTypeId: string; readOnly: boolean }) {
  const [namespaces, setNamespaces] = useState<VarNamespace[]>([]);
  const [defs, setDefs] = useState<VarDef[]>([]);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  // new-variable form
  const [nsId, setNsId] = useState("");
  const [vName, setVName] = useState("");
  const [vType, setVType] = useState("str");
  const [vLabel, setVLabel] = useState("");
  const [vRequired, setVRequired] = useState(false);
  const [vIndexed, setVIndexed] = useState(false);

  // migration wizard (Phase 4)
  const [blobKeys, setBlobKeys] = useState<{
    key: string; count: number; inferred_type: string;
    valid_name: boolean; pii_hint: boolean; promoted_to: string | null;
  }[]>([]);
  const [promoteTarget, setPromoteTarget] = useState<Record<string, string>>({});

  const load = React.useCallback(async () => {
    try {
      const [nsR, defR, scanR] = await Promise.all([
        fetch("/api/v1/variables/namespaces", { headers: vhdr() }),
        fetch(`/api/v1/variables/case-types/${caseTypeId}`, { headers: vhdr() }),
        fetch(`/api/v1/variables/case-types/${caseTypeId}/blob-keys`, { headers: vhdr() }),
      ]);
      if (nsR.ok) setNamespaces(await nsR.json());
      if (defR.ok) setDefs(await defR.json());
      if (scanR.ok) setBlobKeys((await scanR.json()).keys ?? []);
    } catch { /* non-critical */ }
  }, [caseTypeId]);

  React.useEffect(() => { load(); }, [load]);

  // velaris is virtual — not a definable target
  const definableNs = namespaces.filter(n => n.name !== "velaris" && n.status === "active");

  async function addVariable() {
    if (!nsId || !vName.trim()) { setMsg({ text: "Pick a namespace and enter a name.", ok: false }); return; }
    setBusy(true); setMsg(null);
    try {
      const r = await fetch(`/api/v1/variables/case-types/${caseTypeId}`, {
        method: "POST", headers: { "Content-Type": "application/json", ...vhdr() },
        body: JSON.stringify({
          namespace_id: nsId, name: vName.trim().toLowerCase(), var_type: vType,
          label: vLabel || null, required: vRequired, indexed: vIndexed,
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Could not add variable");
      setVName(""); setVLabel(""); setVRequired(false); setVIndexed(false);
      setMsg({ text: `Added ${d.full_key}`, ok: true });
      load();
    } catch (e: any) { setMsg({ text: e.message, ok: false }); }
    finally { setBusy(false); }
  }

  async function patchDef(id: string, body: Record<string, unknown>) {
    const r = await fetch(`/api/v1/variables/case-types/${caseTypeId}/${id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json", ...vhdr() },
      body: JSON.stringify(body),
    });
    if (r.ok) load();
    else { const d = await r.json().catch(() => ({})); setMsg({ text: d.detail ?? "Update failed", ok: false }); }
  }

  async function removeDef(id: string) {
    const r = await fetch(`/api/v1/variables/case-types/${caseTypeId}/${id}`, { method: "DELETE", headers: vhdr() });
    if (r.ok) load();
  }

  async function promoteKey(key: string) {
    setBusy(true); setMsg(null);
    try {
      const r = await fetch(`/api/v1/variables/case-types/${caseTypeId}/promote`, {
        method: "POST", headers: { "Content-Type": "application/json", ...vhdr() },
        body: JSON.stringify({ key, target_namespace: promoteTarget[key] || "legacy" }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Promotion failed");
      setMsg({ text: `Promoted ${key} → ${d.full_key} (${d.promoted} case(s)${d.pii_forced ? ", pii enforced" : ""})`, ok: true });
      load();
    } catch (e: any) { setMsg({ text: e.message, ok: false }); }
    finally { setBusy(false); }
  }

  const undeclared = defs.filter(d => d.definition_status === "undeclared");
  const declared = defs.filter(d => d.definition_status !== "undeclared");

  const cell: React.CSSProperties = { padding: "6px 10px", fontSize: 12, borderBottom: "1px solid var(--border-subtle)" };
  const inp: React.CSSProperties = { padding: "6px 10px", fontSize: 12, border: "1px solid var(--border-default)", borderRadius: 6, background: "var(--bg-input, var(--bg-elevated))", color: "var(--text-primary)" };
  const sensColor = (s: string) => s === "secret" ? "#dc2626" : s === "pii" ? "#d97706" : "var(--text-muted)";

  return (
    <div style={{ maxWidth: 920 }}>
      <div style={{ fontSize: 18, fontWeight: 700, marginBottom: 4 }}>Variables</div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
        Typed, namespaced case data. A variable belongs to one namespace (the owning
        integration or platform pipeline) and one case type. <code>velaris.*</code> natives
        (subject, priority, status) are the case's own fields and are not defined here.
      </div>

      {/* Undeclared — needs operator attention */}
      {undeclared.length > 0 && (
        <div style={{ marginBottom: 20, border: "1px solid #d97706", borderRadius: 8, padding: 12, background: "#d9770611" }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#b45309", marginBottom: 8 }}>
            ⚠ {undeclared.length} undeclared variable(s) received from integrations — classify them
          </div>
          {undeclared.map(d => (
            <div key={d.id} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <code style={{ flex: 1, fontSize: 12 }}>{d.full_key}</code>
              {!readOnly && <>
                <select defaultValue="any" onChange={e => patchDef(d.id, { var_type: e.target.value, definition_status: "defined" })} style={inp}>
                  {VAR_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                </select>
                <button onClick={() => patchDef(d.id, { definition_status: "defined" })} style={{ ...inp, cursor: "pointer", color: "#16a34a" }}>Define</button>
                <button onClick={() => patchDef(d.id, { definition_status: "ignored" })} style={{ ...inp, cursor: "pointer", color: "var(--text-muted)" }}>Ignore</button>
              </>}
            </div>
          ))}
        </div>
      )}

      {/* Declared variables table */}
      <table style={{ width: "100%", borderCollapse: "collapse", marginBottom: 20 }}>
        <thead>
          <tr style={{ textAlign: "left", fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase" }}>
            <th style={cell}>Full Key</th><th style={cell}>Type</th><th style={cell}>Label</th>
            <th style={cell}>Sensitivity</th><th style={cell}>Req</th><th style={cell}>Idx</th><th style={cell}></th>
          </tr>
        </thead>
        <tbody>
          {declared.length === 0 && (
            <tr><td colSpan={7} style={{ ...cell, color: "var(--text-muted)", fontStyle: "italic" }}>No variables defined yet.</td></tr>
          )}
          {declared.map(d => {
            const ns = namespaces.find(n => n.name === d.namespace);
            const sens = d.sensitivity_override ?? ns?.sensitivity ?? "internal";
            return (
              <tr key={d.id}>
                <td style={cell}><code>{d.full_key}</code>{d.definition_status === "ignored" && <span style={{ color: "var(--text-muted)" }}> (ignored)</span>}</td>
                <td style={cell}>{d.var_type}</td>
                <td style={cell}>{d.label}</td>
                <td style={{ ...cell, color: sensColor(sens), fontWeight: 600 }}>{sens}</td>
                <td style={cell}>{d.required ? "✓" : ""}</td>
                <td style={cell}>{d.indexed ? "✓" : ""}</td>
                <td style={cell}>{!readOnly && d.definition_status !== "undeclared" && <button onClick={() => removeDef(d.id)} style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 12 }}>Remove</button>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {/* Add variable */}
      {!readOnly && (
        <div style={{ border: "1px solid var(--border-default)", borderRadius: 8, padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>Add Variable</div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <select value={nsId} onChange={e => setNsId(e.target.value)} style={inp}>
              <option value="">namespace…</option>
              {definableNs.map(n => (
                <option key={n.id} value={n.id}>{n.name}{n.sensitivity !== "internal" ? ` (${n.sensitivity})` : ""}</option>
              ))}
            </select>
            <input placeholder="variable_name" value={vName} onChange={e => setVName(e.target.value)} style={{ ...inp, width: 160 }} />
            <select value={vType} onChange={e => setVType(e.target.value)} style={inp}>
              {VAR_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
            <input placeholder="Label" value={vLabel} onChange={e => setVLabel(e.target.value)} style={{ ...inp, width: 140 }} />
            <label style={{ fontSize: 12, display: "flex", gap: 4, alignItems: "center" }}><input type="checkbox" checked={vRequired} onChange={e => setVRequired(e.target.checked)} />required</label>
            <label style={{ fontSize: 12, display: "flex", gap: 4, alignItems: "center" }}><input type="checkbox" checked={vIndexed} onChange={e => setVIndexed(e.target.checked)} />indexed</label>
            <button onClick={addVariable} disabled={busy} style={{ padding: "6px 16px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: 6, fontWeight: 700, fontSize: 12, cursor: "pointer", opacity: busy ? 0.6 : 1 }}>Add</button>
          </div>
          {definableNs.length === 0 && (
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>
              No integration namespaces registered yet — an admin registers them when connecting integrations.
            </div>
          )}
        </div>
      )}

      {/* Migrate case.data (Phase 4 wizard) */}
      {blobKeys.length > 0 && (
        <div style={{ border: "1px solid var(--border-default)", borderRadius: 8, padding: 14, marginTop: 20 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 4 }}>Migrate case.data</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 10 }}>
            Promote legacy <code>case.data</code> keys into typed variables. The blob is never modified —
            promoted keys are served from the typed store instead. Keys HxSync classifies as pii get
            pii sensitivity enforced automatically.
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase" }}>
                <th style={cell}>Key</th><th style={cell}>Cases</th><th style={cell}>Type</th>
                <th style={cell}></th><th style={cell}>Target</th><th style={cell}></th>
              </tr>
            </thead>
            <tbody>
              {blobKeys.map(bk => (
                <tr key={bk.key}>
                  <td style={cell}><code>{bk.key}</code></td>
                  <td style={cell}>{bk.count}</td>
                  <td style={cell}>{bk.inferred_type}</td>
                  <td style={cell}>
                    {bk.pii_hint && <span style={{ color: "#d97706", fontWeight: 700, fontSize: 11 }}>pii</span>}
                    {!bk.valid_name && <span style={{ color: "var(--text-muted)", fontSize: 11 }} title="Key needs a valid target name (lowercase, a-z0-9_)"> rename req.</span>}
                  </td>
                  <td style={cell}>
                    {bk.promoted_to
                      ? <code style={{ color: "#16a34a" }}>{bk.promoted_to}</code>
                      : !readOnly && bk.valid_name && (
                        <select value={promoteTarget[bk.key] || "legacy"}
                                onChange={e => setPromoteTarget(t => ({ ...t, [bk.key]: e.target.value }))} style={inp}>
                          <option value="legacy">legacy</option>
                          {definableNs.filter(n => n.name !== "legacy" && !["form", "portal"].includes(n.name)).map(n => (
                            <option key={n.id} value={n.name}>{n.name}</option>
                          ))}
                        </select>
                      )}
                  </td>
                  <td style={cell}>
                    {!bk.promoted_to && !readOnly && bk.valid_name && (
                      <button onClick={() => promoteKey(bk.key)} disabled={busy}
                        style={{ ...inp, cursor: "pointer", color: "var(--accent)", fontWeight: 700 }}>Promote</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {msg && <div style={{ fontSize: 12, marginTop: 12, color: msg.ok ? "#22c55e" : "#ef4444" }}>{msg.text}</div>}
    </div>
  );
}
