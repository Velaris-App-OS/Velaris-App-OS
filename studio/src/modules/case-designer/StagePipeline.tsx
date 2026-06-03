import React from "react";
import type { CasePriority } from "@shared/types";

/* ═══════════════════════════════════════════════════════════════════
   StagePipeline — horizontal stage/step visualization
   ═══════════════════════════════════════════════════════════════════ */

export interface StepDef {
  id: string;
  name: string;
  step_type: string;
  bpmn_element_id: string;
  description?: string;
  form_id?: string;
  sla_policy_id?: string;
  required?: boolean;
  assignment?: AssignmentDef | null;
}

export interface AssignmentDef {
  strategy: string;
  target?: string;
  fallback_strategy?: string;
  fallback_target?: string;
  skill_requirements?: string[];
}

export interface SLAPolicyDef {
  id: string;
  name: string;
  goal_duration: string;
  deadline_duration: string;
  at_risk_threshold?: number;
}

export interface StageDef {
  id: string;
  name: string;
  stage_type: string;
  order: number;
  description?: string;
  sla_policy_id?: string;
  steps: StepDef[];
  entry_criteria?: string[];
  exit_criteria?: string[];
}

interface StagePipelineProps {
  stages: StageDef[];
  sla_policies: SLAPolicyDef[];
  selectedStageId: string | null;
  selectedStepId: string | null;
  readOnly?: boolean;
  onSelectStage: (id: string | null) => void;
  onSelectStep: (stageId: string, stepId: string) => void;
  onAddStage: () => void;
  onAddStep: (stageId: string) => void;
  onReorderStage: (stageId: string, direction: "left" | "right") => void;
  onDeleteStage: (stageId: string) => void;
  onDeleteStep: (stageId: string, stepId: string) => void;
}

const STAGE_TYPE_COLORS: Record<string, string> = {
  linear: "var(--accent)",
  parallel: "var(--status-running)",
  conditional: "#a855f7",
  optional: "var(--text-muted)",
  repeatable: "#06b6d4",
};

const STEP_TYPE_ICONS: Record<string, string> = {
  user_task: "👤",
  service_task: "⚙️",
  script_task: "📜",
  send_task: "📨",
  manual_task: "✋",
  subprocess: "📦",
  call_activity: "📞",
  approval: "✅",
  // HxConnect
  payment_request: "💳",
  payment_disbursement: "💸",
  identity_verify: "🪪",
  esign_request: "✍️",
  crm_sync: "☁️",
  invoice_generate: "🧾",
  sms_send: "📱",
  slack_notify: "💬",
  doc_extract: "🔍",
  doc_store: "📂",
};

export default function StagePipeline({
  stages,
  sla_policies,
  selectedStageId,
  selectedStepId,
  readOnly = false,
  onSelectStage,
  onSelectStep,
  onAddStage,
  onAddStep,
  onReorderStage,
  onDeleteStage,
  onDeleteStep,
}: StagePipelineProps) {
  const sorted = [...stages].sort((a, b) => a.order - b.order);

  return (
    <div style={{ padding: "var(--space-lg) 0" }}>
      {/* Pipeline container */}
      <div style={{
        display: "flex", alignItems: "flex-start", gap: 0,
        overflowX: "auto", paddingBottom: "var(--space-md)",
      }}>
        {/* Start node */}
        <div style={{ display: "flex", alignItems: "center", flexShrink: 0 }}>
          <div style={{
            width: 36, height: 36, borderRadius: "50%",
            border: "2px solid var(--accent)", background: "var(--accent-dim)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>
            <div style={{ width: 10, height: 10, borderRadius: "50%", background: "var(--accent)" }} />
          </div>
          <Connector />
        </div>

        {/* Stages */}
        {sorted.map((stage, idx) => {
          const isSelected = selectedStageId === stage.id;
          const stageColor = STAGE_TYPE_COLORS[stage.stage_type] || "var(--accent)";
          const sla = sla_policies.find(s => s.id === stage.sla_policy_id);

          return (
            <React.Fragment key={stage.id}>
              <div style={{ flexShrink: 0, position: "relative" }}>
                {/* Stage card */}
                <div
                  onClick={() => onSelectStage(stage.id)}
                  style={{
                    minWidth: 200, maxWidth: 280,
                    background: isSelected ? "var(--bg-card-hover)" : "var(--bg-card)",
                    border: `1.5px solid ${isSelected ? stageColor : "var(--border-default)"}`,
                    borderRadius: "var(--radius-md)",
                    cursor: "pointer", transition: "all 0.15s",
                    overflow: "hidden",
                  }}
                >
                  {/* Stage header */}
                  <div style={{
                    padding: "10px 14px",
                    borderBottom: `1px solid ${isSelected ? `color-mix(in srgb, ${stageColor} 30%, transparent)` : "var(--border-subtle)"}`,
                    background: isSelected ? `color-mix(in srgb, ${stageColor} 8%, transparent)` : "transparent",
                  }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
                        {stage.name}
                      </span>
                      <span style={{
                        fontSize: 9, padding: "1px 6px", borderRadius: 100,
                        color: stageColor, fontFamily: "var(--font-mono)",
                        textTransform: "uppercase", letterSpacing: "0.06em",
                        background: `color-mix(in srgb, ${stageColor} 12%, transparent)`,
                        border: `1px solid color-mix(in srgb, ${stageColor} 25%, transparent)`,
                      }}>
                        {stage.stage_type}
                      </span>
                    </div>
                    {sla && (
                      <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: 4 }}>
                        ⏱ {sla.goal_duration} / {sla.deadline_duration}
                      </div>
                    )}
                  </div>

                  {/* Steps */}
                  <div style={{ padding: "8px 10px" }}>
                    {stage.steps.length === 0 && (
                      <div style={{
                        padding: "12px", fontSize: 11,
                        color: "var(--text-muted)", fontStyle: "italic",
                      }}>
                        No steps yet
                      </div>
                    )}
                    {stage.steps.map((step) => {
                      const isStepSelected = selectedStepId === step.id && selectedStageId === stage.id;
                      return (
                        <div
                          key={step.id}
                          onClick={(e) => { e.stopPropagation(); onSelectStep(stage.id, step.id); }}
                          style={{
                            display: "flex", alignItems: "center", gap: 8,
                            padding: "6px 8px", marginBottom: 4, borderRadius: "var(--radius-sm)",
                            background: isStepSelected ? "var(--accent-dim)" : "transparent",
                            border: isStepSelected ? "1px solid var(--accent)" : "1px solid transparent",
                            cursor: "pointer", transition: "all 0.1s",
                          }}
                          onMouseEnter={(e) => !isStepSelected && (e.currentTarget.style.background = "var(--bg-elevated)")}
                          onMouseLeave={(e) => !isStepSelected && (e.currentTarget.style.background = "transparent")}
                        >
                          <span style={{ fontSize: 14, flexShrink: 0 }}>
                            {STEP_TYPE_ICONS[step.step_type] || "📋"}
                          </span>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{
                              fontSize: 12, fontWeight: 500,
                              color: isStepSelected ? "var(--accent)" : "var(--text-primary)",
                              whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                            }}>
                              {step.name}
                            </div>
                            <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                              {step.step_type.replace("_", " ")}
                              {step.assignment && ` · ${step.assignment.strategy.replace("_", " ")}`}
                            </div>
                          </div>
                          {!step.required && (
                            <span style={{ fontSize: 9, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>OPT</span>
                          )}
                          {/* Delete step */}
                          {!readOnly && <span
                            onClick={(e) => { e.stopPropagation(); onDeleteStep(stage.id, step.id); }}
                            style={{ fontSize: 12, color: "var(--text-muted)", cursor: "pointer", opacity: 0.4, padding: "0 2px" }}
                            onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
                            onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.4")}
                          >×</span>}
                        </div>
                      );
                    })}

                    {/* Add step button */}
                    {!readOnly && <button
                      onClick={(e) => { e.stopPropagation(); onAddStep(stage.id); }}
                      style={{
                        width: "100%", padding: "6px", marginTop: 4,
                        fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)",
                        background: "transparent", border: "1px dashed var(--border-default)",
                        borderRadius: "var(--radius-sm)", cursor: "pointer",
                        transition: "all 0.1s",
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--accent)"; e.currentTarget.style.color = "var(--accent)"; }}
                      onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--border-default)"; e.currentTarget.style.color = "var(--text-muted)"; }}
                    >
                      + Add Step
                    </button>}
                  </div>

                  {/* Stage actions (visible on hover/select) */}
                  {isSelected && !readOnly && (
                    <div style={{
                      display: "flex", justifyContent: "center", gap: "var(--space-xs)",
                      padding: "6px 10px", borderTop: "1px solid var(--border-subtle)",
                    }}>
                      {idx > 0 && (
                        <MiniBtn label="←" onClick={(e) => { e.stopPropagation(); onReorderStage(stage.id, "left"); }} />
                      )}
                      {idx < sorted.length - 1 && (
                        <MiniBtn label="→" onClick={(e) => { e.stopPropagation(); onReorderStage(stage.id, "right"); }} />
                      )}
                      <MiniBtn label="✕" danger onClick={(e) => { e.stopPropagation(); onDeleteStage(stage.id); }} />
                    </div>
                  )}
                </div>
              </div>

              {/* Connector between stages */}
              {idx < sorted.length - 1 && <Connector />}
            </React.Fragment>
          );
        })}

        {/* Add stage button */}
        {!readOnly && <div style={{ display: "flex", alignItems: "center", flexShrink: 0 }}>
          <Connector />
          <button
            onClick={onAddStage}
            style={{
              width: 44, height: 44, borderRadius: "var(--radius-md)",
              border: "2px dashed var(--border-default)", background: "transparent",
              cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
              color: "var(--text-muted)", fontSize: 20, transition: "all 0.15s",
            }}
            onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--accent)"; e.currentTarget.style.color = "var(--accent)"; }}
            onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--border-default)"; e.currentTarget.style.color = "var(--text-muted)"; }}
          >
            +
          </button>
          <Connector />
        </div>}

        {/* End node */}
        <div style={{ display: "flex", alignItems: "center", flexShrink: 0 }}>
          <div style={{
            width: 36, height: 36, borderRadius: "50%",
            border: "3px solid var(--status-failed)", background: "color-mix(in srgb, var(--status-failed) 10%, transparent)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>
            <div style={{ width: 12, height: 12, borderRadius: "50%", background: "var(--status-failed)" }} />
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── Connector arrow between stages ───────────────────────────── */

function Connector() {
  return (
    <div style={{ display: "flex", alignItems: "center", flexShrink: 0, padding: "0 2px" }}>
      <div style={{ width: 24, height: 2, background: "var(--border-strong)" }} />
      <div style={{
        width: 0, height: 0,
        borderTop: "5px solid transparent", borderBottom: "5px solid transparent",
        borderLeft: "6px solid var(--border-strong)",
      }} />
    </div>
  );
}

/* ── Mini button for stage actions ────────────────────────────── */

function MiniBtn({ label, onClick, danger }: { label: string; onClick: (e: React.MouseEvent) => void; danger?: boolean }) {
  return (
    <button onClick={onClick} style={{
      width: 24, height: 24, fontSize: 12, borderRadius: "var(--radius-sm)",
      border: "1px solid var(--border-subtle)", background: "transparent",
      color: danger ? "var(--status-failed)" : "var(--text-muted)",
      cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center",
    }}>{label}</button>
  );
}
