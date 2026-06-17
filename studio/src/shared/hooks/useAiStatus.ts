/**
 * useAiStatus — polls /api/v1/sitemap/ai-status every 30 seconds.
 *
 * Shared module-level cache prevents multiple mounted components from
 * firing concurrent requests: all consumers receive the same result.
 */
import { useEffect, useState } from "react";
import { apiFetch } from "@shared/api/client";

interface AiStatus {
  ai_available: boolean;
  degraded_modules: string[];
}

const CACHE_TTL = 30_000; // 30 seconds
let _cache: { data: AiStatus; fetchedAt: number } | null = null;
let _inflight: Promise<AiStatus> | null = null;

async function fetchAiStatus(): Promise<AiStatus> {
  const now = Date.now();
  if (_cache && now - _cache.fetchedAt < CACHE_TTL) return _cache.data;
  if (_inflight) return _inflight;

  _inflight = apiFetch("/sitemap/ai-status")
    .then((r) => r.json())
    .then((data: AiStatus) => {
      _cache = { data, fetchedAt: Date.now() };
      return data;
    })
    .catch((): AiStatus => {
      const fallback: AiStatus = { ai_available: false, degraded_modules: [] };
      _cache = { data: fallback, fetchedAt: Date.now() };
      return fallback;
    })
    .finally(() => { _inflight = null; });

  return _inflight;
}

export function useAiStatus(): { available: boolean; loading: boolean } {
  const [status, setStatus] = useState<AiStatus | null>(
    _cache ? _cache.data : null
  );

  useEffect(() => {
    let cancelled = false;

    const check = () => {
      fetchAiStatus().then((s) => { if (!cancelled) setStatus(s); });
    };

    check();
    const timer = setInterval(check, CACHE_TTL);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  return {
    available: status?.ai_available ?? true, // optimistic until first response
    loading: status === null,
  };
}
