/* ═══════════════════════════════════════════════════════════════════
   API Client — typed wrapper around the Velaris REST API

   All calls go through /api which Vite proxies to localhost:8100.
   ═══════════════════════════════════════════════════════════════════ */

import type {
  DeployResponse,
  InstanceDetail,
  InstanceListResponse,
  ProcessListResponse,
} from "@shared/types";
import { getAccessToken, attemptRefresh } from "../../auth/tokenManager";

const BASE = "/api";

function authHeaders(token?: string): Record<string, string> {
  const t = token ?? getAccessToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

/** Open or save a document through an authenticated request.
 *
 * A plain `<a href="/api/v1/documents/{id}/download">` cannot be used: browser
 * navigation does not send the `Authorization` header, and since 2026-08-15
 * those routes require either a Bearer token (operators) or a signed grant
 * (portal customers). Before that they were unauthenticated, which is exactly
 * why the anchors worked — and why every document in the deployment was
 * readable by UUID.
 *
 * Portal code must NOT use this: it has no token, and the server already hands
 * it a `download_url` carrying a grant. Use that URL as-is.
 */
export async function openDocument(
  documentId: string,
  opts: { preview?: boolean; filename?: string } = {},
): Promise<void> {
  const path = opts.preview ? "preview" : "download";
  const res = await fetch(`/api/v1/documents/${documentId}/${path}`, {
    headers: { ...authHeaders() },
  });
  if (!res.ok) throw await errorFromResponse(res, `Document ${path} failed (${res.status})`);

  const url = URL.createObjectURL(await res.blob());
  try {
    if (opts.preview) {
      window.open(url, "_blank", "noreferrer");
    } else {
      const a = document.createElement("a");
      a.href = url;
      a.download = opts.filename || "document";
      document.body.appendChild(a);
      a.click();
      a.remove();
    }
  } finally {
    // Give the browser a moment to start the download/tab before revoking.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
}

/** Parse a success response, tolerating an empty body.
 *
 * A 204 has no body by definition, so `res.json()` on one always rejects with
 * "Unexpected end of JSON input" — which surfaced as a *failed* delete even
 * though the server had already deleted the row, leaving the caller's list
 * stale and an error alert on screen. Every DELETE in this file returns 204.
 */
async function parseBody<T>(res: Response): Promise<T> {
  if (res.status === 204) return null as T;
  try {
    return await res.json();
  } catch {
    // An empty 200 lands here too. Either way the caller wanted a value that
    // is not there, and null says so better than a SyntaxError does.
    return null as T;
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  // ...options is spread FIRST: a caller passing its own `headers` must merge
  // with (not replace) the auth header — replacing it drops Authorization and
  // the resulting 401 force-logs-out the user via the global interceptor.
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...options?.headers },
  });

  if (res.status === 401) {
    const newToken = await attemptRefresh();
    if (newToken) {
      const retry = await fetch(`${BASE}${path}`, {
        ...options,
        headers: { "Content-Type": "application/json", ...authHeaders(newToken), ...options?.headers },
      });
      if (retry.status === 401) {
        window.dispatchEvent(new Event("velaris:unauthorized"));
        throw new ApiError(401, "Session expired. Please log in again.");
      }
      if (!retry.ok) {
        const body = await retry.json().catch(() => ({ detail: retry.statusText }));
        throw new ApiError(retry.status, typeof body.detail === "object" ? JSON.stringify(body.detail) : (body.detail || retry.statusText), codedFrom(body));
      }
      return parseBody<T>(retry);
    }
    window.dispatchEvent(new Event("velaris:unauthorized"));
    throw new ApiError(401, "Session expired. Please log in again.");
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, typeof body.detail === 'object' ? JSON.stringify(body.detail) : (body.detail || res.statusText), codedFrom(body));
  }

  return parseBody<T>(res);
}

/** A structured `detail` (e.g. a lock refusal carrying the open rollout) says
 *  what happened in its `message`; a bare `[object Object]` says nothing. */
export function detailText(detail: unknown): string {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  if (typeof detail === "object" && typeof (detail as { message?: unknown }).message === "string") {
    return (detail as { message: string }).message;
  }
  return JSON.stringify(detail);
}

export class ApiError extends Error {
  /**
   * Stable error code from `core/error_codes.py`, e.g. `VEL-DOC-STORAGE-503`.
   *
   * The backend has emitted `{"detail": …, "error": {"code", "docs"}}` since
   * the catalog was wired, but nothing read it — so the code appeared in the
   * response and the server log and then died at the network boundary, which
   * is the one place a user could have used it. Undefined for the many
   * subsystems that still raise a plain `HTTPException`.
   */
  code?: string;
  /** Deep link the server supplies alongside the code, e.g. `/docs/errors#VEL-…`. */
  docs?: string;

  constructor(public status: number, message: string, coded?: { code?: string; docs?: string }) {
    super(message);
    this.name = "ApiError";
    this.code = coded?.code;
    this.docs = coded?.docs;
  }
}

/**
 * Pull the coded-error block off a parsed error body.
 *
 * Defensive on purpose: this runs on a response that already failed, so a
 * body in an unexpected shape must yield "no code" rather than throw and
 * replace a useful HTTP error with a TypeError.
 */
/**
 * Build an ApiError from a failed Response, reading the coded block off it.
 *
 * Several modules predate the shared client and hand-roll their own fetch
 * helpers. Those threw `new Error(\`${url} → ${status}\`)` without ever reading
 * the body — so the error code the API returns died at the network boundary
 * and `<ErrorCodeLink>` had nothing to show. This gives them a one-line way to
 * keep it without adopting the whole client.
 *
 * Never throws: it runs on a response that has already failed, and a
 * non-JSON body (a gateway HTML page, say) must still yield a useful error
 * rather than a parse exception.
 */
export async function errorFromResponse(res: Response, fallback?: string): Promise<ApiError> {
  let body: any = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON error body — still an error, just an uncoded one */
  }
  const detail =
    typeof body?.detail === "string" ? body.detail
    : body?.detail ? JSON.stringify(body.detail)
    : fallback || res.statusText || `Request failed (${res.status})`;
  return new ApiError(res.status, detail, codedFrom(body));
}

export function codedFrom(body: any): { code?: string; docs?: string } | undefined {
  const e = body?.error;
  if (!e || typeof e !== "object") return undefined;
  const code = typeof e.code === "string" ? e.code : undefined;
  return code ? { code, docs: typeof e.docs === "string" ? e.docs : undefined } : undefined;
}

/* ── Processes ──────────────────────────────────────────────────── */

export async function listProcesses(): Promise<ProcessListResponse> {
  return request("/processes");
}

export async function getProcess(processId: string): Promise<DeployResponse> {
  return request(`/processes/${processId}`);
}

export async function deployProcess(
  bpmnXml: string,
  name?: string,
  tags?: Record<string, string>
): Promise<DeployResponse> {
  return request("/processes/deploy", {
    method: "POST",
    body: JSON.stringify({ bpmn_xml: bpmnXml, name, tags }),
  });
}

/* ── Instances ─────────────────────────────────────────────────── */

export async function listInstances(processId: string): Promise<InstanceListResponse> {
  return request(`/processes/${processId}/instances`);
}

export async function getInstance(
  processId: string,
  instanceId: string
): Promise<InstanceDetail> {
  return request(`/processes/${processId}/instances/${instanceId}`);
}

export async function startInstance(
  processId: string,
  variables?: Record<string, unknown>,
  businessKey?: string
): Promise<{ instance_id: string; status: string }> {
  return request(`/processes/${processId}/start`, {
    method: "POST",
    body: JSON.stringify({ variables: variables || {}, business_key: businessKey }),
  });
}

export async function cancelInstance(
  processId: string,
  instanceId: string
): Promise<InstanceDetail> {
  return request(`/processes/${processId}/instances/${instanceId}/cancel`, {
    method: "POST",
  });
}

/* ── Health ─────────────────────────────────────────────────────── */

export async function getHealth(): Promise<{ status: string }> {
  return request("/health");
}

export async function getReady(): Promise<{
  status: string;
  database: string;
  temporal: string;
}> {
  return request("/ready");
}

export async function deleteProcess(processId: string): Promise<void> {
  await request(`/processes/${processId}`, { method: "DELETE" });
}

export async function getProcessDetail(processId: string): Promise<DeployResponse & { bpmn_xml: string | null }> {
  return request(`/processes/${processId}`);
}

export async function completeUserTask(
  processId: string,
  instanceId: string,
  taskId: string,
  variables: Record<string, unknown>,
): Promise<InstanceDetail> {
  return request(`/processes/${processId}/instances/${instanceId}/complete-task`, {
    method: "POST",
    body: JSON.stringify({ task_id: taskId, variables }),
  });
}

export async function createProcessSchedule(
  processId: string,
  cron: string,
  variables?: Record<string, unknown>,
  description?: string,
): Promise<{ schedule_id: string; process_id: string; cron: string; status: string }> {
  return request(`/processes/${processId}/schedules`, {
    method: "POST",
    body: JSON.stringify({ cron, variables: variables || {}, description }),
  });
}

/* ═══════════════════════════════════════════════════════════════════
   Case Management API — calls go through /api/v1 → case-service:8200
   ═══════════════════════════════════════════════════════════════════ */

import type {
  CaseAssignment,
  CaseAuditEntry,
  CaseListResponse,
  CaseRelationship,
  CaseSummary,
  CaseTypeListResponse,
  CaseTypeSummary,
  QueueStats,
  SLAStatusInfo,
  WorkQueueSummary,
} from "@shared/types";

const CASE_BASE = "/api/v1";

/**
 * Join a caller-supplied path onto CASE_BASE, tolerating a path that already
 * carries the prefix.
 *
 * Callers are meant to pass `/risk/packs`, not `/api/v1/risk/packs`. Passing the
 * full path used to produce `/api/v1/api/v1/risk/packs` — a plain routing 404
 * that screens then misread as "feature not enabled", because HxSentinel's
 * dark-mode gate answers 404 by design and the two are indistinguishable by
 * status alone. Normalising here makes both spellings correct.
 */
function caseUrl(path: string): string {
  return path.startsWith(`${CASE_BASE}/`) ? path : `${CASE_BASE}${path}`;
}

async function caseRequest<T>(path: string, options?: RequestInit): Promise<T> {
  // Same spread order as request(): options first, headers last — a caller's
  // `headers` must never replace the merged object and drop Authorization.
  const res = await fetch(caseUrl(path), {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...options?.headers },
  });

  if (res.status === 401) {
    const newToken = await attemptRefresh();
    if (newToken) {
      const retry = await fetch(caseUrl(path), {
        ...options,
        headers: { "Content-Type": "application/json", ...authHeaders(newToken), ...options?.headers },
      });
      if (retry.status === 401) {
        window.dispatchEvent(new Event("velaris:unauthorized"));
        throw new ApiError(401, "Session expired. Please log in again.");
      }
      if (!retry.ok) {
        const body = await retry.json().catch(() => ({ detail: retry.statusText }));
        throw new ApiError(retry.status, detailText(body.detail) || retry.statusText, codedFrom(body));
      }
      return parseBody<T>(retry);
    }
    window.dispatchEvent(new Event("velaris:unauthorized"));
    throw new ApiError(401, "Session expired. Please log in again.");
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, detailText(body.detail) || res.statusText, codedFrom(body));
  }

  return parseBody<T>(res);
}

/* ── HxStream ──────────────────────────────────────────────────── */

/** Fire-and-forget UI interaction event — never throws. */
export function emitUIEvent(
  action: string,
  payload: Record<string, unknown> = {},
  caseId?: string,
): void {
  fetch(`${CASE_BASE}/hxstream/event`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      event_type: "ui_interaction",
      case_id: caseId ?? null,
      payload: { action, ...payload },
    }),
  }).catch(() => { /* swallow — HxStream must never break the UI */ });
}

/* ── Case Types ────────────────────────────────────────────────── */

export async function listCaseTypes(page = 1, tenantId?: string): Promise<CaseTypeListResponse> {
  const qs = new URLSearchParams({ page: String(page) });
  if (tenantId) qs.set("tenant_id", tenantId);
  return caseRequest(`/case-types?${qs}`);
}

export async function getCaseType(id: string): Promise<CaseTypeSummary> {
  return caseRequest(`/case-types/${id}`);
}

export async function getDataModel(id: string): Promise<{ id: string; name: string; definition_json: { fields: { name: string; field_type: string }[] } }> {
  return caseRequest(`/data-models/${id}`);
}

export async function deployCaseType(body: {
  name: string;
  version: string;
  tenant_id?: string | null;      // null / undefined = global
  lifecycle_process_id?: string | null;
  definition_json: Record<string, unknown>;
  default_priority?: string;
  description?: string;
  tags?: string[];
  icon?: string;
  color?: string;
}): Promise<CaseTypeSummary> {
  return caseRequest("/case-types", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function deleteCaseType(id: string): Promise<void> {
  await caseRequest(`/case-types/${id}`, { method: "DELETE" });
}

/* ── Cases ─────────────────────────────────────────────────────── */

export async function listCases(params?: {
  status?: string;
  priority?: string;
  case_type_id?: string;
  /** Indexed-variable filters, each "namespace.name:value" (AND semantics) */
  vars?: string[];
  page?: number;
  page_size?: number;
}): Promise<CaseListResponse> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.priority) qs.set("priority", params.priority);
  if (params?.case_type_id) qs.set("case_type_id", params.case_type_id);
  for (const v of params?.vars ?? []) qs.append("var", v);
  if (params?.page) qs.set("page", String(params.page));
  if (params?.page_size) qs.set("page_size", String(params.page_size));
  const q = qs.toString();
  return caseRequest(`/cases${q ? `?${q}` : ""}`);
}

export async function getCase(id: string): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}`);
}

/**
 * Everything the case detail view needs, in one request.
 *
 * Replaces five calls — case, history, SLA, case type, my-task — one of which
 * was a real waterfall: the case type could not be requested until the case had
 * returned its `case_type_id`.
 *
 * The individual getters stay exported and live; other callers still use them,
 * and marketplace packages call the REST paths directly. This is additive.
 */
export async function getCaseDetail(id: string): Promise<{
  case: CaseSummary;
  history: CaseAuditEntry[];
  slas: SLAStatusInfo[];
  case_type: CaseTypeSummary | null;
  my_task: MyTaskResult | null;
}> {
  return caseRequest(`/cases/${id}/detail`);
}

export async function createCase(body: {
  case_type_id: string;
  data?: Record<string, unknown>;
  priority?: string;
}): Promise<CaseSummary> {
  return caseRequest("/cases", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateCaseData(
  id: string,
  data: Record<string, unknown>
): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ data }),
  });
}

/**
 * Escape hatch for the case-config authoring screen.
 *
 * `caseRequest` is module-private on purpose — every other caller goes through a
 * named helper so the URL shape lives in one file. The config screen is CRUD
 * over two tables with no interesting shape of its own, and eight near-identical
 * wrappers would be more code than the screen. This keeps the base-URL, auth and
 * 204 handling that `caseRequest` provides.
 */
export async function caseConfigRequest<T>(path: string, options?: RequestInit): Promise<T> {
  return caseRequest<T>(path, options);
}


// ── Case actions (migration 198) ──────────────────────────────────────────
//
// Six named routes — /status, /priority, /resolve, /close, /reopen, /cancel —
// became one dispatcher: `POST /cases/{id}/actions/{action}`. The helpers below
// keep their names and signatures so no calling screen changes, but each is now
// one line over the dispatcher.
//
// Bulk is the SAME endpoint over many ids (`bulkCaseAction`), not a second
// implementation, which is why an action added to the registry is immediately
// available in bulk with nothing more written here.

export interface CaseActionResult {
  case_id: string;
  action: string;
  ok: boolean;
  detail?: Record<string, unknown>;
  code?: string;
  error?: string;
  case?: CaseSummary;
}

export interface BulkActionResult {
  action: string;
  requested: number;
  succeeded: number;
  /** Partial success is the NORMAL outcome — always render this, never assume 0. */
  failed: number;
  results: CaseActionResult[];
}

export interface CaseActionDef {
  name: string;
  label: string;
  privilege: string;
  status_effect: string | null;
  effects: string[];
  summary: string;
  destructive: boolean;
  reversible_by: string | null;
  required_params: string[];
  allowed_from: string[];
  /** False = the caller lacks the privilege; the button should not be offered. */
  permitted: boolean;
}

export interface CaseStatusDef {
  name: string;
  label: string;
  category: "open" | "working" | "waiting" | "terminal";
  is_terminal: boolean;
  is_default: boolean;
  allowed_from: string[];
  sort_order: number;
  enabled: boolean;
}

/**
 * What this operator may do, and the status set in force. Render the action bar
 * from this.
 *
 * Pass `caseId` on a single-case screen: the answer then also drops the actions
 * the case's STAGE does not offer, and carries a `stage` block so the screen can
 * say why. Without it the answer is tenant-wide — right for a bulk bar, which
 * spans many stages, and wrong for one case.
 */
export async function caseActionCatalogue(caseTypeId?: string, caseId?: string): Promise<{
  actions: CaseActionDef[];
  statuses: CaseStatusDef[];
  bulk_ceiling: number;
  /** Whether "Move to stage" may be offered — its own privilege. Absent on an older server. */
  bulk_stage_permitted?: boolean;
  stage?: { id: string; name: string; actions_allowed: string[] | null; on_enter_status: string | null } | null;
}> {
  const params = new URLSearchParams();
  if (caseTypeId) params.set("case_type_id", caseTypeId);
  if (caseId) params.set("case_id", caseId);
  const q = params.toString() ? `?${params}` : "";
  return caseRequest(`/cases/actions/catalogue${q}`);
}

export async function performCaseAction(
  id: string,
  action: string,
  params: Record<string, unknown> = {},
  reason?: string
): Promise<CaseActionResult> {
  return caseRequest(`/cases/${id}/actions/${action}`, {
    method: "POST",
    body: JSON.stringify({ params, reason }),
  });
}

/**
 * One action across many cases.
 *
 * Over the ceiling the server REFUSES rather than truncating, so a caller must
 * surface that error rather than assuming the first N were applied.
 */
export async function bulkCaseAction(
  caseIds: string[],
  action: string,
  params: Record<string, unknown> = {},
  reason?: string
): Promise<BulkActionResult> {
  return caseRequest(`/cases/bulk`, {
    method: "POST",
    body: JSON.stringify({ case_ids: caseIds, action, params, reason }),
  });
}

// ── Bulk STAGE transition ────────────────────────────────────────────────────
//
// Two calls, and the second is refused without the first. A stage move fires the
// stage's SLA, its on_enter_status and workflow signals per case, and moving
// back undoes none of it — so the server issues a confirmation for exactly the
// cases a preview found movable, and `bulkStageExecute` must send that set back
// with it. Changing the selection or the stage means previewing again.

export interface BulkStageEffect {
  case_type_id: string;
  case_type_name: string | null;
  stage_name: string;
  cases: number;
  starts_sla: string | null;
  on_enter_status: string | null;
  signals_workflows: boolean;
}

export interface BulkStagePreview {
  target_stage_id: string;
  requested: number;
  would_move: number;
  unchanged: number;
  refused: number;
  effects: BulkStageEffect[];
  results: { case_id: string; ok: boolean; from_stage?: string | null; unchanged?: boolean; code?: string; error?: string }[];
  eligible_case_ids: string[];
  /** Null when nothing would move — there is nothing to confirm. */
  confirmation_token: string | null;
  confirm_expires_at: number | null;
}

export interface BulkStageResult {
  target_stage_id: string;
  bulk_run_id: string;
  requested: number;
  moved: number;
  unchanged: number;
  /** Partial success is normal — cases are re-checked at execution. */
  failed: number;
  results: CaseActionResult[];
}

export async function bulkStagePreview(caseIds: string[], targetStageId: string): Promise<BulkStagePreview> {
  return caseRequest(`/cases/bulk/stage/preview`, {
    method: "POST",
    body: JSON.stringify({ case_ids: caseIds, target_stage_id: targetStageId }),
  });
}

export async function bulkStageExecute(
  preview: BulkStagePreview,
  reason?: string
): Promise<BulkStageResult> {
  return caseRequest(`/cases/bulk/stage`, {
    method: "POST",
    body: JSON.stringify({
      case_ids: preview.eligible_case_ids,
      target_stage_id: preview.target_stage_id,
      confirmation_token: preview.confirmation_token,
      reason: reason || undefined,
    }),
  });
}

export async function changeCaseStatus(
  id: string,
  status: string,
  reason?: string
): Promise<CaseActionResult> {
  return performCaseAction(id, "change_status", { status }, reason);
}

export async function changeCasePriority(
  id: string,
  priority: string
): Promise<CaseActionResult> {
  return performCaseAction(id, "change_priority", { priority });
}

export async function resolveCase(
  id: string,
  resolution?: Record<string, unknown>
): Promise<CaseActionResult> {
  return performCaseAction(id, "resolve", { resolution });
}

export async function closeCase(id: string): Promise<CaseActionResult> {
  return performCaseAction(id, "close");
}

export async function reopenCase(id: string, reason?: string): Promise<CaseActionResult> {
  return performCaseAction(id, "reopen", {}, reason);
}

export async function cancelCase(id: string, reason?: string): Promise<CaseActionResult> {
  return performCaseAction(id, "cancel", {}, reason);
}

export async function transitionStage(
  id: string,
  targetStageId: string
): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/stage`, {
    method: "POST",
    body: JSON.stringify({ target_stage_id: targetStageId }),
  });
}

export async function getCaseHistory(id: string): Promise<CaseAuditEntry[]> {
  return caseRequest(`/cases/${id}/history`);
}

// P38 — Step completions
export async function completeStep(
  caseId: string,
  stepId: string,
  body: { stage_id: string; step_type: string; status: string; data: Record<string, unknown> }
): Promise<{ auto_advanced: boolean; advance_held_by_risk?: boolean; status: string; [k: string]: unknown }> {
  return caseRequest(`/cases/${caseId}/steps/${stepId}/complete`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listStepCompletions(
  caseId: string,
  stageId?: string
): Promise<Array<{ step_id: string; status: string; data: Record<string, unknown>; step_type: string }>> {
  const qs = stageId ? `?stage_id=${stageId}` : "";
  return caseRequest(`/cases/${caseId}/step-completions${qs}`);
}

export interface MyTaskResult {
  assignment_id: string;
  case_id: string;
  case_number: string | null;
  case_description: string | null;
  case_priority: string | null;
  stage_id: string;
  step_id: string;
  step_def: Record<string, unknown>;
  form_id: string | null;
  completion: Record<string, unknown> | null;
  locked_by: string | null;
  lock_expires_at: string | null;
  is_locked_by_me: boolean;
}

export async function getMyTask(caseId: string): Promise<MyTaskResult | null> {
  const res = await fetch(`${BASE}/v1/cases/${caseId}/my-task`, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  if (!res.ok) return null;
  const json = await res.json();
  return json ?? null;
}

export async function unlockStep(caseId: string, stepId: string): Promise<void> {
  await fetch(`${BASE}/v1/cases/${caseId}/steps/${encodeURIComponent(stepId)}/unlock`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
}

export async function getCaseRelationships(id: string): Promise<CaseRelationship[]> {
  return caseRequest(`/cases/${id}/relationships`);
}

export async function getCaseChildren(id: string): Promise<CaseSummary[]> {
  return caseRequest(`/cases/${id}/children`);
}

export async function getCaseSLA(id: string): Promise<SLAStatusInfo[]> {
  return caseRequest(`/cases/${id}/sla`);
}

/* ── Assignments ───────────────────────────────────────────────── */

export async function getMyAssignments(): Promise<CaseAssignment[]> {
  return caseRequest("/my/assignments");
}

export async function getMyWorkload(): Promise<{
  user_id: string;
  active_count: number;
  assignment_ids: string[];
}> {
  return caseRequest("/my/workload");
}

export async function claimAssignment(
  assignmentId: string,
  userId: string
): Promise<CaseAssignment> {
  return caseRequest(`/assignments/${assignmentId}/claim`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

export async function dismissAssignment(assignmentId: string): Promise<void> {
  await caseRequest(`/assignments/${assignmentId}`, { method: "DELETE" });
}

export async function completeAssignment(
  assignmentId: string,
  result?: Record<string, unknown>
): Promise<CaseAssignment> {
  return caseRequest(`/assignments/${assignmentId}/complete`, {
    method: "POST",
    body: JSON.stringify({ result }),
  });
}

/* ── Work Queues ───────────────────────────────────────────────── */

export async function listQueues(tenantId?: string): Promise<WorkQueueSummary[]> {
  const qs = tenantId ? `?tenant_id=${tenantId}` : "";
  return caseRequest(`/queues${qs}`);
}

export async function getQueueItems(
  queueId: string,
  page = 1,
  pageSize = 50,
  status?: string,
  search?: string,
): Promise<CaseAssignment[]> {
  const p = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (status) p.set("status", status);
  if (search)  p.set("search", search);
  return caseRequest(`/queues/${queueId}/items?${p}`);
}

export async function getQueueItemCount(
  queueId: string,
  status?: string,
  search?: string,
): Promise<{ count: number }> {
  const p = new URLSearchParams();
  if (status) p.set("status", status);
  if (search)  p.set("search", search);
  return caseRequest(`/queues/${queueId}/items/count?${p}`);
}

export async function getQueueStats(queueId: string): Promise<QueueStats> {
  return caseRequest(`/queues/${queueId}/stats`);
}

/* ── Forms (Phase 7) ───────────────────────────────────────────── */

export async function listForms(): Promise<any> {
  return caseRequest("/forms");
}

export async function getForm(formId: string): Promise<any> {
  return caseRequest(`/forms/${formId}`);
}

/** Connectors a form can use — names only matter to the builder's pickers. */
export interface ConnectorSummary { id: string; name: string; connector_type: string; enabled: boolean;
  /** Named operations (2026-11-04); empty = the connector's one request. */
  operations?: { key: string; label: string; method: string }[] }
export async function listFormConnectors(): Promise<ConnectorSummary[]> {
  const r = await caseRequest<{ connectors?: ConnectorSummary[] }>("/hxbridge/connectors?enabled=true");
  return r.connectors ?? [];
}

/* ── Reference tables (Form Builder v2 step 3) ─────────────────── */

export type RefColumn = { key: string; label: string; type: "text" | "number" | "date" | "boolean" };
export type RefTableSummary = { id: string; name: string; description: string; columns: RefColumn[];
  row_count: number; platform: boolean; updated_at: string | null };
export type RefRow = { data: Record<string, any>; is_active: boolean };

export async function listReferenceTables(): Promise<RefTableSummary[]> {
  return (await caseRequest<{ items: RefTableSummary[] }>("/reference-tables")).items ?? [];
}
export async function getReferenceTable(id: string, q = ""): Promise<RefTableSummary & { rows: RefRow[] }> {
  return caseRequest(`/reference-tables/${id}${q ? `?q=${encodeURIComponent(q)}` : ""}`);
}
export async function createReferenceTable(body: { name: string; description?: string; columns: RefColumn[] }): Promise<RefTableSummary> {
  return caseRequest("/reference-tables", { method: "POST", body: JSON.stringify(body) });
}
export async function updateReferenceTable(id: string, body: { name?: string; description?: string; columns?: RefColumn[] }): Promise<RefTableSummary> {
  return caseRequest(`/reference-tables/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}
export async function replaceReferenceRows(id: string, rows: RefRow[]): Promise<RefTableSummary> {
  return caseRequest(`/reference-tables/${id}/rows`, { method: "PUT", body: JSON.stringify({ rows }) });
}
export async function deleteReferenceTable(id: string): Promise<void> {
  await caseRequest(`/reference-tables/${id}`, { method: "DELETE" });
}

/** Run a form lookup. The server runs what the SAVED form says, unless the caller may run connectors by hand. */
export async function runFormLookup(connectorId: string, body: {
  input_data: Record<string, any>; form_id?: string; field_key?: string; case_id?: string;
  /** Used only for an integration administrator's Test; others run the saved field's. */
  operation?: string;
}): Promise<{ ok: boolean; result?: any; error?: string }> {
  return caseRequest(`/hxbridge/connectors/${connectorId}/form-lookup`, { method: "POST", body: JSON.stringify(body) });
}

/** The case variables a form's fields can be pre-filled from (designers only). */
export interface FormVariable { full_key: string; namespace: string | null; label: string | null; var_type: string; case_types: string[] }
/* ── Reusable form sections + form versions per case (Form Builder §5 step 6) ── */

export type FormSectionBody = { title: string; description?: string; fields: any[] };
export type ReusableSection = {
  id: string; name: string; description: string; version: number; section: FormSectionBody;
  platform: boolean; updated_at: string | null; used_by?: string[]; updated_forms?: string[];
};

export async function listFormSections(): Promise<{ items: ReusableSection[] }> {
  return caseRequest("/form-sections");
}
export async function getFormSection(id: string): Promise<ReusableSection> {
  return caseRequest(`/form-sections/${id}`);
}
export async function createFormSection(body: { name: string; description?: string; section: FormSectionBody }): Promise<ReusableSection> {
  return caseRequest("/form-sections", { method: "POST", body: JSON.stringify(body) });
}
/** Changes the section and every form that links it — or nothing (409 / 422 name the form). */
export async function updateFormSection(id: string, body: { name: string; description?: string; section: FormSectionBody }): Promise<ReusableSection> {
  return caseRequest(`/form-sections/${id}`, { method: "PUT", body: JSON.stringify(body) });
}
export async function deleteFormSection(id: string): Promise<void> {
  await caseRequest(`/form-sections/${id}`, { method: "DELETE" });
}
/** A form as this case uses it: its pinned version when the form is versioned per case. */
export async function getCaseForm(caseId: string, formId: string): Promise<any> {
  return caseRequest(`/form-submissions/cases/${caseId}/forms/${formId}`);
}

/** A person's unfinished answers to a case's form (§5 step 7) — theirs only, never checked. */
export type FormDraft = { values: Record<string, any>; updated_at: string | null };
export async function getFormDraft(caseId: string, formId: string): Promise<FormDraft | null> {
  return (await caseRequest<any>(`/form-submissions/cases/${caseId}/forms/${formId}/draft`)).draft;
}
export async function saveFormDraft(caseId: string, formId: string, values: Record<string, any>): Promise<FormDraft> {
  return (await caseRequest<any>(`/form-submissions/cases/${caseId}/forms/${formId}/draft`, {
    method: "PUT", body: JSON.stringify({ values }) })).draft;
}
export async function discardFormDraft(caseId: string, formId: string): Promise<void> {
  await caseRequest(`/form-submissions/cases/${caseId}/forms/${formId}/draft`, { method: "DELETE" });
}

/** The access roles a field can be limited to (field-level security, §5 step 7). */
export async function getFormAccessRoles(): Promise<{ id: string; name: string }[]> {
  return (await caseRequest<any>("/forms/access-roles")).roles;
}

export type FormTestResult = {
  name: string; passed: boolean; problems: string[];
  errors: { field_key: string; message: string }[]; values: Record<string, any>;
};
/** Run a form's test scenarios through the server's own check (§5 step 7). */
export async function runFormTests(definition: Record<string, any>, tests: any[]): Promise<{
  results: FormTestResult[]; passed: number; total: number;
}> {
  return caseRequest<any>("/forms/test-run", { method: "POST", body: JSON.stringify({ definition, tests }) });
}

/** A tenant's approved sites for the form Embed field (2026-10-29). */
/** What a tenant's forms may embed (Admin > Security > Allowed sites, 2026-11-04).
 *  `allowed_origins` is the effective list: the tenant's own (`own_origins`, when
 *  `may_add`) and the platform's, minus the blocked. An older server sends only
 *  the first two keys; the rest default to "as before". */
export type EmbedSettings = {
  allowed_origins: string[]; portal: boolean;
  own_origins?: string[]; platform_origins?: string[]; blocked?: string[];
  may_add?: boolean; allow_forms?: boolean;
};

export async function getEmbedSettings(): Promise<EmbedSettings> {
  return caseRequest("/forms/embed-settings");
}

/** Admin only — the server refuses anyone else. Sends the tenant's OWN list. */
export async function putEmbedSettings(body: { allowed_origins: string[]; portal: boolean }): Promise<EmbedSettings> {
  return caseRequest("/forms/embed-settings", { method: "PUT", body: JSON.stringify(body) });
}

/** The platform operator's policy for embedded sites. */
export type AllowedSitesPolicy = {
  platform_origins: string[]; blocked: string[]; tenants_may_add: boolean; allow_forms: boolean;
};

export async function getAllowedSitesPolicy(): Promise<AllowedSitesPolicy> {
  return caseRequest("/forms/allowed-sites/policy");
}

export async function putAllowedSitesPolicy(body: AllowedSitesPolicy): Promise<AllowedSitesPolicy> {
  return caseRequest("/forms/allowed-sites/policy", { method: "PUT", body: JSON.stringify(body) });
}

export async function getFormVariables(formId: string): Promise<{
  case_types: { id: string; name: string }[]; variables: FormVariable[];
}> {
  return caseRequest(`/forms/${formId}/variables`);
}

export async function createForm(body: {
  name: string;
  version: string;
  data_model_id?: string;
  definition_json: Record<string, unknown>;
}): Promise<any> {
  return caseRequest("/forms", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateForm(formId: string, body: {
  definition_json?: Record<string, unknown>;
  data_model_id?: string;
  version?: string;
}): Promise<any> {
  return caseRequest(`/forms/${formId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function deleteForm(formId: string): Promise<void> {
  await caseRequest(`/forms/${formId}`, { method: "DELETE" });
}

export async function getAssignmentForm(assignmentId: string): Promise<{
  has_form: boolean;
  form: { id: string; name: string; version: string; definition_json: Record<string, any> } | null;
}> {
  return caseRequest(`/form-submissions/${assignmentId}/form`);
}

export async function getCaseVariables(caseId: string): Promise<{
  case_id: string;
  variables: Record<string, any>;
}> {
  return caseRequest(`/variables/cases/${caseId}`);
}

export async function listCaseShares(caseId: string): Promise<
  { user_id: string; relation: string; username?: string | null; display_name?: string | null; created_by: string | null; created_at: string | null }[]
> {
  return caseRequest(`/cases/${caseId}/shares`);
}

export async function shareCase(caseId: string, userId: string, relation: string): Promise<void> {
  await caseRequest(`/cases/${caseId}/shares`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, relation }),
  });
}

export async function unshareCase(caseId: string, userId: string, relation: string): Promise<void> {
  await caseRequest(`/cases/${caseId}/shares?user_id=${encodeURIComponent(userId)}&relation=${encodeURIComponent(relation)}`, {
    method: "DELETE",
  });
}

/* ── HxMeet — real-time case sessions ─────────────────────────── */

export interface CaseSession {
  id: string;
  case_id: string;
  driver: string;
  provider: string;
  status: string;
  record_intent?: boolean;
  recording_status?: string;   // none | recording | processing | sealed | failed
  recording_document_id?: string | null;
  transcript_status?: string;  // none | sealed | failed (P4a-live-2)
  transcript_document_id?: string | null;
  title: string | null;
  join_url: string | null;
  external_meeting_id: string | null;
  started_by: string;
  started_at: string | null;
  ended_at: string | null;
  created_at: string | null;
}

export interface MeetProvider {
  connector_id: string;
  name: string;
  provider: string;
  is_default: boolean;
}

export interface MeetRoomToken {
  url: string;
  token: string;
  room: string;
  identity: string;
  display_name?: string | null;
  title?: string | null;
  session_id?: string;
  record_intent?: boolean;
}

export interface MeetGuestInvite {
  participant_id: string;
  identity: string;
  invite_token: string;
  join_path: string;
  /** Absolute, when the deployment has said where Studio lives
   *  (`VELARIS_CASE_PUBLIC_BASE_URL`). Null otherwise — the browser's own
   *  origin is the fallback, which is right for a link a host copies out and
   *  wrong for one a server sends on. */
  join_url: string | null;
  expires_at: string;
}

export interface MeetParticipant {
  id: string;
  identity: string;
  display_name: string | null;
  role: string;
  invited_by: string | null;
  joined_at: string | null;
  left_at: string | null;
}

export async function listMeetProviders(): Promise<{
  driver: string; embedded_available: boolean; providers: MeetProvider[];
}> {
  return caseRequest(`/meet/providers`);
}

export async function getMeetSessionToken(sessionId: string): Promise<MeetRoomToken> {
  return caseRequest(`/meet/sessions/${sessionId}/token`, { method: "POST" });
}

export async function startMeetRecording(sessionId: string): Promise<CaseSession> {
  return caseRequest(`/meet/sessions/${sessionId}/recording/start`, { method: "POST" });
}

export async function stopMeetRecording(sessionId: string): Promise<CaseSession> {
  return caseRequest(`/meet/sessions/${sessionId}/recording/stop`, { method: "POST" });
}

export async function verifyMeetRecording(sessionId: string): Promise<{
  verified: boolean; sha256?: string; sealed_sha256?: string | null; anchor_ref?: string; reason?: string;
}> {
  return caseRequest(`/meet/sessions/${sessionId}/recording/verify`);
}

// ── HxMeet P4a — session intelligence (local transcription + summary) ──

export type SessionIntelligence = {
  status: "none" | "pending" | "running" | "completed" | "failed";
  transcript_document_id?: string | null;
  summary?: string | null;
  action_items?: string[];
  language?: string | null;
  duration_seconds?: number | null;
  model_versions?: Record<string, string>;
  error?: string | null;
  completed_at?: string | null;
};

export async function runSessionIntelligence(sessionId: string): Promise<{ status: string }> {
  return caseRequest(`/meet/sessions/${sessionId}/intelligence`, { method: "POST" });
}

export async function getSessionIntelligence(sessionId: string): Promise<SessionIntelligence> {
  return caseRequest(`/meet/sessions/${sessionId}/intelligence`);
}

// ── HxMeet P4b — document verification (the document-first KYC gate) ──

export type DocCheck = { name: string; result: "pass" | "fail" | "skipped"; detail: string };

export async function verifyDocument(documentId: string, body: {
  status: "passed" | "failed" | "review";
  mrz_line2?: string; expiry_date?: string;
  checklist?: Record<string, boolean>; notes?: string;
}): Promise<{ id: string; status: string; checks: DocCheck[] }> {
  return caseRequest(`/documents/${documentId}/verify`, { method: "POST", body: JSON.stringify(body) });
}

export async function listDocumentVerifications(documentId: string): Promise<{
  verifications: { id: string; status: string; checks: DocCheck[]; verified_by: string; notes: string | null; created_at: string | null }[];
}> {
  return caseRequest(`/documents/${documentId}/verifications`);
}

// ── HxMeet P4c — Video-KYC liveness challenges ──

export type MeetChallenge = {
  id: string;
  session_id: string;
  kind: "head_turn" | "phrase_readback" | "document_tilt";
  payload: { instruction?: string; phrase?: string; sequence?: string[]; side?: string };
  issued_by: string;
  issued_at: string | null;
  result: "pending" | "passed" | "failed" | "skipped";
  result_notes: string | null;
  result_by: string | null;
  result_at: string | null;
};

export async function issueMeetChallenges(sessionId: string, kinds?: string[]): Promise<{ challenges: MeetChallenge[] }> {
  return caseRequest(`/meet/sessions/${sessionId}/challenges`, {
    method: "POST", body: JSON.stringify({ kinds: kinds ?? null }),
  });
}

export async function listMeetChallenges(sessionId: string): Promise<{ challenges: MeetChallenge[] }> {
  return caseRequest(`/meet/sessions/${sessionId}/challenges`);
}

export async function recordMeetChallengeResult(
  sessionId: string, challengeId: string, result: "passed" | "failed" | "skipped", notes?: string,
): Promise<MeetChallenge> {
  return caseRequest(`/meet/sessions/${sessionId}/challenges/${challengeId}/result`, {
    method: "POST", body: JSON.stringify({ result, notes: notes ?? null }),
  });
}

export type KycCheck = {
  name: string; score?: number; skipped?: boolean; detail: string; model?: string; challenge_id?: string;
};

export type KycAnalysis = {
  status: "none" | "pending" | "running" | "completed" | "failed";
  risk_score?: number | null;
  review_recommended?: boolean | null;
  review_threshold?: number;
  checks?: KycCheck[];
  challenge_checks?: KycCheck[];
  model_versions?: Record<string, string>;
  error?: string | null;
  completed_at?: string | null;
};

export async function runKycAnalysis(sessionId: string): Promise<{ status: string }> {
  return caseRequest(`/meet/sessions/${sessionId}/kyc-analysis`, { method: "POST" });
}

export async function getKycAnalysis(sessionId: string): Promise<KycAnalysis> {
  return caseRequest(`/meet/sessions/${sessionId}/kyc-analysis`);
}

/** Public, non-consuming invite preview — recording notice before consent. */
export async function previewMeetGuestInvite(inviteToken: string): Promise<{
  title: string | null; display_name: string | null; record_intent: boolean;
  biometric_notice?: boolean;
}> {
  const res = await fetch(`/api/v1/meet/guest/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invite_token: inviteToken }),
  });
  if (!res.ok) throw new ApiError(res.status, "This invite link is invalid, already used, or expired.");
  return res.json();
}

export async function inviteMeetGuest(
  sessionId: string,
  body: { customer_id?: string; email?: string; display_name?: string },
): Promise<MeetGuestInvite> {
  return caseRequest(`/meet/sessions/${sessionId}/invites`, {
    method: "POST", body: JSON.stringify(body),
  });
}

export async function listMeetParticipants(
  sessionId: string,
): Promise<{ participants: MeetParticipant[] }> {
  return caseRequest(`/meet/sessions/${sessionId}/participants`);
}

/** Public guest exchange — no auth header on purpose (guests have no account). */
export async function exchangeMeetGuestToken(inviteToken: string): Promise<MeetRoomToken> {
  const res = await fetch(`/api/v1/meet/guest/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invite_token: inviteToken }),
  });
  if (!res.ok) throw new ApiError(res.status, res.status === 404
    ? "This invite link is invalid, already used, or expired."
    : "Could not join the session.");
  return res.json();
}

export async function listCaseSessions(caseId: string): Promise<{ sessions: CaseSession[] }> {
  return caseRequest(`/meet/cases/${caseId}/sessions`);
}

export async function startCaseSession(
  caseId: string,
  body: { title?: string; provider?: string; connector_id?: string },
): Promise<CaseSession> {
  return caseRequest(`/meet/cases/${caseId}/sessions`, { method: "POST", body: JSON.stringify(body) });
}

export async function endCaseSession(sessionId: string, cancelled = false): Promise<CaseSession> {
  return caseRequest(`/meet/sessions/${sessionId}/end?cancelled=${cancelled}`, { method: "POST" });
}

/** Sealed-recording stream for the in-Studio player (meet.recording.view).
 * Returns a blob object-URL — caller MUST URL.revokeObjectURL() when done. */
export async function fetchRecordingUrl(sessionId: string): Promise<string> {
  const res = await fetch(`/api/v1/meet/sessions/${sessionId}/recording`, {
    headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail || res.statusText, codedFrom(body));
  }
  return URL.createObjectURL(await res.blob());
}

/** Sealed live transcript, unsealed server-side (meet.recording.view; every
 * fetch is audited like a recording view). Returns plain text. */
export async function fetchSessionTranscript(sessionId: string): Promise<string> {
  const res = await fetch(`/api/v1/meet/sessions/${sessionId}/transcript`, {
    headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail || res.statusText, codedFrom(body));
  }
  return res.text();
}

export async function verifySessionTranscript(sessionId: string): Promise<{
  verified: boolean; sha256?: string; sealed_sha256?: string; anchor_ref?: string; reason?: string;
}> {
  return caseRequest(`/meet/sessions/${sessionId}/transcript/verify`);
}

// ── HxNexus case-scoped Q&A ────────────────────────────────────────

export type CaseAskResult = {
  answer: string;
  sources: { sid: string; kind: string; label: string }[];
  withheld: string[];
  external_ai: boolean;
};

export async function askCase(caseId: string, question: string): Promise<CaseAskResult> {
  return caseRequest(`/hxnexus/cases/${caseId}/ask`, {
    method: "POST", body: JSON.stringify({ question }),
  });
}

// ── Case messages (Portal v2 P4 — worker ↔ customer thread) ────────

export type CaseMessage = {
  id: string; author: string; author_name: string | null;
  body: string; portal_visible: boolean; created_at: string | null;
};

export async function listCaseMessages(caseId: string): Promise<{ messages: CaseMessage[] }> {
  return caseRequest(`/cases/${caseId}/messages`);
}

export async function postCaseMessage(caseId: string, body: string, portalVisible = true): Promise<CaseMessage> {
  return caseRequest(`/cases/${caseId}/messages`, {
    method: "POST",
    body: JSON.stringify({ body, portal_visible: portalVisible }),
  });
}

export async function submitForm(assignmentId: string, body: {
  form_id: string;
  values: Record<string, any>;
  completed_by?: string;
}): Promise<any> {
  return caseRequest(`/form-submissions/${assignmentId}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function getAssignmentsForCase(caseId: string): Promise<any[]> {
  return caseRequest(`/cases/${caseId}/assignments`);
}

/* ── Analytics (Phase 9) ───────────────────────────────────────── */

export interface AnalyticsDashboard {
  overview: {
    total_cases: number;
    open_cases: number;
    resolved_cases: number;
    closed_cases: number;
    cancelled_cases: number;
    avg_resolution_hours: number | null;
    cases_created_today: number;
    cases_resolved_today: number;
  };
  status_breakdown: { status: string; count: number }[];
  priority_breakdown: { priority: string; count: number }[];
  case_type_breakdown: { case_type_id: string; case_type_name: string; count: number }[];
  sla_compliance: {
    total_sla_instances: number;
    on_track: number;
    at_risk: number;
    breached: number;
    paused: number;
    compliance_rate: number;
  };
  assignments: {
    total_assignments: number;
    active: number;
    completed: number;
    avg_completion_hours: number | null;
    unassigned: number;
  };
  cases_over_time: { date: string; count: number }[];
  resolved_over_time: { date: string; count: number }[];
}

export async function getAnalyticsDashboard(params?: {
  days?: number;
  case_type_id?: string;
  tenant_id?: string;
}): Promise<AnalyticsDashboard> {
  const qs = new URLSearchParams();
  if (params?.days) qs.set("days", String(params.days));
  if (params?.case_type_id) qs.set("case_type_id", params.case_type_id);
  if (params?.tenant_id) qs.set("tenant_id", params.tenant_id);
  const q = qs.toString();
  return caseRequest(`/analytics/dashboard${q ? `?${q}` : ""}`);
}

/* ── Admin (Phase 11) ──────────────────────────────────────────── */

export async function getSystemInfo(): Promise<any> {
  return caseRequest("/admin/system-info");
}

export async function searchAuditLog(params?: {
  action?: string;
  actor_id?: string;
  case_id?: string;
  days?: number;
  page?: number;
  page_size?: number;
}): Promise<any> {
  const qs = new URLSearchParams();
  if (params?.action) qs.set("action", params.action);
  if (params?.actor_id) qs.set("actor_id", params.actor_id);
  if (params?.case_id) qs.set("case_id", params.case_id);
  if (params?.days) qs.set("days", String(params.days));
  if (params?.page) qs.set("page", String(params.page));
  if (params?.page_size) qs.set("page_size", String(params.page_size));
  const q = qs.toString();
  return caseRequest(`/admin/audit-log${q ? `?${q}` : ""}`);
}

export async function getAuditActions(): Promise<{ actions: string[] }> {
  return caseRequest("/admin/audit-log/actions");
}

export async function createQueue(body: Record<string, any>): Promise<any> {
  return caseRequest("/admin/queues", { method: "POST", body: JSON.stringify(body) });
}

export async function updateQueue(queueId: string, body: Record<string, any>): Promise<any> {
  return caseRequest(`/admin/queues/${queueId}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function deleteQueue(queueId: string): Promise<void> {
  await caseRequest(`/admin/queues/${queueId}`, { method: "DELETE" });
}

export async function listWebhooks(): Promise<any[]> {
  return caseRequest("/webhooks");
}

export async function createWebhook(body: {
  name: string; url: string; events: string[];
  secret?: string; case_type_id?: string;
  headers?: Record<string, string>; retry_count?: number; timeout_seconds?: number;
}): Promise<any> {
  return caseRequest("/webhooks", { method: "POST", body: JSON.stringify(body) });
}

export async function updateWebhook(id: string, body: Record<string, any>): Promise<any> {
  return caseRequest(`/webhooks/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function testWebhook(id: string): Promise<{ success: boolean; status_code?: number; error?: string }> {
  return caseRequest(`/webhooks/${id}/test`, { method: "POST" });
}

export async function listWebhookEvents(): Promise<{ events: string[] }> {
  return caseRequest("/webhooks/events");
}

export async function deleteWebhook(id: string): Promise<void> {
  await caseRequest(`/webhooks/${id}`, { method: "DELETE" });
}

// ── SSO providers (Admin Console → SSO) ──────────────────────────────────────
// The admin listing is a different endpoint from the one the login page calls:
// that one is anonymous and withholds tenant_id, config and disabled rows.
export interface SamlCertHealth {
  fingerprint: string;
  not_after: string | null;
  days_remaining: number | null;
  expired: boolean;
}

export interface SsoProviderAdmin {
  id: string;
  provider: string;
  client_id: string;
  enabled: boolean;
  tenant_id: string | null;
  config: Record<string, any>;
  has_secret: boolean;
  created_at: string | null;
  /** SAML rows only. Certificate expiry is computed server-side — it means
   *  parsing DER, and the screen that warns about an expiring certificate must
   *  not be the thing that breaks when one cannot be read. */
  saml_health?: {
    certs: SamlCertHealth[];
    metadata_sync: {
      last_checked?: string; last_error?: string | null; last_error_at?: string;
      certs_added?: number; drift?: Record<string, { configured: string; published: string }> | null;
    } | null;
    /** The SP's own key pair — a summary, never the key. */
    sp_key: (SamlCertHealth & { created_at: string | null }) | null;
  };
}

/** Create (or replace) the SP key pair. Returns the certificate to hand the IdP. */
export async function generateSamlSpKey(tenant: string, regenerate = false): Promise<{
  certificate: string;
  summary: (SamlCertHealth & { created_at: string | null }) | null;
  next_step: string;
}> {
  return caseRequest(`/auth/saml/${encodeURIComponent(tenant)}/sp-key`,
    { method: "POST", body: JSON.stringify({ regenerate }) });
}

export interface SamlMetadata {
  idp_entity_id: string;
  idp_sso_url: string;
  idp_slo_url: string | null;
  idp_x509_certs: string[];
  name_id_format: string | null;
  certs: SamlCertHealth[];
  source: "url" | "xml";
}

/** Read an IdP's federation metadata. Returns the fields; saves nothing. */
export async function parseSamlMetadata(body: { url?: string; xml?: string }): Promise<SamlMetadata> {
  return caseRequest("/auth/saml/metadata/parse", { method: "POST", body: JSON.stringify(body) });
}

export async function listSsoProvidersAdmin(): Promise<SsoProviderAdmin[]> {
  const d = await caseRequest<{ providers: SsoProviderAdmin[] }>("/auth/real/sso/providers/manage");
  return d.providers ?? [];
}

export async function createSsoProvider(body: {
  provider: string; client_id: string; client_secret: string;
  tenant_id?: string | null; config?: Record<string, any>;
}): Promise<SsoProviderAdmin> {
  return caseRequest("/auth/real/sso/providers", { method: "POST", body: JSON.stringify(body) });
}

/** Omit `client_secret` (or send it blank) to keep the stored one. */
export async function updateSsoProvider(
  id: string,
  body: Partial<{
    client_id: string; client_secret: string; enabled: boolean;
    tenant_id: string | null; config: Record<string, any>;
  }>,
): Promise<SsoProviderAdmin> {
  return caseRequest(`/auth/real/sso/providers/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function deleteSsoProvider(id: string): Promise<void> {
  await caseRequest(`/auth/real/sso/providers/${id}`, { method: "DELETE" });
}

/* ── Enterprise identity (SAML + SCIM) ───────────────────────────── */

export interface ScimToken {
  id: string; name: string; tenant_id: string | null; created_by: string;
  created_at: string | null; last_used_at: string | null;
  expires_at: string | null; revoked_at: string | null;
}

export async function listScimTokens(): Promise<ScimToken[]> {
  const d = await caseRequest<{ tokens: ScimToken[] }>("/scim/tokens");
  return d.tokens ?? [];
}

/** The response carries the plaintext token, and it is the only time it exists
 *  anywhere outside the IdP — the server stores a SHA-256 and cannot show it
 *  again. */
export async function createScimToken(body: {
  name: string; tenant_id?: string | null; expires_at?: string | null;
}): Promise<{ id: string; name: string; token: string; warning: string }> {
  return caseRequest("/scim/tokens", { method: "POST", body: JSON.stringify(body) });
}

export async function revokeScimToken(id: string): Promise<void> {
  await caseRequest(`/scim/tokens/${id}`, { method: "DELETE" });
}

export interface IdentityGroupMapping {
  id: string; tenant_id: string; source: string; external_group: string;
  access_group_id: string; access_group_name: string | null;
  priority: number; created_by: string; created_at: string | null;
}

export async function listGroupMappings(params?: { tenant?: string; source?: string }):
  Promise<IdentityGroupMapping[]> {
  const q = new URLSearchParams();
  if (params?.tenant) q.set("tenant", params.tenant);
  if (params?.source) q.set("source", params.source);
  const suffix = q.toString() ? `?${q}` : "";
  const d = await caseRequest<{ mappings: IdentityGroupMapping[] }>(
    `/identity/provisioning/group-mappings${suffix}`);
  return d.mappings ?? [];
}

export async function createGroupMapping(body: {
  tenant: string; source: string; external_group: string;
  access_group_id: string; priority?: number;
}): Promise<IdentityGroupMapping> {
  return caseRequest("/identity/provisioning/group-mappings",
    { method: "POST", body: JSON.stringify(body) });
}

export async function deleteGroupMapping(id: string): Promise<void> {
  await caseRequest(`/identity/provisioning/group-mappings/${id}`, { method: "DELETE" });
}

export interface ProvisionLogEntry {
  id: string; tenant_id: string | null; source: string;
  external_id: string | null; username: string | null;
  action: string; result: string; detail: Record<string, any>;
  created_at: string | null;
}

export async function listProvisionLog(params?: {
  source?: string; username?: string; action?: string; limit?: number;
}): Promise<ProvisionLogEntry[]> {
  const q = new URLSearchParams();
  if (params?.source) q.set("source", params.source);
  if (params?.username) q.set("username", params.username);
  if (params?.action) q.set("action", params.action);
  if (params?.limit) q.set("limit", String(params.limit));
  const suffix = q.toString() ? `?${q}` : "";
  const d = await caseRequest<{ entries: ProvisionLogEntry[] }>(
    `/identity/provisioning/log${suffix}`);
  return d.entries ?? [];
}

/** An account and the IdP identity bound to it, if any. */
export interface AccountLink {
  user_id: string; username: string; email: string;
  display_name: string | null; external_id: string | null;
  /** OIDC keys on this pair and never reads `external_id`. */
  sso_provider: string | null; sso_subject: string | null;
  is_active: boolean; has_password: boolean;
}

/** Accounts bound to an IdP identity — or, with `linked_only: false`, any
 *  account matching the search, which is how an admin finds the one to link. */
export async function listAccountLinks(params?: {
  q?: string; linked_only?: boolean; limit?: number;
}): Promise<AccountLink[]> {
  const query = new URLSearchParams();
  if (params?.q) query.set("q", params.q);
  if (params?.linked_only === false) query.set("linked_only", "false");
  if (params?.limit) query.set("limit", String(params.limit));
  const suffix = query.toString() ? `?${query}` : "";
  const d = await caseRequest<{ links: AccountLink[] }>(
    `/identity/provisioning/links${suffix}`);
  return d.links ?? [];
}

/** Bind an existing account to the identity its IdP asserts.
 *
 *  A SAML login refuses to adopt an account it matches only by email address —
 *  an assertion naming an address is not proof of owning the account that holds
 *  it. This is how the link gets made deliberately instead. */
export async function linkAccount(identifier: string, binding: {
  external_id?: string; sso_provider?: string; sso_subject?: string;
}): Promise<AccountLink> {
  return caseRequest("/identity/provisioning/links", {
    method: "POST", body: JSON.stringify({ identifier, ...binding }),
  });
}

export async function unlinkAccount(identifier: string): Promise<AccountLink> {
  return caseRequest("/identity/provisioning/links/unlink", {
    method: "POST", body: JSON.stringify({ identifier }),
  });
}

export async function listUnmappedGroups():
  Promise<{ source: string; external_group: string; seen: number }[]> {
  const d = await caseRequest<{ unmapped: { source: string; external_group: string; seen: number }[] }>(
    "/identity/provisioning/unmapped-groups");
  return d.unmapped ?? [];
}

/** Dry-run a pasted assertion. Issues nothing, provisions nobody, and does not
 *  consume the assertion id — an admin can paste the same capture repeatedly
 *  while fixing an attribute map. */
export async function testSamlAssertion(tenant: string, samlResponse: string): Promise<any> {
  return caseRequest(`/auth/saml/${encodeURIComponent(tenant)}/test`,
    { method: "POST", body: JSON.stringify({ saml_response: samlResponse }) });
}

/** Access groups, as the mapping editor's target list. */
export async function listAccessGroupsBrief():
  Promise<{ id: string; name: string; tenant_id: string }[]> {
  return caseRequest("/access-groups");
}

export async function createCalendar(body: {
  name: string; timezone: string; work_days: number[];
  work_start_hour: number; work_end_hour: number;
  description?: string; holidays?: string[];
}): Promise<any> {
  return caseRequest("/admin/calendars", { method: "POST", body: JSON.stringify(body) });
}

export async function updateCalendar(id: string, body: Record<string, any>): Promise<any> {
  return caseRequest(`/admin/calendars/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function listCalendars(): Promise<any[]> {
  return caseRequest("/admin/calendars");
}

// ── Calendar — people time ─────────────────────────────────────────────────
//
// Distinct from the business calendars above: those describe the ORGANISATION's
// hours (what the SLA clock counts), these describe a PERSON's.

export interface Absence {
  id: string;
  user_id: string;
  starts_at: string;
  ends_at: string;
  all_day: boolean;
  status: "requested" | "approved" | "cancelled";
  delegate_user_id: string | null;
  source: string;
  approved_by: string | null;
  approved_at: string | null;
  created_by: string;
  created_at: string;
  // Personal — present only where the caller is the person, their manager or an
  // admin. A colleague sees busy, never why.
  kind?: string;
  note?: string;
}

export async function listAbsences(params: {
  user_id?: string; status?: string; start?: string; end?: string;
} = {}): Promise<Absence[]> {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v) q.set(k, v); });
  const qs = q.toString();
  return caseRequest(`/calendar/absences${qs ? `?${qs}` : ""}`);
}

/* ── Calendar: import, subscriptions, export ─────────────────────────────
 *
 * `source='import'` sat in the schema with no producer until 2026-08-27. An
 * upload is a snapshot; a subscription is the same reader on a URL, re-read on
 * a timetable — "add a calendar by URL", the way Google and Outlook offer it.
 * Both are read-only against the far end.
 */

export interface CalendarSubscription {
  id: string;
  user_id: string;
  name: string;
  enabled: boolean;
  interval_minutes: number;
  last_status: "never" | "ok" | "unchanged" | "error";
  last_fetched_at: string | null;
  last_success_at: string | null;
  last_event_count: number;
  consecutive_errors: number;
  /** Only returned to somebody who may see the detail — a Google or Outlook
   *  private address embeds a token, so the URL is a credential in all but name. */
  url?: string;
  last_error?: string;
}

export async function importCalendarIcs(body: {
  ics: string; user_id?: string; dry_run?: boolean;
}): Promise<any> {
  return caseRequest("/calendar/import", {
    method: "POST", body: JSON.stringify(body),
  });
}

export async function listCalendarSubscriptions(user_id?: string): Promise<{
  subscriptions: CalendarSubscription[]; allowed_hosts: string[];
}> {
  const qs = user_id ? `?user_id=${encodeURIComponent(user_id)}` : "";
  return caseRequest(`/calendar/subscriptions${qs}`);
}

export async function createCalendarSubscription(body: {
  name: string; url: string; user_id?: string; interval_minutes?: number;
}): Promise<CalendarSubscription> {
  return caseRequest("/calendar/subscriptions", {
    method: "POST", body: JSON.stringify(body),
  });
}

export async function updateCalendarSubscription(
  id: string,
  body: { name?: string; url?: string; interval_minutes?: number; enabled?: boolean },
): Promise<CalendarSubscription> {
  return caseRequest(`/calendar/subscriptions/${id}`, {
    method: "PATCH", body: JSON.stringify(body),
  });
}

export async function deleteCalendarSubscription(
  id: string, keepEvents = true,
): Promise<void> {
  return caseRequest(`/calendar/subscriptions/${id}?keep_events=${keepEvents}`, {
    method: "DELETE",
  });
}

export async function refreshCalendarSubscription(id: string): Promise<any> {
  return caseRequest(`/calendar/subscriptions/${id}/refresh`, { method: "POST" });
}

/** The `.ics` text. Not JSON, so it cannot go through `caseRequest` — and the
 *  download is built in the browser rather than followed as a link, because a
 *  plain `<a href>` carries no Authorization header and would 401. */
export async function exportCalendarIcs(params: {
  start: string; end: string; user_id?: string; include_absences?: boolean;
}): Promise<string> {
  const q = new URLSearchParams({ start: params.start, end: params.end });
  if (params.user_id) q.set("user_id", params.user_id);
  if (params.include_absences === false) q.set("include_absences", "false");
  const res = await fetch(caseUrl(`/calendar/export?${q.toString()}`), {
    headers: { ...authHeaders() },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail || "Export failed");
  }
  return res.text();
}

export async function createAbsence(body: {
  user_id?: string; kind?: string; starts_at: string; ends_at: string;
  all_day?: boolean; delegate_user_id?: string | null; note?: string;
}): Promise<Absence> {
  return caseRequest("/calendar/absences", { method: "POST", body: JSON.stringify(body) });
}

export async function approveAbsence(id: string): Promise<Absence> {
  return caseRequest(`/calendar/absences/${id}/approve`, { method: "POST" });
}

export async function cancelAbsence(id: string): Promise<Absence> {
  return caseRequest(`/calendar/absences/${id}/cancel`, { method: "POST" });
}

export async function getAvailability(userIds: string[], at?: string): Promise<{
  at: string;
  users: { user_id: string; available: boolean; delegate_user_id?: string | null }[];
}> {
  const q = new URLSearchParams();
  userIds.forEach(u => q.append("user_ids", u));
  if (at) q.set("at", at);
  return caseRequest(`/calendar/availability?${q.toString()}`);
}

export async function getWorkPattern(userId: string): Promise<{
  user_id: string; work_days: number[]; work_start_hour: number;
  work_end_hour: number; timezone: string;
}> {
  return caseRequest(`/calendar/patterns/${encodeURIComponent(userId)}`);
}

export async function saveWorkPattern(body: {
  user_id: string; business_calendar_id?: string | null; work_days?: number[] | null;
  work_start_hour?: number | null; work_end_hour?: number | null; timezone?: string | null;
}): Promise<any> {
  return caseRequest("/calendar/patterns", { method: "PUT", body: JSON.stringify(body) });
}

export interface Recurrence { freq: "daily" | "weekly" | "monthly"; interval?: number; until?: string; count?: number }

export interface CalendarEvent {
  id: string; title: string; description: string;
  starts_at: string; ends_at: string; timezone: string;
  organiser_user_id: string; attendee_user_ids: string[];
  external_emails: string[];
  case_id: string | null; meet_session_id: string | null;
  meeting_url: string | null;
  recurrence: Recurrence | null; excluded_dates: string[];
  /** `active`, `ended`, `cancelled`, or null when the meeting has no room. */
  room_status?: string | null;
  visibility: "team" | "private"; status: string;
  created_by: string; created_at: string; updated_by?: string | null;
  // Present on listings, which expand a series into its occurrences.
  occurrence_date?: string; is_recurring?: boolean;
}

export async function listEvents(params: { start?: string; end?: string } = {}): Promise<CalendarEvent[]> {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v) q.set(k, v); });
  const qs = q.toString();
  return caseRequest(`/calendar/events${qs ? `?${qs}` : ""}`);
}

export async function createEvent(body: {
  title: string; description?: string; starts_at: string; ends_at: string;
  timezone?: string; attendee_user_ids?: string[]; external_emails?: string[];
  case_id?: string | null; meet_session_id?: string | null;
  meeting_url?: string | null; recurrence?: Recurrence | null;
  visibility?: "team" | "private";
  /** A start time in the past is refused unless this says it is deliberate. */
  allow_past?: boolean;
  /** Ask HxMeet for a room as part of scheduling, so the invitation has a link. */
  create_room?: boolean;
  /** Email the guests now. On by default at creation; an edit never sends. */
  send_invites?: boolean;
}): Promise<CalendarEvent & {
  join_url?: string | null;
  room_error?: string;
  invitations?: { sent: string[]; failed: { email: string; error: string }[]; from?: string };
}> {
  return caseRequest("/calendar/events", { method: "POST", body: JSON.stringify(body) });
}

export async function updateEvent(id: string, body: Record<string, unknown>): Promise<CalendarEvent> {
  return caseRequest(`/calendar/events/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

/** Cancel one occurrence of a series — "we skip next Monday". */
export async function skipOccurrence(id: string, on: string): Promise<CalendarEvent> {
  return caseRequest(`/calendar/events/${id}/skip?on=${encodeURIComponent(on)}`, { method: "POST" });
}

/** Ask HxMeet for a room. HxMeet owns rooms; this only remembers the answer. */
/** Ask HxMeet for a room. A live one is reused; one LiveKit already closed is
 *  replaced, so rejoining a stand-up puts you back in a room. */
export async function createMeetingRoom(id: string): Promise<CalendarEvent & { join_url: string | null; driver: string; reused: boolean }> {
  return caseRequest(`/calendar/events/${id}/meeting`, { method: "POST" });
}

export async function inviteGuests(id: string): Promise<{ sent: string[]; failed: { email: string; error: string }[] }> {
  return caseRequest(`/calendar/events/${id}/invite`, { method: "POST" });
}

export async function updateAbsence(id: string, body: Record<string, unknown>): Promise<Absence> {
  return caseRequest(`/calendar/absences/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function cancelEvent(id: string): Promise<CalendarEvent> {
  return caseRequest(`/calendar/events/${id}/cancel`, { method: "POST" });
}

export async function proposeSlots(body: {
  user_ids: string[]; start: string; end: string; minutes?: number; limit?: number;
}): Promise<{ slots: { starts_at: string; ends_at: string }[]; searched: Record<string, unknown> }> {
  return caseRequest("/calendar/slots", { method: "POST", body: JSON.stringify(body) });
}

export async function listRules(): Promise<any> {
  return caseRequest("/rules");
}

/* ── DMN interchange (market gap B5) ───────────────────────────────────────
   The engine has always run decision tables; the standard FILE is what was
   missing, and it is the half procurement asks about. */

export type DmnImportedTable = {
  name: string; rule_type: string; hit_policy: string;
  input_count: number; output_count: number; row_count: number;
  notes: string[]; rule_id?: string | null;
};

/** `dry_run` defaults to true on the server: an import previews first and
 *  creates nothing, so the second click confirms something that was read. */
export async function importDmn(body: {
  xml: string; dry_run?: boolean; scope?: string;
  scope_target_id?: string | null; version?: string; enabled?: boolean;
}): Promise<{ dry_run: boolean; tables: DmnImportedTable[]; created: number }> {
  return caseRequest("/rules/dmn/import", { method: "POST", body: JSON.stringify(body) });
}

/** The rule as a `.dmn` file. A blob rather than a link, for the same reason
 *  every other download here is one: a plain `<a href>` carries no
 *  Authorization header. */
export async function exportRuleAsDmn(ruleId: string): Promise<Blob> {
  const resp = await fetch(`${CASE_BASE}/rules/${ruleId}/dmn`, { headers: authHeaders() });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail?.message || body.detail || "This rule cannot be exported as DMN");
  }
  return resp.blob();
}

export async function createRule(body: {
  name: string; version: string; rule_type: string;
  scope?: string; scope_target_id?: string | null;
  definition_json: Record<string, any>;
  enabled?: boolean; priority?: number;
}): Promise<any> {
  return caseRequest("/rules", { method: "POST", body: JSON.stringify(body) });
}

export async function updateRule(id: string, body: {
  definition_json?: Record<string, any>;
  enabled?: boolean;
  priority?: number;
}): Promise<any> {
  return caseRequest(`/rules/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function deleteRule(id: string): Promise<void> {
  await caseRequest(`/rules/${id}`, { method: "DELETE" });
}


/* ── Process Mining (Phase 14) ─────────────────────────────────── */

export async function getPMSummary(caseTypeId?: string, days = 30, tenantId?: string): Promise<any> {
  const qs = new URLSearchParams();
  if (caseTypeId) qs.set("case_type_id", caseTypeId);
  if (tenantId) qs.set("tenant_id", tenantId);
  qs.set("days", String(days));
  return caseRequest(`/process-mining/summary?${qs}`);
}

export async function getPMActivityStats(caseTypeId?: string, days = 30, tenantId?: string): Promise<any[]> {
  const qs = new URLSearchParams();
  if (caseTypeId) qs.set("case_type_id", caseTypeId);
  if (tenantId) qs.set("tenant_id", tenantId);
  qs.set("days", String(days));
  return caseRequest(`/process-mining/activity-stats?${qs}`);
}

export async function getPMBottlenecks(caseTypeId?: string, days = 30, tenantId?: string): Promise<any[]> {
  const qs = new URLSearchParams();
  if (caseTypeId) qs.set("case_type_id", caseTypeId);
  if (tenantId) qs.set("tenant_id", tenantId);
  qs.set("days", String(days));
  return caseRequest(`/process-mining/bottlenecks?${qs}`);
}

export async function getPMVariants(caseTypeId?: string, days = 30, tenantId?: string): Promise<any[]> {
  const qs = new URLSearchParams();
  if (caseTypeId) qs.set("case_type_id", caseTypeId);
  if (tenantId) qs.set("tenant_id", tenantId);
  qs.set("days", String(days));
  return caseRequest(`/process-mining/variants?${qs}`);
}

export async function getPMFlowGraph(caseTypeId?: string, days = 30, tenantId?: string): Promise<any> {
  const qs = new URLSearchParams();
  if (caseTypeId) qs.set("case_type_id", caseTypeId);
  if (tenantId) qs.set("tenant_id", tenantId);
  qs.set("days", String(days));
  return caseRequest(`/process-mining/flow-graph?${qs}`);
}

export async function getPMConformance(caseTypeId: string, days = 30): Promise<any> {
  return caseRequest(`/process-mining/conformance/${caseTypeId}?days=${days}`);
}

export async function getPMEvents(caseId?: string, limit = 100): Promise<any[]> {
  const qs = new URLSearchParams();
  if (caseId) qs.set("case_id", caseId);
  qs.set("limit", String(limit));
  return caseRequest(`/process-mining/events?${qs}`);
}

/* ── NLP (Phase 15) ────────────────────────────────────────────── */

export async function getNLPStatus(): Promise<any> {
  return caseRequest("/nlp/status");
}

export async function generateCaseTypeFromText(
  description: string, deploy = false, nameOverride?: string
): Promise<any> {
  return caseRequest("/nlp/generate-case-type", {
    method: "POST",
    body: JSON.stringify({ description, deploy, ...(nameOverride ? { name_override: nameOverride } : {}) }),
  });
}

export async function generateFullCaseType(
  description: string, deploy = false, nameOverride?: string
): Promise<any> {
  return caseRequest("/nlp/generate-full", {
    method: "POST",
    body: JSON.stringify({ description, deploy, ...(nameOverride ? { name_override: nameOverride } : {}) }),
  });
}

/* ── Payments P48 ─────────────────────────────────────────────── */

export async function initiatePaymentRequest(
  caseId: string, stepId: string, amountCents: number,
  currency: string, description: string, customerEmail?: string
): Promise<any> {
  return caseRequest(`/payments/cases/${caseId}/charge`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId, amount_cents: amountCents, currency, description, customer_email: customerEmail }),
  });
}

export async function listCasePaymentRequests(caseId: string): Promise<any[]> {
  return caseRequest(`/payments/cases/${caseId}/requests`);
}

export async function confirmDisbursement(
  caseId: string, stepId: string, amountCents: number,
  currency: string, description: string, bankReference?: string, notes?: string
): Promise<any> {
  return caseRequest(`/payments/cases/${caseId}/disburse`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId, amount_cents: amountCents, currency, description, bank_reference: bankReference, notes }),
  });
}

export async function listCaseDisbursements(caseId: string): Promise<any[]> {
  return caseRequest(`/payments/cases/${caseId}/disbursements`);
}

export async function markDisbursementSent(disbursementId: string): Promise<any> {
  return caseRequest(`/payments/disbursements/${disbursementId}/mark-sent`, { method: "POST" });
}

/* ── KYC / E-sign P49 ─────────────────────────────────────────── */

export async function initiateIdentityVerification(
  caseId: string, stepId: string, firstName: string, lastName: string
): Promise<any> {
  return caseRequest(`/identity/cases/${caseId}/verify`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId, first_name: firstName, last_name: lastName }),
  });
}

export async function listCaseVerifications(caseId: string): Promise<any[]> {
  return caseRequest(`/identity/cases/${caseId}/verifications`);
}

export async function sendESignRequest(
  caseId: string, stepId: string, signerEmail: string, signerName: string, documentName: string
): Promise<any> {
  return caseRequest(`/esign/cases/${caseId}/send`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId, signer_email: signerEmail, signer_name: signerName, document_name: documentName }),
  });
}

export async function listCaseESignRequests(caseId: string): Promise<any[]> {
  return caseRequest(`/esign/cases/${caseId}/requests`);
}

/* ── CRM / Invoices P50 ───────────────────────────────────────── */

export async function syncToCrm(
  caseId: string, stepId: string, firstName: string, lastName: string,
  email: string, subject: string, description: string
): Promise<any> {
  return caseRequest(`/crm/cases/${caseId}/sync`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId, first_name: firstName, last_name: lastName, email, subject, description }),
  });
}

export async function listCaseCrmRecords(caseId: string): Promise<any[]> {
  return caseRequest(`/crm/cases/${caseId}/records`);
}

export async function generateInvoice(
  caseId: string, stepId: string, contactName: string, description: string,
  amountCents: number, currency: string, lineItems: any[], reference: string
): Promise<any> {
  return caseRequest(`/invoices/cases/${caseId}/generate`, {
    method: "POST",
    body: JSON.stringify({ step_id: stepId, contact_name: contactName, description, amount_cents: amountCents, currency, line_items: lineItems, reference }),
  });
}

export async function listCaseInvoices(caseId: string): Promise<any[]> {
  return caseRequest(`/invoices/cases/${caseId}/records`);
}

/* ── P51 Communications ─────────────────────────────────────────── */

export async function sendSms(caseId: string, data: { step_id: string; to_number: string; body: string; from_number?: string; connector_id?: string }): Promise<any> {
  return caseRequest(`/comms/sms/cases/${caseId}/send`, { method: "POST", body: JSON.stringify(data) });
}

export async function listCaseSms(caseId: string): Promise<any[]> {
  return caseRequest(`/comms/sms/cases/${caseId}/messages`);
}

export async function sendSlack(caseId: string, data: { step_id: string; message: string; channel?: string; blocks?: any[]; connector_id?: string }): Promise<any> {
  return caseRequest(`/comms/slack/cases/${caseId}/send`, { method: "POST", body: JSON.stringify(data) });
}

export async function listCaseSlack(caseId: string): Promise<any[]> {
  return caseRequest(`/comms/slack/cases/${caseId}/notifications`);
}

export async function listCommsConnectors(): Promise<any[]> {
  return caseRequest(`/comms/connectors`);
}

/* ── P52 Document Intelligence & Storage ───────────────────────── */

export async function extractDocument(caseId: string, data: { step_id: string; source_url?: string; document_name?: string; connector_id?: string }): Promise<any> {
  return caseRequest(`/docintel/cases/${caseId}/extract`, { method: "POST", body: JSON.stringify(data) });
}

export async function listCaseExtractions(caseId: string): Promise<any[]> {
  return caseRequest(`/docintel/cases/${caseId}/extractions`);
}

export async function routeToStorage(caseId: string, data: { step_id: string; document_name: string; content_type?: string; size_bytes?: number; connector_id?: string }): Promise<any> {
  return caseRequest(`/docintel/cases/${caseId}/store`, { method: "POST", body: JSON.stringify(data) });
}

export async function listCaseStorageRoutes(caseId: string): Promise<any[]> {
  return caseRequest(`/docintel/cases/${caseId}/storage`);
}

export async function listDocIntelConnectors(): Promise<any[]> {
  return caseRequest(`/docintel/connectors`);
}

/* ── Scout (Phase 16) ──────────────────────────────────────────── */

export async function listScoutPlatforms(): Promise<any> {
  return caseRequest("/scout/platforms");
}

export async function createScoutScan(
  name: string, content: string, source_platform = "", filename = ""
): Promise<any> {
  return caseRequest("/scout/scan", {
    method: "POST",
    body: JSON.stringify({ name, content, source_platform, filename }),
  });
}

export async function listScoutScans(): Promise<any[]> {
  return caseRequest("/scout/scans");
}

export async function getScoutScan(id: string): Promise<any> {
  return caseRequest(`/scout/scans/${id}`);
}

export async function getScoutPlan(id: string): Promise<any> {
  return caseRequest(`/scout/scans/${id}/plan`);
}

export async function deleteScoutScan(id: string): Promise<void> {
  await caseRequest(`/scout/scans/${id}`, { method: "DELETE" });
}

/* ── Tenants (Phase 17) ────────────────────────────────────────── */

export interface Tenant {
  id: string;
  slug: string;
  name: string;
  description: string;
  status: string;
  settings: Record<string, any>;
  max_cases: number | null;
  max_users: number | null;
  created_at: string;
  updated_at: string;
}

export async function listTenants(): Promise<Tenant[]> {
  return caseRequest("/tenants");
}

export async function createTenant(body: {
  slug: string; name: string; description?: string;
  max_cases?: number | null; max_users?: number | null;
}): Promise<Tenant> {
  return caseRequest("/tenants", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateTenant(id: string, body: Partial<Tenant>): Promise<Tenant> {
  return caseRequest(`/tenants/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function deleteTenant(id: string): Promise<void> {
  await caseRequest(`/tenants/${id}`, { method: "DELETE" });
}

export async function permanentDeleteTenant(id: string): Promise<void> {
  await caseRequest(`/tenants/${id}/permanent`, { method: "DELETE" });
}

export async function listTenantMembers(id: string): Promise<any[]> {
  return caseRequest(`/tenants/${id}/members`);
}

export async function addTenantMember(
  id: string, user_id: string, role = "member"
): Promise<any> {
  return caseRequest(`/tenants/${id}/members`, {
    method: "POST",
    body: JSON.stringify({ user_id, role }),
  });
}

export async function updateTenantMember(
  id: string, user_id: string, role: string
): Promise<any> {
  return caseRequest(`/tenants/${id}/members/${user_id}`, {
    method: "PATCH",
    body: JSON.stringify({ user_id, role }),
  });
}

export async function removeTenantMember(id: string, user_id: string): Promise<void> {
  await caseRequest(`/tenants/${id}/members/${user_id}`, { method: "DELETE" });
}

/* ── Codegen (Phase 18) ────────────────────────────────────────── */

export async function listCodegenPlatforms(): Promise<any> {
  return caseRequest("/codegen/platforms");
}

export async function previewApp(config: any): Promise<any> {
  return caseRequest("/codegen/preview", {
    method: "POST",
    body: JSON.stringify(config),
  });
}

export async function downloadGeneratedApp(config: any): Promise<Blob> {
  const resp = await fetch(`${CASE_BASE}/codegen/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(config),
  });
  if (!resp.ok) throw new Error("Generation failed");
  return resp.blob();
}

/* ── Scout AI (Phase 19) ───────────────────────────────────────── */

export async function getScoutAIStatus(): Promise<any> {
  return caseRequest("/scout-ai/status");
}

export async function analyzeArtifact(body: {
  code: string; artifact_type?: string; source_platform?: string;
  identifier?: string; save?: boolean; scan_id?: string | null;
}): Promise<any> {
  return caseRequest("/scout-ai/analyze", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function generateHelixCode(body: {
  code: string; artifact_type?: string; source_platform?: string;
}): Promise<any> {
  return caseRequest("/scout-ai/generate-code", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function listArtifactAnalyses(scanId?: string): Promise<any[]> {
  const q = scanId ? `?scan_id=${scanId}` : "";
  return caseRequest(`/scout-ai/analyses${q}`);
}

/* ── Enterprise (Phase 20) ─────────────────────────────────────── */

export async function listSecurityEvents(params: any = {}): Promise<any[]> {
  const qs = new URLSearchParams(params).toString();
  return caseRequest(`/enterprise/security-events${qs ? "?" + qs : ""}`);
}

export async function getSecurityEventStats(days = 30): Promise<any> {
  return caseRequest(`/enterprise/security-events/stats?days=${days}`);
}

export async function createGDPRRequest(body: any): Promise<any> {
  return caseRequest("/enterprise/gdpr/requests", {
    method: "POST", body: JSON.stringify(body),
  });
}

export async function listGDPRRequests(): Promise<any[]> {
  return caseRequest("/enterprise/gdpr/requests");
}

export async function lookupUserData(userId: string): Promise<{ user_id: string; exists: boolean; counts: Record<string, number> }> {
  return caseRequest(`/enterprise/gdpr/lookup/${encodeURIComponent(userId)}`);
}

export async function downloadUserExport(userId: string): Promise<Blob> {
  const resp = await fetch(`${CASE_BASE}/enterprise/gdpr/export/${userId}`, {
    headers: authHeaders(),
  });
  if (!resp.ok) throw new Error("Export failed");
  return resp.blob();
}

export async function anonymizeUser(userId: string): Promise<any> {
  return caseRequest(`/enterprise/gdpr/anonymize/${userId}`, { method: "POST" });
}

export async function listRetentionPolicies(): Promise<any[]> {
  return caseRequest("/enterprise/retention-policies");
}

export async function updateRetentionPolicy(id: string, body: any): Promise<any> {
  return caseRequest(`/enterprise/retention-policies/${id}`, {
    method: "PATCH", body: JSON.stringify(body),
  });
}

export async function getEnterpriseSystemInfo(): Promise<any> {
  return caseRequest("/enterprise/system-info");
}

/* ── Audit streaming to a SIEM (migration 200) ─────────────────────────────
   The stream is PLATFORM-scoped: `security_events` has no tenant column, so a
   target receives every tenant's events. The API says so on create by refusing
   a request that has not acknowledged it, and the screen says so too. */

export type AuditStreamTarget = {
  id: string; name: string; kind: string; endpoint_url: string;
  has_token: boolean; verify_tls: boolean; enabled: boolean; scope: string;
  min_severity: string | null; event_types: string[];
  cursor_timestamp: string | null; last_success_at: string | null;
  last_error: string | null; last_error_at: string | null;
  forwarded_count: number; created_at: string | null;
};

/** The API answers `{items, kinds}` — `items`, not `targets`. Typed the way the
 *  server actually replies, so a screen reading the wrong key fails to compile
 *  rather than rendering an empty list against a working endpoint. */
export async function listAuditStreamTargets(): Promise<{ items: AuditStreamTarget[]; kinds: string[] }> {
  return caseRequest("/audit-stream/targets");
}

export async function createAuditStreamTarget(body: {
  name: string; endpoint_url: string; token?: string | null; kind?: string;
  verify_tls?: boolean; min_severity?: string | null; event_types?: string[] | null;
  acknowledge_platform_scope: boolean;
}): Promise<AuditStreamTarget> {
  return caseRequest("/audit-stream/targets", { method: "POST", body: JSON.stringify(body) });
}

export async function updateAuditStreamTarget(
  id: string,
  body: Partial<{ name: string; endpoint_url: string; token: string | null;
                  verify_tls: boolean; enabled: boolean;
                  min_severity: string | null; event_types: string[] | null }>,
): Promise<AuditStreamTarget> {
  return caseRequest(`/audit-stream/targets/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function deleteAuditStreamTarget(id: string): Promise<void> {
  return caseRequest(`/audit-stream/targets/${id}`, { method: "DELETE" });
}

/** One synthetic event. Proves the URL, the token and the certificate without
 *  moving the cursor or forwarding anything real. */
export async function testAuditStreamTarget(id: string): Promise<{ ok: boolean; error?: string; message?: string }> {
  return caseRequest(`/audit-stream/targets/${id}/test`, { method: "POST" });
}

export async function runAuditStreamTarget(id: string): Promise<any> {
  return caseRequest(`/audit-stream/targets/${id}/run`, { method: "POST" });
}

export async function resolveGDPRRequest(requestId: string, status: "completed" | "rejected" = "completed"): Promise<any> {
  return caseRequest(`/enterprise/gdpr/requests/${requestId}`, {
    method: "PATCH", body: JSON.stringify({ status }),
  });
}

// (getSystemInfo defined elsewhere — Phase 20 uses /enterprise/system-info via a different name)

/* ── Site Map (Phase 20) ───────────────────────────────────────── */

export async function listModules(): Promise<any> {
  return caseRequest("/sitemap/modules");
}

export async function listPhases(): Promise<any> {
  return caseRequest("/sitemap/phases");
}

export async function searchModules(q: string): Promise<any> {
  return caseRequest(`/sitemap/search?q=${encodeURIComponent(q)}`);
}


/* ── Orchestrator (Phase 21) ───────────────────────────────────── */

export async function createMigrationProject(name: string, scan_id: string): Promise<any> {
  return caseRequest("/orchestrator/projects", {
    method: "POST", body: JSON.stringify({ name, scan_id }),
  });
}

export async function listMigrationProjects(): Promise<any[]> {
  return caseRequest("/orchestrator/projects");
}

export async function getMigrationProject(id: string): Promise<any> {
  return caseRequest(`/orchestrator/projects/${id}`);
}

export async function deleteMigrationProject(id: string): Promise<void> {
  await caseRequest(`/orchestrator/projects/${id}`, { method: "DELETE" });
}

export async function getMigrationRoadmap(id: string): Promise<any> {
  return caseRequest(`/orchestrator/projects/${id}/roadmap`);
}

export async function listMigrationTasks(projectId: string): Promise<any[]> {
  return caseRequest(`/orchestrator/projects/${projectId}/tasks`);
}

export async function analyzeTask(taskId: string): Promise<any> {
  return caseRequest(`/orchestrator/tasks/${taskId}/analyze`, { method: "POST" });
}

export async function generateTaskCode(taskId: string): Promise<any> {
  return caseRequest(`/orchestrator/tasks/${taskId}/generate`, { method: "POST" });
}

export async function markTaskPorted(taskId: string): Promise<any> {
  return caseRequest(`/orchestrator/tasks/${taskId}/mark-ported`, { method: "POST" });
}

export async function runFullPipeline(projectId: string, maxTasks = 50): Promise<any> {
  return caseRequest(`/orchestrator/projects/${projectId}/run-all?max_tasks=${maxTasks}`, {
    method: "POST",
  });
}

export async function downloadMigrationZip(projectId: string): Promise<Blob> {
  const resp = await fetch(`${CASE_BASE}/orchestrator/projects/${projectId}/export`, {
    headers: authHeaders(),
  });
  if (!resp.ok) throw new Error("Export failed");
  return resp.blob();
}

// ── HxBranch v2 ──────────────────────────────────────────────────

export async function listBranches(params: {
  status?: string;
  owner_id?: string;
  assigned_reviewer_id?: string;
  artifact_type?: string;
} = {}): Promise<{ branches: any[]; total: number }> {
  const qs = new URLSearchParams();
  if (params.status)               qs.set("status", params.status);
  if (params.owner_id)             qs.set("owner_id", params.owner_id);
  if (params.assigned_reviewer_id) qs.set("assigned_reviewer_id", params.assigned_reviewer_id);
  if (params.artifact_type)        qs.set("artifact_type", params.artifact_type);
  const resp = await fetch(`${CASE_BASE}/branches${qs.toString() ? "?" + qs : ""}`, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function getBranchDiff(branchId: string): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches/${branchId}/diff`, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function postBranchReview(branchId: string, decision: "approved" | "rejected" | "changes_requested", comments?: string): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches/${branchId}/reviews`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ decision, comments }),
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function createBranch(params: {
  name: string;
  artifact_type: string;
  artifact_id: string;
  description?: string;
  assigned_reviewer_id?: string;
}): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ branch_type: "artifact", ...params }),
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function getBranch(branchId: string): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches/${branchId}`, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function patchBranchContent(branchId: string, content: Record<string, unknown>): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches/${branchId}/content`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ content_snapshot: content }),
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function submitBranchForReview(branchId: string, assignedReviewerId: string): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches/${branchId}/submit`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ assigned_reviewer_id: assignedReviewerId }),
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export async function recallBranch(branchId: string): Promise<any> {
  const resp = await fetch(`${CASE_BASE}/branches/${branchId}/recall`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
  });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}

export interface UserDirectoryEntry {
  user_id: string;
  email: string | null;
  display_name: string | null;
  tenant_id: string | null;
  access_group_ids: string[];
}

export async function searchUsers(q: string, accessGroupId?: string, limit = 20): Promise<UserDirectoryEntry[]> {
  const params = new URLSearchParams({ active_only: "true", limit: String(limit) });
  if (q) params.set("q", q);
  if (accessGroupId) params.set("access_group_id", accessGroupId);
  return caseRequest(`/user-directory?${params}`);
}

export async function getMyDirectoryEntry(userId: string): Promise<UserDirectoryEntry & { access_group_ids: string[] }> {
  return caseRequest(`/user-directory/${encodeURIComponent(userId)}`);
}

/**
 * Authenticated fetch against the case-service base (/api/v1).
 * Handles 401 refresh and re-auth the same way as all other case endpoints.
 * Use this instead of raw fetch() so auth headers are always included.
 */
export async function apiFetch(path: string, options?: RequestInit): Promise<Response> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...authHeaders(),
    ...(options?.headers as Record<string, string> | undefined),
  };
  const res = await fetch(caseUrl(path), { ...options, headers });
  if (res.status === 401) {
    const newToken = await attemptRefresh();
    if (newToken) {
      const retry = await fetch(caseUrl(path), {
        ...options,
        headers: { ...headers, ...authHeaders(newToken) },
      });
      return retry;
    }
    window.dispatchEvent(new Event("velaris:unauthorized"));
  }
  return res;
}

/* ── Omnichannel (SMS / WhatsApp / voice) ────────────────────────── */

export interface ChannelConversation {
  id: string; endpoint_id: string; endpoint_address: string | null;
  tenant_id: string; peer_address: string; case_id: string | null;
  status: string; assigned_to: string | null; unread_count: number;
  last_message_at: string | null; window_expires_at: string | null;
}

export interface ChannelMessage {
  id: string; direction: string; channel: string; author: string | null;
  peer_address: string; body: string; media: unknown[];
  template_name: string | null; status: string; error: string | null;
  is_read: boolean; created_at: string | null;
}

export interface WhatsAppTemplate {
  id: string; endpoint_id: string; name: string; language: string;
  provider_template_id: string | null; body_params: string[];
  approval_status: string;
}

export async function listChannelConversations(params?: {
  status?: string; case_id?: string; unread_only?: boolean;
}): Promise<ChannelConversation[]> {
  const q = new URLSearchParams();
  if (params?.status) q.set("status", params.status);
  if (params?.case_id) q.set("case_id", params.case_id);
  if (params?.unread_only) q.set("unread_only", "true");
  const suffix = q.toString() ? `?${q}` : "";
  const d = await caseRequest<{ conversations: ChannelConversation[] }>(
    `/channels/conversations${suffix}`);
  return d.conversations ?? [];
}

export async function getChannelConversation(id: string):
  Promise<{ conversation: ChannelConversation; messages: ChannelMessage[] }> {
  return caseRequest(`/channels/conversations/${id}`);
}

/** Refused with a specific code when the number opted out (403) or the
 *  WhatsApp 24-hour window has closed without a template (409). */
export async function replyToConversation(id: string, body: {
  body: string; template_name?: string; template_params?: string[];
}): Promise<ChannelMessage> {
  return caseRequest(`/channels/conversations/${id}/reply`,
    { method: "POST", body: JSON.stringify(body) });
}

export async function updateChannelConversation(id: string, body: {
  case_id?: string; assigned_to?: string; status?: string;
}): Promise<ChannelConversation> {
  return caseRequest(`/channels/conversations/${id}`,
    { method: "PATCH", body: JSON.stringify(body) });
}

export async function listChannelTemplates(endpointId?: string):
  Promise<WhatsAppTemplate[]> {
  const suffix = endpointId ? `?endpoint_id=${encodeURIComponent(endpointId)}` : "";
  const d = await caseRequest<{ templates: WhatsAppTemplate[] }>(
    `/channels/templates${suffix}`);
  return d.templates ?? [];
}

export async function listCaseConversations(caseId: string):
  Promise<ChannelConversation[]> {
  const d = await caseRequest<{ conversations: ChannelConversation[] }>(
    `/channels/cases/${caseId}/conversations`);
  return d.conversations ?? [];
}

/* ── RPA bots ────────────────────────────────────────────────────── */

export interface BotDefinition {
  id: string; tenant_id: string; name: string; description: string;
  image: string; digest: string; signature_verified: boolean;
  declared_command?: string[]; declared_env?: Record<string, unknown>;
  input_schema?: Record<string, unknown>; output_schema?: Record<string, unknown>;
  credential_refs: string[]; granted_domains: string[];
  timeout_seconds: number; max_concurrency: number; resource_profile: string;
  /** Null = no fan-out. Names the input key holding the collection to spread
   *  over; it lives on the definition so the approver reviews it. */
  fanout_input_key?: string | null; fanout_max?: number;
  status: string; created_by: string; approved_by: string | null;
  approved_at: string | null;
}

export interface BotCredential {
  id: string; tenant_id: string; ref: string; version: number;
  created_by: string; env_name: string; updated_at: string | null;
}

export interface BotBatch {
  batch_id: string; bot_id: string; size: number; finished: boolean;
  succeeded: number; counts: Record<string, number>; jobs: string[];
}

export interface BotJob {
  id: string; bot_id: string; case_id: string | null; trigger: string;
  requested_by: string | null; status: string; attempt: number;
  error: string | null; output: Record<string, unknown> | null;
  /** Set when this job is one of a fan-out batch: N ordinary jobs from one run,
   *  sharing a batch id so they can be reported together. */
  batch_id?: string | null; batch_index?: number | null; batch_size?: number | null;
  /** Not before this instant — a retry serving its backoff, or a queued
   *  scheduled run. */
  run_after?: string | null;
  queued_at: string | null; started_at: string | null; ended_at: string | null;
}

export interface BotProposal {
  job_id: string; case_id: string | null; checksum: string;
  changes: { field: string; current: unknown; proposed: unknown }[];
}

export async function listBots(): Promise<BotDefinition[]> {
  const d = await caseRequest<{ bots: BotDefinition[] }>("/bots");
  return d.bots ?? [];
}

/** Refused (409) when the approver is the bot's own author — approval IS the
 *  review, and reviewing your own container image is not one. */
export async function approveBot(id: string): Promise<BotDefinition> {
  return caseRequest(`/bots/${id}/approve`, { method: "POST" });
}

export async function listBotJobs(params?: {
  status?: string; case_id?: string; bot_id?: string;
}): Promise<BotJob[]> {
  const q = new URLSearchParams();
  if (params?.status) q.set("status", params.status);
  if (params?.case_id) q.set("case_id", params.case_id);
  if (params?.bot_id) q.set("bot_id", params.bot_id);
  const suffix = q.toString() ? `?${q}` : "";
  const d = await caseRequest<{ jobs: BotJob[] }>(`/bots/jobs${suffix}`);
  return d.jobs ?? [];
}

export async function runBot(id: string, body: {
  input?: Record<string, unknown>; case_id?: string; step_id?: string;
}): Promise<BotJob> {
  return caseRequest(`/bots/${id}/run`, { method: "POST", body: JSON.stringify(body) });
}

/** Creates a bot **draft**. It cannot run until a second person approves it. */
export async function createBot(
  body: Partial<BotDefinition>,
): Promise<BotDefinition> {
  return caseRequest("/bots", { method: "POST", body: JSON.stringify(body) });
}

/** **Any change returns the bot to draft**, clearing its approver — editing an
 *  approved container image without a second review is the thing the approval
 *  gate exists to prevent. Saving with nothing changed changes nothing. */
export async function updateBot(
  id: string, body: Partial<BotDefinition>,
): Promise<BotDefinition> {
  return caseRequest(`/bots/${id}`, { method: "PUT", body: JSON.stringify(body) });
}

export async function disableBot(id: string):
  Promise<{ disabled: boolean; cancelled_jobs: number }> {
  return caseRequest(`/bots/${id}/disable`, { method: "POST" });
}

export async function listBotCredentials(): Promise<BotCredential[]> {
  const d = await caseRequest<{ credentials: BotCredential[] }>("/bots/credentials");
  return d.credentials ?? [];
}

/** The value is write-only: it is encrypted under the tenant's key and is never
 *  returned to anyone, including whoever stored it. */
export async function upsertBotCredential(body: {
  ref: string; secret: string; tenant_id?: string;
}): Promise<{ ref: string; version: number; env_name: string }> {
  return caseRequest("/bots/credentials", {
    method: "PUT", body: JSON.stringify(body),
  });
}

export interface BotJobAudit {
  job: BotJob;
  events: { kind: string; detail: Record<string, unknown>; at: string | null }[];
  /** The reference and the lease window — never the value, which nothing can
   *  read back at all. */
  credentials: { ref: string; leased_at: string | null;
                 expires_at: string | null; revoked_at: string | null }[];
  /** Every host the container tried to reach, allowed or blocked, from the
   *  egress gateway's own log. */
  network: { destination: string; ip: string | null; status: string;
             declared: boolean; bytes_out: number | null;
             bytes_in: number | null; at: string | null }[];
}

/** The whole account of one run: what it did, which credentials it held while
 *  doing it, and every host it tried to reach. */
export async function getBotJob(id: string): Promise<BotJobAudit> {
  return caseRequest(`/bots/jobs/${id}`);
}

export async function getBotBatch(batchId: string): Promise<BotBatch> {
  return caseRequest(`/bots/batches/${batchId}`);
}

export async function getBotLimits(): Promise<{
  default: number;
  limits: { tenant_id: string; max_concurrent_jobs: number;
            updated_by: string; updated_at: string | null }[];
}> {
  return caseRequest("/bots/limits");
}

export async function setBotLimit(body: {
  tenant_id: string; max_concurrent_jobs: number;
}): Promise<{ tenant_id: string; max_concurrent_jobs: number }> {
  return caseRequest("/bots/limits", { method: "PUT", body: JSON.stringify(body) });
}

/* ── Picker sources for the scheduler editor ─────────────────────── */

/** Named things a schedule can point at. The editor asks for these by name so
 *  nobody has to paste a UUID into a JSON box to say "run this bot". */
export async function listSchedulerTargets(): Promise<{
  bots: { id: string; name: string; status: string }[];
  connectors: { id: string; name: string; enabled: boolean }[];
  emailAccounts: { id: string; name: string; address: string }[];
  syncDestinations: { id: string; name: string }[];
}> {
  const safe = async <T,>(fn: () => Promise<T>, fallback: T): Promise<T> => {
    // One unavailable list must not empty the whole editor: a deployment may
    // legitimately have no mailer, no warehouse, or no connectors at all.
    try { return await fn(); } catch { return fallback; }
  };
  const [bots, connectors, accounts, destinations] = await Promise.all([
    safe(() => listBots(), [] as BotDefinition[]),
    safe(() => caseRequest<{ connectors?: { id: string; name: string; enabled: boolean }[] }>(
      "/hxbridge/connectors"), {}),
    safe(() => caseRequest<{ accounts?: { id: string; name: string; address: string }[] }>(
      "/email/accounts"), {}),
    safe(() => caseRequest<{ destinations?: { id: string; name: string }[] }>(
      "/sync/destinations"), {}),
  ]);
  const asArray = <T,>(v: unknown, key: string): T[] =>
    Array.isArray(v) ? v as T[] : ((v as Record<string, unknown>)?.[key] as T[]) ?? [];
  return {
    bots: bots.map(b => ({ id: b.id, name: b.name, status: b.status })),
    connectors: asArray(connectors, "connectors"),
    emailAccounts: asArray(accounts, "accounts"),
    syncDestinations: asArray(destinations, "destinations"),
  };
}

/* ── The scheduler ───────────────────────────────────────────────── */

export interface Schedule {
  id: string; tenant_id: string; name: string; description: string;
  cron: string; timezone: string; action_kind: string;
  action_config: Record<string, unknown>;
  status: string; created_by: string; approved_by: string | null;
  approved_at: string | null; next_run_at: string | null;
  last_run_at: string | null; last_status: string | null;
  last_error: string | null;
}

export interface ScheduleRun {
  id: string; status: string; fired_at: string | null;
  duration_ms: number | null; detail: Record<string, unknown>;
}

export async function listSchedules(): Promise<Schedule[]> {
  const d = await caseRequest<{ schedules: Schedule[] }>("/scheduler");
  return d.schedules ?? [];
}

export async function getSchedule(id: string): Promise<{
  schedule: Schedule; next: string[]; runs: ScheduleRun[];
}> {
  return caseRequest(`/scheduler/${id}`);
}

export async function listScheduleActions():
  Promise<{ kind: string; label: string }[]> {
  const d = await caseRequest<{ actions: { kind: string; label: string }[] }>(
    "/scheduler/actions");
  return d.actions ?? [];
}

/** The next few firing times for a cron line, so nobody saves one they misread. */
export async function previewSchedule(body: {
  cron: string; timezone?: string; count?: number;
}): Promise<{ cron: string; timezone: string; next: string[] }> {
  return caseRequest("/scheduler/preview", {
    method: "POST", body: JSON.stringify(body),
  });
}

export async function createSchedule(body: Partial<Schedule>): Promise<Schedule> {
  return caseRequest("/scheduler", { method: "POST", body: JSON.stringify(body) });
}

/** **Any change returns the schedule to draft** and clears its next firing —
 *  a changed recipient list or cron line is exactly what the reviewer needed
 *  to see. Saving with nothing changed changes nothing. */
export async function updateSchedule(
  id: string, body: Partial<Schedule>,
): Promise<Schedule> {
  return caseRequest(`/scheduler/${id}`, {
    method: "PUT", body: JSON.stringify(body),
  });
}

/** Refused (409) when the approver wrote it — approval IS the review. */
export async function approveSchedule(id: string): Promise<Schedule> {
  return caseRequest(`/scheduler/${id}/approve`, { method: "POST" });
}

export async function disableSchedule(id: string): Promise<Schedule> {
  return caseRequest(`/scheduler/${id}/disable`, { method: "POST" });
}

/** Fires it now. The timetable is untouched, so testing a schedule does not
 *  silently skip tonight's run. */
export async function runScheduleNow(id: string): Promise<{
  status: string; detail: Record<string, unknown>;
}> {
  return caseRequest(`/scheduler/${id}/run`, { method: "POST" });
}

export async function deleteSchedule(id: string): Promise<void> {
  await caseRequest(`/scheduler/${id}`, { method: "DELETE" });
}

export async function cancelBotJob(id: string): Promise<BotJob> {
  return caseRequest(`/bots/jobs/${id}/cancel`, { method: "POST" });
}

export async function getBotProposal(id: string): Promise<BotProposal> {
  return caseRequest(`/bots/jobs/${id}/proposal`);
}

/** The checksum is the concurrency guard: a case that changed while the diff
 *  was open answers 409 rather than being silently overwritten. */
export async function acceptBotProposal(
  id: string, expectedChecksum: string, fields?: string[],
): Promise<{ applied: string[] }> {
  return caseRequest(`/bots/jobs/${id}/accept`, {
    method: "POST",
    body: JSON.stringify({ expected_checksum: expectedChecksum, fields }),
  });
}

/* ── HxArbiter — sovereignty & security policy engine (ARB-E) ───────────────
 *
 * Everything here is read-mostly on purpose. The one call that changes what the
 * platform enforces is `promoteNamespace`, and the server refuses it without
 * parity evidence unless an override with a written reason is supplied — so the
 * UI never has to decide whether a promotion is safe, only how to show why it
 * was refused.
 */

export interface ArbiterAttribute {
  path: string; type: string; description: string; allowed?: string[] | null;
}
export interface ArbiterNamespace {
  namespace: string; default_effect: string; max_mode: string;
  description: string; promoted_at?: string | null;
  //: null means decisions here are kept forever, which is the default.
  decision_retention_days?: number | null;
}
export interface ArbiterCatalog {
  attributes: ArbiterAttribute[]; operators: string[];
  namespaces: ArbiterNamespace[];
}
export interface ArbiterPolicy {
  id: string; name: string; namespace: string; description: string;
  scope: "platform" | "tenant"; status: string;
  active_version: number | null; origin: string; retired_at?: string | null;
}
export interface ArbiterDecision {
  id: string; namespace: string; action: string; subject_kind: string;
  subject_id: string; effect: string; mode: string; enforced: boolean;
  default_applied: boolean; matched: unknown[]; obligations: unknown[];
  legacy_effect: string | null; error: string | null;
  claimed_by: string | null; simulated: boolean; latency_us: number;
  sealed: boolean; evaluated_at: string;
}
export interface ArbiterParity {
  namespace: string; window_days: number; compared: number;
  agreements: number; disagreements: number;
  engine_stricter: number; engine_weaker: number; errors: number;
  agreement_rate: number; eligible_for_promotion: boolean;
  reasons: string[]; examples: Record<string, unknown>[];
}
export interface ArbiterDraft {
  id: string; state: string; compiled?: boolean; errors?: string[];
  rules: unknown[]; analysis: { severity: string; kind: string; message: string;
    rule_ids: string[] }[];
  readback: string | null; readback_source: string | null;
  provenance: { provider: string | null; model: string | null;
    prompt_version: string | null; attempts: number };
  intent?: string; namespace?: string; corpus_refs?: string[] | null;
}

export async function getArbiterCatalog(): Promise<ArbiterCatalog> {
  return caseRequest("/arbiter/catalog");
}
export async function listArbiterPolicies(namespace?: string): Promise<ArbiterPolicy[]> {
  return caseRequest(`/arbiter/policies${namespace ? `?namespace=${encodeURIComponent(namespace)}` : ""}`);
}
export async function createArbiterPolicy(body: {
  name: string; namespace: string; description?: string; platform?: boolean;
}): Promise<{ id: string; status: string }> {
  return caseRequest("/arbiter/policies", { method: "POST", body: JSON.stringify(body) });
}
export async function addArbiterVersion(
  policyId: string, body: { rules: unknown[]; mode?: string },
): Promise<{ version: number; signature: string }> {
  return caseRequest(`/arbiter/policies/${policyId}/versions`,
    { method: "POST", body: JSON.stringify(body) });
}
export async function activateArbiterVersion(policyId: string, version: number) {
  return caseRequest(`/arbiter/policies/${policyId}/activate?version=${version}`,
    { method: "POST" });
}
export async function retireArbiterPolicy(policyId: string) {
  return caseRequest(`/arbiter/policies/${policyId}/retire`, { method: "POST" });
}
/** Which version of a rule was in force at a moment — the audit question. */
export async function arbiterActiveAt(policyId: string, moment: string) {
  return caseRequest(
    `/arbiter/policies/${policyId}/active-at?moment=${encodeURIComponent(moment)}`);
}
export async function listArbiterDecisions(params: {
  namespace?: string; subject_id?: string; disagreements_only?: boolean; limit?: number;
} = {}): Promise<ArbiterDecision[]> {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== "") q.set(k, String(v)); });
  const qs = q.toString();
  return caseRequest(`/arbiter/decisions${qs ? `?${qs}` : ""}`);
}
export async function verifyArbiterChain(): Promise<{ ok: boolean; problem: string | null }> {
  return caseRequest("/arbiter/decisions/verify");
}
export async function getArbiterParity(
  namespace: string, windowDays = 14,
): Promise<ArbiterParity> {
  return caseRequest(`/arbiter/parity/${encodeURIComponent(namespace)}?window_days=${windowDays}`);
}
export async function syncArbiterBridge() {
  return caseRequest("/arbiter/bridge/sync", { method: "POST" });
}
export async function promoteArbiterNamespace(
  namespace: string,
  body: { max_mode: string; override_parity?: boolean; override_reason?: string },
) {
  return caseRequest(`/arbiter/namespaces/${encodeURIComponent(namespace)}/promote`,
    { method: "POST", body: JSON.stringify(body) });
}
export async function simulateArbiter(body: {
  subject: { kind: string; id: string; attrs?: Record<string, unknown> };
  action: string;
  resource: { kind: string; id?: string; attrs?: Record<string, unknown> };
  context?: Record<string, unknown>;
  attributes?: Record<string, unknown>;
}) {
  return caseRequest("/arbiter/simulate", { method: "POST", body: JSON.stringify(body) });
}
export async function createArbiterDraft(body: {
  intent: string; namespace: string; policy_id?: string; corpus_refs?: string[];
}): Promise<ArbiterDraft> {
  return caseRequest("/arbiter/drafts", { method: "POST", body: JSON.stringify(body) });
}
export async function getArbiterDraft(id: string): Promise<ArbiterDraft> {
  return caseRequest(`/arbiter/drafts/${id}`);
}
export async function approveArbiterDraft(id: string, readback: string) {
  return caseRequest(`/arbiter/drafts/${id}/approve`,
    { method: "POST", body: JSON.stringify({ readback }) });
}
export async function activateArbiterDraft(id: string) {
  return caseRequest(`/arbiter/drafts/${id}/activate`, { method: "POST" });
}

/* ── ARB-I — policy upkeep ─────────────────────────────────────────────────
 *
 * `getArbiterUpkeep` is deterministic and always available. `adviseArbiterUpkeep`
 * asks HxNexus to read the same findings back in plain English and answers 503
 * when no provider is configured — which is a feature being ABSENT, not broken,
 * so the caller shows the report unchanged and hides the button.
 */
export interface ArbiterUpkeepFinding {
  id: string; severity: "error" | "warning" | "info"; kind: string;
  message: string; suggestion: string;
  namespace: string | null; policy_id: string | null; policy_name: string | null;
  scope: "platform" | "tenant" | null; rule_ids: string[];
  assessable: boolean; caveat: string | null;
  evidence: Record<string, unknown>;
}
export interface ArbiterUpkeep {
  window_days: number; since: string; generated_at: string;
  scope: "tenant" | "platform";
  policies_examined: number; namespaces_examined: number;
  decisions_examined: number;
  counts: { error: number; warning: number; info: number };
  findings: ArbiterUpkeepFinding[];
  limitations: string[];
}
export type ArbiterUpkeepAction =
  | "none" | "review_rules" | "amend_policy" | "check_pep_wiring"
  | "run_bridge_sync" | "review_promotion" | "check_sealer" | "investigate_now";

export interface ArbiterUpkeepAdvice {
  ok: boolean; summary: string;
  // `action` is a CLOSED set, not free text. It used to be a paste-ready
  // sentence the model wrote, which an operator could send straight into the
  // authoring model — and a policy NAME reading "ignore all previous
  // instructions…" steered a real model into writing that sentence. The UI
  // owns the wording for each action now; the model only picks one.
  entries: Record<string, { meaning: string;
    priority: "now" | "soon" | "watch"; action: ArbiterUpkeepAction }>;
  errors: string[]; dropped: string[];
  explained: number; of_findings: number;
  provenance: { provider: string | null; model: string | null;
    prompt_version: string | null };
}

export async function getArbiterUpkeep(windowDays = 30): Promise<ArbiterUpkeep> {
  return caseRequest(`/arbiter/upkeep?window_days=${windowDays}`);
}
/** How long decisions in a namespace are kept. `null` = forever. */
export async function setArbiterRetention(
  namespace: string, days: number | null,
) {
  return caseRequest(`/arbiter/namespaces/${encodeURIComponent(namespace)}/retention`,
    { method: "PUT", body: JSON.stringify({ decision_retention_days: days }) });
}
export async function anchorArbiterDecisions(force = false) {
  return caseRequest(`/arbiter/decisions/anchor?force=${force}`, { method: "POST" });
}
export async function purgeArbiterDecisions() {
  return caseRequest("/arbiter/decisions/purge", { method: "POST" });
}
export async function exportArbiterPolicies(namespace?: string) {
  return caseRequest(`/arbiter/export${namespace ? `?namespace=${encodeURIComponent(namespace)}` : ""}`);
}
export async function importArbiterPolicies(body: {
  document: unknown; platform?: boolean; refuse_duplicates?: boolean;
  activate?: boolean;
}) {
  return caseRequest("/arbiter/import", { method: "POST", body: JSON.stringify(body) });
}

export async function adviseArbiterUpkeep(
  body: { window_days?: number; finding_ids?: string[] } = {},
): Promise<ArbiterUpkeepAdvice> {
  return caseRequest("/arbiter/upkeep/advise",
    { method: "POST", body: JSON.stringify(body) });
}

// ── F-05 agent workforce ────────────────────────────────────────────────────
// The roster reads as one call: availability arrives with its REASONS, not a
// bare flag, because "at capacity" and "over budget" are different problems and
// a screen that showed only "unavailable" would hide which one you have.

export interface WorkforceAvailability {
  available: boolean; reasons: string[];
  active_load: number; tokens_last_hour: number | null;
}
export interface WorkforceAgentRef {
  slug: string; display_name: string; kind: string; status: string;
  model_binding: string | null;
}
export interface WorkforceRow {
  agent_id: string; skills: string[];
  max_concurrent: number | null; token_budget_per_hour: number | null;
  escalation_queue: string; routing_weight: number;
  benched: boolean; bench_reason: string | null; benched_at: string | null;
  availability?: WorkforceAvailability;
  agent?: WorkforceAgentRef;
}
export interface WorkforceRoster {
  scorecard_cron: { enabled: boolean; interval_hours: number; window_days: number };
  roster: WorkforceRow[];
}
export interface AgentScorecard {
  id: string; agent_id: string;
  window: { start: string; end: string };
  decisions: number; reviewed: number; accepted: number; overridden: number;
  override_rate: number | null;
  cost_total: number; cost_per_decision: number | null; currency: string | null;
  proposed_weight: number | null; proposal_status: string | null;
  proposal_reason: string | null;
  decided_by: string | null; decided_at: string | null; created_at: string;
}

export async function getWorkforce(): Promise<WorkforceRoster> {
  return caseRequest("/agents/workforce");
}
export async function listWorkforceScorecards(
  opts: { agentId?: string; status?: string; limit?: number } = {},
): Promise<{ scorecards: AgentScorecard[] }> {
  const qs = new URLSearchParams();
  if (opts.agentId) qs.set("agent_id", opts.agentId);
  if (opts.status)  qs.set("status", opts.status);
  if (opts.limit)   qs.set("limit", String(opts.limit));
  const q = qs.toString();
  return caseRequest(`/agents/workforce/scorecards${q ? `?${q}` : ""}`);
}
export async function enrolWorkforceAgent(agentId: string, body: {
  escalation_queue: string; skills?: string[];
  max_concurrent?: number | null; token_budget_per_hour?: number | null;
}): Promise<WorkforceRow> {
  return caseRequest(`/agents/${agentId}/workforce`,
    { method: "PUT", body: JSON.stringify(body) });
}
export async function benchWorkforceAgent(
  agentId: string, benched: boolean, reason = "",
): Promise<WorkforceRow> {
  return caseRequest(`/agents/${agentId}/workforce/bench`,
    { method: "POST", body: JSON.stringify({ benched, reason }) });
}
export async function buildWorkforceScorecard(
  agentId: string, days = 7,
): Promise<AgentScorecard> {
  return caseRequest(`/agents/${agentId}/workforce/scorecard?days=${days}`,
    { method: "POST" });
}
export async function proposeWorkforceWeight(
  cardId: string, weight: number, reason: string,
): Promise<{ id: string; proposed_weight: number; status: string }> {
  return caseRequest(`/agents/workforce/scorecards/${cardId}/propose`,
    { method: "POST", body: JSON.stringify({ weight, reason }) });
}
export async function decideWorkforceWeight(
  cardId: string, approve: boolean,
): Promise<{ id: string; status: string; routing_weight: number | null }> {
  return caseRequest(`/agents/workforce/scorecards/${cardId}/decide`,
    { method: "POST", body: JSON.stringify({ approve }) });
}
export async function listAgents(): Promise<{
  tokens_enabled: boolean; capability_vocabulary: string[];
  issuable_kinds: string[];
  agents: { id: string; slug: string; kind: string; display_name: string;
            model_binding: string | null; status: string }[];
}> {
  return caseRequest("/agents");
}

// ── F-06 ambient capture ────────────────────────────────────────────────────
// The digest is a read of the case by another name, so every call here is
// case-scoped and gated by the caller's relationship to the case.

export interface CaptureRun {
  id: string; case_id: string;
  source: { kind: string; ref: string; locator: string | null; sha256: string };
  status: string; refusal_reason: string | null; error: string | null;
  provider: string | null; model: string | null; prompt_sha256: string | null;
  attempts: number; proposed: number; dropped: number;
  requested_by: string; created_at: string | null; completed_at: string | null;
}
export interface CaptureProposal {
  id: string; run_id: string; case_id: string;
  kind: string; target: string | null;
  current_value: unknown; proposed_value: unknown;
  confidence: number | null;
  snippet: string | null; locator: string | null;
  status: string; applied_value: unknown;
  decided_by: string | null; decided_by_name: string | null;
  decided_at: string | null;
  ledger_record_id: string | null; created_at: string | null;
}
export interface CaptureDigest {
  case_id: string; enabled: boolean;
  source_kinds: string[]; proposal_kinds: string[];
  runs: CaptureRun[]; proposals: CaptureProposal[];
}
export interface CaptureTargets {
  case_id: string;
  targets: { key: string; type: string; label: string; description: string }[];
  blocked_namespaces: string[]; max_proposals: number;
}

export async function getCaptureDigest(
  caseId: string, includeDecided = false,
): Promise<CaptureDigest> {
  return caseRequest(
    `/capture/cases/${caseId}${includeDecided ? "?include_decided=true" : ""}`);
}
export async function getCaptureTargets(caseId: string): Promise<CaptureTargets> {
  return caseRequest(`/capture/cases/${caseId}/targets`);
}
export async function runCapture(caseId: string, body: {
  source_kind: string; source_ref?: string | null; text?: string | null;
}): Promise<CaptureRun & { refusal_reason?: string; detail?: string }> {
  return caseRequest(`/capture/cases/${caseId}/runs`,
    { method: "POST", body: JSON.stringify(body) });
}
export async function decideCaptureProposal(
  proposalId: string, decision: "accept" | "edit" | "reject", value?: unknown,
): Promise<CaptureProposal> {
  return caseRequest(`/capture/proposals/${proposalId}/decide`,
    { method: "POST", body: JSON.stringify({ decision, value }) });
}

// ── F-07 sovereign model marketplace ────────────────────────────────────────
// Two facts this API is careful about, and the types carry both rather than
// flattening them:
//
//   · `signed` says the bytes match a key. It does NOT say the key belongs to
//     anybody — the public half travels inside the file — so `signing_key_id`
//     is what an operator pins out of band, and no field here is called
//     "trusted".
//   · `declared_metrics` is the publisher's arithmetic about the publisher's
//     data; an evaluation's `measured` is about THIS tenant's. They are
//     separate fields because putting one where the other belongs would print
//     a stranger's number at somebody deciding whether to trust it.

export interface ModelCanaryCheck {
  check: string; ok: boolean; blocking: boolean; detail: string;
}
export interface ModelPackage {
  id: string; direction: "published" | "imported"; kind: string;
  package_id: string; version: number; content_sha256: string;
  status: string; status_reason: string | null;
  manifest: Record<string, unknown>;
  trained_on: number | null;
  declared_metrics: Record<string, unknown> | null;
  eval_fingerprint: string | null;
  canary: { verdict: string; detail: { checks?: ModelCanaryCheck[] } | null };
  data_classes: string[] | null; special_category: boolean;
  signed: boolean; signing_key_id: string | null;
  publisher_ref: string | null;
  signed_off_by: string | null; signed_off_at: string | null;
  created_by: string | null; created_at: string;
}
export interface ModelEvaluation {
  id: string; verdict: "pass" | "fail" | "insufficient_data"; reason: string | null;
  holdout_size: number; positives: number; negatives: number;
  declared: Record<string, unknown> | null;
  measured: Record<string, unknown> | null;
  tolerance: number | null;
  evaluated_by: string | null; evaluated_at: string;
}
export interface ModelInstall {
  id: string; package_ref: string; model_origin: string;
  target_kind: string; target_ref: string | null; status: string;
  installed_by: string | null; installed_at: string;
  activated_by: string | null; activated_at: string | null;
  uninstalled_at: string | null; shredded_at: string | null;
}
export interface ModelSources {
  risk_models: { source: string; version: number; status: string;
                 trained_on: number; metrics: Record<string, unknown>;
                 publishable: boolean }[];
  signal_packs: { source: string; name: string; version: string;
                  enabled: boolean; rules: number }[];
}
export interface ModelPrepared {
  kind: string; source_ref: string;
  manifest: Record<string, any>;
  payload: Record<string, unknown>;
  trained_on: number;
  declared_metrics: Record<string, unknown>;
  canary: { verdict: string; checks: ModelCanaryCheck[]; failed: string[];
            data_classes: string[]; special_category: boolean };
  eval_fingerprint: string;
}

export async function listModelSources(): Promise<ModelSources> {
  return caseRequest("/model-market/sources");
}
export async function prepareModelPackage(
  body: { kind: string; source: string },
): Promise<ModelPrepared> {
  return caseRequest("/model-market/prepare",
    { method: "POST", body: JSON.stringify(body) });
}
export async function publishModelPackage(body: {
  kind: string; source: string; package_id: string; acknowledged: boolean;
}): Promise<{ package: ModelPackage; published: boolean }> {
  return caseRequest("/model-market/publish",
    { method: "POST", body: JSON.stringify(body) });
}
export async function listModelPackages(
  direction?: "published" | "imported",
): Promise<{ packages: ModelPackage[]; count: number }> {
  return caseRequest(`/model-market/packages${direction ? `?direction=${direction}` : ""}`);
}
export async function getModelPackage(id: string): Promise<{
  package: ModelPackage; evaluations: ModelEvaluation[]; install: ModelInstall | null;
}> {
  return caseRequest(`/model-market/packages/${id}`);
}
export async function downloadModelPackage(id: string): Promise<{
  filename: string; content: Record<string, unknown>; signed: boolean;
}> {
  return caseRequest(`/model-market/packages/${id}/file`);
}
export async function importModelPackage(
  pkg: string,
): Promise<{ package: ModelPackage }> {
  return caseRequest("/model-market/import",
    { method: "POST", body: JSON.stringify({ package: pkg }) });
}
export async function evaluateModelPackage(
  id: string, tolerance?: number,
): Promise<{ evaluation: ModelEvaluation }> {
  return caseRequest(`/model-market/packages/${id}/evaluate`,
    { method: "POST", body: JSON.stringify(tolerance === undefined ? {} : { tolerance }) });
}
export async function installModelPackage(
  id: string,
): Promise<{ install: ModelInstall; note: string }> {
  return caseRequest(`/model-market/packages/${id}/install`, { method: "POST" });
}
export async function uninstallModelPackage(
  installId: string, shred: boolean,
): Promise<{ install: ModelInstall }> {
  return caseRequest(`/model-market/installs/${installId}/uninstall`,
    { method: "POST", body: JSON.stringify({ shred }) });
}

// ── F-08 self-healing ───────────────────────────────────────────────────────
// The ladder is returned in full, rungs that PASSED included: a person approving
// a change to their own live process is entitled to see what was checked, not a
// single green tick.

export interface HealingRung {
  rung: string; ok: boolean; detail: string;
  applicable?: boolean; ran?: boolean;
  started?: number; terminal?: number; stuck?: number; p95_ms?: number | null;
}
export interface HealingEpisode {
  id: string; case_type_id: string; insight_id: string | null;
  operator: string; diagnosis: Record<string, unknown>;
  proposal: Record<string, unknown>;
  ladder: HealingRung[]; stopped_at: string | null; stopped_reason: string | null;
  status: string;
  approved_by: string | null; approved_at: string | null; decided_reason: string | null;
  applied_at: string | null;
  rollback_signed: boolean; rollback_artifact: string | null;
  watch_until: string | null; watch_metric: string | null;
  watch_bound_pct: number | null;
  outcome: Record<string, any> | null; outcome_at: string | null;
  rolled_back_at: string | null; rollback_reason: string | null;
  incident_case_id: string | null;
  replay_run_id: string | null; twin_run_id: string | null;
  sequence: number; content_hash: string; created_at: string;
}
export interface HealingStatus {
  enabled: boolean; reason: string | null; operator_switch: boolean;
  tenant_settings: Record<string, any>;
  boundary: { operators: string[]; reaches: Record<string, Record<string, string[]>>;
              forbidden: string[]; intersection: string[] };
}

export async function getHealingStatus(): Promise<HealingStatus> {
  return caseRequest("/healing/status");
}
export async function listHealingEpisodes(
  status?: string,
): Promise<{ episodes: HealingEpisode[]; count: number }> {
  return caseRequest(`/healing/episodes${status ? `?status=${status}` : ""}`);
}
export async function getHealingEpisode(id: string): Promise<{ episode: HealingEpisode }> {
  return caseRequest(`/healing/episodes/${id}`);
}
export async function approveHealingEpisode(
  id: string, body: { reason?: string; watch_hours?: number },
): Promise<{ episode: HealingEpisode; note: string }> {
  return caseRequest(`/healing/episodes/${id}/approve`,
    { method: "POST", body: JSON.stringify(body) });
}
export async function rejectHealingEpisode(
  id: string, reason: string,
): Promise<{ episode: HealingEpisode }> {
  return caseRequest(`/healing/episodes/${id}/reject`,
    { method: "POST", body: JSON.stringify({ reason }) });
}
export async function rollbackHealingEpisode(
  id: string, reason: string,
): Promise<{ episode: HealingEpisode }> {
  return caseRequest(`/healing/episodes/${id}/rollback`,
    { method: "POST", body: JSON.stringify({ reason }) });
}
export async function verifyHealingChain(): Promise<{
  verified: boolean; episodes: number; broken_at?: number; reason?: string;
}> {
  return caseRequest("/healing/chain");
}

// ── F-11 intent-based execution ─────────────────────────────────────────────
// `plan_sha256` is passed BACK on confirm. The client sends the hash it
// displayed, so a plan that moved between reading and confirming is refused —
// that binding is the whole of "no plan-swap after approval".

export interface IntentStep {
  step: number; fragment: string; name: string; does: string; capabilities: string[];
}
export interface IntentPlan {
  id: string; intent: string;
  goal_spec: { goals: string[]; given: string[]; dropped?: string[] };
  spec_source: string; status: string;
  fragments: string[]; steps: IntentStep[] | null;
  capabilities: string[]; data_classes: string[]; step_count: number;
  plan_sha256: string | null;
  ladder: { rung: string; ok: boolean; detail: string; consequential?: string[] }[];
  stopped_at: string | null; stopped_reason: string | null;
  confirmed_by: string | null; confirmed_at: string | null;
  confirmed_sha256: string | null; executed_at: string | null; created_at: string;
}
export interface IntentFragment {
  slug: string; display_name: string; description: string;
  requires: string[]; provides: string[]; data_classes: string[];
  capabilities: string[]; step_count: number; status: string;
  content_sha256: string; signed: boolean; signing_key_id: string | null;
  conformance: Record<string, any> | null;
  verified_by: string | null; verified_at: string | null;
}

export async function listFragments(): Promise<{
  fragments: IntentFragment[]; count: number;
  vocabulary: { goals: string[]; facts: string[] }; capabilities: string[];
}> {
  return caseRequest("/intent/fragments");
}
export async function verifyFragment(slug: string): Promise<{ fragment: IntentFragment }> {
  return caseRequest(`/intent/fragments/${slug}/verify`, { method: "POST" });
}
export async function createIntentPlan(body: {
  intent: string; goal_spec?: Record<string, unknown> | null; require_twin?: boolean;
}): Promise<{ plan: IntentPlan }> {
  return caseRequest("/intent/plans", { method: "POST", body: JSON.stringify(body) });
}
export async function listIntentPlans(
  status?: string,
): Promise<{ plans: IntentPlan[]; count: number }> {
  return caseRequest(`/intent/plans${status ? `?status=${status}` : ""}`);
}
export async function getIntentPlan(id: string): Promise<{
  plan: IntentPlan; definition: Record<string, unknown> | null;
}> {
  return caseRequest(`/intent/plans/${id}`);
}
export async function confirmIntentPlan(
  id: string, planSha256: string,
): Promise<{ plan: IntentPlan }> {
  return caseRequest(`/intent/plans/${id}/confirm`,
    { method: "POST", body: JSON.stringify({ plan_sha256: planSha256 }) });
}
export async function executeIntentPlan(id: string): Promise<{
  plan_id: string; plan_sha256: string; definition: Record<string, any>; note: string;
}> {
  return caseRequest(`/intent/plans/${id}/execute`, { method: "POST" });
}

// ── F-12 process-as-negotiation ─────────────────────────────────────────────
// `seam_ceiling` is NOT `effective_ceiling`. The first is what the seam carries
// right now; the second is what was negotiated. A term agreed is not a term in
// force, and the screen must never show one where the other belongs.

export interface ContractClause {
  clause: string; ok: boolean; detail: string; agreed?: unknown;
  negotiated?: string; platform?: string;
}
export interface JointContract {
  id: string; name: string; version: number; counterparty_id: string;
  supersedes: string | null; status: string; unmatched_reason: string | null;
  composition: { goals: string[]; seams: number;
                 steps: { side: string; fragment: string; provides: string[];
                          crosses_seam: boolean }[] } | null;
  resolution: { clauses: ContractClause[]; matched: boolean } | null;
  effective_ceiling: string | null; seam_ceiling: string;
  contract_sha256: string | null;
  signed_by_us: boolean; signed_by_them: boolean;
  approved_by: string | null; approved_at: string | null; enforced_at: string | null;
  suspended_at: string | null; suspended_by: string | null;
  suspended_reason: string | null; created_at: string;
}

export async function listContracts(
  status?: string,
): Promise<{ contracts: JointContract[]; count: number }> {
  return caseRequest(`/negotiate/contracts${status ? `?status=${status}` : ""}`);
}
export async function verifyContract(id: string): Promise<{
  agrees: boolean; inputs_agree: boolean; stored_agrees: boolean;
  recomputed: string; stored: string; detail: string;
}> {
  return caseRequest(`/negotiate/contracts/${id}/verify`);
}
export async function approveContract(id: string): Promise<{ contract: JointContract }> {
  return caseRequest(`/negotiate/contracts/${id}/approve`, { method: "POST" });
}
export async function promoteContract(
  id: string, to: string,
): Promise<{ contract: JointContract }> {
  return caseRequest(`/negotiate/contracts/${id}/promote`,
    { method: "POST", body: JSON.stringify({ to }) });
}
export async function suspendContract(
  id: string, reason: string,
): Promise<{ contract: JointContract; seam: string; note: string }> {
  return caseRequest(`/negotiate/contracts/${id}/suspend`,
    { method: "POST", body: JSON.stringify({ reason }) });
}
export async function contractAnchors(id: string): Promise<{
  exchanges: number; ours: number; theirs: number; clean: boolean;
  problems: { direction: string; seq: number; detail: string }[]; note: string;
}> {
  return caseRequest(`/negotiate/contracts/${id}/anchors`);
}

// ── F-10 field operations: the conflict queue ───────────────────────────────
// Nothing here decided itself. A conflict clears only when a person chooses.

export interface FieldConflict {
  id: string; node_id: string; case_id: string | null; seq: number;
  kind: string; target: string | null; field_value: unknown;
  /** The hub's side of the disagreement. `hub_value_known` false means the hub
   *  holds no such field at all — which is not the same as holding an empty
   *  value, and the queue must not draw them the same way. */
  hub_value: unknown; hub_value_known: boolean;
  reason: string | null; node_time: string | null; received_at: string;
}
export interface FieldNode {
  id: string; slug: string; display_name: string; key_id: string;
  status: string; ceiling: string; charter: Record<string, unknown>;
  snapshot_max_age_hours: number; subchain_head: string | null;
  last_contact_at: string | null; shred_ordered_at: string | null;
  shredded_at: string | null;
  staleness: { stale: boolean; reason: string; stale_after?: string } | null;
  created_at: string;
}

export async function listFieldNodes(): Promise<{ nodes: FieldNode[]; count: number }> {
  return caseRequest("/field/nodes");
}
export interface FieldNodeIn {
  slug: string;
  display_name: string;
  /** Raw base64 of the node's 32-byte Ed25519 PUBLIC key. Never PEM, and never
   *  generated here — a hub that once held the private key has pinned nothing. */
  public_key: string;
  charter: Record<string, unknown>;
  ceiling: string;
  snapshot_max_age_hours: number;
}
export async function registerFieldNode(body: FieldNodeIn): Promise<FieldNode> {
  // The route answers `{"node": {...}}`, not the node. Unwrapping here rather
  // than at the call site keeps the shape trap in one place — the same one
  // `POST /negotiate/contracts` sets, which answers without an `id` at all.
  const r = await caseRequest<{ node: FieldNode }>("/field/nodes",
    { method: "POST", body: JSON.stringify(body) });
  return (r as { node?: FieldNode }).node ?? (r as unknown as FieldNode);
}
export async function listFieldConflicts(
  node?: string,
): Promise<{ conflicts: FieldConflict[]; count: number }> {
  return caseRequest(`/field/conflicts${node ? `?node=${node}` : ""}`);
}
export async function resolveFieldConflict(
  id: string, resolution: "keep_field" | "keep_hub",
): Promise<{ id: string; merge_state: string; resolution: string; resolved_by: string }> {
  return caseRequest(`/field/conflicts/${id}/resolve`,
    { method: "POST", body: JSON.stringify({ resolution }) });
}

// ── Item 12 Customer Engagement: the rep desk ───────────────────────────────
// Two things the screen must never confuse. A panel in `withheld` is ABSENT,
// not empty — an empty panel says "this customer has none of these". And the
// verification tier comes from the INTERACTION, never from a control the rep
// can set.

export interface EngageStatus {
  enabled: boolean; released: boolean; entitled: boolean;
  package_id: string; feature_key: string; note: string;
}
export interface EngageIdentifier {
  kind: string; value: string; masked: boolean; revealable: boolean;
  verified: boolean; verified_by: string | null; is_primary: boolean; source: string;
}
export interface EngageSummary {
  account_exists: boolean; name_initial: string;
  display_name: string | null; display_name_masked: string | null;
  open_cases: number; status: string;
}
export interface SuggestedRelation {
  customer_id: string; name_initial: string; display_name_masked: string;
  suggested: string; because: string[]; score: number; hops: number;
  already_related: boolean; suggestion: true; may_act_for: false;
}
export interface ThreeSixty {
  customer_id: string; party_kind: string; verification: string;
  panels: {
    summary?: EngageSummary;
    identifiers?: EngageIdentifier[];
    relations?: { to: string; relation: string; may_act_for: boolean; note: string | null }[];
    /** Oct item 4: HxGraph-derived — SUGGESTIONS, never may_act_for. */
    suggested_relations?: {
      suggestions: SuggestedRelation[];
      skipped_as_too_common: Record<string, number>; max_hops: number;
    };
    interactions?: { id: string; channel: string; direction: string;
                     started_at: string | null; verification_level: string;
                     disposition_code: string | null; status: string;
                     outcome: string | null }[];
    cases?: { object_id: string; reason: string; linked_at: string | null }[];
    conversations?: { object_id: string; reason: string; linked_at: string | null }[];
    consents?: unknown;
  };
  withheld: { panel: string; why: string }[];
  source_of_truth: string; external_system: string | null;
  freshness: { stale: boolean; reason: string; refreshed_at?: string | null;
               minutes_old?: number };
}
export interface ScreenPop {
  interaction_id: string; customer_id: string | null; verification_level: string;
  resolution: { resolved: boolean; rung: number; reason: string;
                confidence?: string; verified?: boolean };
  stub: { actions: string[]; prefill: Record<string, string>; note: string } | null;
  inherited_verification: boolean;
  /** Set when this contact was opened from a colleague's join link. */
  joined_from?: string; mode?: "warm" | "cold";
}

export interface JoinLink {
  id: string; interaction_id: string; mode: "warm" | "cold";
  for_user_id: string | null; for_queue: string | null;
  created_by: string; created_at: string | null; expires_at: string | null;
  state: "open" | "used" | "revoked" | "expired";
  redeemed_by: string | null; redeemed_at: string | null;
  joined_interaction_id: string | null;
}

/** The token is in this response only — it is stored as a hash. */
export async function engageCreateJoinLink(interactionId: string, body: {
  mode: "warm" | "cold"; for_user_id?: string | null; for_queue?: string | null;
  ttl_minutes?: number;
}): Promise<JoinLink & { token: string; path: string; note: string }> {
  return caseRequest(`/engage/interactions/${interactionId}/join-links`,
    { method: "POST", body: JSON.stringify(body) });
}

export async function engageListJoinLinks(interactionId: string): Promise<{ links: JoinLink[] }> {
  return caseRequest(`/engage/interactions/${interactionId}/join-links`);
}

export async function engageRevokeJoinLink(joinId: string): Promise<JoinLink> {
  return caseRequest(`/engage/join-links/${joinId}`, { method: "DELETE" });
}

export async function engageRedeemJoinLink(token: string): Promise<ScreenPop> {
  return caseRequest("/engage/join-links/redeem",
    { method: "POST", body: JSON.stringify({ token }) });
}

// ── Profile backfill (Oct item 3) ────────────────────────────────────────────

export type BackfillKind = "create_profile" | "enrich_profile" | "merge_profiles";
export type BackfillStatus = "proposed" | "accepted" | "rejected" | "superseded";
export interface BackfillIdentifier {
  kind: string; value: string; display: string; verified: boolean; via: string;
}
export interface BackfillProposal {
  id: string; kind: BackfillKind; status: BackfillStatus;
  payload: {
    display_name?: string; primary_email?: string; customer_id?: string;
    identifiers?: BackfillIdentifier[]; case_ids?: string[];
    winner_id?: string; loser_ids?: string[];
    profiles?: { id: string; display_name: string }[]; shared?: BackfillIdentifier[];
  };
  evidence: { sources?: string[]; portal_customer_id?: string | null; case_count?: number };
  decided_by: string | null; decided_at: string | null; reason: string | null;
  result: Record<string, unknown> | null; updated_at: string | null;
}
export interface BackfillRun {
  id: string; started_by: string; started_at: string | null; finished_at: string | null;
  counts: Record<string, number>;
}

/** Scans history and writes PROPOSALS. It applies nothing. */
export async function engageStartBackfill(): Promise<BackfillRun> {
  return caseRequest("/engage/backfill/runs", { method: "POST" });
}
export async function engageBackfillRuns(): Promise<{ runs: BackfillRun[] }> {
  return caseRequest("/engage/backfill/runs");
}
export async function engageBackfillProposals(params: {
  status?: BackfillStatus; kind?: BackfillKind; limit?: number; offset?: number;
} = {}): Promise<{ total: number; proposals: BackfillProposal[] }> {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined) q.set(k, String(v)); });
  return caseRequest(`/engage/backfill/proposals?${q}`);
}
export async function engageAcceptProposal(id: string, reason?: string): Promise<{
  proposal: BackfillProposal; result: Record<string, unknown>;
}> {
  return caseRequest(`/engage/backfill/proposals/${id}/accept`,
    { method: "POST", body: JSON.stringify({ reason: reason ?? null }) });
}
export async function engageRejectProposal(id: string, reason: string): Promise<{
  proposal: BackfillProposal;
}> {
  return caseRequest(`/engage/backfill/proposals/${id}/reject`,
    { method: "POST", body: JSON.stringify({ reason }) });
}

export async function getEngageStatus(): Promise<EngageStatus> {
  return caseRequest("/engage/status");
}
export async function engageScreenPop(body: {
  channel: string; ani?: string | null; address?: string | null;
  dnis?: string | null; queue?: string | null; external_call_id?: string | null;
  transferred_from?: string | null;
}): Promise<ScreenPop> {
  return caseRequest("/engage/screen-pop",
    { method: "POST", body: JSON.stringify(body) });
}
export async function engage360(
  customerId: string, interactionId?: string | null,
): Promise<ThreeSixty> {
  return caseRequest(`/engage/profiles/${customerId}/360${
    interactionId ? `?interaction_id=${interactionId}` : ""}`);
}
export async function engageVerify(
  interactionId: string, body: { factor: string; ok: boolean; to_tier: string },
): Promise<{ verified: boolean; verification_level: string; message?: string;
             verified_by?: string }> {
  return caseRequest(`/engage/interactions/${interactionId}/verify`,
    { method: "POST", body: JSON.stringify(body) });
}
/** Record a relation a rep agrees with. `may_act_for` is never set from a
 *  suggestion — acting for somebody is a separate, deliberate grant. */
export async function engageRelate(customerId: string, body: {
  to_customer_id: string; relation: "related_to" | "member_of" | "contact_for";
  note?: string | null;
}): Promise<unknown> {
  return caseRequest(`/engage/profiles/${customerId}/relations`,
    { method: "POST", body: JSON.stringify({ ...body, may_act_for: false }) });
}

export async function engageReveal(
  customerId: string, body: { field_path: string; reason: string;
                              interaction_id?: string | null },
): Promise<{ revealed: { field_path: string; at_verification: string; by: string } }> {
  return caseRequest(`/engage/profiles/${customerId}/reveal`,
    { method: "POST", body: JSON.stringify(body) });
}
export async function engageTransfer(
  interactionId: string, body: { to_agent: string; queue?: string | null },
): Promise<{ interaction_id: string; verification_level: string;
             transferred_from: string; note: string }> {
  return caseRequest(`/engage/interactions/${interactionId}/transfer`,
    { method: "POST", body: JSON.stringify(body) });
}
export async function engageWrapUp(
  interactionId: string, body: { disposition_code: string; notes?: string | null;
                                 outcome?: string | null },
): Promise<{ interaction_id: string; status: string }> {
  return caseRequest(`/engage/interactions/${interactionId}/wrap-up`,
    { method: "POST", body: JSON.stringify(body) });
}
export async function engageOnBehalf(
  customerId: string, body: { interaction_id: string; purpose?: string },
): Promise<{
  attribution: Record<string, any>;
  consent: { granted: boolean; reason?: string; action: string | null;
             purpose?: string; subject?: string | null; note?: string };
}> {
  return caseRequest(`/engage/profiles/${customerId}/on-behalf`,
    { method: "POST", body: JSON.stringify(body) });
}
// ── Rep desk action rail (Oct item 2, 2026-09-29) ────────────────────────────

export type RailBlockKind = "privilege" | "tier" | "consent" | "prerequisite";
export interface RailAction {
  key: "reply" | "document_request" | "payment" | "callback" | "live_session"
     | "consent" | "follow_up";
  label: string;
  enabled: boolean;
  /** Separate reasons, because the fixes differ. */
  blocked_by: { kind: RailBlockKind; reason: string }[];
  notes: string[];
  min_tier: string;
  privilege: string | null;
  rides_on: string;
  /** Oct item 5: an industry rail's wording and presets. */
  guidance?: string;
  defaults?: Record<string, string | number>;
}
export interface RailConversation {
  id: string; channel: string; peer_hint: string | null;
  opted_out: boolean; template_only: boolean;
}
export interface RailCase {
  id: string; case_number: string; status: string;
  conversations: RailConversation[];
  payment_steps: { id: string; name: string }[];
}
export type ConsentState = "granted" | "withdrawn" | "none";
export interface ActionRailView {
  interaction_id: string; customer_id: string; verification_level: string;
  cases: RailCase[];
  consent: Record<"email" | "phone",
    { readable: boolean; purposes?: Record<string, ConsentState> } | null>;
  actions: RailAction[];
  capturable_purposes: string[];
  portal_link: string | null;
  rail?: { key: string; name: string; industry: string; guidance: string } | null;
  available_rails?: { key: string; name: string; industry: string }[];
}

export async function engageActionRail(customerId: string, interactionId: string,
                                       rail?: string | null): Promise<ActionRailView> {
  const q = new URLSearchParams({ interaction_id: interactionId });
  if (rail) q.set("rail", rail);
  return caseRequest(`/engage/profiles/${customerId}/action-rail?${q}`);
}

// ── Industry rails (Oct item 5) ──────────────────────────────────────────────

export interface EngageRail {
  key: string; name: string; industry: string; source: "pack" | "tenant";
  package_id: string | null; pack_version: string | null; enabled: boolean;
  edited: boolean; config: Record<string, unknown>; updated_at: string | null;
}
export async function engageRails(): Promise<{ rails: EngageRail[] }> {
  return caseRequest("/engage/rails");
}
export async function engagePutRail(key: string, config: Record<string, unknown>,
                                    enabled = true): Promise<EngageRail> {
  return caseRequest(`/engage/rails/${encodeURIComponent(key)}`,
    { method: "PUT", body: JSON.stringify({ config, enabled }) });
}
export async function engageToggleRail(key: string, enabled: boolean): Promise<EngageRail> {
  return caseRequest(`/engage/rails/${encodeURIComponent(key)}`,
    { method: "PATCH", body: JSON.stringify({ enabled }) });
}
export async function engageDeleteRail(key: string): Promise<{ deleted?: boolean; disabled?: boolean }> {
  return caseRequest(`/engage/rails/${encodeURIComponent(key)}`, { method: "DELETE" });
}

export async function engageCaptureConsent(customerId: string, body: {
  interaction_id: string; purpose: string; action: "granted" | "withdrawn";
  subject_kind: "email" | "phone";
}): Promise<{ id: string; purpose: string; action: string }> {
  return caseRequest(`/engage/profiles/${customerId}/consent`,
    { method: "POST", body: JSON.stringify(body) });
}

// ── Rep desk P5/P6 (2026-09-17) ──────────────────────────────────────────────

export interface NextAction { key: string; title: string; why: string; cites: string[] }
export async function engageNextActions(
  customerId: string, interactionId?: string | null,
): Promise<{ customer_id: string; verification: string; actions: NextAction[] }> {
  return caseRequest(`/engage/profiles/${customerId}/next-actions${
    interactionId ? `?interaction_id=${interactionId}` : ""}`);
}
export async function engageNextActionFeedback(
  customerId: string, body: { action_key: string; verdict: "accepted" | "rejected";
                              reason?: string | null; interaction_id?: string | null },
): Promise<{ id: string; action_key: string; verdict: string }> {
  return caseRequest(`/engage/profiles/${customerId}/next-actions/feedback`,
    { method: "POST", body: JSON.stringify(body) });
}
export interface RecapPoint { text: string; sources: { sid: string; kind: string; ref: string }[] }
export async function engageRecap(customerId: string, interactionId: string): Promise<{
  points: RecapPoint[]; sources: { sid: string; kind: string; ref: string }[];
  withheld: string[]; dropped: number; external_ai?: boolean; note?: string;
}> {
  return caseRequest(`/engage/profiles/${customerId}/recap?interaction_id=${interactionId}`,
    { method: "POST" });
}
export interface EngageAnalytics {
  window_days: number; repeat_window_days: number; since: string; truncated: boolean;
  interactions: number; by_channel: Record<string, number>; by_status: Record<string, number>;
  wrap_up_mix: Record<string, number>; aht_seconds: number | null;
  unresolved_rate: number | null; transfer_rate: number | null; abandoned_rate: number | null;
  repeat_contact_rate: number | null; first_contact_resolution: number | null;
  per_agent: { agent: string; interactions: number; aht_seconds: number | null }[];
  definitions: Record<string, string>;
}
export async function engageAnalytics(days = 30, repeatWindowDays = 7): Promise<EngageAnalytics> {
  return caseRequest(`/engage/analytics?days=${days}&repeat_window_days=${repeatWindowDays}`);
}
export type DispositionCode = { code: string; label: string };
export async function engageDispositionCodes(): Promise<{ codes: DispositionCode[] | null; free_text: boolean }> {
  return caseRequest("/engage/config/disposition-codes");
}
export async function engageSetDispositionCodes(
  codes: DispositionCode[] | null,
): Promise<{ codes: DispositionCode[] | null; free_text: boolean }> {
  return caseRequest("/engage/config/disposition-codes",
    { method: "PUT", body: JSON.stringify({ codes }) });
}
export async function engageCreateProfile(body: {
  display_name: string; party_kind?: string;
}): Promise<{ customer: { id: string; display_name: string; party_kind: string;
                          status: string } }> {
  return caseRequest("/engage/profiles",
    { method: "POST", body: JSON.stringify(body) });
}
export async function engageAddIdentifier(
  customerId: string, body: { kind: string; value: string; verified?: boolean;
                              is_primary?: boolean },
): Promise<{ identifier: { id: string; kind: string; verified: boolean; value: string } }> {
  return caseRequest(`/engage/profiles/${customerId}/identifiers`,
    { method: "POST", body: JSON.stringify(body) });
}

// ── Moving a case between tenants (mig 205; Studio screen = Oct item 6) ─────

export type TenantTransferState =
  "requested" | "approved" | "accepted" | "refused" | "cancelled";

export interface TenantTransfer {
  id: string; case_id: string; case_number: string | null;
  case_type_name: string | null; state: TenantTransferState; reason: string;
  source_tenant_id: string; destination_tenant_id: string;
  requested_at: string | null; approved_at: string | null;
  decided_at: string | null; completed_at: string | null;
  decision_note: string | null;
  /** What moved, closed, was severed — the tombstone, once accepted. */
  manifest: Record<string, unknown> | null;
  /** Source side only; the destination never sees source staff ids. */
  requested_by?: string | null; approved_by?: string | null;
  decided_by?: string | null; cancelled_by?: string | null;
  side?: "source" | "destination";
}

export interface TenantTransferPreflight {
  case_id: string; destination_tenant_id: string;
  can_move: boolean; blockers: string[]; open_transfer_id: string | null;
}

export async function preflightTenantTransfer(caseId: string, destination: string) {
  return caseRequest<TenantTransferPreflight>(
    `/cases/${caseId}/tenant-transfers/preflight?destination_tenant=${encodeURIComponent(destination)}`);
}

export async function requestTenantTransfer(caseId: string, destination: string, reason: string) {
  return caseRequest<TenantTransfer>(`/cases/${caseId}/tenant-transfers`, {
    method: "POST", body: JSON.stringify({ destination_tenant: destination, reason }),
  });
}

export async function listTenantTransfers(direction: "all" | "outgoing" | "incoming" = "all") {
  return caseRequest<{ items: TenantTransfer[] }>(`/case-tenant-transfers?direction=${direction}`);
}

export async function decideTenantTransfer(
  id: string, action: "approve" | "cancel" | "accept" | "refuse", note?: string,
) {
  const body = action === "accept" || action === "refuse" ? JSON.stringify({ note: note || null }) : undefined;
  return caseRequest<TenantTransfer>(`/case-tenant-transfers/${id}/${action}`, { method: "POST", body });
}

// ── Legal holds (mig 215; Oct item 13) ──────────────────────────────────────

export interface LegalHold {
  id: string; case_id: string; case_number: string | null; tenant_id: string | null;
  matter_ref: string; reason: string; status: "active" | "released";
  placed_by: string; placed_at: string | null;
  released_by: string | null; released_at: string | null; release_reason: string | null;
  // Release approval (mig 218). Absent on an older server = no approval.
  approval_required?: boolean;
  pending_release?: LegalHoldReleaseRequest | null;
  can_approve?: boolean; can_withdraw?: boolean;
}

export interface LegalHoldReleaseRequest {
  id: string; status: string; reason: string; requested_by: string; requested_at: string | null;
}

export interface LegalHoldSettings {
  available: boolean;
  approver_group_id: string | null;
  approver_group: { id: string; name: string; is_active: boolean; members: number } | null;
  approver_group_unusable: boolean;
  can_configure: boolean; can_change: boolean;
  groups: Array<{ id: string; name: string; is_active: boolean; members: number }>;
}

/** What anyone who can read the case is told. `holds` only with `legal.holds.read`. */
export interface CaseLegalHoldStatus {
  case_id: string; held: boolean; frozen: string[];
  can_read: boolean; can_place: boolean; can_release: boolean;
  holds?: LegalHold[]; other_active_holds?: number;
}

export interface PlaceLegalHoldResult {
  placed: number; requested: number;
  results: Array<{ case_id: string; ok: boolean; hold?: LegalHold; code?: string; error?: string }>;
}

export async function getCaseLegalHold(caseId: string) {
  return caseRequest<CaseLegalHoldStatus>(`/legal-holds/cases/${caseId}`);
}

export async function listLegalHolds(opts: { status?: "active" | "released"; matter?: string } = {}) {
  const q = new URLSearchParams({ limit: "500" });
  if (opts.status) q.set("status", opts.status);
  if (opts.matter) q.set("matter_ref", opts.matter);
  return caseRequest<{ holds: LegalHold[]; count: number; frozen: string[]; can_release: boolean }>(
    `/legal-holds?${q}`);
}

export async function placeLegalHold(caseIds: string[], matterRef: string, reason: string) {
  return caseRequest<PlaceLegalHoldResult>("/legal-holds", {
    method: "POST", body: JSON.stringify({ case_ids: caseIds, matter_ref: matterRef, reason }),
  });
}

export async function releaseLegalHold(holdId: string, reason: string) {
  // `pending: true` (HTTP 202): the tenant names an approving group, so this
  // asked for the release; the hold stands until a member approves.
  return caseRequest<{ hold: LegalHold; case_still_held: boolean; pending?: boolean }>(
    `/legal-holds/${holdId}/release`, { method: "POST", body: JSON.stringify({ reason }) });
}

export async function releaseLegalMatter(matterRef: string, reason: string) {
  return caseRequest<{ released: number; pending_approval?: number; matter_ref: string; case_ids: string[] }>(
    "/legal-holds/matters/release",
    { method: "POST", body: JSON.stringify({ matter_ref: matterRef, reason }) });
}

export async function approveLegalRelease(holdId: string, note?: string) {
  return caseRequest<{ hold: LegalHold; case_still_held: boolean }>(
    `/legal-holds/${holdId}/release/approve`,
    { method: "POST", body: JSON.stringify(note ? { note } : {}) });
}

/** Refuse (a member of the approving group) or withdraw (the person who asked). */
export async function rejectLegalRelease(holdId: string, note: string) {
  return caseRequest<{ hold: LegalHold }>(
    `/legal-holds/${holdId}/release/reject`, { method: "POST", body: JSON.stringify({ note }) });
}

export async function getLegalHoldSettings() {
  return caseRequest<LegalHoldSettings>("/legal-holds/settings");
}

export async function setLegalHoldApprover(groupId: string | null) {
  return caseRequest<LegalHoldSettings>("/legal-holds/settings", {
    method: "PUT", body: JSON.stringify({ approver_group_id: groupId }),
  });
}

// ── Oct item 18a — staged rollouts of case type versions ─────────────────────

export type RolloutStrategy = "immediate" | "flag" | "percentage" | "blue_green";

export interface ContentRollout {
  id: string;
  artifact_kind: string;
  artifact_name: string;
  baseline_id: string;
  candidate_id: string;
  baseline_version: string | null;
  candidate_version: string | null;
  strategy: RolloutStrategy;
  percent: number;
  state: "requested" | "dormant" | "rolling" | "promoted" | "rolled_back" | "superseded";
  open: boolean;
  source: string;
  requested_by: string | null;
  approved_by: string | null;
  closed_by: string | null;
  superseded_by: string | null;
  note: string | null;
  created_at: string | null;
  approved_at: string | null;
  closed_at: string | null;
  arms: { baseline: number; candidate: number };
  policy?: RolloutPolicy | null;
}

export interface RolloutPolicy {
  shadow: boolean;
  targeting: { include?: string[]; exclude?: string[]; field?: string; values?: string[] } | null;
  ramp: { steps: number[]; every_hours: number } | null;
  guard: { metric: string; max_delta: number; min_cases: number; action: "hold" | "rollback";
           require_significance: boolean } | null;
  guard_state: "ok" | "held" | "tripped";
  guard_detail: Record<string, unknown> | null;
  next_step_at: string | null;
}

export interface RolloutMetric {
  baseline: number | null; candidate: number | null; delta: number | null;
  p_value: number | null; verdict: string;
}
export interface RolloutOutcomes {
  arms: Record<"baseline" | "candidate", { cases: number; resolved: number; open: number;
                                           sla_breached: number; reopened: number }>;
  metrics: Record<string, RolloutMetric>;
  min_cases: number;
  note: string;
}

export interface ContentRolloutEvent {
  action: string; from_state: string | null; to_state: string | null;
  from_percent: number | null; to_percent: number | null;
  actor: string | null; reason: string | null; at: string | null;
}

export async function listRollouts(state: "open" | "closed" | "all" = "all"):
    Promise<{ rollouts: ContentRollout[]; count: number }> {
  return caseRequest(`/rollouts?state=${state}`);
}
export async function getRollout(id: string):
    Promise<{ rollout: ContentRollout; events: ContentRolloutEvent[] }> {
  return caseRequest(`/rollouts/${encodeURIComponent(id)}`);
}
export async function requestRollout(body: {
  baseline_id: string; candidate_id: string; strategy: RolloutStrategy;
  percent?: number; note?: string;
}): Promise<{ rollout: ContentRollout }> {
  return caseRequest("/rollouts", { method: "POST", body: JSON.stringify(body) });
}
export async function approveRollout(id: string, body: {
  strategy?: RolloutStrategy; percent?: number; reason?: string;
} = {}): Promise<{ rollout: ContentRollout }> {
  return caseRequest(`/rollouts/${encodeURIComponent(id)}/approve`,
    { method: "POST", body: JSON.stringify(body) });
}
export async function setRolloutPercent(id: string, percent: number, reason?: string):
    Promise<{ rollout: ContentRollout }> {
  return caseRequest(`/rollouts/${encodeURIComponent(id)}/percent`,
    { method: "PATCH", body: JSON.stringify({ percent, reason }) });
}
export async function closeRollout(id: string, how: "promote" | "rollback", reason?: string):
    Promise<{ rollout: ContentRollout }> {
  return caseRequest(`/rollouts/${encodeURIComponent(id)}/${how}`,
    { method: "POST", body: JSON.stringify({ reason }) });
}
export async function supersedeRollout(id: string, body: {
  candidate_id: string; strategy: RolloutStrategy; percent?: number; reason?: string;
}): Promise<{ rollout: ContentRollout }> {
  return caseRequest(`/rollouts/${encodeURIComponent(id)}/supersede`,
    { method: "POST", body: JSON.stringify(body) });
}

export async function setRolloutPolicy(id: string, body: {
  shadow?: boolean; targeting?: RolloutPolicy["targeting"]; ramp?: RolloutPolicy["ramp"];
  guard?: Partial<NonNullable<RolloutPolicy["guard"]>> | null;
}): Promise<{ rollout: ContentRollout }> {
  return caseRequest(`/rollouts/${encodeURIComponent(id)}/policy`,
    { method: "PUT", body: JSON.stringify(body) });
}
export async function rolloutOutcomes(id: string): Promise<RolloutOutcomes> {
  return caseRequest(`/rollouts/${encodeURIComponent(id)}/outcomes`);
}
export async function rolloutShadow(id: string): Promise<{
  evaluated: number; diverged: number; by_determinacy: Record<string, number>;
  examples: { case_id: string; determinacy: string; detail: Record<string, unknown> }[];
}> {
  return caseRequest(`/rollouts/${encodeURIComponent(id)}/shadow`);
}
export async function rebaseRollout(id: string, reason: string, dryRun = true): Promise<{
  dry_run: boolean; note: string;
  movable: { case_id: string; case_number: string | null }[];
  blocked: { case_id: string; case_number: string | null; problems: string[] }[];
}> {
  return caseRequest(`/rollouts/${encodeURIComponent(id)}/rebase`,
    { method: "POST", body: JSON.stringify({ reason, dry_run: dryRun }) });
}
export async function rolloutVersions(): Promise<{ versions: {
  case_type_id: string; name: string | null; version: string | null; status: string;
  rollout_id: string; new_cases_go_to: string | null }[] }> {
  return caseRequest("/rollouts/versions");
}

// ── Oct item 8f — a customer's subject-access export (GDPR Art. 15) ──────────

export interface CustomerExportRequest {
  emails?: string[]; phones?: string[]; portal_customer_ids?: string[];
  customer_ids?: string[]; include_internal?: boolean; summary?: boolean; reason: string;
}

/** With `summary`, the counts object; without, the export JSON as text. */
export async function customerExport(body: CustomerExportRequest):
    Promise<{ exists: boolean; counts: Record<string, number>;
              internal_notes_withheld: number } | string> {
  const resp = await fetch(`${CASE_BASE}/enterprise/gdpr/customer-export`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const b = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new ApiError(resp.status, detailText(b.detail) || resp.statusText, codedFrom(b));
  }
  return body.summary ? resp.json() : resp.text();
}
