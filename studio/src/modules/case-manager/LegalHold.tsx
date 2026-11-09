/**
 * Legal holds — the Studio screens (Oct item 13; API = mig 215).
 *
 * Three pieces:
 *
 *   - `LegalHoldBanner` sits above a case's tabs while it is held, so a case
 *     worker whose Delete is refused has already read why.
 *   - `LegalHoldTab` sits on a case: what the hold freezes, the holds on it,
 *     and — for whoever the server says may — placing and releasing.
 *   - `LegalHoldsPanel` (Compliance → Legal Holds) lists every hold in the
 *     tenant by matter, and releases a whole matter at once.
 *
 * The server decides everything. `can_read` / `can_place` / `can_release` come
 * from the status route and are never inferred from a role name: since Phase 6
 * an operator may grant a privilege to another role in data. Anyone who can
 * read the case is told THAT it is held; the matter and the reason are sent
 * only to holders of `legal.holds.read`, so there is nothing here to hide.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";

import {
  approveLegalRelease,
  getCaseLegalHold,
  getLegalHoldSettings,
  listLegalHolds,
  placeLegalHold,
  rejectLegalRelease,
  releaseLegalHold,
  releaseLegalMatter,
  setLegalHoldApprover,
  type CaseLegalHoldStatus,
  type LegalHold,
  type LegalHoldSettings,
} from "@shared/api/client";
import {
  Banner, Button, Card, EmptyState, ErrorNote, FIELD_STYLE, MONO, Pill,
  SectionTitle, Spinner, TextInput,
} from "@shared/components";

const REASON_MIN = 10;
const muted: React.CSSProperties = { fontSize: 12, color: "var(--text-muted)" };
const label: React.CSSProperties = {
  ...muted, fontFamily: "var(--font-mono)", textTransform: "uppercase",
  letterSpacing: "0.06em", fontSize: 11, marginBottom: 6,
};

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

// ── The case's status ────────────────────────────────────────────────────────

/**
 * Whether this case is held, and what the caller may do about it.
 *
 * `null` while loading or when the route cannot be read (an older server, a
 * case the caller may not read) — callers treat that as "show nothing", never
 * as "not held".
 */
export function useCaseLegalHold(caseId: string | null | undefined) {
  const [status, setStatus] = useState<CaseLegalHoldStatus | null>(null);
  const reload = useCallback(async () => {
    if (!caseId) { setStatus(null); return; }
    try { setStatus(await getCaseLegalHold(caseId)); }
    catch { setStatus(null); }
  }, [caseId]);
  useEffect(() => { setStatus(null); reload(); }, [reload]);
  return { status, reload };
}

export function LegalHoldBanner({ status }: { status: CaseLegalHoldStatus | null }) {
  if (!status?.held) return null;
  return (
    <Banner tone="warn" title="This case is under a legal hold">
      It can still be worked. It cannot be deleted, anonymized, erased or moved to
      another tenant until the hold is released.
    </Banner>
  );
}

// ── Releasing one hold ───────────────────────────────────────────────────────

function ReleaseForm({ what, onRelease, ask = false }: {
  what: string; onRelease: (reason: string) => Promise<void>;
  /** The tenant names an approving group: this ASKS, it does not release. */
  ask?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ok = reason.trim().length >= REASON_MIN;

  const submit = async () => {
    if (!window.confirm(ask
      ? `Ask for ${what} to be released? A member of the approving group must agree before it is. This is recorded.`
      : `Release ${what}? Retention and erasure will apply to it again. This is recorded.`)) return;
    setBusy(true); setError(null);
    try { await onRelease(reason.trim()); setOpen(false); setReason(""); }
    catch (e) { setError(errText(e)); }
    finally { setBusy(false); }
  };

  if (!open) {
    return (
      <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
        {ask ? "Ask to release…" : "Release…"}
      </Button>
    );
  }
  return (
    <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
      <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2}
                aria-label="Reason for release"
                placeholder="Why the hold is being lifted — at least 10 characters"
                style={{ ...FIELD_STYLE, width: "100%", resize: "vertical" }} />
      {error && <ErrorNote>{error}</ErrorNote>}
      {!ok && (
        <span style={muted}>
          The reason needs {REASON_MIN - reason.trim().length} more character(s).
        </span>
      )}
      <div style={{ display: "flex", gap: 8 }}>
        <Button variant="danger" size="sm" onClick={submit} disabled={!ok || busy}>
          {busy ? (ask ? "Asking…" : "Releasing…") : (ask ? "Ask for release" : "Release hold")}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => { setOpen(false); setError(null); }}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

// ── A release waiting for its second person (mig 218) ────────────────────────

function PendingRelease({ hold, onChanged }: { hold: LegalHold; onChanged: () => void }) {
  const req = hold.pending_release!;
  const [mode, setMode] = useState<"idle" | "refuse">("idle");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const noteOk = note.trim().length >= REASON_MIN;

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError(null);
    try { await fn(); setMode("idle"); setNote(""); onChanged(); }
    catch (e) { setError(errText(e)); }
    finally { setBusy(false); }
  };
  const approve = () => {
    if (!window.confirm(`Approve releasing the hold for "${hold.matter_ref}"? `
      + "Retention and erasure will apply to the case again. This is recorded.")) return;
    run(() => approveLegalRelease(hold.id));
  };

  return (
    <div style={{
      marginTop: 10, padding: "10px 12px", borderRadius: "var(--radius-sm)",
      background: "color-mix(in srgb, var(--status-running) 6%, var(--bg-elevated))",
      borderLeft: "3px solid var(--status-running)",
    }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Pill tone="warn">release awaiting approval</Pill>
        <span style={muted}>
          asked by <span style={MONO}>{req.requested_by}</span> · {when(req.requested_at)}
        </span>
      </div>
      <div style={{ marginTop: 6, fontSize: 13, color: "var(--text-primary)", whiteSpace: "pre-wrap" }}>
        {req.reason}
      </div>
      {!hold.can_approve && !hold.can_withdraw && (
        <div style={{ ...muted, marginTop: 6 }}>
          A member of the approving group, other than the person who asked, must approve it.
        </div>
      )}
      {error && <div style={{ marginTop: 8 }}><ErrorNote>{error}</ErrorNote></div>}
      {mode === "idle" && (hold.can_approve || hold.can_withdraw) && (
        <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          {hold.can_approve && (
            <Button variant="danger" size="sm" onClick={approve} disabled={busy}>
              {busy ? "Approving…" : "Approve release"}
            </Button>
          )}
          <Button variant="secondary" size="sm" onClick={() => setMode("refuse")} disabled={busy}>
            {hold.can_withdraw ? "Withdraw request…" : "Refuse…"}
          </Button>
        </div>
      )}
      {mode === "refuse" && (
        <div style={{ display: "grid", gap: 8, marginTop: 10 }}>
          <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2}
                    aria-label={hold.can_withdraw ? "Reason for withdrawing" : "Reason for refusing"}
                    placeholder="Why — at least 10 characters"
                    style={{ ...FIELD_STYLE, width: "100%", resize: "vertical" }} />
          {!noteOk && (
            <span style={muted}>The reason needs {REASON_MIN - note.trim().length} more character(s).</span>
          )}
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" variant="secondary" disabled={!noteOk || busy}
                    onClick={() => run(() => rejectLegalRelease(hold.id, note.trim()))}>
              {hold.can_withdraw ? "Withdraw" : "Refuse release"}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => { setMode("idle"); setError(null); }}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function HoldCard({ hold, canRelease, onChanged, showCase }: {
  hold: LegalHold; canRelease: boolean; onChanged: () => void; showCase?: boolean;
}) {
  const active = hold.status === "active";
  return (
    <Card style={{ marginBottom: "var(--space-md)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <Pill tone={active ? "warn" : "muted"}>{active ? "active" : "released"}</Pill>
        <b style={{ color: "var(--text-primary)" }}>{hold.matter_ref}</b>
        {showCase && (
          <span style={{ ...MONO, color: "var(--text-muted)" }}>
            {hold.case_number ?? hold.case_id}
          </span>
        )}
      </div>
      <div style={{ marginTop: 8, fontSize: 13, color: "var(--text-primary)", whiteSpace: "pre-wrap" }}>
        {hold.reason}
      </div>
      <div style={{ ...muted, marginTop: 8 }}>
        Placed by <span style={MONO}>{hold.placed_by}</span> · {when(hold.placed_at)}
      </div>
      {!active && (
        <div style={{ ...muted, marginTop: 4 }}>
          Released by <span style={MONO}>{hold.released_by}</span> · {when(hold.released_at)}
          {hold.release_reason ? ` — ${hold.release_reason}` : ""}
        </div>
      )}
      {active && hold.pending_release && <PendingRelease hold={hold} onChanged={onChanged} />}
      {active && canRelease && !hold.pending_release && (
        <div style={{ marginTop: 10 }}>
          <ReleaseForm what={`the hold for "${hold.matter_ref}"`} ask={!!hold.approval_required}
                       onRelease={async (reason) => {
            await releaseLegalHold(hold.id, reason);
            onChanged();
          }} />
        </div>
      )}
    </Card>
  );
}

// ── On a case ────────────────────────────────────────────────────────────────

export function LegalHoldTab({ caseId, status, onChanged }: {
  caseId: string; status: CaseLegalHoldStatus | null; onChanged: () => void;
}) {
  const [matter, setMatter] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!status) return <div style={muted}><Spinner size={12} /> Loading…</div>;

  const canSubmit = matter.trim().length > 0 && reason.trim().length >= REASON_MIN && !busy;
  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const out = await placeLegalHold([caseId], matter.trim(), reason.trim());
      const first = out.results[0];
      if (!first?.ok) { setError(first?.error ?? "The hold was not placed."); return; }
      setMatter(""); setReason(""); onChanged();
    } catch (e) { setError(errText(e)); }
    finally { setBusy(false); }
  };

  const holds = status.holds ?? [];
  return (
    <div style={{ display: "grid", gap: "var(--space-md)", maxWidth: 720 }}>
      {status.held ? (
        <Banner tone="warn" title="Under a legal hold">
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {status.frozen.map((line) => <li key={line}>{line}</li>)}
          </ul>
        </Banner>
      ) : (
        <Banner tone="muted" title="Not under a legal hold">
          A hold freezes this case's records while a dispute or inquiry is open. The
          case can still be worked; it cannot be deleted, anonymized, erased or moved.
        </Banner>
      )}

      {!!status.other_active_holds && (
        <div style={muted}>
          {status.other_active_holds} more active hold(s) on this case belong to another
          tenant and are not shown.
        </div>
      )}

      {status.can_read && holds.length > 0 && (
        <div>
          <SectionTitle>Holds on this case</SectionTitle>
          {holds.map((h) => (
            <HoldCard key={h.id} hold={h} canRelease={status.can_release} onChanged={onChanged} />
          ))}
        </div>
      )}

      {status.can_place && status.held && (
        <div style={muted}>
          A case takes one legal hold at a time. Release this one before placing another.
        </div>
      )}

      {status.can_place && !status.held && (
        <div style={{ display: "grid", gap: 10 }}>
          <SectionTitle>Place a hold</SectionTitle>
          <div>
            <div style={label}>Matter — the lawsuit, inquiry or reference</div>
            <TextInput value={matter} onChange={(e) => setMatter(e.target.value)}
                       aria-label="Matter" maxLength={120}
                       placeholder="e.g. Claim 2026/114" style={{ width: "100%" }} />
          </div>
          <div>
            <div style={label}>Reason</div>
            <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3}
                      aria-label="Reason" placeholder="At least 10 characters"
                      style={{ ...FIELD_STYLE, width: "100%", resize: "vertical" }} />
          </div>
          {error && <ErrorNote>{error}</ErrorNote>}
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <Button onClick={submit} disabled={!canSubmit}>
              {busy ? "Placing…" : "Place legal hold"}
            </Button>
            {!canSubmit && !busy && (
              <span style={muted}>
                {matter.trim().length === 0
                  ? "Enter the matter first."
                  : `The reason needs ${REASON_MIN - reason.trim().length} more character(s).`}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Release approval setting (mig 218) ───────────────────────────────────────

export function ReleaseApprovalSettings() {
  const [s, setS] = useState<LegalHoldSettings | null>(null);
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const out = await getLegalHoldSettings();
      setS(out); setPick(out.approver_group_id ?? "");
    } catch { setS(null); }        // older server, or no legal.holds.read: show nothing
  }, []);
  useEffect(() => { load(); }, [load]);

  if (!s || !s.available) return null;
  const named = s.approver_group;
  const save = async () => {
    setBusy(true); setError(null);
    try { const out = await setLegalHoldApprover(pick || null); setS(out); setPick(out.approver_group_id ?? ""); }
    catch (e) { setError(errText(e)); }
    finally { setBusy(false); }
  };

  return (
    <Card style={{ marginTop: "var(--space-md)" }}>
      <SectionTitle>Release approval</SectionTitle>
      {s.approver_group_id ? (
        <div style={{ fontSize: 13, color: "var(--text-primary)" }}>
          A release is asked for, then approved by a member of{" "}
          <b>{named?.name ?? "a group that no longer exists"}</b> other than the person who asked.
        </div>
      ) : (
        <div style={{ fontSize: 13, color: "var(--text-primary)" }}>
          Off — a holder of the release permission releases a hold alone.
        </div>
      )}
      {s.approver_group_unusable && (
        <div style={{ marginTop: 8 }}>
          <Banner tone="bad" title="The approving group cannot approve">
            It has been deleted or switched off, so releases wait. A member of it, or the
            platform superadmin, can name another group.
          </Banner>
        </div>
      )}
      {s.can_configure && s.can_change && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 12, flexWrap: "wrap" }}>
          <select value={pick} onChange={(e) => setPick(e.target.value)} aria-label="Approving group"
                  style={{ ...FIELD_STYLE, minWidth: 240 }}>
            <option value="">Off — no second person</option>
            {s.groups.filter((g) => g.is_active || g.id === s.approver_group_id).map((g) => (
              <option key={g.id} value={g.id}>{g.name} · {g.members} member{g.members === 1 ? "" : "s"}</option>
            ))}
          </select>
          <Button size="sm" onClick={save} disabled={busy || pick === (s.approver_group_id ?? "")}>
            {busy ? "Saving…" : "Save"}
          </Button>
        </div>
      )}
      {s.can_configure && !s.can_change && (
        <div style={{ ...muted, marginTop: 8 }}>
          Only a member of the approving group, or the platform superadmin, can change or turn
          this off — so the people it binds cannot switch it off.
        </div>
      )}
      {error && <div style={{ marginTop: 8 }}><ErrorNote>{error}</ErrorNote></div>}
    </Card>
  );
}

// ── Compliance → Legal Holds ─────────────────────────────────────────────────

export default function LegalHoldsPanel() {
  const [holds, setHolds] = useState<LegalHold[] | null>(null);
  const [frozen, setFrozen] = useState<string[]>([]);
  const [canRelease, setCanRelease] = useState(false);
  const [show, setShow] = useState<"active" | "released">("active");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const out = await listLegalHolds({ status: show });
      setHolds(out.holds); setFrozen(out.frozen); setCanRelease(!!out.can_release);
    } catch (e) { setHolds([]); setError(errText(e)); }
  }, [show]);
  useEffect(() => { setHolds(null); load(); }, [load]);

  const matters = useMemo(() => {
    const by = new Map<string, LegalHold[]>();
    for (const h of holds ?? []) by.set(h.matter_ref, [...(by.get(h.matter_ref) ?? []), h]);
    return [...by.entries()];
  }, [holds]);

  return (
    <div style={{ maxWidth: 860 }}>
      <Banner tone="muted" title="Legal holds">
        A hold freezes a case's records while a dispute or inquiry is open. Place one
        from the case's <b>Legal hold</b> tab.
        {frozen.length > 0 && (
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {frozen.map((line) => <li key={line}>{line}</li>)}
          </ul>
        )}
      </Banner>

      <ReleaseApprovalSettings />

      <div style={{ display: "flex", gap: 8, margin: "var(--space-md) 0" }}>
        {(["active", "released"] as const).map((s) => (
          <Button key={s} size="sm" variant={show === s ? "primary" : "secondary"}
                  onClick={() => setShow(s)}>
            {s === "active" ? "Active" : "Released"}
          </Button>
        ))}
      </div>

      {notice && <div style={{ ...muted, margin: "var(--space-md) 0" }}>{notice}</div>}
      {error && <ErrorNote>{error}</ErrorNote>}
      {holds === null && <div style={muted}><Spinner size={12} /> Loading…</div>}
      {holds !== null && !error && holds.length === 0 && (
        <EmptyState title={show === "active" ? "No case is under a legal hold" : "No released holds"}
                    description={show === "active"
                      ? "Nothing in this tenant is frozen."
                      : "A released hold stays here as the record that it existed."} />
      )}

      {matters.map(([matter, rows]) => (
        <div key={matter} style={{ marginBottom: "var(--space-lg)" }}>
          <SectionTitle right={show === "active" && canRelease && rows.length > 1 ? (
            <ReleaseForm what={`all ${rows.length} holds for "${matter}"`}
                         ask={rows.some((h) => h.approval_required)}
                         onRelease={async (reason) => {
                           const out = await releaseLegalMatter(matter, reason);
                           setNotice(out.pending_approval
                             ? `${out.pending_approval} release(s) are waiting for approval.` : null);
                           load();
                         }} />
          ) : undefined}>
            {matter} <span style={muted}>· {rows.length} case{rows.length === 1 ? "" : "s"}</span>
          </SectionTitle>
          {rows.map((h) => (
            <HoldCard key={h.id} hold={h} canRelease={canRelease} onChanged={load} showCase />
          ))}
        </div>
      ))}
    </div>
  );
}
