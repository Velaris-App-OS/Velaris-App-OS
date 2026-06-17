import React, { useState, useEffect, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useApi, useInterval } from "@shared/hooks";
import { useAuth } from "@/auth";
import {
  listQueues,
  listCases,
  getCase,
  getCaseType,
  changeCaseStatus,
  resolveCase,
  closeCase,
  transitionStage,
  getQueueItems,
  getQueueItemCount,
  getQueueStats,
  getMyAssignments,
  getMyWorkload,
  claimAssignment,
  completeAssignment,
  dismissAssignment,
  getAssignmentForm,
  getCaseVariables,
  submitForm,
  initiatePaymentRequest,
  listCasePaymentRequests,
  confirmDisbursement,
  listCaseDisbursements,
  markDisbursementSent,
  initiateIdentityVerification,
  listCaseVerifications,
  sendESignRequest,
  listCaseESignRequests,
  syncToCrm,
  listCaseCrmRecords,
  generateInvoice,
  listCaseInvoices,
  sendSms,
  listCaseSms,
  sendSlack,
  listCaseSlack,
  extractDocument,
  listCaseExtractions,
  routeToStorage,
  listCaseStorageRoutes,
  listBranches,
  getBranchDiff,
  postBranchReview,
} from "@shared/api/client";

function deployAuthHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}`, "Content-Type": "application/json" } : { "Content-Type": "application/json" };
}
import {
  Card,
  Button,
  Spinner,
  EmptyState,
  TimeAgo,
  Stat,
} from "@shared/components";
import FormRenderer from "../form-builder/FormRenderer";
import type { CaseAssignment, WorkQueueSummary, QueueStats } from "@shared/types";

/* ═══════════════════════════════════════════════════════════════════
   WorkCenter — work queues, assignments, and personal workload
   ═══════════════════════════════════════════════════════════════════ */

export default function WorkCenter() {
  const [view, setView] = useState<"cases" | "my-work" | "queues">("cases");
  const [selectedQueueId, setSelectedQueueId] = useState<string | null>(null);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-xl)" }}>
        {/* View toggle */}
        <div style={{ display: "flex", background: "var(--bg-card)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)" }}>
          {(["cases", "my-work", "queues"] as const).map((v) => (
            <button key={v} onClick={() => { setView(v); setSelectedQueueId(null); setSelectedCaseId(null); }} style={{
              padding: "8px 16px", fontSize: 12, fontWeight: 500, fontFamily: "var(--font-mono)",
              textTransform: "uppercase", letterSpacing: "0.04em", border: "none", cursor: "pointer",
              color: view === v ? "var(--accent)" : "var(--text-muted)",
              background: view === v ? "var(--accent-dim)" : "transparent",
              borderRadius: "var(--radius-sm)",
            }}>{v === "my-work" ? "My Assignments" : v === "cases" ? "Open Cases" : "Queues"}</button>
          ))}
        </div>
      </div>

      {/* Content */}
      {view === "cases" && <OpenCasesView selectedCaseId={selectedCaseId} onSelectCase={setSelectedCaseId} />}
      {view === "my-work" && <MyWorkView />}
      {view === "queues" && !selectedQueueId && <QueuesOverview onSelectQueue={setSelectedQueueId} />}
      {view === "queues" && selectedQueueId && (
        <QueueDetailView queueId={selectedQueueId} onBack={() => setSelectedQueueId(null)} />
      )}
    </div>
  );
}

/* ── Open Cases View ──────────────────────────────────────────── */

const CASE_STATUS_COLORS: Record<string, string> = {
  new: "#0d9488", open: "#3b82f6", in_progress: "#f59e0b", pending_external: "#8b5cf6",
  resolved: "#22c55e", closed: "#6b7280", reopened: "#f59e0b", cancelled: "#ef4444",
};

const CASE_PRIORITY_COLORS: Record<string, string> = {
  low: "#6b7280", medium: "#f59e0b", high: "#ef4444", critical: "#dc2626", blocker: "#7f1d1d",
};

const WC_PAGE_SIZE = 20;

function OpenCasesView({ selectedCaseId, onSelectCase }: { selectedCaseId: string | null; onSelectCase: (id: string) => void }) {
  const [statusFilter, setStatusFilter] = useState("open");
  const [page, setPage] = useState(1);
  const { data, loading, error, refetch } = useApi(
    () => listCases({ status: statusFilter || undefined, page, page_size: WC_PAGE_SIZE }),
    [statusFilter, page]
  );
  useInterval(refetch, 20000);

  const cases = (data as any)?.items ?? [];
  const total = (data as any)?.total ?? 0;
  const totalPages = Math.ceil(total / WC_PAGE_SIZE);

  const setFilterAndReset = (s: string) => { setStatusFilter(s); setPage(1); };

  return (
    <div style={{ display: "flex", height: "calc(100vh - 160px)", gap: "var(--space-lg)" }}>
      {/* Left: case list */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
        {/* Status filter chips + pagination top */}
        <div style={{ display: "flex", gap: 6, marginBottom: "var(--space-md)", flexWrap: "wrap", alignItems: "center", flexShrink: 0 }}>
          {["", "new", "open", "reopened", "pending_external"].map((s) => (
            <button key={s} onClick={() => setFilterAndReset(s)} style={{
              padding: "4px 12px", border: "1px solid var(--border-default)", borderRadius: 20,
              cursor: "pointer", fontSize: 12, fontFamily: "var(--font-mono)",
              background: statusFilter === s ? "var(--accent)" : "var(--bg-elevated)",
              color: statusFilter === s ? "#fff" : "var(--text-secondary)",
              fontWeight: statusFilter === s ? 600 : 400,
            }}>{s || "All Active"}</button>
          ))}
          <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              {total} case{total !== 1 ? "s" : ""}
            </span>
            {totalPages > 1 && <WcPager page={page} totalPages={totalPages} total={total} onChange={setPage} />}
          </div>
        </div>

        {loading && <div style={{ display: "flex", justifyContent: "center", padding: 32 }}><Spinner size={24} /></div>}
        {error && <p style={{ color: "var(--status-failed)", fontSize: 13 }}>Failed to load: {error}</p>}

        {!loading && !error && cases.length === 0 && (
          <EmptyState title="No cases" description="No cases match this filter." />
        )}

        <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-xs)" }}>
          {cases.map((c: any) => {
            const isSelected = selectedCaseId === c.id;
            const portalSource = (c.data?.source === "customer_portal");
            return (
              <div
                key={c.id}
                onClick={() => onSelectCase(c.id)}
                style={{
                  padding: "var(--space-md)", borderRadius: "var(--radius-md)",
                  border: `1px solid ${isSelected ? "var(--accent)" : "var(--border-subtle)"}`,
                  background: isSelected ? "color-mix(in srgb, var(--accent) 8%, transparent)" : "var(--bg-card)",
                  cursor: "pointer", transition: "all 0.1s",
                  borderLeft: `4px solid ${CASE_STATUS_COLORS[c.status] || "#0d9488"}`,
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
                      <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
                        {c.data?.subject || c.data?.title || c.id?.slice(0, 8)}
                      </span>
                      {portalSource && (
                        <span style={{ fontSize: 10, padding: "1px 6px", borderRadius: 10, background: "#ede9fe", color: "#7c3aed", fontWeight: 600 }}>
                          Portal
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                      {c.id.slice(0, 8)} · {c.current_stage_id || "no stage"} · by {c.created_by || "system"}
                    </div>
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4, flexShrink: 0 }}>
                    <span style={{
                      fontSize: 10, padding: "2px 8px", borderRadius: 10, fontWeight: 600, fontFamily: "var(--font-mono)",
                      background: (CASE_STATUS_COLORS[c.status] || "#0d9488") + "22",
                      color: CASE_STATUS_COLORS[c.status] || "#0d9488",
                    }}>{c.status}</span>
                    <span style={{
                      fontSize: 10, padding: "2px 8px", borderRadius: 10, fontFamily: "var(--font-mono)",
                      color: CASE_PRIORITY_COLORS[c.priority] || "#6b7280",
                      background: (CASE_PRIORITY_COLORS[c.priority] || "#6b7280") + "15",
                    }}>{c.priority}</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
        </div>

        {/* Bottom pagination */}
        {totalPages > 1 && (
          <div style={{ flexShrink: 0, paddingTop: "var(--space-sm)", display: "flex", justifyContent: "flex-end" }}>
            <WcPager page={page} totalPages={totalPages} total={total} onChange={setPage} />
          </div>
        )}
      </div>

      {/* Right: inline case detail */}
      {selectedCaseId && (
        <WorkCenterCaseDetail caseId={selectedCaseId} onClose={() => onSelectCase("")} onUpdate={refetch} />
      )}
    </div>
  );
}

/* ── Payment step panels ──────────────────────────────────────── */

const STATUS_COLOR: Record<string, string> = {
  pending: "#94a3b8", processing: "#3b82f6", succeeded: "#22c55e",
  failed: "#ef4444", refunded: "#a855f7", cancelled: "#6b7280",
  confirmed: "#22c55e", completed: "#22c55e",
};

function fmtMoney(cents: number, currency: string) {
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: currency.toUpperCase() }).format(cents / 100);
  } catch { return `${cents / 100} ${currency}`; }
}

/** Panel shown on a payment_request step — collect money from customer. */
function PaymentRequestPanel({ caseId, stepId, onComplete }: { caseId: string; stepId: string; onComplete: () => void }) {
  const [existing, setExisting] = useState<any | null>(null);
  const [amount, setAmount]     = useState("");
  const [currency, setCurrency] = useState("usd");
  const [description, setDescription] = useState("");
  const [email, setEmail]       = useState("");
  const [loading, setLoading]   = useState(false);
  const [msg, setMsg]           = useState<string | null>(null);
  const [copied, setCopied]     = useState(false);

  const loadExisting = useCallback(async () => {
    try {
      const rows = await listCasePaymentRequests(caseId);
      const match = rows.find((r: any) => r.step_id === stepId);
      if (match) setExisting(match);
    } catch { /* ignore */ }
  }, [caseId, stepId]);

  useEffect(() => { loadExisting(); }, [loadExisting]);

  const handleGenerate = async () => {
    const cents = Math.round(parseFloat(amount) * 100);
    if (!cents || cents <= 0) { setMsg("Enter a valid amount."); return; }
    setLoading(true); setMsg(null);
    try {
      const result = await initiatePaymentRequest(caseId, stepId, cents, currency, description || "Payment", email || undefined);
      setExisting(result);
    } catch (e: any) { setMsg(e.message || "Failed to generate payment link"); }
    finally { setLoading(false); }
  };

  const handleCopy = () => {
    if (existing?.checkout_url) {
      navigator.clipboard.writeText(existing.checkout_url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const statusColor = STATUS_COLOR[existing?.status] ?? "#94a3b8";

  return (
    <div style={{ background: "color-mix(in srgb, #3b82f6 6%, transparent)", border: "1px solid #3b82f644", borderRadius: 8, padding: "14px 16px", marginTop: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#3b82f6", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>
        💳 Collect Payment from Customer
      </div>

      {existing ? (
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
            <span style={{ fontSize: 15, fontWeight: 700 }}>{fmtMoney(existing.amount_cents, existing.currency)}</span>
            <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4, background: statusColor + "22", color: statusColor, fontWeight: 700, textTransform: "uppercase" }}>
              {existing.status}
            </span>
          </div>

          {existing.checkout_url && existing.status === "pending" && (
            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>Payment link for customer:</div>
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <input
                  readOnly value={existing.checkout_url}
                  style={{ flex: 1, fontSize: 11, padding: "6px 8px", border: "1px solid var(--border-subtle)", borderRadius: 4, background: "var(--bg-input)", color: "var(--text-primary)", fontFamily: "monospace" }}
                />
                <Button size="sm" variant="secondary" onClick={handleCopy}>{copied ? "Copied!" : "Copy"}</Button>
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6 }}>
                Share this link with the customer. The case advances automatically when payment is confirmed.
              </div>
            </div>
          )}

          {existing.status === "succeeded" && (
            <div style={{ fontSize: 12, color: "#22c55e", fontWeight: 600 }}>
              ✓ Payment received — step completed
            </div>
          )}

          {existing.status === "failed" && (
            <div>
              <div style={{ fontSize: 12, color: "#ef4444", marginBottom: 8 }}>✗ Payment failed</div>
              <Button size="sm" onClick={() => setExisting(null)}>Generate New Link</Button>
            </div>
          )}
        </div>
      ) : (
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 80px", gap: 6, marginBottom: 6 }}>
            <input placeholder="Amount (e.g. 250.00)" value={amount} onChange={e => setAmount(e.target.value)}
              style={{ padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)" }} />
            <select value={currency} onChange={e => setCurrency(e.target.value)}
              style={{ padding: "7px 8px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)" }}>
              {["usd","gbp","eur","inr","aud","cad","sgd"].map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
          <input placeholder="Description (e.g. Invoice #1234)" value={description} onChange={e => setDescription(e.target.value)}
            style={{ width: "100%", marginBottom: 6, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          <input placeholder="Customer email (optional)" value={email} onChange={e => setEmail(e.target.value)}
            style={{ width: "100%", marginBottom: 10, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <Button size="sm" disabled={loading || !amount} onClick={handleGenerate}>
            {loading ? "Generating…" : "Generate Payment Link"}
          </Button>
        </div>
      )}
    </div>
  );
}

/** Panel shown on a payment_disbursement step — pay money to customer. */
function PaymentDisbursementPanel({ caseId, stepId, onComplete }: { caseId: string; stepId: string; onComplete: () => void }) {
  const [existing, setExisting] = useState<any | null>(null);
  const [amount, setAmount]     = useState("");
  const [currency, setCurrency] = useState("usd");
  const [description, setDescription] = useState("");
  const [bankRef, setBankRef]   = useState("");
  const [notes, setNotes]       = useState("");
  const [loading, setLoading]   = useState(false);
  const [markingDone, setMarkingDone] = useState(false);
  const [msg, setMsg]           = useState<string | null>(null);
  const [confirm, setConfirm]   = useState(false);

  useEffect(() => {
    listCaseDisbursements(caseId).then((rows: any[]) => {
      const match = rows.find(r => r.step_id === stepId);
      if (match) setExisting(match);
    }).catch(() => {});
  }, [caseId, stepId]);

  const handleAuthorise = async () => {
    const cents = Math.round(parseFloat(amount) * 100);
    if (!cents || cents <= 0) { setMsg("Enter a valid amount."); return; }
    setLoading(true); setMsg(null);
    try {
      const result = await confirmDisbursement(caseId, stepId, cents, currency, description || "Disbursement", bankRef || undefined, notes || undefined);
      setExisting(result);
    } catch (e: any) { setMsg(e.message || "Failed to authorise disbursement"); }
    finally { setLoading(false); setConfirm(false); }
  };

  const handleMarkSent = async () => {
    if (!existing?.id) return;
    setMarkingDone(true); setMsg(null);
    try {
      const result = await markDisbursementSent(existing.id);
      setExisting((prev: any) => ({ ...prev, disbursement_executed: true, disbursement_executed_at: result.executed_at, status: "executed" }));
      onComplete();
    } catch (e: any) { setMsg(e.message || "Failed to mark as sent"); }
    finally { setMarkingDone(false); }
  };

  const statusColor = STATUS_COLOR[existing?.status] ?? "#94a3b8";

  return (
    <div style={{ background: "color-mix(in srgb, #22c55e 6%, transparent)", border: "1px solid #22c55e44", borderRadius: 8, padding: "14px 16px", marginTop: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#22c55e", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>
        💸 Pay to Customer
      </div>

      {existing ? (
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
            <span style={{ fontSize: 15, fontWeight: 700 }}>{fmtMoney(existing.amount_cents, existing.currency)}</span>
            <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4, background: statusColor + "22", color: statusColor, fontWeight: 700, textTransform: "uppercase" }}>
              {existing.status}
            </span>
          </div>
          {existing.description && <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 4 }}>{existing.description}</div>}
          {existing.bank_reference && <div style={{ fontSize: 11, fontFamily: "monospace", color: "var(--text-muted)" }}>Ref: {existing.bank_reference}</div>}
          {existing.confirmed_by && <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>Authorised by {existing.confirmed_by}</div>}

          {/* SD-3: Mark as Sent / Sent badge */}
          {existing.disbursement_executed ? (
            <div style={{ fontSize: 12, color: "#22c55e", fontWeight: 700, marginTop: 10, display: "flex", alignItems: "center", gap: 6 }}>
              ✓ Transfer sent · {existing.disbursement_executed_at ? new Date(existing.disbursement_executed_at).toLocaleString() : ""}
            </div>
          ) : (
            <div style={{ marginTop: 10 }}>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8, fontStyle: "italic" }}>
                ⚠️ This authorises the transfer only. Process the bank transfer in your banking system, then click Mark as Sent.
              </div>
              {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 6 }}>{msg}</div>}
              <Button size="sm" disabled={markingDone} onClick={handleMarkSent}>
                {markingDone ? "Marking…" : "✓ Mark as Sent"}
              </Button>
            </div>
          )}
        </div>
      ) : confirm ? (
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>
            Authorise payment of <strong>{fmtMoney(Math.round(parseFloat(amount || "0") * 100), currency)}</strong> to customer?
          </div>
          {/* SD-3: disclaimer — no money moves yet */}
          <div style={{ fontSize: 12, color: "#d97706", background: "#fffbeb", border: "1px solid #fde68a", borderRadius: 6, padding: "8px 12px", marginBottom: 12 }}>
            <strong>Important:</strong> This authorises the transfer only. Funds must be processed via your banking system or a connected payout provider. Click <em>Mark as Sent</em> after completing the transfer.
          </div>
          {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" disabled={loading} onClick={handleAuthorise}>{loading ? "Processing…" : "Yes, Authorise Payment"}</Button>
            <Button size="sm" variant="secondary" onClick={() => setConfirm(false)}>Cancel</Button>
          </div>
        </div>
      ) : (
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 80px", gap: 6, marginBottom: 6 }}>
            <input placeholder="Amount (e.g. 500.00)" value={amount} onChange={e => setAmount(e.target.value)}
              style={{ padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)" }} />
            <select value={currency} onChange={e => setCurrency(e.target.value)}
              style={{ padding: "7px 8px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)" }}>
              {["usd","gbp","eur","inr","aud","cad","sgd"].map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
          <input placeholder="Description (e.g. Claim settlement #45)" value={description} onChange={e => setDescription(e.target.value)}
            style={{ width: "100%", marginBottom: 6, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          <input placeholder="Bank reference / sort code (optional)" value={bankRef} onChange={e => setBankRef(e.target.value)}
            style={{ width: "100%", marginBottom: 6, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          <textarea placeholder="Notes (optional)" value={notes} onChange={e => setNotes(e.target.value)} rows={2}
            style={{ width: "100%", marginBottom: 10, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box", resize: "none" }} />
          {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <Button size="sm" disabled={!amount} onClick={() => { if (!amount) return; setMsg(null); setConfirm(true); }}>
            Authorise Payment
          </Button>
        </div>
      )}
    </div>
  );
}

/** Panel for identity_verify step — generate Onfido link, send to customer. */
function IdentityVerifyPanel({ caseId, stepId, onComplete }: { caseId: string; stepId: string; onComplete: () => void }) {
  const [existing, setExisting] = useState<any | null>(null);
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName]   = useState("");
  const [loading, setLoading]     = useState(false);
  const [msg, setMsg]             = useState<string | null>(null);
  const [copied, setCopied]       = useState(false);

  useEffect(() => {
    listCaseVerifications(caseId).then((rows: any[]) => {
      const m = rows.find(r => r.step_id === stepId);
      if (m) setExisting(m);
    }).catch(() => {});
  }, [caseId, stepId]);

  const handleGenerate = async () => {
    if (!firstName.trim()) { setMsg("Enter customer first name."); return; }
    setLoading(true); setMsg(null);
    try {
      const r = await initiateIdentityVerification(caseId, stepId, firstName, lastName);
      setExisting(r);
    } catch (e: any) { setMsg(e.message || "Failed"); }
    finally { setLoading(false); }
  };

  const statusColor: Record<string, string> = { pending: "#94a3b8", in_progress: "#3b82f6", complete: "#22c55e", withdrawn: "#6b7280" };
  const resultColor: Record<string, string> = { clear: "#22c55e", consider: "#f59e0b", unidentified: "#ef4444" };
  const sc = statusColor[existing?.status] ?? "#94a3b8";

  return (
    <div style={{ background: "color-mix(in srgb, #0d9488 6%, transparent)", border: "1px solid #0d948844", borderRadius: 8, padding: "14px 16px", marginTop: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#0d9488", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>
        🪪 Verify Customer Identity
      </div>
      {existing ? (
        <div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
            <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4, background: sc + "22", color: sc, fontWeight: 700, textTransform: "uppercase" }}>{existing.status}</span>
            {existing.result && <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4, background: (resultColor[existing.result] ?? "#94a3b8") + "22", color: resultColor[existing.result] ?? "#94a3b8", fontWeight: 700, textTransform: "uppercase" }}>{existing.result}</span>}
          </div>
          {existing.verification_url && existing.status === "pending" && (
            <div style={{ marginBottom: 8 }}>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>Verification link for customer:</div>
              <div style={{ display: "flex", gap: 6 }}>
                <input readOnly value={existing.verification_url} style={{ flex: 1, fontSize: 11, padding: "6px 8px", border: "1px solid var(--border-subtle)", borderRadius: 4, background: "var(--bg-input)", color: "var(--text-primary)", fontFamily: "monospace" }} />
                <button style={{ padding: "6px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, cursor: "pointer", fontSize: 11, background: "var(--bg-elevated)", color: "var(--text-secondary)" }}
                  onClick={() => { navigator.clipboard.writeText(existing.verification_url); setCopied(true); setTimeout(() => setCopied(false), 2000); }}>
                  {copied ? "Copied!" : "Copy"}
                </button>
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6 }}>Customer opens this link to complete document + selfie capture. Case advances automatically when Onfido confirms identity.</div>
            </div>
          )}
          {existing.status === "complete" && existing.result === "clear" && <div style={{ fontSize: 12, color: "#22c55e", fontWeight: 600 }}>✓ Identity verified — step completed</div>}
          {existing.status === "complete" && existing.result !== "clear" && <div style={{ fontSize: 12, color: "#f59e0b", fontWeight: 600 }}>⚠ Verification result: {existing.result} — manual review required</div>}
        </div>
      ) : (
        <div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginBottom: 6 }}>
            <input placeholder="First name" value={firstName} onChange={e => setFirstName(e.target.value)}
              style={{ padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)" }} />
            <input placeholder="Last name" value={lastName} onChange={e => setLastName(e.target.value)}
              style={{ padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)" }} />
          </div>
          {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <Button size="sm" disabled={loading || !firstName.trim()} onClick={handleGenerate}>
            {loading ? "Generating…" : "Generate Verification Link"}
          </Button>
        </div>
      )}
    </div>
  );
}

/** Panel for esign_request step — send DocuSign envelope, get signing link. */
function ESignPanel({ caseId, stepId, onComplete }: { caseId: string; stepId: string; onComplete: () => void }) {
  const [existing, setExisting]   = useState<any | null>(null);
  const [email, setEmail]         = useState("");
  const [name, setName]           = useState("");
  const [docName, setDocName]     = useState("");
  const [loading, setLoading]     = useState(false);
  const [msg, setMsg]             = useState<string | null>(null);
  const [copied, setCopied]       = useState(false);

  useEffect(() => {
    listCaseESignRequests(caseId).then((rows: any[]) => {
      const m = rows.find(r => r.step_id === stepId);
      if (m) setExisting(m);
    }).catch(() => {});
  }, [caseId, stepId]);

  const handleSend = async () => {
    if (!email.trim()) { setMsg("Enter signer email."); return; }
    setLoading(true); setMsg(null);
    try {
      const r = await sendESignRequest(caseId, stepId, email, name, docName || "Document for Signature");
      setExisting(r);
    } catch (e: any) { setMsg(e.message || "Failed"); }
    finally { setLoading(false); }
  };

  const statusColor: Record<string, string> = { pending: "#94a3b8", sent: "#3b82f6", delivered: "#0d9488", completed: "#22c55e", declined: "#ef4444", voided: "#6b7280" };
  const sc = statusColor[existing?.status] ?? "#94a3b8";

  return (
    <div style={{ background: "color-mix(in srgb, #f59e0b 6%, transparent)", border: "1px solid #f59e0b44", borderRadius: 8, padding: "14px 16px", marginTop: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#f59e0b", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>
        ✍️ Send for Signature
      </div>
      {existing ? (
        <div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
            <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4, background: sc + "22", color: sc, fontWeight: 700, textTransform: "uppercase" }}>{existing.status}</span>
            {existing.document_name && <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{existing.document_name}</span>}
          </div>
          {existing.signing_url && ["sent","delivered"].includes(existing.status) && (
            <div style={{ marginBottom: 8 }}>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>Signing link for {existing.signer_email}:</div>
              <div style={{ display: "flex", gap: 6 }}>
                <input readOnly value={existing.signing_url} style={{ flex: 1, fontSize: 11, padding: "6px 8px", border: "1px solid var(--border-subtle)", borderRadius: 4, background: "var(--bg-input)", color: "var(--text-primary)", fontFamily: "monospace" }} />
                <button style={{ padding: "6px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, cursor: "pointer", fontSize: 11, background: "var(--bg-elevated)", color: "var(--text-secondary)" }}
                  onClick={() => { navigator.clipboard.writeText(existing.signing_url); setCopied(true); setTimeout(() => setCopied(false), 2000); }}>
                  {copied ? "Copied!" : "Copy"}
                </button>
              </div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6 }}>Customer opens this link to sign the document. Case advances automatically when signing is complete.</div>
            </div>
          )}
          {existing.status === "completed" && <div style={{ fontSize: 12, color: "#22c55e", fontWeight: 600 }}>✓ Document signed — step completed</div>}
          {existing.status === "declined" && <div style={{ fontSize: 12, color: "#ef4444", fontWeight: 600 }}>✗ Signing declined</div>}
        </div>
      ) : (
        <div>
          <input placeholder="Signer email" value={email} onChange={e => setEmail(e.target.value)}
            style={{ width: "100%", marginBottom: 6, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          <input placeholder="Signer name" value={name} onChange={e => setName(e.target.value)}
            style={{ width: "100%", marginBottom: 6, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          <input placeholder="Document name (e.g. Loan Agreement)" value={docName} onChange={e => setDocName(e.target.value)}
            style={{ width: "100%", marginBottom: 10, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <Button size="sm" disabled={loading || !email.trim()} onClick={handleSend}>
            {loading ? "Sending…" : "Send for Signature"}
          </Button>
        </div>
      )}
    </div>
  );
}

/** Panel for crm_sync step — push case data to Salesforce. */
function CrmSyncPanel({ caseId, stepId, onComplete }: { caseId: string; stepId: string; onComplete: () => void }) {
  const [existing, setExisting] = useState<any | null>(null);
  const [form, setForm]         = useState({ firstName: "", lastName: "", email: "", subject: "", description: "" });
  const [loading, setLoading]   = useState(false);
  const [msg, setMsg]           = useState<string | null>(null);

  useEffect(() => {
    listCaseCrmRecords(caseId).then((rows: any[]) => {
      const m = rows.find(r => r.step_id === stepId);
      if (m) setExisting(m);
    }).catch(() => {});
  }, [caseId, stepId]);

  const handleSync = async () => {
    setLoading(true); setMsg(null);
    try {
      const r = await syncToCrm(caseId, stepId, form.firstName, form.lastName, form.email, form.subject, form.description);
      setExisting(r); onComplete();
    } catch (e: any) { setMsg(e.message || "Sync failed"); }
    finally { setLoading(false); }
  };

  const statusColor: Record<string, string> = { pending: "#94a3b8", synced: "#22c55e", failed: "#ef4444" };
  const sc = statusColor[existing?.status] ?? "#94a3b8";

  return (
    <div style={{ background: "color-mix(in srgb, #0ea5e9 6%, transparent)", border: "1px solid #0ea5e944", borderRadius: 8, padding: "14px 16px", marginTop: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#0ea5e9", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>☁️ Sync to Salesforce</div>
      {existing ? (
        <div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
            <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4, background: sc + "22", color: sc, fontWeight: 700, textTransform: "uppercase" }}>{existing.status}</span>
          </div>
          {existing.crm_record_url && <a href={existing.crm_record_url} target="_blank" rel="noreferrer" style={{ fontSize: 12, color: "var(--accent)" }}>View in Salesforce ↗</a>}
          {existing.status === "synced" && <div style={{ fontSize: 12, color: "#22c55e", fontWeight: 600, marginTop: 6 }}>✓ Synced to Salesforce — step completed</div>}
          {existing.error && <div style={{ fontSize: 12, color: "#ef4444", marginTop: 6 }}>{existing.error}</div>}
        </div>
      ) : (
        <div>
          {[["First name","firstName"],["Last name","lastName"],["Email","email"],["Case subject","subject"],["Description","description"]].map(([label, key]) => (
            <input key={key} placeholder={label} value={(form as any)[key]} onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))}
              style={{ width: "100%", marginBottom: 6, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          ))}
          {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <Button size="sm" disabled={loading} onClick={handleSync}>{loading ? "Syncing…" : "Sync to Salesforce"}</Button>
        </div>
      )}
    </div>
  );
}

/** Panel for invoice_generate step — create Xero invoice draft. */
function InvoicePanel({ caseId, stepId, onComplete }: { caseId: string; stepId: string; onComplete: () => void }) {
  const [existing, setExisting] = useState<any | null>(null);
  const [contactName, setContactName] = useState("");
  const [amount, setAmount]     = useState("");
  const [currency, setCurrency] = useState("usd");
  const [description, setDescription] = useState("");
  const [reference, setReference] = useState("");
  const [loading, setLoading]   = useState(false);
  const [msg, setMsg]           = useState<string | null>(null);

  useEffect(() => {
    listCaseInvoices(caseId).then((rows: any[]) => {
      const m = rows.find(r => r.step_id === stepId);
      if (m) setExisting(m);
    }).catch(() => {});
  }, [caseId, stepId]);

  const handleGenerate = async () => {
    const cents = Math.round(parseFloat(amount) * 100);
    if (!contactName.trim() || !cents) { setMsg("Enter contact name and amount."); return; }
    setLoading(true); setMsg(null);
    try {
      const r = await generateInvoice(caseId, stepId, contactName, description, cents, currency, [], reference);
      setExisting(r); onComplete();
    } catch (e: any) { setMsg(e.message || "Failed"); }
    finally { setLoading(false); }
  };

  const statusColor: Record<string, string> = { pending: "#94a3b8", draft: "#3b82f6", submitted: "#0d9488", authorised: "#22c55e", paid: "#22c55e", failed: "#ef4444" };
  const sc = statusColor[existing?.status] ?? "#94a3b8";

  return (
    <div style={{ background: "color-mix(in srgb, #10b981 6%, transparent)", border: "1px solid #10b98144", borderRadius: 8, padding: "14px 16px", marginTop: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#10b981", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>🧾 Generate Invoice (Xero)</div>
      {existing ? (
        <div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
            <span style={{ fontSize: 15, fontWeight: 700 }}>{existing.invoice_number ?? "Draft"}</span>
            <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4, background: sc + "22", color: sc, fontWeight: 700, textTransform: "uppercase" }}>{existing.status}</span>
          </div>
          {existing.contact_name && <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 4 }}>To: {existing.contact_name}</div>}
          {existing.invoice_url && <a href={existing.invoice_url} target="_blank" rel="noreferrer" style={{ fontSize: 12, color: "var(--accent)" }}>View in Xero ↗</a>}
          {existing.status === "draft" && <div style={{ fontSize: 12, color: "#3b82f6", fontWeight: 600, marginTop: 6 }}>✓ Invoice draft created — step completed</div>}
        </div>
      ) : (
        <div>
          <input placeholder="Contact / customer name" value={contactName} onChange={e => setContactName(e.target.value)}
            style={{ width: "100%", marginBottom: 6, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          <div style={{ display: "grid", gridTemplateColumns: "1fr 80px", gap: 6, marginBottom: 6 }}>
            <input placeholder="Amount (e.g. 1500.00)" value={amount} onChange={e => setAmount(e.target.value)}
              style={{ padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)" }} />
            <select value={currency} onChange={e => setCurrency(e.target.value)}
              style={{ padding: "7px 8px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)" }}>
              {["usd","gbp","eur","aud","nzd","sgd"].map(c => <option key={c}>{c}</option>)}
            </select>
          </div>
          <input placeholder="Description" value={description} onChange={e => setDescription(e.target.value)}
            style={{ width: "100%", marginBottom: 6, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          <input placeholder="Reference (optional)" value={reference} onChange={e => setReference(e.target.value)}
            style={{ width: "100%", marginBottom: 10, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <Button size="sm" disabled={loading || !contactName || !amount} onClick={handleGenerate}>{loading ? "Generating…" : "Generate Invoice"}</Button>
        </div>
      )}
    </div>
  );
}

/** Panel for sms_send step — send Twilio SMS. */
function SmsPanel({ caseId, stepId, onComplete }: { caseId: string; stepId: string; onComplete: () => void }) {
  const [existing, setExisting] = useState<any | null>(null);
  const [toNumber, setToNumber] = useState("");
  const [body, setBody]         = useState("");
  const [loading, setLoading]   = useState(false);
  const [msg, setMsg]           = useState<string | null>(null);

  useEffect(() => {
    listCaseSms(caseId).then((rows: any[]) => {
      const m = rows.find(r => r.step_id === stepId);
      if (m) setExisting(m);
    }).catch(() => {});
  }, [caseId, stepId]);

  const handleSend = async () => {
    if (!toNumber.trim() || !body.trim()) { setMsg("Enter phone number and message."); return; }
    setLoading(true); setMsg(null);
    try {
      const r = await sendSms(caseId, { step_id: stepId, to_number: toNumber, body });
      setExisting(r); onComplete();
    } catch (e: any) { setMsg(e.message || "Failed"); }
    finally { setLoading(false); }
  };

  const sc = existing?.status === "failed" ? "#ef4444" : existing?.status === "sent" || existing?.status === "delivered" ? "#22c55e" : "#94a3b8";

  return (
    <div style={{ background: "color-mix(in srgb, #3b82f6 6%, transparent)", border: "1px solid #3b82f644", borderRadius: 8, padding: "14px 16px", marginTop: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#3b82f6", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>📱 Send SMS (Twilio)</div>
      {existing ? (
        <div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}>To: {existing.to_number}</span>
            <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4, background: sc + "22", color: sc, fontWeight: 700, textTransform: "uppercase" }}>{existing.status}</span>
          </div>
          {existing.message_sid && <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>SID: {existing.message_sid}</div>}
          {existing.error && <div style={{ fontSize: 11, color: "#ef4444", marginTop: 4 }}>{existing.error}</div>}
        </div>
      ) : (
        <div>
          <input placeholder="To phone number (+1234567890)" value={toNumber} onChange={e => setToNumber(e.target.value)}
            style={{ width: "100%", marginBottom: 6, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          <textarea placeholder="Message body" value={body} onChange={e => setBody(e.target.value)} rows={3}
            style={{ width: "100%", marginBottom: 8, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box", resize: "vertical" }} />
          {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <Button size="sm" disabled={loading || !toNumber || !body} onClick={handleSend}>{loading ? "Sending…" : "Send SMS"}</Button>
        </div>
      )}
    </div>
  );
}

/** Panel for slack_notify step — post Slack message. */
function SlackPanel({ caseId, stepId, onComplete }: { caseId: string; stepId: string; onComplete: () => void }) {
  const [existing, setExisting] = useState<any | null>(null);
  const [channel, setChannel]   = useState("");
  const [message, setMessage]   = useState("");
  const [loading, setLoading]   = useState(false);
  const [msg, setMsg]           = useState<string | null>(null);

  useEffect(() => {
    listCaseSlack(caseId).then((rows: any[]) => {
      const m = rows.find(r => r.step_id === stepId);
      if (m) setExisting(m);
    }).catch(() => {});
  }, [caseId, stepId]);

  const handleSend = async () => {
    if (!message.trim()) { setMsg("Enter a message."); return; }
    setLoading(true); setMsg(null);
    try {
      const r = await sendSlack(caseId, { step_id: stepId, message, channel: channel || undefined });
      setExisting(r); onComplete();
    } catch (e: any) { setMsg(e.message || "Failed"); }
    finally { setLoading(false); }
  };

  const sc = existing?.status === "failed" ? "#ef4444" : existing?.status === "sent" ? "#22c55e" : "#94a3b8";

  return (
    <div style={{ background: "color-mix(in srgb, #8b5cf6 6%, transparent)", border: "1px solid #8b5cf644", borderRadius: 8, padding: "14px 16px", marginTop: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#8b5cf6", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>💬 Slack Notification</div>
      {existing ? (
        <div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}>{existing.channel ? `#${existing.channel}` : "Default channel"}</span>
            <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4, background: sc + "22", color: sc, fontWeight: 700, textTransform: "uppercase" }}>{existing.status}</span>
          </div>
          <div style={{ fontSize: 12, color: "var(--text-secondary)", fontStyle: "italic" }}>"{existing.message}"</div>
          {existing.error && <div style={{ fontSize: 11, color: "#ef4444", marginTop: 4 }}>{existing.error}</div>}
        </div>
      ) : (
        <div>
          <input placeholder="Channel (optional, e.g. #ops)" value={channel} onChange={e => setChannel(e.target.value)}
            style={{ width: "100%", marginBottom: 6, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          <textarea placeholder="Message" value={message} onChange={e => setMessage(e.target.value)} rows={3}
            style={{ width: "100%", marginBottom: 8, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box", resize: "vertical" }} />
          {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <Button size="sm" disabled={loading || !message} onClick={handleSend}>{loading ? "Sending…" : "Send to Slack"}</Button>
        </div>
      )}
    </div>
  );
}

/** Panel for doc_extract step — AI document field extraction. */
function DocExtractPanel({ caseId, stepId, onComplete }: { caseId: string; stepId: string; onComplete: () => void }) {
  const [existing, setExisting] = useState<any | null>(null);
  const [sourceUrl, setSourceUrl] = useState("");
  const [docName, setDocName]   = useState("");
  const [loading, setLoading]   = useState(false);
  const [msg, setMsg]           = useState<string | null>(null);

  useEffect(() => {
    listCaseExtractions(caseId).then((rows: any[]) => {
      const m = rows.find(r => r.step_id === stepId);
      if (m) setExisting(m);
    }).catch(() => {});
  }, [caseId, stepId]);

  const handleExtract = async () => {
    if (!sourceUrl.trim()) { setMsg("Enter a document URL."); return; }
    setLoading(true); setMsg(null);
    try {
      const r = await extractDocument(caseId, { step_id: stepId, source_url: sourceUrl, document_name: docName || undefined });
      setExisting(r); onComplete();
    } catch (e: any) { setMsg(e.message || "Extraction failed"); }
    finally { setLoading(false); }
  };

  const sc = existing?.status === "failed" ? "#ef4444" : existing?.status === "completed" ? "#22c55e" : "#94a3b8";

  return (
    <div style={{ background: "color-mix(in srgb, #f59e0b 6%, transparent)", border: "1px solid #f59e0b44", borderRadius: 8, padding: "14px 16px", marginTop: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#f59e0b", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>🔍 Extract Document Fields (AI)</div>
      {existing ? (
        <div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}>{existing.document_name ?? "Document"}</span>
            <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4, background: sc + "22", color: sc, fontWeight: 700, textTransform: "uppercase" }}>{existing.status}</span>
            {existing.confidence != null && <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{Math.round(existing.confidence * 100)}% confidence</span>}
          </div>
          {existing.status === "completed" && Object.keys(existing.extracted_fields || {}).length > 0 && (
            <div style={{ background: "var(--bg-elevated)", borderRadius: 6, padding: "8px 10px", fontSize: 11 }}>
              {Object.entries(existing.extracted_fields).map(([k, v]) => (
                <div key={k} style={{ display: "flex", gap: 8, marginBottom: 3 }}>
                  <span style={{ color: "var(--text-muted)", minWidth: 120, fontWeight: 600 }}>{k}</span>
                  <span style={{ color: "var(--text-primary)" }}>{String(v)}</span>
                </div>
              ))}
            </div>
          )}
          {existing.error && <div style={{ fontSize: 11, color: "#ef4444", marginTop: 6 }}>{existing.error}</div>}
        </div>
      ) : (
        <div>
          <input placeholder="Document URL (presigned or internal)" value={sourceUrl} onChange={e => setSourceUrl(e.target.value)}
            style={{ width: "100%", marginBottom: 6, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          <input placeholder="Document name (optional)" value={docName} onChange={e => setDocName(e.target.value)}
            style={{ width: "100%", marginBottom: 8, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <Button size="sm" disabled={loading || !sourceUrl} onClick={handleExtract}>{loading ? "Extracting…" : "Extract Fields"}</Button>
        </div>
      )}
    </div>
  );
}

/** Panel for doc_store step — route document to cloud storage and get upload URL. */
function DocStorePanel({ caseId, stepId, onComplete }: { caseId: string; stepId: string; onComplete: () => void }) {
  const [existing, setExisting] = useState<any | null>(null);
  const [docName, setDocName]   = useState("");
  const [copied, setCopied]     = useState(false);
  const [loading, setLoading]   = useState(false);
  const [msg, setMsg]           = useState<string | null>(null);

  useEffect(() => {
    listCaseStorageRoutes(caseId).then((rows: any[]) => {
      const m = rows.find(r => r.step_id === stepId);
      if (m) setExisting(m);
    }).catch(() => {});
  }, [caseId, stepId]);

  const handleRoute = async () => {
    if (!docName.trim()) { setMsg("Enter a document name."); return; }
    setLoading(true); setMsg(null);
    try {
      const r = await routeToStorage(caseId, { step_id: stepId, document_name: docName });
      setExisting(r); onComplete();
    } catch (e: any) { setMsg(e.message || "Failed"); }
    finally { setLoading(false); }
  };

  const sc = existing?.status === "failed" ? "#ef4444" : existing?.status === "uploaded" ? "#22c55e" : "#3b82f6";

  return (
    <div style={{ background: "color-mix(in srgb, #0d9488 6%, transparent)", border: "1px solid #0d948844", borderRadius: 8, padding: "14px 16px", marginTop: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#0d9488", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>☁️ Route to Cloud Storage (S3)</div>
      {existing ? (
        <div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}>{existing.document_name}</span>
            <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4, background: sc + "22", color: sc, fontWeight: 700, textTransform: "uppercase" }}>{existing.status}</span>
          </div>
          {existing.object_key && <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>Key: {existing.object_key}</div>}
          {existing.presigned_url && (
            <button
              onClick={() => { navigator.clipboard.writeText(existing.presigned_url); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
              style={{ fontSize: 11, color: "#0d9488", background: "none", border: "1px solid #0d948844", borderRadius: 4, padding: "4px 10px", cursor: "pointer" }}
            >{copied ? "Copied!" : "Copy Upload URL"}</button>
          )}
          {existing.error && <div style={{ fontSize: 11, color: "#ef4444", marginTop: 6 }}>{existing.error}</div>}
        </div>
      ) : (
        <div>
          <input placeholder="Document name (e.g. passport.pdf)" value={docName} onChange={e => setDocName(e.target.value)}
            style={{ width: "100%", marginBottom: 8, padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" }} />
          {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}
          <Button size="sm" disabled={loading || !docName} onClick={handleRoute}>{loading ? "Routing…" : "Get Upload URL"}</Button>
        </div>
      )}
    </div>
  );
}

/* ── Work Center inline case detail ───────────────────────────── */

function WorkCenterCaseDetail({ caseId, onClose, onUpdate }: { caseId: string; onClose: () => void; onUpdate: () => void }) {
  const [tab, setTab] = useState<"action" | "data">("action");
  const { data: caseData, refetch } = useApi(() => getCase(caseId), [caseId]);
  const { data: caseTypeData } = useApi(
    () => caseData?.case_type_id ? getCaseType(caseData.case_type_id) : Promise.resolve(null),
    [caseData?.case_type_id]
  );

  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function doAction(fn: () => Promise<any>, label: string) {
    setBusy(label);
    setErr(null);
    try { await fn(); refetch(); onUpdate(); }
    catch (e: any) { setErr(e.message || "Failed"); }
    finally { setBusy(null); }
  }

  if (!caseData) return (
    <div style={{ width: 380, border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", display: "flex", justifyContent: "center", padding: 32 }}>
      <Spinner size={24} />
    </div>
  );

  const c = caseData as any;
  const stages: any[] = ((caseTypeData as any)?.definition_json?.stages ?? [])
    .slice().sort((a: any, b: any) => (a.order ?? 0) - (b.order ?? 0));
  const currentIdx = stages.findIndex((s: any) => s.id === c.current_stage_id);
  const nextStage = stages[currentIdx + 1];
  const isActive = ["new", "open", "reopened"].includes(c.status);
  const isPortal = c.data?.source === "customer_portal";

  return (
    <div style={{
      width: 380, border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)",
      background: "var(--bg-panel)", display: "flex", flexDirection: "column", overflow: "hidden",
    }}>
      {/* Header */}
      <div style={{ padding: "var(--space-md)", borderBottom: "1px solid var(--border-subtle)", background: "var(--bg-card)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <div style={{ fontWeight: 700, fontSize: 14, color: "var(--text-primary)" }}>
              {c.data?.subject || c.data?.title || "Case Detail"}
            </div>
            <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginTop: 2 }}>
              {c.id.slice(0, 12)} · {c.status}
              {isPortal && <span style={{ marginLeft: 6, color: "#7c3aed" }}>· Portal submission</span>}
            </div>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 16, color: "var(--text-muted)" }}>✕</button>
        </div>

        {isPortal && c.data?.description && (
          <div style={{ marginTop: 10, padding: 10, background: "#f5f3ff", borderRadius: "var(--radius-sm)", fontSize: 12, color: "#374151" }}>
            <strong>Customer:</strong> {c.portal_submitter_name || c.created_by?.replace("portal:", "")}<br />
            <strong>Request:</strong> {c.data.description}
          </div>
        )}
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", borderBottom: "1px solid var(--border-subtle)" }}>
        {(["action", "data"] as const).map(t => (
          <button key={t} onClick={() => setTab(t)} style={{
            flex: 1, padding: "8px 0", fontSize: 11, fontWeight: 500, fontFamily: "var(--font-mono)",
            textTransform: "uppercase", letterSpacing: "0.04em", border: "none", cursor: "pointer",
            color: tab === t ? "var(--accent)" : "var(--text-muted)", background: "transparent",
            borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent",
          }}>{t === "action" ? "Lifecycle" : "Data"}</button>
        ))}
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: "var(--space-md)" }}>
        {err && <div style={{ color: "var(--status-failed)", fontSize: 12, marginBottom: 10 }}>{err}</div>}

        {tab === "action" && (
          <div>
            {/* Stage pipeline */}
            {stages.length > 0 && (
              <div style={{ marginBottom: "var(--space-lg)" }}>
                <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>
                  Lifecycle Stages
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {stages.map((stage: any, idx: number) => {
                    const isDone = currentIdx > idx;
                    const isCurrent = currentIdx === idx;
                    const isNext = idx === currentIdx + 1;
                    return (
                      <div key={stage.id} style={{
                        display: "flex", alignItems: "center", gap: 8,
                        padding: "6px 10px", borderRadius: "var(--radius-sm)",
                        border: `1px solid ${isCurrent ? "var(--accent)" : isDone ? "var(--status-completed)" : "var(--border-subtle)"}`,
                        background: isCurrent ? "color-mix(in srgb, var(--accent) 8%, transparent)" : isDone ? "color-mix(in srgb, var(--status-completed) 5%, transparent)" : "transparent",
                        opacity: !isDone && !isCurrent && !isNext ? 0.5 : 1,
                      }}>
                        <div style={{
                          width: 20, height: 20, borderRadius: "50%", flexShrink: 0,
                          display: "flex", alignItems: "center", justifyContent: "center",
                          background: isCurrent ? "var(--accent)" : isDone ? "var(--status-completed)" : "var(--bg-elevated)",
                          fontSize: 10, fontWeight: 700, color: isCurrent || isDone ? "#fff" : "var(--text-muted)",
                        }}>
                          {isDone ? "✓" : idx + 1}
                        </div>
                        <span style={{ flex: 1, fontSize: 12, fontWeight: isCurrent ? 600 : 400, color: isCurrent ? "var(--accent)" : "var(--text-primary)" }}>
                          {stage.name}
                        </span>
                        {isNext && isActive && (
                          <Button size="sm" disabled={!!busy} onClick={() => doAction(() => transitionStage(caseId, stage.id), stage.id)}>
                            {busy === stage.id ? "…" : "→"}
                          </Button>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Payment step panels — shown for steps in the current stage */}
            {isActive && stages[currentIdx]?.steps?.some((s: any) =>
              ["payment_request","payment_disbursement","identity_verify","esign_request","crm_sync","invoice_generate","sms_send","slack_notify","doc_extract","doc_store"].includes(s.step_type)
            ) && (
              <div style={{ marginBottom: "var(--space-lg)" }}>
                <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>
                  Action Steps
                </div>
                {stages[currentIdx].steps.map((step: any) => {
                  if (step.step_type === "payment_request")
                    return <PaymentRequestPanel key={step.id} caseId={caseId} stepId={step.id} onComplete={() => { refetch(); onUpdate(); }} />;
                  if (step.step_type === "payment_disbursement")
                    return <PaymentDisbursementPanel key={step.id} caseId={caseId} stepId={step.id} onComplete={() => { refetch(); onUpdate(); }} />;
                  if (step.step_type === "identity_verify")
                    return <IdentityVerifyPanel key={step.id} caseId={caseId} stepId={step.id} onComplete={() => { refetch(); onUpdate(); }} />;
                  if (step.step_type === "esign_request")
                    return <ESignPanel key={step.id} caseId={caseId} stepId={step.id} onComplete={() => { refetch(); onUpdate(); }} />;
                  if (step.step_type === "crm_sync")
                    return <CrmSyncPanel key={step.id} caseId={caseId} stepId={step.id} onComplete={() => { refetch(); onUpdate(); }} />;
                  if (step.step_type === "invoice_generate")
                    return <InvoicePanel key={step.id} caseId={caseId} stepId={step.id} onComplete={() => { refetch(); onUpdate(); }} />;
                  if (step.step_type === "sms_send")
                    return <SmsPanel key={step.id} caseId={caseId} stepId={step.id} onComplete={() => { refetch(); onUpdate(); }} />;
                  if (step.step_type === "slack_notify")
                    return <SlackPanel key={step.id} caseId={caseId} stepId={step.id} onComplete={() => { refetch(); onUpdate(); }} />;
                  if (step.step_type === "doc_extract")
                    return <DocExtractPanel key={step.id} caseId={caseId} stepId={step.id} onComplete={() => { refetch(); onUpdate(); }} />;
                  if (step.step_type === "doc_store")
                    return <DocStorePanel key={step.id} caseId={caseId} stepId={step.id} onComplete={() => { refetch(); onUpdate(); }} />;
                  return null;
                })}
              </div>
            )}

            {/* Status actions */}
            <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 10 }}>
              Case Actions
            </div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {c.status === "new" && (
                <Button size="sm" disabled={!!busy} onClick={() => doAction(() => changeCaseStatus(caseId, "open"), "open")}>
                  {busy === "open" ? "…" : "▶ Open Case"}
                </Button>
              )}
              {["open", "reopened"].includes(c.status) && nextStage && (
                <Button size="sm" variant="secondary" disabled={!!busy} onClick={() => doAction(() => transitionStage(caseId, nextStage.id), "next")}>
                  {busy === "next" ? "…" : `→ ${nextStage.name}`}
                </Button>
              )}
              {["open", "reopened"].includes(c.status) && (
                <Button size="sm" disabled={!!busy} onClick={() => doAction(() => resolveCase(caseId), "resolve")}>
                  {busy === "resolve" ? "…" : "✓ Resolve"}
                </Button>
              )}
              {c.status === "resolved" && (
                <Button size="sm" variant="secondary" disabled={!!busy} onClick={() => doAction(() => closeCase(caseId), "close")}>
                  {busy === "close" ? "…" : "Close"}
                </Button>
              )}
            </div>
          </div>
        )}

        {tab === "data" && (
          <pre style={{
            fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-secondary)",
            background: "var(--bg-input)", padding: "var(--space-md)", borderRadius: "var(--radius-sm)",
            overflow: "auto", whiteSpace: "pre-wrap", lineHeight: 1.6, margin: 0,
          }}>
            {JSON.stringify(c.data, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}

/* ── My Work View ─────────────────────────────────────────────── */

function MyWorkView() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const { data: workload, refetch: refetchWorkload } = useApi(getMyWorkload);
  const { data: assignments, loading, refetch } = useApi(getMyAssignments);

  // Deployment approvals — identifiers that may appear as assigned_to_user_id
  // (user-directory uses username as user_id, auth JWT uses UUID as user_id)
  const myIds = new Set([user?.user_id, user?.username, user?.email].filter(Boolean) as string[]);

  const [deployApprovals, setDeployApprovals] = useState<any[]>([]);
  const [deployLoading, setDeployLoading] = useState(false);
  const [deployMsg, setDeployMsg] = useState<string | null>(null);

  const [reviewTasks, setReviewTasks] = useState<any[]>([]);
  const [reviewTasksLoading, setReviewTasksLoading] = useState(false);

  const fetchDeployApprovals = useCallback(async () => {
    setDeployLoading(true);
    try {
      const r = await fetch("/api/v1/deploy/my-approvals", { headers: deployAuthHdr() });
      if (r.ok) setDeployApprovals(await r.json());
    } finally { setDeployLoading(false); }
  }, []);

  const fetchReviewTasks = useCallback(async () => {
    if (!user?.user_id) return;
    setReviewTasksLoading(true);
    try {
      const result = await listBranches({ assigned_reviewer_id: user.user_id, status: "pending_review" });
      setReviewTasks(result.branches ?? []);
    } catch { /* ignore */ } finally { setReviewTasksLoading(false); }
  }, [user?.user_id]);

  useEffect(() => { fetchDeployApprovals(); }, [fetchDeployApprovals]);
  useEffect(() => { fetchReviewTasks(); }, [fetchReviewTasks]);
  useInterval(() => { refetch(); refetchWorkload(); fetchDeployApprovals(); fetchReviewTasks(); }, 30000);

  const approveRun = async (id: string) => {
    setDeployMsg(null);
    const r = await fetch(`/api/v1/deploy/runs/${id}/approve`, { method: "POST", headers: deployAuthHdr(), body: "{}" });
    if (r.ok) { setDeployMsg("Approved and deployed."); fetchDeployApprovals(); }
    else setDeployMsg("Approval failed.");
    setTimeout(() => setDeployMsg(null), 4000);
  };

  const rejectRun = async (id: string, reason: string) => {
    if (!reason.trim()) { setDeployMsg("Enter a rejection reason."); return; }
    const r = await fetch(`/api/v1/deploy/runs/${id}/reject`, { method: "POST", headers: deployAuthHdr(), body: JSON.stringify({ reason }) });
    if (r.ok) { setDeployMsg("Run rejected."); fetchDeployApprovals(); }
    else setDeployMsg("Rejection failed.");
    setTimeout(() => setDeployMsg(null), 4000);
  };

  const items = assignments ?? [];
  const activeCount = workload?.active_count ?? 0;

  const RISK_COLOR: Record<string, string> = {
    low: "#22c55e", medium: "#f59e0b", high: "#ef4444", critical: "#7c3aed",
  };

  return (
    <div>
      {/* Stats */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: "var(--space-md)", marginBottom: "var(--space-xl)" }}>
        <Card><Stat label="Case Assignments" value={activeCount} /></Card>
        <Card><Stat label="Review Tasks" value={reviewTasks.length} /></Card>
        <Card><Stat label="Deploy Approvals" value={deployApprovals.filter(r => myIds.has(r.risk_summary?.assigned_to_user_id)).length} /></Card>
        <Card><Stat label="Due Today" value={items.filter((a) => a.due_at && isToday(a.due_at)).length} /></Card>
        <Card><Stat label="Overdue" value={items.filter((a) => a.due_at && isPast(a.due_at)).length} /></Card>
      </div>

      {/* ── Deployment Approvals ── */}
      {(deployLoading || deployApprovals.length > 0) && (
        <div style={{ marginBottom: "var(--space-xl)" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "var(--space-md)" }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)" }}>
              Deployment Approvals
              {deployApprovals.length > 0 && (
                <span style={{ marginLeft: 8, fontSize: 11, padding: "2px 8px", borderRadius: 10, background: "#f59e0b22", color: "#f59e0b", fontWeight: 700 }}>
                  {deployApprovals.length}
                </span>
              )}
            </div>
            <Button size="sm" variant="ghost" onClick={() => navigate("/deploy")}>Open HxDeploy →</Button>
          </div>

          {deployMsg && (
            <div style={{ fontSize: 12, padding: "8px 12px", borderRadius: 4, marginBottom: 10, background: "var(--bg-elevated)", color: "var(--text-primary)" }}>
              {deployMsg}
            </div>
          )}

          {deployLoading && <Spinner size={20} />}

          {!deployLoading && deployApprovals.map((run: any) => (
            <DeployApprovalRow key={run.id} run={run} riskColor={RISK_COLOR} onApprove={approveRun} onReject={rejectRun} />
          ))}
        </div>
      )}

      {/* ── Review Tasks ── */}
      <div style={{ marginBottom: "var(--space-xl)" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "var(--space-md)" }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)" }}>
            Review Tasks
            {reviewTasks.length > 0 && (
              <span style={{ marginLeft: 8, fontSize: 11, padding: "2px 8px", borderRadius: 10, background: "#f59e0b22", color: "#f59e0b", fontWeight: 700 }}>
                {reviewTasks.length}
              </span>
            )}
          </div>
          <Button size="sm" variant="ghost" onClick={() => navigate("/hxbranch")}>Open HxBranch →</Button>
        </div>

        {reviewTasksLoading && <Spinner size={20} />}

        {!reviewTasksLoading && reviewTasks.length === 0 && (
          <EmptyState
            title="No review tasks"
            description="Branches assigned to you for review will appear here."
          />
        )}

        {!reviewTasksLoading && reviewTasks.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {reviewTasks.map((branch) => (
              <ReviewTaskRow key={branch.id} branch={branch} onAction={fetchReviewTasks} />
            ))}
          </div>
        )}
      </div>

      {/* ── Case Assignments ── */}
      <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)", marginBottom: "var(--space-md)" }}>Case Assignments</div>
      {loading && <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}><Spinner size={28} /></div>}

      {!loading && items.length === 0 && (
        <EmptyState
          title="No case assignments"
          description="Assignments appear here when a case step is routed to you."
        />
      )}

      {!loading && items.length > 0 && (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
            {items.map((a) => (
              <AssignmentRow key={a.id} assignment={a} allowDirectComplete
                onComplete={async () => { await completeAssignment(a.id); refetch(); refetchWorkload(); }}
                onDismiss={async () => { await dismissAssignment(a.id); refetch(); refetchWorkload(); }}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

/* ── Review Task Row ──────────────────────────────────────────── */

const ARTIFACT_LABEL: Record<string, string> = {
  case_type: "Case Type", form: "Form", rule: "Rule",
  integration: "Connector", escalation: "Escalation",
};

const BRANCH_STATUS_COLOR: Record<string, string> = {
  open: "#3b82f6", pending_review: "#f59e0b", approved: "#22c55e",
  merged: "#0d9488", rejected: "#ef4444", closed: "#6b7280",
};

function ReviewTaskRow({ branch, onAction }: { branch: any; onAction: () => void }) {
  const [diff, setDiff] = useState<any>(null);
  const [expanded, setExpanded] = useState(false);
  const [decision, setDecision] = useState<"approved" | "rejected" | "changes_requested" | null>(null);
  const [comments, setComments] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const loadDiff = async () => {
    if (diff) { setExpanded(e => !e); return; }
    try {
      const d = await getBranchDiff(branch.id);
      setDiff(d);
      setExpanded(true);
    } catch { setExpanded(e => !e); }
  };

  const submitReview = async () => {
    if (!decision) return;
    if ((decision === "rejected" || decision === "changes_requested") && !comments.trim()) {
      setMsg("Comments are required when rejecting or requesting changes.");
      return;
    }
    setBusy(true); setMsg(null);
    try {
      await postBranchReview(branch.id, decision, comments || undefined);
      onAction();
    } catch (e: any) { setMsg(e.message || "Failed to submit review"); }
    finally { setBusy(false); }
  };

  const totalChanges = diff?.diff_from_base?.total_changes ?? null;
  const conflict = diff?.conflict ?? false;

  return (
    <div style={{
      background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
      borderLeft: `3px solid #f59e0b`, borderRadius: "var(--radius-sm)",
    }}>
      {/* Header row */}
      <div style={{ display: "flex", alignItems: "flex-start", gap: "var(--space-md)", padding: "var(--space-md) var(--space-lg)" }}>
        <div style={{ flex: 1 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
            <span style={{ fontSize: 11, padding: "2px 8px", borderRadius: 4, fontWeight: 700, background: "#f59e0b22", color: "#f59e0b", fontFamily: "var(--font-mono)", textTransform: "uppercase" as const }}>
              {ARTIFACT_LABEL[branch.artifact_type] ?? branch.artifact_type ?? "Branch"}
            </span>
            {conflict && (
              <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 4, fontWeight: 700, background: "#ef444422", color: "#ef4444" }}>
                Conflict
              </span>
            )}
            <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)" }}>{branch.name}</span>
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
            by {branch.owner_id ?? branch.created_by} ·{" "}
            {branch.created_at ? new Date(branch.created_at).toLocaleDateString() : ""}
            {branch.description && <span> · {branch.description}</span>}
          </div>
          {totalChanges !== null && (
            <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 3 }}>
              {totalChanges} field{totalChanges !== 1 ? "s" : ""} changed
            </div>
          )}
        </div>
        <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
          <Button size="sm" variant="ghost" onClick={loadDiff}>
            {expanded ? "Hide Diff" : "View Diff"}
          </Button>
        </div>
      </div>

      {/* Diff panel */}
      {expanded && diff && (
        <div style={{ margin: "0 var(--space-lg)", padding: "var(--space-md)", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", marginBottom: "var(--space-md)", maxHeight: 280, overflowY: "auto" }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8 }}>
            Changes from base ({diff.diff_from_base?.total_changes ?? 0} fields)
          </div>
          {(diff.diff_from_base?.changed_fields ?? []).length === 0 ? (
            <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic" }}>No field-level changes detected.</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {(diff.diff_from_base.changed_fields as any[]).slice(0, 20).map((cf: any) => (
                <div key={cf.field} style={{ fontSize: 11, display: "flex", gap: 8, alignItems: "flex-start" }}>
                  <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", minWidth: 120, flexShrink: 0 }}>{cf.field}</span>
                  <span style={{ color: "#ef4444", fontFamily: "var(--font-mono)", flex: 1, wordBreak: "break-all" as const }}>
                    {JSON.stringify(cf.base)?.slice(0, 80)}
                  </span>
                  <span style={{ color: "var(--text-muted)", fontSize: 10 }}>→</span>
                  <span style={{ color: "#22c55e", fontFamily: "var(--font-mono)", flex: 1, wordBreak: "break-all" as const }}>
                    {JSON.stringify(cf.branch)?.slice(0, 80)}
                  </span>
                </div>
              ))}
              {diff.diff_from_base.changed_fields.length > 20 && (
                <div style={{ fontSize: 11, color: "var(--text-muted)", fontStyle: "italic" }}>
                  … and {diff.diff_from_base.changed_fields.length - 20} more
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Review actions */}
      <div style={{ padding: "0 var(--space-lg) var(--space-md)" }}>
        {msg && <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 8 }}>{msg}</div>}

        {decision ? (
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)", marginBottom: 6 }}>
              {decision === "approved" ? "Approve branch" : decision === "rejected" ? "Reject branch" : "Request changes"}
              {(decision === "rejected" || decision === "changes_requested") && <span style={{ color: "#ef4444" }}> *</span>}
            </div>
            <textarea
              placeholder={decision === "approved" ? "Comments (optional)" : "Explain what needs to change… (required)"}
              value={comments}
              onChange={e => setComments(e.target.value)}
              rows={3}
              style={{ width: "100%", boxSizing: "border-box", padding: "7px 10px", border: "1px solid var(--border-subtle)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", resize: "vertical", marginBottom: 8, fontFamily: "var(--font-mono)" }}
            />
            <div style={{ display: "flex", gap: 6 }}>
              <Button size="sm" disabled={busy} onClick={submitReview}>
                {busy ? "Submitting…" : decision === "approved" ? "Confirm Approve" : decision === "rejected" ? "Confirm Reject" : "Send Feedback"}
              </Button>
              <Button size="sm" variant="secondary" onClick={() => { setDecision(null); setComments(""); setMsg(null); }}>
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <div style={{ display: "flex", gap: 6 }}>
            <Button size="sm" onClick={() => setDecision("approved")}>Approve</Button>
            <Button size="sm" variant="secondary" onClick={() => setDecision("changes_requested")}>Request Changes</Button>
            <Button size="sm" variant="danger" onClick={() => setDecision("rejected")}>Reject</Button>
          </div>
        )}
      </div>
    </div>
  );
}

function DeployApprovalRow({ run, riskColor, onApprove, onReject }: {
  run: any;
  riskColor: Record<string, string>;
  onApprove: (id: string) => void;
  onReject: (id: string, reason: string) => void;
}) {
  const [rejectReason, setRejectReason] = useState("");
  const [showReject, setShowReject] = useState(false);
  const rc = riskColor[run.risk_level] ?? "#94a3b8";

  return (
    <div style={{
      background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
      borderLeft: `3px solid ${rc}`, borderRadius: "var(--radius-sm)",
      padding: "var(--space-md) var(--space-lg)", marginBottom: 6,
    }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: "var(--space-md)" }}>
        <div style={{ flex: 1 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
            <span style={{ fontSize: 11, padding: "2px 8px", borderRadius: 4, fontWeight: 700, background: rc + "22", color: rc, fontFamily: "var(--font-mono)", textTransform: "uppercase" as const }}>
              {run.risk_level}
            </span>
            <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)" }}>
              Deployment to {run.risk_summary?.to_env ?? "environment"}
            </span>
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
            Initiated by {run.initiated_by} · {new Date(run.created_at).toLocaleString()}
          </div>
          {run.deploy_notes && (
            <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 3, fontStyle: "italic" }}>{run.deploy_notes}</div>
          )}
          {run.risk_summary?.reason && (
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 3 }}>{run.risk_summary.reason}</div>
          )}
        </div>
        <div style={{ display: "flex", gap: 6, flexShrink: 0, flexDirection: "column" as const, alignItems: "flex-end" }}>
          <div style={{ display: "flex", gap: 6 }}>
            <Button size="sm" onClick={() => onApprove(run.id)}>Approve</Button>
            <Button size="sm" variant="danger" onClick={() => setShowReject(v => !v)}>Reject</Button>
          </div>
          {showReject && (
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input value={rejectReason} onChange={e => setRejectReason(e.target.value)}
                placeholder="Rejection reason…"
                style={{ padding: "4px 8px", border: "1px solid var(--border-default)", borderRadius: 4, fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)", width: 200 }} />
              <Button size="sm" variant="secondary" onClick={() => { onReject(run.id, rejectReason); setShowReject(false); }}>
                Confirm
              </Button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── Queues Overview ──────────────────────────────────────────── */

function QueuesOverview({ onSelectQueue }: { onSelectQueue: (id: string) => void }) {
  const { data: queues, loading } = useApi(listQueues);
  const items = queues ?? [];

  return (
    <div>
      {loading && <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}><Spinner size={28} /></div>}

      {!loading && items.length === 0 && (
        <EmptyState title="No work queues" description="Work queues are configured in the case type definition." />
      )}

      {!loading && items.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: "var(--space-md)" }}>
          {items.map((q) => (
            <QueueCard key={q.id} queue={q} onClick={() => onSelectQueue(q.id)} />
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Queue Card ───────────────────────────────────────────────── */

function QueueCard({ queue, onClick }: { queue: WorkQueueSummary; onClick: () => void }) {
  const { data: stats } = useApi(() => getQueueStats(queue.id), [queue.id]);

  return (
    <Card onClick={onClick}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "var(--space-md)" }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text-primary)" }}>{queue.name}</div>
          {queue.description && (
            <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>{queue.description}</div>
          )}
        </div>
        <QueueIcon />
      </div>

      {stats && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "var(--space-sm)" }}>
          <MiniStat label="Active" value={stats.active_items} />
          <MiniStat label="On Track" value={stats.sla_on_track} color="var(--status-completed)" />
          <MiniStat label="At Risk" value={stats.sla_at_risk + stats.sla_breached} color={stats.sla_breached > 0 ? "var(--status-failed)" : "var(--status-running)"} />
        </div>
      )}

      <div style={{ display: "flex", gap: 6, marginTop: "var(--space-md)" }}>
        {(queue.visible_to_roles || []).slice(0, 3).map((role) => (
          <span key={role} style={{
            fontSize: 10, padding: "2px 6px", borderRadius: "var(--radius-sm)",
            background: "var(--bg-elevated)", color: "var(--text-muted)", fontFamily: "var(--font-mono)",
          }}>{role}</span>
        ))}
      </div>
    </Card>
  );
}

/* ── Queue Detail View ────────────────────────────────────────── */

const PAGE_SIZE = 25;
const STATUS_OPTS = [
  { value: "active",    label: "Active" },
  { value: "claimed",   label: "Claimed" },
  { value: "completed", label: "Completed" },
];

function QueueDetailView({ queueId, onBack }: { queueId: string; onBack: () => void }) {
  const { user } = useAuth();
  const CURRENT_USER = user?.user_id ?? "current-user";

  const [statusFilter, setStatusFilter] = useState("active");
  const [search, setSearch]             = useState("");
  const [debouncedSearch, setDebSearch] = useState("");
  const [page, setPage]                 = useState(1);
  const [totalCount, setTotalCount]     = useState<number | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Debounce search input
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => { setDebSearch(search); setPage(1); }, 350);
  }, [search]);

  // Reset page when filter changes
  useEffect(() => { setPage(1); }, [statusFilter]);

  const { data: items, loading, refetch } = useApi(
    () => getQueueItems(queueId, page, PAGE_SIZE, statusFilter, debouncedSearch || undefined),
    [queueId, page, statusFilter, debouncedSearch],
  );
  const { data: stats } = useApi(() => getQueueStats(queueId), [queueId]);

  // Fetch total count for pagination
  useEffect(() => {
    getQueueItemCount(queueId, statusFilter, debouncedSearch || undefined)
      .then(d => setTotalCount(d.count))
      .catch(() => setTotalCount(null));
  }, [queueId, statusFilter, debouncedSearch]);

  useInterval(refetch, 15000);

  const assignments = items ?? [];
  const totalPages  = totalCount !== null ? Math.max(1, Math.ceil(totalCount / PAGE_SIZE)) : null;

  return (
    <div>
      {/* Back + stats header */}
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-md)", marginBottom: "var(--space-lg)" }}>
        <Button variant="ghost" size="sm" onClick={onBack}>← Queues</Button>
        {stats && (
          <div style={{ display: "flex", gap: "var(--space-lg)", marginLeft: "auto" }}>
            <MiniStat label="Total" value={stats.total_items} />
            <MiniStat label="Active" value={stats.active_items} />
            <MiniStat label="SLA OK" value={stats.sla_on_track} color="var(--status-completed)" />
            <MiniStat label="At Risk" value={stats.sla_at_risk} color="var(--status-running)" />
            <MiniStat label="Breached" value={stats.sla_breached} color="var(--status-failed)" />
          </div>
        )}
      </div>

      {/* Filters row */}
      <div style={{ display: "flex", gap: "var(--space-md)", marginBottom: "var(--space-md)", alignItems: "center", flexWrap: "wrap" as const }}>
        {/* Status tabs */}
        <div style={{ display: "flex", background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)", overflow: "hidden" }}>
          {STATUS_OPTS.map(opt => (
            <button key={opt.value} onClick={() => setStatusFilter(opt.value)} style={{
              padding: "6px 14px", fontSize: 12, fontWeight: statusFilter === opt.value ? 700 : 400,
              border: "none", cursor: "pointer", fontFamily: "var(--font-mono)",
              background: statusFilter === opt.value ? "var(--accent)" : "transparent",
              color: statusFilter === opt.value ? "#fff" : "var(--text-muted)",
            }}>{opt.label}</button>
          ))}
        </div>

        {/* Search */}
        <input value={search} onChange={e => setSearch(e.target.value)}
          placeholder="Search by assignee or step…"
          style={{ flex: 1, minWidth: 200, padding: "7px 10px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 12, background: "var(--bg-input)", color: "var(--text-primary)" }} />

        <Button size="sm" variant="secondary" onClick={() => { setSearch(""); setDebSearch(""); setPage(1); refetch(); }}>
          ↻ Refresh
        </Button>
      </div>

      {/* Result info */}
      {totalCount !== null && (
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: "var(--space-sm)" }}>
          {totalCount} {statusFilter} item{totalCount !== 1 ? "s" : ""}
          {debouncedSearch ? ` matching "${debouncedSearch}"` : ""}
          {totalPages && totalPages > 1 ? ` — page ${page} of ${totalPages}` : ""}
        </div>
      )}

      {loading && <div style={{ display: "flex", justifyContent: "center", padding: "var(--space-2xl)" }}><Spinner size={28} /></div>}

      {!loading && assignments.length === 0 && (
        <EmptyState
          title={`No ${statusFilter} items`}
          description={debouncedSearch ? `No matches for "${debouncedSearch}"` : `No ${statusFilter} work items in this queue.`}
        />
      )}

      {!loading && assignments.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
          {assignments.map((a) => (
            <AssignmentRow key={a.id} assignment={a} showClaim onClaim={async () => {
              await claimAssignment(a.id, CURRENT_USER);
              refetch();
            }} />
          ))}
        </div>
      )}

      {/* Pagination */}
      {totalPages !== null && totalPages > 1 && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "var(--space-md)", marginTop: "var(--space-lg)" }}>
          <Button size="sm" variant="secondary" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>← Prev</Button>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Page {page} / {totalPages}</span>
          <Button size="sm" variant="secondary" disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}>Next →</Button>
        </div>
      )}
    </div>
  );
}

/* ── Assignment Row ───────────────────────────────────────────── */

function AssignmentRow({ assignment, showClaim, allowDirectComplete, onClaim, onComplete, onDismiss }: {
  assignment: CaseAssignment;
  showClaim?: boolean;
  allowDirectComplete?: boolean;  // skip claimed_at check (for self-assigned / My Work)
  onClaim?: () => void;
  onComplete?: () => void;
  onDismiss?: () => void;
}) {
  const { user } = useAuth();
  const CURRENT_USER = user?.user_id ?? "current-user";
  const [showForm, setShowForm] = React.useState(false);
  const [formData, setFormData] = React.useState<any>(null);
  const [prefillValues, setPrefillValues] = React.useState<Record<string, any>>({});
  const [loadingForm, setLoadingForm] = React.useState(false);
  const [confirmDismiss, setConfirmDismiss] = React.useState(false);

  const isOverdue = assignment.due_at && isPast(assignment.due_at);
  const isDueToday = assignment.due_at && isToday(assignment.due_at);

  const handleComplete = async () => {
    // Check if assignment has a form
    setLoadingForm(true);
    try {
      const formInfo = await getAssignmentForm(assignment.id);
      if (formInfo.has_form && formInfo.form) {
        // Pre-fill from case variables (Case Variables Phase 3): explicit
        // per-field `variable` binding wins, else field_key match. Reads are
        // redacted server-side — masked values show as ***.
        let prefill: Record<string, any> = {};
        try {
          const { variables } = await getCaseVariables(assignment.case_id);
          for (const section of formInfo.form.definition_json?.sections ?? []) {
            for (const field of section.fields ?? []) {
              const key = field.field_key || field.id;
              const source = field.variable || key;
              if (variables[source] !== undefined && variables[source] !== null) {
                prefill[key] = variables[source];
              }
            }
          }
        } catch { /* variables unavailable — open the form unfilled */ }
        setPrefillValues(prefill);
        setFormData(formInfo.form);
        setShowForm(true);
      } else {
        // No form — complete directly
        onComplete?.();
      }
    } catch (e) {
      // Fallback — complete directly
      onComplete?.();
    } finally {
      setLoadingForm(false);
    }
  };

  const handleFormSubmit = async (values: Record<string, any>) => {
    await submitForm(assignment.id, {
      form_id: formData.id,
      values,
      completed_by: CURRENT_USER,
    });
    setShowForm(false);
    onComplete?.();
  };

  return (
    <>
      <div style={{
        display: "flex", alignItems: "center", gap: "var(--space-md)",
        padding: "var(--space-md) var(--space-lg)",
        background: "var(--bg-card)",
        borderBottom: "1px solid var(--border-subtle)",
      }}>
        {/* Status indicator */}
        <div style={{
          width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
          background: assignment.status === "completed" ? "var(--status-completed)" :
            isOverdue ? "var(--status-failed)" :
            isDueToday ? "var(--status-running)" :
            "var(--accent)",
        }} />

        {/* Info */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
            <span style={{ fontSize: 12, fontWeight: 500, color: "var(--text-primary)", fontFamily: "var(--font-mono)" }}>
              {assignment.step_id}
            </span>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              on case {assignment.case_id.slice(0, 8)}
            </span>
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
            {assignment.assignee_type}: {assignment.assignee_id} · <TimeAgo date={assignment.assigned_at} />
          </div>
        </div>

        {/* Due date */}
        {assignment.due_at && (
          <span style={{
            fontSize: 10, fontFamily: "var(--font-mono)",
            color: isOverdue ? "var(--status-failed)" : isDueToday ? "var(--status-running)" : "var(--text-muted)",
          }}>
            due <TimeAgo date={assignment.due_at} />
          </span>
        )}

        {/* Actions */}
        {showClaim && assignment.status === "active" && !assignment.claimed_at && onClaim && (
          <Button size="sm" onClick={(e: any) => { e.stopPropagation(); onClaim(); }}>Claim</Button>
        )}
        {assignment.status === "active" && (assignment.claimed_at || allowDirectComplete) && onComplete && (
          <Button size="sm" variant="secondary" disabled={loadingForm}
            onClick={(e: any) => { e.stopPropagation(); handleComplete(); }}>
            {loadingForm ? "Loading…" : "Complete"}
          </Button>
        )}
        {onDismiss && assignment.status === "active" && (
          confirmDismiss ? (
            <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
              <span style={{ fontSize: 11, color: "var(--text-secondary)", whiteSpace: "nowrap" }}>Remove?</span>
              <button
                onClick={(e: any) => { e.stopPropagation(); setConfirmDismiss(false); onDismiss(); }}
                style={{ padding: "3px 10px", fontSize: 11, borderRadius: 4, border: "none", background: "var(--status-failed)", color: "#fff", cursor: "pointer", fontWeight: 600 }}
              >Yes</button>
              <button
                onClick={(e: any) => { e.stopPropagation(); setConfirmDismiss(false); }}
                style={{ padding: "3px 10px", fontSize: 11, borderRadius: 4, border: "1px solid var(--border-default)", background: "transparent", color: "var(--text-secondary)", cursor: "pointer" }}
              >No</button>
            </div>
          ) : (
            <button onClick={(e: any) => { e.stopPropagation(); setConfirmDismiss(true); }}
              title="Remove assignment"
              style={{ background: "none", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", cursor: "pointer", fontSize: 12, color: "var(--text-muted)", padding: "4px 8px" }}
              onMouseEnter={e => { (e.currentTarget as HTMLElement).style.borderColor = "var(--status-failed)"; (e.currentTarget as HTMLElement).style.color = "var(--status-failed)"; }}
              onMouseLeave={e => { (e.currentTarget as HTMLElement).style.borderColor = "var(--border-default)"; (e.currentTarget as HTMLElement).style.color = "var(--text-muted)"; }}
            >✕</button>
          )
        )}
        {assignment.status === "completed" && (
          <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--status-completed)" }}>DONE</span>
        )}
      </div>

      {/* Form modal */}
      {showForm && formData && (
        <div onClick={() => setShowForm(false)} style={{
          position: "fixed", inset: 0, background: "var(--bg-overlay)",
          display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100,
        }}>
          <div onClick={(e) => e.stopPropagation()} style={{
            width: 600, maxHeight: "80vh", overflow: "auto",
          }}>
            <FormRenderer
              formName={formData.name}
              definition={formData.definition_json}
              initialValues={prefillValues}
              caseId={assignment.case_id}
              onSubmit={handleFormSubmit}
              onCancel={() => setShowForm(false)}
            />
          </div>
        </div>
      )}
    </>
  );
}

/* ── Shared helpers ───────────────────────────────────────────── */

function MiniStat({ label, value, color }: { label: string; value: number; color?: string }) {
  return (
    <div style={{  }}>
      <div style={{ fontSize: 18, fontWeight: 600, color: color || "var(--text-primary)", fontFamily: "var(--font-display)" }}>
        {value}
      </div>
      <div style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)" }}>
        {label}
      </div>
    </div>
  );
}

function QueueIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
      <rect x="2" y="3" width="16" height="3" rx="1" stroke="var(--text-muted)" strokeWidth="1.5" />
      <rect x="2" y="9" width="16" height="3" rx="1" stroke="var(--text-muted)" strokeWidth="1.5" />
      <rect x="2" y="15" width="10" height="3" rx="1" stroke="var(--text-muted)" strokeWidth="1.5" />
    </svg>
  );
}

function WcPager({ page, totalPages, total, onChange }: { page: number; totalPages: number; total: number; onChange: (p: number) => void }) {
  const pages: (number | "…")[] = [];
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - page) <= 1) pages.push(i);
    else if (pages[pages.length - 1] !== "…") pages.push("…");
  }
  const btnStyle = (active: boolean): React.CSSProperties => ({
    width: 26, height: 26, borderRadius: 4, border: `1px solid ${active ? "var(--accent)" : "var(--border-default)"}`,
    background: active ? "var(--accent)" : "transparent", color: active ? "#fff" : "var(--text-secondary)",
    fontSize: 11, cursor: "pointer", fontFamily: "var(--font-mono)",
    display: "inline-flex", alignItems: "center", justifyContent: "center",
  });
  return (
    <div style={{ display: "flex", gap: 3, alignItems: "center" }}>
      <span style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginRight: 4 }}>
        {(page - 1) * WC_PAGE_SIZE + 1}–{Math.min(page * WC_PAGE_SIZE, total)} / {total}
      </span>
      <button onClick={() => onChange(page - 1)} disabled={page === 1} style={{ ...btnStyle(false), opacity: page === 1 ? 0.35 : 1 }}>‹</button>
      {pages.map((p, i) => p === "…"
        ? <span key={`e${i}`} style={{ fontSize: 11, color: "var(--text-muted)" }}>…</span>
        : <button key={p} onClick={() => onChange(p as number)} style={btnStyle(page === p)}>{p}</button>
      )}
      <button onClick={() => onChange(page + 1)} disabled={page >= totalPages} style={{ ...btnStyle(false), opacity: page >= totalPages ? 0.35 : 1 }}>›</button>
    </div>
  );
}

function isToday(dateStr: string): boolean {
  const d = new Date(dateStr);
  const now = new Date();
  return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
}

function isPast(dateStr: string): boolean {
  return new Date(dateStr).getTime() < Date.now();
}
