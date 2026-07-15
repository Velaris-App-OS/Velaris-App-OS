/**
 * Portal v2 (P1) — customer-session API layer.
 *
 * The customer token (24h HS256 JWT, P65) lives in localStorage under
 * `helix_cust_${slug}`. `slideSession` renews it once per page load while it
 * is still valid, so an active customer never gets logged out mid-visit; an
 * abandoned session still dies within 24h (an expired token cannot refresh).
 */

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
    throw new Error(typeof body.detail === "string" ? body.detail : res.statusText);
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

export const getSessionToken = (slug: string, caseId: string, sessionId: string) =>
  custFetch<{ url: string; token: string; title: string | null; record_intent: boolean; session_id?: string }>(
    slug, `/account/cases/${caseId}/sessions/${sessionId}/token`, { method: "POST" });
