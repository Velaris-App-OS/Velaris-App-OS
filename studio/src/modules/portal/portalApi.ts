/**
 * Portal v2 (P1) — customer-session API layer.
 *
 * The customer token (24h HS256 JWT, P65) lives in localStorage under
 * `helix_cust_${slug}`. `slideSession` renews it once per page load while it
 * is still valid, so an active customer never gets logged out mid-visit; an
 * abandoned session still dies within 24h (an expired token cannot refresh).
 */

import { portalErrorText } from "./portalError";

const tokenKey = (slug: string) => `helix_cust_${slug}`;

export const getCustomerToken = (slug: string) => localStorage.getItem(tokenKey(slug));
export const setCustomerToken = (slug: string, tok: string) => localStorage.setItem(tokenKey(slug), tok);
export const clearCustomerToken = (slug: string) => localStorage.removeItem(tokenKey(slug));

export class PortalAuthError extends Error {}

async function custFetch<T>(slug: string, path: string, options?: RequestInit): Promise<T> {
  const tok = getCustomerToken(slug);
  const res = await fetch(`/api/v1/portal/${slug}${path}`, {
    ...options,
    headers: {
      ...(options?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(tok ? { Authorization: `Bearer ${tok}` } : {}),
      ...options?.headers,
    },
  });
  if (res.status === 401) {
    clearCustomerToken(slug);
    throw new PortalAuthError("Your session has expired — please log in again.");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(portalErrorText(body, res.statusText));
  }
  return res.json();
}

let _slid = false;
/** Renew the customer token once per page load (sliding 24h window). */
export async function slideSession(slug: string): Promise<void> {
  if (_slid || !getCustomerToken(slug)) return;
  _slid = true;
  try {
    const d = await custFetch<{ customer_token: string }>(slug, "/auth/refresh", { method: "POST" });
    setCustomerToken(slug, d.customer_token);
  } catch { /* expired/invalid — custFetch already cleared it */ }
}

// ── Case detail (customer-JWT endpoints) ──────────────────────────

export type StageRailEntry = { id: string; label: string; current: boolean; reached: boolean };
export type CaseDetailData = {
  case_id: string; case_number: string | null; tracking_token: string | null;
  subject: string; description: string; status: string; priority: string;
  case_type_name: string; submitted_at: string | null; updated_at: string | null;
  resolved_at: string | null;
  stage_rail: StageRailEntry[];
  expected_days: number | null;
  sla: {
    deadline_at: string; status: string; tier: "green" | "amber" | "red";
    remaining_seconds: number; breached: boolean; breached_at: string | null;
  } | null;
};
export type TimelineEntry = {
  id: string; action: string; label: string; timestamp: string | null;
  details: Record<string, string>;
};
export type CaseDoc = {
  id: string; filename: string; content_type: string; size_bytes: number | null;
  source: string; uploaded_at: string | null; download_url: string;
};
export type CaseMeetSession = {
  session_id: string; title: string | null; record_intent: boolean; started_at: string | null;
};

export const getCaseDetail = (slug: string, caseId: string) =>
  custFetch<CaseDetailData>(slug, `/account/cases/${caseId}`);

export const getCaseTimeline = (slug: string, caseId: string) =>
  custFetch<{ timeline: TimelineEntry[]; pending_payment_step: boolean }>(
    slug, `/account/cases/${caseId}/timeline`);

export const getCaseDocuments = (slug: string, caseId: string) =>
  custFetch<{ documents: CaseDoc[] }>(slug, `/account/cases/${caseId}/documents`);

export function uploadCaseDocument(slug: string, caseId: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  return custFetch<{ filename: string }>(slug, `/account/cases/${caseId}/documents`,
    { method: "POST", body: form });
}

export const sendCaseChat = (slug: string, caseId: string, message: string) =>
  custFetch<{ reply: string; ai_available: boolean }>(slug, `/account/cases/${caseId}/chat`,
    { method: "POST", body: JSON.stringify({ message }) });

export type CaseAction = {
  step_id: string; name: string; type: "approval" | "form" | "document";
  prompt: string; required: boolean;
  form_fields: { key: string; label: string; type?: string; required?: boolean }[];
};

export const getCaseActions = (slug: string, caseId: string) =>
  custFetch<{ actions: CaseAction[] }>(slug, `/account/cases/${caseId}/actions`);

export const completeCaseAction = (
  slug: string, caseId: string, stepId: string,
  body: { decision?: string; data?: Record<string, string>; comment?: string },
) =>
  custFetch<{ step_id: string; status: string; auto_advanced: boolean }>(
    slug, `/account/cases/${caseId}/actions/${stepId}/complete`,
    { method: "POST", body: JSON.stringify(body) });

export type PortalMessage = {
  id: string; author_name: string | null; mine: boolean;
  body: string; created_at: string | null;
};

export const getCaseMessages = (slug: string, caseId: string) =>
  custFetch<{ messages: PortalMessage[] }>(slug, `/account/cases/${caseId}/messages`);

export const postCaseMessage = (slug: string, caseId: string, body: string) =>
  custFetch<{ id: string }>(slug, `/account/cases/${caseId}/messages`,
    { method: "POST", body: JSON.stringify({ body }) });

export const setNotifyEmail = (slug: string, value: boolean) =>
  custFetch<{ ok: boolean }>(slug, "/account",
    { method: "PUT", body: JSON.stringify({ notify_email: value }) });

export const getCaseSessions = (slug: string, caseId: string) =>
  custFetch<{ sessions: CaseMeetSession[] }>(slug, `/account/cases/${caseId}/sessions`);

// ── P5: CSAT + deflection feedback + portal forms ───────────────────

export const getCsat = (slug: string, caseId: string) =>
  custFetch<{ rated: boolean; rating: number | null; can_rate: boolean }>(
    slug, `/account/cases/${caseId}/csat`);

export const postCsat = (slug: string, caseId: string, rating: number, comment?: string) =>
  custFetch<{ ok: boolean }>(slug, `/account/cases/${caseId}/csat`,
    { method: "POST", body: JSON.stringify({ rating, comment }) });

/** Anonymous — no customer token involved. */
export async function postAskFeedback(slug: string, question: string, helpful: boolean): Promise<void> {
  await fetch(`/api/v1/portal/${slug}/ask/feedback`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, helpful }),
  }).catch(() => {});
}

export type PortalFormField = {
  key: string; label: string; type: string; required: boolean;
  placeholder: string; options: string[];
};

export async function getCaseTypeForm(slug: string, caseTypeId: string): Promise<PortalFormField[]> {
  const r = await fetch(`/api/v1/portal/${slug}/case-types/${caseTypeId}/form`);
  if (!r.ok) return [];
  return (await r.json()).fields ?? [];
}

/** The case type's form as the portal draws it (part 10): fields a customer
 *  cannot fill in are left out by the server. Null when none is attached. */
export async function getCaseTypeFormDefinition(slug: string, caseTypeId: string): Promise<any | null> {
  const r = await fetch(`/api/v1/portal/${slug}/case-types/${caseTypeId}/form`);
  if (!r.ok) return null;
  const body = await r.json();
  const d = body.definition;
  // Embeds (2026-10-29): the sites this portal may show, checked again in the browser.
  return d && d.sections?.length ? { ...d, embed_origins: Array.isArray(body.embed_origins) ? body.embed_origins : [] } : null;
}

// ── P3b: voice input on the portal ──────────────────────────────────

export type PortalVoiceStatus = {
  available: boolean; fill_available: boolean;
  consent_text: string | null; engine: string | null;
  realtime: boolean | null; max_clip_seconds: number;
};

export type VoiceCandidate = {
  field_key: string; label: string; value: unknown; kind: string;
  quote: string; speaker: string | null; confidence: number;
};

export const getVoiceStatus = (slug: string) =>
  custFetch<PortalVoiceStatus>(slug, "/account/voice/status");

/**
 * POST one PCM16 clip. Raw bytes, not JSON — `custFetch` would stamp a JSON
 * content-type on it, and the abort ceiling below is specific to this call:
 * ~1.4 MB up a phone's uplink can take 15 s, and a dropped connection has to
 * surface as a retryable error rather than a spinner that never ends.
 */
export async function transcribeVoice(
  slug: string, caseId: string, pcm: ArrayBuffer, consent: boolean,
): Promise<{ text: string; engine: string | null; realtime: boolean | null }> {
  const tok = getCustomerToken(slug);
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 90_000);
  try {
    const res = await fetch(
      `/api/v1/portal/${slug}/account/cases/${caseId}/voice/transcribe?consent=${consent}`,
      {
        method: "POST", body: pcm, signal: ctl.signal,
        headers: {
          "Content-Type": "application/octet-stream",
          ...(tok ? { Authorization: `Bearer ${tok}` } : {}),
        },
      },
    );
    if (res.status === 401) {
      clearCustomerToken(slug);
      throw new PortalAuthError("Your session has expired — please log in again.");
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(portalErrorText(body, res.statusText));
    }
    return await res.json();
  } catch (e: any) {
    if (e?.name === "AbortError") throw new Error("The upload timed out — please try again.");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

export const voiceFillAction = (slug: string, caseId: string, stepId: string, text: string) =>
  custFetch<{ candidates: VoiceCandidate[]; fields_considered: number }>(
    slug, `/account/cases/${caseId}/voice/fill`,
    { method: "POST", body: JSON.stringify({ text, step_id: stepId }) });

export const getSessionToken = (slug: string, caseId: string, sessionId: string) =>
  custFetch<{ url: string; token: string; title: string | null; record_intent: boolean; session_id?: string }>(
    slug, `/account/cases/${caseId}/sessions/${sessionId}/token`, { method: "POST" });

/** Oct item 8f — GDPR Art. 15: the customer downloads what the organisation
 *  holds about them. Their OWN account only; returned as the JSON text. */
export async function downloadMyData(slug: string): Promise<string> {
  const tok = getCustomerToken(slug);
  const res = await fetch(`/api/v1/portal/${slug}/account/export`, {
    headers: tok ? { Authorization: `Bearer ${tok}` } : {},
  });
  if (res.status === 401) {
    clearCustomerToken(slug);
    throw new PortalAuthError("Your session has expired — please log in again.");
  }
  if (res.status === 429) throw new Error("You have downloaded your data several times recently — please try again later.");
  if (!res.ok) throw new Error("Your data could not be prepared just now.");
  return res.text();
}
