/**
 * Save as draft (Form Builder v2 §5 step 7, 2026-10-29): a case's form keeps
 * the person's answers so far, unchecked, until they submit. One hook for Work
 * Center and Case Manager. The server keeps only the keys the person may
 * change and drops the draft when they submit.
 */
import React, { useEffect, useState } from "react";
import { discardFormDraft, getFormDraft, saveFormDraft, type FormDraft } from "@shared/api/client";

const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "earlier");

export function useFormDraft(caseId: string | undefined, formId: string | undefined) {
  const [draft, setDraft] = useState<FormDraft | null>(null);
  const [ready, setReady] = useState(!caseId || !formId);
  // Remounts the form when a restored draft is thrown away.
  const [run, setRun] = useState(0);

  useEffect(() => {
    if (!caseId || !formId) return;
    let live = true;
    setReady(false);
    getFormDraft(caseId, formId)
      .then(d => { if (live) setDraft(d); })
      .catch(() => { if (live) setDraft(null); })        // no draft is never a reason not to open the form
      .finally(() => { if (live) setReady(true); });
    return () => { live = false; };
  }, [caseId, formId]);

  if (!caseId || !formId) return { ready: true, values: {}, key: 0, notice: null, action: undefined };

  const notice = draft ? (
    <span>
      Your draft from {when(draft.updated_at)} is filled in.{" "}
      <button type="button" onClick={async () => {
        try { await discardFormDraft(caseId, formId); } catch { /* the next save replaces it anyway */ }
        setDraft(null); setRun(r => r + 1);
      }} style={{ background: "none", border: "none", padding: 0, color: "var(--accent)", cursor: "pointer", fontSize: 12 }}>
        Discard draft
      </button>
    </span>
  ) : null;

  return {
    ready,
    values: draft?.values ?? {},
    key: run,
    notice,
    action: {
      label: "Save draft",
      run: async (values: Record<string, any>) => {
        const saved = await saveFormDraft(caseId, formId, values);
        return `Draft saved ${when(saved.updated_at)} — only you can see it`;
      },
    },
  };
}
