/**
 * HxMeet P2 — public guest join page (/meet/join?token=…).
 *
 * The invite token is single-use: it is only exchanged when the guest clicks
 * Join (which also satisfies the browser's user-gesture requirement for
 * camera/microphone). A refresh after joining therefore shows the honest
 * "invalid or already used" message — the worker can mint a fresh invite.
 */
import React, { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { exchangeMeetGuestToken, previewMeetGuestInvite } from "@shared/api/client";
import type { MeetRoomToken } from "@shared/api/client";
import MeetRoom from "@shared/components/MeetRoom";

export default function MeetJoin() {
  const [params] = useSearchParams();
  const token = params.get("token") || "";
  const [room, setRoom] = useState<MeetRoomToken | null>(null);
  const [recordIntent, setRecordIntent] = useState(false);
  const [biometricNotice, setBiometricNotice] = useState(false);
  const [sessionTitle, setSessionTitle] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [left, setLeft] = useState(false);

  // Non-consuming preview: the recording notice must be visible BEFORE the
  // consent act (the exchange). A dead link surfaces its error here too.
  useEffect(() => {
    if (!token) return;
    previewMeetGuestInvite(token)
      .then((p) => {
        setRecordIntent(p.record_intent);
        setBiometricNotice(!!p.biometric_notice);
        setSessionTitle(p.title);
      })
      .catch((e: any) => setError(e?.message || "Could not load the invite."));
  }, [token]);

  const join = async () => {
    setBusy(true);
    setError(null);
    try {
      setRoom(await exchangeMeetGuestToken(token));
    } catch (e: any) {
      setError(e?.message || "Could not join the session.");
    } finally {
      setBusy(false);
    }
  };

  const shell: React.CSSProperties = {
    minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center",
    background: "var(--bg-primary, #0b0d12)", color: "var(--text-primary, #eee)",
    padding: 24, fontFamily: "var(--font-body, system-ui, sans-serif)",
  };

  if (room) {
    return (
      <div style={{ ...shell, alignItems: "stretch" }}>
        <div style={{ width: "min(1100px, 100%)", padding: "24px 0" }}>
          {left
            ? <div style={{ textAlign: "center", marginTop: "30vh", fontSize: 15 }}>
                You have left the session. You can close this tab.
              </div>
            : <MeetRoom url={room.url} token={room.token} title={room.title}
                        recordIntent={room.record_intent} sessionId={room.session_id}
                        onLeave={() => setLeft(true)} />}
        </div>
      </div>
    );
  }

  return (
    <div style={shell}>
      <div style={{
        width: 380, padding: 32, borderRadius: 14, textAlign: "center",
        border: "1px solid var(--border-subtle, #333)", background: "var(--bg-secondary, #12151c)",
      }}>
        <div style={{ fontSize: 18, fontWeight: 700, marginBottom: 8 }}>
          {sessionTitle ? `Join: ${sessionTitle}` : "Join case session"}
        </div>
        <div style={{ fontSize: 13, color: "var(--text-muted, #999)", marginBottom: recordIntent ? 12 : 20 }}>
          You've been invited to a live session. Your browser will ask for camera
          and microphone access.{!recordIntent && " This session is not recorded."}
        </div>
        {recordIntent && (
          <div style={{
            fontSize: 13, color: "var(--status-failed, #f66)", marginBottom: biometricNotice ? 12 : 20,
            padding: "10px 12px", borderRadius: 8, textAlign: "left",
            border: "1px solid var(--status-failed, #f66)",
          }}>
            <strong>This session is recorded.</strong> Your audio, video, and anything
            you share on screen will be recorded and attached to the case.
            Joining counts as your consent.
          </div>
        )}
        {biometricNotice && (
          <div style={{
            fontSize: 13, color: "var(--status-failed, #f66)", marginBottom: 20,
            padding: "10px 12px", borderRadius: 8, textAlign: "left",
            border: "1px solid var(--status-failed, #f66)",
          }}>
            <strong>Biometric identity check.</strong> Your face in this session may be
            compared against the ID document on file to verify your identity.
            The comparison runs on the case system, the biometric data is not
            retained — only the match score is kept. Joining counts as your
            explicit consent to this check.
          </div>
        )}
        {!token && <div style={{ fontSize: 13, color: "var(--status-failed, #f66)" }}>This link is missing its invite token.</div>}
        {error && <div style={{ fontSize: 13, color: "var(--status-failed, #f66)", marginBottom: 12 }}>{error}</div>}
        {token && !error && (
          <button onClick={join} disabled={busy} style={{
            padding: "10px 28px", fontSize: 14, fontWeight: 700, borderRadius: 8,
            cursor: "pointer", border: "1px solid var(--accent, #6cf)",
            background: "transparent", color: "var(--accent, #6cf)",
          }}>
            {busy ? "Joining…" : recordIntent ? "Consent & join" : "Join session"}
          </button>
        )}
      </div>
    </div>
  );
}
