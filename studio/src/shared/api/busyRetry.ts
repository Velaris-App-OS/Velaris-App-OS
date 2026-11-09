/**
 * Retry a request the server refused as "busy" — VEL-AUTH-CHECK-BUSY-503.
 *
 * That code means the per-request sign-in check could not reach the database,
 * so the route never ran: retrying is safe for every method, a create
 * included. Any other 503 is passed straight back — it may come from inside a
 * route that partly ran.
 *
 * The wait follows the server's Retry-After first, then backs off
 * 5 → 10 → 20 → 30 → 30 → 30 s (about two minutes in all) before handing the
 * last 503 back to the caller. No page reload: whatever the user was typing
 * stays on screen.
 *
 * Progress is published as window events for <BusyBanner />:
 *   velaris:busy        { retrying: number, nextInSeconds: number }
 *   velaris:busy-clear  the last retrying request finished
 *   velaris:busy-gaveup a request gave up; its caller got the 503
 */

export const AUTH_CHECK_BUSY_CODE = "VEL-AUTH-CHECK-BUSY-503";

/** Seconds to wait before each retry; the first is replaced by Retry-After. */
export const BUSY_RETRY_SCHEDULE = [5, 10, 20, 30, 30, 30];

type FetchFn = (...args: Parameters<typeof fetch>) => Promise<Response>;

let retrying = 0;

function emit(name: string, detail?: unknown) {
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

/** True when this response is the sign-in check's "busy" refusal. */
export async function isAuthCheckBusy(res: Response): Promise<boolean> {
  if (res.status !== 503) return false;
  try {
    const body = await res.clone().json();
    return body?.error?.code === AUTH_CHECK_BUSY_CODE;
  } catch {
    return false;
  }
}

/** Seconds from a Retry-After header (delta-seconds only), else `fallback`. */
export function retryAfterSeconds(res: Response, fallback: number): number {
  const n = Number(res.headers.get("Retry-After"));
  return Number.isFinite(n) && n > 0 ? Math.min(n, 60) : fallback;
}

function canResend(args: Parameters<typeof fetch>): boolean {
  // A stream body is consumed by the first send and cannot be sent again.
  const body = args[1]?.body;
  return !(typeof ReadableStream !== "undefined" && body instanceof ReadableStream);
}

function aborted(args: Parameters<typeof fetch>): boolean {
  const input = args[0];
  const signal = args[1]?.signal ?? (input instanceof Request ? input.signal : undefined);
  return !!signal?.aborted;
}

function wait(seconds: number, args: Parameters<typeof fetch>): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, seconds * 1000);
    const input = args[0];
    const signal = args[1]?.signal ?? (input instanceof Request ? input.signal : undefined);
    signal?.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
  });
}

/**
 * `fetch`, retried while the server answers VEL-AUTH-CHECK-BUSY-503.
 * `sleep` is injectable for tests.
 */
export async function fetchWithBusyRetry(
  doFetch: FetchFn,
  args: Parameters<typeof fetch>,
  sleep: (seconds: number, args: Parameters<typeof fetch>) => Promise<void> = wait,
): Promise<Response> {
  // A Request object's body is read by the first send: keep a copy to resend.
  const original = args[0] instanceof Request ? args[0].clone() : null;
  let res = await doFetch(...args);
  if (!(await isAuthCheckBusy(res)) || !canResend(args)) return res;

  retrying += 1;
  try {
    for (let i = 0; i < BUSY_RETRY_SCHEDULE.length; i++) {
      const delay = i === 0 ? retryAfterSeconds(res, BUSY_RETRY_SCHEDULE[0]) : BUSY_RETRY_SCHEDULE[i];
      emit("velaris:busy", { retrying, nextInSeconds: delay });
      await sleep(delay, args);
      if (aborted(args)) return res;
      const next: Parameters<typeof fetch> = original ? [original.clone(), args[1]] : args;
      res = await doFetch(...next);
      if (!(await isAuthCheckBusy(res))) return res;
    }
    emit("velaris:busy-gaveup");
    return res;
  } finally {
    retrying -= 1;
    if (retrying === 0) emit("velaris:busy-clear");
  }
}
