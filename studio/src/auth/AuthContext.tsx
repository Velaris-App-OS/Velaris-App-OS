import React, { createContext, useContext, useState, useEffect, useCallback } from "react";
import { hxStream } from "@shared/realtime/hxstream-singleton";
import { getAccessToken, getRefreshToken, setTokens, clearTokens, attemptRefresh, getDeviceId, setDeviceId } from "./tokenManager";

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

  // Restore session from localStorage on reload.
  // If the access token is expired, attempt a silent refresh before giving up.
  useEffect(() => {
    const saved = getAccessToken();
    if (!saved) { setLoading(false); return; }

    (async () => {
      try {
        const u = await fetchMe(saved);
        setToken(saved); setUser(u); hxStream.init(u.user_id);
      } catch {
        const newToken = await attemptRefresh();
        if (newToken) {
          try {
            const u = await fetchMe(newToken);
            setToken(newToken); setUser(u); hxStream.init(u.user_id);
            return;
          } catch { /* fall through to clearTokens */ }
        }
        clearTokens();
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // Listen for the global 401 event dispatched by the fetch interceptor.
  // Only acts when there is a live session — the interceptor already guards
  // this, but we double-check here so a race condition can't fire the message
  // while the user is mid-login on the login page.
  useEffect(() => {
    const handle = () => {
      if (!getAccessToken()) return; // no session → ignore
      hxStream.destroy();
      setUser(null);
      setToken(null);
      clearTokens();
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
    setError(null);
    setLoading(true);
    try {
      const resp = await fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password: password || "", device_id: getDeviceId() }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "Login failed" }));
        throw new Error(err.detail || "Login failed");
      }
      const data = await resp.json();
      setToken(data.access_token);
      setUser(data.user);
      // Only persist tokens when login is complete; MFA challenge returns access_token=""
      if (!data.mfa_required && data.access_token) {
        setTokens(data.access_token, data.refresh_token ?? "");
        if (data.device_id) setDeviceId(data.device_id);
      }
      if (!data.mfa_required) {
        hxStream.init(data.user.user_id);
      }
    } catch (e: any) {
      setError(e.message);
      throw e;
    } finally {
      setLoading(false);
    }
  }, []);

  const logout = useCallback(() => {
    hxStream.destroy();
    const currentToken  = getAccessToken();
    const refreshToken  = getRefreshToken();
    // Fire-and-forget server-side revocation — never block the UI
    if (refreshToken || currentToken) {
      fetch("/api/v1/auth/real/logout", {
        method:  "POST",
        headers: {
          "Content-Type":  "application/json",
          ...(currentToken ? { Authorization: `Bearer ${currentToken}` } : {}),
        },
        body: JSON.stringify({ refresh_token: refreshToken ?? "" }),
      }).catch(() => {});
    }
    setUser(null);
    setToken(null);
    clearTokens();
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
