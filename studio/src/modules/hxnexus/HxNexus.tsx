import React, { useEffect, useRef, useState } from "react";

type Tab = "chat" | "suggest" | "qa" | "translator";
type Message = { role: "user" | "assistant"; content: string };
type Suggestion = { action: string; reason: string; priority: "high" | "medium" | "low" };
type BackendStatus = { name: string; backend: string; available: boolean; capabilities: string[] };
type Summary = { summary: string; key_points: string[]; action_items: string[] };

function _nexusHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = localStorage.getItem("helix_token");
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(extra ?? {}),
  };
}

async function apiJSON<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(url, {
    ...opts,
    headers: _nexusHeaders(opts.headers as Record<string, string>),
  });
  if (!r.ok) {
    const txt = await r.text().catch(() => r.statusText);
    throw new Error(txt);
  }
  return r.json();
}

const PRIORITY_COLOR: Record<string, string> = {
  high: "#ef4444", medium: "#f59e0b", low: "#22c55e",
};

export default function HxNexus() {
  const [tab, setTab] = useState<Tab>("chat");
  const [status, setStatus] = useState<BackendStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);

  // Chat state
  const [messages, setMessages] = useState<Message[]>([]);
  const [convId, setConvId] = useState<string | null>(null);
  const [caseId, setCaseId] = useState("");
  const [input, setInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Suggest state
  const [suggestCaseId, setSuggestCaseId] = useState("");
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [suggestLoading, setSuggestLoading] = useState(false);
  const [suggestError, setSuggestError] = useState<string | null>(null);

  // Q&A state
  const [qaCaseId, setQaCaseId] = useState("");
  const [qaQuestion, setQaQuestion] = useState("");
  const [qaAnswer, setQaAnswer] = useState<string | null>(null);
  const [qaSources, setQaSources] = useState<any[]>([]);
  const [qaLoading, setQaLoading] = useState(false);
  const [qaError, setQaError] = useState<string | null>(null);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadLoading, setUploadLoading] = useState(false);
  const [uploadResult, setUploadResult] = useState<string | null>(null);

  useEffect(() => {
    apiJSON<BackendStatus>("/api/v1/hxnexus/status")
      .then(setStatus)
      .catch(e => setStatusError(e.message));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function sendChat() {
    if (!input.trim()) return;
    const userMsg = input.trim();
    setInput("");
    setSummary(null);
    setMessages(m => [...m, { role: "user", content: userMsg }]);
    setChatLoading(true);
    try {
      const body: any = { message: userMsg };
      if (convId) body.conversation_id = convId;
      if (caseId.trim()) body.case_id = caseId.trim();
      const res = await apiJSON<{ reply: string; conversation_id: string }>(
        "/api/v1/hxnexus/chat", { method: "POST", body: JSON.stringify(body) }
      );
      setConvId(res.conversation_id);
      setMessages(m => [...m, { role: "assistant", content: res.reply }]);
    } catch (e: any) {
      setMessages(m => [...m, { role: "assistant", content: `Error: ${e.message}` }]);
    } finally {
      setChatLoading(false);
    }
  }

  function downloadTranscript() {
    if (!convId) return;
    window.open(`/api/v1/hxnexus/conversations/${convId}/transcript`, "_blank");
  }

  async function fetchSummary() {
    if (!convId) return;
    setSummaryLoading(true);
    setSummary(null);
    try {
      const res = await apiJSON<Summary>(
        `/api/v1/hxnexus/conversations/${convId}/summarize`, { method: "POST", body: "{}" }
      );
      setSummary(res);
    } catch (e: any) {
      setSummary({ summary: `Error: ${e.message}`, key_points: [], action_items: [] });
    } finally {
      setSummaryLoading(false);
    }
  }

  function newConversation() {
    setConvId(null);
    setMessages([]);
    setSummary(null);
  }

  async function fetchSuggestions() {
    if (!suggestCaseId.trim()) return;
    setSuggestLoading(true); setSuggestError(null); setSuggestions([]);
    try {
      const res = await apiJSON<{ suggestions: Suggestion[] }>(
        `/api/v1/hxnexus/cases/${suggestCaseId.trim()}/suggest`, { method: "POST", body: "{}" }
      );
      setSuggestions(res.suggestions || []);
      if (!res.suggestions?.length) setSuggestError("No suggestions returned — LLM may be unavailable.");
    } catch (e: any) {
      setSuggestError(e.message);
    } finally {
      setSuggestLoading(false);
    }
  }

  async function fetchQA() {
    if (!qaCaseId.trim() || !qaQuestion.trim()) return;
    setQaLoading(true); setQaError(null); setQaAnswer(null); setQaSources([]);
    try {
      const res = await apiJSON<{ answer: string; sources: any[] }>(
        `/api/v1/hxnexus/cases/${qaCaseId.trim()}/qa`,
        { method: "POST", body: JSON.stringify({ question: qaQuestion.trim(), top_k: 5 }) }
      );
      setQaAnswer(res.answer);
      setQaSources(res.sources || []);
    } catch (e: any) {
      setQaError(e.message);
    } finally {
      setQaLoading(false);
    }
  }

  async function uploadDocument() {
    if (!uploadFile) return;
    setUploadLoading(true); setUploadResult(null);
    try {
      const form = new FormData();
      form.append("file", uploadFile);
      if (qaCaseId.trim()) form.append("case_id", qaCaseId.trim());
      const token = localStorage.getItem("helix_token");
      const r = await fetch("/api/v1/hxnexus/documents/upload", {
        method: "POST", body: form,
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!r.ok) throw new Error(await r.text());
      const res = await r.json();
      setUploadResult(`✓ Indexed ${res.chunks_indexed} chunks from "${res.filename}"`);
      setUploadFile(null);
    } catch (e: any) {
      setUploadResult(`Error: ${e.message}`);
    } finally {
      setUploadLoading(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Tab bar — matches Work Center format, no duplicate title */}
      <div style={{
        padding: "var(--space-md) var(--space-2xl)",
        display: "flex", alignItems: "center", gap: "var(--space-md)", flexShrink: 0,
      }}>
        <div style={{ display: "flex", background: "var(--bg-card)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)" }}>
          {(["chat", "suggest", "qa", "translator"] as Tab[]).map(t => (
            <button key={t} onClick={() => setTab(t)} style={{
              padding: "8px 16px", fontSize: 12, fontWeight: 500,
              fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.04em",
              border: "none", cursor: "pointer", borderRadius: "var(--radius-sm)",
              color: tab === t ? "var(--accent)" : "var(--text-muted)",
              background: tab === t ? "var(--accent-dim)" : "transparent",
            }}>
              {t === "chat" ? "Chat" : t === "suggest" ? "Suggestions" : t === "qa" ? "Doc Q&A" : "BPM Translator"}
            </button>
          ))}
        </div>
        {/* Status indicator at far right */}
        {status && (
          <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6, fontSize: 11, fontFamily: "var(--font-mono)", color: status.available ? "var(--status-completed)" : "var(--status-failed)" }}>
            <span style={{ width: 6, height: 6, borderRadius: "50%", background: status.available ? "var(--status-completed)" : "var(--status-failed)", display: "inline-block" }} />
            {status.backend} {status.available ? "online" : "offline"}
          </div>
        )}
        {statusError && <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--status-failed)", fontFamily: "var(--font-mono)" }}>{statusError}</span>}
      </div>

      {/* ── Chat tab ── */}
      {tab === "chat" && (
        <div style={{ display: "flex", flexDirection: "column", flex: 1, overflow: "hidden" }}>
          {/* Case ID + conversation controls */}
          <div style={{ padding: "var(--space-sm) var(--space-2xl)", borderBottom: "1px solid var(--border-subtle)", flexShrink: 0 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input
                value={caseId} onChange={e => { setCaseId(e.target.value); newConversation(); }}
                placeholder="Optional: Case ID to scope conversation"
                style={{ flex: 1, padding: "7px 12px", border: "1px solid var(--border-default)", borderRadius: 6, fontSize: 13, boxSizing: "border-box" }}
              />
              {convId && (
                <>
                  <button
                    onClick={downloadTranscript}
                    title="Download transcript"
                    style={{ padding: "7px 12px", border: "1px solid var(--border-default)", borderRadius: 6, cursor: "pointer", fontSize: 12, background: "var(--bg-card)", whiteSpace: "nowrap" }}
                  >
                    ↓ Transcript
                  </button>
                  <button
                    onClick={fetchSummary} disabled={summaryLoading}
                    title="Summarize conversation"
                    style={{ padding: "7px 12px", border: "1px solid var(--border-default)", borderRadius: 6, cursor: "pointer", fontSize: 12, background: "var(--bg-card)", whiteSpace: "nowrap", opacity: summaryLoading ? 0.5 : 1 }}
                  >
                    {summaryLoading ? "…" : "∑ Summarize"}
                  </button>
                  <button
                    onClick={newConversation}
                    title="New conversation"
                    style={{ padding: "7px 12px", border: "1px solid var(--border-default)", borderRadius: 6, cursor: "pointer", fontSize: 12, background: "var(--bg-card)", whiteSpace: "nowrap" }}
                  >
                    + New
                  </button>
                </>
              )}
            </div>
          </div>

          {/* Summary panel */}
          {summary && (
            <div style={{ padding: "12px 24px", background: "var(--accent-dim)", borderBottom: "1px solid var(--border-subtle)", flexShrink: 0, fontSize: 13 }}>
              <strong style={{ color: "#0d9488" }}>Summary:</strong> {summary.summary}
              {summary.key_points.length > 0 && (
                <div style={{ marginTop: 6 }}>
                  <span style={{ color: "var(--text-muted)" }}>Key points: </span>
                  {summary.key_points.map((p, i) => <span key={i} style={{ marginRight: 8 }}>· {p}</span>)}
                </div>
              )}
              {summary.action_items.length > 0 && (
                <div style={{ marginTop: 4 }}>
                  <span style={{ color: "var(--text-muted)" }}>Action items: </span>
                  {summary.action_items.map((a, i) => <span key={i} style={{ marginRight: 8 }}>→ {a}</span>)}
                </div>
              )}
            </div>
          )}

          {/* Messages */}
          <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-lg) var(--space-2xl)", display: "flex", flexDirection: "column", gap: 12 }}>
            {messages.length === 0 && (
              <div style={{ color: "var(--text-muted)", marginTop: 40 }}>
                <div style={{ fontSize: 32, marginBottom: 8 }}>✦</div>
                <p style={{ fontSize: 14 }}>Ask HxNexus anything about your cases.</p>
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} style={{
                alignSelf: m.role === "user" ? "flex-end" : "flex-start",
                maxWidth: "75%",
                padding: "10px 14px",
                borderRadius: m.role === "user" ? "16px 16px 4px 16px" : "16px 16px 16px 4px",
                background: m.role === "user" ? "var(--accent)" : "var(--bg-elevated)",
                color: m.role === "user" ? "#fff" : "var(--text-primary)",
                fontSize: 14, lineHeight: 1.5,
                whiteSpace: "pre-wrap",
              }}>
                {m.content}
              </div>
            ))}
            {chatLoading && (
              <div style={{ alignSelf: "flex-start", padding: "10px 14px", borderRadius: "16px 16px 16px 4px", background: "var(--bg-elevated)", color: "var(--text-muted)", fontSize: 13 }}>
                HxNexus is thinking…
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {/* Input */}
          <div style={{ padding: "var(--space-md) var(--space-2xl)", borderTop: "1px solid var(--border-subtle)", display: "flex", gap: 8, flexShrink: 0 }}>
            <input
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => e.key === "Enter" && !e.shiftKey && sendChat()}
              placeholder="Ask HxNexus…"
              style={{ flex: 1, padding: "10px 14px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 13, outline: "none", background: "var(--bg-input)", color: "var(--text-primary)" }}
            />
            <button
              onClick={sendChat} disabled={chatLoading || !input.trim()}
              style={{ padding: "9px 20px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: "var(--radius-sm)", cursor: "pointer", fontWeight: 600, fontSize: 13, opacity: chatLoading || !input.trim() ? 0.5 : 1 }}
            >
              Send
            </button>
          </div>
        </div>
      )}

      {/* ── Suggest tab ── */}
      {tab === "suggest" && (
        <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-xl) var(--space-2xl)" }}>
          <p style={{ color: "var(--text-muted)", fontSize: 14, marginTop: 0 }}>Get AI-powered next-best-action suggestions for any case.</p>
          <div style={{ display: "flex", gap: 8, marginBottom: 24 }}>
            <input
              value={suggestCaseId} onChange={e => setSuggestCaseId(e.target.value)}
              placeholder="Case ID (UUID)"
              style={{ flex: 1, padding: "9px 12px", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)" }}
            />
            <button
              onClick={fetchSuggestions} disabled={suggestLoading || !suggestCaseId.trim()}
              style={{ padding: "9px 20px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", fontWeight: 600, opacity: suggestLoading || !suggestCaseId.trim() ? 0.5 : 1 }}
            >
              {suggestLoading ? "Thinking…" : "Get Suggestions"}
            </button>
          </div>
          {suggestError && <div style={{ color: "#dc2626", marginBottom: 16, fontSize: 14 }}>{suggestError}</div>}
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {suggestions.map((s, i) => (
              <div key={i} style={{ padding: 16, border: "1px solid var(--border-subtle)", borderRadius: 10, background: "var(--bg-card)", borderLeft: `4px solid ${PRIORITY_COLOR[s.priority] || "#0d9488"}` }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                  <span style={{ fontWeight: 600, fontSize: 15 }}>{s.action}</span>
                  <span style={{ fontSize: 11, padding: "2px 8px", borderRadius: 4, background: PRIORITY_COLOR[s.priority] + "20", color: PRIORITY_COLOR[s.priority], fontWeight: 600 }}>
                    {s.priority}
                  </span>
                </div>
                <p style={{ margin: 0, fontSize: 13, color: "var(--text-muted)" }}>{s.reason}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Q&A tab ── */}
      {tab === "qa" && (
        <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-xl) var(--space-2xl)" }}>
          <p style={{ color: "var(--text-muted)", fontSize: 14, marginTop: 0 }}>Ask questions about documents attached to a case (RAG).</p>

          {/* Document upload */}
          <div style={{ padding: 16, background: "var(--bg-elevated)", border: "1px solid var(--border-subtle)", borderRadius: 10, marginBottom: 20 }}>
            <p style={{ margin: "0 0 10px", fontWeight: 600, fontSize: 13, color: "var(--text-secondary)" }}>Upload a document for indexing (PDF, DOCX, TXT)</p>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <input
                type="file" accept=".pdf,.docx,.txt"
                onChange={e => setUploadFile(e.target.files?.[0] || null)}
                style={{ fontSize: 13 }}
              />
              <input
                value={qaCaseId} onChange={e => setQaCaseId(e.target.value)}
                placeholder="Case ID (optional)"
                style={{ padding: "7px 10px", border: "1px solid var(--border-default)", borderRadius: 6, fontSize: 13, width: 220 }}
              />
              <button
                onClick={uploadDocument} disabled={uploadLoading || !uploadFile}
                style={{ padding: "7px 16px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", fontWeight: 600, fontSize: 13, opacity: uploadLoading || !uploadFile ? 0.5 : 1 }}
              >
                {uploadLoading ? "Indexing…" : "Upload & Index"}
              </button>
            </div>
            {uploadResult && (
              <p style={{ margin: "8px 0 0", fontSize: 13, color: uploadResult.startsWith("Error") ? "#dc2626" : "#16a34a" }}>{uploadResult}</p>
            )}
          </div>

          {/* Q&A form */}
          <div style={{ display: "flex", flexDirection: "column", gap: 10, marginBottom: 20 }}>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                value={qaQuestion} onChange={e => setQaQuestion(e.target.value)}
                onKeyDown={e => e.key === "Enter" && fetchQA()}
                placeholder="Ask a question about the case documents…"
                style={{ flex: 1, padding: "9px 12px", border: "1px solid var(--border-default)", borderRadius: 6, fontSize: 14 }}
              />
              <button
                onClick={fetchQA} disabled={qaLoading || !qaQuestion.trim()}
                style={{ padding: "9px 20px", background: "var(--accent)", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", fontWeight: 600, opacity: qaLoading || !qaQuestion.trim() ? 0.5 : 1 }}
              >
                {qaLoading ? "Searching…" : "Ask"}
              </button>
            </div>
          </div>
          {qaError && <div style={{ color: "#dc2626", marginBottom: 16, fontSize: 14 }}>{qaError}</div>}
          {qaAnswer && (
            <div style={{ marginBottom: 20 }}>
              <div style={{ padding: 16, background: "var(--accent-dim)", border: "1px solid var(--border-subtle)", borderRadius: 10, fontSize: 14, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
                <strong style={{ color: "#0d9488" }}>HxNexus:</strong> {qaAnswer}
              </div>
              {qaSources.length > 0 && (
                <div style={{ marginTop: 10 }}>
                  <p style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 6 }}>Sources used ({qaSources.length}):</p>
                  {qaSources.map((s, i) => (
                    <div key={i} style={{ fontSize: 12, color: "var(--text-muted)", padding: "4px 0" }}>
                      Chunk {s.chunk_id?.slice(0, 8)}… · score {s.score}
                      {s.document_id && <span> · doc {s.document_id.slice(0, 8)}…</span>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
      {/* ── BPM Translator tab ── */}
      {tab === "translator" && <BpmTranslatorPanel />}
    </div>
  );
}


// ── BPM Translator Panel ───────────────────────────────────────────────────

const BPM_TOOLS = ["pega", "camunda", "appian", "servicenow"] as const;
type BpmTool = typeof BPM_TOOLS[number];

type Concept = {
  source_tool: string; source_concept: string; helix_equiv: string;
  helix_node_type: string | null; description: string;
  example: string | null; confidence: string; notes: string | null;
};

const CONFIDENCE_COLOR: Record<string, string> = {
  exact: "#22c55e", close: "#3b82f6", partial: "#f59e0b", manual: "#ef4444",
};

function BpmTranslatorPanel() {
  const [tool, setTool]           = useState<BpmTool>("pega");
  const [mode, setMode]           = useState<"lookup" | "analyze" | "compare">("lookup");
  const [concept, setConcept]     = useState("");
  const [text, setText]           = useState("");
  const [question, setQuestion]   = useState("");
  const [loading, setLoading]     = useState(false);
  const [result, setResult]       = useState<any>(null);
  const [concepts, setConcepts]   = useState<Concept[]>([]);
  const [filter, setFilter]       = useState("");

  useEffect(() => {
    fetch(`/api/v1/hxnexus/polyglot/concepts?tool=${tool}`, {
      headers: _nexusHeaders(),
    }).then(r => r.ok ? r.json() : null)
      .then(d => setConcepts(d?.concepts ?? []));
  }, [tool]);

  const filtered = concepts.filter(c =>
    !filter || c.source_concept.toLowerCase().includes(filter.toLowerCase())
  );

  async function handleAction(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true); setResult(null);
    try {
      let r;
      if (mode === "lookup") {
        r = await fetch("/api/v1/hxnexus/polyglot/translate", {
          method: "POST", headers: _nexusHeaders(),
          body: JSON.stringify({ tool, concept }),
        });
      } else if (mode === "analyze") {
        r = await fetch("/api/v1/hxnexus/polyglot/analyze", {
          method: "POST", headers: _nexusHeaders(),
          body: JSON.stringify({ tool, text }),
        });
      } else {
        r = await fetch("/api/v1/hxnexus/polyglot/compare", {
          method: "POST", headers: _nexusHeaders(),
          body: JSON.stringify({ tool, question }),
        });
      }
      if (r?.ok) setResult(await r.json());
    } finally { setLoading(false); }
  }

  return (
    <div style={{ display: "flex", height: "100%", overflow: "hidden" }}>
      {/* Concept list */}
      <div style={{
        width: 240, flexShrink: 0, borderRight: "1px solid var(--border-subtle)",
        display: "flex", flexDirection: "column", background: "var(--bg-elevated)",
      }}>
        <div style={{ padding: "12px 12px 8px" }}>
          <select value={tool} onChange={e => setTool(e.target.value as BpmTool)} style={{
            width: "100%", padding: "5px 8px", marginBottom: 8,
            border: "1px solid var(--border-default)", borderRadius: 6, fontSize: 12, background: "var(--bg-card)",
          }}>
            {BPM_TOOLS.map(t => <option key={t} value={t}>{t.charAt(0).toUpperCase() + t.slice(1)}</option>)}
          </select>
          <input value={filter} onChange={e => setFilter(e.target.value)}
            placeholder="Filter concepts…"
            style={{ width: "100%", padding: "5px 8px", border: "1px solid var(--border-default)", borderRadius: 6, fontSize: 12, boxSizing: "border-box" }}
          />
        </div>
        <div style={{ flex: 1, overflow: "auto" }}>
          {filtered.map(c => (
            <div key={c.source_concept}
              onClick={() => { setConcept(c.source_concept); setMode("lookup"); }}
              style={{
                padding: "7px 12px", cursor: "pointer", borderBottom: "1px solid var(--border-subtle)",
                background: concept === c.source_concept ? "var(--accent-dim)" : "transparent",
              }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)" }}>{c.source_concept}</div>
              <div style={{ fontSize: 10, color: CONFIDENCE_COLOR[c.confidence] || "#6b7280" }}>
                → {c.helix_equiv}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Right panel */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {/* Mode tabs */}
        <div style={{ display: "flex", gap: 2, padding: "var(--space-md) var(--space-lg) 0", background: "var(--bg-elevated)", borderBottom: "1px solid var(--border-subtle)", flexShrink: 0 }}>
          {(["lookup", "analyze", "compare"] as const).map(m => (
            <button key={m} onClick={() => { setMode(m); setResult(null); }} style={{
              padding: "6px 14px", border: "none", borderBottom: mode === m ? "2px solid var(--accent)" : "2px solid transparent",
              background: "none", fontSize: 12, fontWeight: mode === m ? 700 : 400,
              color: mode === m ? "var(--accent)" : "var(--text-muted)", cursor: "pointer",
            }}>
              {m === "lookup" ? "Translate" : m === "analyze" ? "Analyze Fragment" : "Compare"}
            </button>
          ))}
        </div>

        <div style={{ flex: 1, overflow: "auto", padding: 16 }}>
          <form onSubmit={handleAction} style={{ marginBottom: 16 }}>
            {mode === "lookup" && (
              <div style={{ display: "flex", gap: 8 }}>
                <input value={concept} onChange={e => setConcept(e.target.value)}
                  placeholder={`${tool} concept name…`}
                  style={{ flex: 1, padding: "7px 10px", border: "1px solid var(--border-default)", borderRadius: 6, fontSize: 13 }}
                />
                <button type="submit" disabled={loading} style={{
                  padding: "7px 16px", background: "var(--accent)", color: "#fff",
                  border: "none", borderRadius: 6, fontSize: 12, fontWeight: 700, cursor: "pointer",
                }}>Translate</button>
              </div>
            )}
            {mode === "analyze" && (
              <div>
                <textarea value={text} onChange={e => setText(e.target.value)} rows={5}
                  placeholder={`Paste a ${tool} configuration fragment here…`}
                  style={{ width: "100%", padding: "8px", border: "1px solid var(--border-default)", borderRadius: 6, fontSize: 12, resize: "vertical", boxSizing: "border-box" }}
                />
                <button type="submit" disabled={loading} style={{
                  marginTop: 8, padding: "7px 16px", background: "var(--accent)", color: "#fff",
                  border: "none", borderRadius: 6, fontSize: 12, fontWeight: 700, cursor: "pointer",
                }}>Analyze</button>
              </div>
            )}
            {mode === "compare" && (
              <div style={{ display: "flex", gap: 8 }}>
                <input value={question} onChange={e => setQuestion(e.target.value)}
                  placeholder={`e.g. "how do I model an approval in ${tool}?"`}
                  style={{ flex: 1, padding: "7px 10px", border: "1px solid var(--border-default)", borderRadius: 6, fontSize: 13 }}
                />
                <button type="submit" disabled={loading} style={{
                  padding: "7px 16px", background: "var(--accent)", color: "#fff",
                  border: "none", borderRadius: 6, fontSize: 12, fontWeight: 700, cursor: "pointer",
                }}>Compare</button>
              </div>
            )}
          </form>

          {loading && <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Thinking…</div>}

          {result && mode === "lookup" && (
            <div style={{ background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: 8, padding: 16 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
                <span style={{ fontWeight: 700, fontSize: 14 }}>{result.source_concept}</span>
                <span style={{ color: "var(--text-muted)" }}>→</span>
                <span style={{ fontWeight: 700, fontSize: 14, color: "var(--accent)" }}>{result.helix_equiv}</span>
                <span style={{
                  marginLeft: "auto", padding: "2px 8px", borderRadius: 10, fontSize: 10, fontWeight: 700,
                  background: (CONFIDENCE_COLOR[result.confidence] || "#6b7280") + "22",
                  color: CONFIDENCE_COLOR[result.confidence] || "#6b7280",
                }}>{result.confidence}</span>
              </div>
              <p style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 8 }}>{result.description}</p>
              {result.example && <div style={{ background: "var(--bg-elevated)", borderRadius: 6, padding: 10, fontSize: 12, fontFamily: "monospace", color: "var(--text-secondary)" }}>{result.example}</div>}
              {result.notes && <p style={{ fontSize: 12, color: "#f59e0b", marginTop: 8 }}>⚠ {result.notes}</p>}
              {result.enrichment && <p style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 10, fontStyle: "italic" }}>{result.enrichment}</p>}
            </div>
          )}

          {result && mode === "analyze" && (
            <div>
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 8 }}>{result.hint}</div>
              {(result.keyword_hits ?? []).length === 0 && !result.llm_analysis && (
                <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "8px 0" }}>No known BPM concepts detected in the fragment.</div>
              )}
              {(result.keyword_hits ?? []).map((h: Concept, i: number) => (
                <div key={i} style={{ background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: 6, padding: 10, marginBottom: 8 }}>
                  <span style={{ fontWeight: 600, fontSize: 12 }}>{h.source_concept}</span>
                  <span style={{ color: "var(--text-muted)", margin: "0 6px" }}>→</span>
                  <span style={{ fontSize: 12, color: "var(--accent)" }}>{h.helix_equiv}</span>
                </div>
              ))}
              {result.llm_analysis && (
                <div style={{ background: "var(--bg-elevated)", borderRadius: 8, padding: 12, fontSize: 13, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
                  {result.llm_analysis}
                </div>
              )}
            </div>
          )}

          {result && mode === "compare" && (
            <div style={{ background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: 8, padding: 16 }}>
              <div style={{ fontSize: 13, lineHeight: 1.7, whiteSpace: "pre-wrap", color: "var(--text-secondary)" }}>{result.answer}</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
