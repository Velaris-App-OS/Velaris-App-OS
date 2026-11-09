// R5d 11b — "data as of".
//
// A read path that may run on a read replica answers with
// `X-Velaris-Data-As-Of`: the time its data is current to. Normally that is a
// moment ago; at a peak the replica can fall behind, and the platform keeps the
// read there rather than move the load onto the main database (agreed
// 2026-10-22). The page should then say so instead of passing old figures off
// as live. Every response goes through main.tsx's fetch hook, which hands it
// here; DataAsOfNotice shows the result.
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1

export const DATA_AS_OF_EVENT = "velaris:data-as-of";
/** Below this, figures count as live and nothing is shown. */
export const STALE_NOTICE_SECONDS = 10;

export type DataAsOf = { asOf: string; ageSeconds: number; url: string };

export function noteDataAsOf(res: Response, now: number = Date.now()): DataAsOf | null {
  const raw = res.headers?.get?.("x-velaris-data-as-of");
  if (!raw) return null;
  const t = Date.parse(raw);
  if (Number.isNaN(t)) return null;
  const detail: DataAsOf = { asOf: raw, ageSeconds: Math.max(0, (now - t) / 1000), url: res.url };
  window.dispatchEvent(new CustomEvent<DataAsOf>(DATA_AS_OF_EVENT, { detail }));
  return detail;
}
