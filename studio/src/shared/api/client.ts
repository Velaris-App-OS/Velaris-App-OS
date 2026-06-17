/* ═══════════════════════════════════════════════════════════════════
   API Client — typed wrapper around the Helix Engine REST API

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

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...authHeaders(), ...options?.headers },
    ...options,
  });

  if (res.status === 401) {
    const newToken = await attemptRefresh();
    if (newToken) {
      const retry = await fetch(`${BASE}${path}`, {
        headers: { "Content-Type": "application/json", ...authHeaders(newToken), ...options?.headers },
        ...options,
      });
      if (retry.status === 401) {
        window.dispatchEvent(new Event("velaris:unauthorized"));
        throw new ApiError(401, "Session expired. Please log in again.");
      }
      if (!retry.ok) {
        const body = await retry.json().catch(() => ({ detail: retry.statusText }));
        throw new ApiError(retry.status, typeof body.detail === "object" ? JSON.stringify(body.detail) : (body.detail || retry.statusText));
      }
      return retry.json();
    }
    window.dispatchEvent(new Event("velaris:unauthorized"));
    throw new ApiError(401, "Session expired. Please log in again.");
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, typeof body.detail === 'object' ? JSON.stringify(body.detail) : (body.detail || res.statusText));
  }

  return res.json();
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
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

async function caseRequest<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${CASE_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...authHeaders(), ...options?.headers },
    ...options,
  });

  if (res.status === 401) {
    const newToken = await attemptRefresh();
    if (newToken) {
      const retry = await fetch(`${CASE_BASE}${path}`, {
        headers: { "Content-Type": "application/json", ...authHeaders(newToken), ...options?.headers },
        ...options,
      });
      if (retry.status === 401) {
        window.dispatchEvent(new Event("velaris:unauthorized"));
        throw new ApiError(401, "Session expired. Please log in again.");
      }
      if (!retry.ok) {
        const body = await retry.json().catch(() => ({ detail: retry.statusText }));
        throw new ApiError(retry.status, body.detail || retry.statusText);
      }
      return retry.json();
    }
    window.dispatchEvent(new Event("velaris:unauthorized"));
    throw new ApiError(401, "Session expired. Please log in again.");
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail || res.statusText);
  }

  return res.json();
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

export async function changeCaseStatus(
  id: string,
  status: string,
  reason?: string
): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/status`, {
    method: "POST",
    body: JSON.stringify({ status, reason }),
  });
}

export async function changeCasePriority(
  id: string,
  priority: string
): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/priority`, {
    method: "POST",
    body: JSON.stringify({ priority }),
  });
}

export async function resolveCase(
  id: string,
  resolution?: Record<string, unknown>
): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/resolve`, {
    method: "POST",
    body: JSON.stringify({ resolution }),
  });
}

export async function closeCase(id: string): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/close`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function reopenCase(id: string): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/reopen`, { method: "POST", body: JSON.stringify({}) });
}

export async function cancelCase(id: string, reason?: string): Promise<CaseSummary> {
  return caseRequest(`/cases/${id}/cancel`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
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
): Promise<{ auto_advanced: boolean; status: string; [k: string]: unknown }> {
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
  { user_id: string; relation: string; created_by: string | null; created_at: string | null }[]
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

export async function listRules(): Promise<any> {
  return caseRequest("/rules");
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
  const res = await fetch(`${CASE_BASE}${path}`, { ...options, headers });
  if (res.status === 401) {
    const newToken = await attemptRefresh();
    if (newToken) {
      const retry = await fetch(`${CASE_BASE}${path}`, {
        ...options,
        headers: { ...headers, ...authHeaders(newToken) },
      });
      return retry;
    }
    window.dispatchEvent(new Event("velaris:unauthorized"));
  }
  return res;
}
