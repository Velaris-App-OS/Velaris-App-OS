/**
 * useCommit — wraps any save operation with the Commit pattern.
 *
 * Usage:
 *   const { commitOpen, commitSaving, requestCommit, handleCommit, cancelCommit } =
 *     useCommit("case_type", ct.id, ct.name);
 *
 *   // In JSX:
 *   <button onClick={() => requestCommit(saveFn, { before: dbState, after: newState })}>Commit</button>
 *   <CommitModal
 *     open={commitOpen} saving={commitSaving}
 *     componentType="case_type" componentName={ct.name}
 *     pendingChanges={pendingChanges}
 *     onCommit={handleCommit} onCancel={cancelCommit}
 *   />
 */
import { useState, useCallback } from "react";

const CASE_BASE = "/api/v1";

async function postCommit(payload: {
  component_type: string;
  component_id: string;
  component_name: string;
  commit_message: string;
  diff_snapshot?: Record<string, unknown> | null;
}): Promise<void> {
  await fetch(`${CASE_BASE}/commits`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${localStorage.getItem("helix_token") ?? ""}`,
    },
    body: JSON.stringify(payload),
  });
}

export interface CommitSnapshot {
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

/** Human-readable change entry */
export interface ChangeEntry {
  symbol: "+" | "-" | "~";
  text: string;
}

/** Compute a meaningful diff between two case type definition_json snapshots. */
export function computeCaseTypeDiff(
  before: Record<string, unknown> | null,
  after: Record<string, unknown> | null,
): ChangeEntry[] {
  if (!before || !after) return [];
  const changes: ChangeEntry[] = [];
  const bStages = (before.stages || []) as any[];
  const aStages = (after.stages || []) as any[];

  // Stages
  for (const s of aStages) {
    if (!bStages.find((b) => b.id === s.id)) {
      changes.push({ symbol: "+", text: `Stage added: "${s.name}"` });
    }
  }
  for (const s of bStages) {
    if (!aStages.find((a) => a.id === s.id)) {
      changes.push({ symbol: "-", text: `Stage removed: "${s.name}"` });
    }
  }

  // Stage renames and step changes
  for (const aStage of aStages) {
    const bStage = bStages.find((b) => b.id === aStage.id);
    if (!bStage) continue;
    if (bStage.name !== aStage.name) {
      changes.push({ symbol: "~", text: `Stage renamed: "${bStage.name}" → "${aStage.name}"` });
    }
    const bSteps = (bStage.steps || []) as any[];
    const aSteps = (aStage.steps || []) as any[];
    for (const st of aSteps) {
      if (!bSteps.find((b) => b.id === st.id)) {
        changes.push({ symbol: "+", text: `Step added: "${st.name}" in "${aStage.name}"` });
      }
    }
    for (const st of bSteps) {
      if (!aSteps.find((a) => a.id === st.id)) {
        changes.push({ symbol: "-", text: `Step removed: "${st.name}" from "${aStage.name}"` });
      }
    }
    for (const aSt of aSteps) {
      const bSt = bSteps.find((b) => b.id === aSt.id);
      if (bSt && bSt.name !== aSt.name) {
        changes.push({ symbol: "~", text: `Step renamed: "${bSt.name}" → "${aSt.name}" in "${aStage.name}"` });
      }
      if (bSt && bSt.step_type !== aSt.step_type) {
        changes.push({ symbol: "~", text: `Step type changed: "${aSt.name}" (${bSt.step_type} → ${aSt.step_type})` });
      }
    }
  }

  // SLA policies
  const bSLAs = (before.sla_policies || []) as any[];
  const aSLAs = (after.sla_policies || []) as any[];
  for (const s of aSLAs) {
    const b = bSLAs.find((x) => x.id === s.id);
    if (!b) { changes.push({ symbol: "+", text: `SLA added: "${s.name}"` }); continue; }
    if (b.goal_duration !== s.goal_duration || b.deadline_duration !== s.deadline_duration) {
      changes.push({ symbol: "~", text: `SLA "${s.name}": goal ${b.goal_duration}→${s.goal_duration}, deadline ${b.deadline_duration}→${s.deadline_duration}` });
    }
  }
  for (const s of bSLAs) {
    if (!aSLAs.find((x) => x.id === s.id)) {
      changes.push({ symbol: "-", text: `SLA removed: "${s.name}"` });
    }
  }

  return changes;
}

export function useCommit(
  componentType: string,
  componentId: string,
  componentName: string,
) {
  const [commitOpen, setCommitOpen]     = useState(false);
  const [commitSaving, setCommitSaving] = useState(false);
  const [pendingSaveFn, setPendingSaveFn] = useState<(() => Promise<void>) | null>(null);
  const [snapshot, setSnapshot]         = useState<CommitSnapshot>({ before: null, after: null });

  const pendingChanges = computeCaseTypeDiff(snapshot.before, snapshot.after);

  /**
   * Call this instead of directly calling the save function.
   * Pass { before: currentDbState, after: newState } for meaningful diffs.
   */
  const requestCommit = useCallback(
    (saveFn: () => Promise<void>, snap?: Partial<CommitSnapshot> | Record<string, unknown>) => {
      setPendingSaveFn(() => saveFn);
      // Support both new { before, after } shape and legacy snapshot object
      if (snap && ("before" in snap || "after" in snap)) {
        setSnapshot({ before: (snap as any).before ?? null, after: (snap as any).after ?? null });
      } else if (snap) {
        // Legacy: treat entire snapshot as "after" (old behaviour)
        setSnapshot({ before: null, after: snap as Record<string, unknown> });
      } else {
        setSnapshot({ before: null, after: null });
      }
      setCommitOpen(true);
    },
    [],
  );

  const handleCommit = useCallback(
    async (message: string) => {
      if (!pendingSaveFn) return;
      setCommitSaving(true);
      try {
        await pendingSaveFn();
        postCommit({
          component_type:  componentType,
          component_id:    componentId,
          component_name:  componentName,
          commit_message:  message,
          diff_snapshot:   (snapshot.before || snapshot.after)
            ? { before: snapshot.before, after: snapshot.after }
            : null,
        }).catch(() => {});
      } finally {
        setCommitSaving(false);
        setCommitOpen(false);
        setPendingSaveFn(null);
        setSnapshot({ before: null, after: null });
      }
    },
    [pendingSaveFn, componentType, componentId, componentName, snapshot],
  );

  const cancelCommit = useCallback(() => {
    setCommitOpen(false);
    setPendingSaveFn(null);
    setSnapshot({ before: null, after: null });
  }, []);

  return { commitOpen, commitSaving, requestCommit, handleCommit, cancelCommit, pendingChanges };
}
