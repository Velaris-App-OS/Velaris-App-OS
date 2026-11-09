/**
 * Enterprise → Audit stream.
 *
 * Where this platform's security events LEAVE it. Three things shape the
 * screen, and each of them is a rule from the API rather than a preference:
 *
 *  * **whose events.** The platform operator's target receives every tenant's
 *    events; a tenant admin's target receives its own tenant's only
 *    (`security_events.tenant_id`, migration 230, 2026-11-04). The API refuses a create that
 *    has not acknowledged that, and this screen makes the acknowledgement a
 *    sentence somebody reads rather than a checkbox they flick.
 *  * **creating is not enabling.** A target is created, looked at, tested, and
 *    only then switched on — an endpoint that starts receiving security events
 *    the moment a URL is pasted is a decision nobody made yet.
 *  * **the token is absent, never masked.** A masked field invites the client
 *    to send the stars back, and then the stars are the secret. A saved target
 *    shows only whether it HAS one.
 */
import React, { useCallback, useEffect, useState } from "react";
import { usePermissions } from "@/auth/PermissionsContext";
import {
  AuditStreamTarget,
  createAuditStreamTarget, deleteAuditStreamTarget, listAuditStreamTargets,
  runAuditStreamTarget, testAuditStreamTarget, updateAuditStreamTarget,
} from "@shared/api/client";
import { Button, Card, EmptyState, Spinner, TimeAgo } from "@shared/components";

const SEVERITIES = ["", "info", "warning", "error", "critical"];

function fmt(s: string | null) {
  return s ? new Date(s).toLocaleString() : "—";
}

export default function AuditStreamTab() {
  const { isOperator } = usePermissions();
  const [targets, setTargets] = useState<AuditStreamTarget[]>([]);
  const [kinds, setKinds] = useState<string[]>(["splunk_hec"]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<{ id: string; ok: boolean; text: string } | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listAuditStreamTargets();
      setTargets(data.items || []);
      if (data.kinds?.length) setKinds(data.kinds);
      setError(null);
    } catch (e: any) {
      setError(e?.message || "Could not load audit stream targets");
    }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);

  const act = async (id: string, what: "test" | "run") => {
    setBusy(id); setResult(null);
    try {
      const out = what === "test"
        ? await testAuditStreamTarget(id)
        : await runAuditStreamTarget(id);
      setResult({
        id, ok: !!out.ok,
        text: out.ok
          ? (what === "test"
              ? (out.message || "The SIEM accepted a test event.")
              : `Forwarded ${out.sent ?? 0} event(s).`)
          : (out.error || "The SIEM did not accept it."),
      });
    } catch (e: any) {
      setResult({ id, ok: false, text: e?.message || "Failed" });
    }
    setBusy(null);
    await load();
  };

  const toggle = async (t: AuditStreamTarget) => {
    if (!t.enabled && !window.confirm(
      `Switch on "${t.name}"?\n\n` +
      (isOperator ? "Every tenant's security events on this platform " : "Your tenant's security events ") +
      `will start being forwarded to ${t.endpoint_url} on the next scheduled run.`)) return;
    setBusy(t.id);
    try {
      await updateAuditStreamTarget(t.id, { enabled: !t.enabled });
    } catch (e: any) {
      setError(e?.message || "Could not change the target");
    }
    setBusy(null);
    await load();
  };

  const remove = async (t: AuditStreamTarget) => {
    if (!window.confirm(`Delete "${t.name}"? Forwarding to it stops immediately.`)) return;
    setBusy(t.id);
    try {
      await deleteAuditStreamTarget(t.id);
    } catch (e: any) {
      setError(e?.message || "Could not delete the target");
    }
    setBusy(null);
    await load();
  };

  if (loading) return <Spinner size={28} />;

  return (
    <>
      <Card>
        <div style={{ fontSize: 13, lineHeight: 1.6, color: "var(--text-secondary)" }}>
          Velaris forwards its own security events — every sign-in, every
          authorization decision, every configuration change — to your SIEM. It
          <strong> reads them after they are written</strong>, on a schedule, so a
          SIEM outage can never become a sign-in outage. Nothing is forwarded
          until a target exists <em>and</em> is switched on.
          <br /><br />
          Forwarding runs when a <strong>scheduler action of kind{" "}
          <code>audit_stream</code></strong> does. Without one, a target sits
          enabled and quiet — use <em>Forward now</em> to prove it works.
        </div>
      </Card>

      {error && (
        <Card><div style={{ color: "var(--status-failed)" }}>{error}</div></Card>
      )}

      {targets.length === 0 && !creating && (
        <EmptyState
          title="No audit stream target"
          description="Your security events stay in this platform's database until you add one."
          action={<Button onClick={() => setCreating(true)}>Add a target</Button>}
        />
      )}

      {targets.map(t => (
        <Card key={t.id}>
          <div style={{ display: "flex", alignItems: "flex-start", gap: 16, flexWrap: "wrap" }}>
            <div style={{ flex: 1, minWidth: 260 }}>
              <div style={{ fontWeight: 700, fontSize: 15 }}>
                {t.name}{" "}
                <span style={{
                  fontSize: 11, fontWeight: 700, padding: "2px 8px", borderRadius: 10,
                  background: t.enabled ? "var(--status-completed)22" : "var(--border)",
                  color: t.enabled ? "var(--status-completed)" : "var(--text-secondary)",
                }}>{t.enabled ? "forwarding" : "off"}</span>
              </div>
              <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>
                {t.kind} · <code>{t.endpoint_url}</code>
              </div>
              <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>
                {t.has_token ? "Token stored" : "No token"} ·{" "}
                {t.verify_tls ? "TLS verified" : "TLS verification off"} ·{" "}
                severity ≥ {t.min_severity || "everything"} ·{" "}
                {t.event_types.length ? `${t.event_types.length} event type(s)` : "all event types"}
              </div>
              <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>
                {t.forwarded_count} forwarded · last success {fmt(t.last_success_at)}
                {t.last_success_at && <> (<TimeAgo date={t.last_success_at} />)</>}
              </div>
              {t.last_error && (
                <div style={{ fontSize: 12, color: "var(--status-failed)", marginTop: 6 }}>
                  Last error {fmt(t.last_error_at)}: {t.last_error}
                </div>
              )}
              {result?.id === t.id && (
                <div style={{
                  fontSize: 12, marginTop: 8,
                  color: result.ok ? "var(--status-completed)" : "var(--status-failed)",
                }}>{result.text}</div>
              )}
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Button variant="secondary" disabled={busy === t.id}
                      onClick={() => act(t.id, "test")}>Send test event</Button>
              <Button variant="secondary" disabled={busy === t.id || !t.enabled}
                      onClick={() => act(t.id, "run")}>Forward now</Button>
              <Button variant="secondary" disabled={busy === t.id}
                      onClick={() => toggle(t)}>{t.enabled ? "Switch off" : "Switch on"}</Button>
              <Button variant="danger" disabled={busy === t.id}
                      onClick={() => remove(t)}>Delete</Button>
            </div>
          </div>
        </Card>
      ))}

      {targets.length > 0 && !creating && (
        <Button onClick={() => setCreating(true)}>Add a target</Button>
      )}

      {creating && (
        <CreateTarget
          kinds={kinds}
          onCancel={() => setCreating(false)}
          onCreated={async () => { setCreating(false); await load(); }}
        />
      )}
    </>
  );
}

function CreateTarget({ kinds, onCancel, onCreated }: {
  kinds: string[]; onCancel: () => void; onCreated: () => void;
}) {
  const [form, setForm] = useState({
    name: "", endpoint_url: "", token: "", kind: kinds[0] || "splunk_hec",
    verify_tls: true, min_severity: "", event_types: "",
  });
  const { isOperator } = usePermissions();
  // Only the operator's (platform-wide) target needs the acknowledgement.
  const [ackChecked, setAck] = useState(false);
  const ack = !isOperator || ackChecked;
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setSaving(true); setError(null);
    try {
      await createAuditStreamTarget({
        name: form.name.trim(),
        endpoint_url: form.endpoint_url.trim(),
        token: form.token ? form.token : null,
        kind: form.kind,
        verify_tls: form.verify_tls,
        min_severity: form.min_severity || null,
        event_types: form.event_types
          ? form.event_types.split(",").map(x => x.trim()).filter(Boolean)
          : null,
        acknowledge_platform_scope: ack,
      });
      onCreated();
    } catch (e: any) {
      setError(e?.message || "Could not create the target");
    }
    setSaving(false);
  };

  const field: React.CSSProperties = {
    width: "100%", padding: "7px 11px", border: "1px solid var(--border)",
    borderRadius: 6, fontSize: 13, boxSizing: "border-box",
    background: "var(--bg-main)", color: "var(--text-primary)",
  };
  const label: React.CSSProperties = {
    fontSize: 12, fontWeight: 600, color: "var(--text-secondary)",
    marginBottom: 4, marginTop: 12, display: "block",
  };

  return (
    <Card>
      <div style={{ fontWeight: 700, fontSize: 15 }}>New audit stream target</div>

      <label style={label}>Name</label>
      <input style={field} value={form.name}
             onChange={e => setForm({ ...form, name: e.target.value })}
             placeholder="Corporate Splunk" />

      <label style={label}>HEC endpoint</label>
      <input style={field} value={form.endpoint_url}
             onChange={e => setForm({ ...form, endpoint_url: e.target.value })}
             placeholder="https://splunk.internal:8088/services/collector" />
      <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 4 }}>
        https only. If the SIEM uses an internal certificate, turn TLS
        verification off below — do not fall back to plain http, which would put
        the token and every event on the wire in clear.
      </div>

      <label style={label}>HEC token</label>
      <input style={field} type="password" value={form.token}
             onChange={e => setForm({ ...form, token: e.target.value })}
             placeholder="stored encrypted; never shown again" />

      <label style={label}>Transport</label>
      <select style={field} value={form.kind}
              onChange={e => setForm({ ...form, kind: e.target.value })}>
        {kinds.map(k => <option key={k} value={k}>{k}</option>)}
      </select>

      <label style={label}>Minimum severity</label>
      <select style={field} value={form.min_severity}
              onChange={e => setForm({ ...form, min_severity: e.target.value })}>
        {SEVERITIES.map(s => (
          <option key={s || "all"} value={s}>{s || "everything (recommended)"}</option>
        ))}
      </select>
      <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 4 }}>
        A SOC that receives too much can filter. One that quietly receives too
        little cannot know.
      </div>

      <label style={label}>Event types (comma separated, blank for all)</label>
      <input style={field} value={form.event_types}
             onChange={e => setForm({ ...form, event_types: e.target.value })}
             placeholder="auth.login, authz.denied" />

      <label style={{ ...label, display: "flex", alignItems: "center", gap: 8 }}>
        <input type="checkbox" checked={form.verify_tls}
               onChange={e => setForm({ ...form, verify_tls: e.target.checked })} />
        Verify the SIEM's TLS certificate
      </label>

      <div style={{
        marginTop: 16, padding: "12px 14px", borderRadius: 6,
        border: "1px solid var(--border)", background: "var(--bg-main)",
      }}>
        {isOperator ? (
          <label style={{ display: "flex", gap: 10, alignItems: "flex-start", fontSize: 13 }}>
            <input type="checkbox" checked={ackChecked} onChange={e => setAck(e.target.checked)} />
            <span>
              I understand this stream carries <strong>every tenant's</strong>
              {" "}security events — the whole platform's.
            </span>
          </label>
        ) : (
          <div style={{ fontSize: 13 }}>
            This stream carries <strong>your tenant's</strong> security events only.
            The endpoint must be https on the internet.
          </div>
        )}
      </div>

      {error && (
        <div style={{ color: "var(--status-failed)", fontSize: 13, marginTop: 12 }}>{error}</div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
        <Button disabled={saving || !ack || !form.name.trim() || !form.endpoint_url.trim()}
                onClick={save}>Create (switched off)</Button>
        <Button variant="secondary" disabled={saving} onClick={onCancel}>Cancel</Button>
      </div>
      <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 8 }}>
        A new target is created switched off. Send it a test event, then switch
        it on.
      </div>
    </Card>
  );
}
