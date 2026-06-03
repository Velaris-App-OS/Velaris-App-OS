import React, { useEffect, useRef, useState, useCallback } from "react";
import QRCode from "qrcode";
import { useAuth } from "@/auth";
import { BRAND } from "@/branding";

/* ═══════════════════════════════════════════════════════════════════
   ProfileDrawer — slide-over panel for the current user's profile.

   Sections:
     1. Personal Info  — display name, email (editable), username (read-only)
     2. Roles & Access — role badges, access groups
     3. Security       — change password, MFA enrol/disable
     4. Account        — joined, last login, account type, status
   ═══════════════════════════════════════════════════════════════════ */

interface Profile {
  user_id: string;
  username: string;
  email: string;
  display_name: string | null;
  roles: string[];
  is_admin: boolean;
  is_designer: boolean;
  is_case_worker: boolean;
  is_active: boolean;
  is_sso: boolean;
  sso_provider: string | null;
  mfa_enabled: boolean;
  password_change_required: boolean;
  last_login_at: string | null;
  created_at: string | null;
}

interface Props {
  open: boolean;
  onClose: () => void;
}

// MFA enrol step: idle → loading → scan → verify → done | error
type MfaStep = "idle" | "loading" | "scan" | "verify" | "done" | "disabling" | "confirm_disable";

const ROLE_META: Record<string, { label: string; color: string }> = {
  admin:       { label: "Administrator", color: "#ef4444" },
  designer:    { label: "Designer",      color: "#8b5cf6" },
  case_worker: { label: "Case Worker",   color: "#3b82f6" },
  manager:     { label: "Manager",       color: "#f59e0b" },
  devops:      { label: "DevOps",        color: "#10b981" },
  integration: { label: "Integration",   color: "#06b6d4" },
  security:    { label: "Security",      color: "#f97316" },
  viewer:      { label: "Viewer",        color: "#6b7280" },
  developer:   { label: "Developer",     color: "#a78bfa" },
};

function authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

function fmt(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export default function ProfileDrawer({ open, onClose }: Props) {
  const { user } = useAuth();
  const [profile, setProfile] = useState<Profile | null>(null);
  const [loading, setLoading] = useState(false);

  // Personal info
  const [editName, setEditName] = useState("");
  const [editEmail, setEditEmail] = useState("");
  const [infoMsg, setInfoMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [infoSaving, setInfoSaving] = useState(false);

  // Password
  const [curPw, setCurPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [pwMsg, setPwMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [pwSaving, setPwSaving] = useState(false);

  // MFA
  const [mfaStep, setMfaStep] = useState<MfaStep>("idle");
  const [mfaUri, setMfaUri] = useState("");
  const [mfaSecret, setMfaSecret] = useState("");
  const [mfaToken, setMfaToken] = useState("");
  const [mfaMsg, setMfaMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [showSecret, setShowSecret] = useState(false);
  const qrCanvasRef = useRef<HTMLCanvasElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/v1/auth/real/me/profile", { headers: authHdr() });
      if (r.ok) {
        const p: Profile = await r.json();
        setProfile(p);
        setEditName(p.display_name ?? "");
        setEditEmail(p.email ?? "");
      }
    } finally { setLoading(false); }
  }, []);

  useEffect(() => {
    if (open) {
      load();
      setInfoMsg(null); setPwMsg(null); setMfaMsg(null);
      setCurPw(""); setNewPw(""); setConfirmPw("");
      setMfaStep("idle"); setMfaToken(""); setShowSecret(false);
    }
  }, [open, load]);

  // Render QR code to canvas when URI is available and step = "scan"
  useEffect(() => {
    if (mfaStep === "scan" && mfaUri && qrCanvasRef.current) {
      QRCode.toCanvas(qrCanvasRef.current, mfaUri, {
        width: 200, margin: 2,
        color: { dark: "#000000", light: "#ffffff" },
      }).catch(() => {});
    }
  }, [mfaStep, mfaUri]);

  // ── Personal info save ──────────────────────────────────────────
  async function saveInfo(e: React.FormEvent) {
    e.preventDefault();
    setInfoSaving(true); setInfoMsg(null);
    try {
      const r = await fetch("/api/v1/auth/real/me/profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...authHdr() },
        body: JSON.stringify({ display_name: editName || null, email: editEmail }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Update failed");
      setProfile(prev => prev ? { ...prev, display_name: d.display_name, email: d.email } : prev);
      setInfoMsg({ text: "Profile updated.", ok: true });
    } catch (err: any) { setInfoMsg({ text: err.message, ok: false }); }
    finally { setInfoSaving(false); }
  }

  // ── Password change ─────────────────────────────────────────────
  async function changePassword(e: React.FormEvent) {
    e.preventDefault();
    if (newPw !== confirmPw) { setPwMsg({ text: "New passwords do not match.", ok: false }); return; }
    if (newPw.length < 8)    { setPwMsg({ text: "Password must be at least 8 characters.", ok: false }); return; }
    setPwSaving(true); setPwMsg(null);
    try {
      const r = await fetch("/api/v1/auth/real/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHdr() },
        body: JSON.stringify({ current_password: curPw, new_password: newPw }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Password change failed");
      setPwMsg({ text: "Password changed successfully.", ok: true });
      setCurPw(""); setNewPw(""); setConfirmPw("");
    } catch (err: any) { setPwMsg({ text: err.message, ok: false }); }
    finally { setPwSaving(false); }
  }

  // ── MFA enrollment ──────────────────────────────────────────────
  async function startMfaEnrol() {
    setMfaStep("loading"); setMfaMsg(null);
    try {
      const r = await fetch("/api/v1/auth/real/mfa/enrol", {
        method: "POST", headers: authHdr(),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Failed to start MFA setup.");
      setMfaUri(d.provisioning_uri);
      setMfaSecret(d.secret);
      setMfaStep("scan");
    } catch (err: any) {
      setMfaMsg({ text: err.message, ok: false });
      setMfaStep("idle");
    }
  }

  async function verifyMfaEnrol(e: React.FormEvent) {
    e.preventDefault();
    if (mfaToken.length !== 6) { setMfaMsg({ text: "Enter the 6-digit code from your authenticator app.", ok: false }); return; }
    setMfaMsg(null);
    try {
      const r = await fetch("/api/v1/auth/real/mfa/verify-enrol", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHdr() },
        body: JSON.stringify({ token: mfaToken }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Invalid code. Try again.");
      setMfaStep("done");
      setProfile(prev => prev ? { ...prev, mfa_enabled: true } : prev);
    } catch (err: any) {
      setMfaMsg({ text: err.message, ok: false });
      setMfaToken("");
    }
  }

  function disableMfa() {
    // Transition to confirmation step — server requires TOTP code to disable.
    setMfaToken("");
    setMfaMsg(null);
    setMfaStep("confirm_disable");
  }

  async function confirmDisableMfa(e: React.FormEvent) {
    e.preventDefault();
    if (mfaToken.length !== 6) {
      setMfaMsg({ text: "Enter the 6-digit code from your authenticator app.", ok: false });
      return;
    }
    setMfaStep("disabling");
    try {
      const r = await fetch("/api/v1/auth/real/mfa/disable", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHdr() },
        body: JSON.stringify({ token: mfaToken }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Failed to disable MFA.");
      setProfile(prev => prev ? { ...prev, mfa_enabled: false } : prev);
      setMfaMsg({ text: "Two-factor authentication has been disabled.", ok: false });
      setMfaStep("idle");
    } catch (err: any) {
      setMfaMsg({ text: err.message, ok: false });
      setMfaStep("confirm_disable");
    }
    setMfaToken("");
  }

  if (!open) return null;

  const initials = (profile?.display_name || profile?.username || user?.username || "?")
    .split(" ").map(w => w[0]).join("").slice(0, 2).toUpperCase();

  return (
    <>
      <div onClick={onClose} style={{
        position: "fixed", inset: 0, zIndex: 900,
        background: "rgba(0,0,0,0.45)", backdropFilter: "blur(2px)",
      }} />

      <div style={{
        position: "fixed", top: 0, right: 0, bottom: 0, zIndex: 901,
        width: "min(460px, 100vw)",
        background: "var(--bg-card)",
        borderLeft: "1px solid var(--border)",
        display: "flex", flexDirection: "column",
        boxShadow: "-8px 0 32px rgba(0,0,0,0.3)",
        overflowY: "auto",
      }}>

        {/* Header */}
        <div style={{
          padding: "20px 24px", borderBottom: "1px solid var(--border)",
          display: "flex", alignItems: "center", gap: 14, flexShrink: 0,
          background: "var(--bg-surface)", position: "sticky", top: 0, zIndex: 1,
        }}>
          <div style={{
            width: 52, height: 52, borderRadius: "50%", flexShrink: 0,
            background: "linear-gradient(135deg, var(--accent), #818cf8)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 18, fontWeight: 700, color: "#fff", fontFamily: "var(--font-mono)",
          }}>{initials}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {profile?.display_name || profile?.username || user?.username}
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
              @{profile?.username || user?.username}
            </div>
            <div style={{ display: "flex", gap: 4, marginTop: 4, flexWrap: "wrap" }}>
              {(profile?.roles ?? user?.roles ?? []).slice(0, 3).map(r => {
                const m = ROLE_META[r];
                return (
                  <span key={r} style={{
                    fontSize: 9, padding: "1px 6px", borderRadius: 4,
                    fontWeight: 700, fontFamily: "var(--font-mono)", textTransform: "uppercase",
                    background: (m?.color ?? "#6b7280") + "22", color: m?.color ?? "#6b7280",
                  }}>{m?.label ?? r}</span>
                );
              })}
            </div>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 18, padding: 4, flexShrink: 0 }}>✕</button>
        </div>

        {loading && (
          <div style={{ padding: 32, textAlign: "center", color: "var(--text-muted)", fontSize: 13 }}>Loading profile…</div>
        )}

        {!loading && profile && (
          <div style={{ padding: "0 24px 40px", display: "flex", flexDirection: "column", gap: 24 }}>

            {/* ── Personal Info ───────────────────────────────────── */}
            <Section title="Personal Info">
              <form onSubmit={saveInfo} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                <Field label="Username">
                  <input value={profile.username} readOnly style={{ ...inputStyle, opacity: 0.6, cursor: "not-allowed" }} />
                </Field>
                <Field label="Display Name">
                  <input value={editName} onChange={e => setEditName(e.target.value)} placeholder="Your full name" style={inputStyle} />
                </Field>
                <Field label="Email">
                  {profile.is_sso ? (
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <input value={editEmail} readOnly style={{ ...inputStyle, flex: 1, opacity: 0.6, cursor: "not-allowed" }} />
                      <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4, background: "#dbeafe", color: "#1d4ed8", fontWeight: 600, whiteSpace: "nowrap" }}>
                        {profile.sso_provider?.toUpperCase()} SSO
                      </span>
                    </div>
                  ) : (
                    <input type="email" value={editEmail} onChange={e => setEditEmail(e.target.value)} placeholder="your@email.com" style={inputStyle} />
                  )}
                </Field>
                {infoMsg && <Msg msg={infoMsg} />}
                <button type="submit" disabled={infoSaving} style={primaryBtn}>
                  {infoSaving ? "Committing…" : "Commit Changes"}
                </button>
              </form>
            </Section>

            {/* ── Roles & Access ──────────────────────────────────── */}
            <Section title="Roles & Access">
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
                {profile.roles.length === 0 ? (
                  <span style={{ fontSize: 12, color: "var(--text-muted)" }}>No roles assigned</span>
                ) : profile.roles.map(r => {
                  const m = ROLE_META[r];
                  return (
                    <div key={r} style={{
                      display: "flex", alignItems: "center", gap: 6, padding: "5px 10px", borderRadius: 6,
                      background: (m?.color ?? "#6b7280") + "15", border: `1px solid ${(m?.color ?? "#6b7280")}40`,
                    }}>
                      <div style={{ width: 7, height: 7, borderRadius: "50%", background: m?.color ?? "#6b7280", flexShrink: 0 }} />
                      <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)" }}>{m?.label ?? r}</span>
                    </div>
                  );
                })}
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", padding: "8px 10px", background: "var(--bg-elevated)", borderRadius: 6 }}>
                Role assignments are managed by an administrator.
              </div>
            </Section>

            {/* ── Security ────────────────────────────────────────── */}
            <Section title="Security">

              {/* MFA block */}
              <MfaBlock
                profile={profile}
                mfaStep={mfaStep}
                mfaUri={mfaUri}
                mfaSecret={mfaSecret}
                mfaToken={mfaToken}
                mfaMsg={mfaMsg}
                showSecret={showSecret}
                qrCanvasRef={qrCanvasRef}
                onStartEnrol={startMfaEnrol}
                onVerify={verifyMfaEnrol}
                onDisable={disableMfa}
                onConfirmDisable={confirmDisableMfa}
                onTokenChange={v => { setMfaToken(v.replace(/\D/g, "").slice(0, 6)); setMfaMsg(null); }}
                onToggleSecret={() => setShowSecret(s => !s)}
                onBackToScan={() => setMfaStep("scan")}
                onCancelEnrol={() => { setMfaStep("idle"); setMfaMsg(null); setMfaToken(""); }}
                onCancelDisable={() => { setMfaStep("idle"); setMfaMsg(null); setMfaToken(""); }}
              />

              {/* Password change */}
              {!(profile.is_sso && !profile.password_change_required) && (
                <div style={{ marginTop: 20 }}>
                  <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 12, paddingTop: 16, borderTop: "1px solid var(--border-subtle)" }}>
                    Change Password
                  </div>
                  {profile.password_change_required && (
                    <div style={{ fontSize: 11, padding: "6px 10px", borderRadius: 6, background: "#fef3c7", color: "#92400e", marginBottom: 10 }}>
                      A password change is required for your account.
                    </div>
                  )}
                  <form onSubmit={changePassword} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    <Field label="Current Password">
                      <input type="password" value={curPw} onChange={e => setCurPw(e.target.value)} placeholder="Current password" style={inputStyle} autoComplete="current-password" />
                    </Field>
                    <Field label="New Password">
                      <input type="password" value={newPw} onChange={e => setNewPw(e.target.value)} placeholder="8+ characters" style={inputStyle} autoComplete="new-password" />
                    </Field>
                    <Field label="Confirm New Password">
                      <input type="password" value={confirmPw} onChange={e => setConfirmPw(e.target.value)} placeholder="Repeat new password" style={inputStyle} autoComplete="new-password" />
                    </Field>
                    {pwMsg && <Msg msg={pwMsg} />}
                    <button type="submit" disabled={pwSaving || !curPw || !newPw || !confirmPw} style={primaryBtn}>
                      {pwSaving ? "Changing…" : "Change Password"}
                    </button>
                  </form>
                </div>
              )}
              {profile.is_sso && !profile.password_change_required && (
                <div style={{ marginTop: 12, fontSize: 12, color: "var(--text-muted)", padding: "10px 12px", background: "var(--bg-elevated)", borderRadius: 8 }}>
                  Your account uses {profile.sso_provider?.toUpperCase()} SSO — password management is handled by your identity provider.
                </div>
              )}
            </Section>

            {/* ── Account Details ──────────────────────────────────── */}
            <Section title="Account">
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {([
                  ["Status",       profile.is_active
                    ? <span style={{ color: "#16a34a", fontWeight: 600 }}>● Active</span>
                    : <span style={{ color: "#dc2626", fontWeight: 600 }}>● Inactive</span>],
                  ["Account Type", profile.is_sso ? `SSO (${profile.sso_provider ?? "external"})` : "Password"],
                  ["Platform",     BRAND.name],
                  ["Last Login",   fmt(profile.last_login_at)],
                  ["Member Since", fmt(profile.created_at)],
                ] as [string, React.ReactNode][]).map(([label, value]) => (
                  <div key={label} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12, padding: "6px 0", borderBottom: "1px solid var(--border-subtle)" }}>
                    <span style={{ color: "var(--text-muted)" }}>{label}</span>
                    <span style={{ color: "var(--text-primary)", fontWeight: 500 }}>{value}</span>
                  </div>
                ))}
              </div>
            </Section>

          </div>
        )}
      </div>
    </>
  );
}

// ── MFA Block ─────────────────────────────────────────────────────

interface MfaBlockProps {
  profile: Profile;
  mfaStep: MfaStep;
  mfaUri: string;
  mfaSecret: string;
  mfaToken: string;
  mfaMsg: { text: string; ok: boolean } | null;
  showSecret: boolean;
  qrCanvasRef: React.RefObject<HTMLCanvasElement>;
  onStartEnrol: () => void;
  onVerify: (e: React.FormEvent) => void;
  onDisable: () => void;
  onConfirmDisable: (e: React.FormEvent) => void;
  onTokenChange: (v: string) => void;
  onToggleSecret: () => void;
  onBackToScan: () => void;
  onCancelEnrol: () => void;
  onCancelDisable: () => void;
}

function MfaBlock({
  profile, mfaStep, mfaSecret, mfaToken, mfaMsg, showSecret,
  qrCanvasRef, onStartEnrol, onVerify, onDisable, onConfirmDisable,
  onTokenChange, onToggleSecret, onBackToScan, onCancelEnrol, onCancelDisable,
}: MfaBlockProps) {

  // ── Status bar (always visible) ──────────────────────────────────
  const statusBar = (
    <div style={{
      display: "flex", alignItems: "center", gap: 10,
      padding: "12px 14px", borderRadius: 10,
      background: "var(--bg-elevated)", border: "1px solid var(--border)",
      marginBottom: mfaStep === "idle" ? 0 : 16,
    }}>
      <span style={{ fontSize: 20 }}>{profile.mfa_enabled ? "🔒" : "🔓"}</span>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
          Two-Factor Authentication
        </div>
        <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {profile.mfa_enabled
            ? "TOTP authenticator app is active."
            : "Add an extra layer of security to your account."}
        </div>
      </div>
      <span style={{
        fontSize: 10, fontWeight: 700, padding: "3px 8px", borderRadius: 4, flexShrink: 0,
        background: profile.mfa_enabled ? "#dcfce7" : "#fee2e2",
        color: profile.mfa_enabled ? "#166534" : "#991b1b",
      }}>
        {profile.mfa_enabled ? "ENABLED" : "DISABLED"}
      </span>
    </div>
  );

  // ── Idle: show enable / disable button ───────────────────────────
  if (mfaStep === "idle" || mfaStep === "disabling") {
    return (
      <div>
        {statusBar}
        {mfaMsg && <div style={{ marginTop: 8 }}><Msg msg={mfaMsg} /></div>}
        <div style={{ marginTop: 12 }}>
          {!profile.mfa_enabled ? (
            <button onClick={onStartEnrol} disabled={mfaStep === "disabling"} style={{ ...primaryBtn, display: "flex", alignItems: "center", gap: 8 }}>
              <span>🔐</span> Enable Two-Factor Authentication
            </button>
          ) : (
            <button onClick={onDisable} disabled={mfaStep === "disabling"} style={{ ...primaryBtn, background: "#dc2626" }}>
              {mfaStep === "disabling" ? "Disabling…" : "Disable Two-Factor Authentication"}
            </button>
          )}
        </div>
      </div>
    );
  }

  // ── Loading ──────────────────────────────────────────────────────
  if (mfaStep === "loading") {
    return (
      <div>
        {statusBar}
        <div style={{ textAlign: "center", padding: "20px 0", color: "var(--text-muted)", fontSize: 13 }}>
          Generating your authenticator key…
        </div>
      </div>
    );
  }

  // ── Scan QR ──────────────────────────────────────────────────────
  if (mfaStep === "scan") {
    return (
      <div>
        {statusBar}
        <Steps current={1} />

        <div style={{ textAlign: "center", marginBottom: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: 6 }}>
            Scan with your authenticator app
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 16 }}>
            Use Google Authenticator, Authy, or any TOTP-compatible app.
          </div>
          {/* QR canvas */}
          <div style={{ display: "inline-block", padding: 12, background: "#fff", borderRadius: 10, border: "1px solid var(--border)" }}>
            <canvas ref={qrCanvasRef} style={{ display: "block" }} />
          </div>
        </div>

        {/* Manual entry fallback */}
        <button
          onClick={onToggleSecret}
          style={{ background: "none", border: "none", cursor: "pointer", fontSize: 11, color: "var(--accent)", marginBottom: 8, padding: 0 }}
        >
          {showSecret ? "▲ Hide manual entry key" : "▼ Can't scan? Enter key manually"}
        </button>
        {showSecret && (
          <div style={{ padding: "10px 14px", background: "var(--bg-elevated)", borderRadius: 8, marginBottom: 12, border: "1px solid var(--border)" }}>
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 4 }}>Enter this key in your authenticator app:</div>
            <div style={{
              fontFamily: "var(--font-mono)", fontSize: 14, fontWeight: 700,
              color: "var(--accent)", letterSpacing: "0.15em", wordBreak: "break-all",
            }}>{mfaSecret}</div>
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>Account: Time-based (TOTP)</div>
          </div>
        )}

        <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
          <button onClick={onCancelEnrol} style={{ ...ghostBtn }}>Cancel</button>
        </div>
        <MfaVerifyInline step="scan-verify" mfaToken={mfaToken} mfaMsg={mfaMsg} onVerify={onVerify} onTokenChange={onTokenChange} />
      </div>
    );
  }

  // ── Verify ───────────────────────────────────────────────────────
  if (mfaStep === "verify") {
    return (
      <div>
        {statusBar}
        <Steps current={2} />
        <MfaVerifyInline step="verify" mfaToken={mfaToken} mfaMsg={mfaMsg} onVerify={onVerify} onTokenChange={onTokenChange} />
        <div style={{ marginTop: 8 }}>
          <button onClick={onBackToScan} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 11, color: "var(--accent)", padding: 0 }}>
            ← Back to QR code
          </button>
        </div>
      </div>
    );
  }

  // ── Confirm disable — require TOTP before removing MFA ──────────
  if (mfaStep === "confirm_disable") {
    return (
      <div>
        {statusBar}
        <div style={{
          padding: "10px 14px", borderRadius: 8, marginBottom: 16,
          background: "rgba(220,38,38,0.08)", border: "1px solid rgba(220,38,38,0.3)",
          fontSize: 12, color: "var(--text-secondary)",
        }}>
          To confirm, enter the 6-digit code currently shown in your authenticator app. This verifies you still have access to your device before removing 2FA.
        </div>
        <form onSubmit={onConfirmDisable} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <input
            value={mfaToken}
            onChange={e => onTokenChange(e.target.value)}
            placeholder="000000"
            maxLength={6}
            inputMode="numeric"
            autoComplete="one-time-code"
            autoFocus
            style={{
              ...inputStyle,
              fontSize: 22, fontWeight: 700, letterSpacing: "0.3em",
              textAlign: "center", fontFamily: "var(--font-mono)",
            }}
          />
          {mfaMsg && <Msg msg={mfaMsg} />}
          <div style={{ display: "flex", gap: 8 }}>
            <button type="submit" disabled={mfaToken.length !== 6} style={{ ...primaryBtn, background: "#dc2626" }}>
              Confirm Disable
            </button>
            <button type="button" onClick={onCancelDisable} style={ghostBtn}>
              Cancel
            </button>
          </div>
        </form>
      </div>
    );
  }

  // ── Done ─────────────────────────────────────────────────────────
  if (mfaStep === "done") {
    return (
      <div>
        <div style={{
          display: "flex", alignItems: "center", gap: 10, padding: "12px 14px", borderRadius: 10,
          background: "#dcfce7", border: "1px solid #86efac", marginBottom: 12,
        }}>
          <span style={{ fontSize: 20 }}>✅</span>
          <div>
            <div style={{ fontSize: 13, fontWeight: 600, color: "#166534" }}>Two-Factor Authentication Enabled</div>
            <div style={{ fontSize: 11, color: "#15803d" }}>Your account is now protected with TOTP authentication.</div>
          </div>
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "10px 12px", background: "var(--bg-elevated)", borderRadius: 8 }}>
          From your next login, you will be asked for a 6-digit code from your authenticator app after entering your password.
        </div>
        <div style={{ marginTop: 12 }}>
          <button onClick={onDisable} style={{ ...primaryBtn, background: "#dc2626", fontSize: 12 }}>
            Disable Two-Factor Authentication
          </button>
        </div>
      </div>
    );
  }

  return null;
}

// Inline verify form — used inside the scan step and standalone verify step
function MfaVerifyInline({ step, mfaToken, mfaMsg, onVerify, onTokenChange }: {
  step: "scan-verify" | "verify";
  mfaToken: string;
  mfaMsg: { text: string; ok: boolean } | null;
  onVerify: (e: React.FormEvent) => void;
  onTokenChange: (v: string) => void;
}) {
  return (
    <form onSubmit={onVerify} style={{ marginTop: step === "scan-verify" ? 16 : 0, display: "flex", flexDirection: "column", gap: 10 }} id="mfa-verify-section">
      <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
        Step 2 — Verify your authenticator code
      </div>
      <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
        Open your authenticator app and enter the 6-digit code shown for this account.
      </div>
      <input
        value={mfaToken}
        onChange={e => onTokenChange(e.target.value)}
        placeholder="000000"
        maxLength={6}
        inputMode="numeric"
        autoComplete="one-time-code"
        style={{
          ...inputStyle,
          fontSize: 24, fontWeight: 700, letterSpacing: "0.3em",
          textAlign: "center", fontFamily: "var(--font-mono)",
        }}
      />
      {mfaMsg && <Msg msg={mfaMsg} />}
      <button type="submit" disabled={mfaToken.length !== 6} style={primaryBtn}>
        🔐 Confirm &amp; Enable MFA
      </button>
    </form>
  );
}

function Steps({ current }: { current: 1 | 2 }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
      {[
        { n: 1, label: "Scan QR" },
        { n: 2, label: "Verify Code" },
      ].map(({ n, label }, i) => (
        <React.Fragment key={n}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <div style={{
              width: 22, height: 22, borderRadius: "50%", flexShrink: 0,
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 11, fontWeight: 700,
              background: n <= current ? "var(--accent)" : "var(--bg-elevated)",
              color: n <= current ? "#fff" : "var(--text-muted)",
              border: n <= current ? "none" : "1px solid var(--border)",
            }}>{n}</div>
            <span style={{ fontSize: 11, color: n <= current ? "var(--text-primary)" : "var(--text-muted)", fontWeight: n === current ? 600 : 400 }}>
              {label}
            </span>
          </div>
          {i === 0 && <div style={{ flex: 1, height: 1, background: current >= 2 ? "var(--accent)" : "var(--border)", maxWidth: 40 }} />}
        </React.Fragment>
      ))}
    </div>
  );
}

// ── Shared sub-components ─────────────────────────────────────────

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 20 }}>
      <div style={{ fontSize: 10, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.08em", fontFamily: "var(--font-mono)", marginBottom: 12, paddingBottom: 8, borderBottom: "1px solid var(--border-subtle)" }}>
        {title}
      </div>
      {children}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label style={{ display: "block", fontSize: 11, fontWeight: 600, color: "var(--text-muted)", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.05em" }}>{label}</label>
      {children}
    </div>
  );
}

function Msg({ msg }: { msg: { text: string; ok: boolean } }) {
  return (
    <div style={{ fontSize: 12, padding: "8px 12px", borderRadius: 6, background: msg.ok ? "#dcfce7" : "#fee2e2", color: msg.ok ? "#166534" : "#991b1b" }}>
      {msg.text}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "8px 12px", fontSize: 13,
  background: "var(--bg-input, var(--bg-elevated))",
  border: "1px solid var(--border-default, var(--border))",
  borderRadius: 6, color: "var(--text-primary)",
  fontFamily: "var(--font-body)", outline: "none", boxSizing: "border-box",
};

const primaryBtn: React.CSSProperties = {
  padding: "9px 18px", background: "var(--accent)", color: "#fff",
  border: "none", borderRadius: 6, cursor: "pointer",
  fontSize: 13, fontWeight: 600, fontFamily: "var(--font-body)",
  alignSelf: "flex-start",
};

const ghostBtn: React.CSSProperties = {
  padding: "9px 16px", background: "var(--bg-elevated)",
  border: "1px solid var(--border)", color: "var(--text-secondary)",
  borderRadius: 6, cursor: "pointer", fontSize: 13, fontFamily: "var(--font-body)",
};
