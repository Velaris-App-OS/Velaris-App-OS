import { describe, expect, it, vi } from "vitest";
import { AUTH_CHECK_BUSY_CODE, BUSY_RETRY_SCHEDULE, fetchWithBusyRetry } from "./busyRetry";

const busy = () =>
  new Response(JSON.stringify({ detail: "busy", error: { code: AUTH_CHECK_BUSY_CODE } }), {
    status: 503, headers: { "Retry-After": "5", "Content-Type": "application/json" },
  });
const ok = () => new Response(JSON.stringify({ id: 1 }), { status: 201 });
const noSleep = vi.fn(async () => {});

describe("fetchWithBusyRetry", () => {
  it("retries a busy refusal until it succeeds, resending the body", async () => {
    const doFetch = vi.fn().mockResolvedValueOnce(busy()).mockResolvedValueOnce(busy()).mockResolvedValueOnce(ok());
    const res = await fetchWithBusyRetry(doFetch, ["/api/v1/cases", { method: "POST", body: "{}" }], noSleep);
    expect(res.status).toBe(201);
    expect(doFetch).toHaveBeenCalledTimes(3);
    expect(doFetch.mock.calls[2][1]).toMatchObject({ method: "POST", body: "{}" });
  });

  it("waits Retry-After first, then the back-off schedule", async () => {
    const sleep = vi.fn(async (_s: number) => {});
    const doFetch = vi.fn().mockImplementation(async () => busy());
    const res = await fetchWithBusyRetry(doFetch, ["/x"], sleep);
    expect(res.status).toBe(503);   // gave up: the caller gets the last 503
    expect(sleep.mock.calls.map((c) => c[0] as number)).toEqual([5, ...BUSY_RETRY_SCHEDULE.slice(1)]);
    expect(doFetch).toHaveBeenCalledTimes(1 + BUSY_RETRY_SCHEDULE.length);
  });

  it("does not retry any other 503", async () => {
    const other = new Response(JSON.stringify({ error: { code: "VEL-DB-UNAVAILABLE-503" } }), { status: 503 });
    const doFetch = vi.fn().mockResolvedValue(other);
    const res = await fetchWithBusyRetry(doFetch, ["/x", { method: "POST", body: "{}" }], noSleep);
    expect(res.status).toBe(503);
    expect(doFetch).toHaveBeenCalledTimes(1);
  });

  it("stops when the caller aborts", async () => {
    const ctl = new AbortController();
    const doFetch = vi.fn().mockImplementation(async () => busy());
    const sleep = vi.fn(async () => { ctl.abort(); });
    await fetchWithBusyRetry(doFetch, ["/x", { signal: ctl.signal }], sleep);
    expect(doFetch).toHaveBeenCalledTimes(1);
  });
});
