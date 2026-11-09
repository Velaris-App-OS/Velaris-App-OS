import React, { useState, useEffect } from "react";
import type { AuthUser } from "@/auth";
import { useAuth } from "@/auth";
import { usePermissions } from "@/auth/PermissionsContext";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { BRAND } from "@/branding";
import { useTheme } from "@/theme/ThemeContext";
import GlobalSearch from "@shared/components/GlobalSearch";
import ProfileDrawer from "@shared/components/ProfileDrawer";
import PageHeader from "@shared/components/PageHeader";
import PageFooter from "@shared/components/PageFooter";
import { NAV_DATA } from "@/app/nav-data";
import { NAV_ICONS, getNavIcon, NAV_ICON_SIZE } from "@/app/nav-icons";
import { useFeatureFlags } from "@/app/FeatureFlagsContext";
import SandboxBanner from "@shared/components/SandboxBanner";
import DataAsOfNotice from "@shared/components/DataAsOfNotice";

/* ═══════════════════════════════════════════════════════════════════
   App Layout — sidebar + main content area
   ═══════════════════════════════════════════════════════════════════ */

// ── Role keys (match AuthUser flags) ──────────────────────────────
// Roles are evaluated with OR logic: user needs at least one.
// Empty array = visible to every authenticated user.
type NavRole = "admin" | "manager" | "designer" | "case_worker" | "devops" | "integration" | "security" | "viewer" | "developer";

interface NavItem {
  path:        string;
  label:       string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  icon:        any;
  roles?:      NavRole[];
  group?:      string;
  featureKey?: string;
}

// Icons come from nav-icons.tsx — edit that file to change any icon.
const NAV_ITEMS: NavItem[] = NAV_DATA.map(e => ({
  path:       e.path,
  label:      e.label,
  icon:       getNavIcon(e.path),
  roles:      e.roles.length > 0 ? e.roles : undefined,
  featureKey: e.featureKey,
}));

export default function AppLayout() {
  const [searchOpen, setSearchOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const location = useLocation();

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setSearchOpen(o => !o);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  const navEntry = NAV_DATA.find(
    n => n.path === "/" ? location.pathname === "/" : location.pathname === n.path || location.pathname.startsWith(n.path + "/")
  );

  return (
    <div style={{ display: "flex", height: "100vh", width: "100vw" }}>
      <Sidebar onOpenSearch={() => setSearchOpen(true)} onOpenProfile={() => setProfileOpen(true)} />
      <main style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", background: "var(--bg-root)" }}>
        <SandboxBanner />
        <DataAsOfNotice />
        <PageHeader
          title={navEntry?.label ?? BRAND.name}
          description={navEntry?.description}
          icon={navEntry ? getNavIcon(navEntry.path) : undefined}
          path={navEntry?.path}
        />
        <div style={{ flex: 1, overflow: "auto" }}>
          <Outlet />
        </div>
        <PageFooter />
      </main>
      <GlobalSearch open={searchOpen} onClose={() => setSearchOpen(false)} />
      <ProfileDrawer open={profileOpen} onClose={() => setProfileOpen(false)} />
    </div>
  );
}

// Maps NavRole keys to AuthUser flags
function userHasRole(user: { is_admin: boolean; is_designer: boolean; is_case_worker: boolean; roles: string[] } | null, roles: NavRole[]): boolean {
  if (!user) return false;
  if (user.is_admin) return true;                           // admin sees everything
  if (roles.length === 0) return true;                      // no restriction = everyone
  return roles.some(r => {
    if (r === "admin")       return user.is_admin;
    if (r === "manager")     return user.roles.includes("manager");
    if (r === "designer")    return user.is_designer || user.roles.includes("designer");
    if (r === "developer")   return user.is_designer || user.roles.includes("developer");
    if (r === "case_worker") return user.is_case_worker || user.roles.includes("case_worker");
    if (r === "devops")      return user.roles.includes("devops");
    if (r === "integration") return user.roles.includes("integration");
    if (r === "security")    return user.roles.includes("security");
    if (r === "viewer")      return user.roles.includes("viewer");
    return false;
  });
}

// Section groupings — evaluated top to bottom; first match wins.
// Security is listed before Development so HxStream/HxLogs land correctly.
const SECTION_GROUPS: { label: string; filter: (item: NavItem) => boolean }[] = [
  { label: "Workspace",   filter: i => !i.roles || i.roles.length === 0 },
  { label: "Cases",       filter: i => !!i.roles?.includes("case_worker") && !i.roles.includes("designer") && !i.roles.includes("devops") && !i.roles.includes("security") },
  { label: "Security",    filter: i => !!i.roles?.includes("security") },
  { label: "Development", filter: i => !!i.roles?.includes("designer") && !i.roles.includes("devops") && !i.roles.includes("security") && !i.roles.includes("integration") },
  { label: "DevOps",      filter: i => !!i.roles?.includes("devops") },
  { label: "Integration", filter: i => !!i.roles?.includes("integration") },
  { label: "Admin",        filter: i => !!i.roles?.length && i.roles.every(r => r === "admin" || r === "manager") },
];

export function Sidebar({ onOpenSearch, onOpenProfile }: { onOpenSearch: () => void; onOpenProfile: () => void }) {
  const location = useLocation();
  const { user } = useAuth();
  const { isRouteAllowed } = usePermissions();
  const { isEnabled, loading: flagsLoading } = useFeatureFlags();

  const visibleItems = flagsLoading ? [] : NAV_ITEMS.filter(item => {
    if (!isRouteAllowed(item.path, user?.roles ?? [], user?.is_admin ?? false)) return false;
    if (item.featureKey && !isEnabled(item.featureKey)) return false;
    return true;
  });

  // Group visible items into sections
  const sections = SECTION_GROUPS.map(g => ({
    label: g.label,
    items: visibleItems.filter(g.filter),
  })).filter(s => s.items.length > 0);
  const [openSections, toggleSection] = useOpenSections();

  return (
    <nav
      style={{
        width: 220,
        minWidth: 220,
        background: "var(--bg-panel)",
        borderRight: "1px solid var(--border-subtle)",
        display: "flex",
        flexDirection: "column",
        height: "100vh",
      }}
    >
      {/* Logo — click goes to Dashboard */}
      <div style={{ padding: "var(--space-md)", display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
        <NavLink to="/" style={{ display: "flex", alignItems: "center", gap: 10, textDecoration: "none", flex: 1 }}>
          <HelixLogo />
          <span style={{ fontFamily: "var(--font-mono)", fontWeight: 700, fontSize: 15, letterSpacing: "0.08em", color: "var(--text-primary)" }}>
            {BRAND.name}
          </span>
          <span style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--text-muted)", padding: "2px 6px", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)" }}>
            {BRAND.studio}
          </span>
        </NavLink>
        <ThemeToggle />
      </div>

      {/* Role badge */}
      {user && (
        <div style={{ padding: "0 var(--space-md) var(--space-sm)", flexShrink: 0 }}>
          <div style={{
            fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)",
            background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)",
            padding: "3px 8px", display: "inline-block",
          }}>
            {user.is_admin ? "admin" : user.is_designer ? "developer" : user.is_case_worker ? "staff" : "viewer"}
          </div>
        </div>
      )}

      {/* Global search trigger */}
      <div style={{ padding: "0 var(--space-md) var(--space-sm)", flexShrink: 0 }}>
        <button
          onClick={onOpenSearch}
          title="Search pages (Ctrl+K)"
          style={{
            width: "100%", display: "flex", alignItems: "center", gap: 8,
            padding: "7px 10px", borderRadius: "var(--radius-sm)",
            border: "1px solid var(--border-subtle)", background: "var(--bg-elevated)",
            color: "var(--text-muted)", cursor: "pointer", fontFamily: "var(--font-body)",
            fontSize: 12, textAlign: "left",
          }}
        >
          <span style={{ fontSize: 13 }}>🔍</span>
          <span style={{ flex: 1 }}>Search pages…</span>
          <kbd style={{
            fontSize: 9, padding: "1px 5px", borderRadius: 3,
            border: "1px solid var(--border-subtle)", background: "var(--bg-card)",
            fontFamily: "var(--font-mono)", color: "var(--text-muted)",
          }}>⌘K</kbd>
        </button>
      </div>

      {/* Scrollable nav */}
      <div style={{ flex: 1, overflowY: "auto", overflowX: "hidden", padding: "0 var(--space-md)", display: "flex", flexDirection: "column", gap: "var(--space-xs)" }}>
        {sections.map(section => {
          const closed = !openSections.has(section.label);
          // A closed section still shows the page you are on, so you never lose your place.
          const items = closed ? section.items.filter(item => isActivePath(item.path, location.pathname)) : section.items;
          return (
            <React.Fragment key={section.label}>
              <SectionLabel label={section.label} open={!closed} count={section.items.length}
                onToggle={() => toggleSection(section.label)} />
              {items.map(item => (
                <NavItem key={item.path} item={item} location={location} />
              ))}
            </React.Fragment>
          );
        })}
      </div>

      {/* Bottom */}
      <div style={{ flexShrink: 0, padding: "0 var(--space-md) var(--space-md)" }}>
        <UserBadge onOpenProfile={onOpenProfile} />
        <EngineStatus />
      </div>
    </nav>
  );
}

/* ── Collapsible sections (2026-10-29) ─────────────────────────
   Every section starts closed (user's choice); the page you are on always
   shows. What someone opens is remembered in this browser only — a
   convenience, so storage failing just means everything starts closed. */
const OPEN_KEY = "velaris.nav.open";

function useOpenSections(): [Set<string>, (label: string) => void] {
  const [open, setOpen] = useState<Set<string>>(() => {
    try {
      const raw = JSON.parse(localStorage.getItem(OPEN_KEY) || "[]");
      return new Set(Array.isArray(raw) ? raw.filter((x: unknown) => typeof x === "string") : []);
    } catch { return new Set(); }
  });
  const toggle = (label: string) => setOpen(prev => {
    const next = new Set(prev);
    if (next.has(label)) next.delete(label); else next.add(label);
    try { localStorage.setItem(OPEN_KEY, JSON.stringify([...next])); } catch { /* stays in memory */ }
    return next;
  });
  return [open, toggle];
}

function isActivePath(path: string, pathname: string): boolean {
  return path === "/" ? pathname === "/" : pathname === path || pathname.startsWith(path + "/");
}

function SectionLabel({ label, open, count, onToggle }: { label: string; open: boolean; count: number; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      title={open ? `Collapse ${label}` : `Expand ${label} (${count})`}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        width: "100%",
        background: "none",
        border: "none",
        cursor: "pointer",
        textAlign: "left",
        fontSize: 10,
        fontWeight: 600,
        color: "var(--text-muted)",
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        fontFamily: "var(--font-mono)",
        padding: "var(--space-md) var(--space-sm) var(--space-xs)",
      }}
    >
      <span aria-hidden style={{ display: "inline-block", width: 8, transition: "transform 0.12s", transform: open ? "rotate(90deg)" : "none" }}>›</span>
      <span style={{ flex: 1 }}>{label}</span>
      {!open && <span style={{ fontWeight: 400, letterSpacing: 0 }}>{count}</span>}
    </button>
  );
}

function NavItem({ item, location }: { item: typeof NAV_ITEMS[0]; location: ReturnType<typeof useLocation> }) {
  const isActive = isActivePath(item.path, location.pathname);

  return (
    <NavLink
      to={item.path}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "10px 12px",
        borderRadius: "var(--radius-sm)",
        fontSize: 13,
        fontWeight: 500,
        color: isActive ? "var(--accent)" : "var(--text-secondary)",
        background: isActive ? "var(--accent-dim)" : "transparent",
        textDecoration: "none",
        transition: "all 0.12s ease",
      }}
    >
      <item.icon active={isActive} size={NAV_ICON_SIZE} />
      {item.label}
    </NavLink>
  );
}

/* ── Engine status indicator at bottom of sidebar ─────────────── */

function EngineStatus() {
  const [status, setStatus] = React.useState<{
    database: string;
    temporal: string;
  } | null>(null);

  React.useEffect(() => {
    const check = () =>
      fetch("/api/ready")
        .then((r) => r.json())
        .then(setStatus)
        .catch(() => setStatus(null));

    check();
    const id = setInterval(check, 10000);
    return () => clearInterval(id);
  }, []);

  const dbOk = status?.database === "connected";
  const tmpOk = status?.temporal === "connected";

  return (
    <div
      style={{
        padding: "var(--space-md) var(--space-sm)",
        borderTop: "1px solid var(--border-subtle)",
        display: "flex",
        flexDirection: "column",
        gap: 6,
        fontSize: 11,
        fontFamily: "var(--font-mono)",
        color: "var(--text-muted)",
      }}
    >
      {/* This is the workflow ENGINE's database link, not the database itself.
          "offline" was read as "PostgreSQL is down" on a server that was plainly
          serving data: the engine had started without its database and was
          running on its in-memory store, which a restart of the engine fixes. */}
      <div
        style={{ display: "flex", alignItems: "center", gap: 6 }}
        title={
          dbOk
            ? "The workflow engine is connected to its database."
            : "The workflow engine is not connected to its database (it may be running on its in-memory store). The rest of the platform can still be serving data. Restart velaris-engine."
        }
      >
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: dbOk ? "var(--status-completed)" : "var(--status-failed)",
          }}
        />
        {dbOk ? "Engine database connected" : "Engine database not connected"}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: tmpOk ? "var(--status-completed)" : "var(--status-failed)",
          }}
        />
        Temporal {tmpOk ? "connected" : "offline"}
      </div>
    </div>
  );
}

/* ── Theme toggle ────────────────────────────────────────────────── */

function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const icon  = theme === "dark" ? "☀" : theme === "light" ? "◑" : "⬡";
  const label = theme === "dark" ? "dark → light" : theme === "light" ? "light → system" : "system → dark";
  return (
    <button onClick={toggle} title={`Theme: ${theme} (click: ${label})`}
      style={{
        background: "none", border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-sm)", cursor: "pointer",
        padding: "3px 6px", flexShrink: 0, color: "var(--text-muted)",
        fontSize: 13, lineHeight: 1, transition: "border-color 0.15s",
      }}>
      {icon}
    </button>
  );
}

/* ── Icons (inline SVG for zero dependencies) ─────────────────── */

function HelixLogo() {
  return (
    <img src={BRAND.logoSrc} alt={BRAND.name} width={28} height={28} style={{ objectFit: "contain", display: "block" }} />
  );
}

function UserBadge({ onOpenProfile }: { onOpenProfile: () => void }) {
  const { user, logout } = useAuth();
  if (!user) return null;
  return (
    <div style={{
      padding: "var(--space-sm) var(--space-sm)",
      borderTop: "1px solid var(--border-subtle)",
      display: "flex", alignItems: "center", gap: 8,
      marginBottom: "var(--space-xs)",
    }}>
      <button onClick={onOpenProfile} title="View your profile" style={{
        display: "flex", alignItems: "center", gap: 8, flex: 1, minWidth: 0,
        background: "none", border: "none", cursor: "pointer", padding: 0, textAlign: "left",
      }}>
        <div style={{
          width: 28, height: 28, borderRadius: "50%",
          background: "var(--accent-dim)", border: "1px solid var(--accent)",
          display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: 12, fontWeight: 600, color: "var(--accent)",
          fontFamily: "var(--font-mono)", flexShrink: 0,
        }}>
          {user.username[0]?.toUpperCase()}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 12, fontWeight: 500, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {user.username}
          </div>
          <div style={{ fontSize: 9, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase" }}>
            {user.roles.slice(0, 2).join(" · ")}
          </div>
        </div>
      </button>
      <button onClick={logout} title="Sign out" style={{
        background: "transparent", border: "none", color: "var(--text-muted)",
        cursor: "pointer", fontSize: 14, padding: 4, flexShrink: 0,
      }}
        onMouseEnter={e => e.currentTarget.style.color = "var(--status-failed)"}
        onMouseLeave={e => e.currentTarget.style.color = "var(--text-muted)"}
      >⏻</button>
    </div>
  );
}

