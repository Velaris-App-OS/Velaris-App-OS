import React, { createContext, useContext, useState, useEffect, useRef } from "react";
import { useAuth } from "./AuthContext";
import { Button, Spinner } from "@shared/components";
import { BRAND, THEME_STORAGE_KEY } from "@/branding";

/* ═══════════════════════════════════════════════════════════════════
   Login Page — P64 Real Auth
   ═══════════════════════════════════════════════════════════════════ */

type Stage = "login" | "mfa" | "forgot" | "reset" | "change_required";
type SsoProvider = { id: string; provider: string; client_id: string };
type LoginThemePref = "dark" | "light" | "system";

/* ── Theme context (login-page scoped) ─────────────────────────── */
const LoginThemeCtx = createContext<{ isDark: boolean }>({ isDark: true });
const useDark = () => useContext(LoginThemeCtx).isDark;

function useLoginTheme() {
  const [pref, setPref] = useState<LoginThemePref>(
    () => (localStorage.getItem(THEME_STORAGE_KEY) as LoginThemePref) || "system"
  );
  const [sysDark, setSysDark] = useState(
    () => window.matchMedia("(prefers-color-scheme: dark)").matches
  );

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = (e: MediaQueryListEvent) => setSysDark(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  const save = (t: LoginThemePref) => {
    localStorage.setItem(THEME_STORAGE_KEY, t);
    // Notify ThemeProvider in the same tab — native storage event is cross-tab only
    window.dispatchEvent(new StorageEvent("storage", {
      key: THEME_STORAGE_KEY, newValue: t, storageArea: localStorage,
    }));
    setPref(t);
  };

  const isDark = pref === "dark" || (pref === "system" && sysDark);
  return { pref, setPref: save, isDark };
}

/* ── Dynamic styles — fully visible in both modes ──────────────── */
function inputStyle(isDark: boolean): React.CSSProperties {
  return {
    width: "100%", padding: "11px 14px", fontSize: 14,
    fontFamily: "var(--font-body)", outline: "none", boxSizing: "border-box",
    borderRadius: 10, transition: "border-color 0.15s",
    background: isDark ? "rgba(255,255,255,0.09)" : "#fff",
    border: isDark ? "1px solid rgba(255,255,255,0.22)" : "1px solid #c8ccd4",
    color: isDark ? "#fff" : "#111",
  };
}

function labelStyle(isDark: boolean): React.CSSProperties {
  return {
    display: "block", fontSize: 11, fontWeight: 600,
    /* Light: same near-black as "Sign in to your account" subtitle */
    color: isDark ? "rgba(255,255,255,0.55)" : "#1a1a2e",
    textTransform: "uppercase", fontFamily: "var(--font-mono)",
    letterSpacing: "0.06em", marginBottom: 5,
  };
}

/* Fallbacks used by SSO buttons and any inline styles */
const INPUT_STYLE: React.CSSProperties = inputStyle(true);
const LABEL_STYLE: React.CSSProperties = labelStyle(true);

const PROVIDER_LABEL: Record<string, string> = {
  google: "Google", github: "GitHub", azure: "Microsoft", saml: "SSO",
};
const PROVIDER_ICON: Record<string, string> = {
  google: "G", github: "⌥", azure: "M", saml: "🔒",
};

/* Wordmark inside the glass card — just the subtitle, no brand name */
function Logo() {
  const isDark = useDark();
  return (
    <div style={{ marginBottom: 28 }}>
      <div style={{ fontSize: 13, fontWeight: 500, color: isDark ? "rgba(255,255,255,0.5)" : "#1a1a2e" }}>
        Sign in to your account
      </div>
    </div>
  );
}

function Msg({ msg }: { msg: { text: string; ok: boolean } | null }) {
  if (!msg) return null;
  return (
    <div style={{ fontSize: 12, marginBottom: 14, padding: "8px 12px", borderRadius: 8,
      background: msg.ok ? "rgba(16,185,129,0.18)" : "rgba(239,68,68,0.18)",
      color: msg.ok ? "#059669" : "#dc2626",
      border: `1px solid ${msg.ok ? "rgba(5,150,105,0.35)" : "rgba(220,38,38,0.35)"}` }}>
      {msg.text}
    </div>
  );
}

/* Theme toggle pill */
function ThemeToggle({ pref, onChange }: { pref: LoginThemePref; onChange: (t: LoginThemePref) => void }) {
  const isDark = useDark();
  const btn = (t: LoginThemePref, icon: string, label: string) => (
    <button
      key={t}
      title={label}
      onClick={() => onChange(t)}
      style={{
        padding: "5px 12px", fontSize: 13, border: "none", cursor: "pointer",
        borderRadius: 8, fontFamily: "var(--font-mono)",
        background: pref === t
          ? (isDark ? "rgba(255,255,255,0.18)" : "rgba(0,0,0,0.12)")
          : "transparent",
        color: isDark ? (pref === t ? "#fff" : "rgba(255,255,255,0.4)") : (pref === t ? "#111" : "rgba(0,0,0,0.35)"),
        transition: "all 0.15s",
      }}
    >
      {icon}
    </button>
  );
  return (
    <div style={{
      display: "inline-flex", alignItems: "center", gap: 2,
      padding: "4px", borderRadius: 12,
      background: isDark ? "rgba(255,255,255,0.07)" : "rgba(0,0,0,0.07)",
      border: isDark ? "1px solid rgba(255,255,255,0.1)" : "1px solid rgba(0,0,0,0.1)",
    }}>
      {btn("light",  "☀", "Light")}
      {btn("system", "⊙", "System")}
      {btn("dark",   "☽", "Dark")}
    </div>
  );
}

function Box({ isDark, pref, setPref, children }: {
  isDark: boolean; pref: LoginThemePref;
  setPref: (t: LoginThemePref) => void; children: React.ReactNode;
}) {
  const pageBg  = isDark ? "#0d1117"
    : "linear-gradient(135deg,#cfe8f5 0%,#ddeaf7 40%,#c8e3f0 100%)";
  const cardBg  = isDark ? "rgba(255,255,255,0.07)" : "rgba(255,255,255,0.38)";
  const cardBdr = isDark ? "rgba(255,255,255,0.14)" : "rgba(255,255,255,0.75)";
  const cardShad = isDark
    ? "0 32px 80px rgba(0,0,0,0.55), inset 0 1px 0 rgba(255,255,255,0.10)"
    : "0 24px 60px rgba(0,0,0,0.10), inset 0 1px 0 rgba(255,255,255,0.9)";

  return (
    <LoginThemeCtx.Provider value={{ isDark }}>
      <div style={{ display: "flex", height: "100vh", overflow: "hidden", background: pageBg, transition: "background 0.4s" }}>

        {/* Theme toggle — top right corner */}
        <div style={{ position: "absolute", top: 20, right: 24, zIndex: 10 }}>
          <ThemeToggle pref={pref} onChange={setPref} />
        </div>

        {/* ── LEFT HALF — single stacked block: logo → name → description ── */}
        <div style={{
          width: "50%",
          display: "flex", alignItems: "center",
          justifyContent: "flex-end",   /* push content toward centre seam */
          paddingRight: "6%",
        }}>
          <div style={{
            display: "flex", flexDirection: "column",
            alignItems: "center", textAlign: "center",
            maxWidth: 380,
          }}>
            {/* Logo directly above the text */}
            <img
              src="/velaris.png"
              alt={BRAND.name}
              style={{
                width: 350, height: 350,
                objectFit: "contain",
                marginBottom: 24,
                filter: isDark
                  ? "drop-shadow(0 16px 48px rgba(0,0,0,0.6))"
                  : "drop-shadow(0 12px 32px rgba(0,0,0,0.3))",
              }}
            />

            {/* Velaris — always teal */}
            <div style={{
              fontFamily: "var(--font-mono)", fontWeight: 800,
              fontSize: 42, letterSpacing: "0.06em",
              color: "#0d9488", marginBottom: 14, lineHeight: 1.1,
            }}>
              {BRAND.name}
            </div>

            {/* Description — white dark mode, black light mode */}
            <p style={{
              fontSize: 15, lineHeight: 1.8, margin: 0,
              color: isDark ? "#fff" : "#111",
              fontWeight: 400, maxWidth: 360,
            }}>
              Enterprise case management and process automation — structured workflows, AI-assisted triage, and real-time tracking built for operations teams.
              Full source control, zero SaaS lock-in.
            </p>
          </div>
        </div>

        {/* ── RIGHT HALF — glass card toward centre seam ── */}
        <div style={{
          width: "50%",
          display: "flex", alignItems: "center",
          justifyContent: "flex-start",   /* push card toward centre */
          paddingLeft: "6%", paddingRight: "6%",
        }}>
          <div style={{
            width: "100%", maxWidth: 400,
            padding: "40px 36px",
            borderRadius: 20,
            backdropFilter: "blur(48px) saturate(180%)",
            WebkitBackdropFilter: "blur(48px) saturate(180%)",
            background: cardBg,
            border: `1px solid ${cardBdr}`,
            boxShadow: cardShad,
            transition: "background 0.3s, border-color 0.3s",
          }}>
            {children}
          </div>
        </div>

      </div>
    </LoginThemeCtx.Provider>
  );
}

export default function LoginPage() {
  const { login, error: authError, loading } = useAuth();
  /* Theme lives here so LoginPage can compute IS/LS with the real value */
  const { pref, setPref, isDark } = useLoginTheme();

  const [stage, setStage]           = useState<Stage>("login");
  const [username, setUsername]     = useState("");
  const [password, setPassword]     = useState("");
  const [mfaToken, setMfaToken]     = useState("");
  const [forgotEmail, setForgotEmail] = useState("");
  const [otp, setOtp]               = useState("");
  const [newPass, setNewPass]       = useState("");
  const [confirmPass, setConfirmPass] = useState("");
  const [busy, setBusy]             = useState(false);
  const [msg, setMsg]               = useState<{ text: string; ok: boolean } | null>(null);
  const [ssoProviders, setSsoProviders] = useState<SsoProvider[]>([]);
  // Holds a short-lived token for the forced-change-password flow
  const pendingTokenRef = useRef<string | null>(null);

  // Load SSO providers (non-blocking)
  useEffect(() => {
    fetch("/api/v1/auth/real/sso/providers", {
      headers: { Authorization: `Bearer ${localStorage.getItem("helix_token") ?? ""}` },
    })
      .then(r => r.ok ? r.json() : { providers: [] })
      .then(d => setSsoProviders(d.providers ?? []))
      .catch(() => {});
  }, []);

  if (loading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100vh", background: "var(--bg-root)" }}>
        <Spinner size={32} />
      </div>
    );
  }

  // ── Handlers ────────────────────────────────────────────────────

  const handleLogin = async () => {
    if (!username.trim() || !password.trim()) return;
    setBusy(true); setMsg(null);
    try {
      const r = await fetch("/api/v1/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      const d = await r.json();
      if (!r.ok) {
        throw new Error(d.detail ?? "Login failed");
      }
      // MFA must be resolved first — token is empty until MFA is verified
      if (d.mfa_required) {
        setStage("mfa");
        return;
      }
      // Password change required — we have a real token at this point
      if (d.user?.password_change_required) {
        pendingTokenRef.current = d.access_token;
        setStage("change_required");
        return;
      }
      // Normal path — commit to auth context
      await login(username.trim(), password);
    } catch (e: any) {
      setMsg({ text: e.message || "Login failed", ok: false });
    } finally { setBusy(false); }
  };

  const handleChangeRequired = async () => {
    if (newPass !== confirmPass) { setMsg({ text: "Passwords do not match.", ok: false }); return; }
    if (newPass.length < 8)      { setMsg({ text: "Minimum 8 characters.", ok: false }); return; }
    setBusy(true); setMsg(null);
    try {
      const r = await fetch("/api/v1/auth/real/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${pendingTokenRef.current ?? ""}` },
        body: JSON.stringify({ current_password: password, new_password: newPass }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Password change failed");
      pendingTokenRef.current = null;
      // Now log in properly with the new password
      await login(username.trim(), newPass);
    } catch (e: any) { setMsg({ text: e.message, ok: false }); }
    finally { setBusy(false); }
  };

  const handleDevLogin = async (role: string) => {
    setBusy(true);
    try { await login(role); } catch { /* ignore */ }
    finally { setBusy(false); }
  };

  const handleMfa = async () => {
    if (!mfaToken.trim()) return;
    setBusy(true); setMsg(null);
    try {
      const r = await fetch("/api/v1/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password, mfa_token: mfaToken }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Invalid MFA code.");
      // MFA succeeded — we now have a real token.
      // Check if password change is also required.
      if (d.user?.password_change_required) {
        pendingTokenRef.current = d.access_token;
        setStage("change_required");
        return;
      }
      localStorage.setItem("helix_token", d.access_token);
      window.location.reload();
    } catch (e: any) { setMsg({ text: e.message, ok: false }); }
    finally { setBusy(false); }
  };

  const handleForgot = async () => {
    if (!forgotEmail.trim()) return;
    setBusy(true); setMsg(null);
    try {
      const r = await fetch("/api/v1/auth/real/forgot-password", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: forgotEmail }),
      });
      const d = await r.json();
      setMsg({ text: d.message ?? "OTP sent.", ok: true });
      if (r.ok) setStage("reset");
    } catch (e: any) { setMsg({ text: e.message, ok: false }); }
    finally { setBusy(false); }
  };

  const handleReset = async () => {
    if (newPass !== confirmPass) { setMsg({ text: "Passwords do not match.", ok: false }); return; }
    if (newPass.length < 8) { setMsg({ text: "Password must be at least 8 characters.", ok: false }); return; }
    setBusy(true); setMsg(null);
    try {
      const r = await fetch("/api/v1/auth/real/reset-password", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: forgotEmail, otp, new_password: newPass }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Reset failed");
      setMsg({ text: "Password reset. Please log in.", ok: true });
      setStage("login");
      setPassword("");
    } catch (e: any) { setMsg({ text: e.message, ok: false }); }
    finally { setBusy(false); }
  };

  const handleSso = async (provider: SsoProvider) => {
    setBusy(true);
    try {
      const redirect = `${window.location.origin}/auth/sso/callback`;
      const r = await fetch(`/api/v1/auth/real/sso/${provider.provider}/auth-url?redirect_uri=${encodeURIComponent(redirect)}`, {
        headers: { "Content-Type": "application/json" },
      });
      const d = await r.json();
      if (d.auth_url) window.location.href = d.auth_url;
    } catch { setBusy(false); }
  };

  // Computed per-render so every stage picks up the current mode
  const IS = inputStyle(isDark);
  const LS = labelStyle(isDark);
  const textMain = isDark ? "#fff" : "#111";
  const textSub  = isDark ? "rgba(255,255,255,0.5)" : "#666";
  const backBtn  = { display: "block", margin: "12px auto 0", background: "none", border: "none", cursor: "pointer", fontSize: 12, color: textSub } as React.CSSProperties;

  // ── MFA stage ────────────────────────────────────────────────────
  if (stage === "mfa") return (
    <Box isDark={isDark} pref={pref} setPref={setPref}>
      <Logo />
      <div style={{ textAlign: "center", marginBottom: 20 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: textMain }}>Two-Factor Authentication</div>
        <div style={{ fontSize: 12, color: textSub, marginTop: 4 }}>Enter the 6-digit code from your authenticator app</div>
      </div>
      <Msg msg={msg} />
      <input value={mfaToken} onChange={e => setMfaToken(e.target.value)} onKeyDown={e => e.key === "Enter" && handleMfa()}
        placeholder="000000" maxLength={6} autoFocus
        style={{ ...IS, textAlign: "center", letterSpacing: "0.3em", fontSize: 22, marginBottom: 16 }} />
      <Button onClick={handleMfa} disabled={busy || mfaToken.length !== 6} style={{ width: "100%", justifyContent: "center" }}>
        {busy ? "Verifying…" : "Verify"}
      </Button>
      <button onClick={() => setStage("login")} style={backBtn}>← Back to login</button>
    </Box>
  );

  // ── Forgot password stage ────────────────────────────────────────
  if (stage === "forgot") return (
    <Box isDark={isDark} pref={pref} setPref={setPref}>
      <Logo />
      <div style={{ fontSize: 14, fontWeight: 600, color: textMain, marginBottom: 4 }}>Forgot Password</div>
      <div style={{ fontSize: 12, color: textSub, marginBottom: 16 }}>Enter your email address to receive a reset OTP.</div>
      <Msg msg={msg} />
      <input value={forgotEmail} onChange={e => setForgotEmail(e.target.value)} onKeyDown={e => e.key === "Enter" && handleForgot()}
        placeholder="your@email.com" type="email" autoFocus style={{ ...IS, marginBottom: 16 }} />
      <Button onClick={handleForgot} disabled={busy || !forgotEmail.trim()} style={{ width: "100%", justifyContent: "center" }}>
        {busy ? "Sending…" : "Send OTP"}
      </Button>
      <button onClick={() => setStage("login")} style={backBtn}>← Back to login</button>
    </Box>
  );

  // ── Reset password stage ─────────────────────────────────────────
  if (stage === "reset") return (
    <Box isDark={isDark} pref={pref} setPref={setPref}>
      <Logo />
      <div style={{ fontSize: 14, fontWeight: 600, color: textMain, marginBottom: 4 }}>Reset Password</div>
      <div style={{ fontSize: 12, color: textSub, marginBottom: 16 }}>Check your email for the 6-digit OTP, then set a new password.</div>
      <Msg msg={msg} />
      <input value={otp} onChange={e => setOtp(e.target.value)} placeholder="OTP code" maxLength={6}
        style={{ ...IS, marginBottom: 10, textAlign: "center", letterSpacing: "0.2em" }} />
      <input value={newPass} onChange={e => setNewPass(e.target.value)} type="password" placeholder="New password (min 8 chars)"
        style={{ ...IS, marginBottom: 10 }} />
      <input value={confirmPass} onChange={e => setConfirmPass(e.target.value)} type="password" placeholder="Confirm new password"
        onKeyDown={e => e.key === "Enter" && handleReset()} style={{ ...IS, marginBottom: 16 }} />
      <Button onClick={handleReset} disabled={busy || !otp || !newPass} style={{ width: "100%", justifyContent: "center" }}>
        {busy ? "Resetting…" : "Reset Password"}
      </Button>
      <button onClick={() => setStage("forgot")} style={backBtn}>← Back</button>
    </Box>
  );

  // ── Change required stage (first login) ─────────────────────────
  if (stage === "change_required") return (
    <Box isDark={isDark} pref={pref} setPref={setPref}>
      <Logo />
      <div style={{ fontSize: 14, fontWeight: 600, color: textMain, marginBottom: 4 }}>Set a New Password</div>
      <div style={{ fontSize: 12, color: textSub, marginBottom: 16 }}>
        This is your first login. Please set a new password before continuing.
      </div>
      <Msg msg={msg} />
      <input value={newPass} onChange={e => setNewPass(e.target.value)} type="password"
        placeholder="New password (min 8 chars)" autoFocus style={{ ...IS, marginBottom: 10 }} />
      <input value={confirmPass} onChange={e => setConfirmPass(e.target.value)} type="password"
        placeholder="Confirm new password" onKeyDown={e => e.key === "Enter" && handleChangeRequired()}
        style={{ ...IS, marginBottom: 16 }} />
      <Button onClick={handleChangeRequired}
        disabled={busy || newPass.length < 8 || newPass !== confirmPass}
        style={{ width: "100%", justifyContent: "center" }}>
        {busy ? "Setting password…" : "Set Password & Sign In"}
      </Button>
    </Box>
  );

  // ── Main login stage ─────────────────────────────────────────────
  return (
    <Box isDark={isDark} pref={pref} setPref={setPref}>
      <Logo />

      {/* SSO buttons */}
      {ssoProviders.length > 0 && (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: "var(--space-md)" }}>
            {ssoProviders.map(p => (
              <button key={p.id} onClick={() => handleSso(p)} disabled={busy}
                style={{ padding: "10px 16px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", background: "var(--bg-elevated)", cursor: "pointer", fontSize: 13, display: "flex", alignItems: "center", gap: 10, color: "var(--text-primary)" }}>
                <span style={{ fontWeight: 700, fontSize: 16, width: 20, textAlign: "center" }}>{PROVIDER_ICON[p.provider] ?? "🔒"}</span>
                Continue with {PROVIDER_LABEL[p.provider] ?? p.provider}
              </button>
            ))}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: "var(--space-md)" }}>
            <div style={{ flex: 1, height: 1, background: "var(--border-subtle)" }} />
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>or</span>
            <div style={{ flex: 1, height: 1, background: "var(--border-subtle)" }} />
          </div>
        </>
      )}

      {/* Username + Password */}
      <div style={{ marginBottom: 10 }}>
        <label style={LS}>Username or Email</label>
        <input value={username} onChange={e => setUsername(e.target.value)}
          onKeyDown={e => e.key === "Enter" && handleLogin()}
          placeholder="admin" autoFocus style={IS} />
      </div>
      <div style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
          <label style={LS}>Password</label>
          <button onClick={() => { setForgotEmail(username); setStage("forgot"); }}
            style={{ background: "none", border: "none", cursor: "pointer", fontSize: 11, color: "#0d9488", padding: 0 }}>
            Forgot password?
          </button>
        </div>
        <input value={password} onChange={e => setPassword(e.target.value)} type="password"
          onKeyDown={e => e.key === "Enter" && handleLogin()}
          placeholder="••••••••" style={IS} />
      </div>

      {(authError || msg) && (
        <Msg msg={{ text: msg?.text ?? authError ?? "", ok: msg?.ok ?? false }} />
      )}

      <Button onClick={handleLogin} disabled={busy || !username.trim() || !password.trim()} style={{ width: "100%", justifyContent: "center", marginBottom: "var(--space-lg)" }}>
        {busy ? "Signing in…" : "Sign In"}
      </Button>

      {/* Dev mode quick buttons — hidden */}
    </Box>
  );
}
