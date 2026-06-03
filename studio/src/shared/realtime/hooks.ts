/**
 * React hooks for real-time collaboration.
 */
import { useEffect, useState, useRef } from "react";
import { getRealtimeClient } from "./client";

export function useSubscribe(
  channel: string | null,
  handler: (event: any) => void,
  deps: any[] = [],
) {
  const handlerRef = useRef(handler);
  useEffect(() => { handlerRef.current = handler; }, [handler]);

  useEffect(() => {
    if (!channel) return;
    const client = getRealtimeClient();
    const unsub = client.subscribe(channel, (e) => handlerRef.current(e));
    return unsub;
  }, [channel, ...deps]);
}

export function usePresence(resource: string | null, action = "viewing") {
  useEffect(() => {
    if (!resource) return;
    const client = getRealtimeClient();
    client.setPresence(resource, action);
    // We don't need to do anything on cleanup — the server times out presence after 5 min
  }, [resource, action]);
}

export function useActiveUsers(resource: string | null, pollMs = 5000) {
  const [users, setUsers] = useState<string[]>([]);

  useEffect(() => {
    if (!resource) return;
    let cancelled = false;

    const fetchUsers = async () => {
      try {
        const resp = await fetch(`/api/v1/realtime/presence/${encodeURIComponent(resource)}`);
        if (!cancelled && resp.ok) {
          const data = await resp.json();
          setUsers(data.users || []);
        }
      } catch {}
    };

    fetchUsers();
    const timer = setInterval(fetchUsers, pollMs);
    return () => { cancelled = true; clearInterval(timer); };
  }, [resource, pollMs]);

  return users;
}

export function useConnectionStatus(): boolean {
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const client = getRealtimeClient();
    const check = () => setConnected(client.isConnected());
    check();
    const timer = setInterval(check, 1000);
    return () => clearInterval(timer);
  }, []);

  return connected;
}
