/**
 * Standalone Form Builder page — lists all forms, lets you create/edit them
 * using the FormBuilder widget.
 */
import React, { useEffect, useState, useCallback } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import FormBuilder, { FormDefinition } from "./FormBuilder";
import { listForms, createForm, updateForm, deleteForm, createBranch } from "@shared/api/client";
import { useCommit, useBranchMode, useCurrentUserGroups } from "@shared/hooks";
import { CommitModal, CommitHistory, BranchModeBanner, ReviewerPicker } from "@shared/components";
import type { CommitRecord } from "@shared/components/CommitHistory";

type FormRecord = {
  id: string;
  name: string;
  version: string;
  definition_json: Record<string, unknown>;
};

const EMPTY_DEF: FormDefinition = { sections: [] };

function bumpVersion(v: string): string {
  const parts = v.split(".");
  const minor = parseInt(parts[1] ?? "0", 10);
  return `${parts[0]}.${minor + 1}`;
}

export default function FormBuilderPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const branchId = searchParams.get("branch");
  const branchMode = useBranchMode(branchId);
  const myGroups = useCurrentUserGroups();

  const [forms, setForms]       = useState<FormRecord[]>([]);
  const [loading, setLoading]   = useState(true);
  const [err, setErr]           = useState<string | null>(null);
  const [selected, setSelected] = useState<FormRecord | null>(null);
  const [def, setDef]           = useState<FormDefinition>(EMPTY_DEF);
  const [saving, setSaving]     = useState(false);
  const [saveMsg, setSaveMsg]   = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName]   = useState("");
  const [tab, setTab]           = useState<"build" | "history">("build");
  const [savedDef, setSavedDef] = useState<FormDefinition>(EMPTY_DEF);
  const [restoreBanner, setRestoreBanner] = useState<string | null>(null);
  // Branch creation inline state
  const [branchingId, setBranchingId]     = useState<string | null>(null);
  const [branchName, setBranchName]       = useState("");
  const [branchReviewer, setBranchReviewer] = useState("");
  const [branchBusy, setBranchBusy]       = useState(false);
  const [branchCreated, setBranchCreated] = useState<any>(null);
  const [branchErr, setBranchErr]         = useState<string | null>(null);

  useEffect(() => { load(); }, []);

  async function load() {
    setLoading(true); setErr(null);
    try {
      const data = await listForms();
      setForms(data.items ?? data ?? []);
    } catch (e: any) { setErr(e.message); }
    finally { setLoading(false); }
  }

  function openForm(f: FormRecord, branchSnapshot?: any) {
    setSelected(f);
    setSaveMsg(null);
    setRestoreBanner(null);
    setTab("build");
    const raw = branchSnapshot ?? f.definition_json as any;
    const parsed = raw?.sections ? (raw as FormDefinition) : EMPTY_DEF;
    setDef(parsed);
    setSavedDef(parsed);
  }

  // Auto-select the form the branch belongs to when branch loads
  useEffect(() => {
    if (!branchMode.branch?.artifact_id || !forms.length || selected) return;
    const f = forms.find(f => f.id === branchMode.branch.artifact_id);
    if (f) openForm(f, branchMode.branch.content_snapshot?.definition_json);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [branchMode.branch, forms]);

  const handleChange = useCallback((next: FormDefinition) => {
    setDef(next);
    setSaveMsg(null);
    setRestoreBanner(null);
  }, []);

  const { commitOpen, commitSaving, requestCommit, handleCommit, cancelCommit } =
    useCommit("form", selected?.id ?? "", selected?.name ?? "Form");

  async function doSave(defToSave: FormDefinition, versionOverride?: string) {
    if (!selected) return;
    if (branchMode.isBranchMode) {
      await branchMode.patchContent({ definition_json: defToSave as any });
      setSavedDef(defToSave);
      setRestoreBanner(null);
      return;
    }
    const payload: any = { definition_json: defToSave as any };
    if (versionOverride) payload.version = versionOverride;
    await updateForm(selected.id, payload);
    const nextVersion = versionOverride ?? selected.version;
    setForms(prev => prev.map(f =>
      f.id === selected.id ? { ...f, definition_json: defToSave as any, version: nextVersion } : f
    ));
    setSelected(prev => prev ? { ...prev, version: nextVersion } : prev);
    setSavedDef(defToSave);
    setRestoreBanner(null);
  }

  function makeSnapshot(beforeVersion: string, afterVersion: string, beforeDef: FormDefinition, afterDef: FormDefinition) {
    return {
      before: { _version: beforeVersion, sections: (beforeDef as any).sections ?? [] } as any,
      after:  { _version: afterVersion,  sections: (afterDef  as any).sections ?? [] } as any,
    };
  }

  function handleSave() {
    if (!selected) return;
    setSaveMsg(null);
    if (branchMode.isBranchMode) {
      doSave(def);
      return;
    }
    requestCommit(
      () => doSave(def),
      makeSnapshot(selected.version, selected.version, savedDef, def),
    );
  }

  function handleVersionCommit() {
    if (!selected) return;
    setSaveMsg(null);
    const next = bumpVersion(selected.version);
    requestCommit(
      () => doSave(def, next),
      makeSnapshot(selected.version, next, savedDef, def),
    );
  }

  function handleRestoreRequest(commit: CommitRecord) {
    const afterData = (commit.diff_snapshot as any)?.after;
    if (!afterData) return;
    const restoredDef: FormDefinition = { sections: afterData.sections ?? [] };
    const restoredVersion: string | undefined = afterData._version;
    setDef(restoredDef);
    setRestoreBanner(
      `Previewing ${restoredVersion ? `v${restoredVersion} — ` : ""}committed ${new Date(commit.committed_at).toLocaleString()}. Commit to make this current.`
    );
    setTab("build");
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    setSaving(true);
    try {
      const f = await createForm({ name: newName.trim(), version: "1.0", definition_json: EMPTY_DEF as any });
      const record: FormRecord = { id: f.id, name: f.name ?? newName.trim(), version: f.version ?? "1.0", definition_json: EMPTY_DEF as any };
      setForms(prev => [record, ...prev]);
      setNewName(""); setCreating(false);
      openForm(record);
    } catch (e: any) { setErr(e.message); }
    finally { setSaving(false); }
  }

  async function handleDelete(id: string) {
    if (!window.confirm("Delete this form?")) return;
    await deleteForm(id);
    setForms(prev => prev.filter(f => f.id !== id));
    if (selected?.id === id) { setSelected(null); setDef(EMPTY_DEF); }
  }

  function openBranchForm(f: FormRecord) {
    const slug = f.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
    setBranchingId(f.id);
    setBranchName(`fix/${slug}`);
    setBranchReviewer(""); setBranchCreated(null); setBranchErr(null);
  }

  async function handleBranchCreate(formId: string, formName: string) {
    if (!branchName.trim()) return;
    setBranchBusy(true); setBranchErr(null);
    try {
      const b = await createBranch({
        name: branchName.trim(),
        artifact_type: "form",
        artifact_id: formId,
        description: `Branch of form "${formName}"`,
        assigned_reviewer_id: branchReviewer.trim() || undefined,
      });
      setBranchCreated(b);
    } catch (e: any) { setBranchErr(e.message); }
    finally { setBranchBusy(false); }
  }

  return (
    <>
    <div style={{ display: "flex", height: "100%", overflow: "hidden" }}>

      {/* ── Left panel: form list ── */}
      <div style={{
        width: 260, flexShrink: 0, borderRight: "1px solid var(--border)",
        display: "flex", flexDirection: "column", background: "var(--bg-surface)",
      }}>
        <div style={{ padding: "16px 16px 12px", borderBottom: "1px solid var(--border)" }}>
          <div style={{ fontWeight: 700, fontSize: 14, color: "var(--text-primary)", marginBottom: 10 }}>
            Form Builder
          </div>
          {creating ? (
            <form onSubmit={handleCreate} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <input
                autoFocus value={newName} onChange={e => setNewName(e.target.value)}
                placeholder="Form name…"
                style={{ padding: "6px 8px", border: "1px solid var(--border)", borderRadius: 6, fontSize: 13, fontFamily: "inherit" }}
              />
              <div style={{ display: "flex", gap: 6 }}>
                <button type="submit" disabled={saving}
                  style={{ flex: 1, padding: "5px", fontSize: 12, fontWeight: 600, background: "var(--accent)", color: "#fff", border: "none", borderRadius: 5, cursor: "pointer" }}>
                  Create
                </button>
                <button type="button" onClick={() => setCreating(false)}
                  style={{ flex: 1, padding: "5px", fontSize: 12, background: "var(--bg-root)", border: "1px solid var(--border)", borderRadius: 5, cursor: "pointer", color: "var(--text-secondary)" }}>
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <button onClick={() => setCreating(true)}
              style={{ width: "100%", padding: "6px 0", fontSize: 12, fontWeight: 600, background: "var(--accent)", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer" }}>
              + New Form
            </button>
          )}
        </div>

        <div style={{ flex: 1, overflow: "auto", padding: 8 }}>
          {loading && <div style={{ padding: 12, color: "var(--text-muted)", fontSize: 12 }}>Loading…</div>}
          {err && <div style={{ padding: 12, color: "#ef4444", fontSize: 12 }}>{err}</div>}
          {!loading && forms.length === 0 && (
            <div style={{ padding: 12, color: "var(--text-muted)", fontSize: 12 }}>No forms yet. Create one above.</div>
          )}
          {forms.map(f => (
            <div key={f.id} style={{ marginBottom: 2 }}>
              <div
                onClick={() => openForm(f)}
                style={{
                  padding: "8px 10px", borderRadius: 6, cursor: "pointer",
                  background: selected?.id === f.id ? "var(--accent-muted, #ede9fe)" : "transparent",
                  color: selected?.id === f.id ? "var(--accent)" : "var(--text-primary)",
                  display: "flex", alignItems: "center", gap: 8,
                }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 600, fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.name}</div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>v{f.version}</div>
                </div>
                <button
                  onClick={e => { e.stopPropagation(); openBranchForm(f); }}
                  style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 11, padding: "0 3px", lineHeight: 1 }}
                  title="Create branch">⎇</button>
                <button
                  onClick={e => { e.stopPropagation(); handleDelete(f.id); }}
                  style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 14, padding: "0 2px", lineHeight: 1 }}
                  title="Delete form">✕</button>
              </div>
              {branchingId === f.id && (
                <div style={{ padding: "8px 10px", background: "var(--bg-elevated)", borderRadius: 4, marginTop: 2 }}>
                  {branchCreated ? (
                    <div style={{ fontSize: 11 }}>
                      <div style={{ padding: "4px 8px", background: "#dcfce7", color: "#16a34a", borderRadius: 4, marginBottom: 6 }}>
                        ✓ Branch <b>{branchCreated.name}</b> created
                      </div>
                      <div style={{ display: "flex", gap: 6 }}>
                        <button
                          onClick={() => navigate(`/form-builder?branch=${branchCreated.id}`)}
                          style={{ padding: "4px 10px", fontSize: 11, fontWeight: 600, background: "var(--accent)", color: "#fff", border: "none", borderRadius: 4, cursor: "pointer" }}>
                          Open in Editor →
                        </button>
                        <button
                          onClick={() => setBranchingId(null)}
                          style={{ padding: "4px 8px", fontSize: 11, background: "none", border: "1px solid var(--border)", borderRadius: 4, cursor: "pointer", color: "var(--text-secondary)" }}>
                          Close
                        </button>
                      </div>
                    </div>
                  ) : (
                    <>
                      {branchErr && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 4 }}>✗ {branchErr}</div>}
                      <input
                        style={{ width: "100%", padding: "4px 8px", border: "1px solid var(--border)", borderRadius: 4, fontSize: 11, fontFamily: "monospace", marginBottom: 4, boxSizing: "border-box" as const }}
                        placeholder="branch name"
                        value={branchName}
                        onChange={e => setBranchName(e.target.value)}
                        onClick={e => e.stopPropagation()}
                      />
                      <div onClick={e => e.stopPropagation()} style={{ marginBottom: 6 }}>
                        <ReviewerPicker
                          value={branchReviewer}
                          onChange={setBranchReviewer}
                          accessGroupId={myGroups[0]}
                          placeholder="Reviewer (optional)"
                        />
                      </div>
                      <div style={{ display: "flex", gap: 6 }}>
                        <button
                          disabled={branchBusy || !branchName.trim()}
                          onClick={e => { e.stopPropagation(); handleBranchCreate(f.id, f.name); }}
                          style={{ flex: 1, padding: "4px 0", fontSize: 11, fontWeight: 600, background: "var(--accent)", color: "#fff", border: "none", borderRadius: 4, cursor: "pointer", opacity: (branchBusy || !branchName.trim()) ? 0.6 : 1 }}>
                          {branchBusy ? "…" : "Create Branch"}
                        </button>
                        <button
                          onClick={e => { e.stopPropagation(); setBranchingId(null); }}
                          style={{ padding: "4px 8px", fontSize: 11, background: "none", border: "1px solid var(--border)", borderRadius: 4, cursor: "pointer", color: "var(--text-secondary)" }}>
                          ✕
                        </button>
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* ── Right panel ── */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {!selected ? (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-muted)", fontSize: 14 }}>
            Select a form to edit, or create a new one.
          </div>
        ) : (
          <>
            {/* Branch mode banner */}
            {branchMode.isBranchMode && (
              <BranchModeBanner
                branch={branchMode.branch}
                saving={branchMode.saving}
                error={branchMode.error}
                accessGroupId={myGroups[0]}
                onSubmitForReview={branchMode.submitForReview}
                onRecall={branchMode.recall}
              />
            )}

            {/* Toolbar */}
            <div style={{
              padding: "10px 20px", borderBottom: "1px solid var(--border)",
              display: "flex", alignItems: "center", gap: 12, background: "var(--bg-surface)",
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 700, fontSize: 14, color: "var(--text-primary)" }}>{selected.name}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 1 }}>v{selected.version}</div>
              </div>

              {/* Tabs */}
              <div style={{ display: "flex", background: "var(--bg-root)", borderRadius: 6, border: "1px solid var(--border)", overflow: "hidden" }}>
                {(["build", "history"] as const).map(t => (
                  <button key={t} onClick={() => setTab(t)} style={{
                    padding: "5px 14px", fontSize: 12, fontWeight: tab === t ? 600 : 400, border: "none",
                    cursor: "pointer", background: tab === t ? "var(--accent)" : "transparent",
                    color: tab === t ? "#fff" : "var(--text-secondary)",
                  }}>
                    {t === "build" ? "Build" : "History"}
                  </button>
                ))}
              </div>

              {saveMsg && (
                <span style={{ fontSize: 12, color: saveMsg.startsWith("Error") ? "#ef4444" : "#16a34a" }}>{saveMsg}</span>
              )}
              {branchMode.isBranchMode ? (
                <button
                  onClick={handleSave}
                  disabled={commitSaving || branchMode.saving || branchMode.isLocked || branchMode.isReadOnly}
                  style={{ padding: "6px 18px", fontSize: 13, fontWeight: 600, background: "var(--accent)", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", opacity: (commitSaving || branchMode.saving || branchMode.isLocked || branchMode.isReadOnly) ? 0.6 : 1 }}>
                  {branchMode.saving ? "Saving…" : branchMode.isLocked ? "Locked" : branchMode.isReadOnly ? "Read Only" : "Save to Branch"}
                </button>
              ) : restoreBanner ? (
                <button onClick={handleSave} disabled={commitSaving}
                  style={{ padding: "6px 18px", fontSize: 13, fontWeight: 600, background: "#ca8a04", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", opacity: commitSaving ? 0.6 : 1 }}>
                  {commitSaving ? "Restoring…" : "↩ Restore"}
                </button>
              ) : (
                <>
                  <button onClick={handleVersionCommit} disabled={commitSaving}
                    style={{ padding: "6px 14px", fontSize: 13, fontWeight: 600, background: "var(--bg-surface)", color: "var(--accent)", border: "1px solid var(--accent)", borderRadius: 6, cursor: "pointer", opacity: commitSaving ? 0.6 : 1 }}
                    title={`Save and bump to v${bumpVersion(selected.version)}`}>
                    {commitSaving ? "…" : "Version + Commit"}
                  </button>
                  <button onClick={handleSave} disabled={commitSaving}
                    style={{ padding: "6px 18px", fontSize: 13, fontWeight: 600, background: "var(--accent)", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", opacity: commitSaving ? 0.6 : 1 }}>
                    {commitSaving ? "Committing…" : "Commit"}
                  </button>
                </>
              )}
            </div>

            {tab === "build" && (
              <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
                {restoreBanner && (
                  <div style={{
                    padding: "8px 20px", background: "#fefce8", borderBottom: "1px solid #fde047",
                    fontSize: 12, color: "#854d0e", display: "flex", alignItems: "center", gap: 10,
                  }}>
                    <span style={{ flex: 1 }}>↩ {restoreBanner}</span>
                    <button onClick={() => { setDef(savedDef); setRestoreBanner(null); }}
                      style={{ background: "none", border: "1px solid #ca8a04", borderRadius: 4, padding: "2px 10px", fontSize: 11, cursor: "pointer", color: "#854d0e" }}>
                      Discard
                    </button>
                  </div>
                )}
                <div style={{ flex: 1, overflow: "auto" }}>
                  <FormBuilder definition={def} onChange={handleChange} />
                </div>
              </div>
            )}

            {tab === "history" && (
              <div style={{ flex: 1, overflow: "auto", padding: "16px 24px" }}>
                <CommitHistory
                  componentType="form"
                  componentId={selected.id}
                  onRestoreRequest={handleRestoreRequest}
                />
              </div>
            )}
          </>
        )}
      </div>
    </div>
    <CommitModal
      open={commitOpen} saving={commitSaving}
      componentType="form" componentName={selected?.name ?? "Form"}
      onCommit={handleCommit} onCancel={cancelCommit}
    />
    </>
  );
}
