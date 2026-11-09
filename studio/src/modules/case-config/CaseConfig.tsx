/**
 * Authoring the status set and the action configuration (migration 198).
 *
 * Design: docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md
 *
 * The split this screen makes visible: **statuses are yours, actions are ours.**
 * The status half is a full editor — names, labels, categories, terminality,
 * transitions. The action half offers a label, a switch, an order and the
 * statuses an action is offered from, and deliberately offers no way to change
 * what an action *does*. `escalate` fires an escalation tree; that is behaviour,
 * and behaviour is code.
 *
 * Two things the UI must not soften:
 *
 * - **Disabling an action makes it unreachable through the API**, not merely
 *   hidden here. The copy says so, because an operator who thinks they have only
 *   hidden a button will be surprised.
 * - **A status a case is sitting on cannot be deleted.** The server refuses;
 *   this explains the alternative rather than showing a bare error.
 */
import React, { useCallback, useEffect, useState } from "react";
import { Button, Card, EmptyState, Spinner, Tabs } from "@shared/components";
import { caseConfigRequest } from "@shared/api/client";

interface StatusRow {
  name: string;
  label: string;
  category: "open" | "working" | "waiting" | "terminal";
  is_terminal: boolean;
  is_default: boolean;
  allowed_from: string[];
  sort_order: number;
  enabled: boolean;
}

interface ActionRow {
  name: string;
  label: string;
  privilege: string;
  summary: string;
  effects: string[];
  destructive: boolean;
  required_params: string[];
  allowed_from: string[];
  enabled: boolean;
  configured: boolean;
  sort_order: number;
}

const CATEGORY_HELP: Record<string, string> = {
  open: "Accepted, not being worked yet.",
  working: "Someone is working it now.",
  waiting: "Waiting on someone else — SLAs are PAUSED on this category.",
  terminal: "Ends the case. SLAs are cancelled and the lifecycle workflow exits.",
};

export default function CaseConfig() {
  const [tab, setTab] = useState<"statuses" | "actions">("statuses");
  const [statuses, setStatuses] = useState<StatusRow[]>([]);
  const [actions, setActions] = useState<ActionRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Adding a status is the whole point of the screen being editable — without
  // it a tenant can only rename what the platform already seeded.
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState<StatusRow>({
    name: "", label: "", category: "open", is_terminal: false,
    is_default: false, allowed_from: [], sort_order: 500, enabled: true,
  });

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, a] = await Promise.all([
        caseConfigRequest<{ statuses: StatusRow[] }>("/case-config/statuses"),
        caseConfigRequest<{ actions: ActionRow[] }>("/case-config/actions"),
      ]);
      setStatuses(s.statuses);
      setActions(a.actions);
    } catch (e: any) {
      setError(e?.message || "Could not load the configuration");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function saveStatus(row: StatusRow) {
    setNotice(null);
    try {
      await caseConfigRequest("/case-config/statuses", {
        method: "PUT", body: JSON.stringify(row),
      });
      setNotice(`Saved “${row.label}”.`);
      load();
    } catch (e: any) {
      setError(e?.message || "Could not save");
    }
  }

  async function deleteStatus(name: string) {
    setNotice(null);
    try {
      await caseConfigRequest(`/case-config/statuses/${encodeURIComponent(name)}`,
                              { method: "DELETE" });
      setNotice(`Removed “${name}”.`);
      load();
    } catch (e: any) {
      // The server refuses (409) while any case is sitting on the status —
      // deleting would strand those cases with no label and no transition
      // graph. Surface the reason rather than a bare failure.
      setError(e?.message || "Could not remove that status");
    }
  }

  async function saveAction(row: ActionRow) {
    setNotice(null);
    try {
      await caseConfigRequest("/case-config/actions", {
        method: "PUT",
        body: JSON.stringify({
          action: row.name, label: row.label, enabled: row.enabled,
          sort_order: row.sort_order, allowed_from: row.allowed_from,
        }),
      });
      setNotice(`Saved “${row.label}”.`);
      load();
    } catch (e: any) {
      setError(e?.message || "Could not save");
    }
  }

  if (loading) return <Spinner />;

  return (
    <div style={{ padding: "var(--space-lg)" }}>
      {/* No PageHeader: AppLayout renders one from nav-data. */}
      {error && (
        <Card style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)",
                       borderColor: "var(--danger)" }}>
          <span style={{ color: "var(--danger)", fontSize: 13 }}>{error}</span>
        </Card>
      )}
      {notice && (
        <p style={{ fontSize: 12, color: "var(--text-muted)" }}>{notice}</p>
      )}

      <Tabs
        tabs={[
          { key: "statuses", label: "Statuses", badge: statuses.length },
          { key: "actions", label: "Actions", badge: actions.length },
        ] as const}
        value={tab}
        onChange={setTab}
        ariaLabel="Case configuration"
      />

      {tab === "statuses" && (
        <>
          <div style={{ display: "flex", justifyContent: "flex-end",
                        margin: "var(--space-md) 0 0" }}>
            <Button size="sm" onClick={() => { setAdding(a => !a); setNotice(null); }}>
              {adding ? "Cancel" : "+ New status"}
            </Button>
          </div>

          {adding && (
            <Card style={{ padding: "var(--space-md)", marginTop: "var(--space-sm)",
                           borderColor: "var(--accent)" }}>
              <div style={{ display: "flex", gap: "var(--space-md)",
                            alignItems: "center", flexWrap: "wrap" }}>
                <input
                  value={draft.name}
                  placeholder="name (letters, digits, _)"
                  onChange={(e) => setDraft(d => ({ ...d, name: e.target.value }))}
                  style={{ padding: "4px 8px", fontSize: 13, width: 180 }} />
                <input
                  value={draft.label}
                  placeholder="Label an operator sees"
                  onChange={(e) => setDraft(d => ({ ...d, label: e.target.value }))}
                  style={{ padding: "4px 8px", fontSize: 13, width: 200 }} />
                <select value={draft.category}
                        onChange={(e) => setDraft(d => ({ ...d, category: e.target.value as any }))}
                        style={{ padding: "4px 8px", fontSize: 13 }}>
                  {Object.keys(CATEGORY_HELP).map(c => <option key={c} value={c}>{c}</option>)}
                </select>
                <label style={{ fontSize: 12, display: "flex", gap: 4, alignItems: "center" }}>
                  <input type="checkbox" checked={draft.is_terminal}
                         onChange={(e) => setDraft(d => ({ ...d, is_terminal: e.target.checked }))} />
                  ends the case
                </label>
                <input
                  value={draft.allowed_from.join(", ")}
                  placeholder="reachable from (blank = any)"
                  onChange={(e) => setDraft(d => ({
                    ...d,
                    allowed_from: e.target.value.split(",").map(v => v.trim()).filter(Boolean),
                  }))}
                  style={{ padding: "4px 8px", fontSize: 12, flex: 1, minWidth: 200 }} />
                <Button size="sm"
                        disabled={!draft.name.trim() || !draft.label.trim()}
                        onClick={async () => {
                          await saveStatus(draft);
                          setAdding(false);
                          setDraft({ name: "", label: "", category: "open",
                                     is_terminal: false, is_default: false,
                                     allowed_from: [], sort_order: 500, enabled: true });
                        }}>
                  Add
                </Button>
              </div>
              <p style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                {CATEGORY_HELP[draft.category]}
              </p>
            </Card>
          )}

          <p style={{ fontSize: 12, color: "var(--text-muted)", margin: "var(--space-md) 0" }}>
            These are yours. Add a status, rename one, decide which end a case and
            which transitions are legal. <strong>An empty “reachable from” means
            from any status</strong>, which is how every status ships — tightening
            the graph is your decision. A status a case is currently sitting on
            cannot be deleted; switch it off instead, and the cases holding it keep
            working.
          </p>

          {statuses.length === 0 && <EmptyState title="No statuses" description="" />}

          {statuses.map((s) => (
            <Card key={s.name} style={{ padding: "var(--space-md)", marginBottom: "var(--space-sm)" }}>
              <div style={{ display: "flex", gap: "var(--space-md)", alignItems: "center", flexWrap: "wrap" }}>
                <code style={{ fontFamily: "var(--font-mono)", fontSize: 12, minWidth: 140 }}>{s.name}</code>
                <input
                  value={s.label}
                  onChange={(e) => setStatuses(prev => prev.map(x =>
                    x.name === s.name ? { ...x, label: e.target.value } : x))}
                  style={{ padding: "4px 8px", fontSize: 13, width: 180 }}
                />
                <select
                  value={s.category}
                  onChange={(e) => setStatuses(prev => prev.map(x =>
                    x.name === s.name ? { ...x, category: e.target.value as any } : x))}
                  style={{ padding: "4px 8px", fontSize: 13 }}
                >
                  {Object.keys(CATEGORY_HELP).map(c => <option key={c} value={c}>{c}</option>)}
                </select>
                <label style={{ fontSize: 12, display: "flex", gap: 4, alignItems: "center" }}>
                  <input type="checkbox" checked={s.is_terminal}
                         onChange={(e) => setStatuses(prev => prev.map(x =>
                           x.name === s.name ? { ...x, is_terminal: e.target.checked } : x))} />
                  ends the case
                </label>
                <label style={{ fontSize: 12, display: "flex", gap: 4, alignItems: "center" }}>
                  <input type="checkbox" checked={s.enabled}
                         onChange={(e) => setStatuses(prev => prev.map(x =>
                           x.name === s.name ? { ...x, enabled: e.target.checked } : x))} />
                  offered
                </label>
                <input
                  value={s.allowed_from.join(", ")}
                  placeholder="reachable from (blank = any)"
                  onChange={(e) => setStatuses(prev => prev.map(x =>
                    x.name === s.name
                      ? { ...x, allowed_from: e.target.value.split(",").map(v => v.trim()).filter(Boolean) }
                      : x))}
                  style={{ padding: "4px 8px", fontSize: 12, flex: 1, minWidth: 200 }}
                />
                <Button size="sm" onClick={() => saveStatus(s)}>Save</Button>
                <Button size="sm" variant="secondary"
                        onClick={() => deleteStatus(s.name)}>Remove</Button>
              </div>
              <p style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                {CATEGORY_HELP[s.category]}
              </p>
            </Card>
          ))}
        </>
      )}

      {tab === "actions" && (
        <>
          <p style={{ fontSize: 12, color: "var(--text-muted)", margin: "var(--space-md) 0" }}>
            These are ours. You can rename an action, hide it, reorder it and say
            which statuses it is offered from — but not change what it does, because
            what an action does is code. <strong>Switching one off makes it
            unreachable through the API, not merely hidden here.</strong> New
            behaviour is a rule or an intent fragment, not a new action.
          </p>

          <p style={{ fontSize: 12, color: "var(--text-muted)",
                      margin: "0 0 var(--space-md)" }}>
            <strong>There is deliberately no “new action” button.</strong> An
            action is behaviour — <code>escalate</code> fires an escalation tree
            and notifies — and that cannot be inferred from a name you type here.
            The list below is the whole vocabulary.
          </p>

          {actions.map((a) => (
            <Card key={a.name} style={{ padding: "var(--space-md)", marginBottom: "var(--space-sm)" }}>
              <div style={{ display: "flex", gap: "var(--space-md)", alignItems: "center", flexWrap: "wrap" }}>
                <code style={{ fontFamily: "var(--font-mono)", fontSize: 12, minWidth: 140 }}>{a.name}</code>
                <input
                  value={a.label}
                  onChange={(e) => setActions(prev => prev.map(x =>
                    x.name === a.name ? { ...x, label: e.target.value } : x))}
                  style={{ padding: "4px 8px", fontSize: 13, width: 180 }}
                />
                <label style={{ fontSize: 12, display: "flex", gap: 4, alignItems: "center" }}>
                  <input type="checkbox" checked={a.enabled}
                         onChange={(e) => setActions(prev => prev.map(x =>
                           x.name === a.name ? { ...x, enabled: e.target.checked } : x))} />
                  offered
                </label>
                <input
                  value={a.allowed_from.join(", ")}
                  placeholder="offered from (blank = any status)"
                  onChange={(e) => setActions(prev => prev.map(x =>
                    x.name === a.name
                      ? { ...x, allowed_from: e.target.value.split(",").map(v => v.trim()).filter(Boolean) }
                      : x))}
                  style={{ padding: "4px 8px", fontSize: 12, flex: 1, minWidth: 200 }}
                />
                <Button size="sm" onClick={() => saveAction(a)}>Save</Button>
              </div>
              <p style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                {a.summary} · gated by <code>{a.privilege}</code>
                {a.effects.length > 0 && <> · affects: {a.effects.join(", ")}</>}
                {a.required_params.length > 0 && <> · needs: {a.required_params.join(", ")}</>}
              </p>
            </Card>
          ))}
        </>
      )}
    </div>
  );
}
