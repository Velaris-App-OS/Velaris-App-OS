import { useCallback, useEffect, useState } from "react";

/* ═══════════════════════════════════════════════════════════════════
   useApi — generic async data fetcher with loading/error states
   ═══════════════════════════════════════════════════════════════════ */

interface UseApiState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

export function useApi<T>(fetcher: () => Promise<T>, deps: unknown[] = []): UseApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const refetch = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    fetcher()
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err.message || "Unknown error");
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [tick, ...deps]);

  return { data, loading, error, refetch };
}

/* ═══════════════════════════════════════════════════════════════════
   useInterval — poll at a fixed interval
   ═══════════════════════════════════════════════════════════════════ */

export function useInterval(callback: () => void, delayMs: number | null) {
  useEffect(() => {
    if (delayMs === null) return;
    const id = setInterval(callback, delayMs);
    return () => clearInterval(id);
  }, [callback, delayMs]);
}
export { useCommit } from "./useCommit";
export type { ChangeEntry, CommitSnapshot } from "./useCommit";
export { useBranchMode } from "./useBranchMode";
export type { BranchModeState } from "./useBranchMode";
export { useCurrentUserGroups } from "./useCurrentUserGroups";
