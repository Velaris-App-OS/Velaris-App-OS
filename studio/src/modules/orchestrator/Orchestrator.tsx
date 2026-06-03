import React, { useState } from "react";
import { useApi } from "@shared/hooks";
import {
  listMigrationProjects, createMigrationProject, deleteMigrationProject,
  getMigrationRoadmap, listMigrationTasks, analyzeTask, generateTaskCode,
  markTaskPorted, runFullPipeline, downloadMigrationZip, listScoutScans,
} from "@shared/api/client";
import { Card, Button, Spinner, EmptyState, Stat, TimeAgo } from "@shared/components";

/* ═══════════════════════════════════════════════════════════════════
   Migration Orchestrator — end-to-end app migration (Phase 21)
   ═══════════════════════════════════════════════════════════════════ */

export default function Orchestrator() {
  const [view, setView] = useState<"list" | "new" | "detail">("list");
  const [projectId, setProjectId] = useState<string | null>(null);

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", overflow: "auto", height: "100%", boxSizing: "border-box" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-xl)" }}>
        <div />
        {view === "list" && <Button onClick={() => setView("new")}>+ New Migration Project</Button>}
        {view !== "list" && <Button variant="ghost" onClick={() => setView("list")}>← Back to Projects</Button>}
      </div>

      {view === "list" && <ProjectList onOpen={(id) => { setProjectId(id); setView("detail"); }} />}
      {view === "new" && <NewProjectForm onCreated={(id) => { setProjectId(id); setView("detail"); }} />}
      {view === "detail" && projectId && <ProjectDetail projectId={projectId} />}
    </div>
  );
}

function ProjectList({ onOpen }: { onOpen: (id: string) => void }) {
  const { data, loading, refetch } = useApi(listMigrationProjects);
  const projects = data ?? [];

  if (loading) return <Spinner size={28} />;
  if (projects.length === 0) {
    return <EmptyState title="No migration projects yet"
      description="Start by creating a Scout scan, then create a migration project from it." />;
  }

  return (
    <>
      {projects.map((p: any) => (
        <Card key={p.id} style={{ marginBottom: "var(--space-sm)", cursor: "pointer" }}
          onClick={() => onOpen(p.id)}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{p.name}</div>
              <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 4 }}>
                {p.source_platform} · {p.total_artifacts} artifacts · <TimeAgo date={p.created_at} />
              </div>
              <div style={{ display: "flex", gap: "var(--space-md)", marginTop: "var(--space-sm)" }}>
                <MiniStat label="Analyzed" value={p.analyzed_count} total={p.total_artifacts} />
                <MiniStat label="Generated" value={p.generated_count} total={p.total_artifacts} />
                <MiniStat label="Ported" value={p.ported_count} total={p.total_artifacts} color="var(--status-completed)" />
              </div>
            </div>
            <span style={{
              fontSize: 10, padding: "3px 8px", borderRadius: 3,
              background: statusColor(p.status) + "33",
              color: statusColor(p.status),
              fontFamily: "var(--font-mono)", textTransform: "uppercase", fontWeight: 600,
            }}>{p.status}</span>
          </div>
        </Card>
      ))}
    </>
  );
}

function NewProjectForm({ onCreated }: { onCreated: (id: string) => void }) {
  const [name, setName] = useState("");
  const [scanId, setScanId] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: scans } = useApi(listScoutScans);

  const handleCreate = async () => {
    if (!name || !scanId) { setError("Name and scan are required"); return; }
    setCreating(true); setError(null);
    try {
      const r = await createMigrationProject(name, scanId);
      onCreated(r.id);
    } catch (e: any) {
      setError(e.message || "Failed");
    } finally {
      setCreating(false);
    }
  };

  return (
    <Card>
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: "var(--space-md)" }}>
        New Migration Project
      </div>

      <div style={{ marginBottom: "var(--space-md)" }}>
        <Label>Project Name</Label>
        <input value={name} onChange={e => setName(e.target.value)}
          placeholder="e.g. Legacy Claims App Migration"
          style={inputStyle as any} />
      </div>

      <div style={{ marginBottom: "var(--space-md)" }}>
        <Label>Source Scan</Label>
        <select value={scanId} onChange={e => setScanId(e.target.value)} style={inputStyle as any}>
          <option value="">Choose a completed Scout scan...</option>
          {(scans || []).filter((s: any) => s.status === "completed").map((s: any) => (
            <option key={s.id} value={s.id}>
              {s.name} ({s.source_platform} · {Object.values(s.artifacts_found || {}).reduce((a: any, b: any) => a + b, 0)} artifacts)
            </option>
          ))}
        </select>
        {(!scans || scans.length === 0) && (
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
            No Scout scans yet. Run a scan in the Scout module first.
          </div>
        )}
      </div>

      {error && <div style={{ color: "var(--status-failed)", fontSize: 12, marginBottom: "var(--space-md)" }}>{error}</div>}

      <Button onClick={handleCreate} disabled={creating || !name || !scanId}>
        {creating ? "Creating..." : "Create Project"}
      </Button>
    </Card>
  );
}

function ProjectDetail({ projectId }: { projectId: string }) {
  const { data: project, refetch: refetchProject } = useApi(() => getMigrationRoadmap(projectId), [projectId]);
  const { data: tasks, refetch: refetchTasks } = useApi(() => listMigrationTasks(projectId), [projectId]);
  const [runningAll, setRunningAll] = useState(false);
  const [downloading, setDownloading] = useState(false);

  const handleRunAll = async () => {
    setRunningAll(true);
    try {
      await runFullPipeline(projectId, 50);
      refetchProject();
      refetchTasks();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setRunningAll(false);
    }
  };

  const handleDownload = async () => {
    setDownloading(true);
    try {
      const blob = await downloadMigrationZip(projectId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${project?.project_name || "migration"}.zip`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      alert(e.message);
    } finally {
      setDownloading(false);
    }
  };

  const handleAnalyzeTask = async (taskId: string) => {
    try {
      await analyzeTask(taskId);
      refetchProject();
      refetchTasks();
    } catch (e: any) {
      alert(e.message);
    }
  };

  const handleGenerateTask = async (taskId: string) => {
    try {
      await generateTaskCode(taskId);
      refetchProject();
      refetchTasks();
    } catch (e: any) {
      alert(e.message);
    }
  };

  const handlePorted = async (taskId: string) => {
    try {
      await markTaskPorted(taskId);
      refetchProject();
      refetchTasks();
    } catch (e: any) {
      alert(e.message);
    }
  };

  if (!project) return <Spinner size={28} />;

  return (
    <div>
      <Card style={{ marginBottom: "var(--space-lg)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <div style={{ fontSize: 18, fontWeight: 700 }}>{project.project_name}</div>
            <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 4 }}>
              {project.source_platform} · {project.total_artifacts} artifacts
            </div>
          </div>
          <div style={{ display: "flex", gap: "var(--space-sm)" }}>
            <Button variant="ghost" onClick={handleDownload} disabled={downloading}>
              {downloading ? "Packaging..." : "📥 Export ZIP"}
            </Button>
            <Button onClick={handleRunAll} disabled={runningAll}>
              {runningAll ? "Running pipeline..." : "▶ Run All (analyze + generate)"}
            </Button>
          </div>
        </div>
      </Card>

      {/* Progress */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-lg)" }}>
        <Card><Stat label="Total Tasks" value={project.total_artifacts} /></Card>
        <Card><Stat label="Analyzed" value={project.analyzed_count} /></Card>
        <Card><Stat label="Code Generated" value={project.generated_count} /></Card>
        <Card><Stat label="Ported" value={project.ported_count} /></Card>
      </div>

      {/* Phases */}
      {project.phases?.map((phase: any) => (
        <Card key={phase.phase} style={{ marginBottom: "var(--space-md)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "var(--space-sm)" }}>
            <div>
              <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>
                Phase {phase.phase}: {phase.name}
              </div>
              <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 2 }}>
                {phase.tasks.length} tasks · ~{Math.round(phase.total_hours)}h estimated
              </div>
            </div>
          </div>

          <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", overflow: "hidden" }}>
            {phase.tasks.map((task: any, i: number) => {
              const fullTask = (tasks || []).find((t: any) => t.id === task.id);
              return (
                <div key={task.id} style={{
                  display: "grid", gridTemplateColumns: "auto 2fr 1fr 1fr 120px",
                  alignItems: "center", gap: "var(--space-sm)",
                  padding: "8px 12px",
                  borderTop: i > 0 ? "1px solid var(--border-subtle)" : "none",
                }}>
                  <span style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--text-muted)", width: 20 }}>
                    #{phase.tasks.indexOf(task) + 1}
                  </span>
                  <div>
                    <div style={{ fontSize: 12, color: "var(--text-primary)" }}>
                      {task.artifact_name}
                    </div>
                    <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                      {task.artifact_type}
                      {task.depends_on?.length > 0 && ` · depends on ${task.depends_on.length}`}
                    </div>
                  </div>
                  <span style={{
                    fontSize: 9, padding: "2px 6px", borderRadius: 3,
                    background: complexityColor(task.complexity) + "33",
                    color: complexityColor(task.complexity),
                    fontFamily: "var(--font-mono)", textTransform: "uppercase",
                    width: "fit-content", fontWeight: 600,
                  }}>{task.complexity}</span>
                  <span style={{
                    fontSize: 9, padding: "2px 6px", borderRadius: 3,
                    background: statusColor(task.status) + "33",
                    color: statusColor(task.status),
                    fontFamily: "var(--font-mono)", textTransform: "uppercase",
                    width: "fit-content", fontWeight: 600,
                  }}>{task.status}</span>
                  <div style={{ display: "flex", gap: 4 }}>
                    {task.status === "pending" && (
                      <button onClick={() => handleAnalyzeTask(task.id)}
                        style={miniButton}>Analyze</button>
                    )}
                    {task.status === "ready" && (
                      <button onClick={() => handleGenerateTask(task.id)}
                        style={miniButton}>Generate</button>
                    )}
                    {task.status === "generated" && (
                      <button onClick={() => handlePorted(task.id)}
                        style={{ ...miniButton, background: "var(--status-completed)" }}>Mark Ported</button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      ))}
    </div>
  );
}

function MiniStat({ label, value, total, color }: { label: string; value: number; total: number; color?: string }) {
  const pct = total ? (value / total) * 100 : 0;
  return (
    <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
      <span style={{ color: color || "var(--accent)" }}>{value}</span>/{total} {label}
      <div style={{ width: 60, height: 2, background: "var(--bg-elevated)", marginTop: 2 }}>
        <div style={{ width: `${pct}%`, height: "100%", background: color || "var(--accent)" }} />
      </div>
    </div>
  );
}

function statusColor(s: string): string {
  return ({
    draft: "var(--text-muted)",
    ready: "var(--accent)",
    pending: "var(--text-muted)",
    analyzing: "#f7b731",
    generating: "#f7b731",
    generated: "var(--accent)",
    ported: "var(--status-completed)",
    in_progress: "#f7b731",
    completed: "var(--status-completed)",
    failed: "var(--status-failed)",
    skipped: "var(--text-muted)",
  } as any)[s] || "var(--text-muted)";
}

function complexityColor(c: string): string {
  return ({
    full: "var(--status-completed)",
    high: "#4ecdc4",
    medium: "#f7b731",
    low: "#fc5c65",
    incompatible: "var(--status-failed)",
  } as any)[c] || "var(--text-muted)";
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

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "8px 12px", fontSize: 13, fontFamily: "var(--font-body)",
  background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
  boxSizing: "border-box",
};

const miniButton: React.CSSProperties = {
  fontSize: 10, padding: "4px 10px", borderRadius: 3,
  background: "var(--accent)", color: "white", border: "none",
  cursor: "pointer", fontFamily: "var(--font-mono)", textTransform: "uppercase",
  fontWeight: 600,
};
