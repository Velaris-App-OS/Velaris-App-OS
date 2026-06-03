import { useState, useEffect, useCallback } from "react";
import {
  getBranch,
  patchBranchContent,
  submitBranchForReview,
  recallBranch,
} from "@shared/api/client";

export interface BranchModeState {
  branch:          any | null;
  loading:         boolean;
  saving:          boolean;
  error:           string | null;
  isBranchMode:    boolean;
  isLocked:        boolean;
  isReadOnly:      boolean;
  patchContent:    (content: Record<string, unknown>) => Promise<void>;
  submitForReview: (reviewerId: string) => Promise<void>;
  recall:          () => Promise<void>;
  refetch:         () => void;
}

export function useBranchMode(branchId: string | null): BranchModeState {
  const [branch,  setBranch]  = useState<any | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving,  setSaving]  = useState(false);
  const [error,   setError]   = useState<string | null>(null);
  const [tick,    setTick]    = useState(0);

  const refetch = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!branchId) { setBranch(null); return; }
    let cancelled = false;
    setLoading(true);
    getBranch(branchId)
      .then((b) => { if (!cancelled) { setBranch(b); setLoading(false); } })
      .catch((e) => { if (!cancelled) { setError(e.message); setLoading(false); } });
    return () => { cancelled = true; };
  }, [branchId, tick]);

  const patchContent = useCallback(async (content: Record<string, unknown>) => {
    if (!branchId) return;
    setSaving(true); setError(null);
    try {
      const updated = await patchBranchContent(branchId, content);
      setBranch(updated);
    } catch (e: any) {
      setError(e.message);
      throw e;
    } finally {
      setSaving(false);
    }
  }, [branchId]);

  const submitForReview = useCallback(async (reviewerId: string) => {
    if (!branchId) return;
    setSaving(true); setError(null);
    try {
      const updated = await submitBranchForReview(branchId, reviewerId);
      setBranch(updated);
    } catch (e: any) {
      setError(e.message);
      throw e;
    } finally {
      setSaving(false);
    }
  }, [branchId]);

  const recall = useCallback(async () => {
    if (!branchId) return;
    setSaving(true); setError(null);
    try {
      const updated = await recallBranch(branchId);
      setBranch(updated);
    } catch (e: any) {
      setError(e.message);
      throw e;
    } finally {
      setSaving(false);
    }
  }, [branchId]);

  return {
    branch,
    loading,
    saving,
    error,
    isBranchMode: branchId !== null,
    isLocked:     branch?.status === "pending_review",
    isReadOnly:   ["merged", "rejected", "closed"].includes(branch?.status ?? ""),
    patchContent,
    submitForReview,
    recall,
    refetch,
  };
}
