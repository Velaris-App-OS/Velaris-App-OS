/**
 * The bulk action bar — the visible half of B3.
 *
 * Design: docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md
 *
 * Three things this component exists to get right, all of which are about
 * partial failure rather than about the happy path:
 *
 * 1. **Actions come from the server**, not from a list hardcoded here. The
 *    catalogue already has the tenant's overrides folded in, so an action the
 *    tenant disabled is ABSENT and one the operator lacks the privilege for is
 *    not offered. A button that 403s is worse than no button.
 * 2. **Partial success is shown per case.** Acting on 400 cases and getting a
 *    green tick while twelve silently refused is how a bulk feature loses an
 *    operator's trust in its first week.
 * 3. **The ceiling refuses rather than truncates**, and this says so before the
 *    request goes rather than after it comes back.
 *
 * "Move to stage" is here too but is not an action: it has its own privilege and
 * a server-checked preview, so it lives in `BulkStageMove`.
 */
import React, { useEffect, useMemo, useState } from "react";
import { Button, Card } from "@shared/components";
import BulkStageMove from "./BulkStageMove";
import {
  bulkCaseAction,
  caseActionCatalogue,
  type BulkActionResult,
  type CaseActionDef,
  type CaseStatusDef,
} from "@shared/api/client";

/** Not an action name: the registry's vocabulary is closed and has no such entry. */
const MOVE_STAGE = "__move_stage__";

interface Props {
  selectedIds: string[];
  /** Case types of the selected cases, for the stage list. */
  caseTypeIds?: string[];
  onClear: () => void;
  onDone: () => void;
}

export default function BulkActionBar({ selectedIds, caseTypeIds = [], onClear, onDone }: Props) {
  const [actions, setActions] = useState<CaseActionDef[]>([]);
  const [statuses, setStatuses] = useState<CaseStatusDef[]>([]);
  const [ceiling, setCeiling] = useState<number>(500);
  const [stagePermitted, setStagePermitted] = useState(false);
  const [action, setAction] = useState<string>("");
  const [param, setParam] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<BulkActionResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    caseActionCatalogue()
      .then((c) => {
        // Only what this operator may actually do, and only what is safe to
        // apply to many cases at once.
        setActions(c.actions.filter((a) => a.permitted && !a.destructive));
        setStatuses(c.statuses.filter((s) => s.enabled));
        setCeiling(c.bulk_ceiling);
        setStagePermitted(c.bulk_stage_permitted === true);
      })
      .catch((e) => setError(e?.message || "Could not load actions"));
  }, []);

  const chosen = useMemo(
    () => actions.find((a) => a.name === action) || null,
    [actions, action]
  );

  // Only single-value parameters are offered here. `assign` needs a step_id
  // that differs per case, and guessing one would write assignments nobody can
  // complete — the same reason the scheduled bulk action refuses assignment.
  const paramName = chosen?.required_params?.[0] ?? null;
  const tooMany = selectedIds.length > ceiling;
  const movingStage = action === MOVE_STAGE;
  const ready = !!chosen && (!paramName || !!param) && !tooMany;

  async function run() {
    if (!chosen) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const params = paramName ? { [paramName]: param } : {};
      const r = await bulkCaseAction(selectedIds, chosen.name, params);
      setResult(r);
      onDone();
    } catch (e: any) {
      setError(e?.message || "The action could not be run");
    } finally {
      setBusy(false);
    }
  }

  if (selectedIds.length === 0) return null;

  return (
    <Card style={{ padding: "var(--space-md) var(--space-lg)", marginBottom: "var(--space-md)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-md)", flexWrap: "wrap" }}>
        <strong style={{ fontSize: 13 }}>
          {selectedIds.length} case{selectedIds.length === 1 ? "" : "s"} selected
        </strong>

        <select
          value={action}
          onChange={(e) => { setAction(e.target.value); setParam(""); setResult(null); }}
          style={{ padding: "4px 8px", fontSize: 13 }}
        >
          <option value="">Choose an action…</option>
          {actions.map((a) => (
            <option key={a.name} value={a.name}>{a.label}</option>
          ))}
          {stagePermitted && <option value={MOVE_STAGE}>Move to stage…</option>}
        </select>

        {paramName === "status" && (
          <select value={param} onChange={(e) => setParam(e.target.value)}
                  style={{ padding: "4px 8px", fontSize: 13 }}>
            <option value="">Status…</option>
            {statuses.map((s) => (
              <option key={s.name} value={s.name}>{s.label}</option>
            ))}
          </select>
        )}
        {paramName && paramName !== "status" && (
          <input value={param} onChange={(e) => setParam(e.target.value)}
                 placeholder={paramName.replace(/_/g, " ")}
                 style={{ padding: "4px 8px", fontSize: 13 }} />
        )}

        {!movingStage && (
          <Button size="sm" disabled={!ready || busy} onClick={run}>
            {busy ? "Working…" : "Apply"}
          </Button>
        )}
        <Button size="sm" variant="secondary" onClick={onClear}>Clear</Button>
      </div>

      {chosen && (
        <p style={{ fontSize: 12, color: "var(--text-muted)", marginTop: "var(--space-sm)" }}>
          {chosen.summary}
          {chosen.effects.length > 0 && <> · affects: {chosen.effects.join(", ")}</>}
        </p>
      )}

      {movingStage && !tooMany && (
        <BulkStageMove selectedIds={selectedIds} caseTypeIds={caseTypeIds} onDone={onDone} />
      )}

      {/* Said BEFORE the request, because the server refuses rather than
          truncating — an operator should not discover that from an error. */}
      {tooMany && (
        <p style={{ fontSize: 12, color: "var(--danger)", marginTop: "var(--space-sm)" }}>
          {selectedIds.length} cases is over the limit of {ceiling}. Narrow the
          selection — the server refuses rather than acting on the first {ceiling}.
        </p>
      )}

      {error && (
        <p style={{ fontSize: 12, color: "var(--danger)", marginTop: "var(--space-sm)" }}>{error}</p>
      )}

      {result && (
        <div style={{ marginTop: "var(--space-sm)", fontSize: 12 }}>
          <div>
            <strong>{result.succeeded}</strong> succeeded
            {result.failed > 0 && (
              <> · <strong style={{ color: "var(--danger)" }}>{result.failed}</strong> refused</>
            )}
          </div>
          {/* Every failure by name and reason. Partial success is the normal
              outcome and must never be reported as a plain success. */}
          {result.failed > 0 && (
            <ul style={{ marginTop: 4, paddingLeft: 18, color: "var(--text-muted)" }}>
              {result.results.filter((r) => !r.ok).map((r) => (
                <li key={r.case_id}>
                  <code style={{ fontFamily: "var(--font-mono)" }}>{r.case_id.slice(0, 8)}</code>
                  {" — "}{r.error}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Card>
  );
}
