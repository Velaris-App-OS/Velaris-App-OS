/* ═══════════════════════════════════════════════════════════════════
   API Types — mirrors engine/helix_engine/api/schemas/process.py
   ═══════════════════════════════════════════════════════════════════ */

export type ProcessStatus = "active" | "suspended" | "deprecated";
export type InstanceStatus = "running" | "completed" | "failed" | "cancelled" | "suspended";

export interface ProcessSummary {
  process_id: string;
  version: number;
  name: string | null;
  status: ProcessStatus;
  element_count: number;
  flow_count: number;
  tags: Record<string, string>;
  deployed_at: string;
  bpmn_xml?: string | null;
}

export interface DeployResponse {
  process_id: string;
  version: number;
  name: string | null;
  status: ProcessStatus;
  element_count: number;
  flow_count: number;
  warnings: string[];
  deployed_at: string;
  bpmn_xml?: string | null;
}

export interface InstanceSummary {
  instance_id: string;
  process_id: string;
  version: number;
  status: InstanceStatus;
  business_key: string | null;
  started_at: string;
  completed_at: string | null;
}

export interface InstanceDetail {
  instance_id: string;
  process_id: string;
  version: number;
  status: InstanceStatus;
  business_key: string | null;
  variables: Record<string, unknown>;
  visited_elements: string[];
  error: string | null;
  started_at: string;
  completed_at: string | null;
}

export interface ProcessListResponse {
  processes: ProcessSummary[];
  total: number;
}

export interface InstanceListResponse {
  instances: InstanceSummary[];
  total: number;
}

/* ═══════════════════════════════════════════════════════════════════
   BPMN Modeler Types
   ═══════════════════════════════════════════════════════════════════ */

export type BpmnElementType =
  | "startEvent"
  | "endEvent"
  | "userTask"
  | "serviceTask"
  | "scriptTask"
  | "sendTask"
  | "exclusiveGateway"
  | "parallelGateway"
  | "inclusiveGateway";

export interface BpmnElement {
  id: string;
  type: BpmnElementType;
  name: string;
  x: number;
  y: number;
  width: number;
  height: number;
  properties: Record<string, string>;
}

export interface BpmnConnection {
  id: string;
  sourceId: string;
  targetId: string;
  name?: string;
  condition?: string;
}

export interface BpmnProcess {
  id: string;
  name: string;
  elements: BpmnElement[];
  connections: BpmnConnection[];
}

/* ═══════════════════════════════════════════════════════════════════
   Case Management Types — mirrors case-service API schemas
   ═══════════════════════════════════════════════════════════════════ */

export type CaseStatus =
  | "new"
  | "open"
  | "pending_external"
  | "pending_subcase"
  | "resolved"
  | "closed"
  | "reopened"
  | "cancelled";

export type CasePriority = "low" | "medium" | "high" | "critical" | "blocker";

export type SLAStatusType = "on_track" | "at_risk" | "breached" | "paused";

export interface CaseTypeSummary {
  id: string;
  name: string;
  version: string;
  lifecycle_process_id: string;
  data_model_id: string | null;
  default_priority: CasePriority;
  description: string;
  tags: string[];
  icon: string | null;
  color: string | null;
  created_at: string;
  updated_at: string;
}

export interface CaseTypeListResponse {
  items: CaseTypeSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface CaseSummary {
  id: string;
  case_type_id: string;
  case_type_version: string;
  process_instance_id: string | null;
  status: CaseStatus;
  priority: CasePriority;
  urgency_score: number;
  current_stage_id: string | null;
  parent_case_id: string | null;
  data: Record<string, unknown>;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  closed_at: string | null;
}

export interface CaseListResponse {
  items: CaseSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface CaseAssignment {
  id: string;
  case_id: string;
  step_id: string;
  assignee_type: string;
  assignee_id: string;
  status: string;
  assigned_at: string;
  due_at: string | null;
  claimed_at: string | null;
  completed_at: string | null;
  assigned_by: string | null;
}

export interface CaseAuditEntry {
  id: string;
  case_id: string;
  action: string;
  actor_id: string | null;
  actor_type: string;
  timestamp: string;
  details: Record<string, unknown>;
  previous_value: Record<string, unknown> | null;
  new_value: Record<string, unknown> | null;
}

export interface CaseRelationship {
  id: string;
  source_case_id: string;
  target_case_id: string;
  relationship_type: string;
  propagate_status: boolean;
  propagate_priority: boolean;
  required: boolean;
  created_at: string;
}

export interface WorkQueueSummary {
  id: string;
  name: string;
  description: string;
  filter_criteria: Record<string, unknown>;
  sort_fields: string[];
  sort_ascending: boolean;
  visible_to_roles: string[];
  auto_assignment: boolean;
  urgency_formula: string | null;
  max_items: number | null;
  created_at: string;
  updated_at: string;
}

export interface QueueStats {
  queue_id: string;
  total_items: number;
  active_items: number;
  avg_wait_seconds: number | null;
  sla_on_track: number;
  sla_at_risk: number;
  sla_breached: number;
}

export interface SLAStatusInfo {
  id: string;
  case_id: string;
  sla_policy_id: string;
  target_id: string;
  status: SLAStatusType;
  started_at: string;
  goal_at: string;
  deadline_at: string;
  paused_duration_seconds: number;
  breached_at: string | null;
}
