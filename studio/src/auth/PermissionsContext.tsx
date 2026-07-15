import React, { createContext, useContext, useState, useEffect, useCallback } from "react";
import { useAuth } from "./AuthContext";
import { NAV_DATA } from "@/app/nav-data";

/* ═══════════════════════════════════════════════════════════════════
   PermissionsContext — dynamic route-level access control

   Fetches the admin-managed permission map from the backend (GET
   /admin/permissions, accessible to all authenticated users).

   While loading or on fetch failure, falls back to the static NAV_DATA
   role definitions so the sidebar and PermRoute always reflect the
   correct access — no open window where all routes are unrestricted.
   ═══════════════════════════════════════════════════════════════════ */

type PermMap = Record<string, string[]>;

// Static defaults derived from nav-data.ts — used before backend responds
const STATIC_DEFAULTS: PermMap = Object.fromEntries(
  NAV_DATA.filter(e => e.roles.length > 0).map(e => [e.path, e.roles])
);

interface PermissionsState {
  permissions: PermMap;
  loading: boolean;
  getRouteRoles: (path: string) => string[];
  isRouteAllowed: (path: string, userRoles: string[], isAdmin: boolean) => boolean;
  refresh: () => void;
}

const PermissionsCtx = createContext<PermissionsState>({
  permissions: STATIC_DEFAULTS,
  loading: true,
  getRouteRoles: (path) => STATIC_DEFAULTS[path] ?? [],
  isRouteAllowed: () => false,
  refresh: () => {},
});

export function usePermissions() { return useContext(PermissionsCtx); }

export function PermissionsProvider({ children }: { children: React.ReactNode }) {
  const { token } = useAuth();
  // Start with static defaults so access control is correct from the first render
  const [permissions, setPermissions] = useState<PermMap>(STATIC_DEFAULTS);
  const [loading, setLoading] = useState(true);

  const fetch_ = useCallback(async (tok: string) => {
    try {
      const res = await fetch("/api/v1/admin/permissions", {
        headers: { Authorization: `Bearer ${tok}` },
      });
      if (res.ok) {
        const data = await res.json();
        // Merge backend overrides on top of static defaults so any
        // unmanaged route still falls back to its built-in role list
        setPermissions({ ...STATIC_DEFAULTS, ...(data.permissions ?? {}) });
      }
      // On non-OK (e.g. 403 for non-admin): keep static defaults — already set
    } catch {
      // Network error: keep static defaults
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (token) {
      setLoading(true);
      fetch_(token);
    } else {
      setLoading(false);
      setPermissions(STATIC_DEFAULTS);
    }
  }, [token, fetch_]);

  // Effective roles for a path: backend value if present, else static default, else []
  const getRouteRoles = useCallback(
    (path: string): string[] => permissions[path] ?? [],
    [permissions],
  );

  const isRouteAllowed = useCallback(
    (path: string, userRoles: string[], isAdmin: boolean): boolean => {
      if (isAdmin) return true;
      const roles = permissions[path];
      // Distinguish "unmanaged" from "explicitly restricted":
      //   • absent (undefined)  → route the matrix has never configured → open
      //     (back-compat: a newly added component stays visible until an admin
      //     decides who sees it).
      //   • present but EMPTY   → the admin unticked every role → admin-only.
      //   • present with roles  → visible to those roles.
      if (roles === undefined) return true;
      if (roles.length === 0) return false;
      return roles.some(r => userRoles.includes(r));
    },
    [permissions],
  );

  const refresh = useCallback(() => {
    if (token) fetch_(token);
  }, [token, fetch_]);

  return (
    <PermissionsCtx.Provider value={{ permissions, loading, getRouteRoles, isRouteAllowed, refresh }}>
      {children}
    </PermissionsCtx.Provider>
  );
}
