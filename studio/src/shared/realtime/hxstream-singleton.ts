/**
 * HxStream persistent singleton — survives page navigation.
 *
 * Opens the WebSocket once when the user logs in and keeps it alive
 * for the entire browser session. The HxStream page is a pure viewer
 * that subscribes to this singleton instead of managing its own socket.
 *
 * Events are buffered in module memory (up to MAX_BUFFER).  When the
 * page mounts after being away, it calls getBufferedEvents() to get
 * everything that arrived during navigation.
 */

export interface TraceEvent {
  id: string;
  event_type: string;
  case_id: string | null;
  tenant_id: string;
  actor_user_id: string | null;
  actor_ip: string | null;
  payload: Record<string, unknown>;
  occurred_at: string;
  session_id: string | null;
  latency_ms: number | null;
}

export interface StreamGap {
  kind: "gap";
  id: string;
  from: string;   // ISO — when connection dropped
  to: string;     // ISO — when it recovered
  durationMs: number;
}

export type StreamEntry = TraceEvent | StreamGap;

type StatusListener = (connected: boolean) => void;
type EventListener  = (event: TraceEvent) => void;

const MAX_BUFFER   = 500;
const PING_EVERY   = 20_000;
const BASE_BACKOFF = 2_000;
const MAX_BACKOFF  = 30_000;

class HxStreamSingleton {
  private ws: WebSocket | null = null;
  private buffer: StreamEntry[] = [];   // newest first
  private statusListeners  = new Set<StatusListener>();
  private eventListeners   = new Set<EventListener>();
  private connected        = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer:      ReturnType<typeof setInterval> | null = null;
  private backoff          = BASE_BACKOFF;
  private userId           = "anon";
  private disconnectedAt: string | null = null;

  // ── Public API ───────────────────────────────────────────────────

  /** Called once after the user authenticates. */
  init(userId: string) {
    this.userId = userId;
    if (!this.ws || this.ws.readyState === WebSocket.CLOSED) {
      this.connect();
    }
  }

  /** Cleanly shut down (logout). */
  destroy() {
    this.clearTimers();
    this.ws?.close();
    this.ws = null;
    this.connected = false;
    this.buffer = [];
  }

  isConnected() { return this.connected; }

  /** Returns all buffered entries (events + gap markers), newest first. */
  getBuffer(): StreamEntry[] { return [...this.buffer]; }

  /** Subscribe to connection status changes. Returns unsubscribe fn. */
  onStatus(fn: StatusListener): () => void {
    this.statusListeners.add(fn);
    fn(this.connected);          // immediate snapshot
    return () => this.statusListeners.delete(fn);
  }

  /** Subscribe to each new incoming event. Returns unsubscribe fn. */
  onEvent(fn: EventListener): () => void {
    this.eventListeners.add(fn);
    return () => this.eventListeners.delete(fn);
  }

  // ── Private ─────────────────────────────────────────────────────

  private connect() {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const token = localStorage.getItem("helix_token") ?? "";
    const url   = `${proto}://${window.location.host}/api/v1/hxstream/ws?token=${encodeURIComponent(token)}`;

    try {
      this.ws = new WebSocket(url);
    } catch {
      this.scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      this.connected = true;
      this.backoff = BASE_BACKOFF;

      // Insert a gap marker if we were disconnected for a meaningful period
      if (this.disconnectedAt) {
        const gapMs = Date.now() - new Date(this.disconnectedAt).getTime();
        if (gapMs > 3_000) {
          const gap: StreamGap = {
            kind: "gap",
            id: `gap-${Date.now()}`,
            from: this.disconnectedAt,
            to: new Date().toISOString(),
            durationMs: gapMs,
          };
          this.buffer = [gap, ...this.buffer].slice(0, MAX_BUFFER);
        }
        this.disconnectedAt = null;
      }

      this.notifyStatus(true);
      this.startPing();
    };

    this.ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data.type === "connected" || data.type === "pong" || data.type === "heartbeat") return;
        const event = data as TraceEvent;
        if (!event.event_type) return;

        this.buffer = [event, ...this.buffer].slice(0, MAX_BUFFER);
        this.eventListeners.forEach(fn => { try { fn(event); } catch { /* ignore */ } });
      } catch { /* ignore parse errors */ }
    };

    this.ws.onclose = () => {
      if (this.connected) {
        this.disconnectedAt = new Date().toISOString();
      }
      this.connected = false;
      this.clearTimers();
      this.notifyStatus(false);
      this.scheduleReconnect();
    };

    this.ws.onerror = () => this.ws?.close();
  }

  private scheduleReconnect() {
    if (this.reconnectTimer !== null) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF);
      this.connect();
    }, this.backoff);
  }

  private startPing() {
    this.pingTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: "ping" }));
      }
    }, PING_EVERY);
  }

  private clearTimers() {
    if (this.pingTimer !== null) { clearInterval(this.pingTimer); this.pingTimer = null; }
    if (this.reconnectTimer !== null) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null; }
  }

  private notifyStatus(c: boolean) {
    this.statusListeners.forEach(fn => { try { fn(c); } catch { /* ignore */ } });
  }
}

// Module-level singleton — one instance for the entire browser session
const _singleton = new HxStreamSingleton();

export const hxStream = {
  init:        (userId: string) => _singleton.init(userId),
  destroy:     ()               => _singleton.destroy(),
  isConnected: ()               => _singleton.isConnected(),
  getBuffer:   ()               => _singleton.getBuffer(),
  onStatus:    (fn: StatusListener) => _singleton.onStatus(fn),
  onEvent:     (fn: EventListener)  => _singleton.onEvent(fn),
};
