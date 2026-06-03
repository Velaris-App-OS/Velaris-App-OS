/**
 * P33 + P39a + P39b + P39c — Customer Portal (public, no auth required)
 * ENH-11: Full visual redesign — professional, mobile-first, customer-facing
 * Route: /portal/:slug  ·  Standalone — no sidebar, no AppLayout.
 */
import React, { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { BRAND } from "@/branding";

// ── Types ─────────────────────────────────────────────────────────────────────

type PortalConfig = {
  slug: string; name: string; welcome_text: string;
  brand_color: string; logo_text: string; enabled: boolean;
  case_types: { id: string; name: string; description: string; default_priority: string }[];
};
type SubmitResult = { tracking_token: string; case_id: string; message: string };
type TrackResult  = {
  case_id: string; subject: string; status: string; priority: string;
  case_type_name: string; submitted_at: string; updated_at: string; resolved_at: string | null;
};
type DashCase = {
  case_id: string; case_number: string | null; tracking_token: string;
  subject: string; status: string; priority: string;
  case_type_name: string; submitted_at: string; updated_at: string | null; resolved_at: string | null;
};
type TimelineEvent = {
  id: string; action: string; label: string; timestamp: string;
  details: Record<string, string>;
};
type TimelineData = {
  case_id: string; case_number: string | null; subject: string;
  status: string; priority: string; case_type_name: string;
  submitted_at: string; resolved_at: string | null;
  timeline: TimelineEvent[];
  pending_payment_step: boolean;
};
type PortalDoc = {
  id: string; filename: string; content_type: string;
  size_bytes: number | null; source: string;
  uploaded_at: string; download_url: string;
};
type SLAInfo = {
  deadline_at: string; status: string; tier: "green" | "amber" | "red";
  remaining_seconds: number; breached: boolean; breached_at: string | null;
} | null;

// ── Constants ─────────────────────────────────────────────────────────────────

const STATUS_CFG: Record<string, { color: string; bg: string; icon: string; label: string }> = {
  new:         { color: "#0d9488", bg: "#eef2ff", icon: "🆕", label: "New" },
  open:        { color: "#3b82f6", bg: "#eff6ff", icon: "📂", label: "Open" },
  in_progress: { color: "#f59e0b", bg: "#fffbeb", icon: "⚙️", label: "In Progress" },
  pending:     { color: "#0f766e", bg: "#f5f3ff", icon: "⏳", label: "Pending" },
  resolved:    { color: "#22c55e", bg: "#f0fdf4", icon: "✅", label: "Resolved" },
  closed:      { color: "#6b7280", bg: "#f9fafb", icon: "🔒", label: "Closed" },
  cancelled:   { color: "#ef4444", bg: "#fef2f2", icon: "❌", label: "Cancelled" },
};

const STATUS_RAIL = ["new", "open", "in_progress", "pending", "resolved"];

const PRIORITY_CFG: Record<string, { color: string; label: string }> = {
  low:    { color: "#6b7280", label: "Low" },
  medium: { color: "#f59e0b", label: "Medium" },
  high:   { color: "#ef4444", label: "High" },
};

const ACTION_ICON: Record<string, string> = {
  case_created: "🎫", stage_transitioned: "➡️", status_changed: "🔄",
  document_uploaded: "📎", case_resolved: "✅", case_closed: "🔒", case_reopened: "🔓",
};

function sl(s: string) { return s.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()); }
function fmtDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}
function fmtDateTime(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
function darken(hex: string, amt: number): string {
  try {
    const n = parseInt(hex.replace("#", ""), 16);
    const r = Math.max(0, Math.min(255, (n >> 16) + amt));
    const g = Math.max(0, Math.min(255, ((n >> 8) & 0xff) + amt));
    const b = Math.max(0, Math.min(255, (n & 0xff) + amt));
    return `#${((1 << 24) + (r << 16) + (g << 8) + b).toString(16).slice(1)}`;
  } catch { return hex; }
}

// ── Main Component ────────────────────────────────────────────────────────────

type View = "home" | "submit" | "track" | "dashboard" | "timeline" | "login" | "account";

type CustomerProfile = {
  id: string; display_name: string;
  primary_email: string; alt_email: string | null;
  preferred_email: "primary" | "alt";
  phone: string | null; verified: boolean; case_count: number;
};
type CustomerCase = {
  case_id: string; case_number: string | null; tracking_token: string | null;
  subject: string; status: string; priority: string;
  submitted_at: string; updated_at: string | null;
};

export default function Portal() {
  const { slug } = useParams<{ slug: string }>();
  const [config, setConfig]   = useState<PortalConfig | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [view, setView]       = useState<View>("home");

  // Submit
  const [step, setStep]             = useState<1 | 2>(1);
  const [caseTypeId, setCaseTypeId] = useState("");
  const [name, setName]             = useState("");
  const [email, setEmail]           = useState("");
  const [subject, setSubject]       = useState("");
  const [description, setDesc]      = useState("");
  const [priority, setPriority]     = useState("medium");
  const [submitting, setSubmitting] = useState(false);
  const [submitErr, setSubmitErr]   = useState<string | null>(null);
  const [submitResult, setSubmitResult] = useState<SubmitResult | null>(null);

  // Track
  const [trackToken, setTrackToken]   = useState("");
  const [tracking, setTracking]       = useState(false);
  const [trackErr, setTrackErr]       = useState<string | null>(null);
  const [trackResult, setTrackResult] = useState<TrackResult | null>(null);
  const [uploadFile, setUploadFile]   = useState<File | null>(null);
  const [uploading, setUploading]     = useState(false);
  const [uploadMsg, setUploadMsg]     = useState<string | null>(null);
  const trackRef = React.useRef<HTMLDivElement>(null);

  // Dashboard
  const [dashEmail, setDashEmail]     = useState("");
  const [dashInput, setDashInput]     = useState("");
  const [dashLoading, setDashLoading] = useState(false);
  const [dashErr, setDashErr]         = useState<string | null>(null);
  const [dashCases, setDashCases]     = useState<DashCase[] | null>(null);

  // Timeline
  const [tlLoading, setTlLoading] = useState(false);
  const [tlErr, setTlErr]         = useState<string | null>(null);
  const [tlData, setTlData]       = useState<TimelineData | null>(null);
  const [docs, setDocs]           = useState<PortalDoc[]>([]);
  const [sla, setSla]             = useState<SLAInfo | undefined>(undefined);

  // AI pre-submit
  const [askQ, setAskQ]           = useState("");
  const [askAnswer, setAskAnswer] = useState<string | null>(null);
  const [askBusy, setAskBusy]     = useState(false);
  const [askOpen, setAskOpen]     = useState(false);
  const [askDone, setAskDone]     = useState(false);

  // Case chat
  const [chatMsg, setChatMsg]         = useState("");
  const [chatHistory, setChatHistory] = useState<{ role: "user" | "ai"; text: string }[]>([]);
  const [chatBusy, setChatBusy]       = useState(false);
  const [chatCaseId, setChatCaseId]   = useState<string | null>(null);

  // P65 — Customer account auth
  const [custToken, setCustToken]         = useState<string | null>(null);
  const [custProfile, setCustProfile]     = useState<CustomerProfile | null>(null);
  const [custCases, setCustCases]         = useState<CustomerCase[] | null>(null);
  const [authEmail, setAuthEmail]         = useState("");
  const [authName, setAuthName]           = useState("");
  const [authPhone, setAuthPhone]         = useState("");
  const [authOtp, setAuthOtp]             = useState("");
  const [authStep, setAuthStep]           = useState<"email" | "otp">("email");
  const [authMode, setAuthMode]           = useState<"login" | "register">("login");
  const [authBusy, setAuthBusy]           = useState(false);
  const [authErr, setAuthErr]             = useState<string | null>(null);
  // Profile edit
  const [editMode, setEditMode]           = useState(false);
  const [editName, setEditName]           = useState("");
  const [editPhone, setEditPhone]         = useState("");
  const [editAltEmail, setEditAltEmail]   = useState("");
  const [editPref, setEditPref]           = useState<"primary" | "alt">("primary");
  const [editBusy, setEditBusy]           = useState(false);
  const [editErr, setEditErr]             = useState<string | null>(null);

  // SD-2 / SD-5 — Bank details OTP flow
  // bdStep: 0 = request OTP, 1 = enter OTP + account details, 2 = success
  const [bdStep, setBdStep]               = useState<0 | 1 | 2>(0);
  const [bdEmail, setBdEmail]             = useState("");
  const [bdOtp, setBdOtp]                 = useState("");
  const [bdAccountName, setBdAccountName] = useState("");
  const [bdAccountNumber, setBdAccountNumber] = useState("");
  const [bdSortCode, setBdSortCode]       = useState("");
  const [bdBusy, setBdBusy]               = useState(false);
  const [bdErr, setBdErr]                 = useState<string | null>(null);

  const brand = config?.brand_color ?? "#0d9488";

  // ── Effects ─────────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!slug) return;
    fetch(`/api/v1/portal/${slug}`)
      .then(r => { if (!r.ok) return r.text().then(t => { throw new Error(t); }); return r.json(); })
      .then(setConfig)
      .catch(e => setLoadErr(e.message));
    // Restore customer session from localStorage
    const stored = localStorage.getItem(`helix_cust_${slug}`);
    if (stored) setCustToken(stored);
  }, [slug]);

  // Load customer profile whenever we have a token
  useEffect(() => {
    if (!custToken || !slug) return;
    fetch(`/api/v1/portal/${slug}/account`, { headers: { Authorization: `Bearer ${custToken}` } })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setCustProfile(d); else { setCustToken(null); localStorage.removeItem(`helix_cust_${slug}`); } })
      .catch(() => {});
  }, [custToken, slug]);

  // Load account cases when account view opens
  useEffect(() => {
    if (view !== "account" || !custToken || !slug) return;
    fetch(`/api/v1/portal/${slug}/account/cases`, { headers: { Authorization: `Bearer ${custToken}` } })
      .then(r => r.ok ? r.json() : { cases: [] })
      .then(d => setCustCases(d.cases ?? []))
      .catch(() => setCustCases([]));
  }, [view, custToken, slug]);

  useEffect(() => {
    if (view === "track" && trackToken && !trackResult && !tracking)
      handleTrack({ preventDefault: () => {} } as React.FormEvent);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, trackToken]);

  useEffect(() => {
    if (trackResult && trackRef.current)
      trackRef.current.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [trackResult]);

  useEffect(() => {
    if (view === "dashboard" && email && !dashEmail) setDashInput(email);
  }, [view, email, dashEmail]);

  // ── Navigation ───────────────────────────────────────────────────────────────

  function go(v: View) {
    setView(v); setSubmitErr(null); setTrackErr(null); setDashErr(null); setStep(1);
    setSubmitResult(null);
    if (v === "login") { setAuthStep("email"); setAuthErr(null); setAuthOtp(""); }
    if (v === "account") { setCustCases(null); setEditMode(false); }
  }

  function custLogout() {
    localStorage.removeItem(`helix_cust_${slug}`);
    setCustToken(null); setCustProfile(null); setCustCases(null);
    go("home");
  }

  async function handleAuthSubmitEmail(e: React.FormEvent) {
    e.preventDefault(); setAuthBusy(true); setAuthErr(null);
    try {
      const endpoint = authMode === "register"
        ? `/api/v1/portal/${slug}/auth/register`
        : `/api/v1/portal/${slug}/auth/request-otp`;
      const body: Record<string, string> = { email: authEmail.trim() };
      if (authMode === "register") { body.display_name = authName.trim(); if (authPhone.trim()) body.phone = authPhone.trim(); }
      const r = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Request failed");
      setAuthStep("otp");
    } catch (e: any) { setAuthErr(e.message); }
    finally { setAuthBusy(false); }
  }

  async function handleAuthVerifyOtp(e: React.FormEvent) {
    e.preventDefault(); setAuthBusy(true); setAuthErr(null);
    try {
      const r = await fetch(`/api/v1/portal/${slug}/auth/verify-otp`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: authEmail.trim(), otp: authOtp.trim() }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Invalid code");
      localStorage.setItem(`helix_cust_${slug}`, d.customer_token);
      setCustToken(d.customer_token);
      setCustProfile(d.customer);
      go("account");
    } catch (e: any) { setAuthErr(e.message); }
    finally { setAuthBusy(false); }
  }

  async function handleProfileSave() {
    if (!custToken) return;
    setEditBusy(true); setEditErr(null);
    try {
      const body: Record<string, string | null> = {
        display_name: editName.trim() || null,
        phone: editPhone.trim() || null,
        alt_email: editAltEmail.trim() || null,
        preferred_email: editPref,
      };
      const r = await fetch(`/api/v1/portal/${slug}/account`, {
        method: "PUT", headers: { "Content-Type": "application/json", Authorization: `Bearer ${custToken}` },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error("Save failed");
      // Refresh profile
      const pr = await fetch(`/api/v1/portal/${slug}/account`, { headers: { Authorization: `Bearer ${custToken}` } });
      if (pr.ok) setCustProfile(await pr.json());
      setEditMode(false);
    } catch (e: any) { setEditErr(e.message); }
    finally { setEditBusy(false); }
  }

  // ── Handlers ────────────────────────────────────────────────────────────────

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!caseTypeId || !name.trim() || !email.trim() || !subject.trim() || !description.trim()) {
      setSubmitErr("All fields are required."); return;
    }
    setSubmitting(true); setSubmitErr(null);
    try {
      const r = await fetch(`/api/v1/portal/${slug}/submit`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ case_type_id: caseTypeId, submitter_name: name, submitter_email: email, subject, description, priority }),
      });
      if (!r.ok) throw new Error(await r.text());
      setSubmitResult(await r.json());
    } catch (err: any) { setSubmitErr(err.message); }
    finally { setSubmitting(false); }
  }

  async function handleTrack(e: React.FormEvent) {
    e.preventDefault();
    if (!trackToken.trim()) return;
    setTracking(true); setTrackErr(null); setTrackResult(null);
    try {
      const r = await fetch(`/api/v1/portal/${slug}/track/${trackToken.trim()}`);
      if (!r.ok) throw new Error(await r.text());
      setTrackResult(await r.json());
    } catch (err: any) { setTrackErr(err.message || "Token not found"); }
    finally { setTracking(false); }
  }

  async function handleUpload() {
    if (!uploadFile || !trackResult) return;
    const token = trackToken.trim() || (submitResult?.tracking_token ?? "");
    setUploading(true); setUploadMsg(null);
    try {
      const form = new FormData(); form.append("file", uploadFile);
      const r = await fetch(`/api/v1/portal/${slug}/track/${token}/documents`, { method: "POST", body: form });
      if (!r.ok) throw new Error(await r.text());
      const res = await r.json();
      setUploadMsg(`Uploaded "${res.filename}" (${(res.size / 1024).toFixed(1)} KB)`);
      setUploadFile(null);
    } catch (err: any) { setUploadMsg(`Error: ${err.message}`); }
    finally { setUploading(false); }
  }

  async function handleDashboard(e: React.FormEvent) {
    e.preventDefault();
    if (!dashInput.trim()) return;
    setDashLoading(true); setDashErr(null); setDashCases(null); setDashEmail(dashInput.trim());
    try {
      const r = await fetch(`/api/v1/portal/${slug}/my-cases?email=${encodeURIComponent(dashInput.trim())}`);
      if (!r.ok) throw new Error(await r.text());
      setDashCases((await r.json()).cases);
    } catch (err: any) { setDashErr(err.message); }
    finally { setDashLoading(false); }
  }

  async function openTimeline(caseId: string, emailAddr: string) {
    setTlLoading(true); setTlErr(null); setTlData(null); setDocs([]); setSla(undefined);
    setChatCaseId(caseId); setChatHistory([]); setChatMsg(""); setView("timeline");
    try {
      const enc = encodeURIComponent(emailAddr);
      const [tl, docsR, slaR] = await Promise.all([
        fetch(`/api/v1/portal/${slug}/cases/${caseId}/timeline?email=${enc}`),
        fetch(`/api/v1/portal/${slug}/cases/${caseId}/documents?email=${enc}`),
        fetch(`/api/v1/portal/${slug}/cases/${caseId}/sla?email=${enc}`),
      ]);
      if (!tl.ok) throw new Error(await tl.text());
      setTlData(await tl.json());
      if (docsR.ok) { const d = await docsR.json(); setDocs(d.documents ?? []); }
      if (slaR.ok)  { const s = await slaR.json(); setSla(s.sla ?? null); }
    } catch (err: any) { setTlErr(err.message); }
    finally { setTlLoading(false); }
  }

  async function handleAsk(e: React.FormEvent) {
    e.preventDefault();
    if (!askQ.trim()) return;
    setAskBusy(true); setAskAnswer(null);
    try {
      const r = await fetch(`/api/v1/portal/${slug}/ask`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: askQ }),
      });
      if (r.ok) setAskAnswer((await r.json()).answer);
    } catch { /* additive */ }
    finally { setAskBusy(false); }
  }

  async function handleChat(e: React.FormEvent, caseId: string, emailAddr: string) {
    e.preventDefault();
    if (!chatMsg.trim()) return;
    const msg = chatMsg; setChatMsg("");
    setChatHistory(h => [...h, { role: "user", text: msg }]);
    setChatBusy(true);
    try {
      const r = await fetch(
        `/api/v1/portal/${slug}/cases/${caseId}/chat?email=${encodeURIComponent(emailAddr)}`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: msg }) },
      );
      if (r.ok) { const d = await r.json(); setChatHistory(h => [...h, { role: "ai", text: d.reply }]); }
    } catch { setChatHistory(h => [...h, { role: "ai", text: "Sorry, couldn't respond right now." }]); }
    finally { setChatBusy(false); }
  }

  async function handleBankOtpRequest(e: React.FormEvent) {
    e.preventDefault();
    if (!bdEmail.trim() || !tlData) return;
    setBdBusy(true); setBdErr(null);
    try {
      const r = await fetch(
        `/api/v1/portal/${slug}/cases/${tlData.case_id}/bank-details/request-otp`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: bdEmail.trim() }) },
      );
      if (!r.ok) {
        const text = await r.text();
        setBdErr(text.includes("429") ? "OTP request limit reached. Please try again after 24 hours." : "Could not send code. Please try again.");
        return;
      }
      setBdStep(1);
    } catch { setBdErr("Network error. Please try again."); }
    finally { setBdBusy(false); }
  }

  async function handleBankDetailsSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!tlData) return;
    if (!bdOtp.trim() || !bdAccountName.trim() || !bdAccountNumber.trim() || !bdSortCode.trim()) {
      setBdErr("All fields are required."); return;
    }
    setBdBusy(true); setBdErr(null);
    try {
      const r = await fetch(
        `/api/v1/portal/${slug}/cases/${tlData.case_id}/bank-details/submit`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            email: bdEmail.trim(), otp: bdOtp.trim(),
            account_name: bdAccountName.trim(),
            account_number: bdAccountNumber.trim(),
            sort_code: bdSortCode.trim(),
          }) },
      );
      if (!r.ok) {
        const d = await r.json().catch(() => ({})) as Record<string, unknown>;
        setBdErr(typeof d.detail === "string" ? d.detail : "Could not save details. Check your code and try again.");
        return;
      }
      setBdStep(2);
    } catch { setBdErr("Network error. Please try again."); }
    finally { setBdBusy(false); }
  }

  // ── Guards ───────────────────────────────────────────────────────────────────

  if (loadErr) return (
    <Shell brand="#0d9488">
      <div style={{ maxWidth: 400, margin: "80px auto", textAlign: "center" }}>
        <div style={{ fontSize: 48, marginBottom: 16 }}>⚠️</div>
        <div style={{ fontWeight: 700, fontSize: 17, color: "#111827", marginBottom: 8 }}>
          {loadErr.includes("not currently active") ? "Portal Unavailable"
            : loadErr.includes("not found") ? "Portal Not Found" : "Unable to Load"}
        </div>
        <div style={{ fontSize: 14, color: "#6b7280" }}>
          {loadErr.includes("not currently active") ? "This portal is currently inactive."
            : loadErr.includes("not found") ? "No portal exists at this address."
            : loadErr}
        </div>
      </div>
    </Shell>
  );

  if (!config) return (
    <Shell brand="#0d9488">
      <div style={{ textAlign: "center", padding: 80 }}>
        <div style={{ width: 36, height: 36, border: "3px solid #e5e7eb", borderTopColor: "#0d9488", borderRadius: "50%", animation: "pspin 0.8s linear infinite", margin: "0 auto 16px" }} />
        <div style={{ color: "#9ca3af", fontSize: 14 }}>Loading portal…</div>
      </div>
    </Shell>
  );

  const NAV: [View, string, string][] = [
    ["home",      "Home",                                              "🏠"],
    ["submit",    "Submit Request",                                    "📝"],
    ["track",     "Track Status",                                      "🔍"],
    ["dashboard", "My Requests",                                       "📋"],
    // P65 HIDDEN — uncomment to enable Customer Accounts
    // [custToken ? "account" : "login", custToken ? (custProfile?.display_name ?? "My Account") : "Login / Register", custToken ? "👤" : "🔑"],
  ];

  return (
    <Shell brand={brand}>
      {/* ── Header ──────────────────────────────────────────────── */}
      <header style={{
        background: `linear-gradient(135deg, ${brand} 0%, ${darken(brand, -24)} 100%)`,
        color: "#fff",
      }}>
        <div style={{ maxWidth: 780, margin: "0 auto", padding: "20px 20px 0", display: "flex", alignItems: "center", gap: 14 }}>
          <div style={{
            width: 48, height: 48, borderRadius: 12, background: "rgba(255,255,255,0.22)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 22, fontWeight: 800, flexShrink: 0,
          }}>
            {config.logo_text.charAt(0).toUpperCase()}
          </div>
          <div>
            <div style={{ fontWeight: 800, fontSize: 19, letterSpacing: "-0.02em" }}>{config.logo_text}</div>
            <div style={{ fontSize: 12, opacity: 0.75, marginTop: 1 }}>Customer Support Portal</div>
          </div>
        </div>

        {view !== "timeline" && !submitResult && (
          <div style={{ maxWidth: 780, margin: "0 auto", padding: "14px 20px 0", display: "flex", gap: 2, overflowX: "auto" }}>
            {NAV.map(([v, label, icon]) => (
              <button key={v} onClick={() => go(v)} style={{
                padding: "9px 16px", border: "none",
                background: view === v ? "rgba(255,255,255,0.18)" : "transparent",
                borderRadius: "8px 8px 0 0", cursor: "pointer", fontSize: 13,
                fontWeight: view === v ? 700 : 500,
                color: view === v ? "#fff" : "rgba(255,255,255,0.65)",
                borderBottom: view === v ? "2px solid #fff" : "2px solid transparent",
                whiteSpace: "nowrap", display: "flex", alignItems: "center", gap: 6,
              }}>
                <span style={{ fontSize: 13 }}>{icon}</span>{label}
              </button>
            ))}
          </div>
        )}
      </header>

      {/* ── Body ────────────────────────────────────────────────── */}
      <main style={{ maxWidth: 780, margin: "0 auto", padding: "28px 20px 60px" }}>

        {/* ════ HOME ════════════════════════════════════════════════════════ */}
        {view === "home" && (
          <div>
            {/* Hero card */}
            <div style={{ ...C.card, borderTop: `4px solid ${brand}`, padding: "28px 28px 24px", marginBottom: 24 }}>
              <div style={{ fontWeight: 800, fontSize: 22, color: "#111827", marginBottom: 8, letterSpacing: "-0.02em" }}>
                {config.welcome_text || `Welcome to ${config.name}`}
              </div>
              <div style={{ fontSize: 14, color: "#6b7280", lineHeight: 1.6, marginBottom: 24 }}>
                Submit a support request, track an existing case by token, or view your full request history.
              </div>
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                {([
                  { v: "submit" as View,    icon: "📝", title: "Submit Request", desc: "Open a new support case" },
                  { v: "track" as View,     icon: "🔍", title: "Track Status",   desc: "Check by tracking token" },
                  { v: "dashboard" as View, icon: "📋", title: "My Requests",    desc: "View all your cases" },
                ] as { v: View; icon: string; title: string; desc: string }[]).map(a => (
                  <button key={a.v} onClick={() => go(a.v)} style={{
                    flex: "1 1 130px", padding: "16px 14px", border: "1px solid #eaecf0",
                    borderRadius: 12, cursor: "pointer", background: "#fff",
                    boxShadow: "0 1px 4px rgba(0,0,0,0.06)", textAlign: "left",
                    transition: "transform 0.12s, box-shadow 0.12s",
                  }}>
                    <div style={{ fontSize: 24, marginBottom: 8 }}>{a.icon}</div>
                    <div style={{ fontWeight: 700, fontSize: 13, color: "#111827", marginBottom: 2 }}>{a.title}</div>
                    <div style={{ fontSize: 12, color: "#9ca3af" }}>{a.desc}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* Services */}
            {config.case_types.length > 0 && (
              <>
                <div style={C.sectionLabel}>Available Services</div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 12 }}>
                  {config.case_types.map(ct => (
                    <div key={ct.id} style={{ ...C.card, cursor: "pointer", padding: "16px 18px", display: "flex", gap: 14, alignItems: "flex-start" }}
                      onClick={() => { setCaseTypeId(ct.id); go("submit"); }}>
                      <div style={{ width: 36, height: 36, borderRadius: 8, background: brand + "18", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 16, flexShrink: 0 }}>
                        📋
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontWeight: 700, fontSize: 13, color: "#111827", marginBottom: 3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{ct.name}</div>
                        {ct.description && <div style={{ fontSize: 12, color: "#6b7280", lineHeight: 1.4 }}>{ct.description}</div>}
                        <div style={{ marginTop: 8, fontSize: 12, color: brand, fontWeight: 600 }}>Get started →</div>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}

        {/* ════ SUBMIT ══════════════════════════════════════════════════════ */}
        {view === "submit" && !submitResult && (
          <div>
            {/* Step progress */}
            <div style={{ marginBottom: 20 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
                <span style={{ fontSize: 13, fontWeight: 700, color: "#111827" }}>
                  {step === 1 ? "Step 1 — Request Type & Subject" : "Step 2 — Your Details"}
                </span>
                <span style={{ fontSize: 11, color: "#9ca3af" }}>Step {step} of 2</span>
              </div>
              <div style={{ height: 4, background: "#e5e7eb", borderRadius: 2, overflow: "hidden" }}>
                <div style={{ width: step === 1 ? "50%" : "100%", height: "100%", background: brand, borderRadius: 2, transition: "width 0.3s ease" }} />
              </div>
            </div>

            {/* AI helper */}
            {!askDone && (
              <div style={{ ...C.card, marginBottom: 16, borderLeft: `3px solid ${brand}`, padding: "14px 16px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ fontWeight: 700, fontSize: 13, color: brand }}>
                    💡 Not sure you need to submit? Ask our AI first
                  </span>
                  <div style={{ display: "flex", gap: 6 }}>
                    <button onClick={() => setAskOpen(o => !o)}
                      style={{ ...C.ghost, fontSize: 11, color: brand }}>
                      {askOpen ? "Hide" : "Ask"}
                    </button>
                    <button onClick={() => setAskDone(true)}
                      style={{ ...C.ghost, fontSize: 11, color: "#9ca3af" }}>✕</button>
                  </div>
                </div>
                {askOpen && (
                  <div style={{ marginTop: 12 }}>
                    {!askAnswer ? (
                      <form onSubmit={handleAsk} style={{ display: "flex", gap: 8 }}>
                        <input value={askQ} onChange={e => setAskQ(e.target.value)}
                          placeholder="Describe your issue and we'll check for an instant answer…"
                          style={{ ...C.input, flex: 1, minWidth: 0, marginBottom: 0, fontSize: 13 }} />
                        <button type="submit" disabled={askBusy} style={{ ...C.primary(brand), opacity: askBusy ? 0.6 : 1, padding: "9px 16px" }}>
                          {askBusy ? "…" : "Ask"}
                        </button>
                      </form>
                    ) : (
                      <>
                        <div style={{ fontSize: 13, color: "#374151", lineHeight: 1.6, background: "#f9fafb", borderRadius: 8, padding: "10px 12px", marginBottom: 10 }}>
                          {askAnswer}
                        </div>
                        <div style={{ display: "flex", gap: 8 }}>
                          <button onClick={() => setAskDone(true)}
                            style={{ ...C.primary("#16a34a"), padding: "6px 14px", fontSize: 12 }}>
                            ✓ That helped
                          </button>
                          <button onClick={() => { setAskAnswer(null); setAskQ(""); }}
                            style={{ ...C.secondary, padding: "6px 14px", fontSize: 12 }}>
                            Ask again
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>
            )}

            <div style={C.card}>
              {/* Step 1 */}
              {step === 1 && (
                <>
                  <div style={C.cardTitle}>What can we help you with?</div>
                  <div style={C.label}>Request Type *</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 20 }}>
                    {config.case_types.map(ct => (
                      <div key={ct.id} onClick={() => setCaseTypeId(ct.id)} style={{
                        padding: "12px 16px", border: `2px solid ${caseTypeId === ct.id ? brand : "#e5e7eb"}`,
                        borderRadius: 10, cursor: "pointer", background: caseTypeId === ct.id ? brand + "08" : "#fff",
                        display: "flex", alignItems: "flex-start", gap: 12, transition: "border-color 0.12s",
                      }}>
                        <div style={{
                          width: 18, height: 18, borderRadius: "50%", marginTop: 1, flexShrink: 0,
                          border: `2px solid ${caseTypeId === ct.id ? brand : "#d1d5db"}`,
                          background: caseTypeId === ct.id ? brand : "#fff",
                          display: "flex", alignItems: "center", justifyContent: "center",
                        }}>
                          {caseTypeId === ct.id && <div style={{ width: 7, height: 7, borderRadius: "50%", background: "#fff" }} />}
                        </div>
                        <div>
                          <div style={{ fontWeight: 600, fontSize: 13, color: "#111827" }}>{ct.name}</div>
                          {ct.description && <div style={{ fontSize: 12, color: "#6b7280", marginTop: 2, lineHeight: 1.4 }}>{ct.description}</div>}
                        </div>
                      </div>
                    ))}
                  </div>
                  <div style={C.label}>Subject *</div>
                  <input value={subject} onChange={e => setSubject(e.target.value)}
                    placeholder="Brief summary of your request"
                    style={C.input} />
                  {submitErr && <p style={C.err}>{submitErr}</p>}
                  <button onClick={() => {
                    if (!caseTypeId) { setSubmitErr("Please select a request type."); return; }
                    if (!subject.trim()) { setSubmitErr("Please enter a subject."); return; }
                    setSubmitErr(null); setStep(2);
                  }} style={{ ...C.primary(brand), width: "100%", justifyContent: "center", padding: "12px" }}>
                    Continue →
                  </button>
                </>
              )}

              {/* Step 2 */}
              {step === 2 && (
                <form onSubmit={handleSubmit}>
                  <div style={C.cardTitle}>Your Details</div>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
                    <div>
                      <div style={C.label}>Full Name *</div>
                      <input value={name} onChange={e => setName(e.target.value)} placeholder="Jane Smith"
                        style={{ ...C.input, marginBottom: 0 }} required />
                    </div>
                    <div>
                      <div style={C.label}>Email Address *</div>
                      <input type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder="jane@example.com"
                        style={{ ...C.input, marginBottom: 0 }} required />
                    </div>
                  </div>
                  <div style={{ height: 14 }} />
                  <div style={C.label}>Description *</div>
                  <textarea value={description} onChange={e => setDesc(e.target.value)}
                    placeholder="Please describe your request in detail…"
                    rows={5} style={{ ...C.input, resize: "vertical" }} required />
                  <div style={C.label}>Priority</div>
                  <div style={{ display: "flex", gap: 8, marginBottom: 20 }}>
                    {(["low", "medium", "high"] as const).map(p => (
                      <button key={p} type="button" onClick={() => setPriority(p)} style={{
                        flex: 1, padding: "9px 4px", borderRadius: 8, cursor: "pointer", fontSize: 13,
                        fontWeight: priority === p ? 700 : 500, transition: "all 0.12s",
                        border: `2px solid ${priority === p ? PRIORITY_CFG[p].color : "#e5e7eb"}`,
                        background: priority === p ? PRIORITY_CFG[p].color + "12" : "#fff",
                        color: priority === p ? PRIORITY_CFG[p].color : "#6b7280",
                      }}>
                        {PRIORITY_CFG[p].label}
                      </button>
                    ))}
                  </div>
                  {submitErr && <p style={C.err}>{submitErr}</p>}
                  <div style={{ display: "flex", gap: 10 }}>
                    <button type="button" onClick={() => setStep(1)} style={{ ...C.secondary, flexShrink: 0 }}>← Back</button>
                    <button type="submit" disabled={submitting}
                      style={{ ...C.primary(brand), flex: 1, justifyContent: "center", opacity: submitting ? 0.7 : 1 }}>
                      {submitting ? "Submitting…" : "Submit Request"}
                    </button>
                  </div>
                </form>
              )}
            </div>
          </div>
        )}

        {/* ════ SUCCESS ═════════════════════════════════════════════════════ */}
        {submitResult && (
          <div style={{ ...C.card, textAlign: "center", borderTop: `4px solid ${brand}` }}>
            <div style={{ width: 64, height: 64, borderRadius: "50%", background: "#f0fdf4", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 32, margin: "0 auto 16px" }}>
              ✅
            </div>
            <div style={{ fontWeight: 800, fontSize: 22, color: "#111827", marginBottom: 8 }}>Request Submitted!</div>
            <div style={{ color: "#6b7280", fontSize: 14, marginBottom: 24 }}>{submitResult.message}</div>

            <div style={{ background: "#f8fafc", borderRadius: 10, padding: "18px 20px", marginBottom: 24, textAlign: "left" }}>
              <div style={{ ...C.sectionLabel, marginBottom: 8 }}>Your Tracking Token</div>
              <div style={{ fontFamily: "monospace", fontSize: 14, fontWeight: 700, color: "#111827", wordBreak: "break-all", marginBottom: 10 }}>
                {submitResult.tracking_token}
              </div>
              <button onClick={() => navigator.clipboard.writeText(submitResult.tracking_token)}
                style={{ ...C.secondary, padding: "5px 14px", fontSize: 12 }}>
                Copy Token
              </button>
            </div>

            <div style={{ fontSize: 13, color: "#9ca3af", marginBottom: 24 }}>
              Save this token to track your request status at any time.
            </div>
            <div style={{ display: "flex", gap: 10, justifyContent: "center", flexWrap: "wrap" }}>
              <button onClick={() => { setTrackResult(null); setTrackToken(submitResult.tracking_token); go("track"); }}
                style={C.primary(brand)}>
                Track This Request
              </button>
              <button onClick={() => { setDashInput(email); go("dashboard"); }} style={C.secondary}>
                View All My Requests
              </button>
            </div>
          </div>
        )}

        {/* ════ TRACK ═══════════════════════════════════════════════════════ */}
        {view === "track" && (
          <div>
            <div style={C.card}>
              <div style={C.cardTitle}>Track Your Request</div>
              <form onSubmit={handleTrack}>
                <div style={C.label}>Tracking Token</div>
                <div style={{ display: "flex", gap: 10 }}>
                  <input value={trackToken} onChange={e => setTrackToken(e.target.value)}
                    placeholder="Paste your tracking token here"
                    style={{ ...C.input, fontFamily: "monospace", flex: 1, minWidth: 0, marginBottom: 0 }} required />
                  <button type="submit" disabled={tracking}
                    style={{ ...C.primary(brand), flexShrink: 0, opacity: tracking ? 0.7 : 1 }}>
                    {tracking ? "…" : "Check"}
                  </button>
                </div>
                {trackErr && <p style={{ ...C.err, marginTop: 8 }}>{trackErr}</p>}
              </form>
            </div>

            {trackResult && (
              <div ref={trackRef} style={{ marginTop: 20 }}>
                <div style={{ ...C.card, borderTop: `4px solid ${STATUS_CFG[trackResult.status]?.color ?? "#6b7280"}` }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, marginBottom: 20 }}>
                    <div>
                      <div style={{ fontWeight: 800, fontSize: 18, color: "#111827", marginBottom: 4 }}>{trackResult.subject}</div>
                      <div style={{ fontSize: 13, color: "#9ca3af" }}>{trackResult.case_type_name}</div>
                    </div>
                    <StatusBadge status={trackResult.status} />
                  </div>

                  <StatusRail current={trackResult.status} brand={brand} />

                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 20 }}>
                    {([
                      ["Priority",     trackResult.priority],
                      ["Submitted",    fmtDate(trackResult.submitted_at)],
                      ["Last Updated", fmtDate(trackResult.updated_at)],
                      ["Resolved",     fmtDate(trackResult.resolved_at)],
                    ] as [string, string][]).map(([lbl, val]) => (
                      <div key={lbl} style={{ background: "#f8fafc", borderRadius: 8, padding: 12 }}>
                        <div style={C.metaLabel}>{lbl}</div>
                        <div style={{ fontWeight: 600, marginTop: 4, fontSize: 13, textTransform: "capitalize" }}>{val}</div>
                      </div>
                    ))}
                  </div>
                </div>

                {!["resolved", "closed", "cancelled"].includes(trackResult.status) && (
                  <div style={{ ...C.card, marginTop: 14 }}>
                    <div style={{ fontWeight: 700, fontSize: 13, color: "#374151", marginBottom: 12 }}>📎 Attach a Document</div>
                    <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                      <label style={{
                        flex: 1, padding: "10px 16px", border: "2px dashed #e5e7eb", borderRadius: 8,
                        cursor: "pointer", fontSize: 13, color: "#6b7280", textAlign: "center",
                        background: uploadFile ? "#f0fdf4" : "#fafafa",
                      }}>
                        <input type="file" onChange={e => setUploadFile(e.target.files?.[0] ?? null)} style={{ display: "none" }} />
                        {uploadFile ? `✓ ${uploadFile.name}` : "Choose a file or drag and drop"}
                      </label>
                      <button onClick={handleUpload} disabled={uploading || !uploadFile}
                        style={{ ...C.primary(brand), flexShrink: 0, opacity: uploading || !uploadFile ? 0.5 : 1 }}>
                        {uploading ? "Uploading…" : "Upload"}
                      </button>
                    </div>
                    {uploadMsg && (
                      <p style={{ marginTop: 8, fontSize: 13, color: uploadMsg.startsWith("Error") ? "#ef4444" : "#16a34a" }}>
                        {uploadMsg}
                      </p>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* ════ DASHBOARD ═══════════════════════════════════════════════════ */}
        {view === "dashboard" && (
          <div>
            <div style={C.card}>
              <div style={C.cardTitle}>My Requests</div>
              <div style={{ fontSize: 13, color: "#6b7280", marginBottom: 16 }}>
                Enter your email to see all your support cases.
              </div>
              <form onSubmit={handleDashboard} style={{ display: "flex", gap: 10 }}>
                <input type="email" value={dashInput} onChange={e => setDashInput(e.target.value)}
                  placeholder="your@email.com"
                  style={{ ...C.input, flex: 1, minWidth: 0, marginBottom: 0 }} required />
                <button type="submit" disabled={dashLoading}
                  style={{ ...C.primary(brand), flexShrink: 0, opacity: dashLoading ? 0.7 : 1 }}>
                  {dashLoading ? "Loading…" : "View"}
                </button>
              </form>
              {dashErr && <p style={{ ...C.err, marginTop: 10 }}>{dashErr}</p>}
            </div>

            {dashCases !== null && (
              <div style={{ marginTop: 20 }}>
                {dashCases.length === 0 ? (
                  <div style={{ ...C.card, textAlign: "center", padding: 48 }}>
                    <div style={{ fontSize: 40, marginBottom: 12 }}>📭</div>
                    <div style={{ fontWeight: 600, color: "#374151", marginBottom: 4 }}>No requests found</div>
                    <div style={{ fontSize: 13, color: "#9ca3af" }}>No cases found for <strong>{dashEmail}</strong>.</div>
                  </div>
                ) : (
                  <>
                    {/* Stats */}
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10, marginBottom: 16 }}>
                      {[
                        { label: "Total",       val: dashCases.length, color: "#0d9488" },
                        { label: "In Progress", val: dashCases.filter(c => ["new","open","in_progress","pending"].includes(c.status)).length, color: "#f59e0b" },
                        { label: "Resolved",    val: dashCases.filter(c => ["resolved","closed"].includes(c.status)).length, color: "#22c55e" },
                      ].map(s => (
                        <div key={s.label} style={{ ...C.card, textAlign: "center", padding: "14px 10px" }}>
                          <div style={{ fontSize: 28, fontWeight: 800, color: s.color }}>{s.val}</div>
                          <div style={{ fontSize: 10, color: "#9ca3af", fontWeight: 700, marginTop: 2, textTransform: "uppercase", letterSpacing: "0.05em" }}>{s.label}</div>
                        </div>
                      ))}
                    </div>

                    <div style={C.sectionLabel}>{dashCases.length} request{dashCases.length !== 1 ? "s" : ""} for {dashEmail}</div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                      {dashCases.map(c => {
                        const sc = STATUS_CFG[c.status] ?? STATUS_CFG.closed;
                        return (
                          <div key={c.case_id} style={{ ...C.card, borderLeft: `4px solid ${sc.color}`, padding: "16px 18px" }}>
                            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, marginBottom: 10 }}>
                              <div style={{ flex: 1, minWidth: 0 }}>
                                <div style={{ fontWeight: 700, fontSize: 15, color: "#111827", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                  {c.subject}
                                </div>
                                <div style={{ fontSize: 12, color: "#9ca3af", marginTop: 2 }}>
                                  {c.case_type_name}
                                  {c.case_number && <span style={{ marginLeft: 8, fontFamily: "monospace" }}>{c.case_number}</span>}
                                </div>
                              </div>
                              <StatusBadge status={c.status} />
                            </div>
                            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, marginBottom: 12 }}>
                              {([["Priority", c.priority], ["Submitted", fmtDate(c.submitted_at)], ["Updated", fmtDate(c.updated_at)]] as [string, string][]).map(([lbl, val]) => (
                                <div key={lbl}>
                                  <div style={C.metaLabel}>{lbl}</div>
                                  <div style={{ fontWeight: 600, fontSize: 12, marginTop: 2, textTransform: "capitalize" }}>{val}</div>
                                </div>
                              ))}
                            </div>
                            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                              <button onClick={() => openTimeline(c.case_id, dashEmail)}
                                style={{ ...C.primary(brand), padding: "7px 16px", fontSize: 12 }}>
                                View Timeline
                              </button>
                              <button onClick={() => { setTrackToken(c.tracking_token); setTrackResult(null); go("track"); }}
                                style={{ ...C.secondary, padding: "7px 16px", fontSize: 12 }}>
                                Track by Token
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        )}

        {/* ════ TIMELINE ════════════════════════════════════════════════════ */}
        {view === "timeline" && (
          <div>
            <button onClick={() => go("dashboard")}
              style={{ ...C.ghost, color: "#6b7280", fontSize: 13, marginBottom: 16 }}>
              ← Back to My Requests
            </button>

            {tlLoading && (
              <div style={{ ...C.card, textAlign: "center", padding: 56 }}>
                <div style={{ width: 32, height: 32, border: "3px solid #e5e7eb", borderTopColor: brand, borderRadius: "50%", animation: "pspin 0.8s linear infinite", margin: "0 auto 12px" }} />
                <div style={{ color: "#9ca3af", fontSize: 13 }}>Loading timeline…</div>
              </div>
            )}
            {tlErr && <div style={{ ...C.card, color: "#ef4444", textAlign: "center", padding: 32 }}>{tlErr}</div>}

            {tlData && (
              <>
                {/* Case header */}
                <div style={{ ...C.card, borderTop: `4px solid ${brand}`, marginBottom: 16 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, marginBottom: 16 }}>
                    <div>
                      <div style={{ fontWeight: 800, fontSize: 20, color: "#111827", marginBottom: 4 }}>{tlData.subject}</div>
                      <div style={{ fontSize: 13, color: "#9ca3af" }}>
                        {tlData.case_type_name}
                        {tlData.case_number && <span style={{ marginLeft: 8, fontFamily: "monospace" }}>{tlData.case_number}</span>}
                      </div>
                    </div>
                    <StatusBadge status={tlData.status} large />
                  </div>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                    {([["Submitted", fmtDate(tlData.submitted_at)], ["Resolved", fmtDate(tlData.resolved_at)]] as [string, string][]).map(([lbl, val]) => (
                      <div key={lbl} style={{ background: "#f8fafc", borderRadius: 8, padding: 12 }}>
                        <div style={C.metaLabel}>{lbl}</div>
                        <div style={{ fontWeight: 600, marginTop: 4, fontSize: 13 }}>{val}</div>
                      </div>
                    ))}
                  </div>
                </div>

                {/* SLA ring */}
                {sla && <SLARing sla={sla} />}

                {/* SD-2 / SD-5 — Bank details widget */}
                {tlData.pending_payment_step && bdStep !== 2 && (
                  <div style={{ ...C.card, marginBottom: 16, borderLeft: "4px solid #0d9488", background: "#f5f3ff" }}>
                    <div style={{ fontWeight: 700, fontSize: 15, color: "#111827", marginBottom: 4, display: "flex", alignItems: "center", gap: 8 }}>
                      <span>🏦</span> Provide Your Bank Details
                    </div>
                    <div style={{ fontSize: 13, color: "#6b7280", marginBottom: 16, lineHeight: 1.6 }}>
                      Your payment is ready to be processed. Please provide your bank details securely.
                      We will send a one-time verification code to your registered email first.
                    </div>

                    {bdStep === 0 && (
                      <form onSubmit={handleBankOtpRequest}>
                        <div style={C.label}>Your registered email</div>
                        <div style={{ display: "flex", gap: 10 }}>
                          <input
                            type="email" value={bdEmail} onChange={e => setBdEmail(e.target.value)}
                            placeholder="your@email.com"
                            style={{ ...C.input, flex: 1, minWidth: 0, marginBottom: 0 }}
                            required
                          />
                          <button type="submit" disabled={bdBusy || !bdEmail.trim()}
                            style={{ ...C.primary("#0d9488"), flexShrink: 0, opacity: bdBusy || !bdEmail.trim() ? 0.6 : 1 }}>
                            {bdBusy ? "Sending…" : "Send Code"}
                          </button>
                        </div>
                        {bdErr && <p style={{ ...C.err, marginTop: 8 }}>{bdErr}</p>}
                      </form>
                    )}

                    {bdStep === 1 && (
                      <form onSubmit={handleBankDetailsSubmit}>
                        <div style={{ background: "#eef2ff", borderRadius: 8, padding: "10px 12px", marginBottom: 14, fontSize: 13, color: "#4338ca" }}>
                          A 6-digit code has been sent to <strong>{bdEmail}</strong>. Enter it below along with your bank details.
                        </div>
                        <div style={C.label}>Verification code</div>
                        <input
                          value={bdOtp} onChange={e => setBdOtp(e.target.value.replace(/\D/g, "").slice(0, 6))}
                          placeholder="6-digit code"
                          inputMode="numeric" maxLength={6}
                          style={{ ...C.input, fontFamily: "monospace", letterSpacing: "0.3em", fontSize: 18 }}
                          required
                        />
                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                          <div>
                            <div style={C.label}>Account name</div>
                            <input value={bdAccountName} onChange={e => setBdAccountName(e.target.value)}
                              placeholder="Jane Smith"
                              style={{ ...C.input, marginBottom: 0 }} required />
                          </div>
                          <div>
                            <div style={C.label}>Sort code</div>
                            <input value={bdSortCode} onChange={e => setBdSortCode(e.target.value)}
                              placeholder="00-00-00"
                              style={{ ...C.input, marginBottom: 0 }} required />
                          </div>
                        </div>
                        <div style={{ height: 12 }} />
                        <div style={C.label}>Account number</div>
                        <input value={bdAccountNumber} onChange={e => setBdAccountNumber(e.target.value)}
                          placeholder="12345678"
                          style={C.input} required />
                        {bdErr && <p style={C.err}>{bdErr}</p>}
                        <div style={{ display: "flex", gap: 10 }}>
                          <button type="button" onClick={() => { setBdStep(0); setBdErr(null); setBdOtp(""); }}
                            style={{ ...C.secondary, flexShrink: 0 }}>
                            ← Request new code
                          </button>
                          <button type="submit" disabled={bdBusy}
                            style={{ ...C.primary("#0d9488"), flex: 1, justifyContent: "center", opacity: bdBusy ? 0.7 : 1 }}>
                            {bdBusy ? "Saving…" : "Submit Bank Details Securely"}
                          </button>
                        </div>
                      </form>
                    )}
                  </div>
                )}

                {tlData.pending_payment_step && bdStep === 2 && (
                  <div style={{ ...C.card, marginBottom: 16, borderLeft: "4px solid #16a34a", background: "#f0fdf4", display: "flex", alignItems: "center", gap: 16 }}>
                    <span style={{ fontSize: 32, flexShrink: 0 }}>✓</span>
                    <div>
                      <div style={{ fontWeight: 700, fontSize: 15, color: "#15803d" }}>Bank details received securely</div>
                      <div style={{ fontSize: 13, color: "#166534", marginTop: 3 }}>
                        Your bank details have been saved. Your payment will be processed shortly.
                      </div>
                    </div>
                  </div>
                )}

                {/* Documents grid */}
                {docs.length > 0 && (
                  <>
                    <div style={C.sectionLabel}>Shared Documents ({docs.length})</div>
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 10, marginBottom: 20 }}>
                      {docs.map(doc => {
                        const icon = doc.content_type.includes("pdf") ? "📄" : doc.content_type.includes("image") ? "🖼️" : "📎";
                        return (
                          <div key={doc.id} style={{ ...C.card, padding: "12px 14px", display: "flex", alignItems: "center", gap: 10 }}>
                            <span style={{ fontSize: 22, flexShrink: 0 }}>{icon}</span>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ fontWeight: 600, fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{doc.filename}</div>
                              <div style={{ fontSize: 11, color: "#9ca3af", marginTop: 2 }}>
                                {doc.source === "customer" ? "Uploaded by you" : "From support"} · {fmtDate(doc.uploaded_at)}
                                {doc.size_bytes && <span> · {(doc.size_bytes / 1024).toFixed(0)} KB</span>}
                              </div>
                            </div>
                            <a href={doc.download_url} download={doc.filename}
                              style={{ ...C.secondary, fontSize: 11, padding: "5px 10px", textDecoration: "none", flexShrink: 0, color: "#374151" }}>
                              ↓
                            </a>
                          </div>
                        );
                      })}
                    </div>
                  </>
                )}

                {/* Timeline */}
                <div style={C.sectionLabel}>Request Timeline</div>
                {tlData.timeline.length === 0 ? (
                  <div style={{ ...C.card, textAlign: "center", color: "#9ca3af", padding: 36, fontSize: 14 }}>
                    No activity recorded yet.
                  </div>
                ) : (
                  <div style={{ position: "relative", paddingLeft: 16 }}>
                    <div style={{ position: "absolute", left: 19, top: 0, bottom: 0, width: 2, background: "#e5e7eb" }} />
                    {tlData.timeline.map((ev) => (
                      <div key={ev.id} style={{ display: "flex", gap: 16, alignItems: "flex-start", paddingBottom: 22, position: "relative" }}>
                        <div style={{
                          width: 38, height: 38, borderRadius: "50%", flexShrink: 0, zIndex: 1,
                          background: "#fff", border: `2px solid ${brand}`,
                          display: "flex", alignItems: "center", justifyContent: "center",
                          fontSize: 16, boxShadow: "0 0 0 4px #fff",
                        }}>
                          {ACTION_ICON[ev.action] ?? "📌"}
                        </div>
                        <div style={{ flex: 1, paddingTop: 7 }}>
                          <div style={{ fontWeight: 700, fontSize: 14, color: "#111827" }}>{ev.label}</div>
                          {ev.details.stage    && <div style={{ fontSize: 12, color: "#6b7280", marginTop: 2 }}>Stage: {ev.details.stage}</div>}
                          {ev.details.status   && <div style={{ fontSize: 12, color: "#6b7280", marginTop: 2 }}>Status: {sl(ev.details.status)}</div>}
                          {ev.details.filename && <div style={{ fontSize: 12, color: "#6b7280", marginTop: 2 }}>File: {ev.details.filename}</div>}
                          <div style={{ fontSize: 11, color: "#c4c9d4", marginTop: 4 }}>{fmtDateTime(ev.timestamp)}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {/* AI Chat */}
                {chatCaseId && (
                  <div style={{ ...C.card, marginTop: 24 }}>
                    <div style={{ fontWeight: 700, fontSize: 14, color: "#111827", marginBottom: 12, display: "flex", alignItems: "center", gap: 8 }}>
                      <span>💬</span> Ask HxNexus about your request
                    </div>
                    <div style={{ maxHeight: 260, overflowY: "auto", marginBottom: 12, display: "flex", flexDirection: "column", gap: 8 }}>
                      {chatHistory.length === 0 && (
                        <div style={{ fontSize: 13, color: "#9ca3af", textAlign: "center", padding: "20px 0" }}>
                          Ask anything about your case — status, next steps, expected resolution…
                        </div>
                      )}
                      {chatHistory.map((m, i) => (
                        <div key={i} style={{
                          padding: "9px 13px", borderRadius: 10, fontSize: 13, lineHeight: 1.55, maxWidth: "84%",
                          alignSelf: m.role === "user" ? "flex-end" : "flex-start",
                          background: m.role === "user" ? brand : "#f3f4f6",
                          color: m.role === "user" ? "#fff" : "#111827",
                        }}>{m.text}</div>
                      ))}
                      {chatBusy && (
                        <div style={{ alignSelf: "flex-start", padding: "9px 13px", background: "#f3f4f6", borderRadius: 10, fontSize: 13, color: "#9ca3af" }}>
                          HxNexus is thinking…
                        </div>
                      )}
                    </div>
                    <form onSubmit={e => handleChat(e, chatCaseId, dashEmail)} style={{ display: "flex", gap: 8 }}>
                      <input value={chatMsg} onChange={e => setChatMsg(e.target.value)}
                        placeholder="Type your question…"
                        style={{ ...C.input, flex: 1, minWidth: 0, marginBottom: 0 }} />
                      <button type="submit" disabled={chatBusy || !chatMsg.trim()}
                        style={{ ...C.primary(brand), flexShrink: 0, opacity: chatBusy || !chatMsg.trim() ? 0.5 : 1 }}>
                        Send
                      </button>
                    </form>
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {/* ════ LOGIN / REGISTER ═══════════════════════════════════════════ */}
        {view === "login" && (
          <div style={{ maxWidth: 420, margin: "0 auto" }}>
            {/* Mode toggle */}
            <div style={{ display: "flex", background: "#f3f4f6", borderRadius: 10, padding: 4, marginBottom: 24 }}>
              {(["login", "register"] as const).map(m => (
                <button key={m} onClick={() => { setAuthMode(m); setAuthStep("email"); setAuthErr(null); }}
                  style={{ flex: 1, padding: "8px 0", border: "none", borderRadius: 8, fontWeight: 600, fontSize: 13, cursor: "pointer",
                    background: authMode === m ? "#fff" : "transparent",
                    color: authMode === m ? "#111827" : "#6b7280",
                    boxShadow: authMode === m ? "0 1px 3px rgba(0,0,0,0.10)" : "none",
                  }}>
                  {m === "login" ? "Login" : "Register"}
                </button>
              ))}
            </div>

            <div style={C.card}>
              <div style={C.cardTitle}>{authMode === "login" ? "Welcome back" : "Create your account"}</div>

              {authStep === "email" && (
                <form onSubmit={handleAuthSubmitEmail}>
                  <label style={C.label}>Email address</label>
                  <input value={authEmail} onChange={e => setAuthEmail(e.target.value)} type="email" required
                    placeholder="you@example.com" style={C.input} autoFocus />
                  {authMode === "register" && (
                    <>
                      <label style={C.label}>Your name</label>
                      <input value={authName} onChange={e => setAuthName(e.target.value)} required
                        placeholder="Full name" style={C.input} />
                      <label style={C.label}>Phone (optional)</label>
                      <input value={authPhone} onChange={e => setAuthPhone(e.target.value)}
                        placeholder="+44 7700 000000" style={C.input} />
                    </>
                  )}
                  {authErr && <div style={C.err}>{authErr}</div>}
                  <button type="submit" disabled={authBusy || !authEmail.trim() || (authMode === "register" && !authName.trim())}
                    style={{ ...C.primary(brand), width: "100%", justifyContent: "center", padding: "11px 0", opacity: authBusy ? 0.6 : 1 }}>
                    {authBusy ? "Sending…" : "Send verification code"}
                  </button>
                </form>
              )}

              {authStep === "otp" && (
                <form onSubmit={handleAuthVerifyOtp}>
                  <div style={{ fontSize: 13, color: "#6b7280", marginBottom: 20 }}>
                    A 6-digit code was sent to <strong>{authEmail}</strong>. Check your inbox (or alt email if set).
                  </div>
                  <label style={C.label}>Verification code</label>
                  <input value={authOtp} onChange={e => setAuthOtp(e.target.value)} required
                    placeholder="000000" maxLength={6} style={{ ...C.input, fontSize: 24, letterSpacing: "0.3em", textAlign: "center" }} autoFocus />
                  {authErr && <div style={C.err}>{authErr}</div>}
                  <button type="submit" disabled={authBusy || authOtp.trim().length < 6}
                    style={{ ...C.primary(brand), width: "100%", justifyContent: "center", padding: "11px 0", opacity: authBusy ? 0.6 : 1 }}>
                    {authBusy ? "Verifying…" : "Verify & Continue"}
                  </button>
                  <button type="button" onClick={() => { setAuthStep("email"); setAuthErr(null); setAuthOtp(""); }}
                    style={{ ...C.secondary, width: "100%", justifyContent: "center", padding: "10px 0", marginTop: 8 }}>
                    ← Back
                  </button>
                </form>
              )}
            </div>
          </div>
        )}

        {/* ════ ACCOUNT ════════════════════════════════════════════════════════ */}
        {view === "account" && custProfile && (
          <div>
            {/* Profile card */}
            <div style={{ ...C.card, marginBottom: 20 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
                  <div style={{ width: 52, height: 52, borderRadius: 26, background: brand, color: "#fff", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 22, fontWeight: 800 }}>
                    {custProfile.display_name.charAt(0).toUpperCase()}
                  </div>
                  <div>
                    <div style={{ fontWeight: 800, fontSize: 18, color: "#111827" }}>{custProfile.display_name}</div>
                    <div style={{ fontSize: 13, color: "#6b7280" }}>{custProfile.primary_email}</div>
                  </div>
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  {!editMode && (
                    <button onClick={() => { setEditName(custProfile.display_name); setEditPhone(custProfile.phone ?? ""); setEditAltEmail(custProfile.alt_email ?? ""); setEditPref(custProfile.preferred_email); setEditMode(true); setEditErr(null); }}
                      style={C.secondary}>Edit</button>
                  )}
                  <button onClick={custLogout} style={{ ...C.secondary, color: "#ef4444", borderColor: "#fca5a5" }}>Logout</button>
                </div>
              </div>

              {!editMode ? (
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                  <div>
                    <div style={C.metaLabel}>Primary Email</div>
                    <div style={{ fontSize: 14, color: "#111827", marginTop: 4 }}>
                      {custProfile.primary_email}
                      {custProfile.preferred_email === "primary" && <span style={{ marginLeft: 6, fontSize: 10, background: brand + "20", color: brand, padding: "2px 6px", borderRadius: 4, fontWeight: 700 }}>Default</span>}
                    </div>
                  </div>
                  <div>
                    <div style={C.metaLabel}>Alt Email</div>
                    <div style={{ fontSize: 14, color: "#111827", marginTop: 4 }}>
                      {custProfile.alt_email
                        ? <>{custProfile.alt_email}{custProfile.preferred_email === "alt" && <span style={{ marginLeft: 6, fontSize: 10, background: brand + "20", color: brand, padding: "2px 6px", borderRadius: 4, fontWeight: 700 }}>Default</span>}</>
                        : <span style={{ color: "#9ca3af" }}>Not set</span>}
                    </div>
                  </div>
                  <div>
                    <div style={C.metaLabel}>Phone</div>
                    <div style={{ fontSize: 14, color: "#111827", marginTop: 4 }}>{custProfile.phone ?? <span style={{ color: "#9ca3af" }}>Not set</span>}</div>
                  </div>
                  <div>
                    <div style={C.metaLabel}>Total Cases</div>
                    <div style={{ fontSize: 14, fontWeight: 700, color: "#111827", marginTop: 4 }}>{custProfile.case_count}</div>
                  </div>
                </div>
              ) : (
                <div>
                  <label style={C.label}>Name</label>
                  <input value={editName} onChange={e => setEditName(e.target.value)} style={C.input} />
                  <label style={C.label}>Phone</label>
                  <input value={editPhone} onChange={e => setEditPhone(e.target.value)} placeholder="Optional" style={C.input} />
                  <label style={C.label}>Alternative email</label>
                  <input value={editAltEmail} onChange={e => setEditAltEmail(e.target.value)} type="email" placeholder="Optional" style={C.input} />
                  {editAltEmail.trim() && (
                    <>
                      <label style={C.label}>Default email for communications</label>
                      <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
                        {(["primary", "alt"] as const).map(p => (
                          <button key={p} type="button" onClick={() => setEditPref(p)}
                            style={{ flex: 1, padding: "9px 0", border: `2px solid ${editPref === p ? brand : "#e5e7eb"}`, borderRadius: 8, background: editPref === p ? brand + "12" : "#fff", color: editPref === p ? brand : "#374151", fontWeight: editPref === p ? 700 : 500, cursor: "pointer", fontSize: 13 }}>
                            {p === "primary" ? "Primary email" : "Alt email"}
                          </button>
                        ))}
                      </div>
                    </>
                  )}
                  {editErr && <div style={C.err}>{editErr}</div>}
                  <div style={{ display: "flex", gap: 8 }}>
                    <button onClick={handleProfileSave} disabled={editBusy}
                      style={{ ...C.primary(brand), flex: 1, justifyContent: "center", opacity: editBusy ? 0.6 : 1 }}>
                      {editBusy ? "Saving…" : "Save changes"}
                    </button>
                    <button onClick={() => setEditMode(false)} style={C.secondary}>Cancel</button>
                  </div>
                </div>
              )}
            </div>

            {/* Case history */}
            <div style={{ ...C.sectionLabel, marginBottom: 12 }}>My Cases</div>
            {custCases === null && (
              <div style={{ ...C.card, textAlign: "center", padding: 40 }}>
                <div style={{ width: 28, height: 28, border: `3px solid #e5e7eb`, borderTopColor: brand, borderRadius: "50%", animation: "pspin 0.8s linear infinite", margin: "0 auto 10px" }} />
                <div style={{ color: "#9ca3af", fontSize: 13 }}>Loading your cases…</div>
              </div>
            )}
            {custCases !== null && custCases.length === 0 && (
              <div style={{ ...C.card, textAlign: "center", padding: 40 }}>
                <div style={{ fontSize: 32, marginBottom: 12 }}>📭</div>
                <div style={{ fontWeight: 700, color: "#111827", marginBottom: 4 }}>No cases yet</div>
                <div style={{ fontSize: 13, color: "#6b7280", marginBottom: 16 }}>Submit your first request to get started.</div>
                <button onClick={() => go("submit")} style={C.primary(brand)}>Submit a Request</button>
              </div>
            )}
            {custCases !== null && custCases.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                {custCases.map(c => (
                  <div key={c.case_id} style={{ ...C.card, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16 }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontWeight: 700, color: "#111827", fontSize: 14, marginBottom: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.subject}</div>
                      <div style={{ fontSize: 12, color: "#9ca3af" }}>
                        {c.case_number && <span style={{ marginRight: 8, fontFamily: "monospace" }}>{c.case_number}</span>}
                        {new Date(c.submitted_at).toLocaleDateString()}
                      </div>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
                      <StatusBadge status={c.status} />
                      {c.tracking_token && (
                        <button onClick={() => { setTrackToken(c.tracking_token!); setTrackResult(null); go("track"); }}
                          style={{ ...C.secondary, padding: "6px 12px", fontSize: 12 }}>Track</button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

      </main>

      <footer style={{ textAlign: "center", padding: "20px 16px", color: "#c4c9d4", fontSize: 12, borderTop: "1px solid #f1f3f5" }}>
        Powered by <strong style={{ color: "#9ca3af" }}>{BRAND.name}</strong> BPM Platform
      </footer>
    </Shell>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function Shell({ brand: _brand, children }: { brand: string; children: React.ReactNode }) {
  return (
    <div style={{ minHeight: "100vh", background: "#f5f7fa", fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', sans-serif", overflowX: "hidden", boxSizing: "border-box" }}>
      <style>{`
        @keyframes pspin { to { transform: rotate(360deg); } }
        *, *::before, *::after { box-sizing: border-box; }
      `}</style>
      {children}
    </div>
  );
}

function StatusBadge({ status, large }: { status: string; large?: boolean }) {
  const sc = STATUS_CFG[status] ?? { color: "#6b7280", bg: "#f9fafb", icon: "•", label: sl(status) };
  return (
    <div style={{
      display: "inline-flex", alignItems: "center", gap: 5, flexShrink: 0,
      padding: large ? "6px 14px" : "4px 10px",
      borderRadius: 20, background: sc.bg, color: sc.color,
      fontSize: large ? 13 : 11, fontWeight: 700,
      border: `1px solid ${sc.color}28`,
    }}>
      <span style={{ fontSize: large ? 14 : 11 }}>{sc.icon}</span>
      {sc.label}
    </div>
  );
}

function StatusRail({ current, brand }: { current: string; brand: string }) {
  if (!STATUS_RAIL.includes(current)) return null;
  const idx = STATUS_RAIL.indexOf(current);
  return (
    <div style={{ display: "flex", alignItems: "center" }}>
      {STATUS_RAIL.map((s, i) => {
        const sc = STATUS_CFG[s];
        const done   = i <= idx;
        const active = i === idx;
        return (
          <React.Fragment key={s}>
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", minWidth: 50 }}>
              <div style={{
                width: active ? 28 : 20, height: active ? 28 : 20, borderRadius: "50%",
                background: done ? sc.color : "#e5e7eb",
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: active ? 13 : 9, color: done ? "#fff" : "#9ca3af",
                boxShadow: active ? `0 0 0 4px ${sc.color}28` : "none",
                transition: "all 0.2s",
              }}>
                {done ? (active ? sc.icon : "✓") : "·"}
              </div>
              <div style={{ fontSize: 9, marginTop: 4, color: done ? sc.color : "#9ca3af", fontWeight: active ? 700 : 500, textAlign: "center", maxWidth: 44, lineHeight: 1.2 }}>
                {sc.label}
              </div>
            </div>
            {i < STATUS_RAIL.length - 1 && (
              <div style={{ flex: 1, height: 2, background: i < idx ? STATUS_CFG[STATUS_RAIL[i + 1]].color : "#e5e7eb", minWidth: 10 }} />
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}

const SLA_COLOR = { green: "#16a34a", amber: "#d97706", red: "#dc2626" };
const SLA_BG    = { green: "#f0fdf4", amber: "#fffbeb", red: "#fef2f2" };

function SLARing({ sla }: { sla: NonNullable<SLAInfo> }) {
  const color = SLA_COLOR[sla.tier];
  const bg    = SLA_BG[sla.tier];
  const hrs   = Math.floor(sla.remaining_seconds / 3600);
  const mins  = Math.floor((sla.remaining_seconds % 3600) / 60);
  const timeLeft = hrs > 0 ? `${hrs}h ${mins}m` : `${mins}m`;
  const pct = sla.breached ? 0 : Math.max(0, Math.min(1, sla.remaining_seconds / (86400 * 3)));
  const R = 28;
  const circ = 2 * Math.PI * R;

  return (
    <div style={{ ...C.card, marginBottom: 16, background: bg, border: `1px solid ${color}28`, display: "flex", alignItems: "center", gap: 20 }}>
      <svg width={72} height={72} style={{ flexShrink: 0 }}>
        <circle cx={36} cy={36} r={R} fill="none" stroke="#e5e7eb" strokeWidth={5} />
        <circle cx={36} cy={36} r={R} fill="none" stroke={color} strokeWidth={5}
          strokeDasharray={circ} strokeDashoffset={circ * (1 - pct)}
          strokeLinecap="round" transform="rotate(-90 36 36)" />
        <text x={36} y={41} textAnchor="middle" fontSize={10} fontWeight={700} fill={color}>
          {sla.breached ? "SLA!" : `${Math.round(pct * 100)}%`}
        </text>
      </svg>
      <div>
        {sla.breached ? (
          <>
            <div style={{ fontWeight: 700, color, fontSize: 15 }}>SLA Breached</div>
            <div style={{ fontSize: 13, color: "#6b7280", marginTop: 3 }}>
              Expected by {fmtDate(sla.deadline_at)} — our team is working to resolve this.
            </div>
          </>
        ) : (
          <>
            <div style={{ fontWeight: 700, color, fontSize: 15 }}>Expected by {fmtDate(sla.deadline_at)}</div>
            <div style={{ fontSize: 13, color: "#6b7280", marginTop: 3 }}>{timeLeft} remaining</div>
          </>
        )}
      </div>
    </div>
  );
}

// ── PortalLogin — branded standalone login page ───────────────────────────────

export function PortalLogin() {
  const { slug } = useParams<{ slug: string }>();
  const [config, setConfig] = useState<PortalConfig | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [ssoProviders, setSsoProviders] = useState<{ id: string; provider: string; client_id: string }[]>([]);
  const brand = config?.brand_color ?? "#0d9488";

  useEffect(() => {
    if (!slug) return;
    // Load portal config for branding
    fetch(`/api/v1/portal/${slug}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => d && setConfig(d))
      .catch(() => {});
    // Load SSO providers for this portal
    fetch(`/api/v1/auth/real/sso/providers?slug=${slug}`)
      .then(r => r.ok ? r.json() : { providers: [] })
      .then(d => setSsoProviders(d.providers ?? []))
      .catch(() => {});
  }, [slug]);

  const handleLogin = async () => {
    if (!username.trim() || !password.trim()) return;
    setBusy(true); setMsg(null);
    try {
      const r = await fetch("/api/v1/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? "Login failed");
      localStorage.setItem("helix_token", d.access_token);
      window.location.href = `/portal/${slug}`;
    } catch (e: any) { setMsg(e.message || "Login failed"); }
    finally { setBusy(false); }
  };

  const handleSso = async (provider: string) => {
    const redirect = `${window.location.origin}/portal/${slug}`;
    const r = await fetch(`/api/v1/auth/real/sso/${provider}/auth-url?redirect_uri=${encodeURIComponent(redirect)}`);
    const d = await r.json();
    if (d.auth_url) window.location.href = d.auth_url;
  };

  const PROVIDER_LABEL: Record<string, string> = { google: "Google", github: "GitHub", azure: "Microsoft", saml: "SSO" };

  return (
    <div style={{ minHeight: "100vh", background: "#f5f7fa", display: "flex", flexDirection: "column", fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif" }}>
      {/* Branded header */}
      <div style={{ background: `linear-gradient(135deg, ${brand} 0%, ${darken(brand, -24)} 100%)`, padding: "20px 24px", display: "flex", alignItems: "center", gap: 14 }}>
        <div style={{ width: 44, height: 44, borderRadius: 10, background: "rgba(255,255,255,0.2)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 20, fontWeight: 800, color: "#fff" }}>
          {config?.logo_text.charAt(0).toUpperCase() ?? "P"}
        </div>
        <div>
          <div style={{ fontWeight: 800, fontSize: 18, color: "#fff" }}>{config?.logo_text ?? "Portal"}</div>
          <div style={{ fontSize: 12, color: "rgba(255,255,255,0.75)" }}>Sign in to continue</div>
        </div>
      </div>

      {/* Login card */}
      <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
        <div style={{ width: "100%", maxWidth: 400, background: "#fff", borderRadius: 12, border: "1px solid #eaecf0", padding: 28, boxShadow: "0 4px 16px rgba(0,0,0,0.08)" }}>
          <div style={{ fontWeight: 800, fontSize: 18, color: "#111827", marginBottom: 4 }}>Sign in</div>
          <div style={{ fontSize: 13, color: "#6b7280", marginBottom: 24 }}>Access your support portal</div>

          {ssoProviders.length > 0 && (
            <>
              <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 16 }}>
                {ssoProviders.map(p => (
                  <button key={p.id} onClick={() => handleSso(p.provider)}
                    style={{ padding: "10px 16px", border: "1px solid #e5e7eb", borderRadius: 8, background: "#fff", cursor: "pointer", fontSize: 13, display: "flex", alignItems: "center", gap: 10 }}>
                    Continue with {PROVIDER_LABEL[p.provider] ?? p.provider}
                  </button>
                ))}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
                <div style={{ flex: 1, height: 1, background: "#e5e7eb" }} />
                <span style={{ fontSize: 11, color: "#9ca3af" }}>or</span>
                <div style={{ flex: 1, height: 1, background: "#e5e7eb" }} />
              </div>
            </>
          )}

          <div style={{ marginBottom: 12 }}>
            <label style={{ display: "block", fontSize: 11, fontWeight: 700, color: "#374151", marginBottom: 5, textTransform: "uppercase", letterSpacing: "0.05em" }}>Email / Username</label>
            <input value={username} onChange={e => setUsername(e.target.value)}
              onKeyDown={e => e.key === "Enter" && handleLogin()}
              placeholder="your@email.com" autoFocus
              style={{ width: "100%", padding: "10px 12px", border: "1px solid #d1d5db", borderRadius: 8, fontSize: 14, boxSizing: "border-box", outline: "none", fontFamily: "inherit" }} />
          </div>
          <div style={{ marginBottom: 20 }}>
            <label style={{ display: "block", fontSize: 11, fontWeight: 700, color: "#374151", marginBottom: 5, textTransform: "uppercase", letterSpacing: "0.05em" }}>Password</label>
            <input type="password" value={password} onChange={e => setPassword(e.target.value)}
              onKeyDown={e => e.key === "Enter" && handleLogin()}
              placeholder="••••••••"
              style={{ width: "100%", padding: "10px 12px", border: "1px solid #d1d5db", borderRadius: 8, fontSize: 14, boxSizing: "border-box", outline: "none", fontFamily: "inherit" }} />
          </div>

          {msg && <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 14, padding: "8px 12px", background: "#fef2f2", borderRadius: 6 }}>{msg}</div>}

          <button onClick={handleLogin} disabled={busy || !username.trim() || !password.trim()}
            style={{ width: "100%", padding: "11px", border: "none", borderRadius: 8, background: brand, color: "#fff", fontWeight: 700, fontSize: 14, cursor: "pointer", opacity: busy || !username.trim() || !password.trim() ? 0.6 : 1 }}>
            {busy ? "Signing in…" : "Sign In"}
          </button>

          <div style={{ marginTop: 16, textAlign: "center" }}>
            <a href={`/portal/${slug}`} style={{ fontSize: 12, color: "#9ca3af", textDecoration: "none" }}>
              ← Back to portal
            </a>
          </div>
        </div>
      </div>

      <footer style={{ textAlign: "center", padding: "16px", color: "#c4c9d4", fontSize: 12 }}>
        Powered by <strong style={{ color: "#9ca3af" }}>{BRAND.name}</strong>
      </footer>
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const C = {
  card:       { background: "#fff", borderRadius: 12, border: "1px solid #eaecf0", padding: 24, boxShadow: "0 1px 4px rgba(0,0,0,0.05)" } as React.CSSProperties,
  cardTitle:  { fontSize: 18, fontWeight: 800, marginBottom: 20, color: "#111827", letterSpacing: "-0.02em" } as React.CSSProperties,
  label:      { display: "block", fontSize: 11, fontWeight: 700, color: "#374151", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.05em" } as React.CSSProperties,
  input:      { display: "block", width: "100%", padding: "10px 12px", border: "1px solid #d1d5db", borderRadius: 8, fontSize: 14, marginBottom: 16, boxSizing: "border-box", fontFamily: "inherit", outline: "none" } as React.CSSProperties,
  primary:    (color: string) => ({ padding: "10px 20px", border: "none", borderRadius: 8, cursor: "pointer", background: color, color: "#fff", fontWeight: 700, fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6 } as React.CSSProperties),
  secondary:  { padding: "10px 20px", border: "1px solid #d1d5db", borderRadius: 8, cursor: "pointer", background: "#fff", color: "#374151", fontWeight: 600, fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6 } as React.CSSProperties,
  ghost:      { padding: "6px 0", border: "none", background: "none", cursor: "pointer", fontWeight: 500, fontSize: 13 } as React.CSSProperties,
  err:        { color: "#ef4444", fontSize: 13, marginBottom: 12 } as React.CSSProperties,
  metaLabel:  { fontSize: 10, color: "#9ca3af", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 700 } as React.CSSProperties,
  sectionLabel:{ fontSize: 10, fontWeight: 700, color: "#9ca3af", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 12 } as React.CSSProperties,
};
