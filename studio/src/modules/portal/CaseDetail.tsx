/**
 * Portal v2 (P1) — logged-in customer case-detail page.
 *
 * One page for everything about a case: friendly stage rail, SLA, live
 * session join (HxMeet), documents, AI chat, and timeline. All data comes
 * from the customer-JWT /account/cases/{id}* endpoints — no email params.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import MeetRoom from "@shared/components/MeetRoom";
import {
  CaseAction, CaseDetailData, CaseDoc, CaseMeetSession, PortalMessage, TimelineEntry,
  completeCaseAction, getCaseActions, getCaseDetail, getCaseDocuments,
  getCaseMessages, getCaseSessions, getCaseTimeline, getCsat, getSessionToken,
  postCaseMessage, postCsat, sendCaseChat, uploadCaseDocument,
} from "./portalApi";
import { C, Skeleton, SkeletonCard, StatusBadge, fmtDate, fmtDateTime } from "./portalUi";

const ACTION_ICON: Record<string, string> = {
  case_created: "🎫", stage_transitioned: "➡️", status_changed: "🔄",
  document_uploaded: "📎", case_resolved: "✅", case_closed: "🔒", case_reopened: "🔓",
};

const SLA_COLOR = { green: "#16a34a", amber: "#d97706", red: "#dc2626" };
const SLA_BG    = { green: "#f0fdf4", amber: "#fffbeb", red: "#fef2f2" };

export default function CaseDetail({ slug, caseId, brand, onBack, onAuthLost }: {
  slug: string; caseId: string; brand: string;
  onBack: () => void;
  onAuthLost: () => void;
}) {
  const [detail, setDetail]     = useState<CaseDetailData | null>(null);
  const [timeline, setTimeline] = useState<TimelineEntry[] | null>(null);
  const [docs, setDocs]         = useState<CaseDoc[] | null>(null);
  const [sessions, setSessions] = useState<CaseMeetSession[]>([]);
  const [err, setErr]           = useState<string | null>(null);

  const [uploading, setUploading] = useState(false);
  const [uploadMsg, setUploadMsg] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const [chatMsg, setChatMsg]         = useState("");
  const [chatBusy, setChatBusy]       = useState(false);
  const [chatHistory, setChatHistory] = useState<{ role: "user" | "ai"; text: string }[]>([]);

  const [room, setRoom] = useState<{ url: string; token: string; title: string | null; record_intent: boolean; session_id?: string } | null>(null);
  const [joinBusy, setJoinBusy] = useState<string | null>(null);

  // P3 — customer actions
  const [actions, setActions] = useState<CaseAction[]>([]);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [formVals, setFormVals] = useState<Record<string, string>>({});
  const [actionDone, setActionDone] = useState<string | null>(null);

  // P4 — human message thread
  const [thread, setThread] = useState<PortalMessage[] | null>(null);
  const [msgDraft, setMsgDraft] = useState("");
  const [msgBusy, setMsgBusy] = useState(false);

  // P5 — CSAT
  const [csat, setCsat] = useState<{ rated: boolean; rating: number | null; can_rate: boolean } | null>(null);
  const [csatRating, setCsatRating] = useState(0);
  const [csatComment, setCsatComment] = useState("");
  const [csatBusy, setCsatBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [d, tl, dc, ss, ac, th, cs] = await Promise.all([
        getCaseDetail(slug, caseId),
        getCaseTimeline(slug, caseId),
        getCaseDocuments(slug, caseId),
        getCaseSessions(slug, caseId).catch(() => ({ sessions: [] })),
        getCaseActions(slug, caseId).catch(() => ({ actions: [] })),
        getCaseMessages(slug, caseId).catch(() => ({ messages: [] })),
        getCsat(slug, caseId).catch(() => null),
      ]);
      setDetail(d); setTimeline(tl.timeline); setDocs(dc.documents); setSessions(ss.sessions);
      setActions(ac.actions); setThread(th.messages); setCsat(cs);
      setErr(null);
    } catch (e: any) {
      if (e?.name === "PortalAuthError" || /session has expired/i.test(e?.message || "")) return onAuthLost();
      setErr(e?.message || "Could not load this case");
    }
  }, [slug, caseId, onAuthLost]);

  useEffect(() => { load(); }, [load]);

  const upload = async (file: File) => {
    setUploading(true); setUploadMsg(null);
    try {
      await uploadCaseDocument(slug, caseId, file);
      setUploadMsg("Uploaded — our team can now see this document.");
      const dc = await getCaseDocuments(slug, caseId);
      setDocs(dc.documents);
    } catch (e: any) { setUploadMsg(e?.message || "Upload failed"); }
    finally { setUploading(false); if (fileRef.current) fileRef.current.value = ""; }
  };

  const chat = async (e: React.FormEvent) => {
    e.preventDefault();
    const msg = chatMsg.trim();
    if (!msg || chatBusy) return;
    setChatMsg(""); setChatBusy(true);
    setChatHistory(h => [...h, { role: "user", text: msg }]);
    try {
      const r = await sendCaseChat(slug, caseId, msg);
      setChatHistory(h => [...h, { role: "ai", text: r.reply }]);
    } catch (e: any) {
      setChatHistory(h => [...h, { role: "ai", text: e?.message || "Something went wrong — please try again." }]);
    } finally { setChatBusy(false); }
  };

  const act = async (a: CaseAction, decision?: string) => {
    setActionBusy(a.step_id); setActionErr(null);
    try {
      const body = a.type === "approval"
        ? { decision, comment: formVals[`${a.step_id}::comment`] || undefined }
        : { data: Object.fromEntries(a.form_fields.map(f => [f.key, formVals[`${a.step_id}::${f.key}`] || ""])) };
      const r = await completeCaseAction(slug, caseId, a.step_id, body);
      setActionDone(r.status === "rejected"
        ? "Your response has been recorded — our team will follow up."
        : "Thank you — your response moved this request forward.");
      setFormVals({});
      await load();
    } catch (e: any) { setActionErr(e?.message || "Could not submit your response"); }
    finally { setActionBusy(null); }
  };

  const sendMessage = async (e: React.FormEvent) => {
    e.preventDefault();
    const text = msgDraft.trim();
    if (!text || msgBusy) return;
    setMsgBusy(true);
    try {
      await postCaseMessage(slug, caseId, text);
      setMsgDraft("");
      const th = await getCaseMessages(slug, caseId);
      setThread(th.messages);
    } catch (e: any) { setErr(e?.message || "Could not send your message"); }
    finally { setMsgBusy(false); }
  };

  const join = async (s: CaseMeetSession) => {
    // Recording notice BEFORE the token request — requesting it is the consent act.
    if (s.record_intent &&
        !window.confirm("This session is recorded. Joining records your audio, video, and screen-share, and counts as your consent. Join?")) {
      return;
    }
    setJoinBusy(s.session_id);
    try { setRoom(await getSessionToken(slug, caseId, s.session_id)); }
    catch (e: any) { setErr(e?.message || "Could not join the session"); }
    finally { setJoinBusy(null); }
  };

  if (err) return (
    <div style={{ ...C.card, textAlign: "center", padding: 40 }}>
      <div style={{ fontSize: 32, marginBottom: 10 }}>⚠️</div>
      <div style={{ fontWeight: 700, color: "#111827", marginBottom: 6 }}>{err}</div>
      <button onClick={onBack} style={C.secondary}>Back to my cases</button>
    </div>
  );

  if (!detail) return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <SkeletonCard lines={4} />
      <SkeletonCard lines={3} />
      <SkeletonCard lines={3} />
    </div>
  );

  const sla = detail.sla;
  const open = !["resolved", "closed", "cancelled"].includes(detail.status);

  return (
    <div>
      <button onClick={onBack} style={{ ...C.ghost, color: brand, marginBottom: 12 }}>← My cases</button>

      {/* ── Header card ─────────────────────────────────────────── */}
      <div style={{ ...C.card, marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 6 }}>
          <div style={{ fontWeight: 800, fontSize: 18, color: "#111827", letterSpacing: "-0.02em", minWidth: 0 }}>
            {detail.subject}
          </div>
          <StatusBadge status={detail.status} large />
        </div>
        <div style={{ fontSize: 12, color: "#9ca3af", display: "flex", gap: 14, flexWrap: "wrap" }}>
          {detail.case_number && <span style={{ fontFamily: "monospace" }}>{detail.case_number}</span>}
          <span>{detail.case_type_name}</span>
          <span>Submitted {fmtDate(detail.submitted_at)}</span>
          {detail.expected_days != null && open && <span>Typically {detail.expected_days} days</span>}
        </div>

        {/* Friendly stage rail */}
        {detail.stage_rail.length > 0 && (
          <div style={{ display: "flex", alignItems: "flex-start", marginTop: 20, overflowX: "auto", paddingBottom: 4 }}>
            {detail.stage_rail.map((st, i) => (
              <React.Fragment key={st.id}>
                <div style={{ display: "flex", flexDirection: "column", alignItems: "center", minWidth: 62 }}>
                  <div style={{
                    width: st.current ? 26 : 18, height: st.current ? 26 : 18, borderRadius: "50%",
                    background: st.reached ? brand : "#e5e7eb",
                    display: "flex", alignItems: "center", justifyContent: "center",
                    fontSize: 11, color: st.reached ? "#fff" : "#9ca3af",
                    boxShadow: st.current ? `0 0 0 4px ${brand}28` : "none",
                    transition: "all 0.2s", flexShrink: 0,
                  }}>
                    {st.reached ? (st.current ? "●" : "✓") : "·"}
                  </div>
                  <div style={{
                    fontSize: 10, marginTop: 5, textAlign: "center", maxWidth: 76, lineHeight: 1.25,
                    color: st.reached ? "#111827" : "#9ca3af", fontWeight: st.current ? 700 : 500,
                  }}>
                    {st.label}
                  </div>
                </div>
                {i < detail.stage_rail.length - 1 && (
                  <div style={{ flex: 1, height: 2, marginTop: st.current ? 12 : 8, minWidth: 14,
                                background: st.reached && detail.stage_rail[i + 1].reached ? brand : "#e5e7eb" }} />
                )}
              </React.Fragment>
            ))}
          </div>
        )}
      </div>

      {/* ── SLA ─────────────────────────────────────────────────── */}
      {sla && open && (
        <div style={{ ...C.card, marginBottom: 16, background: SLA_BG[sla.tier],
                      border: `1px solid ${SLA_COLOR[sla.tier]}28`, padding: "14px 20px",
                      display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 18 }}>{sla.breached ? "⏰" : "🎯"}</span>
          <div style={{ fontSize: 13, color: "#374151" }}>
            {sla.breached
              ? <>We're past our target of <strong>{fmtDate(sla.deadline_at)}</strong> — our team is prioritising this.</>
              : <>Expected by <strong style={{ color: SLA_COLOR[sla.tier] }}>{fmtDate(sla.deadline_at)}</strong></>}
          </div>
        </div>
      )}

      {/* ── Action needed (P3) ──────────────────────────────────── */}
      {actionDone && actions.length === 0 && (
        <div style={{ ...C.card, marginBottom: 16, background: "#f0fdf4", border: "1px solid #86efac",
                      padding: "12px 18px", fontSize: 13, color: "#166534" }}>
          ✓ {actionDone}
        </div>
      )}
      {actions.map(a => (
        <div key={a.step_id} style={{ ...C.card, marginBottom: 16, borderLeft: "4px solid #f59e0b", background: "#fffdf5" }}>
          <div style={{ ...C.sectionLabel, color: "#b45309", marginBottom: 6 }}>⚡ Action needed</div>
          <div style={{ fontWeight: 700, fontSize: 15, color: "#111827", marginBottom: 4 }}>{a.name}</div>
          {a.prompt && <div style={{ fontSize: 13, color: "#6b7280", lineHeight: 1.6, marginBottom: 14 }}>{a.prompt}</div>}

          {a.type === "approval" && (
            <>
              <input value={formVals[`${a.step_id}::comment`] || ""}
                     onChange={e => setFormVals(v => ({ ...v, [`${a.step_id}::comment`]: e.target.value }))}
                     placeholder="Add a comment (optional)" style={{ ...C.input, marginBottom: 12 }} />
              <div style={{ display: "flex", gap: 10 }}>
                <button onClick={() => act(a, "approved")} disabled={actionBusy === a.step_id}
                        style={{ ...C.primary("#16a34a"), opacity: actionBusy === a.step_id ? 0.6 : 1 }}>
                  ✓ Approve
                </button>
                <button onClick={() => act(a, "rejected")} disabled={actionBusy === a.step_id}
                        style={{ ...C.secondary, color: "#dc2626", borderColor: "#fca5a5" }}>
                  ✕ Decline
                </button>
              </div>
            </>
          )}

          {a.type === "form" && (
            <>
              {a.form_fields.map(f => (
                <div key={f.key}>
                  <label style={C.label}>{f.label}{f.required !== false ? " *" : ""}</label>
                  {f.type === "textarea" ? (
                    <textarea rows={3} value={formVals[`${a.step_id}::${f.key}`] || ""}
                              onChange={e => setFormVals(v => ({ ...v, [`${a.step_id}::${f.key}`]: e.target.value }))}
                              style={{ ...C.input, resize: "vertical" }} />
                  ) : (
                    <input type={f.type || "text"} value={formVals[`${a.step_id}::${f.key}`] || ""}
                           onChange={e => setFormVals(v => ({ ...v, [`${a.step_id}::${f.key}`]: e.target.value }))}
                           style={C.input} />
                  )}
                </div>
              ))}
              <button onClick={() => act(a)} disabled={actionBusy === a.step_id}
                      style={{ ...C.primary(brand), opacity: actionBusy === a.step_id ? 0.6 : 1 }}>
                Submit
              </button>
            </>
          )}

          {a.type === "document" && (
            <div style={{ fontSize: 13, color: "#374151" }}>
              Upload the requested document in the <strong>Documents</strong> section below, then confirm:
              <div style={{ marginTop: 10 }}>
                <button onClick={() => act(a)} disabled={actionBusy === a.step_id}
                        style={{ ...C.primary(brand), opacity: actionBusy === a.step_id ? 0.6 : 1 }}>
                  I've uploaded it
                </button>
              </div>
            </div>
          )}

          {actionErr && actionBusy === null && (
            <div style={{ fontSize: 12, color: "#dc2626", marginTop: 10 }}>{actionErr}</div>
          )}
        </div>
      ))}

      {/* ── Live sessions ───────────────────────────────────────── */}
      {sessions.length > 0 && (
        <div style={{ ...C.card, marginBottom: 16, borderLeft: `4px solid ${brand}` }}>
          <div style={{ ...C.sectionLabel, marginBottom: 10 }}>Live session</div>
          {sessions.map(s => (
            <div key={s.session_id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
              <div>
                <div style={{ fontWeight: 700, fontSize: 14, color: "#111827" }}>
                  {s.title || "Case session"}
                  <span style={{ marginLeft: 8, fontSize: 10, color: "#16a34a", fontWeight: 800, textTransform: "uppercase" }}>● live now</span>
                </div>
                <div style={{ fontSize: 12, color: "#9ca3af", marginTop: 2 }}>
                  {s.record_intent ? "This session is recorded" : "Not recorded"}
                  {s.started_at ? ` · started ${fmtDateTime(s.started_at)}` : ""}
                </div>
              </div>
              <button onClick={() => join(s)} disabled={joinBusy === s.session_id}
                style={{ ...C.primary(brand), opacity: joinBusy === s.session_id ? 0.6 : 1 }}>
                {joinBusy === s.session_id ? "Joining…" : "Join session"}
              </button>
            </div>
          ))}
        </div>
      )}

      {/* ── Documents ───────────────────────────────────────────── */}
      <div style={{ ...C.card, marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <div style={C.sectionLabel}>Documents</div>
          {open && (
            <>
              <input ref={fileRef} type="file" style={{ display: "none" }}
                     onChange={e => { const f = e.target.files?.[0]; if (f) upload(f); }} />
              <button onClick={() => fileRef.current?.click()} disabled={uploading}
                      style={{ ...C.secondary, padding: "6px 14px", fontSize: 12, opacity: uploading ? 0.6 : 1 }}>
                {uploading ? "Uploading…" : "📎 Upload"}
              </button>
            </>
          )}
        </div>
        {uploadMsg && <div style={{ fontSize: 12, color: uploadMsg.startsWith("Uploaded") ? "#16a34a" : "#ef4444", marginBottom: 10 }}>{uploadMsg}</div>}
        {docs === null && <Skeleton h={40} />}
        {docs !== null && docs.length === 0 && (
          <div style={{ fontSize: 13, color: "#9ca3af" }}>No documents yet.</div>
        )}
        {docs !== null && docs.map(d => (
          <a key={d.id} href={d.download_url} target="_blank" rel="noopener noreferrer"
             style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 0",
                      borderBottom: "1px solid #f1f3f5", textDecoration: "none" }}>
            <span style={{ fontSize: 16 }}>📄</span>
            <span style={{ flex: 1, fontSize: 13, color: "#111827", fontWeight: 600,
                           overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{d.filename}</span>
            <span style={{ fontSize: 11, color: "#9ca3af" }}>
              {d.source === "customer" ? "You" : "Support team"} · {fmtDate(d.uploaded_at)}
            </span>
          </a>
        ))}
      </div>

      {/* ── CSAT (P5 — after resolution) ────────────────────────── */}
      {csat?.can_rate && (
        <div style={{ ...C.card, marginBottom: 16, borderLeft: `4px solid ${brand}` }}>
          <div style={{ ...C.sectionLabel, marginBottom: 8 }}>How did we do?</div>
          <div style={{ fontSize: 13, color: "#6b7280", marginBottom: 12 }}>
            Your request is {detail.status} — we'd love a quick rating.
          </div>
          <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
            {[1, 2, 3, 4, 5].map(n => (
              <button key={n} onClick={() => setCsatRating(n)} style={{
                border: "none", background: "none", cursor: "pointer",
                fontSize: 26, filter: n <= csatRating ? "none" : "grayscale(1) opacity(.45)",
              }}>⭐</button>
            ))}
          </div>
          {csatRating > 0 && (
            <>
              <input value={csatComment} onChange={e => setCsatComment(e.target.value)}
                     placeholder="Anything to add? (optional)" style={C.input} />
              <button disabled={csatBusy} onClick={async () => {
                setCsatBusy(true);
                try { await postCsat(slug, caseId, csatRating, csatComment.trim() || undefined); await load(); }
                catch (e: any) { setErr(e?.message || "Could not save your rating"); }
                finally { setCsatBusy(false); }
              }} style={{ ...C.primary(brand), opacity: csatBusy ? 0.6 : 1 }}>
                Submit rating
              </button>
            </>
          )}
        </div>
      )}
      {csat?.rated && (
        <div style={{ ...C.card, marginBottom: 16, padding: "12px 18px", fontSize: 13, color: "#6b7280" }}>
          You rated this request {"⭐".repeat(csat.rating || 0)} — thank you!
        </div>
      )}

      {/* ── Messages (P4 — real thread with the support team) ────── */}
      <div style={{ ...C.card, marginBottom: 16 }}>
        <div style={{ ...C.sectionLabel, marginBottom: 12 }}>Messages with our team</div>
        {thread === null && <Skeleton h={40} />}
        {thread !== null && thread.length === 0 && (
          <div style={{ fontSize: 13, color: "#9ca3af", marginBottom: 12 }}>
            No messages yet — write to our team below and we'll reply here (and by email if enabled).
          </div>
        )}
        {thread !== null && thread.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 12, maxHeight: 300, overflowY: "auto" }}>
            {thread.map(m => (
              <div key={m.id} style={{
                alignSelf: m.mine ? "flex-end" : "flex-start",
                maxWidth: "85%", padding: "8px 12px", borderRadius: 12, fontSize: 13, lineHeight: 1.5,
                background: m.mine ? brand : "#f3f4f6",
                color: m.mine ? "#fff" : "#111827",
              }}>
                <div style={{ fontSize: 10, opacity: 0.7, marginBottom: 2 }}>
                  {m.mine ? "You" : (m.author_name || "Support team")}
                  {m.created_at ? ` · ${fmtDateTime(m.created_at)}` : ""}
                </div>
                {m.body}
              </div>
            ))}
          </div>
        )}
        {open && (
          <form onSubmit={sendMessage} style={{ display: "flex", gap: 8 }}>
            <input value={msgDraft} onChange={e => setMsgDraft(e.target.value)}
                   placeholder="Write a message to our team…" style={{ ...C.input, marginBottom: 0, flex: 1 }} />
            <button type="submit" disabled={msgBusy || !msgDraft.trim()}
                    style={{ ...C.primary(brand), opacity: msgBusy || !msgDraft.trim() ? 0.5 : 1 }}>Send</button>
          </form>
        )}
      </div>

      {/* ── AI chat ─────────────────────────────────────────────── */}
      <div style={{ ...C.card, marginBottom: 16 }}>
        <div style={{ ...C.sectionLabel, marginBottom: 12 }}>Ask about this case</div>
        {chatHistory.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 12, maxHeight: 260, overflowY: "auto" }}>
            {chatHistory.map((m, i) => (
              <div key={i} style={{
                alignSelf: m.role === "user" ? "flex-end" : "flex-start",
                maxWidth: "85%", padding: "8px 12px", borderRadius: 12, fontSize: 13, lineHeight: 1.5,
                background: m.role === "user" ? brand : "#f3f4f6",
                color: m.role === "user" ? "#fff" : "#111827",
              }}>{m.text}</div>
            ))}
            {chatBusy && <div style={{ fontSize: 12, color: "#9ca3af" }}>Thinking…</div>}
          </div>
        )}
        <form onSubmit={chat} style={{ display: "flex", gap: 8 }}>
          <input value={chatMsg} onChange={e => setChatMsg(e.target.value)}
                 placeholder="e.g. What happens next?" style={{ ...C.input, marginBottom: 0, flex: 1 }} />
          <button type="submit" disabled={chatBusy || !chatMsg.trim()}
                  style={{ ...C.primary(brand), opacity: chatBusy || !chatMsg.trim() ? 0.5 : 1 }}>Send</button>
        </form>
      </div>

      {/* ── Timeline ────────────────────────────────────────────── */}
      <div style={C.card}>
        <div style={{ ...C.sectionLabel, marginBottom: 14 }}>Activity</div>
        {timeline === null && <Skeleton h={60} />}
        {timeline !== null && timeline.length === 0 && (
          <div style={{ fontSize: 13, color: "#9ca3af" }}>No activity yet.</div>
        )}
        {timeline !== null && [...timeline].reverse().map(ev => (
          <div key={ev.id} style={{ display: "flex", gap: 12, padding: "10px 0", borderBottom: "1px solid #f1f3f5" }}>
            <span style={{ fontSize: 15, flexShrink: 0 }}>{ACTION_ICON[ev.action] ?? "•"}</span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: "#111827" }}>
                {ev.label}
                {ev.details.stage_label && <span style={{ fontWeight: 500, color: "#6b7280" }}> — {ev.details.stage_label}</span>}
                {ev.details.filename && <span style={{ fontWeight: 500, color: "#6b7280" }}> — {ev.details.filename}</span>}
              </div>
              <div style={{ fontSize: 11, color: "#9ca3af", marginTop: 2 }}>{fmtDateTime(ev.timestamp)}</div>
            </div>
          </div>
        ))}
      </div>

      {/* ── Meeting overlay ─────────────────────────────────────── */}
      {room && (
        <div style={{
          position: "fixed", inset: 0, zIndex: 1000, background: "rgba(0,0,0,.8)",
          display: "flex", alignItems: "center", justifyContent: "center", padding: 16,
        }}>
          <div style={{
            width: "min(1100px, 97vw)", height: "min(720px, 94vh)", borderRadius: 14,
            background: "#16181d", border: "1px solid #333", padding: 14,
          }}>
            <MeetRoom url={room.url} token={room.token} title={room.title}
                      recordIntent={room.record_intent} sessionId={room.session_id}
                      onLeave={() => { setRoom(null); load(); }} />
          </div>
        </div>
      )}
    </div>
  );
}
