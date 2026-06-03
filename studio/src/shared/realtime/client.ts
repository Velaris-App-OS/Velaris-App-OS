/**
 * Real-time WebSocket client for HELIX Studio (Phase 22).
 *
 * Usage:
 *   const client = getRealtimeClient();
 *   const unsubscribe = client.subscribe("cases.abc123", (event) => {
 *     console.log("Got update:", event);
 *   });
 *   unsubscribe();
 */

type EventHandler = (event: any) => void;

class RealtimeClient {
  private ws: WebSocket | null = null;
  private handlers: Map<string, Set<EventHandler>> = new Map();
  private connected = false;
  private reconnectTimer: number | null = null;
  private reconnectDelay = 1000;
  private userId: string;
  private presenceResources: Set<string> = new Set();

  constructor(userId: string = "anonymous") {
    this.userId = userId;
  }

  connect() {
    if (this.ws && this.ws.readyState !== WebSocket.CLOSED) return;

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/api/v1/realtime/ws?user_id=${encodeURIComponent(this.userId)}`;

    try {
      this.ws = new WebSocket(url);
    } catch (e) {
      console.warn("WebSocket connect failed:", e);
      this.scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      this.connected = true;
      this.reconnectDelay = 1000;
      // Re-subscribe to all channels
      for (const channel of this.handlers.keys()) {
        this.sendRaw({ type: "subscribe", channel });
      }
      // Re-register presence
      for (const resource of this.presenceResources) {
        this.sendRaw({ type: "presence", resource });
      }
    };

    this.ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.channel) {
          // Event notification — dispatch to all handlers matching this channel
          const handlers = this.handlers.get(msg.channel);
          if (handlers) {
            handlers.forEach(h => { try { h(msg); } catch (err) { console.error(err); } });
          }
          // Wildcard handlers
          const parts = msg.channel.split(".");
          for (let i = parts.length; i > 0; i--) {
            const wild = parts.slice(0, i - 1).concat(["*"]).join(".");
            const wildHandlers = this.handlers.get(wild);
            if (wildHandlers) {
              wildHandlers.forEach(h => { try { h(msg); } catch (err) { console.error(err); } });
            }
          }
        }
      } catch (e) {
        console.warn("WS message parse error:", e);
      }
    };

    this.ws.onclose = () => {
      this.connected = false;
      this.scheduleReconnect();
    };

    this.ws.onerror = () => {
      // onclose will fire too
    };
  }

  private scheduleReconnect() {
    if (this.reconnectTimer !== null) return;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30000);
      this.connect();
    }, this.reconnectDelay);
  }

  private sendRaw(msg: any) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  subscribe(channel: string, handler: EventHandler): () => void {
    if (!this.handlers.has(channel)) {
      this.handlers.set(channel, new Set());
      this.sendRaw({ type: "subscribe", channel });
    }
    this.handlers.get(channel)!.add(handler);

    return () => {
      const handlers = this.handlers.get(channel);
      if (handlers) {
        handlers.delete(handler);
        if (handlers.size === 0) {
          this.handlers.delete(channel);
          this.sendRaw({ type: "unsubscribe", channel });
        }
      }
    };
  }

  setPresence(resource: string, action: string = "viewing"): () => void {
    this.presenceResources.add(resource);
    this.sendRaw({ type: "presence", resource, action });
    return () => {
      this.presenceResources.delete(resource);
    };
  }

  isConnected(): boolean {
    return this.connected;
  }

  setUserId(userId: string) {
    if (this.userId !== userId) {
      this.userId = userId;
      if (this.ws) {
        this.ws.close();  // onclose will reconnect with new user_id
      }
    }
  }
}

// Global singleton
let _client: RealtimeClient | null = null;

export function getRealtimeClient(userId?: string): RealtimeClient {
  if (!_client) {
    _client = new RealtimeClient(userId);
    _client.connect();
  } else if (userId) {
    _client.setUserId(userId);
  }
  return _client;
}

export function useRealtimeClient(userId?: string): RealtimeClient {
  return getRealtimeClient(userId);
}
