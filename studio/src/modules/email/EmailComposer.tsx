// HELIX P25 — Reusable email composer for case-context send.
// Usage in CaseDetail.tsx:
//   <EmailComposer caseId={caseId} onSent={() => refreshTimeline()} />
import React, { useEffect, useState } from "react";

type Tpl = { id: string; name: string; subject: string; body_text: string; body_html: string | null; engine: string };
type Account = { id: string; name: string; address: string; is_default_outbound: boolean };

async function apiJSON<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = `${url} → ${r.status}`;
    try { const j = await r.json(); if (j?.detail) detail = j.detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

export default function EmailComposer({ caseId, caseTypeId, onSent }: {
  caseId?: string; caseTypeId?: string; onSent?: () => void;
}) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountId] = useState<string>("");
  const [templates, setTemplates] = useState<Tpl[]>([]);
  const [templateId, setTemplateId] = useState<string>("");
  const [to, setTo] = useState("");
  const [cc, setCc] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [accs, tpls] = await Promise.all([
          apiJSON<Account[]>("/api/v1/email/accounts?active_only=true"),
          apiJSON<Tpl[]>(`/api/v1/email/templates${caseTypeId ? `?case_type_id=${caseTypeId}` : ""}`),
        ]);
        setAccounts(accs);
        const def = accs.find(a => a.is_default_outbound) || accs[0];
        if (def) setAccountId(def.id);
        setTemplates(tpls);
      } catch (e: any) { setErr(e.message); }
    })();
  }, [caseTypeId]);

  async function send() {
    setBusy(true); setErr(null); setOk(null);
    try {
      const payload: any = {
        case_id: caseId || null,
        account_id: accountId || null,
        to_addresses: to.split(",").map(s => s.trim()).filter(Boolean),
        cc_addresses: cc ? cc.split(",").map(s => s.trim()).filter(Boolean) : [],
      };
      if (templateId) payload.template_id = templateId;
      else { payload.subject = subject; payload.body_text = body; }
      const res = await apiJSON<any>("/api/v1/email/send", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      setOk(`Sent (status: ${res.status})`);
      setSubject(""); setBody(""); setTemplateId("");
      onSent?.();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div style={{ background: "#fff", border: "1px solid #e3e3e8", borderRadius: 8, padding: 14 }}>
      <h3 style={{ margin: "0 0 10px", fontSize: 14 }}>Send email{caseId ? " on this case" : ""}</h3>
      {err && <div style={{ color: "#c33", fontSize: 12, marginBottom: 8 }}>⚠ {err}</div>}
      {ok && <div style={{ color: "#2a7", fontSize: 12, marginBottom: 8 }}>✓ {ok}</div>}

      <label style={lbl}>From account</label>
      <select value={accountId} onChange={e => setAccountId(e.target.value)} style={inp}>
        {accounts.map(a => <option key={a.id} value={a.id}>{a.name} ({a.address})</option>)}
      </select>

      <label style={lbl}>Template (optional)</label>
      <select value={templateId} onChange={e => setTemplateId(e.target.value)} style={inp}>
        <option value="">— Free-form —</option>
        {templates.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
      </select>

      <label style={lbl}>To (comma-separated)</label>
      <input value={to} onChange={e => setTo(e.target.value)} style={inp} />

      <label style={lbl}>Cc</label>
      <input value={cc} onChange={e => setCc(e.target.value)} style={inp} />

      {!templateId && (
        <>
          <label style={lbl}>Subject</label>
          <input value={subject} onChange={e => setSubject(e.target.value)} style={inp} />
          <label style={lbl}>Body</label>
          <textarea value={body} onChange={e => setBody(e.target.value)}
            style={{ ...inp, minHeight: 120, fontFamily: "ui-monospace, monospace" }} />
        </>
      )}

      <div style={{ marginTop: 10, textAlign: "right" }}>
        <button onClick={send}
          disabled={busy || !to.trim() || (!templateId && (!subject.trim() || !body.trim()))}
          style={{ padding: "6px 14px", background: "#4a6cf7", color: "white", border: 0, borderRadius: 3, fontSize: 13, cursor: "pointer" }}>
          {busy ? "Sending…" : "Send"}
        </button>
      </div>
    </div>
  );
}

const lbl: React.CSSProperties = { fontSize: 11, color: "#666", display: "block", marginTop: 8 };
const inp: React.CSSProperties = { width: "100%", padding: "5px 8px", fontSize: 13, border: "1px solid #ccc", borderRadius: 3, marginTop: 2 };
