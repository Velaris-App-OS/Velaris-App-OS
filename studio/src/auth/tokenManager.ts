const ACCESS_KEY  = "helix_token";
const REFRESH_KEY = "helix_refresh_token";
// Group J: id of this browser's auth_devices row. Survives logout on purpose —
// the next login reuses the same device record instead of growing a new row
// per login. The server re-validates ownership + user-agent before reuse.
const DEVICE_KEY  = "helix_device_id";

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_KEY);
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY);
}

export function setTokens(access: string, refresh: string): void {
  localStorage.setItem(ACCESS_KEY, access);
  localStorage.setItem(REFRESH_KEY, refresh);
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

export function getDeviceId(): string | null {
  return localStorage.getItem(DEVICE_KEY);
}

export function setDeviceId(id: string): void {
  if (id) localStorage.setItem(DEVICE_KEY, id);
}

// Deduplicate concurrent refresh attempts — only one in-flight at a time.
let _refreshing: Promise<string | null> | null = null;

export async function attemptRefresh(): Promise<string | null> {
  if (_refreshing) return _refreshing;

  // Capture both tokens before any async gap — logout may call clearTokens()
  // while the fetch is in-flight. We need the current access token for the
  // orphan-revoke Authorization header even after localStorage has been cleared.
  const refreshToken  = getRefreshToken();
  const capturedAccess = getAccessToken();
  if (!refreshToken) return null;

  _refreshing = (async () => {
    try {
      const resp = await fetch("/api/v1/auth/real/refresh", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!resp.ok) {
        clearTokens();
        return null;
      }
      const data = await resp.json();
      // Guard: if the user logged out while this fetch was in-flight, the refresh
      // token has been cleared. Revoke the newly issued pair server-side so it
      // cannot be used — the old access token is used as the auth credential
      // because clearTokens() has already wiped localStorage.
      if (!getRefreshToken()) {
        fetch("/api/v1/auth/real/logout", {
          method:  "POST",
          headers: {
            "Content-Type": "application/json",
            ...(capturedAccess ? { "Authorization": `Bearer ${capturedAccess}` } : {}),
          },
          body: JSON.stringify({ refresh_token: data.refresh_token }),
        }).catch(() => {});
        return null;
      }
      setTokens(data.access_token, data.refresh_token);
      return data.access_token as string;
    } catch {
      return null;
    } finally {
      _refreshing = null;
    }
  })();

  return _refreshing;
}
