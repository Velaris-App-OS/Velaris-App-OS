import React, { createContext, useContext, useState, useEffect, useCallback } from "react";
import { hxStream } from "@shared/realtime/hxstream-singleton";

/* ═══════════════════════════════════════════════════════════════════
   Auth Context — manages authentication state across the app
   ═══════════════════════════════════════════════════════════════════ */

export interface AuthUser {
  user_id: string;
  username: string;
  email: string;
  roles: string[];
  groups: string[];
  is_admin: boolean;
  is_designer: boolean;
  is_case_worker: boolean;
  tenant_id?: string | null;
}

interface AuthState {
  user: AuthUser | null;
  token: string | null;
  loading: boolean;
  error: string | null;
  login: (username: string, password?: string) => Promise<void>;
  logout: () => void;
  hasRole: (role: string) => boolean;
}

const AuthCtx = createContext<AuthState>({
  user: null, token: null, loading: true, error: null,
  login: async () => {}, logout: () => {}, hasRole: () => false,
});

export function useAuth() { return useContext(AuthCtx); }

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Restore session from localStorage on reload
  useEffect(() => {
    const saved = localStorage.getItem("helix_token");
    if (saved) {
      fetchMe(saved)
        .then(u => { setToken(saved); setUser(u); hxStream.init(u.user_id); })
        .catch(() => { localStorage.removeItem("helix_token"); })
        .finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
  }, []);

  // Listen for the global 401 event dispatched by the fetch interceptor.
  // Only acts when there is a live session — the interceptor already guards
  // this, but we double-check here so a race condition can't fire the message
  // while the user is mid-login on the login page.
  useEffect(() => {
    const handle = () => {
      if (!localStorage.getItem("helix_token")) return; // no session → ignore
      hxStream.destroy();
      setUser(null);
      setToken(null);
      localStorage.removeItem("helix_token");
      setError("Your session has expired. Please log in again.");
    };
    window.addEventListener("velaris:unauthorized", handle);
    return () => window.removeEventListener("velaris:unauthorized", handle);
  }, []);

  const fetchMe = async (t: string): Promise<AuthUser> => {
    const resp = await fetch("/api/v1/auth/me", {
      headers: { Authorization: `Bearer ${t}` },
    });
    if (!resp.ok) throw new Error("Unauthorized");
    return resp.json();
  };

  const login = useCallback(async (username: string, password?: string) => {
    setError(null);   // always wipe any stale "session expired" message
    setLoading(true);
    try {
      const resp = await fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password: password || "" }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "Login failed" }));
        throw new Error(err.detail || "Login failed");
      }
      const data = await resp.json();
      setToken(data.access_token);
      setUser(data.user);
      localStorage.setItem("helix_token", data.access_token);
      hxStream.init(data.user.user_id);
    } catch (e: any) {
      setError(e.message);
      throw e;
    } finally {
      setLoading(false);
    }
  }, []);

  const logout = useCallback(() => {
    hxStream.destroy();
    setUser(null);
    setToken(null);
    localStorage.removeItem("helix_token");
  }, []);

  const hasRole = useCallback((role: string) => {
    if (!user) return false;
    if (user.is_admin) return true;
    return user.roles.includes(role);
  }, [user]);

  return (
    <AuthCtx.Provider value={{ user, token, loading, error, login, logout, hasRole }}>
      {children}
    </AuthCtx.Provider>
  );
}
