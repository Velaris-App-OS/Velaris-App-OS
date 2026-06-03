import React, { useState } from "react";
import { useApi } from "@shared/hooks";
import {
  listScoutPlatforms, createScoutScan, listScoutScans,
  getScoutScan, getScoutPlan, deleteScoutScan,
} from "@shared/api/client";
import { Card, Button, Spinner, EmptyState, Stat, TimeAgo } from "@shared/components";

/* ═══════════════════════════════════════════════════════════════════
   Scout — Migration Scanner for Pega/Appian/Camunda
   ═══════════════════════════════════════════════════════════════════ */

const EXAMPLE_BPMN = `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn">
  <bpmn:process id="approval-process" name="Approval Process">
    <bpmn:startEvent id="start" name="Request Received"/>
    <bpmn:userTask id="review" name="Review Request"/>
    <bpmn:exclusiveGateway id="gw1" name="Approved?"/>
    <bpmn:userTask id="approve" name="Manager Approval"/>
    <bpmn:serviceTask id="notify" name="Notify Requester"/>
    <bpmn:endEvent id="end" name="Complete"/>
  </bpmn:process>
</bpmn:definitions>`;

const EXAMPLE_PEGA = `Rule-Obj-CaseType: InsuranceClaim
Rule-Obj-Flow: ClaimIntakeFlow
Rule-HTML-Section: ClaimDetails
Rule-Declare-DecisionTable: CoverageDecision
Rule-Obj-Activity: CalculatePayout
Rule-Connect-REST: PaymentServiceCall
Rule-Access-Role-Obj: ClaimsAdjuster
Rule-Obj-SLA: ResolutionSLA`;

export default function Scout() {
  const [view, setView] = useState<"list" | "new" | "detail">("list");
  const [selectedScan, setSelectedScan] = useState<string | null>(null);

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-xl)" }}>
        <div />
        {view === "list" && <Button onClick={() => setView("new")}>+ New Scan</Button>}
        {view !== "list" && <Button variant="ghost" onClick={() => setView("list")}>← Back to Scans</Button>}
      </div>

      {view === "list" && <ScanList onSelect={(id) => { setSelectedScan(id); setView("detail"); }} />}
      {view === "new" && <NewScanForm onCreated={(id) => { setSelectedScan(id); setView("detail"); }} />}
      {view === "detail" && selectedScan && <ScanDetail scanId={selectedScan} />}
    </div>
  );
}

function ScanList({ onSelect }: { onSelect: (id: string) => void }) {
  const { data, loading, refetch } = useApi(listScoutScans);
  const scans = data ?? [];

  if (loading) return <Spinner size={28} />;
  if (scans.length === 0) {
    return <EmptyState title="No scans yet" description="Create a scan to analyze a Pega, Appian, or Camunda export." />;
  }

  return (
    <>
      {scans.map((s: any) => (
        <Card key={s.id} style={{ marginBottom: "var(--space-sm)", cursor: "pointer" }}
          onClick={() => onSelect(s.id)}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
                <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{s.name}</span>
                <PlatformBadge platform={s.source_platform} />
                {s.status === "failed" && <span style={{ fontSize: 10, padding: "2px 6px", background: "var(--status-failed)", color: "white", borderRadius: 3 }}>FAILED</span>}
              </div>
              <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 4 }}>
                {s.source_version && `v${s.source_version} · `}
                {Object.values(s.artifacts_found || {}).reduce((a: any, b: any) => a + b, 0)} artifacts
                {" · "}<TimeAgo date={s.created_at} />
              </div>
            </div>
            {s.compatibility_score != null && (
              <CompatScore score={s.compatibility_score} />
            )}
          </div>
        </Card>
      ))}
    </>
  );
}

function NewScanForm({ onCreated }: { onCreated: (id: string) => void }) {
  const [name, setName] = useState("");
  const [platform, setPlatform] = useState("");
  const [content, setContent] = useState("");
  const [filename, setFilename] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: platforms } = useApi(listScoutPlatforms);

  const handleSubmit = async () => {
    if (!name || !content) { setError("Name and content are required"); return; }
    setSubmitting(true); setError(null);
    try {
      const r = await createScoutScan(name, content, platform, filename);
      onCreated(r.id);
    } catch (e: any) {
      setError(e.message || "Scan failed");
    } finally {
      setSubmitting(false);
    }
  };

  const handleFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setFilename(file.name);
    const text = await file.text();
    setContent(text);
    if (!name) setName(file.name.replace(/\.[^.]+$/, ""));
  };

  return (
    <Card>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-md)", marginBottom: "var(--space-md)" }}>
        <div>
          <Label>Scan Name</Label>
          <input value={name} onChange={e => setName(e.target.value)}
            placeholder="e.g. ClaimsPlatform-Q4"
            style={inputStyle as any} />
        </div>
        <div>
          <Label>Source Platform (optional — auto-detects)</Label>
          <select value={platform} onChange={e => setPlatform(e.target.value)} style={inputStyle as any}>
            <option value="">Auto-detect</option>
            {(platforms?.platforms || []).map((p: any) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>
      </div>

      <div style={{ marginBottom: "var(--space-md)" }}>
        <Label>Upload File or Paste Content</Label>
        <input type="file" onChange={handleFile} style={{ marginBottom: "var(--space-sm)" }}
          accept=".bpmn,.xml,.zip,.rap" />
        <textarea value={content} onChange={e => setContent(e.target.value)}
          placeholder="Paste XML, BPMN, or exported content here..."
          rows={12}
          style={{ ...inputStyle, fontFamily: "var(--font-mono)", fontSize: 11, resize: "vertical" } as any} />
      </div>

      {/* Sample data shortcuts */}
      <div style={{ display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-md)" }}>
        <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", textTransform: "uppercase", alignSelf: "center" }}>Try sample:</span>
        <Button variant="ghost" size="sm" onClick={() => { setContent(EXAMPLE_BPMN); setFilename("sample.bpmn"); setPlatform("camunda"); }}>BPMN</Button>
        <Button variant="ghost" size="sm" onClick={() => { setContent(EXAMPLE_PEGA); setFilename("sample-pega.txt"); setPlatform("pega"); }}>Pega</Button>
      </div>

      {error && <div style={{ color: "var(--status-failed)", fontSize: 12, marginBottom: "var(--space-md)" }}>{error}</div>}

      <Button onClick={handleSubmit} disabled={submitting || !name || !content}>
        {submitting ? "Scanning..." : "Run Scan"}
      </Button>
    </Card>
  );
}

function ScanDetail({ scanId }: { scanId: string }) {
  const { data: scan, loading: loadingScan } = useApi(() => getScoutScan(scanId), [scanId]);
  const { data: plan, loading: loadingPlan } = useApi(() => getScoutPlan(scanId), [scanId]);

  if (loadingScan || loadingPlan) return <Spinner size={28} />;
  if (!scan) return <div>Scan not found</div>;

  const report = scan.scan_report || {};
  const compatBreakdown = report.counts_by_compatibility || {};

  return (
    <div>
      {/* Header card */}
      <Card style={{ marginBottom: "var(--space-lg)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <div style={{ fontSize: 20, fontWeight: 700, color: "var(--text-primary)" }}>{scan.name}</div>
            <div style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 4 }}>
              <PlatformBadge platform={scan.source_platform} /> {scan.source_version && ` · v${scan.source_version}`}
              {scan.filename && ` · ${scan.filename}`}
            </div>
          </div>
          {scan.compatibility_score != null && <CompatScore score={scan.compatibility_score} large />}
        </div>
      </Card>

      {/* Stat cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-lg)" }}>
        <Card><Stat label="Total Artifacts" value={report.total_artifacts || 0} /></Card>
        <Card><Stat label="Effort (weeks)" value={scan.effort_weeks || 0} /></Card>
        <Card><Stat label="Full Compat" value={compatBreakdown.full || 0} /></Card>
        <Card><Stat label="Needs Rework" value={(compatBreakdown.medium || 0) + (compatBreakdown.low || 0) + (compatBreakdown.incompatible || 0)} /></Card>
      </div>

      {/* Migration plan phases */}
      {plan && plan.phases && (
        <>
          <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-secondary)", fontFamily: "var(--font-display)", marginBottom: "var(--space-md)" }}>
            Migration Plan
          </div>
          {plan.phases.filter((p: any) => p.artifacts.length > 0).map((phase: any) => (
            <Card key={phase.phase} style={{ marginBottom: "var(--space-md)" }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "var(--space-sm)" }}>
                <div>
                  <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>
                    Phase {phase.phase}: {phase.name}
                  </span>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>{phase.description}</div>
                </div>
                <div style={{ textAlign: "right" }}>
                  <div style={{ fontSize: 14, fontWeight: 600, color: "var(--accent)" }}>{phase.duration_weeks}w</div>
                  <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
                    {phase.artifacts.length} artifacts
                  </div>
                </div>
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                {phase.artifacts.slice(0, 15).map((a: any, i: number) => (
                  <span key={i} style={{
                    fontSize: 10, padding: "3px 8px", borderRadius: "var(--radius-sm)",
                    background: "var(--bg-elevated)", color: "var(--text-secondary)", fontFamily: "var(--font-mono)",
                  }}>{a.name}</span>
                ))}
                {phase.artifacts.length > 15 && (
                  <span style={{ fontSize: 10, color: "var(--text-muted)", alignSelf: "center" }}>
                    +{phase.artifacts.length - 15} more
                  </span>
                )}
              </div>
            </Card>
          ))}
        </>
      )}

      {/* Recommendations */}
      {plan?.recommendations && plan.recommendations.length > 0 && (
        <Card style={{ marginBottom: "var(--space-lg)" }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", fontFamily: "var(--font-display)", marginBottom: "var(--space-sm)" }}>
            Recommendations
          </div>
          {plan.recommendations.map((r: any, i: number) => (
            <div key={i} style={{
              padding: "var(--space-sm)", marginBottom: 4,
              background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)",
              borderLeft: `3px solid ${sevColor(r.severity)}`,
            }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: sevColor(r.severity) }}>{r.title}</div>
              <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 2 }}>{r.description}</div>
            </div>
          ))}
        </Card>
      )}

      {/* Raw artifact list */}
      <details style={{ marginTop: "var(--space-lg)" }}>
        <summary style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", cursor: "pointer", fontFamily: "var(--font-display)" }}>
          All Artifacts ({report.artifacts?.length || 0})
        </summary>
        <div style={{ marginTop: "var(--space-md)", maxHeight: 400, overflow: "auto", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)" }}>
          {(report.artifacts || []).map((a: any, i: number) => (
            <div key={i} style={{
              display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr 80px",
              padding: "6px 12px", fontSize: 11, borderBottom: "1px solid var(--border-subtle)",
              alignItems: "center",
            }}>
              <span style={{ color: "var(--text-primary)", fontWeight: 500 }}>{a.name}</span>
              <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>{a.type}</span>
              <span style={{ color: "var(--text-secondary)" }}>{a.mapped_to || "—"}</span>
              <CompatBadge level={a.compatibility} />
              <span style={{ textAlign: "right", fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>{a.effort_hours}h</span>
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}

/* ── Shared components ─────────────────────────────────────────── */

function Label({ children }: { children: React.ReactNode }) {
  return (
    <label style={{
      display: "block", fontSize: 10, fontWeight: 600, color: "var(--text-muted)",
      textTransform: "uppercase", fontFamily: "var(--font-mono)",
      marginBottom: 4, letterSpacing: "0.04em",
    }}>{children}</label>
  );
}

function PlatformBadge({ platform }: { platform: string }) {
  const colors: Record<string, string> = {
    pega: "#FF6B35", camunda: "#0053B5", appian: "#67C2A4", unknown: "#888",
  };
  return (
    <span style={{
      fontSize: 9, padding: "2px 8px", borderRadius: "var(--radius-sm)",
      background: colors[platform] || "#888",
      color: "white", fontFamily: "var(--font-mono)", textTransform: "uppercase", fontWeight: 600,
    }}>{platform}</span>
  );
}

function CompatScore({ score, large }: { score: number; large?: boolean }) {
  const pct = Math.round(score * 100);
  const color = pct >= 70 ? "var(--status-completed)" : pct >= 40 ? "#f7b731" : "var(--status-failed)";
  return (
    <div style={{ textAlign: "right" }}>
      <div style={{ fontSize: large ? 28 : 18, fontWeight: 700, color, fontFamily: "var(--font-display)" }}>{pct}%</div>
      <div style={{ fontSize: 9, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase" }}>compatible</div>
    </div>
  );
}

function CompatBadge({ level }: { level: string }) {
  const colors: Record<string, string> = {
    full: "var(--status-completed)", high: "#4ecdc4",
    medium: "#f7b731", low: "#fc5c65", incompatible: "var(--status-failed)",
  };
  return (
    <span style={{
      fontSize: 9, padding: "2px 6px", borderRadius: 3,
      background: (colors[level] || "#888") + "33",
      color: colors[level] || "#888", fontFamily: "var(--font-mono)", textTransform: "uppercase", fontWeight: 600,
    }}>{level}</span>
  );
}

function sevColor(s: string): string {
  return ({ critical: "var(--status-failed)", high: "#f7b731", info: "var(--accent)" } as any)[s] || "var(--text-muted)";
}

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "8px 12px", fontSize: 13, fontFamily: "var(--font-body)",
  background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
  boxSizing: "border-box",
};
