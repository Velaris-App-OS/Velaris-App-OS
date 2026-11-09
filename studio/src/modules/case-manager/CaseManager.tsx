import React, { useState, useEffect, useRef, useCallback } from "react";
import AIProposals from "./AIProposals";
import BulkActionBar from "./BulkActionBar";
import CapturedDigest from "./CapturedDigest";
import CaseEventStream from "./CaseEventStream";
import { MoveTenantTab, useMayTransferTenant } from "./TenantTransfer";
import { LegalHoldBanner, LegalHoldTab, useCaseLegalHold } from "./LegalHold";
import { useApi } from "@shared/hooks";
import {
  listCases,
  listCaseShares,
  shareCase,
  unshareCase,
  getCaseDetail,
  createCase,
  caseActionCatalogue,
  changeCaseStatus,
  changeCasePriority,
  resolveCase,
  closeCase,
  reopenCase,
  cancelCase,
  getCaseChildren,
  listCaseTypes,
  transitionStage,
  completeStep,
  listStepCompletions,
  unlockStep,
  getCaseForm,
  listMeetProviders,
  listCaseSessions,
  listCaseMessages,
  postCaseMessage,
  fetchRecordingUrl,
  fetchSessionTranscript,
  verifySessionTranscript,
  askCase,
  type CaseAskResult,
  runSessionIntelligence,
  getSessionIntelligence,
  type SessionIntelligence,
  startCaseSession,
  endCaseSession,
  getMeetSessionToken,
  inviteMeetGuest,
  startMeetRecording,
  stopMeetRecording,
  verifyMeetRecording,
  issueMeetChallenges,
  listMeetChallenges,
  recordMeetChallengeResult,
  runKycAnalysis,
  getKycAnalysis,
  type KycAnalysis,
  type KycCheck,
  apiFetch,
  errorFromResponse,
  openDocument,
} from "@shared/api/client";
import ErrorCodeLink from "@shared/errors/ErrorCodeLink";
import type { MyTaskResult, CaseSession, MeetProvider } from "@shared/api/client";
import { loadPrefill } from "../form-builder/prefill";
import FormRenderer from "@modules/form-builder/FormRenderer";
import { useFormDraft } from "@modules/form-builder/drafts";
import { Button, Card, EmptyState, MeetRoom, Spinner, Stat, Tabs, TimeAgo, VoiceInput, appendTranscript } from "@shared/components";
import { getAccessToken } from "@/auth/tokenManager";
import type { CaseSummary, CaseAuditEntry, CaseStatus, CasePriority, SLAStatusInfo, CaseTypeSummary } from "@shared/types";

/* ═══════════════════════════════════════════════════════════════════
   CaseManager — view, manage, and track case instances
   ═══════════════════════════════════════════════════════════════════ */

const STATUS_COLORS: Record<string, string> = {
  new: "var(--text-muted)",
  open: "var(--status-active)",
  pending_external: "var(--status-running)",
  pending_subcase: "var(--status-running)",
  resolved: "var(--status-completed)",
  closed: "var(--status-completed)",
  reopened: "var(--status-running)",
  cancelled: "var(--status-cancelled)",
};

const PRIORITY_COLORS: Record<string, string> = {
  low: "#55556a",
  medium: "#8888a0",
  high: "#f7b731",
  critical: "#fc5c65",
  blocker: "#fc5c65",
};

export default function CaseManager() {
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [priorityFilter, setPriorityFilter] = useState<string>("");
  const [varFilterInput, setVarFilterInput] = useState<string>("");
  const [varFilter, setVarFilter] = useState<string>("");      // applied "ns.name:value"
  const [varFilterError, setVarFilterError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Multi-select for bulk actions. Kept separate from `selectedId` (which opens
  // the detail panel) so ticking a row never yanks the panel out from under an
  // operator who is reading one case while selecting others.
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const toggleChecked = useCallback((id: string) => {
    setChecked(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }, []);
  const [showCreate, setShowCreate] = useState(false);
  const [detailWidth, setDetailWidth] = useState(500);
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 25;
  const dragging = useRef(false);
  const dragStartX = useRef(0);
  const dragStartW = useRef(0);

  const { data, loading, error, errorObj, refetch } = useApi(
    () => listCases({ status: statusFilter || undefined, priority: priorityFilter || undefined, vars: varFilter ? [varFilter] : undefined, page, page_size: PAGE_SIZE })
      .then(r => { setVarFilterError(null); return r; })
      .catch(e => { if (varFilter) setVarFilterError(e?.message || "Invalid variable filter"); throw e; }),
    [statusFilter, priorityFilter, varFilter, page]
  );

  const cases = data?.items ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.ceil(total / PAGE_SIZE);

  const setStatusFilterAndReset  = (v: string) => { setStatusFilter(v);  setPage(1); };
  const setPriorityFilterAndReset = (v: string) => { setPriorityFilter(v); setPage(1); };

  const onDragStart = useCallback((e: React.MouseEvent) => {
    dragging.current = true;
    dragStartX.current = e.clientX;
    dragStartW.current = detailWidth;
    const onMove = (ev: MouseEvent) => {
      if (!dragging.current) return;
      const delta = dragStartX.current - ev.clientX;
      setDetailWidth(Math.max(380, Math.min(860, dragStartW.current + delta)));
    };
    const onUp = () => {
      dragging.current = false;
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, [detailWidth]);

  return (
    <div style={{ display: "flex", height: "100%", overflow: "hidden" }}>
      {/* Case list */}
      <div style={{ flex: 1, padding: "var(--space-xl) var(--space-2xl)", overflow: "auto", minWidth: 320 }}>
        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-lg)" }}>
          <Button onClick={() => setShowCreate(true)}>+ New Case</Button>
          {totalPages > 1 && <PaginationBar page={page} totalPages={totalPages} total={total} pageSize={PAGE_SIZE} onChange={setPage} />}
        </div>

        {/* Filters */}
        <div style={{ display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-lg)", flexWrap: "wrap" }}>
          {["", "new", "open", "pending_external", "resolved", "closed", "cancelled"].map((s) => (
            <FilterChip key={s} label={s || "All"} active={statusFilter === s} onClick={() => setStatusFilterAndReset(s)} />
          ))}
          <div style={{ width: 1, background: "var(--border-subtle)", margin: "0 var(--space-xs)" }} />
          {["", "low", "medium", "high", "critical", "blocker"].map((p) => (
            <FilterChip key={`p-${p}`} label={p || "Any"} active={priorityFilter === p} onClick={() => setPriorityFilterAndReset(p)}
              color={p ? PRIORITY_COLORS[p] : undefined} />
          ))}
          <div style={{ width: 1, background: "var(--border-subtle)", margin: "0 var(--space-xs)" }} />
          <form onSubmit={(e) => { e.preventDefault(); setVarFilter(varFilterInput.trim()); setPage(1); }}
                style={{ display: "flex", gap: 4, alignItems: "center" }}>
            <input
              value={varFilterInput}
              onChange={(e) => setVarFilterInput(e.target.value)}
              placeholder="variable filter — crm.account_status:active"
              title="Filter by an indexed case variable (namespace.name:value)"
              style={{
                fontSize: 11, fontFamily: "var(--font-mono)", padding: "4px 8px",
                background: "var(--bg-input)", color: "var(--text-primary)",
                border: `1px solid ${varFilterError ? "var(--status-failed)" : "var(--border-subtle)"}`,
                borderRadius: "var(--radius-sm)", width: 240,
              }}
            />
            {varFilter && (
              <button type="button" onClick={() => { setVarFilter(""); setVarFilterInput(""); setVarFilterError(null); setPage(1); }}
                style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 12 }}>✕</button>
            )}
          </form>
          {varFilterError && <span style={{ fontSize: 11, color: "var(--status-failed)", alignSelf: "center" }}>{varFilterError}</span>}
        </div>

        {/* List */}
        {/* Spinner only on initial load (no data yet); refetches keep showing stale rows */}
        {loading && cases.length === 0 && <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}><Spinner size={28} /></div>}

        {error && (
          <Card style={{ borderColor: "var(--status-failed)" }}>
            <p style={{ color: "var(--status-failed)", fontSize: 13 }}>
              Failed to load cases: {error}
              <ErrorCodeLink error={errorObj} componentKey="cases" />
            </p>
            <Button variant="secondary" size="sm" onClick={refetch} style={{ marginTop: "var(--space-sm)" }}>Retry</Button>
          </Card>
        )}

        {!loading && !error && cases.length === 0 && (
          <EmptyState title="No cases found" description="Adjust filters or create a new case."
            action={<Button onClick={() => setShowCreate(true)}>+ New Case</Button>} />
        )}

        {!error && cases.length > 0 && (
          <div style={{ padding: "var(--space-sm) var(--space-lg) 0" }}>
            <label style={{ fontSize: 12, color: "var(--text-muted)", display: "flex",
                            alignItems: "center", gap: 6, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={cases.length > 0 && cases.every(c => checked.has(c.id))}
                onChange={(e) => setChecked(prev => {
                  const next = new Set(prev);
                  // Only THIS page. "Select all" across pages would act on cases
                  // the operator has never seen.
                  cases.forEach(c => e.target.checked ? next.add(c.id) : next.delete(c.id));
                  return next;
                })}
              />
              Select all on this page
            </label>
          </div>
        )}

        <BulkActionBar
          selectedIds={[...checked]}
          caseTypeIds={cases.filter((c) => checked.has(c.id)).map((c) => c.case_type_id)}
          onClear={() => setChecked(new Set())}
          onDone={() => { refetch(); }}
        />

        {!error && cases.map((c) => (
          <CaseRow
            key={c.id}
            case_={c}
            selected={selectedId === c.id}
            checked={checked.has(c.id)}
            onCheck={() => toggleChecked(c.id)}
            onClick={() => setSelectedId(c.id)}
          />
        ))}

        {/* Bottom pagination */}
        {!loading && !error && totalPages > 1 && (
          <div style={{ padding: "var(--space-lg) 0", display: "flex", justifyContent: "flex-end" }}>
            <PaginationBar page={page} totalPages={totalPages} total={total} pageSize={PAGE_SIZE} onChange={setPage} />
          </div>
        )}
      </div>

      {/* Detail panel */}
      {selectedId && (
        <>
          {/* Drag handle */}
          <div
            onMouseDown={onDragStart}
            style={{
              width: 5, cursor: "col-resize", flexShrink: 0,
              background: "var(--border-subtle)", transition: "background 0.1s",
            }}
            onMouseEnter={(e) => (e.currentTarget.style.background = "var(--accent)")}
            onMouseLeave={(e) => (e.currentTarget.style.background = "var(--border-subtle)")}
          />
          <CaseDetailPanel
            caseId={selectedId}
            width={detailWidth}
            onClose={() => setSelectedId(null)}
            onUpdate={refetch}
          />
        </>
      )}

      {/* Create modal */}
      {showCreate && (
        <CreateCaseModal onClose={() => setShowCreate(false)} onCreated={() => { setShowCreate(false); refetch(); }} />
      )}
    </div>
  );
}

/* ── Case Row ─────────────────────────────────────────────────── */

function CaseRow({ case_, selected, checked, onCheck, onClick }: {
  case_: CaseSummary; selected: boolean; checked: boolean;
  onCheck: () => void; onClick: () => void;
}) {
  const statusColor = STATUS_COLORS[case_.status] || "var(--text-muted)";
  const priorityColor = PRIORITY_COLORS[case_.priority] || "var(--text-muted)";

  return (
    <div
      onClick={onClick}
      style={{
        display: "flex", alignItems: "center", gap: "var(--space-md)",
        padding: "var(--space-md) var(--space-lg)",
        borderBottom: "1px solid var(--border-subtle)",
        background: selected ? "var(--accent-dim)" : "transparent",
        cursor: "pointer", transition: "background 0.1s ease",
      }}
      onMouseEnter={(e) => !selected && (e.currentTarget.style.background = "var(--bg-card-hover)")}
      onMouseLeave={(e) => !selected && (e.currentTarget.style.background = "transparent")}
    >
      {/* Ticking a row must not also open it — hence stopPropagation. */}
      <input
        type="checkbox"
        checked={checked}
        onChange={onCheck}
        onClick={(e) => e.stopPropagation()}
        aria-label={`Select case ${(case_ as any).case_number ?? case_.id.slice(0, 8)}`}
        style={{ flexShrink: 0, cursor: "pointer" }}
      />

      {/* Status dot */}
      <span style={{ width: 8, height: 8, borderRadius: "50%", background: statusColor, flexShrink: 0 }} />

      {/* Main info */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
          <span style={{ fontSize: 13, fontWeight: 700, color: "var(--accent)", fontFamily: "var(--font-mono)", letterSpacing: "0.04em" }}>
            {(case_ as any).case_number ?? case_.id.slice(0, 8).toUpperCase()}
          </span>
          {case_.current_stage_id && (
            <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
              · {case_.current_stage_id}
            </span>
          )}
        </div>
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
          {case_.created_by || "system"} · <TimeAgo date={case_.created_at} />
        </div>
      </div>

      {/* Priority */}
      <span style={{
        fontSize: 10, fontWeight: 500, padding: "2px 8px", borderRadius: 100,
        color: priorityColor, fontFamily: "var(--font-mono)", textTransform: "uppercase",
        background: `color-mix(in srgb, ${priorityColor} 12%, transparent)`,
        border: `1px solid color-mix(in srgb, ${priorityColor} 25%, transparent)`,
      }}>
        {case_.priority}
      </span>

      {/* Status */}
      <span style={{
        fontSize: 10, fontWeight: 500, padding: "2px 8px", borderRadius: 100,
        color: statusColor, fontFamily: "var(--font-mono)", textTransform: "uppercase",
        background: `color-mix(in srgb, ${statusColor} 12%, transparent)`,
        border: `1px solid color-mix(in srgb, ${statusColor} 25%, transparent)`,
      }}>
        {case_.status}
      </span>

      {/* Urgency */}
      <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", width: 50, textAlign: "right" }}>
        {case_.urgency_score.toFixed(1)}
      </span>
    </div>
  );
}

/* ── Case Detail Panel ────────────────────────────────────────── */

type ConfirmState = { label: string; action: () => Promise<any> } | null;

function CaseDetailPanel({
  caseId, width, onClose, onUpdate,
}: { caseId: string; width: number; onClose: () => void; onUpdate: () => void }) {
  // One request instead of five. The old shape had a genuine waterfall — the
  // case type could not be fetched until the case returned its case_type_id.
  const { data: detail, refetch } = useApi(() => getCaseDetail(caseId), [caseId]);
  const caseData     = detail?.case ?? null;
  const history      = detail?.history ?? null;
  const slaData      = detail?.slas ?? null;
  const caseTypeData = detail?.case_type ?? null;
  const myTask       = detail?.my_task ?? null;
  // The task pane refetches after a claim/complete; re-reading the whole
  // aggregate is one request and keeps every pane consistent with it.
  const refetchMyTask = refetch;
  const [tab, setTab] = useState<"my-task" | "stages" | "details" | "timeline" | "sla" | "sharing" | "sessions" | "messages" | "ask" | "risk" | "ai" | "captured" | "events" | "move-tenant" | "legal-hold">("stages");
  // Offered only to holders of `case.actions.transfer_tenant`, as the server
  // answers it — never inferred from a role name.
  const mayTransferTenant = useMayTransferTenant();
  // Whether the case is under a legal hold, and what the server lets this
  // caller do about it. `null` (loading, or unreadable) shows nothing.
  const { status: legalHold, reload: reloadLegalHold } = useCaseLegalHold(caseId);
  const showLegalHoldTab = !!legalHold && (legalHold.held || legalHold.can_read || legalHold.can_place);
  const [confirm, setConfirm] = useState<ConfirmState>(null);
  const [actionBusy, setActionBusy] = useState(false);

  // Which actions this case's STAGE offers.
  //
  // `null` means "not known yet, or the catalogue could not be read", and it
  // reads as *offer everything* — the behaviour of every install before a stage
  // was ever configured. A failed lookup must not empty an operator's action
  // bar; the dispatcher is the gate, this is only the courtesy of not showing a
  // button that would be refused.
  const [stageActions, setStageActions] = useState<string[] | null>(null);
  useEffect(() => {
    let live = true;
    setStageActions(null);
    caseActionCatalogue(undefined, caseId)
      .then(d => { if (live) setStageActions((d.actions ?? []).map(a => a.name)); })
      .catch(() => { if (live) setStageActions(null); });
    return () => { live = false; };
  }, [caseId, detail?.case?.current_stage_id]);
  const offers = (name: string) => stageActions === null || stageActions.includes(name);

  // Switch to "My Task" tab the first time a task loads for this case
  const switchedToTask = useRef(false);
  useEffect(() => {
    switchedToTask.current = false;
  }, [caseId]);
  useEffect(() => {
    if (myTask && !switchedToTask.current) {
      setTab("my-task");
      switchedToTask.current = true;
    }
  }, [myTask]);

  if (!caseData) return (
    <div style={{ width, background: "var(--bg-panel)", padding: "var(--space-xl)", display: "flex", justifyContent: "center" }}>
      <Spinner />
    </div>
  );

  const requestConfirm = (label: string, action: () => Promise<any>) => setConfirm({ label, action });

  const executeConfirm = async () => {
    if (!confirm || actionBusy) return;
    setActionBusy(true);
    try {
      await confirm.action();
      refetch();
      onUpdate();
    } finally {
      setActionBusy(false);
      setConfirm(null);
    }
  };

  return (
    <div style={{
      width, flexShrink: 0, background: "var(--bg-panel)",
      overflow: "hidden", display: "flex", flexDirection: "column",
    }}>
      {/* Header */}
      <div style={{ padding: "var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", flexShrink: 0 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            {/* Fix 8: Show human-readable case number */}
            <div style={{ fontSize: 14, fontFamily: "var(--font-mono)", fontWeight: 700, color: "var(--accent)", letterSpacing: "0.05em" }}>
              {(caseData as any).case_number ?? caseData.id.slice(0, 8).toUpperCase()}
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: 2 }}>
              {caseData.id.slice(0, 8)}
            </div>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose}>✕</Button>
        </div>

        {/* Status + Priority */}
        <div style={{ display: "flex", gap: "var(--space-sm)", marginTop: "var(--space-md)", flexWrap: "wrap" }}>
          <StatusPill status={caseData.status} />
          <PriorityPill priority={caseData.priority} />
          <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", padding: "3px 8px" }}>
            urgency: {caseData.urgency_score.toFixed(1)}
          </span>
        </div>

        {/* Actions — Fix 6: confirmation before executing, busy state prevents double-click */}
        <div style={{ display: "flex", gap: "var(--space-xs)", marginTop: "var(--space-md)", flexWrap: "wrap" }}>
          {caseData.status === "new" && offers("open") && (
            <Button size="sm" disabled={actionBusy} onClick={() => requestConfirm("Open this case?", () => changeCaseStatus(caseId, "open"))}>
              Open
            </Button>
          )}
          {["open", "reopened"].includes(caseData.status) && (offers("resolve") || offers("cancel")) && (
            <>
              {offers("resolve") && (
                <Button size="sm" disabled={actionBusy} onClick={() => requestConfirm("Resolve this case?", () => resolveCase(caseId))}>
                  Resolve
                </Button>
              )}
              {offers("cancel") && (
                <Button size="sm" variant="danger" disabled={actionBusy} onClick={() => requestConfirm("Cancel this case?", () => cancelCase(caseId))}>
                  Cancel
                </Button>
              )}
            </>
          )}
          {/* Fix 7: Reopen available from both resolved AND closed/cancelled */}
          {caseData.status === "resolved" && (offers("close") || offers("reopen")) && (
            <>
              {offers("close") && (
                <Button size="sm" disabled={actionBusy} onClick={() => requestConfirm("Close this case?", () => closeCase(caseId))}>
                  Close
                </Button>
              )}
              {offers("reopen") && (
                <Button size="sm" variant="secondary" disabled={actionBusy} onClick={() => requestConfirm("Reopen this case?", () => reopenCase(caseId))}>
                  Reopen
                </Button>
              )}
            </>
          )}
          {["closed", "cancelled"].includes(caseData.status) && offers("reopen") && (
            <Button size="sm" variant="secondary" disabled={actionBusy} onClick={() => requestConfirm("Reopen this case?", () => reopenCase(caseId))}>
              Reopen
            </Button>
          )}
        </div>

        {/* Fix 6: Inline confirmation bar */}
        {confirm && (
          <div style={{
            marginTop: "var(--space-md)", padding: "10px 14px",
            background: "color-mix(in srgb, var(--accent) 8%, transparent)",
            border: "1px solid var(--accent)", borderRadius: "var(--radius-sm)",
            display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
          }}>
            <span style={{ fontSize: 13, color: "var(--text-primary)", fontWeight: 500 }}>{confirm.label}</span>
            <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
              <button disabled={actionBusy} onClick={executeConfirm}
                style={{ padding: "5px 14px", background: "var(--accent)", color: "#fff", border: "none",
                  borderRadius: "var(--radius-sm)", fontSize: 12, fontWeight: 600, cursor: "pointer" }}>
                {actionBusy ? "…" : "Confirm"}
              </button>
              <button onClick={() => setConfirm(null)}
                style={{ padding: "5px 14px", background: "transparent", color: "var(--text-secondary)",
                  border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 12, cursor: "pointer" }}>
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      {legalHold?.held && (
        <div style={{ padding: "0 var(--space-lg) var(--space-sm)" }}>
          <LegalHoldBanner status={legalHold} />
        </div>
      )}

      {/* Tabs */}
      <Tabs
        ariaLabel="Case"
        tabs={([
          myTask ? { key: "my-task" as const,  label: "⚡ My Task" } : null,
          { key: "stages"   as const, label: "Stages" },
          { key: "details"  as const, label: "Details" },
          { key: "timeline" as const, label: "Timeline" },
          { key: "sla"      as const, label: "SLA" },
          { key: "risk"     as const, label: "⚠ Risk" },
          { key: "ai"       as const, label: "AI" },
          { key: "captured" as const, label: "✦ Captured" },
          { key: "events"   as const, label: "Events" },
          { key: "sharing"  as const, label: "Sharing" },
          { key: "sessions" as const, label: "HxMeet" },
          { key: "messages" as const, label: "Messages" },
          { key: "ask"      as const, label: "✦ Ask" },
          mayTransferTenant ? { key: "move-tenant" as const, label: "Move tenant" } : null,
          showLegalHoldTab ? { key: "legal-hold" as const,
                               label: legalHold?.held ? "⚖ Legal hold" : "Legal hold" } : null,
        ].filter(Boolean) as Array<{ key: typeof tab; label: string }>)}
        value={tab}
        onChange={setTab}
      />

      {/* Tab content */}
      <div style={{ flex: 1, overflow: "auto", padding: "var(--space-lg)" }}>
        {tab === "my-task" && myTask && (
          <MyTaskTab
            task={myTask}
            onComplete={async () => {
              await refetchMyTask();
              refetch();
              onUpdate();
              setTab("stages");
            }}
            onCancel={async () => {
              await unlockStep(caseId, myTask.step_id);
              await refetchMyTask();
              setTab("stages");
            }}
          />
        )}
        {tab === "stages" && (
          <StagesTab
            case_={caseData}
            caseType={caseTypeData as any}
            onStageTransition={async (stageId) => {
              await transitionStage(caseId, stageId);
              refetch();
              onUpdate();
            }}
            onRefresh={() => { refetch(); onUpdate(); }}
            onStatusChange={async (status) => {
              if (status === "open") await changeCaseStatus(caseId, "open");
              else if (status === "resolved") await resolveCase(caseId);
              else if (status === "closed") await closeCase(caseId);
              refetch();
              onUpdate();
            }}
          />
        )}
        {tab === "details" && <DetailsTab case_={caseData} onPriorityChange={async (p) => { await changeCasePriority(caseId, p); refetch(); onUpdate(); }} />}
        {tab === "timeline" && <TimelineTab entries={history || []} />}
        {tab === "sla" && <SLATab slas={slaData || []} />}
        {tab === "sharing" && <SharingTab caseId={caseId} />}
        {tab === "sessions" && <SessionsTab caseId={caseId} />}
        {tab === "ask" && <AskTab caseId={caseId} />}
        {tab === "risk" && <RiskTab caseId={caseId} onResolved={() => { refetch(); onUpdate(); }} />}
        {tab === "messages" && <MessagesTab caseId={caseId} />}
        {tab === "ai" && <AIProposals caseId={caseId} />}
        {tab === "captured" && <CapturedDigest caseId={caseId} />}
        {tab === "events" && <CaseEventStream caseId={caseId} />}
        {tab === "move-tenant" && <MoveTenantTab caseId={caseId} />}
        {tab === "legal-hold" && (
          <LegalHoldTab caseId={caseId} status={legalHold}
                        onChanged={() => { reloadLegalHold(); refetch(); }} />
        )}
      </div>
    </div>
  );
}

/* ── Sharing Tab (HxGuard Phase B) ───────────────────────────── */

function SharingTab({ caseId }: { caseId: string }) {
  const [shares, setShares] = useState<{ user_id: string; relation: string; username?: string | null; display_name?: string | null; created_by: string | null; created_at: string | null }[]>([]);
  const [userId, setUserId] = useState("");
  const [relation, setRelation] = useState("viewer");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await listCaseShares(caseId);
      setShares(r);
      setMsg(null);
    } catch (e: any) { setMsg(e?.message || "Could not load shares"); }
  }, [caseId]);
  useEffect(() => { load(); }, [load]);

  const add = async () => {
    if (!userId.trim()) return;
    setBusy(true);
    try {
      await shareCase(caseId, userId.trim(), relation);
      setUserId(""); await load();
    } catch (e: any) { setMsg(e?.message || "Share failed"); }
    finally { setBusy(false); }
  };

  const inp: React.CSSProperties = { padding: "6px 10px", fontSize: 12, border: "1px solid var(--border-subtle)", borderRadius: 6, background: "var(--bg-input)", color: "var(--text-primary)" };
  return (
    <div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
        Direct access to this case. Assignees appear automatically; viewers/editors are explicit shares.
      </div>
      {shares.map((s, i) => (
        <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", borderBottom: "1px solid var(--border-subtle)", fontSize: 12 }}>
          <code style={{ flex: 1 }} title={s.user_id}>{s.display_name || s.username || s.user_id}</code>
          <span style={{ color: s.relation === "editor" ? "var(--accent)" : "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: 11 }}>{s.relation}</span>
          {s.relation !== "assignee" && (
            <button onClick={async () => { try { await unshareCase(caseId, s.user_id, s.relation); await load(); } catch (e: any) { setMsg(e?.message || "Remove failed"); } }}
              style={{ background: "none", border: "none", color: "var(--status-failed)", cursor: "pointer", fontSize: 12 }}>✕</button>
          )}
        </div>
      ))}
      {shares.length === 0 && <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "8px 0" }}>No direct access entries.</div>}
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <input style={{ ...inp, flex: 1 }} placeholder="user id, username, or email" value={userId} onChange={(e) => setUserId(e.target.value)} />
        <select style={inp} value={relation} onChange={(e) => setRelation(e.target.value)}>
          <option value="viewer">viewer</option>
          <option value="editor">editor</option>
        </select>
        <button onClick={add} disabled={busy || !userId.trim()}
          style={{ ...inp, cursor: "pointer", color: "var(--accent)", fontWeight: 700 }}>Share</button>
      </div>
      {msg && <div style={{ fontSize: 11, color: "var(--status-failed)", marginTop: 8 }}>{msg}</div>}
    </div>
  );
}

/* ── Messages Tab (Portal v2 P4 — worker ↔ customer thread) ──── */

function MessagesTab({ caseId }: { caseId: string }) {
  const [messages, setMessages] = useState<import("@shared/api/client").CaseMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [internal, setInternal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setMessages((await listCaseMessages(caseId)).messages); setMsg(null); }
    catch (e: any) { setMsg(e?.message || "Could not load messages"); }
  }, [caseId]);

  useEffect(() => { load(); }, [load]);

  const send = async () => {
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    try { await postCaseMessage(caseId, text, !internal); setDraft(""); await load(); }
    catch (e: any) { setMsg(e?.message || "Could not send"); }
    finally { setBusy(false); }
  };

  const inp: React.CSSProperties = { padding: "8px 10px", fontSize: 12, border: "1px solid var(--border-subtle)", borderRadius: 6, background: "var(--bg-input)", color: "var(--text-primary)" };

  return (
    <div style={{ padding: "var(--space-md)", display: "flex", flexDirection: "column", gap: 10, height: "100%" }}>
      <div style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: 8 }}>
        {messages.length === 0 && (
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            No messages yet. Portal-visible messages reach the customer (and email them if they opted in).
          </div>
        )}
        {messages.map((m) => {
          const fromCustomer = m.author.startsWith("customer:");
          return (
            <div key={m.id} style={{
              alignSelf: fromCustomer ? "flex-start" : "flex-end", maxWidth: "85%",
              padding: "8px 12px", borderRadius: 10, fontSize: 12, lineHeight: 1.5,
              background: fromCustomer ? "var(--bg-secondary)" : (m.portal_visible ? "var(--accent-soft, rgba(102,204,255,.12))" : "var(--bg-secondary)"),
              border: m.portal_visible ? "1px solid var(--border-subtle)" : "1px dashed var(--border-subtle)",
            }}>
              <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 3 }}>
                {m.author_name || m.author}
                {!m.portal_visible && " · internal note"}
                {m.created_at ? ` · ${new Date(m.created_at).toLocaleString()}` : ""}
              </div>
              {m.body}
            </div>
          );
        })}
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
        <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={2}
          placeholder={internal ? "Internal note (never shown to the customer)…" : "Message the customer…"}
          style={{ ...inp, flex: 1, resize: "vertical" }} />
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <label style={{ fontSize: 10, color: "var(--text-muted)", display: "flex", gap: 4, alignItems: "center", whiteSpace: "nowrap" }}>
            <input type="checkbox" checked={internal} onChange={(e) => setInternal(e.target.checked)} />
            internal
          </label>
          <button onClick={send} disabled={busy || !draft.trim()}
            style={{ ...inp, cursor: "pointer", color: "var(--accent)", fontWeight: 600, opacity: busy || !draft.trim() ? 0.5 : 1 }}>
            Send
          </button>
        </div>
      </div>
      {msg && <div style={{ fontSize: 11, color: "var(--status-failed)" }}>{msg}</div>}
    </div>
  );
}

/* ── Ask Tab (HxNexus case-scoped Q&A) ───────────────────────── */

// ── HxSentinel risk panel ────────────────────────────────────────────────────
// Recommendations with cited evidence — never a verdict. Shows the engine's
// findings and any open verify-gate hold; a worker clears a hold (with a note)
// to release the case's stage advance.

interface RiskFinding {
  id: string; rule_key: string; severity: string; title: string; message: string;
  evidence: any; status: string;
}
interface RiskHold {
  id: string; rule_key: string; severity: string; status: string;
  resolved_by?: string | null; resolution_note?: string | null;
}

const SEV_COLOR: Record<string, string> = {
  high: "var(--status-failed)", medium: "var(--status-warning, #d98c00)", low: "var(--text-muted)",
};

function SevBadge({ severity }: { severity: string }) {
  return (
    <span style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em",
      padding: "1px 8px", borderRadius: 8, color: "#fff",
      background: SEV_COLOR[severity] || "var(--text-muted)" }}>
      {severity}
    </span>
  );
}

// Per-finding 1–5 score + note (P2b). Scoring requires having seen the evidence,
// so it lives on the finding. Shows your last score and its outcome cross-check.
interface ScoreEntry { id: string; reviewer: string; score: number; note?: string | null; calibration?: string }

function FindingFeedback({ findingId }: { findingId: string }) {
  const [latest, setLatest] = useState<{ score: number; calibration?: string } | null>(null);
  const [history, setHistory] = useState<ScoreEntry[]>([]);
  const [score, setScore] = useState(0);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [showScores, setShowScores] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = async () => {
    try {
      const r = await apiFetch(`/risk/findings/${findingId}/feedback`);
      if (r.ok) { const b = await r.json(); setLatest(b.latest); setHistory(b.history || []); }
    } catch { /* non-fatal */ }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [findingId]);

  // P3b: a supervisor disputes a specific score (excludes it from reliability +
  // training). The API enforces risk.scores.review; a non-supervisor is refused.
  const disputeScore = async (feedbackId: string) => {
    const reason = window.prompt("Why dispute this score? (required)");
    if (!reason || !reason.trim()) return;
    try {
      const r = await apiFetch(`/risk/feedback/${feedbackId}/dispute`, {
        method: "POST", body: JSON.stringify({ note: reason.trim() }) });
      if (!r.ok) { setErr((await errorFromResponse(r, "Dispute failed")).message); return; }
      setErr(null);
      await load();
    } catch (e: any) { setErr(e?.message || "Dispute failed"); }
  };

  const submit = async () => {
    if (!score || busy) return;
    setBusy(true);
    try {
      const r = await apiFetch(`/risk/findings/${findingId}/feedback`, {
        method: "POST", body: JSON.stringify({ score, note: note.trim() || null }) });
      // A silently dropped score is worse than a visible failure: these rows are
      // the ground truth that rule tuning and the local model train on, so a
      // no-op here shows up much later as "analyze produced nothing".
      if (!r.ok) { setErr((await errorFromResponse(r, "Could not save your score")).message); return; }
      setErr(null); setOpen(false); setNote(""); setScore(0); await load();
    } catch (e: any) { setErr(e?.message || "Could not save your score"); }
    finally { setBusy(false); }
  };

  const calColor = latest?.calibration === "miscalibrated" ? "var(--status-failed)"
    : latest?.calibration === "ok" ? "var(--accent)" : "var(--text-muted)";

  return (
    <div style={{ marginTop: 6, borderTop: "1px dashed var(--border-subtle)", paddingTop: 6 }}>
      {err && (
        <div style={{ color: "var(--status-failed)", fontSize: 11, marginBottom: 4 }}>{err}</div>
      )}
      {!open ? (
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11 }}>
          <span style={{ color: "var(--text-muted)" }}>
            {latest ? `Your score: ${latest.score}/5` : "Rate this analysis"}
          </span>
          {latest?.calibration && latest.calibration !== "undetermined" && (
            <span style={{ color: calColor, fontWeight: 600 }}>· {latest.calibration}</span>
          )}
          <button onClick={() => setOpen(true)} style={{ fontSize: 11, background: "transparent",
            border: "none", color: "var(--accent)", cursor: "pointer" }}>
            {latest ? "re-score" : "score"}
          </button>
          {history.length > 0 && (
            <button onClick={() => setShowScores((s) => !s)} style={{ fontSize: 11, background: "transparent",
              border: "none", color: "var(--text-muted)", cursor: "pointer" }}>
              {showScores ? "hide scores" : `all scores (${history.length})`}
            </button>
          )}
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>1=bad … 5=best</span>
            {[1, 2, 3, 4, 5].map((n) => (
              <button key={n} onClick={() => setScore(n)} style={{ width: 24, height: 24,
                borderRadius: 6, fontSize: 12, cursor: "pointer",
                border: "1px solid var(--border-default)",
                background: score === n ? "var(--accent)" : "var(--bg-secondary)",
                color: score === n ? "#fff" : "var(--text-secondary)" }}>{n}</button>
            ))}
          </div>
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note (optional)"
            style={{ fontSize: 12, padding: 6, borderRadius: 6, border: "1px solid var(--border-default)",
              background: "var(--bg-secondary)", color: "var(--text-primary)" }} />
          <div style={{ display: "flex", gap: 6 }}>
            <Button size="sm" disabled={!score || busy} onClick={submit}>Save score</Button>
            <Button size="sm" variant="secondary" onClick={() => setOpen(false)}>Cancel</Button>
          </div>
        </div>
      )}
      {showScores && !open && (
        <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 4 }}>
          {history.map((h) => (
            <div key={h.id} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11,
              color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
              <span>{h.reviewer}</span>
              <span style={{ fontWeight: 700, color: "var(--text-secondary)" }}>{h.score}/5</span>
              {h.calibration && h.calibration !== "undetermined" && (
                <span style={{ color: h.calibration === "miscalibrated" ? "var(--status-failed)" : "var(--accent)" }}>
                  {h.calibration}
                </span>
              )}
              {h.note && <span style={{ fontStyle: "italic" }}>“{h.note}”</span>}
              <button onClick={() => disputeScore(h.id)} title="Dispute this score (supervisor)"
                style={{ marginLeft: "auto", fontSize: 10, background: "transparent",
                  border: "1px solid var(--border-subtle)", borderRadius: 6, padding: "1px 6px",
                  color: "var(--text-muted)", cursor: "pointer" }}>dispute</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

interface OutcomeLabel { key: string; display: string; description?: string | null; is_negative: boolean; is_core: boolean }
interface OutcomeRow { outcome: string; note?: string | null; source?: string | null; labelled_by?: string | null; created_at?: string | null }

/**
 * Confirmed-outcome labelling — HxSentinel's ground truth.
 *
 * `case_outcomes` is append-only: relabelling inserts a row, and the history is
 * evidence in its own right (a case cleared today and confirmed fraud in six
 * months is a different story from one labelled fraud at closure). So this
 * shows the whole trail and says "relabel", never "edit".
 *
 * Labelling is not restricted to closed cases on purpose — outcomes are usually
 * confirmed long after closure, and retroactive labelling is the point.
 */
function OutcomeCard({ caseId }: { caseId: string }) {
  const [labels, setLabels] = useState<OutcomeLabel[]>([]);
  const [current, setCurrent] = useState<OutcomeRow | null>(null);
  const [history, setHistory] = useState<OutcomeRow[]>([]);
  const [choice, setChoice] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [openForm, setOpenForm] = useState(false);

  const load = async () => {
    const t = await apiFetch("/risk/taxonomy");
    if (t.ok) setLabels((await t.json()).labels || []);
    const r = await apiFetch(`/risk/cases/${caseId}/outcome`);
    if (r.ok) { const b = await r.json(); setCurrent(b.current); setHistory(b.history || []); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [caseId]);

  const save = async () => {
    if (!choice) return;
    setBusy(true);
    try {
      const r = await apiFetch(`/risk/cases/${caseId}/outcome`, {
        method: "PUT", body: JSON.stringify({ outcome: choice, note: note.trim() || null }) });
      if (!r.ok) { setErr((await errorFromResponse(r, "Could not record the outcome")).message); return; }
      setErr(null); setOpenForm(false); setNote(""); setChoice("");
      await load();
    } catch (e: any) { setErr(e?.message || "Could not record the outcome"); }
    finally { setBusy(false); }
  };

  const display = (key: string) => labels.find((l) => l.key === key)?.display || key;

  return (
    <div style={{ border: "1px solid var(--border-subtle)", borderRadius: 8, padding: 12,
      display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <strong style={{ fontSize: 13 }}>Confirmed outcome</strong>
        {current ? (
          <span style={{ fontSize: 11, padding: "1px 8px", borderRadius: 8, color: "#fff",
            background: "var(--accent)" }}>{display(current.outcome)}</span>
        ) : (
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>not labelled</span>
        )}
        <button onClick={() => setOpenForm((v) => !v)} style={{ marginLeft: "auto", fontSize: 11,
          background: "transparent", border: "none", color: "var(--accent)", cursor: "pointer" }}>
          {openForm ? "Cancel" : current ? "Relabel" : "Label outcome"}
        </button>
      </div>
      <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
        This is the ground truth HxSentinel learns from. Rules and the local model compare new
        cases against confirmed outcomes — nothing downstream can correct a wrong label.
      </div>
      {err && <div style={{ color: "var(--status-failed)", fontSize: 12 }}>{err}</div>}

      {openForm && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <select value={choice} onChange={(e) => setChoice(e.target.value)}
            style={{ fontSize: 12, padding: "6px 8px", borderRadius: 6, border: "1px solid var(--border-default)",
              background: "var(--bg-secondary)", color: "var(--text-primary)" }}>
            <option value="">Select an outcome…</option>
            {labels.map((l) => (
              <option key={l.key} value={l.key}>{l.display}{l.is_core ? "" : " (tenant)"}</option>
            ))}
          </select>
          {choice && (
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {labels.find((l) => l.key === choice)?.description}
            </div>
          )}
          <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2}
            placeholder="How was this confirmed? (optional, but it is the evidence)"
            style={{ fontSize: 12, padding: 8, borderRadius: 6, border: "1px solid var(--border-default)",
              background: "var(--bg-secondary)", color: "var(--text-primary)", resize: "vertical" }} />
          <div>
            <Button size="sm" disabled={!choice || busy} onClick={save}>
              {busy ? "Recording…" : "Record outcome"}
            </Button>
          </div>
        </div>
      )}

      {history.length > 1 && (
        <details>
          <summary style={{ fontSize: 11, color: "var(--text-muted)", cursor: "pointer" }}>
            Label history ({history.length})
          </summary>
          <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 6 }}>
            {history.map((h, i) => (
              <div key={i} style={{ fontSize: 11, color: "var(--text-secondary)" }}>
                <span style={{ fontWeight: 600 }}>{display(h.outcome)}</span>
                {h.created_at && <span style={{ color: "var(--text-muted)" }}> · {new Date(h.created_at).toLocaleString()}</span>}
                {h.labelled_by && <span style={{ color: "var(--text-muted)" }}> · {h.labelled_by}</span>}
                {h.note && <div style={{ color: "var(--text-muted)", paddingLeft: 8 }}>{h.note}</div>}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function RiskTab({ caseId, onResolved }: { caseId: string; onResolved: () => void }) {
  const [findings, setFindings] = useState<RiskFinding[]>([]);
  const [holds, setHolds] = useState<RiskHold[]>([]);
  const [loading, setLoading] = useState(true);
  const [off, setOff] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const load = async () => {
    setLoading(true); setErr(null);
    try {
      const fr = await apiFetch(`/risk/cases/${caseId}/findings`);
      if (fr.status === 404) {
        // Case-scoped routes answer 404 with the same code whether the module
        // is dark or the case simply isn't visible to you — that ambiguity is
        // deliberate (it stops the surface being used to probe for cases). So
        // ask the public flag endpoint which one it actually is instead of
        // blaming a feature flag for what may be an authorization result.
        const flags = await apiFetch("/releases/flags")
          .then((f) => (f.ok ? f.json() : null))
          .catch(() => null);
        if (flags?.hxsentinel) setErr("No risk data for this case, or it is not visible to you.");
        else setOff(true);
        setLoading(false);
        return;
      }
      if (!fr.ok) throw await errorFromResponse(fr, "Failed to load risk");
      setFindings((await fr.json()).findings || []);
      const hr = await apiFetch(`/risk/cases/${caseId}/holds`);
      if (hr.ok) setHolds((await hr.json()).holds || []);
      else setErr("Verify-gate holds could not be loaded — this list may be incomplete.");
    } catch (e: any) { setErr(e?.message || "Failed to load risk"); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [caseId]);

  const clearHold = async (holdId: string, status: "cleared" | "dismissed") => {
    const note = (notes[holdId] || "").trim();
    if (!note) { setErr("A note is required to clear a hold."); return; }
    setBusy(holdId); setErr(null);
    try {
      const r = await apiFetch(`/risk/holds/${holdId}/resolve`, {
        method: "POST", body: JSON.stringify({ status, note }) });
      if (!r.ok) throw await errorFromResponse(r, "Resolve failed");
      await load();
      onResolved();
    } catch (e: any) { setErr(e?.message || "Resolve failed"); }
    finally { setBusy(null); }
  };

  if (loading) return <div style={{ color: "var(--text-muted)", fontSize: 12 }}>Evaluating…</div>;
  if (off) return (
    <div style={{ color: "var(--text-muted)", fontSize: 12, maxWidth: 620 }}>
      HxSentinel is not enabled on this deployment. Risk recommendations appear here once it is on.
    </div>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, maxWidth: 760 }}>
      <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
        Deterministic recommendations with cited evidence — never a verdict. A human reviews and decides.
      </div>
      {err && <div style={{ color: "var(--status-failed)", fontSize: 12 }}>{err}</div>}

      <OutcomeCard caseId={caseId} />

      {holds.filter((h) => h.status === "open").map((h) => (
        <div key={h.id} style={{ border: "1px solid var(--status-failed)", borderRadius: 8,
          padding: 12, display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <SevBadge severity={h.severity} />
            <strong style={{ fontSize: 13 }}>Stage advance is held</strong>
            <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{h.rule_key}</span>
          </div>
          <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
            A verification is required before this case can advance. Record what you checked, then clear it.
          </div>
          <textarea value={notes[h.id] || ""} onChange={(e) => setNotes((n) => ({ ...n, [h.id]: e.target.value }))}
            placeholder="What did you verify? (required)" rows={2}
            style={{ fontSize: 12, padding: 8, borderRadius: 6, border: "1px solid var(--border-default)",
              background: "var(--bg-secondary)", color: "var(--text-primary)", resize: "vertical" }} />
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" disabled={busy === h.id} onClick={() => clearHold(h.id, "cleared")}>Clear hold</Button>
            <Button size="sm" variant="secondary" disabled={busy === h.id} onClick={() => clearHold(h.id, "dismissed")}>Dismiss</Button>
          </div>
        </div>
      ))}

      {findings.length === 0 && (
        <div style={{ color: "var(--text-muted)", fontSize: 12 }}>No risk findings for this case.</div>
      )}
      {findings.map((f) => (
        <div key={f.id} style={{ border: "1px solid var(--border-subtle)", borderRadius: 8, padding: 12,
          display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <SevBadge severity={f.severity} />
            <strong style={{ fontSize: 13 }}>{f.title}</strong>
            <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{f.rule_key}</span>
          </div>
          <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.5 }}>{f.message}</div>
          {Array.isArray(f.evidence?.matched_cases) && f.evidence.matched_cases.length > 0 && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 2 }}>
              {f.evidence.matched_cases.map((m: any, i: number) => (
                <span key={i} title={m.outcome} style={{ fontSize: 10, padding: "1px 7px", borderRadius: 8,
                  border: "1px solid var(--border-subtle)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  {m.case_number} · {m.outcome}
                </span>
              ))}
            </div>
          )}
          {Array.isArray(f.evidence?.neighbours) && f.evidence.neighbours.length > 0 && (
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 2 }}>
              {f.evidence.neighbours.map((n: any, i: number) => (
                <span key={i} title={`shares: ${(n.shared_signals || []).join(", ")}`}
                  style={{ fontSize: 10, padding: "1px 7px", borderRadius: 8,
                    border: "1px solid var(--border-subtle)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  {n.case_number} · {n.outcome} · {n.shared_count} shared
                </span>
              ))}
            </div>
          )}
          <FindingFeedback findingId={f.id} />
        </div>
      ))}
    </div>
  );
}

function AskTab({ caseId }: { caseId: string }) {
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<{ q: string; r: CaseAskResult }[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const ask = async () => {
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true); setErr(null);
    try {
      const r = await askCase(caseId, q);
      setHistory((h) => [...h, { q, r }]);
      setQuestion("");
    } catch (e: any) { setErr(e?.message || "Ask failed"); }
    finally { setBusy(false); }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 760 }}>
      <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
        Answers come from this case only — variables, timeline, messages, documents,
        verifications, and sealed transcripts (if you may view them). Assistive, not a verdict.
      </div>

      {history.map((h, i) => (
        <div key={i} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ alignSelf: "flex-end", maxWidth: "85%", padding: "8px 12px",
            borderRadius: 10, background: "var(--accent)", color: "#fff", fontSize: 13 }}>
            {h.q}
          </div>
          <div style={{ alignSelf: "flex-start", maxWidth: "90%", padding: "10px 12px",
            borderRadius: 10, background: "var(--bg-secondary)",
            border: "1px solid var(--border-subtle)", fontSize: 13, lineHeight: 1.55,
            whiteSpace: "pre-wrap" }}>
            {h.r.answer}
            <div style={{ marginTop: 8, display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
              {h.r.sources.map((s) => (
                <span key={s.sid} title={s.label} style={{ fontSize: 10, padding: "1px 7px",
                  borderRadius: 8, border: "1px solid var(--border-subtle)",
                  color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  {s.sid} {s.kind}
                </span>
              ))}
              <span style={{ fontSize: 10, fontWeight: 700,
                color: h.r.external_ai ? "var(--status-failed)" : "var(--accent)" }}>
                {h.r.external_ai ? "⚠ processed by external AI (tenant consent)" : "🔒 processed locally"}
              </span>
            </div>
            {h.r.withheld.length > 0 && (
              <div style={{ marginTop: 4, fontSize: 10, color: "var(--text-muted)" }}>
                Withheld: {h.r.withheld.join("; ")}
              </div>
            )}
          </div>
        </div>
      ))}

      {err && <div style={{ fontSize: 12, color: "var(--status-failed)" }}>{err}</div>}

      <div style={{ display: "flex", gap: 8 }}>
        <input value={question} onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ask(); }}
          placeholder="Ask anything about this case…"
          style={{ flex: 1, padding: "9px 12px", border: "1px solid var(--border-default)",
            borderRadius: "var(--radius-sm)", background: "var(--bg-input)",
            color: "var(--text-primary)", fontSize: 13 }} />
        <VoiceInput size={34} caseId={caseId} disabled={busy}
          onText={(t) => setQuestion((q) => appendTranscript(q, t))} />
        <button onClick={ask} disabled={busy || !question.trim()}
          style={{ padding: "9px 18px", border: "none", borderRadius: "var(--radius-sm)",
            background: "var(--accent)", color: "#fff", fontSize: 13, fontWeight: 600,
            cursor: busy ? "wait" : "pointer", opacity: busy || !question.trim() ? 0.6 : 1 }}>
          {busy ? "Thinking…" : "Ask"}
        </button>
      </div>
    </div>
  );
}

/* ── Sessions Tab (HxMeet P1) ────────────────────────────────── */

const PROVIDER_LABELS: Record<string, string> = {
  teams: "Teams", zoom: "Zoom", gmeet: "Google Meet", generic: "Meeting", livekit: "Velaris",
};

function SessionsTab({ caseId }: { caseId: string }) {
  const [sessions, setSessions] = useState<CaseSession[]>([]);
  const [providers, setProviders] = useState<MeetProvider[]>([]);
  const [driver, setDriver] = useState<string>("off_platform");
  const [provider, setProvider] = useState<string>("");
  const [title, setTitle] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [room, setRoom] = useState<{ url: string; token: string; session: CaseSession } | null>(null);
  const [record, setRecord] = useState(false);
  const [inviteFor, setInviteFor] = useState<string | null>(null);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteLink, setInviteLink] = useState<string | null>(null);
  // Sealed-recording in-app player (no download affordance — GDPR: the
  // decrypted stream stays in memory; every view is audit-logged server-side)
  const [playback, setPlayback] = useState<{ url: string; sessionId: string } | null>(null);
  const [playBusy, setPlayBusy] = useState<string | null>(null);
  // P4a — per-session intelligence (transcript + summary)
  const [intel, setIntel] = useState<Record<string, SessionIntelligence>>({});
  const [intelBusy, setIntelBusy] = useState<string | null>(null);

  const loadIntel = async (sessionId: string) => {
    try {
      const got = await getSessionIntelligence(sessionId);
      setIntel((m) => ({ ...m, [sessionId]: got }));
    } catch { /* not enabled / none — leave unset */ }
  };

  const analyze = async (sessionId: string) => {
    setIntelBusy(sessionId);
    try {
      await runSessionIntelligence(sessionId);
      setIntel((m) => ({ ...m, [sessionId]: { status: "running" } }));
      // Poll a few times — transcription runs in the background.
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const got = await getSessionIntelligence(sessionId);
        setIntel((m) => ({ ...m, [sessionId]: got }));
        if (got.status === "completed" || got.status === "failed") break;
      }
    } catch (e: any) { setMsg(e?.message || "Could not start analysis"); }
    finally { setIntelBusy(null); }
  };

  const openRecording = async (sessionId: string) => {
    setPlayBusy(sessionId);
    try { setPlayback({ url: await fetchRecordingUrl(sessionId), sessionId }); }
    catch (e: any) { setMsg(e?.message || "Could not load the recording"); }
    finally { setPlayBusy(null); }
  };

  // P4c — Video-KYC signal pass (risk score + per-check breakdown, assistive)
  const [kyc, setKyc] = useState<Record<string, KycAnalysis>>({});
  const [kycBusy, setKycBusy] = useState<string | null>(null);

  const startKyc = async (sessionId: string, force = false) => {
    setKycBusy(sessionId);
    try {
      if (!force) {
        const existing = await getKycAnalysis(sessionId);
        if (existing.status === "completed" || existing.status === "failed") {
          // First click shows what's already there; the panel offers Re-run.
          setKyc((m) => ({ ...m, [sessionId]: existing }));
          return;
        }
      }
      await runKycAnalysis(sessionId);
      setKyc((m) => ({ ...m, [sessionId]: { status: "running" } }));
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const got = await getKycAnalysis(sessionId);
        setKyc((m) => ({ ...m, [sessionId]: got }));
        if (got.status === "completed" || got.status === "failed") break;
      }
    } catch (e: any) { setMsg(e?.message || "Could not run the KYC analysis"); }
    finally { setKycBusy(null); }
  };

  // P4a-live-2 — sealed live transcript viewer
  const [transcripts, setTranscripts] = useState<Record<string, string>>({});
  const [transcriptOpen, setTranscriptOpen] = useState<string | null>(null);

  const openTranscript = async (sessionId: string) => {
    if (transcriptOpen === sessionId) { setTranscriptOpen(null); return; }
    try {
      if (!transcripts[sessionId]) {
        const text = await fetchSessionTranscript(sessionId);
        setTranscripts((m) => ({ ...m, [sessionId]: text }));
      }
      setTranscriptOpen(sessionId);
    } catch (e: any) { setMsg(e?.message || "Could not load the transcript"); }
  };

  const verifyTranscript = async (sessionId: string) => {
    try {
      const r = await verifySessionTranscript(sessionId);
      setMsg(r.verified
        ? `Transcript verified ✓ sha256 ${r.sha256?.slice(0, 16)}… matches the audit-chain seal`
        : `TRANSCRIPT VERIFICATION FAILED: ${r.reason || "hash mismatch"}`);
    } catch (e: any) { setMsg(e?.message || "Verification failed"); }
  };

  const closeRecording = () => {
    if (playback) URL.revokeObjectURL(playback.url);
    setPlayback(null);
  };

  const embedded = driver === "embedded";

  const load = useCallback(async () => {
    try {
      const r = await listCaseSessions(caseId);
      setSessions(r.sessions);
      setMsg(null);
    } catch (e: any) { setMsg(e?.message || "Could not load sessions"); }
  }, [caseId]);

  useEffect(() => {
    load();
    listMeetProviders()
      .then((r) => {
        setDriver(r.driver);
        setProviders(r.providers);
        const def = r.providers.find((p) => p.is_default) ?? r.providers[0];
        if (def) setProvider(def.provider);
      })
      .catch(() => setProviders([]));
  }, [load]);

  const joinEmbedded = async (s: CaseSession, skipConsentPrompt = false) => {
    // Consent is stamped server-side when the token is minted — the notice
    // must come first. The starter checked the record box deliberately.
    if (s.record_intent && !skipConsentPrompt &&
        !window.confirm("This session is recorded. Joining records your audio, video, and screen-share, and counts as your consent. Join?")) {
      return;
    }
    try {
      const t = await getMeetSessionToken(s.id);
      setRoom({ url: t.url, token: t.token, session: s });
    } catch (e: any) { setMsg(e?.message || "Could not join the session"); }
  };

  const start = async () => {
    setBusy(true);
    try {
      const s = await startCaseSession(caseId, {
        title: title.trim() || undefined,
        provider: embedded ? undefined : provider || undefined,
        ...(embedded && record ? { record: true } : {}),
      });
      setTitle("");
      if (s.driver === "embedded") await joinEmbedded(s, true);
      else if (s.join_url) window.open(s.join_url, "_blank", "noopener");
      await load();
    } catch (e: any) { setMsg(e?.message || "Could not start the session"); }
    finally { setBusy(false); }
  };

  const toggleRecording = async () => {
    if (!room) return;
    try {
      const s = room.session.recording_status === "recording"
        ? await stopMeetRecording(room.session.id)
        : await startMeetRecording(room.session.id);
      setRoom({ ...room, session: s });
      await load();
    } catch (e: any) { setMsg(e?.message || "Recording control failed"); }
  };

  const verifyRecording = async (id: string) => {
    try {
      const v = await verifyMeetRecording(id);
      setMsg(v.verified
        ? `Recording verified ✓ sha256 ${v.sha256?.slice(0, 16)}… matches the audit-chain seal.`
        : `Recording NOT verified: ${v.reason || "hash mismatch against the audit-chain seal"}`);
    } catch (e: any) { setMsg(e?.message || "Verification failed"); }
  };

  const end = async (id: string) => {
    try { await endCaseSession(id); await load(); }
    catch (e: any) { setMsg(e?.message || "Could not end the session"); }
  };

  const invite = async (sessionId: string) => {
    try {
      const inv = await inviteMeetGuest(sessionId, { email: inviteEmail.trim() });
      // The server's absolute link wins when the deployment has been told where
      // Studio lives: a host copying this out of the browser may be on an
      // internal address the guest cannot reach.
      setInviteLink(inv.join_url || `${window.location.origin}${inv.join_path}`);
      setInviteEmail("");
    } catch (e: any) { setMsg(e?.message || "Could not create the invite"); }
  };

  const inp: React.CSSProperties = { padding: "6px 10px", fontSize: 12, border: "1px solid var(--border-subtle)", borderRadius: 6, background: "var(--bg-input)", color: "var(--text-primary)" };
  const badge = (s: CaseSession): React.CSSProperties => ({
    fontFamily: "var(--font-mono)", fontSize: 10, textTransform: "uppercase", padding: "2px 6px", borderRadius: 4,
    color: s.status === "active" ? "var(--accent)" : "var(--text-muted)",
    border: `1px solid ${s.status === "active" ? "var(--accent)" : "var(--border-subtle)"}`,
  });

  return (
    <div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
        {embedded
          ? "Live sessions for this case, hosted in Velaris — voice, video, and screen-share in the browser. Sessions are not recorded."
          : "Live meetings for this case, created on your organisation's provider. The recording (if any) stays with that provider — it is not stored on the case."}
      </div>

      {!embedded && providers.length === 0 && (
        <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "8px 0" }}>
          No meeting provider configured. Add a Teams, Zoom, Google Meet, or generic
          meeting connector in HxBridge to enable case sessions.
        </div>
      )}

      {(embedded || providers.length > 0) && (
        <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
          <input style={{ ...inp, flex: 1 }} placeholder="session title (optional)"
            value={title} onChange={(e) => setTitle(e.target.value)} />
          {!embedded && (
            <select style={inp} value={provider} onChange={(e) => setProvider(e.target.value)}>
              {providers.map((p) => (
                <option key={p.connector_id} value={p.provider}>{PROVIDER_LABELS[p.provider] ?? p.provider}</option>
              ))}
            </select>
          )}
          {embedded && (
            <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--text-muted)", whiteSpace: "nowrap", cursor: "pointer" }}>
              <input type="checkbox" checked={record} onChange={(e) => setRecord(e.target.checked)} />
              record &amp; seal to case
            </label>
          )}
          <button onClick={start} disabled={busy}
            style={{ ...inp, cursor: "pointer", color: "var(--accent)", fontWeight: 700, whiteSpace: "nowrap" }}>
            {busy ? "Starting…" : "▶ Start meeting"}
          </button>
        </div>
      )}

      {sessions.map((s) => (
        <React.Fragment key={s.id}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 0", borderBottom: "1px solid var(--border-subtle)", fontSize: 12 }}>
          <span style={badge(s)}>{s.status}</span>
          <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-muted)" }}>
            {PROVIDER_LABELS[s.provider] ?? s.provider}
          </span>
          <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {s.title || "Case session"}
          </span>
          <span style={{ color: "var(--text-muted)", fontSize: 11 }}>
            {s.started_at ? new Date(s.started_at).toLocaleString() : ""}
          </span>
          {s.record_intent && s.recording_status && s.recording_status !== "none" && (
            <span style={{
              fontFamily: "var(--font-mono)", fontSize: 10, textTransform: "uppercase",
              padding: "2px 6px", borderRadius: 4,
              color: s.recording_status === "sealed" ? "var(--accent)" : s.recording_status === "failed" ? "var(--status-failed)" : "var(--text-muted)",
              border: "1px solid var(--border-subtle)",
            }}>
              {s.recording_status === "recording" ? "● rec" : s.recording_status}
            </span>
          )}
          {s.recording_status === "sealed" && (
            <>
              <button onClick={() => openRecording(s.id)} disabled={playBusy === s.id}
                style={{ background: "none", border: "none", color: "var(--accent)", fontWeight: 600, cursor: "pointer", fontSize: 12, opacity: playBusy === s.id ? 0.5 : 1 }}>
                {playBusy === s.id ? "Loading…" : "▶ Play"}
              </button>
              <button onClick={() => verifyRecording(s.id)}
                style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", fontSize: 12 }}>
                Verify
              </button>
            </>
          )}
          {s.transcript_status === "sealed" && (
            <button onClick={() => openTranscript(s.id)}
              style={{ background: "none", border: "none", color: "var(--accent)", fontWeight: 600, cursor: "pointer", fontSize: 12 }}>
              {transcriptOpen === s.id ? "Hide transcript" : "Transcript"}
            </button>
          )}
          {(s.recording_status === "sealed" || s.transcript_status === "sealed") && (
            <button onClick={() => (intel[s.id] ? loadIntel(s.id) : analyze(s.id))} disabled={intelBusy === s.id}
              style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", fontSize: 12, opacity: intelBusy === s.id ? 0.5 : 1 }}>
              {intelBusy === s.id ? "Analyzing…" : (intel[s.id]?.status === "completed" ? "Analysis" : "Analyze")}
            </button>
          )}
          {s.record_intent && (s.recording_status === "sealed" || s.transcript_status === "sealed") && (
            <button onClick={() => startKyc(s.id)} disabled={kycBusy === s.id}
              style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", fontSize: 12, opacity: kycBusy === s.id ? 0.5 : 1 }}>
              {kycBusy === s.id ? "Scanning…" : "KYC signals"}
            </button>
          )}
          {s.status === "active" && s.driver === "embedded" && (
            <>
              <button onClick={() => joinEmbedded(s)}
                style={{ background: "none", border: "none", color: "var(--accent)", fontWeight: 600, cursor: "pointer", fontSize: 12 }}>
                Join
              </button>
              <button onClick={() => { setInviteFor(inviteFor === s.id ? null : s.id); setInviteLink(null); }}
                style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 12 }}>
                Invite
              </button>
            </>
          )}
          {s.status === "active" && s.driver !== "embedded" && s.join_url && (
            <a href={s.join_url} target="_blank" rel="noopener noreferrer"
              style={{ color: "var(--accent)", fontWeight: 600, textDecoration: "none" }}>Join</a>
          )}
          {s.status === "active" && (
            <button onClick={() => end(s.id)}
              style={{ background: "none", border: "none", color: "var(--status-failed)", cursor: "pointer", fontSize: 12 }}>
              End
            </button>
          )}
        </div>

        {/* P4a-live-2 — sealed live transcript panel */}
        {transcriptOpen === s.id && transcripts[s.id] !== undefined && (
          <div style={{ padding: "10px 12px", margin: "6px 0 12px", borderRadius: 8,
            background: "var(--bg-secondary)", border: "1px solid var(--border-subtle)", fontSize: 12 }}>
            <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 6 }}>
              <span style={{ fontWeight: 700 }}>Sealed live transcript</span>
              <button onClick={() => verifyTranscript(s.id)}
                style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", fontSize: 11 }}>
                Verify seal
              </button>
              <span style={{ color: "var(--text-muted)", fontSize: 11 }}>
                tenant-key encrypted at rest · every view audited · assistive, may mis-hear
              </span>
            </div>
            <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontFamily: "var(--font-mono)",
              fontSize: 11, lineHeight: 1.6, maxHeight: 260, overflowY: "auto",
              color: "var(--text-secondary)" }}>
              {transcripts[s.id]}
            </pre>
          </div>
        )}

        {/* P4c — Video-KYC signal panel: risk score + per-check breakdown.
            Assistive framing throughout — a low score is never "verified". */}
        {kyc[s.id] && kyc[s.id].status !== "none" && (
          <div style={{ padding: "10px 12px", margin: "6px 0 12px", borderRadius: 8,
            background: "var(--bg-secondary)", border: "1px solid var(--border-subtle)", fontSize: 12 }}>
            {kyc[s.id].status === "running" && <span style={{ color: "var(--text-muted)" }}>Analyzing the sealed recording… this runs locally and can take a minute.</span>}
            {kyc[s.id].status === "failed" && <span style={{ color: "var(--status-failed)" }}>KYC analysis failed: {kyc[s.id].error || "unknown error"}</span>}
            {kyc[s.id].status === "completed" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                  <span style={{ fontWeight: 700 }}>KYC signal pass</span>
                  {kyc[s.id].risk_score != null ? (
                    <span style={{
                      fontWeight: 700, padding: "2px 10px", borderRadius: 6,
                      color: kyc[s.id].review_recommended ? "var(--status-failed)" : "var(--text-primary)",
                      border: `1px solid ${kyc[s.id].review_recommended ? "var(--status-failed)" : "var(--border-subtle)"}`,
                    }}>
                      risk {(kyc[s.id].risk_score! * 100).toFixed(0)}%
                      {kyc[s.id].review_recommended ? " — review recommended" : ""}
                    </span>
                  ) : (
                    <span style={{ color: "var(--text-muted)" }}>no check could produce a score</span>
                  )}
                  <button onClick={() => startKyc(s.id, true)} disabled={kycBusy === s.id}
                    style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", fontSize: 11 }}>
                    Re-run
                  </button>
                  <span style={{ color: "var(--text-muted)", fontSize: 11 }}>
                    assistive, not a verdict — the worker decides
                  </span>
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {[...(kyc[s.id].checks || []), ...(kyc[s.id].challenge_checks || [])].map((c: KycCheck, i: number) => (
                    <span key={i} title={c.detail + (c.model ? ` · ${c.model}` : "")}
                      style={{
                        padding: "2px 8px", borderRadius: 5, fontSize: 11, cursor: "default",
                        border: "1px solid var(--border-subtle)",
                        color: c.skipped ? "var(--text-muted)"
                             : (c.score ?? 0) >= 0.5 ? "var(--status-failed)" : "var(--text-secondary)",
                        opacity: c.skipped ? 0.7 : 1,
                      }}>
                      {c.name.replace(/_/g, " ")}{c.skipped ? " — skipped" : ` ${((c.score ?? 0) * 100).toFixed(0)}%`}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* P4a — session intelligence panel */}
        {intel[s.id] && intel[s.id].status !== "none" && (
          <div style={{ padding: "10px 12px", margin: "6px 0 12px", borderRadius: 8,
            background: "var(--bg-secondary)", border: "1px solid var(--border-subtle)", fontSize: 12 }}>
            {intel[s.id].status === "running" && <span style={{ color: "var(--text-muted)" }}>Transcribing & summarizing… this runs locally and can take a minute.</span>}
            {intel[s.id].status === "failed" && <span style={{ color: "var(--status-failed)" }}>Analysis failed: {intel[s.id].error || "unknown error"}</span>}
            {intel[s.id].status === "completed" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {intel[s.id].summary && (
                  <div>
                    <div style={{ fontWeight: 700, marginBottom: 3 }}>Summary</div>
                    <div style={{ color: "var(--text-secondary)", lineHeight: 1.5 }}>{intel[s.id].summary}</div>
                  </div>
                )}
                {(intel[s.id].action_items?.length ?? 0) > 0 && (
                  <div>
                    <div style={{ fontWeight: 700, marginBottom: 3 }}>Action items</div>
                    <ul style={{ margin: 0, paddingLeft: 18, color: "var(--text-secondary)" }}>
                      {intel[s.id].action_items!.map((a, i) => <li key={i}>{a}</li>)}
                    </ul>
                  </div>
                )}
                <div style={{ display: "flex", gap: 12, alignItems: "center", color: "var(--text-muted)", fontSize: 11 }}>
                  {intel[s.id].transcript_document_id && (
                    <button
                      onClick={() => openDocument(intel[s.id].transcript_document_id!, { filename: "transcript.txt" })}
                      style={{ background: "none", border: "none", padding: 0, cursor: "pointer",
                               font: "inherit", color: "var(--accent)" }}>Transcript</button>
                  )}
                  {intel[s.id].language && <span>lang: {intel[s.id].language}</span>}
                  <span title={JSON.stringify(intel[s.id].model_versions)}>local models · assistive, not a verdict</span>
                </div>
              </div>
            )}
          </div>
        )}
        </React.Fragment>
      ))}

      {inviteFor && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", padding: "10px 0", borderBottom: "1px solid var(--border-subtle)" }}>
          <input style={{ ...inp, flex: 1 }} placeholder="guest email"
            value={inviteEmail} onChange={(e) => setInviteEmail(e.target.value)} />
          <button onClick={() => invite(inviteFor)} disabled={!inviteEmail.trim()}
            style={{ ...inp, cursor: "pointer", color: "var(--accent)", fontWeight: 600 }}>
            Create invite
          </button>
          {inviteLink && (
            <button onClick={() => navigator.clipboard?.writeText(inviteLink)}
              title={inviteLink}
              style={{ ...inp, cursor: "pointer", whiteSpace: "nowrap" }}>
              Copy link (single-use, 15 min)
            </button>
          )}
        </div>
      )}

      {room && (
        <div style={{
          position: "fixed", inset: 0, zIndex: 1000, background: "rgba(0,0,0,.75)",
          display: "flex", alignItems: "center", justifyContent: "center", padding: 24,
        }}>
          <div style={{
            width: "min(1100px, 96vw)", height: "min(720px, 92vh)", borderRadius: 14,
            background: "var(--bg-primary)", border: "1px solid var(--border-subtle)", padding: 16,
          }}>
            <MeetRoom url={room.url} token={room.token} title={room.session.title}
              recordIntent={room.session.record_intent}
              recordingActive={room.session.recording_status === "recording"}
              onToggleRecording={toggleRecording} sessionId={room.session.id}
              onIssueChallenges={() => issueMeetChallenges(room.session.id)}
              onLoadChallenges={() => listMeetChallenges(room.session.id)}
              onChallengeResult={(cid, result) => recordMeetChallengeResult(room.session.id, cid, result)}
              onLeave={() => { setRoom(null); load(); }} />
          </div>
        </div>
      )}
      {playback && (
        <div onClick={closeRecording} style={{
          position: "fixed", inset: 0, zIndex: 1000, background: "rgba(0,0,0,.85)",
          display: "flex", alignItems: "center", justifyContent: "center", padding: 24,
        }}>
          <div onClick={(e) => e.stopPropagation()} style={{
            width: "min(960px, 96vw)", borderRadius: 14, background: "var(--bg-primary)",
            border: "1px solid var(--border-subtle)", padding: 16,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
              <span style={{ fontWeight: 700, fontSize: 13 }}>Sealed recording</span>
              <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                view-only · this access is audit-logged
              </span>
              <button onClick={closeRecording} style={{
                background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 16,
              }}>✕</button>
            </div>
            <video src={playback.url} controls autoPlay
              controlsList="nodownload noremoteplayback"
              disablePictureInPicture
              onContextMenu={(e) => e.preventDefault()}
              style={{ width: "100%", maxHeight: "72vh", borderRadius: 8, background: "#000" }} />
          </div>
        </div>
      )}
      {sessions.length === 0 && providers.length > 0 && (
        <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "8px 0" }}>No sessions yet.</div>
      )}
      {msg && <div style={{ fontSize: 11, color: "var(--status-failed)", marginTop: 8 }}>{msg}</div>}
    </div>
  );
}

/* ── Status Confirm Bar ──────────────────────────────────────── */

function StatusConfirmBar({
  confirm, busy, onConfirm, onCancel,
}: { confirm: { label: string; status: string } | null; busy: boolean; onConfirm: () => void; onCancel: () => void }) {
  if (!confirm) return null;
  const accentColors: Record<string, string> = {
    resolved: "var(--status-completed)", closed: "var(--status-completed)",
    open: "var(--accent)", cancelled: "var(--status-failed)",
  };
  const accent = accentColors[confirm.status] ?? "var(--accent)";
  return (
    <div style={{
      margin: "var(--space-sm) 0 var(--space-md)",
      padding: "10px 14px",
      background: `color-mix(in srgb, ${accent} 8%, transparent)`,
      border: `1px solid ${accent}`, borderRadius: "var(--radius-sm)",
      display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
    }}>
      <span style={{ fontSize: 13, color: "var(--text-primary)", fontWeight: 500 }}>
        {confirm.label}?
      </span>
      <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
        <button disabled={busy} onClick={onConfirm}
          style={{ padding: "5px 14px", background: accent, color: "#fff", border: "none",
            borderRadius: "var(--radius-sm)", fontSize: 12, fontWeight: 600, cursor: "pointer" }}>
          {busy ? "…" : "Confirm"}
        </button>
        <button onClick={onCancel}
          style={{ padding: "5px 14px", background: "transparent", color: "var(--text-secondary)",
            border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 12, cursor: "pointer" }}>
          Cancel
        </button>
      </div>
    </div>
  );
}

/* ── Stages Tab ───────────────────────────────────────────────── */

type StepField = { name: string; label: string; type: string; required?: boolean; options?: string[] };
type Step  = { id: string; name: string; step_type: string; required?: boolean; fields?: StepField[]; form_id?: string };
type Stage = { id: string; name: string; order: number; stage_type: string; steps: Step[] };
type StepCompletion = { step_id: string; status: string; data: Record<string, unknown>; step_type: string };

function StagesTab({
  case_,
  caseType,
  onStageTransition,
  onRefresh,
  onStatusChange,
}: {
  case_: CaseSummary;
  caseType: any;
  onStageTransition: (stageId: string) => Promise<void>;
  onRefresh: () => void;
  onStatusChange: (status: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [completions, setCompletions] = useState<StepCompletion[]>([]);
  const [completionsLoaded, setCompletionsLoaded] = useState(false);
  const [skipConfirm, setSkipConfirm] = useState<{ stageId: string; stageName: string } | null>(null);
  const [statusConfirm, setStatusConfirm] = useState<{ label: string; status: string } | null>(null);

  const stages: Stage[] = (caseType?.definition_json?.stages ?? [])
    .slice()
    .sort((a: Stage, b: Stage) => (a.order ?? 0) - (b.order ?? 0));

  const currentIdx = stages.findIndex((s) => s.id === case_.current_stage_id);
  const isActive = ["open", "new", "reopened"].includes(case_.status);

  // Load existing step completions once
  React.useEffect(() => {
    if (!case_.id) return;
    listStepCompletions(case_.id).then(setCompletions).catch(() => {}).finally(() => setCompletionsLoaded(true));
  }, [case_.id, case_.current_stage_id]);

  const completionMap = Object.fromEntries(completions.map((c) => [c.step_id, c]));

  async function doTransition(stageId: string) {
    setBusy(stageId); setErr(null);
    try { await onStageTransition(stageId); }
    catch (e: any) { setErr(e.message || "Failed"); }
    finally { setBusy(null); }
  }

  async function doStatus(status: string) {
    setBusy(status); setErr(null);
    try { await onStatusChange(status); }
    catch (e: any) { setErr(e.message || "Failed"); }
    finally { setBusy(null); }
  }

  async function doCompleteStep(step: Step, stage: Stage, data: Record<string, unknown>, status = "completed") {
    setBusy(step.id); setErr(null);
    try {
      const result = await completeStep(case_.id, step.id, {
        stage_id: stage.id, step_type: step.step_type, status, data,
      });
      setCompletions((prev) => {
        const without = prev.filter((c) => c.step_id !== step.id);
        return [...without, { step_id: step.id, status, data, step_type: step.step_type }];
      });
      if (result.auto_advanced) {
        // All required steps done — refresh to show the new current stage
        onRefresh();
      } else if (result.advance_held_by_risk) {
        // The stage WOULD have advanced but an HxSentinel hold stopped it.
        // Silence here reads as "nothing happened" and the case looks stuck.
        setErr("Stage advance is held by an open risk verification. Clear it on the ⚠ Risk tab.");
        onRefresh();
      }
    } catch (e: any) { setErr(e.message || "Failed"); }
    finally { setBusy(null); }
  }

  const statusActions: { label: string; status: string }[] = [];
  if (case_.status === "new") statusActions.push({ label: "▶ Open Case", status: "open" });
  if (["open", "reopened"].includes(case_.status)) statusActions.push({ label: "✓ Resolve", status: "resolved" });
  if (case_.status === "resolved") statusActions.push({ label: "Close", status: "closed" });

  if (stages.length === 0) {
    return (
      <div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: "var(--space-md)" }}>
          No lifecycle stages defined for this case type.
        </div>
        <div style={{ display: "flex", gap: "var(--space-xs)", flexWrap: "wrap" }}>
          {statusActions.map((a) => (
            <Button key={a.status} size="sm" disabled={!!busy} onClick={() => setStatusConfirm({ label: a.label, status: a.status })}>
              {busy === a.status ? "…" : a.label}
            </Button>
          ))}
        </div>
        <StatusConfirmBar confirm={statusConfirm} busy={!!busy} onConfirm={async () => { if (statusConfirm) { await doStatus(statusConfirm.status); setStatusConfirm(null); } }} onCancel={() => setStatusConfirm(null)} />
      </div>
    );
  }

  return (
    <div>
      {err && (
        <div style={{ color: "var(--status-failed)", fontSize: 12, marginBottom: "var(--space-md)",
          padding: "var(--space-sm) var(--space-md)", background: "color-mix(in srgb, var(--status-failed) 10%, transparent)",
          borderRadius: "var(--radius-sm)" }}>
          {err}
        </div>
      )}

      {/* Case status actions — always require confirmation to prevent accidental double-clicks */}
      <div style={{ display: "flex", gap: "var(--space-xs)", marginBottom: "var(--space-sm)", flexWrap: "wrap" }}>
        {statusActions.map((a) => (
          <Button key={a.status} size="sm" disabled={!!busy} onClick={() => setStatusConfirm({ label: a.label, status: a.status })}>
            {busy === a.status ? "…" : a.label}
          </Button>
        ))}
      </div>
      <StatusConfirmBar confirm={statusConfirm} busy={!!busy} onConfirm={async () => { if (statusConfirm) { await doStatus(statusConfirm.status); setStatusConfirm(null); } }} onCancel={() => setStatusConfirm(null)} />

      {skipConfirm && (
        <div style={{
          marginBottom: "var(--space-md)", padding: "10px 14px",
          background: "color-mix(in srgb, #f59e0b 8%, transparent)",
          border: "1px solid #f59e0b", borderRadius: "var(--radius-sm)",
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
        }}>
          <span style={{ fontSize: 13, color: "var(--text-primary)", fontWeight: 500 }}>
            Move to <strong>{skipConfirm.stageName}</strong>? (skips remaining steps)
          </span>
          <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
            <button disabled={!!busy} onClick={async () => { await doTransition(skipConfirm.stageId); setSkipConfirm(null); }}
              style={{ padding: "5px 14px", background: "#f59e0b", color: "#fff", border: "none",
                borderRadius: "var(--radius-sm)", fontSize: 12, fontWeight: 600, cursor: "pointer" }}>
              {busy ? "…" : "Confirm"}
            </button>
            <button onClick={() => setSkipConfirm(null)}
              style={{ padding: "5px 14px", background: "transparent", color: "var(--text-secondary)",
                border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 12, cursor: "pointer" }}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Stage pipeline */}
      <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
        {stages.map((stage, idx) => {
          const isDone = currentIdx > idx;
          const isCurrent = currentIdx === idx;
          const isNext = idx === currentIdx + 1;
          const canAdvance = isActive && isNext && !busy;

          const borderColor = isCurrent ? "var(--accent)"
            : isDone ? "var(--status-completed)" : "var(--border-subtle)";
          const bgColor = isCurrent ? "color-mix(in srgb, var(--accent) 8%, transparent)"
            : isDone ? "color-mix(in srgb, var(--status-completed) 6%, transparent)" : "var(--bg-card)";

          return (
            <div key={stage.id} style={{ position: "relative" }}>
              {idx < stages.length - 1 && (
                <div style={{
                  position: "absolute", left: 16, top: "100%", width: 2, height: 12, zIndex: 1,
                  background: isDone ? "var(--status-completed)" : "var(--border-subtle)",
                }} />
              )}
              <div style={{
                marginBottom: 12, borderRadius: "var(--radius-md)",
                border: `1px solid ${borderColor}`, background: bgColor,
                opacity: !isDone && !isCurrent && !isNext ? 0.55 : 1, transition: "all 0.15s",
              }}>
                {/* Stage header */}
                <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)", padding: "var(--space-sm) var(--space-md)" }}>
                  <div style={{
                    width: 24, height: 24, borderRadius: "50%", flexShrink: 0,
                    display: "flex", alignItems: "center", justifyContent: "center",
                    background: isCurrent ? "var(--accent)" : isDone ? "var(--status-completed)" : "var(--bg-elevated)",
                    border: `2px solid ${borderColor}`,
                    fontSize: 11, fontWeight: 700, color: isCurrent || isDone ? "#fff" : "var(--text-muted)",
                    fontFamily: "var(--font-mono)",
                  }}>
                    {isDone ? "✓" : idx + 1}
                  </div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: isCurrent ? "var(--accent)" : "var(--text-primary)" }}>
                      {stage.name}
                      {isCurrent && <span style={{ marginLeft: 8, fontSize: 10, background: "var(--accent)", color: "#fff", padding: "1px 6px", borderRadius: 10, fontFamily: "var(--font-mono)" }}>CURRENT</span>}
                      {isDone && <span style={{ marginLeft: 8, fontSize: 10, color: "var(--status-completed)", fontFamily: "var(--font-mono)" }}>DONE</span>}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                      {stage.stage_type} · {stage.steps?.length ?? 0} step{stage.steps?.length !== 1 ? "s" : ""}
                    </div>
                  </div>
                  {canAdvance && !skipConfirm && (
                    <button onClick={() => setSkipConfirm({ stageId: stage.id, stageName: stage.name })}
                      style={{ padding: "4px 10px", fontSize: 11, border: "1px solid var(--border-default)",
                        borderRadius: "var(--radius-sm)", background: "var(--bg-elevated)",
                        color: "var(--text-secondary)", cursor: "pointer", fontFamily: "var(--font-mono)" }}>
                      → Move Here
                    </button>
                  )}
                </div>

                {/* Step cards — interactive for current stage, read-only summary for done */}
                {(isCurrent || isDone) && stage.steps && stage.steps.length > 0 && (
                  <div style={{ padding: "0 var(--space-md) var(--space-sm) 48px", display: "flex", flexDirection: "column", gap: 8 }}>
                    {stage.steps.map((step) => (
                      <StepCard
                        key={step.id}
                        step={step}
                        stage={stage}
                        caseId={case_.id}
                        completion={completionMap[step.id]}
                        interactive={isCurrent && isActive}
                        busy={busy === step.id}
                        onComplete={(data, status) => doCompleteStep(step, stage, data, status)}
                      />
                    ))}
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

/* ── Step Card ─────────────────────────────────────────────────── */

function StepCard({
  step, stage, caseId, completion, interactive, busy, onComplete,
}: {
  step: Step;
  stage: Stage;
  caseId: string;
  completion?: StepCompletion;
  interactive: boolean;
  busy: boolean;
  onComplete: (data: Record<string, unknown>, status: string) => Promise<void>;
}) {
  const isDone = !!completion;
  const [expanded, setExpanded] = useState(!isDone && interactive);
  const [reason, setReason] = useState("");

  // Fix 1: collapse and lock when step becomes done
  useEffect(() => { if (isDone) setExpanded(false); }, [isDone]);

  const dotColor = isDone
    ? (completion?.status === "rejected" ? "var(--status-failed)" : "var(--status-completed)")
    : interactive ? "var(--accent)" : "var(--border-subtle)";

  return (
    <div style={{
      borderRadius: "var(--radius-sm)",
      border: `1px solid ${isDone ? (completion?.status === "rejected" ? "var(--status-failed)" : "var(--status-completed)") : interactive ? "var(--accent)" : "var(--border-subtle)"}`,
      background: "var(--bg-elevated)", overflow: "hidden",
      opacity: !isDone && !interactive ? 0.6 : 1,
    }}>
      {/* Step header row — Fix 1: not clickable when done */}
      <div
        style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", cursor: interactive && !isDone ? "pointer" : "default" }}
        onClick={() => interactive && !isDone && setExpanded((e) => !e)}
      >
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: dotColor, flexShrink: 0 }} />
        <span style={{ fontSize: 12, fontWeight: 500, flex: 1, color: "var(--text-primary)" }}>{step.name}</span>
        <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
          {step.step_type}{step.required ? " · required" : " · optional"}
        </span>
        {isDone && (
          <span style={{
            fontSize: 10, padding: "1px 6px", borderRadius: 10, fontWeight: 600,
            fontFamily: "var(--font-mono)",
            background: completion?.status === "rejected" ? "#fee2e2" : "#dcfce7",
            color: completion?.status === "rejected" ? "#dc2626" : "#16a34a",
          }}>
            {completion?.status === "rejected" ? "REJECTED" : "DONE"}
          </span>
        )}
        {interactive && !isDone && (
          <span style={{ fontSize: 10, color: "var(--accent)" }}>{expanded ? "▲" : "▼"}</span>
        )}
      </div>

      {/* Expanded interactive body */}
      {expanded && interactive && (
        <div style={{ borderTop: "1px solid var(--border-subtle)", padding: "10px 12px" }}>
          {step.step_type === "user_task" && (
            <UserTaskForm step={step} caseId={caseId}
              onSubmit={(data) => { setExpanded(false); onComplete(data, "completed"); }}
              onCancel={() => setExpanded(false)} busy={busy} />
          )}
          {step.step_type === "approval" && (
            <ApprovalForm reason={reason} onReason={setReason}
              onApprove={() => { setExpanded(false); onComplete({ reason }, "completed"); }}
              onReject={() => { setExpanded(false); onComplete({ reason }, "rejected"); }}
              busy={busy} />
          )}
          {step.step_type === "document_request" && (
            <DocumentRequestForm caseId={caseId} stepId={step.id}
              onDone={(docId, filename) => { setExpanded(false); onComplete({ document_id: docId, filename }, "completed"); }}
              busy={busy} />
          )}
          {step.step_type === "automated" && (
            <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic" }}>
              This step runs automatically — no action required.
            </div>
          )}
          {!["user_task","approval","document_request","automated"].includes(step.step_type) && (
            <button
              disabled={busy}
              onClick={() => { setExpanded(false); onComplete({}, "completed"); }}
              style={{ padding: "5px 14px", border: "none", borderRadius: "var(--radius-sm)",
                background: "var(--accent)", color: "#fff", fontSize: 12, cursor: "pointer" }}>
              {busy ? "…" : "Mark Complete"}
            </button>
          )}
        </div>
      )}

      {/* Completed data preview + View Form button */}
      {isDone && completion?.data && (
        <div style={{ borderTop: "1px solid var(--border-subtle)", padding: "6px 10px 8px 28px" }}>
          {/* If this step had a form, show View Form button instead of raw key-value dump */}
          {step.form_id ? (
            <ViewFormButton formId={step.form_id} caseId={caseId} data={completion.data} stepName={step.name} />
          ) : (
            Object.entries(completion.data).map(([k, v]) => (
              <div key={k} style={{ fontSize: 11, color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}>
                {k}: <span style={{ color: "var(--text-primary)" }}>{String(v)}</span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

/* ── View Submitted Form (read-only modal) ─────────────────────── */

function ViewFormButton({ formId, caseId, data, stepName }: {
  formId: string;
  /** The form as this case used it (its pinned version when versioned per case). */
  caseId: string;
  data: Record<string, unknown>;
  stepName: string;
}) {
  const [open, setOpen] = useState(false);
  const [formDef, setFormDef] = useState<any>(null);
  const [formName, setFormName] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function openModal() {
    setOpen(true);
    if (formDef) return;
    setLoading(true);
    getCaseForm(caseId, formId)
      .then(f => { setFormDef(f.definition_json); setFormName(f.name); })
      .catch(e => setErr(e.message || "Failed to load form"))
      .finally(() => setLoading(false));
  }

  return (
    <>
      <button onClick={openModal} style={{
        padding: "3px 10px", fontSize: 11, border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-sm)", background: "var(--bg-elevated)",
        color: "var(--text-secondary)", cursor: "pointer", fontFamily: "var(--font-mono)",
        display: "inline-flex", alignItems: "center", gap: 4,
      }}>
        👁 View submitted form
      </button>

      {open && (
        <div style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.55)",
          zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center",
          padding: 24,
        }} onClick={() => setOpen(false)}>
          <div style={{
            width: "min(680px, 100%)", maxHeight: "85vh", overflow: "auto",
            borderRadius: "var(--radius-lg)", background: "var(--bg-panel)",
            boxShadow: "0 24px 64px rgba(0,0,0,0.4)",
          }} onClick={e => e.stopPropagation()}>
            {/* Modal header */}
            <div style={{
              display: "flex", justifyContent: "space-between", alignItems: "center",
              padding: "var(--space-md) var(--space-lg)",
              borderBottom: "1px solid var(--border-subtle)",
            }}>
              <div>
                <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{stepName}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                  Read-only · submitted form
                </div>
              </div>
              <button onClick={() => setOpen(false)} style={{
                background: "none", border: "none", cursor: "pointer",
                fontSize: 18, color: "var(--text-muted)", lineHeight: 1,
              }}>✕</button>
            </div>

            <div style={{ padding: "var(--space-lg)" }}>
              {loading && <div style={{ color: "var(--text-muted)", padding: 32 }}>Loading form…</div>}
              {err && <div style={{ color: "var(--status-failed)", fontSize: 13 }}>{err}</div>}
              {!loading && !err && formDef && (
                <FormRenderer
                  formName={formName}
                  definition={formDef}
                  initialValues={data as Record<string, any>}
                  onSubmit={async () => {}}
                  onCancel={() => setOpen(false)}
                  readOnly
                />
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/* ── Step input sub-components ─────────────────────────────────── */

function UserTaskForm({
  step, caseId, onSubmit, onCancel, busy,
}: {
  step: Step;
  caseId: string;
  onSubmit: (d: Record<string, unknown>) => void;
  onCancel: () => void;
  busy: boolean;
}) {
  if (step.form_id) {
    return <FormBuilderStep formId={step.form_id} caseId={caseId} onSubmit={onSubmit} onCancel={onCancel} busy={busy} />;
  }
  if (step.fields?.length) {
    return <InlineFieldsForm fields={step.fields} onSubmit={onSubmit} onCancel={onCancel} busy={busy} />;
  }
  return <FreeTextForm stepName={step.name} onSubmit={onSubmit} onCancel={onCancel} busy={busy} />;
}

function FormBuilderStep({
  formId, caseId, onSubmit, onCancel, busy,
}: {
  formId: string;
  caseId: string;
  onSubmit: (d: Record<string, unknown>) => void;
  onCancel: () => void;
  busy: boolean;
}) {
  const [formDef, setFormDef] = useState<any>(null);
  const [formName, setFormName] = useState("");
  const [prefill, setPrefill] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  // Save as draft (§5 step 7): the person's own unfinished answers, restored on open.
  const draft = useFormDraft(caseId, formId);

  useEffect(() => {
    // The form opens with the case's variables filled in, as in Work Center —
    // in the version this case uses (§5 step 6).
    getCaseForm(caseId, formId)
      .then(async (f) => {
        setPrefill(await loadPrefill(caseId, f.definition_json));
        setFormDef(f.definition_json); setFormName(f.name);
      })
      .catch((e) => setErr(e.message || "Failed to load form"))
      .finally(() => setLoading(false));
  }, [formId, caseId]);

  if (loading || !draft.ready) return <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Loading form…</div>;
  if (err) return <div style={{ fontSize: 12, color: "var(--status-failed)" }}>Form error: {err}</div>;
  if (!formDef) return null;

  return (
    <FormRenderer
      key={draft.key}
      formName={formName}
      definition={formDef}
      initialValues={{ ...prefill, ...draft.values }}
      onSubmit={async (values) => onSubmit(values)}
      onCancel={onCancel}
      caseId={caseId}
      formId={formId}
      notice={draft.notice}
      secondaryAction={draft.action}
    />
  );
}

function InlineFieldsForm({
  fields, onSubmit, onCancel, busy,
}: {
  fields: StepField[];
  onSubmit: (d: Record<string, unknown>) => void;
  onCancel: () => void;
  busy: boolean;
}) {
  const [formData, setFormData] = useState<Record<string, unknown>>({});

  function set(name: string, value: unknown) {
    setFormData((prev) => ({ ...prev, [name]: value }));
  }

  return (
    <div>
      {fields.map((f) => (
        <div key={f.name} style={{ marginBottom: 8 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-secondary)",
            textTransform: "uppercase", letterSpacing: "0.04em", display: "block", marginBottom: 3 }}>
            {f.label}{f.required ? " *" : ""}
          </label>
          {f.type === "textarea" ? (
            <textarea value={String(formData[f.name] ?? "")} onChange={(e) => set(f.name, e.target.value)}
              rows={3} style={{ width: "100%", padding: "5px 8px", border: "1px solid var(--border-default)",
                borderRadius: "var(--radius-sm)", background: "var(--bg-input)",
                color: "var(--text-primary)", fontSize: 12, resize: "vertical", boxSizing: "border-box" }} />
          ) : f.type === "select" ? (
            <select value={String(formData[f.name] ?? "")} onChange={(e) => set(f.name, e.target.value)}
              style={{ width: "100%", padding: "5px 8px", border: "1px solid var(--border-default)",
                borderRadius: "var(--radius-sm)", background: "var(--bg-input)", color: "var(--text-primary)", fontSize: 12 }}>
              <option value="">— select —</option>
              {(f.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          ) : f.type === "checkbox" ? (
            <input type="checkbox" checked={!!formData[f.name]} onChange={(e) => set(f.name, e.target.checked)} />
          ) : (
            <input type={f.type === "number" ? "number" : f.type === "date" ? "date" : "text"}
              value={String(formData[f.name] ?? "")} onChange={(e) => set(f.name, e.target.value)}
              style={{ width: "100%", padding: "5px 8px", border: "1px solid var(--border-default)",
                borderRadius: "var(--radius-sm)", background: "var(--bg-input)",
                color: "var(--text-primary)", fontSize: 12, boxSizing: "border-box" }} />
          )}
        </div>
      ))}
      <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
        <button disabled={busy} onClick={() => onSubmit(formData)}
          style={{ padding: "5px 14px", border: "none", borderRadius: "var(--radius-sm)",
            background: "var(--accent)", color: "#fff", fontSize: 12, cursor: "pointer" }}>
          {busy ? "…" : "Mark Complete"}
        </button>
        <button onClick={onCancel}
          style={{ padding: "5px 12px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
            background: "transparent", color: "var(--text-secondary)", fontSize: 12, cursor: "pointer" }}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function FreeTextForm({
  stepName, onSubmit, onCancel, busy,
}: {
  stepName: string;
  onSubmit: (d: Record<string, unknown>) => void;
  onCancel: () => void;
  busy: boolean;
}) {
  const [notes, setNotes] = useState("");
  return (
    <div>
      <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-secondary)",
        textTransform: "uppercase", letterSpacing: "0.04em", display: "block", marginBottom: 3 }}>
        Notes
      </label>
      <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3}
        placeholder={`Notes for: ${stepName}`}
        style={{ width: "100%", padding: "5px 8px", border: "1px solid var(--border-default)",
          borderRadius: "var(--radius-sm)", background: "var(--bg-input)",
          color: "var(--text-primary)", fontSize: 12, resize: "vertical", boxSizing: "border-box" }} />
      <button disabled={busy} onClick={() => onSubmit({ notes })}
        style={{ padding: "5px 14px", border: "none", borderRadius: "var(--radius-sm)",
          background: "var(--accent)", color: "#fff", fontSize: 12, cursor: "pointer", marginTop: 6 }}>
        {busy ? "…" : "Mark Complete"}
      </button>
      <button onClick={onCancel}
        style={{ padding: "5px 12px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
          background: "transparent", color: "var(--text-secondary)", fontSize: 12, cursor: "pointer", marginTop: 6 }}>
        Cancel
      </button>
    </div>
  );
}

function ApprovalForm({
  reason, onReason, onApprove, onReject, busy,
}: {
  reason: string;
  onReason: (r: string) => void;
  onApprove: () => void;
  onReject: () => void;
  busy: boolean;
}) {
  return (
    <div>
      <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-secondary)",
        textTransform: "uppercase", letterSpacing: "0.04em", display: "block", marginBottom: 3 }}>
        Reason / Notes (optional)
      </label>
      <textarea value={reason} onChange={(e) => onReason(e.target.value)} rows={2}
        style={{ width: "100%", padding: "5px 8px", border: "1px solid var(--border-default)",
          borderRadius: "var(--radius-sm)", background: "var(--bg-input)",
          color: "var(--text-primary)", fontSize: 12, resize: "vertical",
          boxSizing: "border-box", marginBottom: 8 }} />
      <div style={{ display: "flex", gap: 8 }}>
        <button disabled={busy} onClick={onApprove}
          style={{ padding: "5px 14px", border: "none", borderRadius: "var(--radius-sm)",
            background: "var(--status-completed)", color: "#fff", fontSize: 12, cursor: "pointer" }}>
          {busy ? "…" : "✓ Approve"}
        </button>
        <button disabled={busy} onClick={onReject}
          style={{ padding: "5px 14px", border: "1px solid var(--status-failed)", borderRadius: "var(--radius-sm)",
            background: "transparent", color: "var(--status-failed)", fontSize: 12, cursor: "pointer" }}>
          {busy ? "…" : "✕ Reject"}
        </button>
      </div>
    </div>
  );
}

function DocumentRequestForm({
  caseId, stepId, onDone, busy,
}: {
  caseId: string;
  stepId: string;
  onDone: (docId: string, filename: string) => void;
  busy: boolean;
}) {
  const [uploading, setUploading] = useState(false);
  const [uploaded, setUploaded] = useState<{ id: string; filename: string } | null>(null);

  async function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("document_type", "step_attachment");
      fd.append("description", `Step: ${stepId}`);
      const resp = await fetch(`/api/v1/cases/${caseId}/documents`, {
        method: "POST", body: fd,
        headers: getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {},
      });
      if (!resp.ok) throw new Error(await resp.text());
      const doc = await resp.json();
      setUploaded({ id: doc.id, filename: file.name });
    } catch (err: any) {
      alert(`Upload failed: ${err.message}`);
    } finally { setUploading(false); }
  }

  return (
    <div>
      <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 8 }}>
        Upload the required document to complete this step.
      </div>
      {uploaded ? (
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 12, color: "var(--status-completed)" }}>✓ {uploaded.filename}</span>
          <button disabled={busy} onClick={() => onDone(uploaded.id, uploaded.filename)}
            style={{ padding: "5px 14px", border: "none", borderRadius: "var(--radius-sm)",
              background: "var(--accent)", color: "#fff", fontSize: 12, cursor: "pointer" }}>
            {busy ? "…" : "Mark Complete"}
          </button>
        </div>
      ) : (
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer",
          padding: "5px 14px", border: "1px solid var(--border-default)",
          borderRadius: "var(--radius-sm)", fontSize: 12, color: "var(--text-secondary)",
          background: "var(--bg-elevated)" }}>
          {uploading ? "Uploading…" : "📎 Choose File"}
          <input type="file" style={{ display: "none" }} onChange={handleFile} disabled={uploading} />
        </label>
      )}
    </div>
  );
}

/* ── My Task Tab (Pega Perform view) ──────────────────────────── */

function MyTaskTab({
  task,
  onComplete,
  onCancel,
}: {
  task: MyTaskResult;
  onComplete: () => Promise<void>;
  onCancel: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const lockExpiresAt = task.lock_expires_at ? new Date(task.lock_expires_at) : null;
  const minutesLeft = lockExpiresAt
    ? Math.max(0, Math.floor((lockExpiresAt.getTime() - Date.now()) / 60000))
    : null;

  const handleSubmit = async (data: Record<string, unknown>) => {
    setBusy(true);
    setErr(null);
    try {
      await completeStep(task.case_id, task.step_id, {
        stage_id: task.stage_id,
        step_type: (task.step_def as any).step_type ?? "user_task",
        status: "completed",
        data,
      });
      await onComplete();
    } catch (e: any) {
      setErr(e.message || "Failed to submit");
    } finally {
      setBusy(false);
    }
  };

  const handleCancel = async () => {
    setBusy(true);
    try { await onCancel(); } finally { setBusy(false); }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-lg)" }}>
      {/* Lock indicator */}
      <div style={{
        padding: "10px 14px",
        background: "color-mix(in srgb, var(--accent) 6%, transparent)",
        border: "1px solid color-mix(in srgb, var(--accent) 30%, transparent)",
        borderRadius: "var(--radius-sm)",
        display: "flex", alignItems: "center", gap: 10,
      }}>
        <span style={{ fontSize: 16 }}>🔒</span>
        <div>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--accent)" }}>
            Locked to you
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
            {minutesLeft !== null ? `Lock expires in ~${minutesLeft} min — submit or Cancel to release` : "Submit or Cancel to release lock"}
          </div>
        </div>
      </div>

      {/* Step name + type */}
      <div>
        <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)" }}>
          {(task.step_def as any).name ?? task.step_id}
        </div>
        <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: 2 }}>
          {(task.step_def as any).step_type ?? "user_task"} · step {task.step_id}
        </div>
      </div>

      {err && (
        <div style={{ fontSize: 12, color: "var(--status-failed)", padding: "8px 12px", background: "color-mix(in srgb,var(--status-failed) 8%,transparent)", borderRadius: "var(--radius-sm)" }}>
          {err}
        </div>
      )}

      {/* Form */}
      {task.form_id ? (
        <FormBuilderStep
          formId={task.form_id}
          caseId={task.case_id}
          onSubmit={handleSubmit}
          onCancel={handleCancel}
          busy={busy}
        />
      ) : (task.step_def as any).fields?.length ? (
        <InlineFieldsForm
          fields={(task.step_def as any).fields}
          onSubmit={handleSubmit}
          onCancel={handleCancel}
          busy={busy}
        />
      ) : (
        <FreeTextForm
          stepName={(task.step_def as any).name ?? task.step_id}
          onSubmit={handleSubmit}
          onCancel={handleCancel}
          busy={busy}
        />
      )}
    </div>
  );
}

/* ── Detail Tabs ──────────────────────────────────────────────── */

function DetailsTab({ case_, onPriorityChange }: { case_: CaseSummary; onPriorityChange: (p: string) => void }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-lg)" }}>
      <DetailSection title="Info">
        <DetailField label="ID" value={case_.id} mono />
        <DetailField label="Type ID" value={case_.case_type_id.slice(0, 12) + "…"} mono />
        <DetailField label="Version" value={case_.case_type_version} />
        <DetailField label="Stage" value={case_.current_stage_id || "—"} />
        <DetailField label="Created" value={new Date(case_.created_at).toLocaleString()} />
        {case_.resolved_at && <DetailField label="Resolved" value={new Date(case_.resolved_at).toLocaleString()} />}
        {case_.closed_at && <DetailField label="Closed" value={new Date(case_.closed_at).toLocaleString()} />}
      </DetailSection>

      <DetailSection title="Priority">
        <div style={{ display: "flex", gap: "var(--space-xs)", flexWrap: "wrap" }}>
          {(["low", "medium", "high", "critical", "blocker"] as CasePriority[]).map((p) => (
            <button key={p} onClick={() => onPriorityChange(p)} style={{
              padding: "4px 10px", fontSize: 11, fontFamily: "var(--font-mono)", textTransform: "uppercase",
              borderRadius: "var(--radius-sm)", border: "1px solid",
              cursor: "pointer", transition: "all 0.1s",
              color: PRIORITY_COLORS[p],
              borderColor: case_.priority === p ? PRIORITY_COLORS[p] : "var(--border-subtle)",
              background: case_.priority === p ? `color-mix(in srgb, ${PRIORITY_COLORS[p]} 15%, transparent)` : "transparent",
            }}>{p}</button>
          ))}
        </div>
      </DetailSection>

      {Object.keys(case_.data).length > 0 && (
        <DetailSection title="Data">
          <pre style={{
            fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-secondary)",
            background: "var(--bg-input)", padding: "var(--space-md)", borderRadius: "var(--radius-sm)",
            overflow: "auto", whiteSpace: "pre-wrap", lineHeight: 1.6,
          }}>
            {JSON.stringify(case_.data, null, 2)}
          </pre>
        </DetailSection>
      )}
    </div>
  );
}

function TimelineTab({ entries }: { entries: CaseAuditEntry[] }) {
  if (entries.length === 0) return <EmptyState title="No history yet" />;

  return (
    <div style={{ position: "relative" }}>
      {/* Vertical line */}
      <div style={{ position: "absolute", left: 11, top: 8, bottom: 8, width: 1, background: "var(--border-subtle)" }} />

      {entries.map((entry) => (
        <div key={entry.id} style={{ display: "flex", gap: "var(--space-md)", marginBottom: "var(--space-lg)", position: "relative" }}>
          <div style={{
            width: 22, height: 22, borderRadius: "50%", border: "2px solid var(--border-default)",
            background: "var(--bg-panel)", display: "flex", alignItems: "center", justifyContent: "center",
            flexShrink: 0, zIndex: 1,
          }}>
            <div style={{ width: 6, height: 6, borderRadius: "50%", background: actionColor(entry.action) }} />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
              <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-primary)" }}>
                {formatAction(entry.action)}
              </span>
              <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", flexShrink: 0 }}>
                <TimeAgo date={entry.timestamp} />
              </span>
            </div>
            {entry.actor_id && (
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                by {entry.actor_id} ({entry.actor_type})
              </div>
            )}
            {entry.new_value && (
              <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-secondary)", marginTop: 4 }}>
                {JSON.stringify(entry.new_value)}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function SLATab({ slas }: { slas: SLAStatusInfo[] }) {
  if (slas.length === 0) return <EmptyState title="No SLAs" description="No SLA policies are tracking this case." />;

  const SLA_COLORS: Record<string, string> = {
    on_track: "var(--status-completed)",
    at_risk: "var(--status-running)",
    breached: "var(--status-failed)",
    paused: "var(--text-muted)",
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
      {slas.map((sla) => {
        const color = SLA_COLORS[sla.status] || "var(--text-muted)";
        const goalDate = new Date(sla.goal_at);
        const deadlineDate = new Date(sla.deadline_at);
        const now = Date.now();
        const total = deadlineDate.getTime() - new Date(sla.started_at).getTime();
        const elapsed = now - new Date(sla.started_at).getTime();
        const pct = Math.min(100, Math.max(0, (elapsed / total) * 100));

        return (
          <Card key={sla.id} style={{ padding: "var(--space-md)" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-sm)" }}>
              <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-primary)", fontFamily: "var(--font-mono)" }}>
                {sla.sla_policy_id}
              </span>
              <span style={{
                fontSize: 10, fontWeight: 500, padding: "2px 8px", borderRadius: 100,
                color, textTransform: "uppercase", fontFamily: "var(--font-mono)",
                background: `color-mix(in srgb, ${color} 12%, transparent)`,
                border: `1px solid color-mix(in srgb, ${color} 25%, transparent)`,
              }}>
                {sla.status.replace("_", " ")}
              </span>
            </div>

            {/* Progress bar */}
            <div style={{ height: 4, borderRadius: 2, background: "var(--bg-elevated)", marginBottom: "var(--space-sm)" }}>
              <div style={{ height: "100%", borderRadius: 2, background: color, width: `${pct}%`, transition: "width 0.3s" }} />
            </div>

            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
              <span>Goal: {goalDate.toLocaleString()}</span>
              <span>Deadline: {deadlineDate.toLocaleString()}</span>
            </div>
          </Card>
        );
      })}
    </div>
  );
}

/* ── Create Case Modal ────────────────────────────────────────── */

function compareVersions(a: string, b: string): number {
  const pa = a.split(".").map(Number);
  const pb = b.split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const diff = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

function latestOnly(items: CaseTypeSummary[]): CaseTypeSummary[] {
  const map = new Map<string, CaseTypeSummary>();
  for (const ct of items) {
    const existing = map.get(ct.name);
    if (!existing || compareVersions(ct.version, existing.version) > 0) {
      map.set(ct.name, ct);
    }
  }
  return Array.from(map.values()).sort((a, b) => a.name.localeCompare(b.name));
}

function CreateCaseModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const { data: ctData } = useApi(listCaseTypes);
  const caseTypes = latestOnly(ctData?.items ?? []);

  const [typeId, setTypeId] = useState("");
  const [priority, setPriority] = useState("medium");
  const [dataStr, setDataStr] = useState("{}");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async () => {
    if (!typeId) { setError("Select a case type"); return; }
    setSubmitting(true);
    try {
      const data = JSON.parse(dataStr);
      await createCase({ case_type_id: typeId, priority, data });
      onCreated();
    } catch (e: any) {
      setError(e.message || "Failed to create case");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, background: "var(--bg-overlay)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100,
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        background: "var(--bg-card)", border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-lg)", padding: "var(--space-xl)", width: 440,
        boxShadow: "var(--shadow-lg)",
      }}>
        <h2 style={{ fontFamily: "var(--font-display)", fontSize: 20, fontWeight: 600, color: "var(--text-primary)", marginBottom: "var(--space-lg)" }}>
          New Case
        </h2>

        <div style={{ marginBottom: "var(--space-md)" }}>
          <label style={{ display: "block", fontSize: 11, fontWeight: 500, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)", marginBottom: "var(--space-xs)" }}>Case Type</label>
          <select value={typeId} onChange={(e) => setTypeId(e.target.value)} style={{
            width: "100%", padding: "8px 12px", fontSize: 13, background: "var(--bg-input)",
            border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
            color: "var(--text-primary)", boxSizing: "border-box",
          }}>
            <option value="">Select…</option>
            {caseTypes.map((ct) => <option key={ct.id} value={ct.id}>{ct.name} v{ct.version}</option>)}
          </select>
        </div>

        <div style={{ marginBottom: "var(--space-md)" }}>
          <label style={{ display: "block", fontSize: 11, fontWeight: 500, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)", marginBottom: "var(--space-xs)" }}>Priority</label>
          <select value={priority} onChange={(e) => setPriority(e.target.value)} style={{
            width: "100%", padding: "8px 12px", fontSize: 13, background: "var(--bg-input)",
            border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)",
            color: "var(--text-primary)", boxSizing: "border-box",
          }}>
            {["low", "medium", "high", "critical", "blocker"].map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </div>

        <div style={{ marginBottom: "var(--space-md)" }}>
          <label style={{ display: "block", fontSize: 11, fontWeight: 500, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)", marginBottom: "var(--space-xs)" }}>Initial Data (JSON)</label>
          <textarea value={dataStr} onChange={(e) => setDataStr(e.target.value)} rows={4} style={{
            width: "100%", padding: "8px 12px", fontSize: 12, fontFamily: "var(--font-mono)",
            background: "var(--bg-input)", border: "1px solid var(--border-default)",
            borderRadius: "var(--radius-sm)", color: "var(--text-primary)",
            resize: "vertical", boxSizing: "border-box",
          }} />
        </div>

        {error && <p style={{ color: "var(--status-failed)", fontSize: 12, marginBottom: "var(--space-sm)" }}>{error}</p>}

        <div style={{ display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end" }}>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button onClick={handleSubmit} disabled={submitting}>{submitting ? "Creating…" : "Create Case"}</Button>
        </div>
      </div>
    </div>
  );
}

/* ── Shared helpers ───────────────────────────────────────────── */

function StatusPill({ status }: { status: string }) {
  const color = STATUS_COLORS[status] || "var(--text-muted)";
  return (
    <span style={{
      fontSize: 10, fontWeight: 500, padding: "3px 10px", borderRadius: 100,
      color, fontFamily: "var(--font-mono)", textTransform: "uppercase",
      background: `color-mix(in srgb, ${color} 12%, transparent)`,
      border: `1px solid color-mix(in srgb, ${color} 25%, transparent)`,
    }}>{status}</span>
  );
}

function PriorityPill({ priority }: { priority: string }) {
  const color = PRIORITY_COLORS[priority] || "var(--text-muted)";
  return (
    <span style={{
      fontSize: 10, fontWeight: 500, padding: "3px 10px", borderRadius: 100,
      color, fontFamily: "var(--font-mono)", textTransform: "uppercase",
      background: `color-mix(in srgb, ${color} 12%, transparent)`,
      border: `1px solid color-mix(in srgb, ${color} 25%, transparent)`,
    }}>{priority}</span>
  );
}

function PaginationBar({ page, totalPages, total, pageSize, onChange }: {
  page: number; totalPages: number; total: number; pageSize: number; onChange: (p: number) => void;
}) {
  // Build page numbers: always show first, last, current ±1, with ellipsis gaps
  const pages: (number | "…")[] = [];
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - page) <= 1) {
      pages.push(i);
    } else if (pages[pages.length - 1] !== "…") {
      pages.push("…");
    }
  }

  return (
    <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
      <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginRight: 4, whiteSpace: "nowrap" }}>
        {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, total)} / {total}
      </span>
      <button onClick={() => onChange(page - 1)} disabled={page === 1}
        style={{ ...pageBtnStyle, opacity: page === 1 ? 0.35 : 1 }}>‹</button>
      {pages.map((p, i) =>
        p === "…" ? (
          <span key={`e${i}`} style={{ fontSize: 11, color: "var(--text-muted)", padding: "0 2px" }}>…</span>
        ) : (
          <button key={p} onClick={() => onChange(p as number)}
            style={{ ...pageBtnStyle, background: page === p ? "var(--accent)" : "transparent", color: page === p ? "#fff" : "var(--text-secondary)", borderColor: page === p ? "var(--accent)" : "var(--border-default)" }}>
            {p}
          </button>
        )
      )}
      <button onClick={() => onChange(page + 1)} disabled={page >= totalPages}
        style={{ ...pageBtnStyle, opacity: page >= totalPages ? 0.35 : 1 }}>›</button>
    </div>
  );
}

const pageBtnStyle: React.CSSProperties = {
  width: 28, height: 28, borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
  background: "transparent", color: "var(--text-secondary)", fontSize: 12,
  cursor: "pointer", fontFamily: "var(--font-mono)", display: "flex", alignItems: "center", justifyContent: "center",
};

function FilterChip({ label, active, onClick, color }: { label: string; active: boolean; onClick: () => void; color?: string }) {
  return (
    <button onClick={onClick} style={{
      padding: "4px 10px", fontSize: 11, fontFamily: "var(--font-mono)", textTransform: "uppercase",
      borderRadius: 100, border: "1px solid", cursor: "pointer",
      color: active ? (color || "var(--accent)") : "var(--text-muted)",
      borderColor: active ? (color || "var(--accent)") : "var(--border-subtle)",
      background: active ? `color-mix(in srgb, ${color || "var(--accent)"} 12%, transparent)` : "transparent",
    }}>{label}</button>
  );
}

function DetailSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 11, fontWeight: 500, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)", marginBottom: "var(--space-sm)" }}>
        {title}
      </div>
      {children}
    </div>
  );
}

function DetailField({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", fontSize: 12 }}>
      <span style={{ color: "var(--text-muted)" }}>{label}</span>
      <span style={{ color: "var(--text-primary)", fontFamily: mono ? "var(--font-mono)" : "var(--font-body)" }}>{value}</span>
    </div>
  );
}

function formatAction(action: string): string {
  return action.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function actionColor(action: string): string {
  if (action.includes("created")) return "var(--accent)";
  if (action.includes("resolved") || action.includes("completed")) return "var(--status-completed)";
  if (action.includes("cancelled") || action.includes("failed")) return "var(--status-failed)";
  if (action.includes("sla")) return "var(--status-running)";
  return "var(--border-strong)";
}
