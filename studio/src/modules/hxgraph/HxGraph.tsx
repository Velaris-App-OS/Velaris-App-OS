/**
 * P41 — HxGraph: Helix Native Knowledge Graph
 * Tabs: Visualize · Browse Nodes · Ask HxNexus · Graph Report
 */
import React, { useState, useEffect } from "react";
import { useTheme } from "@/theme/ThemeContext";

const API = "/api/v1/graph";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { ..._authHdr(), ...(opts.headers as object || {}) } });
}

type NodeItem = {
  id: string; node_type: string; label: string;
  summary: string | null; community_id: number | null; properties: Record<string, unknown>;
};

type QueryResult = { question: string; answer: string; relevant_nodes: NodeItem[] };
type SyncResult  = { nodes: number; edges: number; communities: number; embedded: number };

const TYPE_COLOR: Record<string, string> = {
  case_type:    "#818cf8",
  stage:        "#34d399",
  step:         "#60a5fa",
  form:         "#f59e0b",
  field:        "#fb923c",
  module:       "#a78bfa",
  endpoint:     "#94a3b8",
  access_group: "#f472b6",
  access_role:  "#e11d48",
  connector:    "#2dd4bf",
  concept:      "#e879f9",
  pattern:      "#fbbf24",
};

const NODE_TYPES = [
  "case_type","stage","step","form","field","module",
  "endpoint","access_group","access_role","connector","concept","pattern",
];

const badge = (type: string) => (
  <span style={{
    display: "inline-block", padding: "1px 7px", borderRadius: 10,
    fontSize: 10, fontWeight: 700, textTransform: "uppercase" as const,
    background: (TYPE_COLOR[type] || "#718096") + "22",
    color: TYPE_COLOR[type] || "#718096", marginRight: 6,
  }}>{type}</span>
);

function buildVizHtml(
  data: { nodes: NodeItem[]; edges: { from: string; to: string; edge_type: string; weight: number }[] },
  resolvedTheme: "dark" | "light",
): string {
  const isDark = resolvedTheme === "dark";
  const bg        = isDark ? "#0f1117" : "#f8fafc";
  const toolbarBg = isDark ? "#1a1d2e" : "#ffffff";
  const toolbarBorder = isDark ? "#2d3748" : "#e2e8f0";
  const textColor = isDark ? "#e2e8f0" : "#1e293b";
  const mutedColor = isDark ? "#718096" : "#64748b";
  const inputBg   = isDark ? "#0f1117" : "#f1f5f9";
  const inputBorder = isDark ? "#4a5568" : "#cbd5e1";
  const tipBg     = isDark ? "#1a1d2e" : "#ffffff";
  const tipBorder = isDark ? "#4a5568" : "#e2e8f0";
  const linkColor = isDark ? "#2d3748" : "#cbd5e1";
  const linkSimilar = isDark ? "#4a5568" : "#94a3b8";
  const labelColor = isDark ? "#94a3b8" : "#475569";
  const nodeBorder = isDark ? "#0f1117" : "#f8fafc";
  const h1Color   = "#0d9488";

  const dataJson = JSON.stringify(data);

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>HxGraph</title>
<style>
  *{box-sizing:border-box;}
  body{margin:0;background:${bg};font-family:system-ui,sans-serif;color:${textColor};}
  #toolbar{position:fixed;top:0;left:0;right:0;height:48px;background:${toolbarBg};
    border-bottom:1px solid ${toolbarBorder};display:flex;align-items:center;gap:10px;padding:0 16px;z-index:10;}
  #toolbar h1{font-size:13px;font-weight:700;color:${h1Color};margin:0;white-space:nowrap;}
  #toolbar input,#toolbar select{
    padding:5px 9px;border-radius:5px;border:1px solid ${inputBorder};
    background:${inputBg};color:${textColor};font-size:12px;}
  #toolbar input{width:180px;}
  #search-count{font-size:11px;color:${mutedColor};white-space:nowrap;}
  #stats{margin-left:auto;font-size:10px;color:${mutedColor};white-space:nowrap;}
  #canvas{position:fixed;top:48px;left:0;right:0;bottom:0;}
  #tip{position:fixed;background:${tipBg};border:1px solid ${tipBorder};border-radius:8px;
    padding:8px 12px;font-size:11px;pointer-events:none;display:none;max-width:260px;z-index:20;
    line-height:1.6;color:${textColor};box-shadow:0 4px 12px rgba(0,0,0,0.15);}
  #no-results{position:fixed;top:60px;left:50%;transform:translateX(-50%);
    background:${tipBg};border:1px solid ${tipBorder};border-radius:8px;
    padding:8px 16px;font-size:12px;color:${mutedColor};display:none;z-index:15;}
</style>
</head>
<body>
<div id="toolbar">
  <h1>HxGraph</h1>
  <input id="search" placeholder="Search nodes…" autocomplete="off"/>
  <span id="search-count"></span>
  <select id="typeFilter"><option value="">All types</option></select>
  <div id="stats"></div>
</div>
<svg id="canvas"></svg>
<div id="tip"></div>
<div id="no-results">No matching nodes found</div>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const RAW=${dataJson};
const NC={
  case_type:"#818cf8",stage:"#34d399",step:"#60a5fa",form:"#f59e0b",
  field:"#fb923c",module:"#a78bfa",endpoint:"#94a3b8",access_group:"#f472b6",
  access_role:"#e11d48",connector:"#2dd4bf",concept:"#e879f9",pattern:"#fbbf24"
};
let nodes=RAW.nodes.map(n=>({...n}));
let links=RAW.edges.map(e=>({...e,source:e.from,target:e.to}));
const types=[...new Set(nodes.map(n=>n.node_type))].sort();
const sel=document.getElementById("typeFilter");
types.forEach(t=>{const o=document.createElement("option");o.value=t;o.textContent=t;sel.appendChild(o);});
document.getElementById("stats").textContent=nodes.length+" nodes · "+links.length+" edges";

const svg=d3.select("#canvas");
const w=window.innerWidth,h=window.innerHeight-48;
svg.attr("width",w).attr("height",h);
const g=svg.append("g");
const zoom=d3.zoom().scaleExtent([0.05,4]).on("zoom",e=>g.attr("transform",e.transform));
svg.call(zoom);

const sim=d3.forceSimulation(nodes)
  .force("link",d3.forceLink(links).id(d=>d.id).distance(d=>d.edge_type==="similar_to"?120:55).strength(0.4))
  .force("charge",d3.forceManyBody().strength(-160))
  .force("center",d3.forceCenter(w/2,h/2))
  .force("collision",d3.forceCollide(16));

const link=g.append("g").selectAll("line").data(links).join("line")
  .attr("stroke",d=>d.edge_type==="similar_to"?"${linkSimilar}":"${linkColor}")
  .attr("stroke-width",d=>d.edge_type==="similar_to"?0.5:1.2)
  .attr("stroke-dasharray",d=>d.edge_type==="similar_to"?"3,3":null)
  .attr("opacity",0.7);

const node=g.append("g").selectAll("circle").data(nodes).join("circle")
  .attr("r",d=>d.node_type==="case_type"?10:d.node_type==="module"?7:5)
  .attr("fill",d=>NC[d.node_type]||"#718096")
  .attr("stroke","${nodeBorder}").attr("stroke-width",1.5)
  .call(d3.drag()
    .on("start",(e,d)=>{if(!e.active)sim.alphaTarget(0.3).restart();d.fx=d.x;d.fy=d.y;})
    .on("drag",(e,d)=>{d.fx=e.x;d.fy=e.y;})
    .on("end",(e,d)=>{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}));

const label=g.append("g").selectAll("text").data(nodes).join("text")
  .text(d=>d.label.length>22?d.label.slice(0,22)+"…":d.label)
  .attr("font-size",9).attr("fill","${labelColor}").attr("dx",8).attr("dy",4);

const tip=document.getElementById("tip");
node.on("mouseover",(e,d)=>{
  tip.style.display="block";tip.style.left=(e.clientX+14)+"px";tip.style.top=(e.clientY-12)+"px";
  const c=NC[d.node_type]||"#718096";
  tip.innerHTML='<span style="background:'+c+'22;color:'+c+';padding:1px 6px;border-radius:4px;font-size:9px;font-weight:700;text-transform:uppercase">'+d.node_type+'</span><br><strong>'+d.label+'</strong>'+(d.summary?'<br><span style="color:${mutedColor}">'+d.summary+'</span>':'');
}).on("mousemove",e=>{tip.style.left=(e.clientX+14)+"px";tip.style.top=(e.clientY-12)+"px";})
  .on("mouseout",()=>{tip.style.display="none";});

sim.on("tick",()=>{
  link.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y).attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
  node.attr("cx",d=>d.x).attr("cy",d=>d.y);
  label.attr("x",d=>d.x).attr("y",d=>d.y);
});

function applyFilters(){
  const q=(document.getElementById("search").value||"").toLowerCase().trim();
  const t=document.getElementById("typeFilter").value;
  let matchCount=0;
  node.attr("opacity",d=>{
    const qMatch=!q||d.label.toLowerCase().includes(q)||(d.node_type||"").toLowerCase().includes(q);
    const tMatch=!t||d.node_type===t;
    if(qMatch&&tMatch){matchCount++;return 1;}
    return 0.06;
  });
  label.attr("opacity",d=>{
    const qMatch=!q||d.label.toLowerCase().includes(q)||(d.node_type||"").toLowerCase().includes(q);
    const tMatch=!t||d.node_type===t;
    return qMatch&&tMatch?1:0.02;
  });
  const cnt=document.getElementById("search-count");
  const nr=document.getElementById("no-results");
  if(q||t){
    cnt.textContent=matchCount+" match"+(matchCount===1?"":"es");
    nr.style.display=matchCount===0?"block":"none";
  }else{
    cnt.textContent="";
    nr.style.display="none";
  }
}

document.getElementById("search").addEventListener("input",applyFilters);
document.getElementById("typeFilter").addEventListener("change",applyFilters);

window.addEventListener("resize",()=>{
  const nw=window.innerWidth,nh=window.innerHeight-48;
  svg.attr("width",nw).attr("height",nh);
  sim.force("center",d3.forceCenter(nw/2,nh/2)).alpha(0.1).restart();
});
</script>
</body>
</html>`;
}

export default function HxGraph() {
  const { resolvedTheme } = useTheme();
  const [tab, setTab] = useState<"visualize" | "query" | "report" | "nodes">("visualize");
  const [syncing, setSyncing]       = useState(false);
  const [syncStats, setSyncStats]   = useState<SyncResult | null>(null);
  const [syncErr, setSyncErr]       = useState<string | null>(null);

  const [vizHtml, setVizHtml]       = useState<string | null>(null);
  const [vizLoading, setVizLoading] = useState(false);

  const [question, setQuestion]     = useState("");
  const [querying, setQuerying]     = useState(false);
  const [queryResult, setQueryResult] = useState<QueryResult | null>(null);

  const [report, setReport]         = useState<string | null>(null);
  const [reportLoading, setReportLoading] = useState(false);

  const [nodes, setNodes]           = useState<NodeItem[]>([]);
  const [nodeType, setNodeType]     = useState("");
  const [nodeQ, setNodeQ]           = useState("");
  const [nodesLoading, setNodesLoading] = useState(false);

  async function loadViz() {
    setVizLoading(true);
    try {
      const r = await authFetch(`${API}/export`);
      if (!r.ok) { setVizHtml(null); return; }
      const data = await r.json();
      setVizHtml(buildVizHtml(data, resolvedTheme));
    } catch { setVizHtml(null); }
    finally { setVizLoading(false); }
  }

  async function handleSync() {
    setSyncing(true); setSyncErr(null); setSyncStats(null);
    try {
      const r = await authFetch(`${API}/sync`, { method: "POST" });
      if (!r.ok) throw new Error(await r.text());
      const d = await r.json();
      setSyncStats(d);
      await loadViz();
    } catch (e: any) { setSyncErr(e.message); }
    finally { setSyncing(false); }
  }

  async function handleQuery(e: React.FormEvent) {
    e.preventDefault();
    if (!question.trim()) return;
    setQuerying(true); setQueryResult(null);
    try {
      const r = await authFetch(`${API}/query`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      if (!r.ok) throw new Error(await r.text());
      setQueryResult(await r.json());
    } catch { setQueryResult({ question, answer: "Query failed — check the API.", relevant_nodes: [] }); }
    finally { setQuerying(false); }
  }

  async function loadReport() {
    setReportLoading(true);
    try {
      const r = await authFetch(`${API}/report`);
      setReport(r.ok ? await r.text() : "Failed to load report.");
    } catch { setReport("Failed to load report."); }
    finally { setReportLoading(false); }
  }

  async function downloadGraphJson() {
    try {
      const r = await authFetch(`${API}/export`);
      if (!r.ok) return;
      const blob = new Blob([await r.text()], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "graph.json";
      a.click();
      URL.revokeObjectURL(a.href);
    } catch { /* ignore */ }
  }

  async function loadNodes() {
    setNodesLoading(true);
    try {
      const params = new URLSearchParams();
      if (nodeType) params.set("node_type", nodeType);
      if (nodeQ)    params.set("q", nodeQ);
      params.set("limit", "200");
      const r = await authFetch(`${API}/nodes?${params}`);
      if (r.ok) setNodes((await r.json()).nodes ?? []);
    } finally { setNodesLoading(false); }
  }

  // Reload viz when tab activates or theme changes
  useEffect(() => {
    if (tab === "visualize") loadViz();
  }, [tab, resolvedTheme]);

  useEffect(() => { if (tab === "report" && !report) loadReport(); }, [tab]);
  useEffect(() => { if (tab === "nodes") loadNodes(); }, [tab, nodeType, nodeQ]);

  const TABS = [
    { id: "visualize", label: "Visualize" },
    { id: "nodes",     label: "Browse Nodes" },
    { id: "query",     label: "Ask HxNexus" },
    { id: "report",    label: "Graph Report" },
  ] as const;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>

      {/* Tab bar + sync controls */}
      <div style={{
        display: "flex", alignItems: "center",
        padding: "0 24px", background: "var(--bg-surface)",
        borderBottom: "1px solid var(--border)", flexShrink: 0, gap: 8,
      }}>
        <div style={{ display: "flex", gap: 2, flex: 1 }}>
          {TABS.map(t => (
            <button key={t.id} onClick={() => setTab(t.id as any)} style={{
              padding: "9px 16px", border: "none",
              borderBottom: tab === t.id ? "2px solid var(--accent)" : "2px solid transparent",
              background: "none", fontSize: 13, fontWeight: tab === t.id ? 700 : 400,
              color: tab === t.id ? "var(--accent)" : "var(--text-secondary)",
              cursor: "pointer",
            }}>{t.label}</button>
          ))}
        </div>

        {/* Right side: stats + sync */}
        {syncErr && <span style={{ fontSize: 11, color: "#ef4444" }}>{syncErr}</span>}
        {syncStats && (
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
            {syncStats.nodes}n · {syncStats.edges}e · {syncStats.communities}c
          </span>
        )}
        <button onClick={handleSync} disabled={syncing} style={{
          padding: "5px 14px", fontSize: 12, fontWeight: 700,
          background: "var(--accent)", color: "#fff", border: "none",
          borderRadius: 6, cursor: syncing ? "not-allowed" : "pointer",
          opacity: syncing ? 0.6 : 1, flexShrink: 0,
        }}>
          {syncing ? "Syncing…" : "⟳ Sync"}
        </button>
      </div>

      {/* Content */}
      <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>

        {/* Visualize */}
        {tab === "visualize" && (
          vizLoading ? (
            <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-muted)", fontSize: 13 }}>
              Loading graph…
            </div>
          ) : vizHtml ? (
            <iframe
              srcDoc={vizHtml}
              sandbox="allow-scripts"
              style={{ flex: 1, border: "none", width: "100%", height: "100%" }}
              title="HxGraph Visualizer"
            />
          ) : (
            <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 12, color: "var(--text-muted)", fontSize: 13 }}>
              <div>No graph data yet — run Sync to populate.</div>
              <button onClick={handleSync} disabled={syncing} style={{
                padding: "7px 18px", fontSize: 12, fontWeight: 700,
                background: "var(--accent)", color: "#fff", border: "none",
                borderRadius: 6, cursor: "pointer",
              }}>⟳ Sync Graph Now</button>
            </div>
          )
        )}

        {/* Browse Nodes */}
        {tab === "nodes" && (
          <div style={{ flex: 1, overflow: "auto", padding: 24 }}>
            <div style={{ display: "flex", gap: 10, marginBottom: 20 }}>
              <select value={nodeType} onChange={e => setNodeType(e.target.value)} style={{
                padding: "7px 10px", border: "1px solid var(--border)", borderRadius: 6,
                background: "var(--bg-surface)", color: "var(--text-primary)", fontSize: 13,
              }}>
                <option value="">All types</option>
                {NODE_TYPES.map(t => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
              <input value={nodeQ} onChange={e => setNodeQ(e.target.value)}
                placeholder="Search labels…"
                style={{
                  flex: 1, padding: "7px 12px", border: "1px solid var(--border)", borderRadius: 6,
                  background: "var(--bg-surface)", color: "var(--text-primary)", fontSize: 13,
                }}
              />
            </div>
            {nodesLoading && <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Loading…</div>}
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {nodes.map(n => (
                <div key={n.id} style={{
                  padding: "10px 14px", borderRadius: 8, background: "var(--bg-surface)",
                  border: "1px solid var(--border)",
                }}>
                  <div style={{ display: "flex", alignItems: "center", marginBottom: 3 }}>
                    {badge(n.node_type)}
                    <span style={{ fontWeight: 600, fontSize: 13, color: "var(--text-primary)" }}>{n.label}</span>
                    {n.community_id !== null && (
                      <span style={{ marginLeft: "auto", fontSize: 10, color: "var(--text-muted)" }}>
                        community {n.community_id}
                      </span>
                    )}
                  </div>
                  {n.summary && <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>{n.summary}</div>}
                </div>
              ))}
              {!nodesLoading && nodes.length === 0 && (
                <div style={{ color: "var(--text-muted)", fontSize: 13 }}>
                  No nodes found. Run ⟳ Sync first.
                </div>
              )}
            </div>
          </div>
        )}

        {/* Query */}
        {tab === "query" && (
          <div style={{ flex: 1, overflow: "auto", padding: 24, maxWidth: 800 }}>
            <div style={{ marginBottom: 20 }}>
              <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)", marginBottom: 6 }}>
                Ask HxNexus a question about this platform
              </div>
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 14 }}>
                Examples: "what case types exist?", "how does the insurance claim relate to the review stage?",
                "which modules depend on the access groups API?"
              </div>
              <form onSubmit={handleQuery} style={{ display: "flex", gap: 8 }}>
                <input value={question} onChange={e => setQuestion(e.target.value)}
                  placeholder="Ask anything about the platform…"
                  style={{
                    flex: 1, padding: "9px 12px", border: "1px solid var(--border)", borderRadius: 6,
                    background: "var(--bg-surface)", color: "var(--text-primary)", fontSize: 13,
                  }}
                />
                <button type="submit" disabled={querying || !question.trim()} style={{
                  padding: "9px 20px", fontWeight: 700, fontSize: 13,
                  background: "var(--accent)", color: "#fff", border: "none",
                  borderRadius: 6, cursor: "pointer", opacity: querying ? 0.6 : 1,
                }}>
                  {querying ? "…" : "Ask"}
                </button>
              </form>
            </div>

            {queryResult && (
              <div>
                <div style={{
                  padding: 16, borderRadius: 8, background: "var(--bg-surface)",
                  border: "1px solid var(--border)", marginBottom: 16,
                }}>
                  <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", marginBottom: 8 }}>Answer</div>
                  <div style={{ fontSize: 14, color: "var(--text-primary)", lineHeight: 1.6 }}>{queryResult.answer}</div>
                </div>
                {queryResult.relevant_nodes.length > 0 && (
                  <div>
                    <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", marginBottom: 10 }}>
                      Relevant nodes ({queryResult.relevant_nodes.length})
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                      {queryResult.relevant_nodes.map(n => (
                        <div key={n.id} style={{ padding: "8px 12px", borderRadius: 6, background: "var(--bg-surface)", border: "1px solid var(--border)" }}>
                          {badge(n.node_type)}
                          <span style={{ fontSize: 13, fontWeight: 600 }}>{n.label}</span>
                          {n.summary && <span style={{ fontSize: 12, color: "var(--text-secondary)", marginLeft: 8 }}>— {n.summary}</span>}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Report */}
        {tab === "report" && (
          <div style={{ flex: 1, overflow: "auto", padding: 24 }}>
            <div style={{ display: "flex", gap: 10, marginBottom: 16, flexWrap: "wrap" }}>
              <button onClick={loadReport} style={{
                padding: "5px 14px", fontSize: 12, fontWeight: 600,
                background: "var(--bg-surface)", border: "1px solid var(--border)",
                borderRadius: 6, cursor: "pointer", color: "var(--text-secondary)",
              }}>↻ Refresh</button>
              <button onClick={downloadGraphJson} style={{
                padding: "5px 14px", fontSize: 12, fontWeight: 600,
                background: "var(--bg-surface)", border: "1px solid var(--border)",
                borderRadius: 6, cursor: "pointer", color: "var(--text-secondary)",
              }}>↓ graph.json</button>
              <button onClick={() => {
                if (!vizHtml) return;
                const blob = new Blob([vizHtml], { type: "text/html" });
                const url = URL.createObjectURL(blob);
                window.open(url, "_blank");
                setTimeout(() => URL.revokeObjectURL(url), 5000);
              }} style={{
                padding: "5px 14px", fontSize: 12, fontWeight: 600,
                background: "var(--bg-surface)", border: "1px solid var(--border)",
                borderRadius: 6, cursor: "pointer", color: "var(--text-secondary)",
              }}>↗ Full screen</button>
            </div>
            {reportLoading && <div style={{ color: "var(--text-muted)", fontSize: 13 }}>Generating report…</div>}
            {report && (
              <pre style={{
                fontFamily: "var(--font-mono)", fontSize: 12, lineHeight: 1.7,
                color: "var(--text-primary)", whiteSpace: "pre-wrap", wordBreak: "break-word",
              }}>{report}</pre>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
