/**
 * Allowed sites — Admin › Security (2026-11-04).
 *
 * The one place outside sites are approved. First section: sites a form's
 * Embedded Page field may show. A tenant's administrator keeps the tenant's own
 * list (when the operator allows tenants one); the platform operator also sets
 * the platform policy — sites approved for every tenant, sites blocked for all,
 * whether tenants keep their own lists, and whether an embed may take typing.
 * Every change is a security event, listed at the bottom.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/auth";
import { usePermissions } from "@/auth/PermissionsContext";
import {
  apiFetch, getAllowedSitesPolicy, getEmbedSettings, putAllowedSitesPolicy, putEmbedSettings,
  type AllowedSitesPolicy, type EmbedSettings,
} from "@shared/api/client";
import { embedOrigin, resetEmbedOrigins } from "../form-builder/Embed";

const box: React.CSSProperties = {
  background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)",
  padding: 16, marginBottom: 16,
};
const area: React.CSSProperties = {
  width: "100%", boxSizing: "border-box", fontFamily: "var(--font-mono)", fontSize: 12, padding: 8,
  background: "var(--bg-input)", color: "var(--text-primary)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)",
};
const primary: React.CSSProperties = {
  padding: "6px 14px", fontSize: 12, fontWeight: 600, background: "var(--accent)", color: "#fff",
  border: "none", borderRadius: 6, cursor: "pointer",
};

const lines = (t: string) => t.split("\n").map(l => l.trim()).filter(Boolean);
const badOf = (t: string) => lines(t).filter(l => !embedOrigin(l));

function Muted({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 8 }}>{children}</div>;
}

function List({ title, items, empty }: { title: string; items?: string[]; empty: string }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{title}</div>
      {items && items.length
        ? <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12, fontFamily: "var(--font-mono)" }}>{items.map(o => <li key={o}>{o}</li>)}</ul>
        : <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{empty}</div>}
    </div>
  );
}

function TenantSites({ settings, onSaved }: { settings: EmbedSettings; onSaved: (s: EmbedSettings) => void }) {
  const { user } = useAuth();
  const admin = !!user?.is_admin;
  const mayAdd = settings.may_add !== false;
  const [text, setText] = useState((settings.own_origins ?? settings.allowed_origins).join("\n"));
  const [portal, setPortal] = useState(settings.portal);
  const [state, setState] = useState<"ready" | "saving" | "saved">("ready");
  const [error, setError] = useState<string | null>(null);
  const bad = badOf(text);
  const blocked = lines(text).filter(l => (settings.blocked ?? []).includes(embedOrigin(l) ?? ""));

  const save = async () => {
    setState("saving"); setError(null);
    try {
      const s = await putEmbedSettings({ allowed_origins: lines(text), portal });
      resetEmbedOrigins();
      onSaved(s); setText((s.own_origins ?? s.allowed_origins).join("\n")); setPortal(s.portal);
      setState("saved");
    } catch (e: any) {
      setError(e?.message || "Not saved"); setState("ready");
    }
  };

  return (
    <div>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>Your tenant's sites</div>
      {mayAdd ? (
        <Muted>One per line, https only (e.g. https://www.youtube-nocookie.com). Only the site part is kept.
          Every viewer sees a label naming the site above the embedded page.</Muted>
      ) : (
        <Muted>The platform's operator approves the sites forms may embed — a tenant cannot add its own here.</Muted>
      )}
      <textarea aria-label="Your tenant's sites" rows={5} value={text} disabled={!admin || !mayAdd}
        onChange={e => { setText(e.target.value); setState("ready"); }} style={area} />
      {bad.length > 0 && <div style={{ fontSize: 11, color: "var(--status-failed)", marginTop: 4 }}>Not an https site: {bad.join(", ")}</div>}
      {blocked.length > 0 && <div style={{ fontSize: 11, color: "var(--status-failed)", marginTop: 4 }}>Blocked on this platform: {blocked.join(", ")}</div>}
      <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12, marginTop: 8 }}>
        <input type="checkbox" checked={portal} disabled={!admin} onChange={e => { setPortal(e.target.checked); setState("ready"); }} />
        Also show embedded pages on the customer portal (signed-out visitors)
      </label>
      {error && <div style={{ fontSize: 12, color: "var(--status-failed)", marginTop: 8 }}>{error}</div>}
      <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 8, marginTop: 10 }}>
        {!admin && <span style={{ fontSize: 11, color: "var(--text-muted)", flex: 1 }}>Only an administrator can change this list.</span>}
        {state === "saved" && <span style={{ fontSize: 12, color: "var(--status-success)" }}>Saved</span>}
        {admin && <button style={primary} onClick={save}
          disabled={bad.length > 0 || blocked.length > 0 || state === "saving"}>{state === "saving" ? "Saving…" : "Save"}</button>}
      </div>
    </div>
  );
}

function PlatformPolicy({ onSaved }: { onSaved: () => void }) {
  const [policy, setPolicy] = useState<AllowedSitesPolicy | null>(null);
  const [plat, setPlat] = useState("");
  const [blocked, setBlocked] = useState("");
  const [state, setState] = useState<"ready" | "saving" | "saved">("ready");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getAllowedSitesPolicy().then(p => {
      setPolicy(p); setPlat(p.platform_origins.join("\n")); setBlocked(p.blocked.join("\n"));
    }).catch(e => setError(e?.message || "Could not load the platform policy"));
  }, []);
  if (!policy) return error ? <div style={{ fontSize: 12, color: "var(--status-failed)" }}>{error}</div> : null;

  const bad = [...badOf(plat), ...badOf(blocked)];
  const save = async () => {
    setState("saving"); setError(null);
    try {
      const p = await putAllowedSitesPolicy({ ...policy, platform_origins: lines(plat), blocked: lines(blocked) });
      setPolicy(p); setPlat(p.platform_origins.join("\n")); setBlocked(p.blocked.join("\n"));
      resetEmbedOrigins(); onSaved(); setState("saved");
    } catch (e: any) {
      setError(e?.message || "Not saved"); setState("ready");
    }
  };
  const set = (p: Partial<AllowedSitesPolicy>) => { setPolicy({ ...policy, ...p }); setState("ready"); };

  return (
    <div style={{ borderTop: "1px solid var(--border-subtle)", marginTop: 16, paddingTop: 16 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>Platform policy <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>— platform operator</span></div>
      <Muted>Applies to every tenant on this install.</Muted>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 12 }}>
        <div>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>Approved for every tenant</div>
          <textarea aria-label="Approved for every tenant" rows={5} value={plat} onChange={e => { setPlat(e.target.value); setState("ready"); }} style={area} />
        </div>
        <div>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>Blocked for every tenant</div>
          <textarea aria-label="Blocked for every tenant" rows={5} value={blocked} onChange={e => { setBlocked(e.target.value); setState("ready"); }} style={area} />
        </div>
      </div>
      {bad.length > 0 && <div style={{ fontSize: 11, color: "var(--status-failed)", marginTop: 4 }}>Not an https site: {bad.join(", ")}</div>}
      <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12, marginTop: 10 }}>
        <input type="checkbox" checked={policy.tenants_may_add} onChange={e => set({ tenants_may_add: e.target.checked })} />
        Tenants may approve their own sites (website demo tenants never can)
      </label>
      <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12, marginTop: 6 }}>
        <input type="checkbox" checked={policy.allow_forms} onChange={e => set({ allow_forms: e.target.checked })} />
        An embedded page may take typing (the field's "forms" permission)
      </label>
      {error && <div style={{ fontSize: 12, color: "var(--status-failed)", marginTop: 8 }}>{error}</div>}
      <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 8, marginTop: 10 }}>
        {state === "saved" && <span style={{ fontSize: 12, color: "var(--status-success)" }}>Saved</span>}
        <button style={primary} onClick={save} disabled={bad.length > 0 || state === "saving"}>{state === "saving" ? "Saving…" : "Save policy"}</button>
      </div>
    </div>
  );
}

type Change = { id: string; user_id: string | null; timestamp: string | null; details: Record<string, any> };

function Changes({ tick }: { tick: number }) {
  const [rows, setRows] = useState<Change[] | null>(null);
  useEffect(() => {
    apiFetch("/enterprise/security-events?event_type=allowed_sites_changed&days=365&limit=50")
      .then(r => (r.ok ? r.json() : []))
      .then(d => setRows(Array.isArray(d) ? d : []))
      .catch(() => setRows([]));
  }, [tick]);
  return (
    <div style={box}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>Recent changes</div>
      {!rows ? <Muted>Loading…</Muted> : rows.length === 0 ? <Muted>No change recorded yet.</Muted> : (
        <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
          <thead><tr style={{ textAlign: "left", color: "var(--text-muted)" }}><th>When</th><th>Who</th><th>Scope</th><th>Change</th></tr></thead>
          <tbody>
            {rows.map(r => {
              const d = r.details || {};
              const what = d.scope === "platform"
                ? "Platform policy changed"
                : [d.added?.length ? `added ${d.added.join(", ")}` : "", d.removed?.length ? `removed ${d.removed.join(", ")}` : ""]
                    .filter(Boolean).join("; ") || "portal setting";
              return (
                <tr key={r.id} style={{ borderTop: "1px solid var(--border-subtle)" }}>
                  <td style={{ padding: "4px 6px 4px 0", whiteSpace: "nowrap" }}>{r.timestamp ? new Date(r.timestamp).toLocaleString() : ""}</td>
                  <td style={{ padding: "4px 6px", fontFamily: "var(--font-mono)" }}>{r.user_id}</td>
                  <td style={{ padding: "4px 6px" }}>{d.scope}</td>
                  <td style={{ padding: "4px 0" }}>{what}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function AllowedSites() {
  const { isOperator } = usePermissions();
  const [settings, setSettings] = useState<EmbedSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => {
    getEmbedSettings().then(setSettings).catch(e => setError(e?.message || "Could not load the approved sites"));
    setTick(t => t + 1);
  }, []);
  useEffect(() => { reload(); }, [reload]);

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", height: "100%", overflow: "auto" }}>
      <div style={box}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 2 }}>Embedded in forms</div>
        <Muted>Sites a form's Embedded Page field may show. A site on no list is never shown, and a form naming one
          cannot be saved. Every embedded page carries a label naming its site.</Muted>
        {error && <div style={{ fontSize: 12, color: "var(--status-failed)" }}>{error}</div>}
        {settings && (
          <>
            <List title="Shown in your tenant's forms now" items={settings.allowed_origins} empty="None — no form shows an embedded page." />
            {(settings.platform_origins?.length ?? 0) > 0 && <List title="Approved by the platform for every tenant" items={settings.platform_origins} empty="" />}
            {(settings.blocked?.length ?? 0) > 0 && <List title="Blocked on this platform" items={settings.blocked} empty="" />}
            <TenantSites key={tick} settings={settings} onSaved={s => { setSettings(s); setTick(t => t + 1); }} />
            {isOperator && <PlatformPolicy onSaved={reload} />}
          </>
        )}
      </div>
      <Changes tick={tick} />
    </div>
  );
}
