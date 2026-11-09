// HxDR (§8.9) — disaster recovery posture.
//
// The one thing this screen must never do is show green for a backup nobody
// has restored. `untested` gets its own colour, not a rounding-up to "ok".
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1
import React, { useEffect, useState } from "react";
import { apiJSON, postJSON, Banner, EmptyHint, Kpi, btn, card, h2, inp, mono, table, td, th, when } from "./shared";

type Region = {
  region: string;
  region_id: string;
  is_primary: boolean;
  state: "ok" | "untested" | "rpo_breached" | "no_backup";
  detail: string;
  last_backup_at: string | null;
  last_restore_test_at: string | null;
};

type Finding = { code: string; severity: "high" | "medium"; message: string };

type BackupOps = {
  available: boolean;
  facts: {
    last_run_at?: string | null;
    last_outcome?: string | null;
    last_good_at?: string | null;
    encrypted?: boolean;
    offbox_configured?: boolean;
    last_offbox?: Record<string, boolean>;
    timer_installed?: boolean;
    last_proven_restore_at?: string | null;
    private_key_on_server?: boolean;
  };
  findings: Finding[];
};

type StandbyEvent = { event: string; peer: string | null; occurred_at: string; detail: Record<string, unknown> };

type Standby = {
  linked: boolean;
  role: "main" | "demoted" | null;
  facts: {
    standby?: string;
    linked_at?: string | null;
    state?: string | null;
    version?: string | null;
    standby_version?: string | null;
    serve_reads?: { since?: string | null; tunnel_up?: boolean; credentials_current?: boolean } | null;  // R5d 11b (c)
    last_report_at?: string | null;
    databases?: Record<string, { state?: string; replay_lag_seconds?: number | null }>;
    parts?: Record<string, string | null | undefined>;
    promoted_at?: string | null;
    demoted_at?: string | null;
  };
  findings: Finding[];
  events?: StandbyEvent[];
  /** Automatic failover (step 12) — absent when it is not enabled on this server. */
  failover?: {
    witness?: string | null; node?: string | null; term?: number | null; holding?: boolean;
    lease_seconds?: number | null; agent_at?: string | null; agent_role?: string | null;
    fenced_at?: string | null; auto_promoted_at?: string | null;
  };
};

type Posture = {
  rpo_hours: number;
  rto_hours: number;
  restore_test_valid_days: number;
  regions: Region[];
  all_regions_ok: boolean;
  regions_at_risk: string[];
  backup_operations?: BackupOps;   // absent on a server older than R5a step c
  standby?: Standby;               // absent on a server older than R5c
};

const STATE_STYLE: Record<Region["state"], { fg: string; bg: string; label: string }> = {
  ok:           { fg: "var(--status-completed)", bg: "rgba(13,148,136,0.15)", label: "OK" },
  untested:     { fg: "var(--status-running)", bg: "rgba(247,183,49,0.15)", label: "UNTESTED" },
  rpo_breached: { fg: "var(--status-failed)", bg: "rgba(220,38,38,0.15)", label: "RPO BREACHED" },
  no_backup:    { fg: "var(--status-failed)", bg: "rgba(220,38,38,0.15)", label: "NO BACKUP" },
};

export default function DRPanel() {
  const [posture, setPosture] = useState<Posture | null>(null);
  const [rpo, setRpo] = useState(24);
  const [rto, setRto] = useState(4);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function load() {
    setBusy("load"); setErr(null);
    try {
      setPosture(await apiJSON<Posture>(
        `/api/v1/compliance/dr/posture?rpo_hours=${rpo}&rto_hours=${rto}`,
      ));
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(null); }
  }

  useEffect(() => { load(); }, []);           // eslint-disable-line react-hooks/exhaustive-deps

  const regions = posture?.regions ?? [];
  const atRisk = posture?.regions_at_risk ?? [];

  return (
    <>
      <section style={card}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 10, flexWrap: "wrap" }}>
          <h2 style={{ ...h2, margin: 0 }}>Recovery posture</h2>
          <div style={{ marginLeft: "auto", display: "flex", gap: 6, alignItems: "center" }}>
            <label style={{ fontSize: 12, color: "var(--text-secondary)" }}>RPO</label>
            <input type="number" min={1} value={rpo}
              onChange={e => setRpo(Number(e.target.value) || 24)} style={{ ...inp, width: 64 }} />
            <label style={{ fontSize: 12, color: "var(--text-secondary)" }}>RTO</label>
            <input type="number" min={1} value={rto}
              onChange={e => setRto(Number(e.target.value) || 4)} style={{ ...inp, width: 64 }} />
            <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>hours</span>
            <button onClick={load} disabled={!!busy} style={btn(true)}>
              {busy === "load" ? "Loading…" : "Refresh"}
            </button>
          </div>
        </div>

        {err && <Banner tone="bad">⚠ {err}</Banner>}
        {note && <Banner tone="ok">{note}</Banner>}

        {posture && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
            <Kpi label="Regions" value={regions.length} />
            <Kpi label="At risk" value={atRisk.length} warn={atRisk.length > 0} />
            <Kpi label="RPO" value={`${posture.rpo_hours}h`} />
            <Kpi label="Drill valid for" value={`${posture.restore_test_valid_days}d`} />
          </div>
        )}

        {posture && regions.length > 0 && (
          posture.all_regions_ok
            ? <Banner tone="ok">✓ Every region has a recent, demonstrably restorable backup.</Banner>
            : <Banner tone="bad">✗ At risk: {atRisk.join(", ")}. A single green light would hide these behind the healthy regions.</Banner>
        )}
      </section>

      {posture?.backup_operations && <BackupOpsCard ops={posture.backup_operations} />}
      {posture?.standby && <StandbyCard sb={posture.standby} />}

      <section style={card}>
        <h2 style={h2}>Per region</h2>
        {regions.length === 0 ? (
          <EmptyHint>
            No regions are registered, so there is nothing to report a posture against. Register a
            region in HxGlobal first — a backup recorded without one is stored but never shown here.
          </EmptyHint>
        ) : (
          <table style={table}>
            <thead>
              <tr>
                <th style={th}>Region</th>
                <th style={th}>State</th>
                <th style={th}>Why</th>
                <th style={th}>Last backup</th>
                <th style={th}>Last restore drill</th>
                <th style={th}>Record drill</th>
              </tr>
            </thead>
            <tbody>
              {regions.map(r => {
                const s = STATE_STYLE[r.state];
                return (
                  <tr key={r.region_id}>
                    <td style={{ ...td, ...mono }}>
                      {r.region}{r.is_primary && <span style={{ color: "var(--accent-text)" }}> ★</span>}
                    </td>
                    <td style={td}>
                      <span style={{ background: s.bg, color: s.fg, padding: "2px 6px",
                                     borderRadius: 3, fontSize: 11, fontWeight: 600 }}>
                        {s.label}
                      </span>
                    </td>
                    <td style={{ ...td, color: "var(--text-secondary)" }}>{r.detail}</td>
                    <td style={td}>{when(r.last_backup_at)}</td>
                    <td style={td}>{when(r.last_restore_test_at)}</td>
                    <td style={td}><DrillButtons region={r} onDone={m => { setNote(m); load(); }} onError={setErr} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 10 }}>
          Backups are taken on the server by <code style={mono}>backup-velaris.sh</code> (daily
          timer) and before every update, and recorded from there — this platform is a registry,
          not an executor. A fresh backup nobody has restored reports
          as <strong>untested</strong>, never as healthy.
        </div>
      </section>
    </>
  );
}

/** A drill is recorded against the region's newest backup, which is the one
 *  the posture above is judging. A failed drill is recorded too — knowing a
 *  backup does NOT restore is worth more than any number of untested ones. */
function DrillButtons({ region, onDone, onError }: {
  region: Region;
  onDone: (msg: string) => void;
  onError: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);

  async function record(result: "passed" | "failed") {
    setBusy(true);
    try {
      const backups = await apiJSON<{ backups: { id: string }[] }>(
        `/api/v1/compliance/dr/backups?region_id=${region.region_id}&limit=1`,
      );
      const newest = backups.backups?.[0];
      if (!newest) { onError(`${region.region} has no recorded backup to drill.`); return; }
      await postJSON(`/api/v1/compliance/dr/backups/${newest.id}/restore-test`, {
        result, notes: `Recorded from Studio for ${region.region}`,
      });
      onDone(`Restore drill recorded as ${result} for ${region.region}.`);
    } catch (e: any) { onError(e.message); }
    finally { setBusy(false); }
  }

  if (region.state === "no_backup") return <span style={{ color: "var(--text-muted)" }}>—</span>;
  return (
    <>
      <button onClick={() => record("passed")} disabled={busy} style={btn()}>Passed</button>{" "}
      <button onClick={() => record("failed")} disabled={busy} style={btn()}>Failed</button>
    </>
  );
}

/** What the backup tooling on this server last did. Every high finding is
 *  also pushed to admins by the backup watcher; this is where they can see all
 *  of them at once. Facts the server could not read are shown as unknown. */
function BackupOpsCard({ ops }: { ops: BackupOps }) {
  const f = ops.facts || {};
  const yes = (v: boolean | undefined, good = true) =>
    v === undefined ? "unknown" : (v === good ? "yes" : "no");
  const offbox = Object.entries(f.last_offbox || {});
  return (
    <section style={card}>
      <h2 style={h2}>Backups on this server</h2>
      {ops.findings.length === 0
        ? <Banner tone="ok">✓ Encrypted, copied off the server, on schedule, and proven to restore.</Banner>
        : ops.findings.map(x => (
            <Banner key={x.code} tone={x.severity === "high" ? "bad" : "warn"}>
              {x.severity === "high" ? "✗" : "⚠"} {x.message}
            </Banner>
          ))}
      {ops.available && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginTop: 10 }}>
          <Kpi label="Last complete backup" value={when(f.last_good_at ?? null)} warn={!f.last_good_at} />
          <Kpi label="Last proven restore" value={when(f.last_proven_restore_at ?? null)} warn={!f.last_proven_restore_at} />
          <Kpi label="Encrypted" value={yes(f.encrypted)} warn={f.encrypted === false} />
          <Kpi label="Daily timer" value={yes(f.timer_installed)} warn={f.timer_installed === false} />
          <Kpi label="Off-box copy" value={offbox.length ? offbox.map(([k, ok]) => `${k} ${ok ? "✓" : "✗"}`).join(" · ") : "none"}
               warn={!offbox.length || offbox.some(([, ok]) => !ok)} />
          <Kpi label="Private key on server" value={f.private_key_on_server === undefined ? "unknown" : f.private_key_on_server ? "YES" : "no"}
               warn={!!f.private_key_on_server} />
          <Kpi label="Last run" value={when(f.last_run_at ?? null)} />
          <Kpi label="Last outcome" value={f.last_outcome ?? "—"} warn={!!f.last_outcome && f.last_outcome !== "ok"} />
        </div>
      )}
    </section>
  );
}

/** The standby server (R5c). Optional: when none is linked this says so and
 *  how to add one, without a warning — backups are the default protection.
 *  High findings are also pushed and emailed by the watcher. */
const PART_LABEL: Record<string, string> = {
  docs: "Documents", recordings: "Recordings", env: "Settings", config: "velaris.yaml",
  identity: "Setup marker + key", vault: "Vault", code: "Code",
};

export function StandbyCard({ sb }: { sb: Standby }) {
  const f = sb.facts || {};
  const dbs = Object.entries(f.databases || {});
  const lag = Math.max(0, ...dbs.map(([, v]) => v.replay_lag_seconds ?? 0));
  return (
    <section style={card}>
      <h2 style={h2}>Standby server</h2>
      {sb.findings.map(x => (
        <Banner key={x.code} tone={x.severity === "high" ? "bad" : "warn"}>
          {x.severity === "high" ? "✗" : "⚠"} {x.message}
        </Banner>
      ))}
      {!sb.linked && sb.findings.length === 0 && (
        <EmptyHint>
          No standby is linked. Backups protect this install with up to a day of data at risk; a
          standby on a second server brings that to seconds — <span style={mono}>./standby-velaris.sh link &lt;host&gt;</span>
          {" "}(recovery runbook §8).
        </EmptyHint>
      )}
      {sb.linked && sb.findings.length === 0 && (
        <Banner tone="ok">✓ {f.standby} receives every change and holds current copies of everything else.</Banner>
      )}
      {sb.linked && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginTop: 10 }}>
          <Kpi label="Standby" value={f.standby ?? "—"} />
          <Kpi label="Replication" value={f.state ?? "unknown"} warn={f.state !== "streaming"} />
          <Kpi label="Behind by" value={`${lag.toFixed(1)} s`} warn={lag > 60} />
          <Kpi label="Last report" value={when(f.last_report_at ?? null)} warn={!f.last_report_at} />
          {f.serve_reads && (
            <Kpi label="Reads from standby" value={f.serve_reads.tunnel_up ? "on" : "tunnel down"}
                 warn={!f.serve_reads.tunnel_up} />
          )}
          <Kpi label="Version" value={`${f.version ?? "?"} / ${f.standby_version ?? "?"}`}
               warn={!!f.standby_version && f.standby_version !== f.version} />
          {Object.entries(f.parts || {}).filter(([, v]) => v).map(([k, v]) => (
            <Kpi key={k} label={PART_LABEL[k] ?? k} value={v as string} warn={v !== "ok" && v !== "off"} />
          ))}
        </div>
      )}
      {sb.failover && <FailoverRow fo={sb.failover} />}
      {(sb.events?.length ?? 0) > 0 && (
        <table style={{ ...table, marginTop: 12 }}>
          <thead><tr><th style={th}>When</th><th style={th}>Event</th><th style={th}>Other server</th></tr></thead>
          <tbody>
            {sb.events!.map((e, i) => (
              <tr key={i}><td style={td}>{when(e.occurred_at)}</td><td style={td}>{e.event}</td><td style={td}>{e.peer ?? "—"}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** Automatic failover (step 12): who holds the lease at the witness, and
 *  whether this server's agent is alive. Problems come in as the card's
 *  findings above (and are pushed by the watcher when high). */
export function FailoverRow({ fo }: { fo: NonNullable<Standby["failover"]> }) {
  const agentAge = fo.agent_at ? (Date.now() - new Date(fo.agent_at).getTime()) / 1000 : Infinity;
  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>Automatic failover</div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
        <Kpi label="Witness" value={fo.witness ?? "—"} />
        <Kpi label="Lease" value={fo.holding ? `held here (term ${fo.term ?? "?"})` : `held by the other server (term ${fo.term ?? "?"})`} />
        <Kpi label="Agent" value={fo.agent_at ? `${fo.agent_role ?? "?"} · ${when(fo.agent_at)}` : "not running"}
             warn={agentAge > 60} />
        <Kpi label="This server" value={fo.fenced_at ? `stopped itself ${when(fo.fenced_at)}` : "running"}
             warn={!!fo.fenced_at} />
      </div>
    </div>
  );
}
