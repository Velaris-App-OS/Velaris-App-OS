/**
 * Embed field — another site's page in a form, approved sites only (2026-10-29).
 *
 * The server keeps each tenant's approved sites (velaris_shared/forms/embed.py)
 * and refuses to save a form naming any other. This is the last check, and the
 * one every form passes however it was written: the frame is drawn only when
 * its https origin is on the list and is not Studio's own (a page from there
 * could reach the signed-in session). The frame is sandboxed, sends no
 * referrer and gets no camera, microphone or location.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { createContext, useContext, useEffect, useState } from "react";
import type { FormFieldDef } from "./FormBuilder";
import { getEmbedSettings } from "@shared/api/client";

/** The extra sandbox permissions a designer may grant (the server's PERMISSIONS). */
export const EMBED_PERMISSIONS: Record<string, string> = { forms: "allow-forms", popups: "allow-popups" };
const HOST = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/;

/** `https://host[:port]` of an https url with a real host name, else null — as the server. */
export function embedOrigin(url: unknown): string | null {
  if (typeof url !== "string" || url.length > 2000 || /\s/.test(url)) return null;
  let u: URL;
  try { u = new URL(url); } catch { return null; }
  const host = u.hostname.toLowerCase();
  if (u.protocol !== "https:" || u.username || u.password) return null;
  if (!HOST.test(host) || /^[0-9.]+$/.test(host)) return null;
  return `https://${host}${u.port && u.port !== "443" ? `:${u.port}` : ""}`;
}

/** The approved sites: given by the page (the portal), else read for the signed-in user. */
export const EmbedOriginsContext = createContext<string[] | null>(null);

let cached: Promise<string[]> | null = null;
/** Whether an embed may take typing (`allow-forms`) — the operator's policy. */
let cachedForms: Promise<boolean> | null = null;
/** For tests and after an admin changes the list. */
export function resetEmbedOrigins() { cached = null; cachedForms = null; }

export function useEmbedAllowsForms(): boolean {
  // On the portal the sites come with the form (context) and the server has
  // already removed "forms" when typing is off — never ask the signed-in API there.
  const given = useContext(EmbedOriginsContext);
  const [ok, setOk] = useState(true);
  useEffect(() => {
    if (given) return;
    let live = true;
    cachedForms ??= getEmbedSettings().then(s => s.allow_forms !== false, () => true);
    cachedForms.then(v => live && setOk(v));
    return () => { live = false; };
  }, [given]);
  return given ? true : ok;
}

export function useEmbedOrigins(): string[] | null {
  const given = useContext(EmbedOriginsContext);
  const [loaded, setLoaded] = useState<string[] | null>(null);
  useEffect(() => {
    if (given) return;
    let live = true;
    cached ??= getEmbedSettings().then(s => s.allowed_origins, () => []);
    cached.then(o => live && setLoaded(o));
    return () => { live = false; };
  }, [given]);
  return given ?? loaded;
}

/** Why an embed is not drawn, or null when it may be. */
export function embedRefusal(url: unknown, origins: string[] | null, own: string = window.location.origin): string | null {
  const origin = embedOrigin(url);
  if (!origin) return "Not an https page address.";
  if (origin === own) return "This platform's own pages cannot be embedded.";
  if (!origins) return "Checking the approved sites…";
  if (!origins.includes(origin)) return `${origin} is not an approved site.`;
  return null;
}

export function EmbedBlock({ field }: { field: FormFieldDef }) {
  const f = field as FormFieldDef & { url?: string; height?: number; permissions?: string[] };
  const origins = useEmbedOrigins();
  const refusal = embedRefusal(f.url, origins);
  const height = Math.min(Math.max(Number(f.height) || 400, 100), 1200);
  const allowsForms = useEmbedAllowsForms();
  const sandbox = ["allow-scripts", "allow-same-origin",
    ...(Array.isArray(f.permissions) ? f.permissions : [])
      .filter(p => p !== "forms" || allowsForms)          // the operator may switch typing off
      .map(p => EMBED_PERMISSIONS[p]).filter(Boolean)].join(" ");
  const source = embedOrigin(f.url)?.replace(/^https:\/\//, "");
  return (
    <div>
      {field.label && <div style={{ fontSize: 12, fontWeight: 500, color: "var(--text-secondary)", marginBottom: 4 }}>{field.label}</div>}
      {refusal ? (
        <div role="note" style={{ padding: "10px 12px", fontSize: 12, color: "var(--text-muted)",
          border: "1px dashed var(--border-default)", borderRadius: "var(--radius-sm)" }}>
          Embedded page not shown. {refusal}
        </div>
      ) : (
        <>
        {/* Always shown, never configurable (2026-11-04): a frame must not pass
            for part of Velaris — a lookalike sign-in or payment box is the attack. */}
        <div role="note" style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11,
          padding: "4px 8px", border: "1px solid var(--border-subtle)", borderBottom: "none",
          borderRadius: "var(--radius-sm) var(--radius-sm) 0 0", background: "var(--bg-subtle, var(--bg-input))",
          color: "var(--text-secondary)" }}>
          <span aria-hidden>↗</span>
          <span>External content from <strong>{source}</strong> — not part of this platform. Do not enter passwords or card details here.</span>
        </div>
        <iframe
          src={f.url}
          title={field.label || "Embedded page"}
          sandbox={sandbox}
          referrerPolicy="no-referrer"
          allow=""
          loading="lazy"
          style={{ width: "100%", height, border: "1px solid var(--border-subtle)", borderRadius: "0 0 var(--radius-sm) var(--radius-sm)", display: "block" }}
        />
        </>
      )}
      {field.description && <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>{field.description}</div>}
    </div>
  );
}
