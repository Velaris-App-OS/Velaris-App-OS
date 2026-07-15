/**
 * HxMeet P2 — embedded in-tab meeting room (self-hosted LiveKit).
 *
 * Shared by the worker path (Case Manager → Sessions tab) and the public
 * guest join page. Receives an already-minted, room-scoped access token —
 * this component never sees credentials, only the token and the LiveKit URL.
 *
 * Layout: camera tiles render in a grid; when anyone screen-shares, the
 * share takes over a large presentation area and cameras drop to a strip.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  ConnectionState,
  LocalParticipant,
  Participant,
  RemoteParticipant,
  Room,
  RoomEvent,
  Track,
} from "livekit-client";

export interface MeetRoomProps {
  url: string;
  token: string;
  title?: string | null;
  onLeave: () => void;
  /** P3: session was started with recording intent — show the persistent notice. */
  recordIntent?: boolean;
  /** P3: recording is live right now (worker view drives this from session state). */
  recordingActive?: boolean;
  /** P3: worker-only Start/Stop recording control; omit for guests. */
  onToggleRecording?: () => void;
  /** P4a-live: session id enables the CC button (captions WebSocket). */
  sessionId?: string | null;
}

const displayName = (p: Participant) =>
  (p.name || p.identity || "?").replace(/^(user|customer|email):/, "");

/* ── P4a-live captions ────────────────────────────────────────────────
 * The browser streams its OWN mic (int16 mono 16 kHz PCM) to case-service
 * over a WebSocket; Whisper runs server-side and returns partial/final
 * captions, which this client fans out to the room on the LiveKit data
 * channel (topic "captions"). CC is a per-person toggle: it starts your
 * own caption stream AND shows everyone's captions. */

type Caption = { speaker: string; text: string; isFinal: boolean; ts: number };

const WORKLET_SRC = `class VxPcm extends AudioWorkletProcessor {
  process(inputs){ const c = inputs[0] && inputs[0][0]; if (c) this.port.postMessage(c.slice(0)); return true; }
}
registerProcessor("vx-pcm", VxPcm);`;

/** Linear-interpolation downsample to 16 kHz int16 — safe on any context rate. */
function toPcm16k(samples: Float32Array, fromRate: number): Int16Array {
  const ratio = fromRate / 16000;
  const out = new Int16Array(Math.floor(samples.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const pos = i * ratio;
    const i0 = Math.floor(pos);
    const frac = pos - i0;
    const s = samples[i0] * (1 - frac) + (samples[Math.min(i0 + 1, samples.length - 1)] || 0) * frac;
    out[i] = Math.max(-32768, Math.min(32767, Math.round(s * 32767)));
  }
  return out;
}

/** Attach exactly one LiveKit track (video or audio) to the DOM. */
function MediaView({ track, fit }: { track: Track; fit?: "cover" | "contain" }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const media = track.attach();
    if (track.kind === Track.Kind.Video) {
      media.style.width = "100%";
      media.style.height = "100%";
      (media.style as any).objectFit = fit || "cover";
    }
    el.appendChild(media);
    return () => { track.detach(media); media.remove(); };
  }, [track, fit]);
  return <div ref={ref} style={{ position: "absolute", inset: 0 }} />;
}

/** One participant's camera tile (screen-shares render in the spotlight, not here). */
function ParticipantTile({ participant, isLocal, compact }: {
  participant: Participant; isLocal: boolean; compact?: boolean;
}) {
  const camPub = participant.getTrackPublication(Track.Source.Camera);
  const camTrack = camPub && !camPub.isMuted ? camPub.track : undefined;
  return (
    <div style={{
      position: "relative", background: "var(--bg-secondary, #111)", borderRadius: 10,
      overflow: "hidden", minWidth: 0,
      // Compact strip thumbnails keep 16:9; grid tiles fill the row the grid
      // gives them — a forced aspect ratio can outgrow the modal and push the
      // controls out of view.
      aspectRatio: compact ? "16 / 9" : undefined,
      height: compact ? 96 : "100%", flex: compact ? "0 0 auto" : undefined,
      border: "1px solid var(--border-subtle, #333)",
    }}>
      {camTrack && <MediaView track={camTrack} fit="cover" />}
      {!camTrack && (
        <div style={{
          position: "absolute", inset: 0, display: "flex", alignItems: "center",
          justifyContent: "center", fontSize: compact ? 22 : 36, color: "var(--text-muted, #888)",
        }}>
          {displayName(participant).charAt(0).toUpperCase()}
        </div>
      )}
      <div style={{
        position: "absolute", left: 8, bottom: 8, fontSize: 11, padding: "2px 8px",
        borderRadius: 4, background: "rgba(0,0,0,.55)", color: "#fff",
        maxWidth: "80%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
      }}>
        {displayName(participant)}{isLocal ? " (you)" : ""}
      </div>
    </div>
  );
}

export default function MeetRoom({
  url, token, title, onLeave, recordIntent, recordingActive, onToggleRecording, sessionId,
}: MeetRoomProps) {
  const roomRef = useRef<Room | null>(null);
  const everConnectedRef = useRef(false);
  const [participants, setParticipants] = useState<Participant[]>([]);
  const [state, setState] = useState<"connecting" | "connected" | "failed">("connecting");
  const [error, setError] = useState<string | null>(null);
  const [mic, setMic] = useState(true);
  const [cam, setCam] = useState(true);
  const [screen, setScreen] = useState(false);

  // P4a-live captions
  const [cc, setCc] = useState(false);
  const [micLevel, setMicLevel] = useState(0);  // 0..100, what we're actually sending
  const [showTranscript, setShowTranscript] = useState(false);
  const [partials, setPartials] = useState<Record<string, Caption>>({});
  const [transcript, setTranscript] = useState<Caption[]>([]);
  const ccStopRef = useRef<(() => void) | null>(null);

  const handleCaption = useCallback((speaker: string, text: string, isFinal: boolean) => {
    const entry: Caption = { speaker, text, isFinal, ts: Date.now() };
    if (isFinal) {
      setPartials((p) => { const n = { ...p }; delete n[speaker]; return n; });
      setTranscript((t) => [...t.slice(-199), entry]);
    } else {
      setPartials((p) => ({ ...p, [speaker]: entry }));
    }
  }, []);

  const refresh = useCallback((room: Room) => {
    setParticipants([room.localParticipant as LocalParticipant,
                     ...Array.from(room.remoteParticipants.values() as Iterable<RemoteParticipant>)]);
  }, []);

  useEffect(() => {
    // React 18 dev StrictMode mounts effects twice; `cancelled` keeps the
    // aborted first attempt from writing its error over the live room.
    let cancelled = false;
    const room = new Room({ adaptiveStream: true, dynacast: true });
    roomRef.current = room;
    const sync = () => refresh(room);
    room
      .on(RoomEvent.ParticipantConnected, sync)
      .on(RoomEvent.ParticipantDisconnected, sync)
      .on(RoomEvent.TrackSubscribed, sync)
      .on(RoomEvent.TrackUnsubscribed, sync)
      .on(RoomEvent.TrackMuted, sync)
      .on(RoomEvent.TrackUnmuted, sync)
      .on(RoomEvent.LocalTrackPublished, sync)
      .on(RoomEvent.LocalTrackUnpublished, (pub) => {
        // Browser "Stop sharing" bar ends the share outside our button.
        if (pub.source === Track.Source.ScreenShare) setScreen(false);
        sync();
      })
      .on(RoomEvent.DataReceived, (payload, participant, _kind, topic) => {
        if (topic !== "captions") return;
        try {
          const m = JSON.parse(new TextDecoder().decode(payload));
          if (typeof m.text === "string" && m.text) {
            handleCaption(m.speaker || (participant ? displayName(participant) : "?"), m.text, !!m.is_final);
          }
        } catch { /* malformed caption payload — drop */ }
      })
      // Only close the room UI on a disconnect that follows a successful
      // connect — a failed initial connect must stay open to show the error.
      .on(RoomEvent.Disconnected, () => { if (!cancelled && everConnectedRef.current) onLeave(); })
      .on(RoomEvent.ConnectionStateChanged, (s) => {
        if (s === ConnectionState.Connected && !cancelled) {
          everConnectedRef.current = true;
          setState("connected");
          setError(null);
        }
      });
    (async () => {
      try {
        // Server config points at loopback; when the browser is on another
        // device, signalling lives on the same host that served this page.
        const wsUrl = /^wss?:\/\/(127\.0\.0\.1|localhost)([:/]|$)/.test(url) &&
          !["127.0.0.1", "localhost"].includes(window.location.hostname)
          ? url.replace(/127\.0\.0\.1|localhost/, window.location.hostname)
          : url;
        await room.connect(wsUrl, token);
        if (cancelled) return;
        everConnectedRef.current = true;
        setState("connected");
        setError(null);
        sync();
      } catch (e: any) {
        if (cancelled) return;
        setState("failed");
        setError(e?.message || "Could not connect to the session");
        return;
      }
      try {
        await room.localParticipant.enableCameraAndMicrophone();
      } catch {
        // No devices / permission denied — still in the room, can subscribe.
        if (!cancelled) { setMic(false); setCam(false); }
      }
      if (!cancelled) sync();
    })();
    return () => {
      cancelled = true;
      ccStopRef.current?.();
      room.disconnect();
      roomRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, token]);

  const stopCaptions = useCallback(() => {
    ccStopRef.current?.();
    ccStopRef.current = null;
    setCc(false);
    setPartials({});
    setMicLevel(0);
  }, []);

  const startCaptions = useCallback(async () => {
    const room = roomRef.current;
    if (!room || !sessionId || state !== "connected") return;
    // Right after joining, the mic may still be mid-publish — poll briefly
    // before declaring it off.
    let micTrack: MediaStreamTrack | undefined;
    for (let i = 0; i < 10 && !micTrack; i++) {
      micTrack = room.localParticipant
        .getTrackPublication(Track.Source.Microphone)?.track?.mediaStreamTrack;
      if (!micTrack) await new Promise((r) => setTimeout(r, 300));
    }
    if (!micTrack) { setError("Turn the microphone on to use captions"); return; }
    setError(null);

    const speaker = displayName(room.localParticipant);
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${window.location.host}/api/v1/meet/sessions/${sessionId}/captions`);
    ws.binaryType = "arraybuffer";

    let ctx: AudioContext | null = null;
    let node: AudioWorkletNode | null = null;
    let src: MediaStreamAudioSourceNode | null = null;
    let workletUrl: string | null = null;
    let pending: Float32Array[] = [];
    let pendingLen = 0;
    let closedByUs = false;

    const cleanup = () => {
      try { node?.port.close(); node?.disconnect(); } catch { /* already gone */ }
      try { src?.disconnect(); } catch { /* already gone */ }
      try { ctx?.close(); } catch { /* already gone */ }
      if (workletUrl) URL.revokeObjectURL(workletUrl);
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        closedByUs = true;
        ws.close();
      }
    };

    ws.onopen = async () => {
      ws.send(JSON.stringify({ token }));
      try {
        ctx = new AudioContext();
        workletUrl = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" }));
        await ctx.audioWorklet.addModule(workletUrl);
        if (ctx.state === "suspended") await ctx.resume();  // autoplay policy
        src = ctx.createMediaStreamSource(new MediaStream([micTrack]));
        node = new AudioWorkletNode(ctx, "vx-pcm");
        src.connect(node);
        // An unconnected subgraph is never rendered — the worklet only runs
        // when it's reachable from the destination. Its outputs stay silent
        // (process() writes nothing), so this is inaudible.
        node.connect(ctx.destination);
        const sampleRate = ctx.sampleRate;
        node.port.onmessage = (e: MessageEvent<Float32Array>) => {
          pending.push(e.data);
          pendingLen += e.data.length;
          if (pendingLen >= sampleRate / 4) {  // ~250 ms batches
            const all = new Float32Array(pendingLen);
            let off = 0;
            for (const c of pending) { all.set(c, off); off += c.length; }
            pending = []; pendingLen = 0;
            let sum = 0;
            for (let i = 0; i < all.length; i += 4) sum += all[i] * all[i];
            setMicLevel(Math.min(100, Math.round(Math.sqrt(sum / (all.length / 4)) * 300)));
            if (ws.readyState === WebSocket.OPEN) ws.send(toPcm16k(all, sampleRate).buffer);
          }
        };
      } catch (e: any) {
        setError(`Captions audio capture failed: ${e?.message || e}`);
        cleanup();
        setCc(false);
      }
    };
    ws.onmessage = (e) => {
      try {
        const m = JSON.parse(e.data);
        if (m.type === "ready" && m.lag_mode) {
          setError("Captions running WITHOUT a GPU — expect noticeable lag");
        }
        if (m.type === "caption" && m.text) {
          const payload = JSON.stringify({ speaker, text: m.text, is_final: !!m.is_final });
          roomRef.current?.localParticipant
            .publishData(new TextEncoder().encode(payload), { topic: "captions", reliable: !!m.is_final })
            .catch(() => { /* data channel hiccup — next caption retries */ });
          handleCaption(speaker, m.text, !!m.is_final);
        }
      } catch { /* non-JSON frame — drop */ }
    };
    ws.onclose = (e) => {
      if (closedByUs) return;
      if (e.code === 4403) setError("Live captions are not enabled for this tenant");
      else if (e.code === 4501) setError("Captions engine is not installed on the server");
      else if (e.code !== 1000) setError("Caption stream disconnected");
      ccStopRef.current = null;
      setCc(false);
    };

    ccStopRef.current = cleanup;
    setCc(true);
  }, [sessionId, state, token, handleCaption]);

  const toggle = async (kind: "mic" | "cam" | "screen") => {
    const room = roomRef.current;
    // Publishing on an unconnected room hangs until an engine timeout —
    // media controls are only meaningful once signalling is up.
    if (!room || state !== "connected") return;
    try {
      if (kind === "mic") { await room.localParticipant.setMicrophoneEnabled(!mic); setMic(!mic); }
      if (kind === "cam") { await room.localParticipant.setCameraEnabled(!cam); setCam(!cam); }
      if (kind === "screen") { await room.localParticipant.setScreenShareEnabled(!screen); setScreen(!screen); }
      setError(null);
    } catch (e: any) {
      // Cancelling the browser's share/device picker is not an error worth showing.
      const msg = e?.message || "";
      if (kind === "screen" && /permission denied|not allowed|aborterror/i.test(msg)) return;
      const what = kind === "screen" ? "share screen" : `enable ${kind === "cam" ? "camera" : "microphone"}`;
      // Firefox reports a missing device as the cryptic "The object can not be
      // found here"; Chrome as "Requested device not found".
      const friendly = e?.name === "NotFoundError" || /not be found|not found/i.test(msg)
        ? `no ${kind === "cam" ? "camera" : kind === "mic" ? "microphone" : "screen"} was found on this device`
        : msg;
      setError(friendly ? `Could not ${what}: ${friendly}` : null);
    }
  };

  const leave = () => { roomRef.current?.disconnect(); onLeave(); };

  const live = state === "connected";
  const btn = (active: boolean): React.CSSProperties => ({
    padding: "8px 14px", fontSize: 12, borderRadius: 8, fontWeight: 600,
    cursor: live ? "pointer" : "not-allowed", opacity: live ? 1 : 0.45,
    border: `1px solid ${active ? "var(--accent, #6cf)" : "var(--border-subtle, #444)"}`,
    background: "transparent", color: active ? "var(--accent, #6cf)" : "var(--text-muted, #999)",
  });

  const local = roomRef.current?.localParticipant;

  // First live screen-share wins the spotlight (local or remote).
  let presenter: { track: Track; participant: Participant } | null = null;
  for (const p of participants) {
    const pub = p.getTrackPublication(Track.Source.ScreenShare);
    if (pub?.track && !pub.isMuted) { presenter = { track: pub.track, participant: p }; break; }
  }

  // Remote audio (mic + screen-share audio) plays through hidden elements;
  // the local mic is never played back.
  const remoteAudio: { key: string; track: Track }[] = [];
  for (const p of participants) {
    if (p === local) continue;
    for (const src of [Track.Source.Microphone, Track.Source.ScreenShareAudio]) {
      const pub = p.getTrackPublication(src);
      if (pub?.track && !pub.isMuted) remoteAudio.push({ key: `${p.identity}:${src}`, track: pub.track });
    }
  }

  const cols = participants.length <= 1 ? 1 : participants.length <= 4 ? 2 : 3;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, height: "100%", minHeight: 420 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13 }}>
        <span style={{ fontWeight: 700 }}>{title || "Case session"}</span>
        <span style={{
          fontSize: 10, textTransform: "uppercase", fontFamily: "var(--font-mono, monospace)",
          color: state === "connected" ? "var(--accent, #6cf)" : "var(--text-muted, #999)",
        }}>
          {state === "connecting" ? "connecting…" : state === "connected" ? "live" : "failed"}
        </span>
        {recordingActive ? (
          <span style={{
            fontSize: 10, textTransform: "uppercase", fontWeight: 700, padding: "2px 8px",
            borderRadius: 4, color: "#fff", background: "var(--status-failed, #c33)",
          }}>
            ● recording
          </span>
        ) : recordIntent ? (
          <span style={{ fontSize: 11, color: "var(--status-failed, #f66)" }}>
            · this session is recorded — all participants have consented
          </span>
        ) : (
          <span style={{ fontSize: 11, color: "var(--text-muted, #999)" }}>· not recorded</span>
        )}
      </div>

      {error && <div style={{ fontSize: 12, color: "var(--status-failed, #f66)" }}>{error}</div>}

      {remoteAudio.map((a) => <MediaView key={a.key} track={a.track} />)}

      {presenter ? (
        <>
          <div style={{
            flex: 1, position: "relative", minHeight: 0, borderRadius: 10, overflow: "hidden",
            background: "#000", border: "1px solid var(--border-subtle, #333)",
          }}>
            <MediaView track={presenter.track} fit="contain" />
            <div style={{
              position: "absolute", left: 8, bottom: 8, fontSize: 11, padding: "2px 8px",
              borderRadius: 4, background: "rgba(0,0,0,.55)", color: "#fff",
            }}>
              {displayName(presenter.participant)}
              {presenter.participant === local ? " (you)" : ""} is presenting
            </div>
          </div>
          <div style={{ display: "flex", gap: 10, overflowX: "auto", flex: "0 0 auto" }}>
            {participants.map((p) => (
              <ParticipantTile key={p.sid || p.identity} participant={p} compact
                               isLocal={p === local} />
            ))}
          </div>
        </>
      ) : (
        <div style={{
          flex: 1, display: "grid", gap: 10, minHeight: 0, overflow: "auto",
          gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
          gridAutoRows: "minmax(160px, 1fr)",
        }}>
          {participants.map((p) => (
            <ParticipantTile key={p.sid || p.identity} participant={p}
                             isLocal={p === local} />
          ))}
        </div>
      )}

      {/* P4a-live: caption bar — the last finalized line per recent speaker plus
          live partials (dimmed). Assistive, never authoritative. */}
      {cc && (() => {
        const now = Date.now();
        const lines = [
          ...transcript.slice(-2).filter((c) => now - c.ts < 8000),
          ...Object.values(partials),
        ].slice(-3);
        return (
          <div style={{
            flex: "0 0 auto", minHeight: 26, padding: "6px 12px", borderRadius: 8,
            background: "rgba(0,0,0,.65)", color: "#fff", fontSize: 13, lineHeight: 1.5,
          }}>
            {lines.length === 0
              ? <span style={{ opacity: 0.5, fontSize: 11 }}>
                  captions on — listening… mic level {micLevel}
                  {micLevel < 2 ? " — NO AUDIO from your microphone (check input device / OS mute)" : ""}
                </span>
              : lines.map((c, i) => (
                  <div key={i} style={{ opacity: c.isFinal ? 1 : 0.55 }}>
                    <strong>{c.speaker}:</strong> {c.text}
                  </div>
                ))}
          </div>
        );
      })()}

      {showTranscript && (
        <div style={{
          flex: "0 0 auto", maxHeight: 140, overflowY: "auto", padding: "8px 12px",
          borderRadius: 8, border: "1px solid var(--border-subtle, #333)",
          fontSize: 12, lineHeight: 1.6,
        }}>
          <div style={{ fontSize: 10, textTransform: "uppercase", color: "var(--text-muted, #888)", marginBottom: 4 }}>
            Live transcript — assistive, not a verdict
          </div>
          {transcript.length === 0 && <div style={{ color: "var(--text-muted, #888)" }}>Nothing transcribed yet.</div>}
          {transcript.map((c, i) => (
            <div key={i}><strong>{c.speaker}:</strong> {c.text}</div>
          ))}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, justifyContent: "center", paddingBottom: 4 }}>
        <button style={btn(mic)} disabled={!live} onClick={() => toggle("mic")}>{mic ? "Mute" : "Unmute"}</button>
        <button style={btn(cam)} disabled={!live} onClick={() => toggle("cam")}>{cam ? "Camera off" : "Camera on"}</button>
        <button style={btn(screen)} disabled={!live} onClick={() => toggle("screen")}>{screen ? "Stop sharing" : "Share screen"}</button>
        {sessionId && (
          <button style={btn(cc)} disabled={!live}
                  onClick={() => (cc ? stopCaptions() : startCaptions())}>
            {cc ? "CC on" : "CC"}
          </button>
        )}
        {sessionId && (
          <button style={btn(showTranscript)} disabled={!live}
                  onClick={() => setShowTranscript((s) => !s)}>
            Transcript
          </button>
        )}
        {onToggleRecording && recordIntent && (
          <button onClick={onToggleRecording} style={{
            padding: "8px 14px", fontSize: 12, borderRadius: 8, cursor: "pointer", fontWeight: 700,
            border: "1px solid var(--status-failed, #c33)", background: recordingActive ? "var(--status-failed, #c33)" : "transparent",
            color: recordingActive ? "#fff" : "var(--status-failed, #c33)",
          }}>
            {recordingActive ? "■ Stop recording" : "● Start recording"}
          </button>
        )}
        <button onClick={leave} style={{
          padding: "8px 14px", fontSize: 12, borderRadius: 8, cursor: "pointer", fontWeight: 700,
          border: "1px solid var(--status-failed, #f66)", background: "transparent",
          color: "var(--status-failed, #f66)",
        }}>Leave</button>
      </div>
    </div>
  );
}
