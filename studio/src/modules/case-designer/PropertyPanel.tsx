import React, { useState, useEffect } from "react";
import type { StageDef, StepDef, AssignmentDef, SLAPolicyDef } from "./StagePipeline";
import { SLAPolicyTreeLink } from "./SLAPolicyTreeLink";
import { Button } from "@shared/components";
import { useApi } from "@shared/hooks";
import { listForms } from "@shared/api/client";

/* ═══════════════════════════════════════════════════════════════════
   PropertyPanel — context-sensitive editor for stages, steps, SLAs
   ═══════════════════════════════════════════════════════════════════ */

interface PropertyPanelProps {
  selectedStage: StageDef | null;
  selectedStep: StepDef | null;
  sla_policies: SLAPolicyDef[];
  readOnly?: boolean;
  onUpdateStage: (stage: StageDef) => void;
  onUpdateStep: (stageId: string, step: StepDef) => void;
  onUpdateSLAPolicies: (policies: SLAPolicyDef[]) => void;
  onClose: () => void;
}

export default function PropertyPanel({
  selectedStage,
  selectedStep,
  sla_policies,
  readOnly = false,
  onUpdateStage,
  onUpdateStep,
  onUpdateSLAPolicies,
  onClose,
}: PropertyPanelProps) {
  if (!selectedStage && !selectedStep) return null;

  return (
    <div style={{
      width: 340, borderLeft: "1px solid var(--border-subtle)",
      background: "var(--bg-panel)", overflow: "auto", display: "flex", flexDirection: "column",
      pointerEvents: readOnly ? "none" : "auto",
      opacity: readOnly ? 0.75 : 1,
    }}>
      {readOnly && (
        <div style={{ padding: "6px 12px", background: "#fef3c7", borderBottom: "1px solid #fde68a", fontSize: 11, color: "#d97706", fontFamily: "var(--font-mono)", fontWeight: 600 }}>
          VIEW ONLY
        </div>
      )}
      {/* Header */}
      <div style={{
        padding: "var(--space-lg)", borderBottom: "1px solid var(--border-subtle)",
        display: "flex", justifyContent: "space-between", alignItems: "center",
      }}>
        <div>
          <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
            {selectedStep ? "Step Properties" : "Stage Properties"}
          </div>
          <div style={{ fontSize: 16, fontWeight: 600, color: "var(--text-primary)", fontFamily: "var(--font-display)", marginTop: 4 }}>
            {selectedStep ? selectedStep.name : selectedStage?.name}
          </div>
        </div>
        <button onClick={onClose} style={{
          background: "transparent", border: "none", color: "var(--text-muted)",
          cursor: "pointer", fontSize: 16, padding: 4,
        }}>✕</button>
      </div>

      {/* Content */}
      <div style={{ flex: 1, overflow: "auto", padding: "var(--space-lg)" }}>
        {selectedStep && selectedStage ? (
          <StepPropertyEditor
            step={selectedStep}
            stageId={selectedStage.id}
            sla_policies={sla_policies}
            onUpdate={(step) => onUpdateStep(selectedStage.id, step)}
            onUpdateSLAPolicies={onUpdateSLAPolicies}
          />
        ) : selectedStage ? (
          <StagePropertyEditor
            stage={selectedStage}
            sla_policies={sla_policies}
            onUpdate={onUpdateStage}
            onUpdateSLAPolicies={onUpdateSLAPolicies}
          />
        ) : null}
      </div>
    </div>
  );
}

/* ── Stage Property Editor ────────────────────────────────────── */

function StagePropertyEditor({
  stage,
  sla_policies,
  onUpdate,
  onUpdateSLAPolicies,
}: {
  stage: StageDef;
  sla_policies: SLAPolicyDef[];
  onUpdate: (s: StageDef) => void;
  onUpdateSLAPolicies: (p: SLAPolicyDef[]) => void;
}) {
  const [local, setLocal] = useState<StageDef>(stage);

  useEffect(() => setLocal(stage), [stage.id]);

  const update = (partial: Partial<StageDef>) => {
    const next = { ...local, ...partial };
    setLocal(next);
    onUpdate(next);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-lg)" }}>
      <Section title="General">
        <Field label="Name">
          <Input value={local.name} onChange={(v) => update({ name: v })} />
        </Field>
        <Field label="Type">
          <Select value={local.stage_type} onChange={(v) => update({ stage_type: v })}
            options={["linear", "parallel", "conditional", "optional", "repeatable"]} />
        </Field>
        <Field label="Description">
          <TextArea value={local.description || ""} onChange={(v) => update({ description: v })}
            placeholder="What happens in this stage…" />
        </Field>
      </Section>

      <Section title="SLA Policy">
        <Field label="Assigned SLA">
          <Select
            value={local.sla_policy_id || ""}
            onChange={(v) => update({ sla_policy_id: v || undefined })}
            options={["", ...sla_policies.map((s) => s.id)]}
            labels={["None", ...sla_policies.map((s) => `${s.name} (${s.goal_duration})`)]}
          />
        </Field>
        {local.sla_policy_id && (() => {
          const policy = sla_policies.find((s) => s.id === local.sla_policy_id);
          if (!policy) return null;
          const updateTree = (treeId: string | null) => {
            onUpdateSLAPolicies(
              sla_policies.map((p) =>
                p.id === policy.id
                  ? { ...p, escalation_tree_id: treeId || undefined, use_v2: !!treeId }
                  : p
              )
            );
          };
          return (
            <SLAPolicyTreeLink
              value={(policy as any).escalation_tree_id || null}
              onChange={updateTree}
            />
          );
        })()}
        <InlineSLACreator
          onAdd={(sla) => {
            onUpdateSLAPolicies([...sla_policies, sla]);
            update({ sla_policy_id: sla.id });
          }}
        />
      </Section>

      <Section title="Criteria">
        <Field label="Entry Criteria (rule IDs)">
          <TagInput values={local.entry_criteria || []} onChange={(v) => update({ entry_criteria: v })} />
        </Field>
        <Field label="Exit Criteria (rule IDs)">
          <TagInput values={local.exit_criteria || []} onChange={(v) => update({ exit_criteria: v })} />
        </Field>
      </Section>

      <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: "var(--space-md)" }}>
        ID: {local.id} · Order: {local.order}
      </div>
    </div>
  );
}

/* ── Step Property Editor ─────────────────────────────────────── */

function StepPropertyEditor({
  step,
  stageId,
  sla_policies,
  onUpdate,
  onUpdateSLAPolicies,
}: {
  step: StepDef;
  stageId: string;
  sla_policies: SLAPolicyDef[];
  onUpdate: (s: StepDef) => void;
  onUpdateSLAPolicies: (p: SLAPolicyDef[]) => void;
}) {
  const [local, setLocal] = useState<StepDef>(step);

  useEffect(() => setLocal(step), [step.id]);

  const update = (partial: Partial<StepDef>) => {
    const next = { ...local, ...partial };
    setLocal(next);
    onUpdate(next);
  };

  const updateAssignment = (partial: Partial<AssignmentDef>) => {
    const current = local.assignment || { strategy: "queue_based" };
    update({ assignment: { ...current, ...partial } });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-lg)" }}>
      <Section title="General">
        <Field label="Name">
          <Input value={local.name} onChange={(v) => update({ name: v })} />
        </Field>
        <Field label="Type">
          <Select value={local.step_type} onChange={(v) => update({ step_type: v })}
            options={[
              // Human steps
              "user_task", "manual_task", "approval",
              // Automated steps
              "service_task", "subprocess", "script_task", "send_task",
              // Specialised automated
              "payment_request", "payment_disbursement",
              "identity_verify", "esign_request",
              "crm_sync", "invoice_generate",
              "sms_send", "slack_notify",
              "doc_extract", "doc_store",
            ]}
            labels={[
              "👤 User Task", "✋ Manual Task", "✅ Approval",
              "⚙️ Service Task (auto)", "📦 Subprocess (HxFusion)", "📜 Script Task (auto)", "📨 Send Task (notify)",
              "💳 Payment Request", "💸 Payment Disbursement",
              "🪪 Identity Verify (KYC)", "✍️ E-Sign Request",
              "☁️ CRM Sync", "🧾 Invoice Generate",
              "📱 SMS Send", "💬 Slack Notify",
              "🔍 Doc Extract (AI)", "📂 Doc Store",
            ]} />
        </Field>
        <Field label="BPMN Element ID">
          <Input value={local.bpmn_element_id} onChange={(v) => update({ bpmn_element_id: v })}
            placeholder="task_review_001" mono />
        </Field>
        <Field label="Description">
          <TextArea value={local.description || ""} onChange={(v) => update({ description: v })} />
        </Field>
        <Checkbox label="Required" checked={local.required !== false}
          onChange={(v) => update({ required: v })} />
      </Section>

      {/* Assignment — only for human-facing steps */}
      {["user_task", "manual_task", "approval"].includes(local.step_type) && (
      <Section title="Assignment">
        <Field label="Strategy">
          <Select value={local.assignment?.strategy || "queue_based"}
            onChange={(v) => updateAssignment({ strategy: v })}
            options={["specific_user", "role_based", "queue_based", "round_robin", "least_loaded", "skill_based", "rule_based", "manager_of", "self_service"]} />
        </Field>
        <Field label="Target">
          <Input value={local.assignment?.target || ""}
            onChange={(v) => updateAssignment({ target: v })}
            placeholder={assignmentPlaceholder(local.assignment?.strategy)} />
        </Field>
        <Field label="Fallback Strategy">
          <Select value={local.assignment?.fallback_strategy || ""}
            onChange={(v) => updateAssignment({ fallback_strategy: v || undefined })}
            options={["", "queue_based", "role_based", "round_robin", "least_loaded"]}
            labels={["None", "queue_based", "role_based", "round_robin", "least_loaded"]} />
        </Field>
      </Section>
      )}

      <Section title="SLA">
        <Field label="Assigned SLA">
          <Select
            value={local.sla_policy_id || ""}
            onChange={(v) => update({ sla_policy_id: v || undefined })}
            options={["", ...sla_policies.map((s) => s.id)]}
            labels={["None", ...sla_policies.map((s) => `${s.name} (${s.goal_duration})`)]}
          />
        </Field>
        {local.sla_policy_id && (() => {
          const policy = sla_policies.find((s) => s.id === local.sla_policy_id);
          if (!policy) return null;
          const updateTree = (treeId: string | null) => {
            onUpdateSLAPolicies(
              sla_policies.map((p) =>
                p.id === policy.id
                  ? { ...p, escalation_tree_id: treeId || undefined, use_v2: !!treeId }
                  : p
              )
            );
          };
          return (
            <SLAPolicyTreeLink
              value={(policy as any).escalation_tree_id || null}
              onChange={updateTree}
            />
          );
        })()}
      </Section>

      <FormPickerSection
        formId={local.form_id}
        onSelect={(v) => update({ form_id: v || undefined })}
      />

      {/* Connectivity panels — one per step type that has automation */}
      {["service_task", "connector_call", "send_task",
        "payment_request", "payment_disbursement",
        "identity_verify", "esign_request",
        "crm_sync", "invoice_generate",
        "sms_send", "slack_notify",
        "doc_extract", "doc_store",
      ].includes(local.step_type) && (
        <ConnectorCallSection
          config={(local as any).connector_config ?? {}}
          onChange={cfg => update({ connector_config: cfg } as any)}
          stepType={local.step_type}
        />
      )}
      {local.step_type === "subprocess" && (
        <SubprocessSection
          config={(local as any).subprocess_config ?? {}}
          onChange={cfg => update({ subprocess_config: cfg } as any)}
        />
      )}
      {local.step_type === "script_task" && (
        <ScriptTaskSection
          config={(local as any).script_config ?? {}}
          onChange={cfg => update({ script_config: cfg } as any)}
        />
      )}
      {local.step_type === "approval" && (
        <ApprovalSection
          config={(local as any).approval_config ?? {}}
          onChange={cfg => update({ approval_config: cfg } as any)}
        />
      )}

      <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: "var(--space-md)" }}>
        ID: {local.id} · Stage: {stageId}
      </div>
    </div>
  );
}

/* ── Connector Call Config ─────────────────────────────────────── */

const STEP_TYPE_TITLES: Record<string, string> = {
  service_task:          "Service Task — Connector",
  send_task:             "Send Task — Notification Connector",
  payment_request:       "Payment Request — Connector",
  payment_disbursement:  "Payment Disbursement — Connector",
  identity_verify:       "Identity Verify (KYC) — Connector",
  esign_request:         "E-Sign Request — Connector",
  crm_sync:              "CRM Sync — Connector",
  invoice_generate:      "Invoice Generate — Connector",
  sms_send:              "SMS Send — Connector",
  slack_notify:          "Slack Notify — Connector",
  doc_extract:           "Document Extract (AI) — Connector",
  doc_store:             "Document Store — Connector",
  connector_call:        "Connector Call",
};

function ConnectorCallSection({
  config, onChange, stepType = "service_task",
}: {
  config: { connector_id?: string; input_mapping?: string; output_mapping?: string; blocking?: boolean };
  onChange: (cfg: any) => void;
  stepType?: string;
}) {
  const [connectors, setConnectors] = useState<any[]>([]);

  useEffect(() => {
    const t = localStorage.getItem("helix_token");
    const hdr: Record<string, string> = t ? { Authorization: `Bearer ${t}` } : {};
    fetch("/api/v1/hxbridge/connectors?enabled=true", { headers: hdr })
      .then(r => r.ok ? r.json() : { connectors: [] })
      .then(d => setConnectors(d.connectors ?? []));
  }, []);

  const up = (partial: any) => onChange({ ...config, ...partial });

  return (
    <Section title={STEP_TYPE_TITLES[stepType] ?? "Connector"}>
      <Field label="Connector">
        <select
          value={config.connector_id ?? ""}
          onChange={e => up({ connector_id: e.target.value })}
          style={{ width: "100%", padding: "6px 8px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)" }}
        >
          <option value="">— select connector —</option>
          {connectors.map((c: any) => (
            <option key={c.id} value={c.id}>{c.name} ({c.connector_type})</option>
          ))}
        </select>
      </Field>
      <Field label='Input Mapping (JSON: {"connector_param": "case_field"})'>
        <textarea
          value={config.input_mapping ?? "{}"}
          onChange={e => up({ input_mapping: e.target.value })}
          rows={3}
          style={{ width: "100%", padding: "6px 8px", fontSize: 11, fontFamily: "monospace", background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", resize: "vertical" as const, boxSizing: "border-box" as const }}
        />
      </Field>
      <Field label='Output Mapping (JSON: {"response.dot.path": "case_field"})'>
        <textarea
          value={config.output_mapping ?? "{}"}
          onChange={e => up({ output_mapping: e.target.value })}
          rows={3}
          style={{ width: "100%", padding: "6px 8px", fontSize: 11, fontFamily: "monospace", background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", resize: "vertical" as const, boxSizing: "border-box" as const }}
        />
      </Field>
      <Checkbox
        label="Block case until connector responds"
        checked={config.blocking !== false}
        onChange={v => up({ blocking: v })}
      />
    </Section>
  );
}

/* ── Subprocess Config (links to HxFusion process definition) ────── */

function SubprocessSection({
  config, onChange,
}: {
  config: { process_definition_id?: string; context_mapping?: string };
  onChange: (cfg: any) => void;
}) {
  const [defs, setDefs] = useState<any[]>([]);

  useEffect(() => {
    const t = localStorage.getItem("helix_token");
    const hdr: Record<string, string> = t ? { Authorization: `Bearer ${t}` } : {};
    fetch("/api/v1/fusion/definitions?status=active", { headers: hdr })
      .then(r => r.ok ? r.json() : [])
      .then(d => setDefs(Array.isArray(d) ? d : []));
  }, []);

  const up = (partial: any) => onChange({ ...config, ...partial });

  return (
    <Section title="Subprocess — HxFusion Process">
      <Field label="Process Definition">
        <select
          value={config.process_definition_id ?? ""}
          onChange={e => up({ process_definition_id: e.target.value })}
          style={{ width: "100%", padding: "6px 8px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)" }}
        >
          <option value="">— select process definition —</option>
          {defs.map((d: any) => (
            <option key={d.id} value={d.id}>{d.name} v{d.version}</option>
          ))}
        </select>
      </Field>
      <Field label='Context Mapping (JSON: {"process_var": "case_field"})'>
        <textarea
          value={config.context_mapping ?? "{}"}
          onChange={e => up({ context_mapping: e.target.value })}
          rows={3}
          style={{ width: "100%", padding: "6px 8px", fontSize: 11, fontFamily: "monospace", background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", resize: "vertical" as const, boxSizing: "border-box" as const }}
        />
      </Field>
      <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
        When this step is activated, the selected process starts automatically and binds to this case.
      </div>
    </Section>
  );
}

/* ── Script Task Config ───────────────────────────────────────────── */

function ScriptTaskSection({
  config, onChange,
}: {
  config: { expression?: string; output_field?: string };
  onChange: (cfg: any) => void;
}) {
  const up = (partial: any) => onChange({ ...config, ...partial });

  return (
    <Section title="Script Task — Inline Expression">
      <Field label="Expression (Python — access case fields as variables)">
        <textarea
          value={config.expression ?? ""}
          onChange={e => up({ expression: e.target.value })}
          rows={4}
          placeholder={"# Example:\nresult = amount * 1.2 if priority == 'high' else amount"}
          style={{ width: "100%", padding: "6px 8px", fontSize: 11, fontFamily: "monospace", background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", resize: "vertical" as const, boxSizing: "border-box" as const }}
        />
      </Field>
      <Field label="Output — write result to case field">
        <input
          value={config.output_field ?? ""}
          onChange={e => up({ output_field: e.target.value })}
          placeholder="e.g. calculated_premium"
          style={{ width: "100%", padding: "6px 8px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", boxSizing: "border-box" as const }}
        />
      </Field>
    </Section>
  );
}

/* ── Approval Config ──────────────────────────────────────────────── */

function ApprovalSection({
  config, onChange,
}: {
  config: { approver_role?: string; approve_label?: string; reject_label?: string; on_approve_stage?: string; on_reject_stage?: string };
  onChange: (cfg: any) => void;
}) {
  const up = (partial: any) => onChange({ ...config, ...partial });

  return (
    <Section title="Approval Step">
      <Field label="Approver Role">
        <input
          value={config.approver_role ?? ""}
          onChange={e => up({ approver_role: e.target.value })}
          placeholder="e.g. senior_underwriter"
          style={{ width: "100%", padding: "6px 8px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", boxSizing: "border-box" as const }}
        />
      </Field>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <Field label="Approve Button Label">
          <input
            value={config.approve_label ?? "Approve"}
            onChange={e => up({ approve_label: e.target.value })}
            style={{ width: "100%", padding: "6px 8px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", boxSizing: "border-box" as const }}
          />
        </Field>
        <Field label="Reject Button Label">
          <input
            value={config.reject_label ?? "Reject"}
            onChange={e => up({ reject_label: e.target.value })}
            style={{ width: "100%", padding: "6px 8px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", boxSizing: "border-box" as const }}
          />
        </Field>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <Field label="On Approve → Stage ID">
          <input
            value={config.on_approve_stage ?? ""}
            onChange={e => up({ on_approve_stage: e.target.value })}
            placeholder="next stage id"
            style={{ width: "100%", padding: "6px 8px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", boxSizing: "border-box" as const }}
          />
        </Field>
        <Field label="On Reject → Stage ID">
          <input
            value={config.on_reject_stage ?? ""}
            onChange={e => up({ on_reject_stage: e.target.value })}
            placeholder="rejection stage id"
            style={{ width: "100%", padding: "6px 8px", fontSize: 12, background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", boxSizing: "border-box" as const }}
          />
        </Field>
      </div>
    </Section>
  );
}

/* ── Inline SLA Creator ───────────────────────────────────────── */

function InlineSLACreator({ onAdd }: { onAdd: (sla: SLAPolicyDef) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("PT4H");
  const [deadline, setDeadline] = useState("PT8H");

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} style={{
        padding: "6px", fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)",
        background: "transparent", border: "1px dashed var(--border-default)",
        borderRadius: "var(--radius-sm)", cursor: "pointer", width: "100%",
      }}>
        + Create SLA Policy
      </button>
    );
  }

  return (
    <div style={{
      padding: "var(--space-sm)", background: "var(--bg-input)",
      borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
    }}>
      <Field label="SLA Name">
        <Input value={name} onChange={setName} placeholder="e.g. Standard Response" />
      </Field>
      <Field label="Goal Duration (ISO 8601)">
        <Input value={goal} onChange={setGoal} placeholder="PT4H" mono />
      </Field>
      <Field label="Deadline Duration (ISO 8601)">
        <Input value={deadline} onChange={setDeadline} placeholder="PT8H" mono />
      </Field>
      <div style={{ display: "flex", gap: "var(--space-xs)", marginTop: "var(--space-sm)" }}>
        <Button size="sm" onClick={() => {
          if (!name) return;
          onAdd({ id: `sla-${name.toLowerCase().replace(/\s+/g, "-")}-${Date.now()}`, name, goal_duration: goal, deadline_duration: deadline });
          setOpen(false); setName(""); setGoal("PT4H"); setDeadline("PT8H");
        }}>Add</Button>
        <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
      </div>
    </div>
  );
}


/* ── Form Picker Section ───────────────────────────────────── */

function FormPickerSection({ formId, onSelect }: { formId?: string; onSelect: (id: string) => void }) {
  const { data: formsData } = useApi(listForms);
  const forms = formsData?.items ?? [];

  return (
    <Section title="Form">
      <Field label="Linked Form">
        <Select
          value={formId || ""}
          onChange={onSelect}
          options={["", ...forms.map((f: any) => f.id)]}
          labels={["None", ...forms.map((f: any) => `${f.name} v${f.version}`)]}
        />
      </Field>
      {formId && (
        <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 4 }}>
          Form ID: {formId}
        </div>
      )}
    </Section>
  );
}
/* ── Shared form primitives ───────────────────────────────────── */

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{
        fontSize: 10, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase",
        letterSpacing: "0.08em", fontFamily: "var(--font-mono)", marginBottom: "var(--space-sm)",
        paddingBottom: "var(--space-xs)", borderBottom: "1px solid var(--border-subtle)",
      }}>{title}</div>
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-sm)" }}>
        {children}
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label style={{
        display: "block", fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)",
        marginBottom: 3,
      }}>{label}</label>
      {children}
    </div>
  );
}

function Input({ value, onChange, placeholder, mono }: {
  value: string; onChange: (v: string) => void; placeholder?: string; mono?: boolean;
}) {
  return (
    <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} style={{
      width: "100%", padding: "6px 10px", fontSize: 12,
      fontFamily: mono ? "var(--font-mono)" : "var(--font-body)",
      background: "var(--bg-input)", border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-sm)", color: "var(--text-primary)",
      outline: "none", boxSizing: "border-box",
    }}
      onFocus={(e) => (e.target.style.borderColor = "var(--border-focus)")}
      onBlur={(e) => (e.target.style.borderColor = "var(--border-default)")}
    />
  );
}

function TextArea({ value, onChange, placeholder }: {
  value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  return (
    <textarea value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} rows={3} style={{
      width: "100%", padding: "6px 10px", fontSize: 12, fontFamily: "var(--font-body)",
      background: "var(--bg-input)", border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-sm)", color: "var(--text-primary)",
      outline: "none", resize: "vertical", boxSizing: "border-box",
    }} />
  );
}

function Select({ value, onChange, options, labels, placeholder }: {
  value: string; onChange: (v: string) => void; options: string[];
  labels?: string[]; placeholder?: string;
}) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} style={{
      width: "100%", padding: "6px 10px", fontSize: 12, fontFamily: "var(--font-body)",
      background: "var(--bg-input)", border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
      boxSizing: "border-box",
    }}>
      {placeholder && <option value="">{placeholder}</option>}
      {options.map((opt, i) => (
        <option key={opt} value={opt}>{labels?.[i] || opt.replace(/_/g, " ")}</option>
      ))}
    </select>
  );
}

function Checkbox({ label, checked, onChange }: {
  label: string; checked: boolean; onChange: (v: boolean) => void;
}) {
  return (
    <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 12, color: "var(--text-secondary)" }}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} style={{ accentColor: "var(--accent)" }} />
      {label}
    </label>
  );
}

function TagInput({ values, onChange }: { values: string[]; onChange: (v: string[]) => void }) {
  const [input, setInput] = useState("");

  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: values.length > 0 ? 6 : 0 }}>
        {values.map((v, i) => (
          <span key={i} style={{
            fontSize: 10, padding: "2px 6px", borderRadius: "var(--radius-sm)",
            background: "var(--bg-elevated)", color: "var(--text-secondary)", fontFamily: "var(--font-mono)",
            display: "inline-flex", alignItems: "center", gap: 4,
          }}>
            {v}
            <span style={{ cursor: "pointer", color: "var(--text-muted)" }}
              onClick={() => onChange(values.filter((_, j) => j !== i))}>×</span>
          </span>
        ))}
      </div>
      <input
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && input.trim()) {
            onChange([...values, input.trim()]);
            setInput("");
          }
        }}
        placeholder="Type and press Enter…"
        style={{
          width: "100%", padding: "4px 8px", fontSize: 11, fontFamily: "var(--font-mono)",
          background: "var(--bg-input)", border: "1px solid var(--border-default)",
          borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
          boxSizing: "border-box",
        }}
      />
    </div>
  );
}

function assignmentPlaceholder(strategy?: string): string {
  switch (strategy) {
    case "specific_user": return "user-id";
    case "role_based": return "role-name";
    case "queue_based": return "queue-id";
    case "skill_based": return "skill-name";
    case "rule_based": return "rule-id";
    default: return "target";
  }
}
