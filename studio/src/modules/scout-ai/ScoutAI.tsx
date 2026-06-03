import React, { useState } from "react";
import { useApi } from "@shared/hooks";
import {
  getScoutAIStatus, analyzeArtifact, generateHelixCode,
} from "@shared/api/client";
import { BRAND } from "@/branding";
import { Card, Button, Spinner } from "@shared/components";

const PEGA_SAMPLE = `// Pega Activity: CalculatePayout
Property.pyStatus = "Processing";
Page PaymentDetails = tools.getProperty("PaymentDetails");
double amount = PaymentDetails.getDouble("Amount");
double adjusted = amount * 0.95;
tools.putProperty("NetAmount", adjusted);

ConnectREST conn = tools.getConnectRest("PaymentServiceCall");
conn.setRequestParam("amount", adjusted);
conn.execute();

tools.logAudit("Payout calculated: " + adjusted);
if (adjusted > 10000) {
    tools.sendEmail("manager@company.com", "Large payout alert");
}`;

const CAMUNDA_SAMPLE = `var order = execution.getVariable("order");
var customerId = order.customerId;
var response = connector.get("/api/customers/" + customerId + "/credit");
var creditScore = response.creditScore;

if (creditScore < 600) {
    execution.setVariable("approval_required", true);
    execution.setVariable("risk_level", "high");
} else if (creditScore < 750) {
    execution.setVariable("approval_required", true);
    execution.setVariable("risk_level", "medium");
} else {
    execution.setVariable("auto_approved", true);
}`;

export default function ScoutAI() {
  const [code, setCode] = useState("");
  const [platform, setPlatform] = useState("pega");
  const [artifactType, setArtifactType] = useState("activity");
  const [identifier, setIdentifier] = useState("");
  const [analysis, setAnalysis] = useState<any>(null);
  const [generating, setGenerating] = useState(false);
  const [generatedCode, setGeneratedCode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { data: status } = useApi(getScoutAIStatus);

  const handleAnalyze = async () => {
    if (!code.trim()) return;
    setGenerating(true); setError(null); setAnalysis(null); setGeneratedCode(null);
    try {
      const r = await analyzeArtifact({
        code, artifact_type: artifactType, source_platform: platform,
        identifier: identifier || "snippet",
      });
      setAnalysis(r);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setGenerating(false);
    }
  };

  const handleGenerateCode = async () => {
    if (!code.trim() || !analysis) return;
    setGenerating(true);
    try {
      const r = await generateHelixCode({
        code, artifact_type: artifactType, source_platform: platform,
      });
      setGeneratedCode(r.generated_code);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setGenerating(false);
    }
  };

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
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
              ? `Ollama connected (${status.ollama_model}) — full AI analysis`
              : "Ollama offline — using heuristic fallback"}
          </span>
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-lg)" }}>
        {/* Input */}
        <Card>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)",
            fontFamily: "var(--font-display)", marginBottom: "var(--space-md)" }}>
            Source Code
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-sm)", marginBottom: "var(--space-md)" }}>
            <div>
              <Label>Platform</Label>
              <select value={platform} onChange={e => setPlatform(e.target.value)} style={inputStyle as any}>
                <option value="pega">Pega PRPC</option>
                <option value="camunda">Camunda</option>
                <option value="appian">Appian</option>
                <option value="servicenow">ServiceNow</option>
              </select>
            </div>
            <div>
              <Label>Artifact Type</Label>
              <select value={artifactType} onChange={e => setArtifactType(e.target.value)} style={inputStyle as any}>
                <option value="activity">Activity</option>
                <option value="script">Script Task</option>
                <option value="expression">Expression Rule</option>
                <option value="decision">Decision Table</option>
                <option value="integration">Integration</option>
              </select>
            </div>
          </div>

          <div style={{ marginBottom: "var(--space-md)" }}>
            <Label>Identifier (optional)</Label>
            <input value={identifier} onChange={e => setIdentifier(e.target.value)}
              placeholder="e.g. CalculatePayout" style={inputStyle as any} />
          </div>

          <div style={{ marginBottom: "var(--space-md)" }}>
            <Label>Code</Label>
            <textarea value={code} onChange={e => setCode(e.target.value)}
              placeholder="Paste legacy code here..."
              rows={14}
              style={{ ...inputStyle, fontFamily: "var(--font-mono)", fontSize: 11, resize: "vertical" } as any} />
          </div>

          <div style={{ display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-sm)" }}>
            <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)",
              textTransform: "uppercase", alignSelf: "center" }}>Try:</span>
            <Button size="sm" variant="ghost" onClick={() => { setCode(PEGA_SAMPLE); setPlatform("pega"); setIdentifier("CalculatePayout"); }}>Pega Sample</Button>
            <Button size="sm" variant="ghost" onClick={() => { setCode(CAMUNDA_SAMPLE); setPlatform("camunda"); setArtifactType("script"); setIdentifier("ValidateOrder"); }}>Camunda Sample</Button>
          </div>

          <Button onClick={handleAnalyze} disabled={!code.trim() || generating}>
            {generating ? "Analyzing..." : "Analyze"}
          </Button>

          {error && <div style={{ color: "var(--status-failed)", fontSize: 12, marginTop: "var(--space-md)" }}>{error}</div>}
        </Card>

        {/* Results */}
        <div>
          {!analysis && !generating && (
            <Card style={{ minHeight: 400, display: "flex", alignItems: "center", justifyContent: "center",
              border: "2px dashed var(--border-subtle)", background: "transparent" }}>
              <div style={{ color: "var(--text-muted)" }}>
                <div style={{ fontSize: 48, marginBottom: "var(--space-md)" }}>🤖</div>
                <div style={{ fontSize: 13 }}>Analysis appears here</div>
              </div>
            </Card>
          )}

          {generating && !analysis && (
            <Card style={{ minHeight: 400, display: "flex", alignItems: "center", justifyContent: "center" }}>
              <div style={{  }}>
                <Spinner size={32} />
                <div style={{ marginTop: "var(--space-md)", fontSize: 12, color: "var(--text-muted)" }}>
                  {status?.ollama_available ? "Reading code..." : "Scanning patterns..."}
                </div>
              </div>
            </Card>
          )}

          {analysis && (
            <Card>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "var(--space-md)" }}>
                <div>
                  <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>
                    {analysis.identifier}
                  </div>
                  <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 2 }}>
                    source: {analysis.source} · confidence: {(analysis.confidence * 100).toFixed(0)}%
                  </div>
                </div>
                <span style={{
                  fontSize: 10, padding: "3px 10px", borderRadius: 3,
                  background: complexityColor(analysis.complexity) + "33",
                  color: complexityColor(analysis.complexity),
                  fontFamily: "var(--font-mono)", textTransform: "uppercase", fontWeight: 600,
                }}>{analysis.complexity}</span>
              </div>

              <Section title="Summary">
                <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.5, width: "100%" }}>
                  {analysis.summary || "—"}
                </div>
              </Section>

              <Section title="Business Logic">
                <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.5, width: "100%" }}>
                  {analysis.business_logic || "—"}
                </div>
              </Section>

              {analysis.external_calls?.length > 0 && (
                <Section title={`External Calls (${analysis.external_calls.length})`}>
                  {analysis.external_calls.map((c: string, i: number) => (
                    <Chip key={i} color="var(--accent)">{c}</Chip>
                  ))}
                </Section>
              )}

              {(analysis.data_reads?.length > 0 || analysis.data_writes?.length > 0) && (
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-md)", marginBottom: "var(--space-md)" }}>
                  {analysis.data_reads?.length > 0 && (
                    <Section title={`Reads (${analysis.data_reads.length})`}>
                      {analysis.data_reads.slice(0, 8).map((d: string, i: number) => (
                        <Chip key={i} color="var(--status-completed)">{d}</Chip>
                      ))}
                    </Section>
                  )}
                  {analysis.data_writes?.length > 0 && (
                    <Section title={`Writes (${analysis.data_writes.length})`}>
                      {analysis.data_writes.slice(0, 8).map((d: string, i: number) => (
                        <Chip key={i} color="#f7b731">{d}</Chip>
                      ))}
                    </Section>
                  )}
                </div>
              )}

              {analysis.side_effects?.length > 0 && (
                <Section title="Side Effects">
                  {analysis.side_effects.map((s: string, i: number) => (
                    <Chip key={i} color="var(--status-failed)">⚠ {s}</Chip>
                  ))}
                </Section>
              )}

              {analysis.helix_mapping?.artifact_type && (
                <Section title={`Suggested ${BRAND.name} Mapping`}>
                  <div style={{ fontSize: 12, padding: "var(--space-sm)", background: "var(--bg-elevated)",
                    borderRadius: "var(--radius-sm)", width: "100%" }}>
                    <div>Type: <code>{analysis.helix_mapping.artifact_type}</code></div>
                    {analysis.helix_mapping.name && (
                      <div>Name: <code>{analysis.helix_mapping.name}</code></div>
                    )}
                    {analysis.helix_mapping.notes && (
                      <div style={{ marginTop: 4, color: "var(--text-muted)", fontSize: 11 }}>
                        {analysis.helix_mapping.notes}
                      </div>
                    )}
                  </div>
                </Section>
              )}

              <div style={{ display: "flex", gap: "var(--space-sm)", marginTop: "var(--space-md)",
                paddingTop: "var(--space-md)", borderTop: "1px solid var(--border-subtle)" }}>
                <Button onClick={handleGenerateCode} disabled={generating}>
                  {generating ? "Generating..." : `🪄 Generate ${BRAND.name} Code`}
                </Button>
              </div>

              {generatedCode && (
                <div style={{ marginTop: "var(--space-md)" }}>
                  <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)",
                    textTransform: "uppercase", marginBottom: 4 }}>Generated Python</div>
                  <pre style={{
                    background: "var(--bg-input)", padding: "var(--space-sm)",
                    borderRadius: "var(--radius-sm)", fontSize: 11,
                    fontFamily: "var(--font-mono)", color: "var(--text-primary)",
                    overflow: "auto", maxHeight: 400,
                  }}>{generatedCode}</pre>
                </div>
              )}
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: "var(--space-md)" }}>
      <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)",
        textTransform: "uppercase", marginBottom: 6, letterSpacing: "0.04em" }}>{title}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>{children}</div>
    </div>
  );
}

function Chip({ children, color }: { children: React.ReactNode; color: string }) {
  return (
    <span style={{
      fontSize: 10, padding: "3px 8px", borderRadius: "var(--radius-sm)",
      background: color + "22", color, fontFamily: "var(--font-mono)",
    }}>{children}</span>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <label style={{
      display: "block", fontSize: 10, fontWeight: 600, color: "var(--text-muted)",
      textTransform: "uppercase", fontFamily: "var(--font-mono)",
      marginBottom: 4, letterSpacing: "0.04em",
    }}>{children}</label>
  );
}

function complexityColor(c: string): string {
  return ({
    low: "var(--status-completed)", medium: "#f7b731",
    high: "#fc5c65", extreme: "var(--status-failed)",
  } as any)[c] || "var(--text-muted)";
}

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "8px 12px", fontSize: 13, fontFamily: "var(--font-body)",
  background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
  boxSizing: "border-box",
};
