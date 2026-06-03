import React, { useState, useMemo, useEffect } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { useApi, useCommit, useCurrentUserGroups } from "@shared/hooks";
import { useAuth } from "@/auth";
import {
  listCaseTypes,
  deployCaseType,
  deleteCaseType,
  listProcesses,
  listTenants,
  createBranch,
} from "@shared/api/client";
import {
  Card,
  Button,
  Spinner,
  EmptyState,
  TimeAgo,
  Stat,
  CommitModal,
  ReviewerPicker,
} from "@shared/components";
import CaseTypeEditor from "./CaseTypeEditor";
import type { CaseTypeSummary } from "@shared/types";

// ── Semver comparison ─────────────────────────────────────────────
function compareSemver(a: string, b: string): number {
  const parse = (v: string) => v.split(".").map(n => parseInt(n) || 0);
  const [aM, am, ap] = parse(a);
  const [bM, bm, bp] = parse(b);
  return (aM - bM) || (am - bm) || (ap - bp);
}

/* ═══════════════════════════════════════════════════════════════════
   CaseDesigner — deploy, manage, and visually design case types
   ═══════════════════════════════════════════════════════════════════ */

const PRIORITY_COLORS: Record<string, string> = {
  low: "var(--text-muted)",
  medium: "var(--text-secondary)",
  high: "var(--status-running)",
  critical: "var(--status-failed)",
  blocker: "var(--status-failed)",
};

export default function CaseDesigner() {
  const [searchParams] = useSearchParams();
  const { data, loading, error, refetch } = useApi(listCaseTypes);
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(() => searchParams.get("caseType"));
  const [editingReadOnly, setEditingReadOnly] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [importJson, setImportJson] = useState("");
  const [importVersion, setImportVersion] = useState("");
  const [importResult, setImportResult] = useState<any | null>(null);
  const [importLoading, setImportLoading] = useState(false);

  // If ?caseType= changes (e.g. after navigation), open that editor
  useEffect(() => {
    const id = searchParams.get("caseType");
    if (id) setEditingId(id);
  }, [searchParams]);

  const caseTypes = data?.items ?? [];
  const total = data?.total ?? 0;

  // Group by name, latest version last in each group
  const groups = useMemo(() => {
    const map: Record<string, CaseTypeSummary[]> = {};
    for (const ct of caseTypes) {
      if (!map[ct.name]) map[ct.name] = [];
      map[ct.name].push(ct);
    }
    for (const name of Object.keys(map)) {
      map[name].sort((a, b) => compareSemver(a.version, b.version));
    }
    return Object.values(map);
  }, [caseTypes]);

  const openEditor = (id: string, readOnly: boolean) => {
    setEditingId(id);
    setEditingReadOnly(readOnly);
  };

  // ── Editor view ────────────────────────────────────────────────
  if (editingId) {
    return (
      <CaseTypeEditor
        caseTypeId={editingId}
        readOnly={editingReadOnly}
        onBack={() => { setEditingId(null); setEditingReadOnly(false); refetch(); }}
        onOpenVersion={(id, ro) => { setEditingId(id); setEditingReadOnly(ro); }}
      />
    );
  }

  // ── List view ──────────────────────────────────────────────────
  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-xl)" }}>
        <div style={{ display: "flex", gap: 8 }}>
          <Button variant="secondary" onClick={() => setShowImport(s => !s)}>⬆ Import Bundle</Button>
          <Button onClick={() => setShowCreate(true)}>+ New Case Type</Button>
        </div>
      </div>

      {/* Import panel */}
      {showImport && (
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 10, padding: 20, marginBottom: 24 }}>
          <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 12 }}>Import Case Type Bundle</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
            Paste the JSON exported from another environment. The case type and all its linked forms will be created or updated.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 160px", gap: 12, marginBottom: 12 }}>
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase" as const, marginBottom: 4 }}>Bundle JSON</div>
              <textarea value={importJson} onChange={e => setImportJson(e.target.value)} rows={6} placeholder='{"schema":"velaris-case-bundle/1.0","case_type":{...}}'
                style={{ width: "100%", padding: "8px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, fontFamily: "var(--font-mono)", background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const, resize: "vertical" }} />
            </div>
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase" as const, marginBottom: 4 }}>Version Override</div>
              <input value={importVersion} onChange={e => setImportVersion(e.target.value)} placeholder="e.g. 2.1.0 (optional)"
                style={{ width: "100%", padding: "8px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const, marginBottom: 8 }} />
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Leave blank to use the version from the bundle</div>
            </div>
          </div>
          {importResult && (
            <div style={{ fontSize: 12, padding: "10px 14px", borderRadius: 6, background: "#22c55e22", color: "#22c55e", marginBottom: 12 }}>
              ✓ Imported: {importResult.case_type?.action} case type v{importResult.case_type?.version}
              {importResult.forms?.length > 0 && ` · ${importResult.forms.length} form(s)`}
            </div>
          )}
          <div style={{ display: "flex", gap: 8 }}>
            <Button disabled={importLoading || !importJson} onClick={async () => {
              setImportLoading(true); setImportResult(null);
              try {
                const bundle = JSON.parse(importJson);
                const r = await fetch("/api/v1/apps/import", {
                  method: "POST",
                  headers: {
                    "Content-Type": "application/json",
                    ...(localStorage.getItem("helix_token") ? { Authorization: `Bearer ${localStorage.getItem("helix_token")}` } : {}),
                  },
                  body: JSON.stringify({ bundle, version_override: importVersion || null }),
                });
                if (r.ok) { setImportResult(await r.json()); refetch(); }
              } catch (e: any) { alert(e.message || "Import failed"); }
              setImportLoading(false);
            }}>{importLoading ? "Importing…" : "Import"}</Button>
            <Button variant="secondary" onClick={() => { setShowImport(false); setImportJson(""); setImportResult(null); }}>Close</Button>
          </div>
        </div>
      )}

      {/* Stats row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-xl)" }}>
        <Card><Stat label="Case Types" value={total} /></Card>
        <Card><Stat label="Active" value={caseTypes.length} /></Card>
        <Card><Stat label="With SLAs" value={caseTypes.filter(ct => {
          const def = (ct as any).definition_json;
          return def?.sla_policies?.length > 0;
        }).length} /></Card>
      </div>

      {/* Content */}
      {loading && (
        <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}>
          <Spinner size={28} />
        </div>
      )}

      {error && (
        <Card style={{ borderColor: "var(--status-failed)" }}>
          <p style={{ color: "var(--status-failed)", fontSize: 13 }}>Failed to load case types: {error}</p>
          <Button variant="secondary" size="sm" onClick={refetch} style={{ marginTop: "var(--space-sm)" }}>Retry</Button>
        </Card>
      )}

      {!loading && !error && caseTypes.length === 0 && (
        <EmptyState
          title="No case types yet"
          description="Create your first case type to start building case management workflows."
          action={<Button onClick={() => setShowCreate(true)}>+ New Case Type</Button>}
        />
      )}

      {!loading && !error && groups.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(420px, 1fr))", gap: "var(--space-md)" }}>
          {groups.map((versions) => (
            <GroupedCaseTypeCard
              key={versions[0].name}
              versions={versions}
              onEdit={(id) => openEditor(id, false)}
              onView={(id) => openEditor(id, true)}
              onDelete={async (id, name, ver) => {
                if (confirm(`Delete "${name}" v${ver}? This cannot be undone.`)) {
                  await deleteCaseType(id);
                  refetch();
                }
              }}
            />
          ))}
        </div>
      )}

      {/* Create modal */}
      {showCreate && (
        <CreateCaseTypeModal onClose={() => setShowCreate(false)} onCreated={() => { setShowCreate(false); refetch(); }} />
      )}
    </div>
  );
}

/* ── Grouped Case Type Card (with version timeline) ──────────────────── */

function GroupedCaseTypeCard({ versions, onEdit, onView, onDelete }: {
  versions: CaseTypeSummary[];
  onEdit: (id: string) => void;
  onView: (id: string) => void;
  onDelete: (id: string, name: string, ver: string) => void;
}) {
  const navigate = useNavigate();
  const myGroups = useCurrentUserGroups();
  const latest = versions[versions.length - 1];
  const older = versions.slice(0, -1).reverse(); // newest-old first
  const def = (latest as any).definition_json || {};
  const stageCount = def.stages?.length || 0;
  const stepCount = (def.stages || []).reduce((n: number, s: any) => n + (s.steps?.length || 0), 0);
  const [showHistory, setShowHistory] = useState(false);
  const [showBranch, setShowBranch] = useState(false);
  const [branchName, setBranchName] = useState("");
  const [reviewerId, setReviewerId] = useState("");
  const [branchBusy, setBranchBusy] = useState(false);
  const [createdBranch, setCreatedBranch] = useState<any>(null);
  const [branchErr, setBranchErr] = useState<string | null>(null);

  const defaultBranchName = () =>
    "fix/" + latest.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);

  const handleCreateBranch = async () => {
    if (!branchName.trim()) return;
    setBranchBusy(true); setBranchErr(null);
    try {
      const b = await createBranch({
        name: branchName.trim(),
        artifact_type: "case_type",
        artifact_id: latest.id,
        description: `Branch of "${latest.name}" v${latest.version}`,
        assigned_reviewer_id: reviewerId.trim() || undefined,
      });
      setCreatedBranch(b);
      setBranchName(""); setReviewerId("");
    } catch (e: any) { setBranchErr(e.message); }
    finally { setBranchBusy(false); }
  };

  return (
    <Card style={{ padding: 0, overflow: showBranch ? "visible" : "hidden" }}>
      {/* ── Latest version header ── */}
      <div style={{ padding: "var(--space-lg)", cursor: "pointer" }} onClick={() => onEdit(latest.id)}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "var(--space-sm)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flex: 1, minWidth: 0 }}>
            {latest.icon && <span style={{ fontSize: 20, flexShrink: 0 }}>{latest.icon}</span>}
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                {latest.name}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 2 }}>
                <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", fontWeight: 700, padding: "1px 6px", borderRadius: 4, background: "var(--accent-dim)", color: "var(--accent)" }}>
                  v{latest.version}
                </span>
                <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", padding: "1px 6px", borderRadius: 4, background: "color-mix(in srgb, var(--status-completed) 12%, transparent)", color: "var(--status-completed)" }}>
                  LATEST
                </span>
              </div>
            </div>
          </div>
          <span style={{
            fontSize: 10, fontWeight: 600, flexShrink: 0,
            color: PRIORITY_COLORS[latest.default_priority] || "var(--text-muted)",
            padding: "2px 8px", borderRadius: 100,
            background: `color-mix(in srgb, ${PRIORITY_COLORS[latest.default_priority] || "var(--text-muted)"} 12%, transparent)`,
            textTransform: "uppercase", fontFamily: "var(--font-mono)",
          }}>
            {latest.default_priority}
          </span>
        </div>

        {latest.description && (
          <p style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.5, margin: "var(--space-sm) 0", overflow: "hidden", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" as any }}>
            {latest.description}
          </p>
        )}

        <div style={{ display: "flex", gap: "var(--space-md)", fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: "var(--space-sm)" }}>
          <span>{stageCount} stage{stageCount !== 1 ? "s" : ""}</span>
          <span>{stepCount} step{stepCount !== 1 ? "s" : ""}</span>
          {(latest.tags || []).slice(0, 2).map((tag) => (
            <span key={tag} style={{ padding: "0px 5px", borderRadius: 3, background: "var(--bg-elevated)" }}>{tag}</span>
          ))}
        </div>
      </div>

      {/* ── Version timeline strip ── */}
      <div style={{ borderTop: "1px solid var(--border-subtle)", padding: "10px var(--space-lg)", background: "var(--bg-elevated)" }}>
        {/* Timeline dots */}
        <div style={{ display: "flex", alignItems: "center", gap: 0, marginBottom: older.length > 0 ? 8 : 0 }}>
          {versions.map((v, i) => {
            const isLatest = i === versions.length - 1;
            return (
              <React.Fragment key={v.id}>
                <button
                  onClick={e => { e.stopPropagation(); isLatest ? onEdit(v.id) : onView(v.id); }}
                  title={isLatest ? `Edit v${v.version}` : `View v${v.version} (read-only)`}
                  style={{
                    width: isLatest ? 28 : 20, height: isLatest ? 28 : 20, borderRadius: "50%", cursor: "pointer",
                    background: isLatest ? "var(--accent)" : "var(--bg-panel)",
                    border: `2px solid ${isLatest ? "var(--accent)" : "var(--border-default)"}`,
                    display: "flex", alignItems: "center", justifyContent: "center",
                    flexShrink: 0, position: "relative", transition: "all 0.15s",
                  }}
                  onMouseEnter={e => { if (!isLatest) (e.currentTarget as HTMLElement).style.borderColor = "var(--accent)"; }}
                  onMouseLeave={e => { if (!isLatest) (e.currentTarget as HTMLElement).style.borderColor = "var(--border-default)"; }}
                >
                  {isLatest
                    ? <span style={{ fontSize: 10, color: "white", fontWeight: 700 }}>✓</span>
                    : <span style={{ fontSize: 8, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>●</span>
                  }
                </button>
                {i < versions.length - 1 && (
                  <div style={{ flex: 1, height: 2, minWidth: 12, background: "var(--border-default)", margin: "0 2px" }} />
                )}
              </React.Fragment>
            );
          })}
          <div style={{ flex: 1 }} />
          <span style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginLeft: "var(--space-sm)" }}>
            {versions.length} version{versions.length !== 1 ? "s" : ""}
          </span>
        </div>

        {/* Version labels */}
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap", alignItems: "center" }}>
          {versions.map((v, i) => {
            const isLatest = i === versions.length - 1;
            return (
              <button
                key={v.id}
                onClick={e => { e.stopPropagation(); isLatest ? onEdit(v.id) : onView(v.id); }}
                style={{
                  padding: "2px 8px", borderRadius: 4, border: "none", cursor: "pointer",
                  fontSize: 10, fontFamily: "var(--font-mono)", fontWeight: isLatest ? 700 : 400,
                  background: isLatest ? "var(--accent-dim)" : "transparent",
                  color: isLatest ? "var(--accent)" : "var(--text-muted)",
                  textDecoration: isLatest ? "none" : "underline dotted",
                }}
              >
                v{v.version}
              </button>
            );
          })}
        </div>
      </div>

      {/* ── Action footer ── */}
      <div style={{ borderTop: "1px solid var(--border-subtle)", padding: "8px var(--space-lg)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
          <TimeAgo date={latest.created_at} />
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <Button variant="secondary" size="sm" onClick={() => {
            setBranchName(defaultBranchName());
            setCreatedBranch(null); setBranchErr(null);
            setShowBranch(s => !s);
          }}>⎇ Branch</Button>
          <Button variant="secondary" size="sm" onClick={() => onEdit(latest.id)}>Edit</Button>
          <Button variant="danger" size="sm" onClick={() => { onDelete(latest.id, latest.name, latest.version); }}>Delete</Button>
        </div>
      </div>

      {/* ── Inline branch form ── */}
      {showBranch && (
        <div style={{ borderTop: "1px solid var(--border-subtle)", padding: "12px var(--space-lg)", background: "var(--bg-elevated)" }}>
          {createdBranch ? (
            <div style={{ fontSize: 12 }}>
              <div style={{ padding: "6px 10px", borderRadius: 4, background: "#dcfce7", color: "#16a34a", marginBottom: 8 }}>
                ✓ Branch <b>{createdBranch.name}</b> created
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <Button size="sm" onClick={() => navigate(`/case-designer?caseType=${latest.id}&branch=${createdBranch.id}`)}>
                  Open in Editor →
                </Button>
                <Button variant="secondary" size="sm" onClick={() => navigate("/hxbranch")}>View in HxBranch</Button>
                <Button variant="ghost" size="sm" onClick={() => { setShowBranch(false); setCreatedBranch(null); }}>Close</Button>
              </div>
            </div>
          ) : (
            <>
              {branchErr && (
                <div style={{ fontSize: 12, marginBottom: 8, padding: "6px 10px", borderRadius: 4, background: "#fee2e2", color: "#ef4444" }}>
                  ✗ {branchErr}
                </div>
              )}
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                <input
                  style={{ flex: 1, padding: "6px 10px", border: "1px solid var(--border-default)", borderRadius: 4, fontSize: 12, fontFamily: "var(--font-mono)", background: "var(--bg-input)", color: "var(--text-primary)" }}
                  placeholder="branch name, e.g. fix/add-resolution-stage"
                  value={branchName}
                  onChange={e => setBranchName(e.target.value)}
                  onKeyDown={e => e.key === "Enter" && handleCreateBranch()}
                />
                <Button size="sm" disabled={branchBusy || !branchName.trim()} onClick={handleCreateBranch}>
                  {branchBusy ? "Creating…" : "Create"}
                </Button>
                <Button variant="secondary" size="sm" onClick={() => { setShowBranch(false); setBranchErr(null); }}>✕</Button>
              </div>
              <ReviewerPicker
                value={reviewerId}
                onChange={setReviewerId}
                accessGroupId={myGroups[0]}
                placeholder="Reviewer (optional, same access group)"
              />
            </>
          )}
        </div>
      )}
    </Card>
  );
}

/* ── Create Case Type Modal ───────────────────────────────────── */

function CreateCaseTypeModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const { user } = useAuth();
  const isAdmin = user?.is_admin ?? false;

  const { data: processData } = useApi(listProcesses);
  const processes = (processData as any)?.processes ?? [];

  const { data: tenantData } = useApi(listTenants);
  const tenants: any[] = (tenantData as any) ?? [];

  const [name, setName] = useState("");
  const [version, setVersion] = useState("1.0.0");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("medium");
  const [processId, setProcessId] = useState("");
  // Default to the user's own tenant — not global.
  // Global (tenant_id = null) makes the case type visible to ALL tenants.
  // Only admins can create global case types.
  const [tenantId, setTenantId] = useState<string>(user?.tenant_id ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const { commitOpen, commitSaving, requestCommit, handleCommit, cancelCommit } =
    useCommit("case_type", "new", name || "New Case Type");

  const doSave = async () => {
    await deployCaseType({
      name,
      version,
      tenant_id: tenantId || null,
      lifecycle_process_id: processId || undefined,
      definition_json: { stages: [], sla_policies: [] },
      default_priority: priority,
      description,
    });
    onCreated();
  };

  const handleSubmit = () => {
    if (!name) { setError("Name is required"); return; }
    setError("");
    requestCommit(doSave);
  };

  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, background: "var(--bg-overlay)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100,
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        background: "var(--bg-card)", border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-lg)", padding: "var(--space-xl)", width: 480,
        boxShadow: "var(--shadow-lg)",
      }}>
        <h2 style={{ fontFamily: "var(--font-display)", fontSize: 20, fontWeight: 600, color: "var(--text-primary)", marginBottom: "var(--space-lg)" }}>
          New Case Type
        </h2>

        <FieldGroup label="Name">
          <ModalInput value={name} onChange={setName} placeholder="e.g. Insurance Claim" />
        </FieldGroup>

        <FieldGroup label="Version">
          <ModalInput value={version} onChange={setVersion} placeholder="1.0.0" />
        </FieldGroup>

        <FieldGroup label="Description">
          <ModalInput value={description} onChange={setDescription} placeholder="Brief description…" />
        </FieldGroup>

        <FieldGroup label="Default Priority">
          <ModalSelect value={priority} onChange={setPriority}
            options={["low", "medium", "high", "critical", "blocker"]} />
        </FieldGroup>

        <FieldGroup label="Scope">
          {isAdmin ? (
            /* Admins: full tenant selector + global option */
            <ModalSelect
              value={tenantId}
              onChange={setTenantId}
              options={["", ...tenants.map((t: any) => t.id)]}
              labels={["Global — available to all tenants in this organisation", ...tenants.map((t: any) => t.name)]}
            />
          ) : (
            /* Non-admins: simple toggle — My Tenant or Global (admin approval required) */
            <ModalSelect
              value={tenantId}
              onChange={setTenantId}
              options={[user?.tenant_id ?? "", ""]}
              labels={["My Tenant (default)", "Global — available to all tenants (requires admin)"]}
            />
          )}
          <p style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4, lineHeight: 1.5 }}>
            {tenantId
              ? `Scoped to ${tenants.find((t: any) => t.id === tenantId)?.name ?? "your tenant"}. Only members of this tenant can create cases with this type.`
              : "Global case types are visible and usable across all tenants in the organisation. Only admins can edit global case types."}
          </p>
        </FieldGroup>

        <FieldGroup label="Lifecycle Process (optional)">
          <ModalSelect value={processId} onChange={setProcessId}
            options={processes.map((p: any) => p.process_id)}
            labels={processes.map((p: any) => p.name || p.process_id)}
            placeholder={processes.length ? "Select a deployed BPMN process…" : "No processes deployed — leave blank"} />
        </FieldGroup>

        {error && <p style={{ color: "var(--status-failed)", fontSize: 12, marginTop: "var(--space-sm)" }}>{error}</p>}

        <div style={{ display: "flex", gap: "var(--space-sm)", justifyContent: "flex-end", marginTop: "var(--space-lg)" }}>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button onClick={handleSubmit} disabled={commitSaving}>
            {commitSaving ? "Committing…" : "Commit Case Type"}
          </Button>
        </div>
      </div>
      <CommitModal
        open={commitOpen}
        saving={commitSaving}
        componentType="case_type"
        componentName={name || "New Case Type"}
        onCommit={handleCommit}
        onCancel={cancelCommit}
      />
    </div>
  );
}

/* ── Modal form primitives ────────────────────────────────────── */

function FieldGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: "var(--space-md)" }}>
      <label style={{
        display: "block", fontSize: 11, fontWeight: 500, color: "var(--text-muted)",
        textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)",
        marginBottom: "var(--space-xs)",
      }}>{label}</label>
      {children}
    </div>
  );
}

function ModalInput({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} style={{
      width: "100%", padding: "8px 12px", fontSize: 13, fontFamily: "var(--font-body)",
      background: "var(--bg-input)", border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none", boxSizing: "border-box",
    }}
      onFocus={(e) => (e.target.style.borderColor = "var(--border-focus)")}
      onBlur={(e) => (e.target.style.borderColor = "var(--border-default)")}
    />
  );
}

function ModalSelect({ value, onChange, options, labels, placeholder }: {
  value: string; onChange: (v: string) => void; options: string[];
  labels?: string[]; placeholder?: string;
}) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} style={{
      width: "100%", padding: "8px 12px", fontSize: 13, fontFamily: "var(--font-body)",
      background: "var(--bg-input)", border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none", boxSizing: "border-box",
    }}>
      {placeholder && <option value="">{placeholder}</option>}
      {options.map((opt, i) => (
        <option key={opt} value={opt}>{labels?.[i] || opt}</option>
      ))}
    </select>
  );
}
