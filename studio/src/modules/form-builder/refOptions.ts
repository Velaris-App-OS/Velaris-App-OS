/* Reference-table options for a form field (Form Builder v2 step 3, 2026-10-29).
   A field reads its options only through the saved form — the server finds
   the table from the field. Staff forms ask case-service; the customer
   portal provides its own fetcher (the portal route) through the context. */

import { createContext } from "react";
import { getAccessToken } from "@/auth/tokenManager";

export type RefOption = { label: string; value: any; row?: Record<string, any> };
export type RefRequest = { field_key: string; group_key?: string; q: string; values: Record<string, any> };
export type RefFetcher = (req: RefRequest) => Promise<RefOption[]>;

function authHeaders(): Record<string, string> {
  const t = getAccessToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

/** Staff forms: POST /reference-tables/options with the saved form's id. */
export function staffFetcher(formId?: string): RefFetcher {
  return async req => {
    if (!formId) return [];
    const r = await fetch("/api/v1/reference-tables/options", {
      method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ form_id: formId, ...req }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Could not load the options");
    return Array.isArray(d.options) ? d.options : [];
  };
}

/** The customer portal: the case type's own form, anonymous. */
export function portalFetcher(slug: string, caseTypeId: string): RefFetcher {
  return async req => {
    const r = await fetch(`/api/v1/portal/${slug}/case-types/${caseTypeId}/form/options`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(req),
    });
    if (!r.ok) throw new Error("Could not load the options");
    const d = await r.json();
    return Array.isArray(d.options) ? d.options : [];
  };
}

/** Set by the portal; absent = the staff fetcher for the form's id. */
export const RefFetcherContext = createContext<RefFetcher | null>(null);
/** Set by a repeating group for its cells: the group's key. */
export const GroupKeyContext = createContext<string | undefined>(undefined);

/** The fields a chosen row fills: `{target: value}`. */
export function fillsFor(src: any, option: RefOption | undefined): Record<string, any> {
  const out: Record<string, any> = {};
  for (const m of Array.isArray(src?.fill) ? src.fill : []) {
    if (m?.field && m?.column) out[m.field] = option?.row?.[m.column] ?? undefined;
  }
  return out;
}
