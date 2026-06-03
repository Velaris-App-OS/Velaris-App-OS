import React, { useState } from "react";
import { useApi } from "@shared/hooks";
import { getNLPStatus, generateCaseTypeFromText, generateFullCaseType } from "@shared/api/client";
import { Card, Button, Spinner } from "@shared/components";
import { useNavigate } from "react-router-dom";

/* ═══════════════════════════════════════════════════════════════════
   NLP Builder — Describe a process in plain English, get a case type.

   Quick mode  → structure only (stages + steps)
   Full mode   → complete application shell:
                 stages · form fields · SLA policies · data model · notifications
   ═══════════════════════════════════════════════════════════════════ */

const EXAMPLES = [
  "An insurance claim process: customer submits claim, then it goes to intake, then an adjuster reviews it, then a manager approves or rejects it, finally payment is issued.",
  "A simple approval workflow for expense reports: submit, review, approve, notify submitter.",
  "An employee onboarding process with application, background check, paperwork, equipment setup, and welcome email.",
  "A customer complaint handling process: receive complaint, investigate, propose resolution, get customer acceptance, close case.",
  "A loan application process with application intake, document collection, credit check, underwriting, risk assessment, decision, agreement signing, and disbursement.",
];

type Mode = "quick" | "full";

const STEP_TYPE_COLOR: Record<string, string> = {
  user_task:    "#3b82f6",
  system_task:  "#0d9488",
  approval:     "#f59e0b",
  decision:     "#9333ea",
  notification: "#22c55e",
};

const FIELD_TYPE_ICON: Record<string, string> = {
  text: "T", number: "#", date: "📅", select: "▾",
  boolean: "✓", textarea: "¶", email: "@", phone: "☎", file: "📎",
};

export default function NLPBuilder() {
  const navigate = useNavigate();
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState<Mode>("quick");
  const [result, setResult] = useState<any>(null);
  const [editedName, setEditedName] = useState<string>("");
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deploying, setDeploying] = useState(false);
  const [expandedStage, setExpandedStage] = useState<string | null>(null);
  const [expandedStep, setExpandedStep] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"stages" | "sla" | "variables" | "notifications">("stages");

  const { data: status } = useApi(getNLPStatus);

  const handleGenerate = async () => {
    if (!description.trim()) return;
    setGenerating(true); setError(null); setResult(null);
    setExpandedStage(null); setExpandedStep(null); setActiveTab("stages");
    try {
      const fn = mode === "full" ? generateFullCaseType : generateCaseTypeFromText;
      const r = await fn(description, false);
      setResult(r);
      setEditedName(r.name ?? "");
    } catch (e: any) {
      setError(e.message || "Generation failed");
    } finally {
      setGenerating(false);
    }
  };

  const handleDeploy = async () => {
    if (!result) return;
    setDeploying(true); setError(null);
    try {
      const fn = mode === "full" ? generateFullCaseType : generateCaseTypeFromText;
      const nameOverride = editedName.trim() !== result?.name ? editedName.trim() : undefined;
      const r = await fn(description, true, nameOverride);
      if (r.deployed_case_type_id) {
        navigate(`/case-designer?id=${r.deployed_case_type_id}`);
      } else {
        setError("Deploy succeeded but no case type ID was returned.");
      }
    } catch (e: any) {
      setError(e.message || "Deploy failed");
    } finally {
      setDeploying(false);
    }
  };

  const isFullResult = result && (result.variables !== undefined || result.notifications !== undefined);

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>

      {/* Status bar */}
      {status && (
        <div style={{
          padding: "var(--space-sm) var(--space-md)",
          background: status.ollama_available ? "var(--accent-dim)" : "var(--bg-card)",
          border: `1px solid ${status.ollama_available ? "var(--accent)" : "var(--border-subtle)"}`,
          borderRadius: "var(--radius-sm)", marginBottom: "var(--space-lg)",
          fontSize: 12, display: "flex", alignItems: "center", gap: "var(--space-sm)",
        }}>
          <span style={{
            width: 8, height: 8, borderRadius: "50%",
            background: status.ollama_available ? "var(--status-completed)" : "var(--status-cancelled)",
          }} />
          <span style={{ color: "var(--text-secondary)" }}>
            {status.ollama_available
              ? `AI connected — ${status.ai_backend ?? "ollama"} (${status.ollama_model})`
              : "AI offline — using heuristic fallback"}
          </span>
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1.4fr", gap: "var(--space-lg)" }}>

        {/* ── Left: input ───────────────────────────────────────────── */}
        <div>
          <Card>
            {/* Mode toggle */}
            <div style={{ display: "flex", gap: 6, marginBottom: "var(--space-md)" }}>
              {(["quick", "full"] as Mode[]).map(m => (
                <button
                  key={m}
                  onClick={() => { setMode(m); setResult(null); }}
                  style={{
                    padding: "5px 16px", border: "1px solid var(--border-default)",
                    borderRadius: "var(--radius-sm)", cursor: "pointer", fontSize: 12,
                    fontWeight: 600, fontFamily: "var(--font-mono)",
                    background: mode === m ? "var(--accent)" : "var(--bg-elevated)",
                    color: mode === m ? "#fff" : "var(--text-secondary)",
                    borderColor: mode === m ? "var(--accent)" : "var(--border-default)",
                  }}
                >
                  {m === "quick" ? "Quick — Structure only" : "Full — Forms + SLAs + Notifications"}
                </button>
              ))}
            </div>

            {mode === "full" && (
              <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: "var(--space-md)",
                padding: "8px 12px", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)",
                borderLeft: "3px solid var(--accent)" }}>
                <strong>Full mode</strong> generates a complete application shell: every stage, every form field,
                SLA deadlines, data model, and notification triggers — ready to deploy.
              </div>
            )}

            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)",
              fontFamily: "var(--font-display)", marginBottom: "var(--space-sm)" }}>
              Describe Your Process
            </div>
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder="e.g. A vacation request process: employee submits request with dates, manager approves or rejects, HR is notified of approval..."
              rows={10}
              style={{
                width: "100%", padding: "10px 14px", fontSize: 13, fontFamily: "var(--font-body)",
                background: "var(--bg-input)", border: "1px solid var(--border-default)",
                borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
                boxSizing: "border-box", resize: "vertical",
              }}
            />
            <div style={{ display: "flex", gap: "var(--space-sm)", marginTop: "var(--space-md)" }}>
              <Button onClick={handleGenerate} disabled={!description.trim() || generating}>
                {generating
                  ? (mode === "full" ? "Building full case type…" : "Generating…")
                  : (mode === "full" ? "Build Full Case Type" : "Generate Case Type")}
              </Button>
              <Button variant="ghost" onClick={() => { setDescription(""); setResult(null); }}>Clear</Button>
            </div>
            {error && (
              <div style={{ marginTop: "var(--space-md)", fontSize: 12, color: "var(--status-failed)" }}>
                {error}
              </div>
            )}
          </Card>

          {/* Examples */}
          <Card style={{ marginTop: "var(--space-md)" }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)",
              fontFamily: "var(--font-mono)", textTransform: "uppercase", marginBottom: "var(--space-sm)" }}>
              Try These Examples
            </div>
            {EXAMPLES.map((ex, i) => (
              <div
                key={i}
                onClick={() => setDescription(ex)}
                style={{
                  padding: "10px 12px", marginBottom: "var(--space-xs)",
                  background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)",
                  cursor: "pointer", fontSize: 12, color: "var(--text-secondary)",
                  border: "1px solid transparent",
                }}
                onMouseEnter={e => {
                  e.currentTarget.style.borderColor = "var(--accent)";
                  e.currentTarget.style.color = "var(--text-primary)";
                }}
                onMouseLeave={e => {
                  e.currentTarget.style.borderColor = "transparent";
                  e.currentTarget.style.color = "var(--text-secondary)";
                }}
              >
                {ex}
              </div>
            ))}
          </Card>
        </div>

        {/* ── Right: result ─────────────────────────────────────────── */}
        <div>
          {generating && (
            <Card style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: 300 }}>
              <div style={{  }}>
                <Spinner size={32} />
                <div style={{ marginTop: "var(--space-md)", fontSize: 12, color: "var(--text-muted)" }}>
                  {mode === "full"
                    ? "Building complete case type…"
                    : (status?.ollama_available ? "Thinking..." : "Parsing...")}
                </div>
              </div>
            </Card>
          )}

          {!generating && !result && (
            <Card style={{ minHeight: 300, display: "flex", alignItems: "center", justifyContent: "center",
              border: "2px dashed var(--border-subtle)", background: "transparent" }}>
              <div style={{ color: "var(--text-muted)", fontSize: 13 }}>
                {mode === "full"
                  ? "Full case type — stages, forms, SLAs, notifications — will appear here"
                  : "Generated case type structure will appear here"}
              </div>
            </Card>
          )}

          {result && !generating && (
            <Card>
              {/* Result header */}
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "var(--space-md)" }}>
                <div>
                  <input
                    value={editedName}
                    onChange={e => setEditedName(e.target.value)}
                    style={{
                      fontSize: 18, fontWeight: 600, color: "var(--text-primary)",
                      background: "var(--bg-input)", border: "1px solid var(--border-default)",
                      borderRadius: "var(--radius-sm)", padding: "4px 8px", width: "100%",
                      fontFamily: "var(--font-body)", outline: "none",
                    }}
                    placeholder="Case type name"
                    title="Edit case type name before deploying"
                  />
                  <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 2 }}>
                    source: {result.source} · priority: {result.default_priority} · {result.stages?.length ?? 0} stages
                  </div>
                  {result.description && (
                    <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 6 }}>
                      {result.description}
                    </div>
                  )}
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  <span style={{
                    fontSize: 10, padding: "3px 8px", borderRadius: "var(--radius-sm)",
                    background: result.source === "llm" ? "var(--accent-dim)" : "var(--bg-elevated)",
                    color: result.source === "llm" ? "var(--accent)" : "var(--text-muted)",
                    fontFamily: "var(--font-mono)", textTransform: "uppercase",
                  }}>
                    {result.source === "llm" ? "AI" : "Heuristic"}
                  </span>
                  {isFullResult && (
                    <span style={{
                      fontSize: 10, padding: "3px 8px", borderRadius: "var(--radius-sm)",
                      background: "#d1fae522", color: "#059669",
                      fontFamily: "var(--font-mono)", textTransform: "uppercase",
                      border: "1px solid #a7f3d0",
                    }}>
                      Full
                    </span>
                  )}
                </div>
              </div>

              {/* Tab bar — only shown in full mode results */}
              {isFullResult && (
                <div style={{ display: "flex", gap: 4, marginBottom: "var(--space-md)",
                  borderBottom: "1px solid var(--border-subtle)", paddingBottom: 0 }}>
                  {(["stages", "sla", "variables", "notifications"] as const).map(t => (
                    <button key={t} onClick={() => setActiveTab(t)} style={{
                      padding: "5px 14px", border: "none", background: "none", cursor: "pointer",
                      fontSize: 12, fontWeight: 500,
                      borderBottom: `2px solid ${activeTab === t ? "var(--accent)" : "transparent"}`,
                      color: activeTab === t ? "var(--accent)" : "var(--text-secondary)",
                    }}>
                      {t === "stages" && `Stages (${result.stages?.length ?? 0})`}
                      {t === "sla" && `SLAs (${result.sla_policies?.length ?? 0})`}
                      {t === "variables" && `Variables (${result.variables?.length ?? 0})`}
                      {t === "notifications" && `Notifications (${result.notifications?.length ?? 0})`}
                    </button>
                  ))}
                </div>
              )}

              {/* Stages panel — always shown when no tabs, or when stages tab active */}
              {(!isFullResult || activeTab === "stages") && (
                <div>
                  <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)",
                    fontFamily: "var(--font-mono)", textTransform: "uppercase", marginBottom: "var(--space-sm)" }}>
                    Stages ({result.stages?.length || 0})
                  </div>
                  {result.stages?.map((stage: any, i: number) => (
                    <div key={i} style={{
                      marginBottom: "var(--space-xs)",
                      background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)",
                      borderLeft: "3px solid var(--accent)", overflow: "hidden",
                    }}>
                      <div
                        style={{ padding: "var(--space-sm) var(--space-md)", cursor: isFullResult ? "pointer" : "default" }}
                        onClick={() => isFullResult && setExpandedStage(expandedStage === stage.id ? null : stage.id)}
                      >
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                          <div style={{ fontSize: 13, fontWeight: 500, color: "var(--text-primary)" }}>
                            {i + 1}. {stage.name}
                          </div>
                          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                            {stage.sla_hours && (
                              <span style={{ fontSize: 10, color: "#f59e0b", fontFamily: "var(--font-mono)" }}>
                                ⏱ {stage.sla_hours}h SLA
                              </span>
                            )}
                            {isFullResult && (
                              <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                                {expandedStage === stage.id ? "▲" : "▼"}
                              </span>
                            )}
                          </div>
                        </div>
                        <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 2 }}>
                          id: {stage.id} · {stage.steps?.length || 0} steps
                        </div>

                        {/* Quick mode: inline steps */}
                        {!isFullResult && stage.steps?.map((step: any, si: number) => (
                          <div key={si} style={{
                            fontSize: 11, color: "var(--text-secondary)", marginLeft: "var(--space-md)",
                            marginTop: 4, fontFamily: "var(--font-mono)",
                          }}>
                            → {step.name}{" "}
                            <span style={{ color: STEP_TYPE_COLOR[step.step_type] ?? "var(--text-muted)" }}>
                              [{step.step_type}]
                            </span>
                          </div>
                        ))}
                      </div>

                      {/* Full mode: expanded step detail with form fields */}
                      {isFullResult && expandedStage === stage.id && (
                        <div style={{ borderTop: "1px solid var(--border-subtle)", padding: "0 var(--space-md) var(--space-sm)" }}>
                          {stage.steps?.map((step: any, si: number) => (
                            <div key={si} style={{ marginTop: "var(--space-sm)" }}>
                              <div
                                style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                                  cursor: "pointer", padding: "4px 0" }}
                                onClick={() => setExpandedStep(expandedStep === `${stage.id}-${si}` ? null : `${stage.id}-${si}`)}
                              >
                                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                                  <span style={{
                                    fontSize: 9, padding: "2px 6px", borderRadius: 3,
                                    background: (STEP_TYPE_COLOR[step.step_type] ?? "#94a3b8") + "22",
                                    color: STEP_TYPE_COLOR[step.step_type] ?? "#94a3b8",
                                    fontFamily: "var(--font-mono)", fontWeight: 700,
                                  }}>
                                    {step.step_type}
                                  </span>
                                  <span style={{ fontSize: 12, fontWeight: 500 }}>{step.name}</span>
                                  {step.form_fields?.length > 0 && (
                                    <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
                                      {step.form_fields.length} field{step.form_fields.length !== 1 ? "s" : ""}
                                    </span>
                                  )}
                                </div>
                                <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                                  {expandedStep === `${stage.id}-${si}` ? "▲" : "▼"}
                                </span>
                              </div>

                              {expandedStep === `${stage.id}-${si}` && step.form_fields?.length > 0 && (
                                <div style={{ marginTop: 6, marginLeft: 12, padding: "8px 12px",
                                  background: "var(--bg-card)", borderRadius: "var(--radius-sm)" }}>
                                  <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)",
                                    textTransform: "uppercase", marginBottom: 6 }}>
                                    Form Fields
                                  </div>
                                  {step.form_fields.map((f: any, fi: number) => (
                                    <div key={fi} style={{ display: "flex", gap: 10, alignItems: "center",
                                      padding: "4px 0", borderBottom: "1px solid var(--border-subtle)",
                                      fontSize: 11 }}>
                                      <span style={{ width: 18, fontSize: 12 }}>
                                        {FIELD_TYPE_ICON[f.field_type] ?? "?"}
                                      </span>
                                      <span style={{ flex: 1, fontWeight: 500 }}>{f.label}</span>
                                      <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
                                        {f.field_type}
                                      </span>
                                      {f.required && (
                                        <span style={{ fontSize: 9, color: "var(--status-failed)" }}>*required</span>
                                      )}
                                      {f.options && (
                                        <span style={{ fontSize: 9, color: "var(--text-muted)" }}>
                                          [{f.options.join(", ")}]
                                        </span>
                                      )}
                                    </div>
                                  ))}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}

              {/* SLA policies tab */}
              {isFullResult && activeTab === "sla" && (
                <div>
                  {result.sla_policies?.length === 0 && (
                    <div style={{ color: "var(--text-muted)", fontSize: 12, padding: 16 }}>No SLA policies generated.</div>
                  )}
                  {result.sla_policies?.map((p: any, i: number) => (
                    <div key={i} style={{ padding: "10px 14px", background: "var(--bg-elevated)",
                      borderRadius: "var(--radius-sm)", marginBottom: 6,
                      borderLeft: "3px solid #f59e0b" }}>
                      <div style={{ fontWeight: 600, fontSize: 13 }}>{p.name}</div>
                      <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 4 }}>
                        target: {p.target_stage} · deadline: {p.hours}h · action: {p.action}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Data model tab */}
              {isFullResult && activeTab === "variables" && (
                <div>
                  {result.variables?.length === 0 && (
                    <div style={{ color: "var(--text-muted)", fontSize: 12, padding: 16 }}>No variables generated.</div>
                  )}
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                    <thead>
                      <tr>
                        {["Field", "Label", "Type", "Required"].map(h => (
                          <th key={h} style={{ textAlign: "left", padding: "6px 10px",
                            color: "var(--text-muted)", fontWeight: 600,
                            borderBottom: "1px solid var(--border-subtle)", fontSize: 11 }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {result.variables?.map((f: any, i: number) => (
                        <tr key={i}>
                          <td style={{ padding: "6px 10px", fontFamily: "var(--font-mono)", fontSize: 11 }}>{f.id}</td>
                          <td style={{ padding: "6px 10px" }}>{f.label}</td>
                          <td style={{ padding: "6px 10px", fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-muted)" }}>{f.field_type}</td>
                          <td style={{ padding: "6px 10px" }}>{f.required ? "✓" : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {/* Notifications tab */}
              {isFullResult && activeTab === "notifications" && (
                <div>
                  {result.notifications?.length === 0 && (
                    <div style={{ color: "var(--text-muted)", fontSize: 12, padding: 16 }}>No notifications generated.</div>
                  )}
                  {result.notifications?.map((n: any, i: number) => (
                    <div key={i} style={{ padding: "10px 14px", background: "var(--bg-elevated)",
                      borderRadius: "var(--radius-sm)", marginBottom: 6,
                      borderLeft: "3px solid #22c55e" }}>
                      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
                        <span style={{ fontSize: 10, padding: "2px 6px", borderRadius: 3,
                          background: "#22c55e22", color: "#22c55e",
                          fontFamily: "var(--font-mono)", fontWeight: 700 }}>
                          {n.trigger}
                        </span>
                        <span style={{ fontSize: 10, padding: "2px 6px", borderRadius: 3,
                          background: "#3b82f622", color: "#3b82f6",
                          fontFamily: "var(--font-mono)" }}>
                          {n.channel}
                        </span>
                        <span style={{ fontSize: 10, color: "var(--text-muted)" }}>→ {n.recipient}</span>
                      </div>
                      <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>{n.template}</div>
                    </div>
                  ))}
                </div>
              )}

              {/* Actions */}
              <div style={{ display: "flex", gap: "var(--space-sm)", marginTop: "var(--space-lg)",
                paddingTop: "var(--space-md)", borderTop: "1px solid var(--border-subtle)" }}>
                <Button onClick={handleDeploy} disabled={deploying}>
                  {deploying ? "Deploying..." : "Deploy as Case Type"}
                </Button>
                <Button variant="ghost" onClick={() => setResult(null)}>Discard</Button>
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
