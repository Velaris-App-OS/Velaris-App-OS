/**
 * P57 — HxCanvas: Visual Whiteboard
 * Infinite canvas with sticky notes, shapes, text, connectors, freehand, graph embeds.
 * P57b: live HxGraph node embeds · P57c: real-time collab · P57d: PNG + BPMN export
 */
import React, { useState, useEffect, useRef, useCallback } from "react";
import { Button } from "@shared/components";
import { AiUnavailableBanner } from "@shared/components/AiUnavailableBanner";

const API = "/api/v1/hxcanvas";
function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}
function authFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  return fetch(url, { ...opts, headers: { ..._authHdr(), ...opts.headers } });
}

const WS_URL = () => {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/api/v1/hxstream/ws?event_types=canvas.item_created,canvas.item_updated,canvas.item_deleted,canvas.bulk_updated`;
};

// ── Types ─────────────────────────────────────────────────────────────────────

type ItemType = "sticky_note" | "shape" | "text" | "connector" | "freehand" | "graph_node_embed";

interface CanvasItem {
  id: string;
  board_id: string;
  type: ItemType;
  x: number;
  y: number;
  width: number;
  height: number;
  data: Record<string, any>;
  z_index: number;
  created_by?: string;
  created_at: string;
  updated_at: string;
}

interface Board {
  id: string;
  name: string;
  description?: string;
  case_id?: string;
  created_by?: string;
  created_at: string;
  updated_at: string;
  item_count: number;
  items?: CanvasItem[];
}

// ── Styles ────────────────────────────────────────────────────────────────────

const S: Record<string, React.CSSProperties> = {
  page:     { height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" },
  topbar:   { padding: "var(--space-md) var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", display: "flex", alignItems: "center", gap: 12, flexShrink: 0 },
  title:    { fontSize: 14, fontWeight: 600, margin: 0, color: "var(--text-primary)" },
  content:  { flex: 1, overflow: "auto", padding: "var(--space-xl) var(--space-2xl)" },
  grid:     { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px,1fr))", gap: "var(--space-md)" },
  card:     { background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", padding: "var(--space-md)", cursor: "pointer", transition: "box-shadow 0.15s" },
  input:    { width: "100%", padding: "7px 10px", border: "1px solid var(--border-default)", borderRadius: 4, fontSize: 13, background: "var(--bg-input)", color: "var(--text-primary)", boxSizing: "border-box" as const, marginBottom: 8 },
  toolbar:  { display: "flex", gap: 6, alignItems: "center", padding: "var(--space-sm) var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", background: "var(--bg-elevated)", flexShrink: 0 },
  toolBtn:  { padding: "5px 12px", borderRadius: 4, fontSize: 12, fontWeight: 600, border: "1px solid var(--border-default)", background: "var(--bg-elevated)", color: "var(--text-primary)", cursor: "pointer" },
  canvas:   { flex: 1, position: "relative" as const, overflow: "hidden", background: "#1a1a2e", cursor: "default" },
};

const STICKY_COLORS = ["#fde68a", "#86efac", "#93c5fd", "#f9a8d4", "#c4b5fd", "#fdba74"];

// ── BoardCard (list view) ─────────────────────────────────────────────────────

function BoardCard({ board, onOpen, onDelete }: { board: Board; onOpen: () => void; onDelete: () => void }) {
  return (
    <div style={S.card} onClick={onOpen}>
      <AiUnavailableBanner featureName="AI BPMN export" />

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)", marginBottom: 4 }}>{board.name}</div>
        <button
          style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 16, padding: 0 }}
          onClick={e => { e.stopPropagation(); onDelete(); }}
        >×</button>
      </div>
      {board.description && <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 8 }}>{board.description}</div>}
      <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
        {board.item_count} item{board.item_count !== 1 ? "s" : ""} · {new Date(board.updated_at).toLocaleDateString()}
      </div>
    </div>
  );
}

// ── Canvas Item Renderers ─────────────────────────────────────────────────────

function StickyNote({ item, onUpdate, onDelete, selected, onSelect }: {
  item: CanvasItem; onUpdate: (patch: Partial<CanvasItem>) => void;
  onDelete: () => void; selected: boolean; onSelect: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(item.data.content ?? "");
  const color = item.data.color ?? STICKY_COLORS[0];

  const handleBlur = () => {
    setEditing(false);
    onUpdate({ data: { ...item.data, content } });
  };

  return (
    <div
      style={{
        position: "absolute", left: item.x, top: item.y, width: item.width, height: item.height,
        background: color, borderRadius: 6, padding: 8, boxSizing: "border-box",
        boxShadow: selected ? "0 0 0 2px #0d9488, 0 4px 12px rgba(0,0,0,0.4)" : "0 2px 8px rgba(0,0,0,0.3)",
        cursor: "move", zIndex: item.z_index + 1, userSelect: "none",
      }}
      onClick={e => { e.stopPropagation(); onSelect(); }}
      onDoubleClick={() => setEditing(true)}
    >
      {editing ? (
        <textarea
          autoFocus
          style={{ width: "100%", height: "100%", border: "none", background: "transparent", resize: "none", fontSize: 12, fontFamily: "inherit", outline: "none", color: "#1f2937" }}
          value={content}
          onChange={e => setContent(e.target.value)}
          onBlur={handleBlur}
          onClick={e => e.stopPropagation()}
        />
      ) : (
        <div style={{ fontSize: 12, color: "#1f2937", whiteSpace: "pre-wrap", wordBreak: "break-word", height: "100%", overflow: "hidden" }}>
          {content || <span style={{ opacity: 0.5 }}>Double-click to edit</span>}
        </div>
      )}
      {selected && (
        <button
          style={{ position: "absolute", top: -8, right: -8, width: 18, height: 18, borderRadius: "50%", background: "#ef4444", border: "none", color: "#fff", fontSize: 11, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", lineHeight: 1 }}
          onClick={e => { e.stopPropagation(); onDelete(); }}
        >×</button>
      )}
    </div>
  );
}

function ShapeItem({ item, onUpdate, onDelete, selected, onSelect }: {
  item: CanvasItem; onUpdate: (patch: Partial<CanvasItem>) => void;
  onDelete: () => void; selected: boolean; onSelect: () => void;
}) {
  const shape = item.data.shape_type ?? "rect";
  const color = item.data.color ?? "#0d9488";
  const label = item.data.label ?? "";

  const shapeEl = shape === "circle"
    ? <ellipse cx={item.width / 2} cy={item.height / 2} rx={item.width / 2 - 2} ry={item.height / 2 - 2} fill={color + "33"} stroke={color} strokeWidth={2} />
    : shape === "diamond"
    ? <polygon
        points={`${item.width / 2},4 ${item.width - 4},${item.height / 2} ${item.width / 2},${item.height - 4} 4,${item.height / 2}`}
        fill={color + "33"} stroke={color} strokeWidth={2}
      />
    : <rect x={2} y={2} width={item.width - 4} height={item.height - 4} rx={4} fill={color + "33"} stroke={color} strokeWidth={2} />;

  return (
    <div
      style={{
        position: "absolute", left: item.x, top: item.y, width: item.width, height: item.height,
        boxShadow: selected ? `0 0 0 2px #0d9488` : "none",
        cursor: "move", zIndex: item.z_index + 1, userSelect: "none",
      }}
      onClick={e => { e.stopPropagation(); onSelect(); }}
    >
      <svg width={item.width} height={item.height} style={{ position: "absolute", top: 0, left: 0 }}>
        {shapeEl}
      </svg>
      {label && (
        <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 12, color: "var(--text-primary)", fontWeight: 600, pointerEvents: "none" }}>
          {label}
        </div>
      )}
      {selected && (
        <button
          style={{ position: "absolute", top: -8, right: -8, width: 18, height: 18, borderRadius: "50%", background: "#ef4444", border: "none", color: "#fff", fontSize: 11, cursor: "pointer" }}
          onClick={e => { e.stopPropagation(); onDelete(); }}
        >×</button>
      )}
    </div>
  );
}

function TextItem({ item, onUpdate, onDelete, selected, onSelect }: {
  item: CanvasItem; onUpdate: (patch: Partial<CanvasItem>) => void;
  onDelete: () => void; selected: boolean; onSelect: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(item.data.content ?? "");
  const fontSize = item.data.fontSize ?? 16;

  return (
    <div
      style={{
        position: "absolute", left: item.x, top: item.y, minWidth: item.width, minHeight: item.height,
        outline: selected ? "2px solid #0d9488" : "none",
        cursor: "move", zIndex: item.z_index + 1, userSelect: "none",
        padding: 4,
      }}
      onClick={e => { e.stopPropagation(); onSelect(); }}
      onDoubleClick={() => setEditing(true)}
    >
      {editing ? (
        <input
          autoFocus
          style={{ fontSize, background: "transparent", border: "none", outline: "1px solid #0d9488", color: "var(--text-primary)", fontWeight: 700, padding: 2 }}
          value={content}
          onChange={e => setContent(e.target.value)}
          onBlur={() => { setEditing(false); onUpdate({ data: { ...item.data, content } }); }}
          onClick={e => e.stopPropagation()}
        />
      ) : (
        <span style={{ fontSize, color: "var(--text-primary)", fontWeight: 700 }}>{content || "Text"}</span>
      )}
      {selected && (
        <button
          style={{ position: "absolute", top: -8, right: -8, width: 18, height: 18, borderRadius: "50%", background: "#ef4444", border: "none", color: "#fff", fontSize: 11, cursor: "pointer" }}
          onClick={e => { e.stopPropagation(); onDelete(); }}
        >×</button>
      )}
    </div>
  );
}

function ConnectorItem({ item, allItems }: { item: CanvasItem; allItems: CanvasItem[] }) {
  const fromId = item.data.from_item_id;
  const toId   = item.data.to_item_id;
  const from   = allItems.find(i => i.id === fromId);
  const to     = allItems.find(i => i.id === toId);
  if (!from || !to) return null;

  const x1 = from.x + from.width / 2;
  const y1 = from.y + from.height / 2;
  const x2 = to.x + to.width / 2;
  const y2 = to.y + to.height / 2;
  const color = item.data.color ?? "#94a3b8";

  return (
    <svg style={{ position: "absolute", top: 0, left: 0, pointerEvents: "none", zIndex: item.z_index + 1, overflow: "visible" }} width="99999" height="99999">
      <defs>
        <marker id={`arrow-${item.id}`} markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
          <path d="M0,0 L0,6 L8,3 z" fill={color} />
        </marker>
      </defs>
      <line x1={x1} y1={y1} x2={x2} y2={y2} stroke={color} strokeWidth={2} markerEnd={`url(#arrow-${item.id})`} />
      {item.data.label && (
        <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 6} fill={color} fontSize={11} textAnchor="middle">{item.data.label}</text>
      )}
    </svg>
  );
}

function FreehandItem({ item }: { item: CanvasItem }) {
  const points: [number, number][] = item.data.points ?? [];
  if (points.length < 2) return null;
  const color = item.data.color ?? "#94a3b8";
  const sw = item.data.strokeWidth ?? 2;
  const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${p[0]},${p[1]}`).join(" ");

  return (
    <svg style={{ position: "absolute", top: 0, left: 0, pointerEvents: "none", zIndex: item.z_index + 1, overflow: "visible" }} width="99999" height="99999">
      <path d={d} stroke={color} strokeWidth={sw} fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ── GraphNodeEmbed ────────────────────────────────────────────────────────────

function GraphNodeEmbed({ item, selected, onSelect, onDelete }: {
  item: CanvasItem; selected: boolean; onSelect: () => void; onDelete: () => void;
}) {
  const live = (item.data as any)._live as { name?: string; node_type?: string; summary?: string } | undefined;
  const name    = live?.name    ?? item.data.node_label ?? "Graph Node";
  const type    = live?.node_type ?? item.data.node_type ?? "";
  const summary = live?.summary ?? "";

  return (
    <div
      style={{
        position: "absolute", left: item.x, top: item.y, width: item.width, height: item.height,
        background: "#1e1b4b", border: `2px solid ${selected ? "#818cf8" : "#0d9488"}`,
        borderRadius: 8, padding: "8px 10px", boxSizing: "border-box", cursor: "move",
        boxShadow: selected ? "0 0 0 2px #818cf8" : "none", zIndex: item.z_index + 1,
      }}
      onClick={e => { e.stopPropagation(); onSelect(); }}
    >
      <div style={{ fontSize: 10, color: "#818cf8", fontWeight: 700, textTransform: "uppercase", marginBottom: 2 }}>⬡ {type}</div>
      <div style={{ fontSize: 13, color: "#e0e7ff", fontWeight: 700, marginBottom: 4 }}>{name}</div>
      {summary && <div style={{ fontSize: 10, color: "#a5b4fc", lineHeight: 1.3, overflow: "hidden", maxHeight: 40 }}>{summary}</div>}
      {selected && (
        <button
          style={{ position: "absolute", top: -8, right: -8, width: 18, height: 18, borderRadius: "50%", background: "#ef4444", border: "none", color: "#fff", fontSize: 11, cursor: "pointer" }}
          onClick={e => { e.stopPropagation(); onDelete(); }}
        >×</button>
      )}
    </div>
  );
}

// ── Canvas Editor ─────────────────────────────────────────────────────────────

type Tool = "select" | "sticky" | "shape" | "text" | "connector" | "freehand" | "graph_node";

function CanvasEditor({ board, onBack }: { board: Board; onBack: () => void }) {
  const [items, setItems]           = useState<CanvasItem[]>(board.items ?? []);
  const [activeTool, setActiveTool] = useState<Tool>("select");
  const [selected, setSelected]     = useState<string | null>(null);
  const [offset, setOffset]         = useState({ x: 0, y: 0 });
  const [scale, setScale]           = useState(1);
  const [dragging, setDragging]     = useState<{ id: string; ox: number; oy: number } | null>(null);
  const [connFrom, setConnFrom]     = useState<string | null>(null);
  const [freePoints, setFreePoints] = useState<[number, number][]>([]);
  const [drawing, setDrawing]       = useState(false);
  const [stickyColorIdx, setStickyColorIdx] = useState(0);
  const [shapeType, setShapeType]   = useState<"rect" | "circle" | "diamond">("rect");
  // P57b — graph node embed
  const [graphSearch, setGraphSearch] = useState("");
  const [graphResults, setGraphResults] = useState<any[]>([]);
  const [graphSearching, setGraphSearching] = useState(false);
  const [pendingGraphNode, setPendingGraphNode] = useState<any | null>(null);
  // P57c — real-time collab WS
  const wsRef = useRef<WebSocket | null>(null);
  const selfActorRef = useRef<string>("");
  // P57d — export state
  const [exporting, setExporting] = useState(false);
  // Right-click pan
  const [panning, setPanning] = useState<{ sx: number; sy: number } | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const itemsLayerRef = useRef<HTMLDivElement>(null);

  const toCanvas = useCallback((clientX: number, clientY: number) => {
    const rect = canvasRef.current!.getBoundingClientRect();
    return {
      x: (clientX - rect.left - offset.x) / scale,
      y: (clientY - rect.top  - offset.y) / scale,
    };
  }, [offset, scale]);

  // Escape cancels connector mode
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setConnFrom(null); setActiveTool("select"); setPendingGraphNode(null); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // P57c — real-time collab: subscribe to HxStream WS for canvas events
  useEffect(() => {
    let destroyed = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    const connect = () => {
      if (destroyed) return;
      ws = new WebSocket(WS_URL());
      wsRef.current = ws;
      ws.onopen  = () => {};
      ws.onclose = () => { if (!destroyed) reconnectTimer = setTimeout(connect, 3000); };
      ws.onerror = () => ws?.close();
      ws.onmessage = (msg) => {
        try {
          const evt = JSON.parse(msg.data);
          if (!evt.event_type?.startsWith("canvas.")) return;
          const payload = evt.payload ?? {};
          if (payload.board_id !== board.id) return;
          if (evt.actor_user_id && evt.actor_user_id === selfActorRef.current) return;
          if (evt.event_type === "canvas.item_created" || evt.event_type === "canvas.item_updated") {
            const item = payload.item as CanvasItem;
            if (!item) return;
            setItems(prev => {
              const exists = prev.find(i => i.id === item.id);
              return exists ? prev.map(i => i.id === item.id ? item : i) : [...prev, item];
            });
          } else if (evt.event_type === "canvas.item_deleted") {
            const itemId = payload.item_id as string;
            if (itemId) setItems(prev => prev.filter(i => i.id !== itemId));
          } else if (evt.event_type === "canvas.bulk_updated") {
            authFetch(`${API}/boards/${board.id}`).then(r => r.ok ? r.json() : null).then(b => {
              if (b?.items && !destroyed) setItems(b.items);
            });
          }
        } catch { /* ignore */ }
      };
    };
    connect();
    const ping = setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: "ping" })), 20000);
    return () => {
      destroyed = true;
      clearInterval(ping);
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (ws) { ws.onclose = null; ws.onerror = null; ws.close(); ws = null; }
    };
  }, [board.id]);

  // P57b — graph node search
  useEffect(() => {
    if (!graphSearch.trim() || graphSearch.length < 2) { setGraphResults([]); return; }
    const t = setTimeout(async () => {
      setGraphSearching(true);
      const r = await authFetch(`${API}/graph-nodes/search?q=${encodeURIComponent(graphSearch)}`);
      if (r.ok) setGraphResults(await r.json());
      setGraphSearching(false);
    }, 300);
    return () => clearTimeout(t);
  }, [graphSearch]);

  // P57d — PNG export using native Canvas API (no external dependencies)
  const exportPNG = useCallback(() => {
    setExporting(true);
    try {
      const PADDING = 40;
      const nonConnectors = items.filter(i => i.type !== "connector" && i.type !== "freehand");
      if (nonConnectors.length === 0) { setExporting(false); return; }

      const minX = Math.min(...nonConnectors.map(i => i.x)) - PADDING;
      const minY = Math.min(...nonConnectors.map(i => i.y)) - PADDING;
      const maxX = Math.max(...nonConnectors.map(i => i.x + i.width)) + PADDING;
      const maxY = Math.max(...nonConnectors.map(i => i.y + i.height)) + PADDING;
      const W = Math.max(maxX - minX, 200);
      const H = Math.max(maxY - minY, 200);

      const cv = document.createElement("canvas");
      cv.width = W; cv.height = H;
      const ctx = cv.getContext("2d")!;

      // Background
      ctx.fillStyle = "#1a1a2e";
      ctx.fillRect(0, 0, W, H);

      // Dot grid
      ctx.fillStyle = "rgba(255,255,255,0.06)";
      for (let gx = 0; gx < W; gx += 24) for (let gy = 0; gy < H; gy += 24) {
        ctx.beginPath(); ctx.arc(gx, gy, 1, 0, Math.PI * 2); ctx.fill();
      }

      // Draw items sorted by z_index
      const sorted = [...items].sort((a, b) => a.z_index - b.z_index);
      for (const item of sorted) {
        const x = item.x - minX;
        const y = item.y - minY;
        const w = item.width;
        const h = item.height;
        ctx.save();

        if (item.type === "sticky_note") {
          ctx.fillStyle = item.data.color ?? "#fde68a";
          ctx.shadowColor = "rgba(0,0,0,0.3)"; ctx.shadowBlur = 6;
          ctx.beginPath();
          ctx.roundRect?.(x, y, w, h, 6) ?? ctx.rect(x, y, w, h);
          ctx.fill();
          ctx.shadowBlur = 0;
          ctx.fillStyle = "#1f2937";
          ctx.font = "12px sans-serif";
          ctx.fillText(item.data.content ?? "", x + 8, y + 20, w - 16);
        } else if (item.type === "shape") {
          const color = item.data.color ?? "#0d9488";
          ctx.strokeStyle = color;
          ctx.fillStyle = color + "33";
          ctx.lineWidth = 2;
          const st = item.data.shape_type ?? "rect";
          if (st === "circle") {
            ctx.beginPath(); ctx.ellipse(x + w / 2, y + h / 2, w / 2 - 2, h / 2 - 2, 0, 0, Math.PI * 2);
            ctx.fill(); ctx.stroke();
          } else if (st === "diamond") {
            ctx.beginPath();
            ctx.moveTo(x + w / 2, y + 4); ctx.lineTo(x + w - 4, y + h / 2);
            ctx.lineTo(x + w / 2, y + h - 4); ctx.lineTo(x + 4, y + h / 2);
            ctx.closePath(); ctx.fill(); ctx.stroke();
          } else {
            ctx.beginPath(); ctx.roundRect?.(x + 2, y + 2, w - 4, h - 4, 4) ?? ctx.rect(x + 2, y + 2, w - 4, h - 4);
            ctx.fill(); ctx.stroke();
          }
          if (item.data.label) {
            ctx.fillStyle = "#e2e8f0"; ctx.font = "bold 12px sans-serif"; ctx.textAlign = "center";
            ctx.fillText(item.data.label, x + w / 2, y + h / 2 + 4);
            ctx.textAlign = "left";
          }
        } else if (item.type === "text") {
          ctx.fillStyle = "#e2e8f0";
          ctx.font = `bold ${item.data.fontSize ?? 16}px sans-serif`;
          ctx.fillText(item.data.content ?? "Text", x, y + (item.data.fontSize ?? 16));
        } else if (item.type === "graph_node_embed") {
          ctx.fillStyle = "#1e1b4b";
          ctx.strokeStyle = "#0d9488"; ctx.lineWidth = 2;
          ctx.beginPath(); ctx.roundRect?.(x, y, w, h, 8) ?? ctx.rect(x, y, w, h);
          ctx.fill(); ctx.stroke();
          ctx.fillStyle = "#818cf8"; ctx.font = "bold 10px sans-serif";
          ctx.fillText((item.data.node_type ?? "node").toUpperCase(), x + 8, y + 16);
          ctx.fillStyle = "#e0e7ff"; ctx.font = "bold 13px sans-serif";
          ctx.fillText(item.data.node_label ?? "Graph Node", x + 8, y + 34, w - 16);
        } else if (item.type === "connector") {
          const from = items.find(i => i.id === item.data.from_item_id);
          const to   = items.find(i => i.id === item.data.to_item_id);
          if (from && to) {
            const x1 = from.x + from.width / 2 - minX;
            const y1 = from.y + from.height / 2 - minY;
            const x2 = to.x + to.width / 2 - minX;
            const y2 = to.y + to.height / 2 - minY;
            ctx.strokeStyle = item.data.color ?? "#94a3b8";
            ctx.lineWidth = 2;
            ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
            // Arrow head
            const angle = Math.atan2(y2 - y1, x2 - x1);
            ctx.fillStyle = item.data.color ?? "#94a3b8";
            ctx.beginPath();
            ctx.moveTo(x2, y2);
            ctx.lineTo(x2 - 10 * Math.cos(angle - 0.4), y2 - 10 * Math.sin(angle - 0.4));
            ctx.lineTo(x2 - 10 * Math.cos(angle + 0.4), y2 - 10 * Math.sin(angle + 0.4));
            ctx.closePath(); ctx.fill();
          }
        } else if (item.type === "freehand") {
          const pts: [number, number][] = item.data.points ?? [];
          if (pts.length > 1) {
            ctx.strokeStyle = item.data.color ?? "#94a3b8";
            ctx.lineWidth = item.data.strokeWidth ?? 2;
            ctx.lineCap = "round"; ctx.lineJoin = "round";
            ctx.beginPath();
            ctx.moveTo(pts[0][0] - minX, pts[0][1] - minY);
            for (let pi = 1; pi < pts.length; pi++) ctx.lineTo(pts[pi][0] - minX, pts[pi][1] - minY);
            ctx.stroke();
          }
        }
        ctx.restore();
      }

      const url = cv.toDataURL("image/png");
      const a = document.createElement("a");
      a.href = url; a.download = `${board.name}.png`; a.click();
    } finally {
      setExporting(false);
    }
  }, [board.name, items]);

  const exportBPMN = useCallback(async () => {
    setExporting(true);
    try {
      const r = await authFetch(`${API}/boards/${board.id}/export/bpmn`, { method: "POST" });
      if (!r.ok) { alert("BPMN export failed (LLM may be unavailable)"); return; }
      const { bpmn_xml, board_name } = await r.json();
      const blob = new Blob([bpmn_xml], { type: "application/xml" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `${board_name}.bpmn`; a.click();
      URL.revokeObjectURL(url);
    } finally {
      setExporting(false);
    }
  }, [board.id, board.name]);

  const postItem = async (data: Omit<CanvasItem, "id" | "board_id" | "created_by" | "created_at" | "updated_at">) => {
    const r = await authFetch(`${API}/boards/${board.id}/items`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (r.ok) {
      const item = await r.json() as CanvasItem;
      setItems(prev => [...prev, item]);
    }
  };

  const patchItem = async (id: string, patch: Partial<CanvasItem>) => {
    const r = await authFetch(`${API}/boards/${board.id}/items/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (r.ok) {
      const updated = await r.json() as CanvasItem;
      setItems(prev => prev.map(i => i.id === id ? updated : i));
    }
  };

  const removeItem = async (id: string) => {
    await authFetch(`${API}/boards/${board.id}/items/${id}`, { method: "DELETE" });
    setItems(prev => prev.filter(i => i.id !== id));
    if (selected === id) setSelected(null);
  };

  const handleCanvasClick = useCallback((e: React.MouseEvent) => {
    if (activeTool === "select") { setSelected(null); return; }
    const { x, y } = toCanvas(e.clientX, e.clientY);

    if (activeTool === "sticky") {
      postItem({ type: "sticky_note", x, y, width: 160, height: 120, data: { color: STICKY_COLORS[stickyColorIdx], content: "" }, z_index: items.length });
    } else if (activeTool === "shape") {
      postItem({ type: "shape", x, y, width: 120, height: 80, data: { shape_type: shapeType, color: "#0d9488", label: "" }, z_index: items.length });
    } else if (activeTool === "text") {
      postItem({ type: "text", x, y, width: 120, height: 40, data: { content: "Text", fontSize: 18 }, z_index: items.length });
    } else if (activeTool === "graph_node" && pendingGraphNode) {
      postItem({ type: "graph_node_embed", x, y, width: 200, height: 80, data: { graph_node_id: pendingGraphNode.id, node_label: pendingGraphNode.label, node_type: pendingGraphNode.node_type }, z_index: items.length });
      setPendingGraphNode(null);
      setActiveTool("select");
    }
  }, [activeTool, toCanvas, items.length, stickyColorIdx, shapeType, pendingGraphNode]);

  const handleMouseDown = (e: React.MouseEvent, itemId: string) => {
    if (activeTool !== "select") return;
    e.stopPropagation();
    const { x, y } = toCanvas(e.clientX, e.clientY);
    const item = items.find(i => i.id === itemId);
    if (!item) return;
    setDragging({ id: itemId, ox: x - item.x, oy: y - item.y });
    setSelected(itemId);
  };

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (panning) {
      setOffset({ x: e.clientX - panning.sx, y: e.clientY - panning.sy });
      return;
    }
    if (dragging) {
      const { x, y } = toCanvas(e.clientX, e.clientY);
      setItems(prev => prev.map(i =>
        i.id === dragging.id ? { ...i, x: x - dragging.ox, y: y - dragging.oy } : i
      ));
    }
    if (drawing && activeTool === "freehand") {
      const { x, y } = toCanvas(e.clientX, e.clientY);
      setFreePoints(prev => [...prev, [x, y]]);
    }
  }, [panning, dragging, toCanvas, drawing, activeTool]);

  const handleMouseUp = useCallback(async (e: React.MouseEvent) => {
    if (e.button === 2) { setPanning(null); return; }
    if (dragging) {
      const item = items.find(i => i.id === dragging.id);
      if (item) await patchItem(dragging.id, { x: item.x, y: item.y });
      setDragging(null);
    }
    if (drawing && activeTool === "freehand" && freePoints.length > 1) {
      setDrawing(false);
      await postItem({ type: "freehand", x: 0, y: 0, width: 0, height: 0, data: { points: freePoints, color: "#94a3b8", strokeWidth: 2 }, z_index: items.length });
      setFreePoints([]);
    }
  }, [dragging, items, drawing, activeTool, freePoints, panning]);

  const handleCanvasMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button === 2) {
      e.preventDefault();
      setPanning({ sx: e.clientX - offset.x, sy: e.clientY - offset.y });
      return;
    }
    if (activeTool === "freehand") {
      e.preventDefault();
      const { x, y } = toCanvas(e.clientX, e.clientY);
      setFreePoints([[x, y]]);
      setDrawing(true);
    }
  }, [offset, activeTool, toCanvas]);

  const handleFreehandStart = (e: React.MouseEvent) => {
    if (activeTool !== "freehand") return;
    e.preventDefault();
    const { x, y } = toCanvas(e.clientX, e.clientY);
    setFreePoints([[x, y]]);
    setDrawing(true);
  };

  const handleConnectorClick = (itemId: string) => {
    if (activeTool !== "connector") return;
    if (!connFrom) {
      setConnFrom(itemId);
    } else if (connFrom !== itemId) {
      postItem({ type: "connector", x: 0, y: 0, width: 0, height: 0, data: { from_item_id: connFrom, to_item_id: itemId, label: "", color: "#94a3b8" }, z_index: 0 });
      setConnFrom(null);
    }
  };

  const handleWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    setScale(s => Math.min(3, Math.max(0.2, s - e.deltaY * 0.001)));
  };

  const renderItem = (item: CanvasItem) => {
    const sel = selected === item.id;
    const isConnFrom = connFrom === item.id;

    // onMouseDown fires before any child onClick stopPropagation — safe for connector
    const wrapMouseDown = (e: React.MouseEvent) => {
      if (activeTool === "connector") {
        e.stopPropagation();
        handleConnectorClick(item.id);
        return;
      }
      handleMouseDown(e, item.id);
    };

    if (item.type === "connector") return <ConnectorItem key={item.id} item={item} allItems={items} />;
    if (item.type === "freehand")  return <FreehandItem  key={item.id} item={item} />;

    const commonProps = {
      selected: sel,
      onSelect: () => setSelected(item.id),
      onDelete: () => removeItem(item.id),
      onUpdate: (patch: Partial<CanvasItem>) => patchItem(item.id, patch),
    };

    // Ring is positioned at the item's actual canvas coordinates so it frames the item
    const connRing = isConnFrom ? (
      <div style={{
        position: "absolute",
        left: item.x - 5, top: item.y - 5,
        width: item.width + 10, height: item.height + 10,
        borderRadius: 10, border: "2px dashed #f59e0b",
        pointerEvents: "none", zIndex: 9999,
        boxShadow: "0 0 0 1px #f59e0b44",
      }} />
    ) : null;

    if (item.type === "graph_node_embed") {
      return (
        <div key={item.id} style={{ position: "absolute", left: 0, top: 0 }} onMouseDown={wrapMouseDown}>
          {connRing}
          <GraphNodeEmbed item={item} selected={sel} onSelect={() => setSelected(item.id)} onDelete={() => removeItem(item.id)} />
        </div>
      );
    }

    const el = item.type === "sticky_note" ? <StickyNote key={item.id} item={item} {...commonProps} />
             : item.type === "shape"        ? <ShapeItem  key={item.id} item={item} {...commonProps} />
             : item.type === "text"         ? <TextItem   key={item.id} item={item} {...commonProps} />
             : null;

    if (!el) return null;
    return (
      <div key={item.id} style={{ position: "absolute", left: 0, top: 0 }} onMouseDown={wrapMouseDown}>
        {connRing}
        {el}
      </div>
    );
  };

  const TOOLS: { id: Tool; label: string }[] = [
    { id: "select",      label: "↖ Select" },
    { id: "sticky",      label: "📝 Note" },
    { id: "shape",       label: "⬜ Shape" },
    { id: "text",        label: "T Text" },
    { id: "connector",   label: "→ Connect" },
    { id: "freehand",    label: "✏ Draw" },
    { id: "graph_node",  label: "⬡ Graph Node" },
  ];

  return (
    <div style={S.page}>
      <div style={S.topbar}>
        <button onClick={onBack} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 20, lineHeight: 1 }}>←</button>
        <h1 style={S.title}>{board.name}</h1>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginLeft: 4 }}>{items.length} items</div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>Zoom: {Math.round(scale * 100)}%</span>
          <button style={S.toolBtn} onClick={() => setScale(1)}>Reset</button>
          <button style={S.toolBtn} onClick={exportPNG} disabled={exporting}>PNG</button>
          <button style={S.toolBtn} onClick={exportBPMN} disabled={exporting}>BPMN</button>
        </div>
      </div>

      <div style={S.toolbar}>
        {TOOLS.map(t => (
          <button
            key={t.id}
            style={{ ...S.toolBtn, background: activeTool === t.id ? "var(--accent)" : "var(--bg-elevated)", color: activeTool === t.id ? "#fff" : "var(--text-primary)", borderColor: activeTool === t.id ? "var(--accent)" : "var(--border-default)" }}
            onClick={() => { setActiveTool(t.id as Tool); setConnFrom(null); setPendingGraphNode(null); }}
          >
            {t.label}
          </button>
        ))}
        {activeTool === "sticky" && (
          <>
            <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 8 }}>Color:</span>
            {STICKY_COLORS.map((c, i) => (
              <div key={c} onClick={() => setStickyColorIdx(i)} style={{ width: 18, height: 18, borderRadius: 3, background: c, cursor: "pointer", border: stickyColorIdx === i ? "2px solid #0d9488" : "2px solid transparent" }} />
            ))}
          </>
        )}
        {activeTool === "shape" && (
          <>
            <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 8 }}>Shape:</span>
            {(["rect", "circle", "diamond"] as const).map(s => (
              <button key={s} style={{ ...S.toolBtn, background: shapeType === s ? "var(--accent)" : "var(--bg-elevated)", color: shapeType === s ? "#fff" : "var(--text-primary)" }} onClick={() => setShapeType(s)}>{s}</button>
            ))}
          </>
        )}
        {activeTool === "connector" && !connFrom && (
          <span style={{ marginLeft: 8, fontSize: 12, color: "var(--text-muted)" }}>Click a note or shape to start the arrow</span>
        )}
        {connFrom && (
          <span style={{ marginLeft: 8, fontSize: 12, color: "#f59e0b", fontWeight: 600 }}>⟶ Now click the destination item — or press Esc to cancel</span>
        )}
        {activeTool === "graph_node" && (
          <div style={{ display: "flex", gap: 6, alignItems: "center", marginLeft: 8, position: "relative" }}>
            <span style={{ fontSize: 11, color: "var(--text-muted)", whiteSpace: "nowrap" }}>Embeds a live HxGraph node that auto-updates:</span>
            <input
              style={{ ...S.input, width: 200, marginBottom: 0, fontSize: 12 }}
              placeholder="Search by name…"
              value={graphSearch}
              onChange={e => setGraphSearch(e.target.value)}
              autoFocus
            />
            {graphSearching && <span style={{ fontSize: 11, color: "var(--text-muted)" }}>…</span>}
            {pendingGraphNode && <span style={{ fontSize: 12, color: "#22c55e" }}>✓ {pendingGraphNode.label} — click canvas to place</span>}
            {graphResults.length > 0 && !pendingGraphNode && (
              <div style={{ position: "absolute", top: "100%", left: 0, background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: 6, zIndex: 9999, minWidth: 280, maxHeight: 220, overflowY: "auto", boxShadow: "0 4px 16px rgba(0,0,0,0.4)" }}>
                {graphResults.map((n: any) => (
                  <div key={n.id} style={{ padding: "8px 12px", cursor: "pointer", borderBottom: "1px solid var(--border-subtle)", fontSize: 12 }}
                    onClick={() => { setPendingGraphNode(n); setGraphResults([]); setGraphSearch(""); }}>
                    <div style={{ fontWeight: 700 }}>{n.label}</div>
                    <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{n.node_type}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div
        ref={canvasRef}
        style={{ ...S.canvas,
          cursor: panning ? "grabbing"
                : activeTool === "freehand" ? "crosshair"
                : activeTool === "select" ? "default"
                : "crosshair",
        }}
        onClick={handleCanvasClick}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseDown={handleCanvasMouseDown}
        onContextMenu={e => e.preventDefault()}
        onWheel={handleWheel}
      >
        {/* Grid dots */}
        <svg style={{ position: "absolute", top: 0, left: 0, width: "100%", height: "100%", pointerEvents: "none" }}>
          <defs>
            <pattern id="grid" width={24 * scale} height={24 * scale} patternUnits="userSpaceOnUse" x={offset.x % (24 * scale)} y={offset.y % (24 * scale)}>
              <circle cx={1} cy={1} r={0.8} fill="#ffffff18" />
            </pattern>
          </defs>
          <rect width="100%" height="100%" fill="url(#grid)" />
        </svg>

        {/* Items layer */}
        <div ref={itemsLayerRef} style={{ position: "absolute", top: 0, left: 0, transformOrigin: "0 0", transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})` }}>
          {items.map(renderItem)}
          {/* Live freehand preview */}
          {drawing && freePoints.length > 1 && (
            <svg style={{ position: "absolute", top: 0, left: 0, pointerEvents: "none" }} width="99999" height="99999">
              <polyline points={freePoints.map(p => p.join(",")).join(" ")} stroke="#94a3b8" strokeWidth={2} fill="none" strokeLinecap="round" />
            </svg>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Board List ────────────────────────────────────────────────────────────────

export default function HxCanvas() {
  const [boards, setBoards]       = useState<Board[]>([]);
  const [loading, setLoading]     = useState(true);
  const [activeBoard, setActiveBoard] = useState<Board | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm]           = useState({ name: "", description: "" });
  const [saving, setSaving]       = useState(false);

  const load = async () => {
    setLoading(true);
    const r = await authFetch(`${API}/boards`);
    if (r.ok) setBoards(await r.json());
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  const openBoard = async (board: Board) => {
    const r = await authFetch(`${API}/boards/${board.id}`);
    if (r.ok) setActiveBoard(await r.json());
  };

  const createBoard = async () => {
    if (!form.name.trim()) return;
    setSaving(true);
    const r = await authFetch(`${API}/boards`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form),
    });
    if (r.ok) {
      setShowCreate(false);
      setForm({ name: "", description: "" });
      await load();
    }
    setSaving(false);
  };

  const deleteBoard = async (id: string) => {
    if (!confirm("Delete this canvas?")) return;
    await authFetch(`${API}/boards/${id}`, { method: "DELETE" });
    await load();
  };

  if (activeBoard) {
    return <CanvasEditor board={activeBoard} onBack={() => { setActiveBoard(null); load(); }} />;
  }

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", width: "100%", height: "100%", overflow: "auto", boxSizing: "border-box" }}>
      {/* Action bar — Work Center format */}
      <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", marginBottom: "var(--space-xl)" }}>
        <Button onClick={() => setShowCreate(true)}>+ New Canvas</Button>
      </div>

      {showCreate && (
        <div style={{ background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", padding: "var(--space-lg)", marginBottom: "var(--space-xl)", maxWidth: 400 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)", marginBottom: "var(--space-md)" }}>New Canvas</div>
          <input style={S.input} placeholder="Name" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} autoFocus />
          <input style={S.input} placeholder="Description (optional)" value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} />
          <div style={{ display: "flex", gap: "var(--space-sm)" }}>
            <Button onClick={createBoard} disabled={saving}>{saving ? "Creating…" : "Create"}</Button>
            <button style={{ ...S.toolBtn }} onClick={() => setShowCreate(false)}>Cancel</button>
          </div>
        </div>
      )}

      {loading ? (
        <div style={{ color: "var(--text-muted)", padding: 40, textAlign: "center" }}>Loading canvases…</div>
      ) : boards.length === 0 ? (
        <div style={{ color: "var(--text-muted)", padding: 60, textAlign: "center" }}>
          <div style={{ fontSize: 40, marginBottom: 12 }}>🎨</div>
          <div style={{ fontSize: 16, fontWeight: 600, color: "var(--text-primary)", marginBottom: 4 }}>No canvases yet</div>
          <div style={{ fontSize: 13 }}>Create your first visual whiteboard</div>
        </div>
      ) : (
        <div style={S.grid}>
          {boards.map(b => (
            <BoardCard key={b.id} board={b} onOpen={() => openBoard(b)} onDelete={() => deleteBoard(b.id)} />
          ))}
        </div>
      )}
    </div>
  );
}
