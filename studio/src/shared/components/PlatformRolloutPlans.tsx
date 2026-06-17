/**
 * PUO Phases 3+4 — platform updates section for the deploy page.
 *
 * Shown beside the registered environments but deliberately its own section:
 * everything here ships PLATFORM CODE — it is not HxDeploy artifact promotion.
 *
 * Surfaces: fleet platform versions (per registered env), update mode policy
 * (auto-soak / per-env / manual) + default soak, rollout plans with ring
 * states and the approval gates (plan approval, per-env ring approval, prod
 * approval), halt, and current-env rollback.
 */
import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@shared/api/client";

interface PlanRun {
  id: string;
  environment: string;
  ring_order: number;
  is_final_ring: boolean;
  state: string;
  detail: string | null;
}

interface Plan {
  id: string;
  resolved_version: string;
  channel: string;
  soak_hours: number;
  state: string;
  halted_reason: string | null;
  soak_started_at: string | null;
  runs: PlanRun[];
}

interface FleetEnv {
  id: string;
  label: string;
  reachable: boolean;
  platform_version: string | null;
  last_result: string | null;
}

const STATE_COLORS: Record<string, string> = {
  draft: "#6b7280", active: "#3b82f6", soaking: "#f59e0b",
  awaiting_prod_approval: "#f59e0b", prod_approved: "#3b82f6",
  completed: "#22c55e", halted: "#ef4444", superseded: "#a855f7",
  pending: "#6b7280", awaiting_approval: "#f59e0b", approved: "#3b82f6",
  triggered: "#3b82f6", running: "#3b82f6",
  succeeded: "#22c55e", failed: "#ef4444",
  updated: "#22c55e", rolled_back: "#f59e0b", unhealthy: "#ef4444",
};

function Badge({ state }: { state: string }) {
  const c = STATE_COLORS[state] ?? "#6b7280";
  return (
    <span style={{
      fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 10,
      color: c, background: `${c}22`, textTransform: "uppercase", letterSpacing: "0.03em",
    }}>
      {state.replace(/_/g, " ")}
    </span>
  );
}

export function PlatformRolloutPlans() {
  const [plans, setPlans] = useState<Plan[]>([]);
  const [fleet, setFleet] = useState<FleetEnv[]>([]);
  const [thisEnv, setThisEnv] = useState<{ platform_version: string; channel: string } | null>(null);
  const [prevVersion, setPrevVersion] = useState<string | null>(null);
  const [settings, setSettings] = useState<{ mode: string; default_soak_hours: number } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [visible, setVisible] = useState(false);

  const load = useCallback(async () => {
    try {
      const [pr, er, sr, str] = await Promise.all([
        apiFetch("/api/v1/platform/update/plans"),
        apiFetch("/api/v1/platform/update/environments"),
        apiFetch("/api/v1/platform/update/settings"),
        apiFetch("/api/v1/platform/update/status"),
      ]);
      if (!pr.ok) return; // non-admin — stay hidden
      setVisible(true);
      setPlans(((await pr.json()) as { plans: Plan[] }).plans);
      if (er.ok) {
        const e = await er.json();
        setFleet(e.environments as FleetEnv[]);
        setThisEnv(e.this_environment);
      }
      if (sr.ok) setSettings(await sr.json());
      if (str.ok) {
        const s = await str.json();
        setPrevVersion(s.last_update_status?.previous_version || null);
      }
    } catch {
      /* silent */
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  const call = async (path: string, opts?: RequestInit) => {
    setBusy(path);
    setError(null);
    try {
      const r = await apiFetch(path, { method: "POST", ...opts });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        setError(body.detail ?? "Action failed");
        return false;
      }
      await load();
      return true;
    } finally {
      setBusy(null);
    }
  };

  // Step-up auth: plan approval, prod approval, and rollback re-verify the
  // admin's password (+ TOTP when MFA is enrolled) at the moment of action.
  const [stepUp, setStepUp] = useState<{ label: string; path: string; extra?: Record<string, unknown> } | null>(null);
  const [suPassword, setSuPassword] = useState("");
  const [suMfa, setSuMfa] = useState("");

  const confirmStepUp = async () => {
    if (!stepUp || !suPassword) return;
    const ok = await call(stepUp.path, {
      body: JSON.stringify({ password: suPassword, mfa_code: suMfa || null, ...(stepUp.extra ?? {}) }),
    });
    if (ok) {
      setStepUp(null);
      setSuPassword("");
      setSuMfa("");
    }
  };

  // Group J: confirm with a passkey instead of password+TOTP
  const confirmStepUpPasskey = async () => {
    if (!stepUp) return;
    try {
      const { getAssertion } = await import("@/auth/webauthn");
      const optR = await apiFetch("/api/v1/auth/real/webauthn/stepup/options", { method: "POST" });
      if (!optR.ok) {
        const d = await optR.json().catch(() => ({}));
        setError(d.detail ?? "No passkeys registered — use password instead.");
        return;
      }
      const credential = await getAssertion(await optR.json());
      const ok = await call(stepUp.path, {
        body: JSON.stringify({ webauthn_credential: credential, ...(stepUp.extra ?? {}) }),
      });
      if (ok) {
        setStepUp(null);
        setSuPassword("");
        setSuMfa("");
      }
    } catch (e: any) {
      if (e?.name !== "NotAllowedError") setError(e?.message ?? "Passkey verification failed");
    }
  };

  const saveSettings = async (next: { mode?: string; default_soak_hours?: number }) => {
    setBusy("settings");
    try {
      const r = await apiFetch("/api/v1/platform/update/settings", {
        method: "PUT",
        body: JSON.stringify(next),
      });
      if (r.ok) setSettings(await r.json());
    } finally {
      setBusy(null);
    }
  };

  if (!visible) return null;

  return (
    <div style={{ marginTop: 28 }}>
      <div style={{ fontWeight: 700, marginBottom: 4 }}>Platform Updates</div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>
        Velaris platform version across registered environments — separate from artifact
        deployment runs above.
      </div>

      {error && (
        <div style={{ fontSize: 12, padding: "8px 12px", borderRadius: 4, marginBottom: 12, background: "#ef444422", color: "#ef4444" }}>
          {error}
        </div>
      )}

      {/* Step-up auth dialog — password (+ MFA) re-verified at the moment of approval */}
      {stepUp && (
        <div style={{
          border: "1px solid var(--border-default)", borderRadius: 8,
          padding: "12px 16px", marginBottom: 12, fontSize: 13,
          display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap",
        }}>
          <span style={{ fontWeight: 600 }}>{stepUp.label}</span>
          <span style={{ opacity: 0.7 }}>— confirm with your password:</span>
          <input
            type="password" placeholder="password" value={suPassword} autoFocus
            onChange={(e) => setSuPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && confirmStepUp()}
          />
          <input
            type="text" placeholder="MFA code (if enrolled)" value={suMfa} style={{ width: 130 }}
            onChange={(e) => setSuMfa(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && confirmStepUp()}
          />
          <button disabled={busy !== null || !suPassword} onClick={confirmStepUp}>Confirm</button>
          {typeof window !== "undefined" && !!window.PublicKeyCredential && (
            <button disabled={busy !== null} onClick={confirmStepUpPasskey} title="Confirm with a registered passkey instead">
              🔑 Use passkey
            </button>
          )}
          <button disabled={busy !== null} onClick={() => { setStepUp(null); setSuPassword(""); setSuMfa(""); }}>
            Cancel
          </button>
        </div>
      )}

      {/* This environment + rollback */}
      {thisEnv && (
        <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13, marginBottom: 10 }}>
          <span>This environment: <strong>v{thisEnv.platform_version}</strong></span>
          <span style={{ opacity: 0.6 }}>({thisEnv.channel} channel)</span>
          {prevVersion && prevVersion !== thisEnv.platform_version && (
            <button
              disabled={busy !== null}
              onClick={() => {
                if (window.confirm(
                  `Revert this environment to v${prevVersion}?\n\nCode and images revert; ` +
                  "the database schema stays as-is. Restoring the pre-update DB backup " +
                  "is a separate manual decision.",
                )) setStepUp({ label: `Revert to v${prevVersion}`, path: "/api/v1/platform/update/rollback" });
              }}
            >
              Revert to v{prevVersion}
            </button>
          )}
        </div>
      )}

      {/* Mode policy */}
      {settings && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, marginBottom: 12 }}>
          <span style={{ opacity: 0.7 }}>Update mode:</span>
          <select
            value={settings.mode}
            disabled={busy === "settings"}
            onChange={(e) => saveSettings({ mode: e.target.value })}
          >
            <option value="auto-soak">auto-soak — one approval, rings cascade (prod gated)</option>
            <option value="per-env">per-env — every ring needs its own approval</option>
            <option value="manual">manual — plans are never auto-drafted</option>
          </select>
          <span style={{ opacity: 0.7 }}>Default soak (h):</span>
          <input
            type="number" min={1} max={720} style={{ width: 64 }}
            defaultValue={settings.default_soak_hours}
            disabled={busy === "settings"}
            onBlur={(e) => {
              const v = Number(e.target.value);
              if (v >= 1 && v <= 720 && v !== settings.default_soak_hours)
                saveSettings({ default_soak_hours: v });
            }}
          />
        </div>
      )}

      {/* Fleet versions */}
      {fleet.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          {fleet.map((e) => (
            <div key={e.id} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, padding: "3px 0" }}>
              <span style={{ minWidth: 140 }}>{e.label}</span>
              {e.reachable ? (
                <>
                  <span>v{e.platform_version}</span>
                  {e.last_result && <Badge state={e.last_result} />}
                </>
              ) : (
                <span style={{ opacity: 0.5 }}>unreachable</span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Rollout plans */}
      {plans.map((p) => (
        <div key={p.id} style={{
          border: "1px solid var(--border-subtle)", borderRadius: 8,
          padding: "12px 16px", marginBottom: 10, fontSize: 13,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <span style={{ fontWeight: 600 }}>Velaris v{p.resolved_version}</span>
            <span style={{ opacity: 0.6 }}>({p.channel} channel · soak {p.soak_hours}h)</span>
            <Badge state={p.state} />
            <span style={{ flex: 1 }} />
            {p.state === "draft" && (
              <button disabled={busy !== null}
                onClick={() => setStepUp({ label: `Approve rollout of v${p.resolved_version}`, path: `/api/v1/platform/update/plans/${p.id}/approve` })}>
                Approve rollout
              </button>
            )}
            {p.state === "awaiting_prod_approval" && (
              <button disabled={busy !== null}
                onClick={() => setStepUp({ label: `Approve PROD ring for v${p.resolved_version}`, path: `/api/v1/platform/update/plans/${p.id}/approve-prod` })}>
                Approve prod ring
              </button>
            )}
            {!["completed", "halted", "superseded"].includes(p.state) && (
              <button disabled={busy !== null} onClick={() => call(`/api/v1/platform/update/plans/${p.id}/halt`)}>
                Halt
              </button>
            )}
          </div>
          {p.halted_reason && (
            <div style={{ color: "#ef4444", fontSize: 12, marginTop: 6 }}>{p.halted_reason}</div>
          )}
          <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 4 }}>
            {p.runs.map((r) => (
              <div key={r.id} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
                <span style={{ opacity: 0.5, width: 44 }}>ring {r.ring_order + 1}</span>
                <span style={{ minWidth: 140 }}>{r.environment}</span>
                {r.is_final_ring && <span style={{ opacity: 0.6 }}>· prod (gated)</span>}
                <Badge state={r.state} />
                {r.state === "awaiting_approval" && (
                  <button disabled={busy !== null} onClick={() => call(`/api/v1/platform/update/runs/${r.id}/approve`)}>
                    Approve ring
                  </button>
                )}
                {r.detail && <span style={{ opacity: 0.6 }}>{r.detail}</span>}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
