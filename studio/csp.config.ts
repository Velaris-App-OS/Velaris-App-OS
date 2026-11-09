/**
 * Studio's security headers (2026-10-29) — sent by the production server
 * (`vite preview`, velaris-studio.service), never by the dev server, which
 * needs inline scripts for hot reload.
 *
 * VELARIS_STUDIO_CSP:
 *   unset / "report-only"  the policy is only REPORTED (Content-Security-Policy-
 *                          Report-Only): nothing is blocked, so an install that
 *                          takes this update behaves exactly as before; browsers
 *                          send what the policy WOULD block to /api/v1/csp-reports.
 *   "enforce"              the policy is applied, with X-Frame-Options,
 *                          X-Content-Type-Options, Referrer-Policy and
 *                          Permissions-Policy. Turn on once the reports are clean.
 *   "off"                  no header at all.
 *
 * Added to connect-src: the meeting server (VELARIS_CASE_LIVEKIT_URL), and
 * VELARIS_STUDIO_CSP_CONNECT — extra origins, space separated, for an
 * operator's own services.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */

export type CspMode = "report-only" | "enforce" | "off";

export function cspMode(raw: string | undefined): CspMode {
  const v = (raw ?? "").trim().toLowerCase();
  return v === "enforce" || v === "off" ? v : "report-only";
}

/** `scheme://host[:port]` of a URL, or null; a ws(s) URL also gives its http(s) twin. */
function origins(url: string | undefined): string[] {
  if (!url) return [];
  try {
    const u = new URL(url.trim());
    if (!["ws:", "wss:", "http:", "https:"].includes(u.protocol) || !u.host) return [];
    const twin = { "ws:": "http:", "wss:": "https:", "http:": "ws:", "https:": "wss:" }[u.protocol]!;
    return [`${u.protocol}//${u.host}`, `${twin}//${u.host}`];
  } catch {
    return [];
  }
}

export function contentSecurityPolicy(env: Record<string, string | undefined>, report = true): string {
  const extra = [
    ...origins(env.VELARIS_CASE_LIVEKIT_URL),
    ...(env.VELARIS_STUDIO_CSP_CONNECT ?? "").split(/\s+/).flatMap(origins),
  ];
  const connect = ["'self'", ...new Set(extra)];
  const directives: [string, string[]][] = [
    ["default-src", ["'self'"]],
    // Studio's own bundle only: no inline script, no eval (checked 2026-10-29).
    ["script-src", ["'self'"]],
    // React style props and a few <style> blocks; the fonts' stylesheet.
    ["style-src", ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"]],
    ["font-src", ["'self'", "data:", "https://fonts.gstatic.com"]],
    // Signatures and charts (data:, blob:), logos and e-mail images (https:).
    ["img-src", ["'self'", "data:", "blob:", "https:"]],
    ["media-src", ["'self'", "blob:"]],
    ["connect-src", connect],
    // Map tiles' workers and the service workers.
    ["worker-src", ["'self'", "blob:"]],
    // Embedded pages: https only here; which sites, per tenant, is the
    // approved list the app and the server check (velaris_shared/forms/embed.py).
    ["frame-src", ["'self'", "blob:", "https:"]],
    ["frame-ancestors", ["'self'"]],
    ["object-src", ["'none'"]],
    ["base-uri", ["'self'"]],
    ["form-action", ["'self'"]],
    ["manifest-src", ["'self'"]],
  ];
  const policy = directives.map(([k, v]) => `${k} ${v.join(" ")}`).join("; ");
  return report ? `${policy}; report-uri /api/v1/csp-reports` : policy;
}

export function securityHeaders(env: Record<string, string | undefined>): Record<string, string> {
  const mode = cspMode(env.VELARIS_STUDIO_CSP);
  if (mode === "off") return {};
  if (mode === "report-only") return { "Content-Security-Policy-Report-Only": contentSecurityPolicy(env) };
  return {
    "Content-Security-Policy": contentSecurityPolicy(env),
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(self), microphone=(self), geolocation=(self), payment=(), usb=(), serial=(), hid=()",
  };
}
