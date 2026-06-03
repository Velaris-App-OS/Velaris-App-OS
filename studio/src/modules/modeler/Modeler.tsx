import React, { useState, useRef, useCallback, useEffect } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { Button } from "@shared/components";
import { deployProcess, getProcessDetail, listProcesses, listForms } from "@shared/api/client";
import type { BpmnElement, BpmnConnection, BpmnElementType } from "@shared/types";

/* ═══════════════════════════════════════════════════════════════════
   BPMN Modeler — Custom SVG editor
   
   Features:
     - Drag-and-drop from palette
     - Connect elements with BPMN rules
     - Undo/Redo (Ctrl+Z / Ctrl+Shift+Z)
     - Copy/Paste (Ctrl+C / Ctrl+V)
     - Auto-layout
     - Delete elements and connections
     - Deploy to engine
   ═══════════════════════════════════════════════════════════════════ */

let nextId = 1;
function genId(type: string) { return `${type}_${nextId++}`; }

const ELEMENT_DEFAULTS: Record<BpmnElementType, { width: number; height: number; label: string }> = {
  startEvent: { width: 36, height: 36, label: "Start" },
  endEvent: { width: 36, height: 36, label: "End" },
  userTask: { width: 120, height: 70, label: "User Task" },
  serviceTask: { width: 120, height: 70, label: "Service Task" },
  scriptTask: { width: 120, height: 70, label: "Script Task" },
  sendTask: { width: 120, height: 70, label: "Send Task" },
  exclusiveGateway: { width: 44, height: 44, label: "XOR" },
  parallelGateway: { width: 44, height: 44, label: "AND" },
  inclusiveGateway: { width: 44, height: 44, label: "OR" },
};

// ── History for undo/redo ────────────────────────────────────────

interface Snapshot {
  elements: BpmnElement[];
  connections: BpmnConnection[];
}

function useHistory(initial: Snapshot) {
  const [state, setState] = useState({ history: [initial], index: 0 });

  const current = state.history[state.index] || initial;

  const push = useCallback((snap: Snapshot) => {
    setState((prev) => {
      const newHist = prev.history.slice(0, prev.index + 1);
      newHist.push(snap);
      if (newHist.length > 50) newHist.shift();
      return { history: newHist, index: newHist.length - 1 };
    });
  }, []);

  const undo = useCallback(() => {
    setState((prev) => ({
      ...prev,
      index: Math.max(0, prev.index - 1),
    }));
  }, []);

  const redo = useCallback(() => {
    setState((prev) => ({
      ...prev,
      index: Math.min(prev.history.length - 1, prev.index + 1),
    }));
  }, []);

  const canUndo = state.index > 0;
  const canRedo = state.index < state.history.length - 1;

  return { current, push, undo, redo, canUndo, canRedo };
}


// ── Parse BPMN XML back into canvas elements ──────────────────
function parseBpmnXml(xml: string): { elements: BpmnElement[]; connections: BpmnConnection[]; processId: string; processName: string } {
  const parser = new DOMParser();
  const doc = parser.parseFromString(xml, "text/xml");
  const ns = "http://www.omg.org/spec/BPMN/20100524/MODEL";
  const proc = doc.getElementsByTagNameNS(ns, "process")[0] || doc.querySelector("process");
  if (!proc) return { elements: [], connections: [], processId: "unknown", processName: "Unknown" };
  const pid = proc.getAttribute("id") || "unknown";
  const pname = proc.getAttribute("name") || pid;
  const elements: BpmnElement[] = [];
  const connections: BpmnConnection[] = [];
  const tagTypes: Record<string, BpmnElementType> = {
    startEvent: "startEvent", endEvent: "endEvent", userTask: "userTask",
    serviceTask: "serviceTask", scriptTask: "scriptTask", sendTask: "sendTask",
    exclusiveGateway: "exclusiveGateway", parallelGateway: "parallelGateway", inclusiveGateway: "inclusiveGateway",
  };
  let idx = 0;
  for (const child of Array.from(proc.children)) {
    const tag = child.localName;
    if (tag === "sequenceFlow") {
      const condEl = child.getElementsByTagNameNS(ns, "conditionExpression")[0] || child.querySelector("conditionExpression");
      connections.push({
        id: child.getAttribute("id") || `flow_${idx}`,
        sourceId: child.getAttribute("sourceRef") || "",
        targetId: child.getAttribute("targetRef") || "",
        name: child.getAttribute("name") || undefined,
        condition: condEl?.textContent?.trim() || undefined,
      });
      continue;
    }
    const type = tagTypes[tag];
    if (!type) continue;
    const defaults = ELEMENT_DEFAULTS[type];
    const row = Math.floor(idx / 4);
    const col = idx % 4;
    elements.push({
      id: child.getAttribute("id") || `el_${idx}`,
      type,
      name: child.getAttribute("name") || defaults.label,
      x: 100 + col * 200,
      y: 100 + row * 120,
      width: defaults.width,
      height: defaults.height,
      properties: {
        ...(child.getAttribute("implementation") ? { implementation: child.getAttribute("implementation")! } : {}),
        ...(child.getAttribute("formKey") ? { formKey: child.getAttribute("formKey")! } : {}),
      },
    });
    idx++;
  }
  return { elements, connections, processId: pid, processName: pname };
}
// ── Demo process: Loan Approval (for HxFusion demo) ─────────────
const DEMO_LOAN_BPMN = `<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="loan_approval_demo" name="Loan Approval Demo" isExecutable="true">
    <startEvent id="start" name="Application Received">
      <outgoing>f1</outgoing>
    </startEvent>
    <sequenceFlow id="f1" sourceRef="start" targetRef="t_collect"/>
    <userTask id="t_collect" name="Collect Applicant Details" formKey="loan-application-form">
      <incoming>f1</incoming>
      <outgoing>f2</outgoing>
    </userTask>
    <sequenceFlow id="f2" sourceRef="t_collect" targetRef="t_credit"/>
    <serviceTask id="t_credit" name="AI Credit Check" implementation="helix://hxnexus/credit-score">
      <incoming>f2</incoming>
      <outgoing>f3</outgoing>
    </serviceTask>
    <sequenceFlow id="f3" sourceRef="t_credit" targetRef="gw1"/>
    <exclusiveGateway id="gw1" name="Score OK?">
      <incoming>f3</incoming>
      <outgoing>f4</outgoing>
      <outgoing>f5</outgoing>
    </exclusiveGateway>
    <sequenceFlow id="f4" sourceRef="gw1" targetRef="t_approve" name="Approved">
      <conditionExpression>credit_score &gt;= 700</conditionExpression>
    </sequenceFlow>
    <sequenceFlow id="f5" sourceRef="gw1" targetRef="t_review" name="Manual Review">
      <conditionExpression>credit_score &lt; 700</conditionExpression>
    </sequenceFlow>
    <serviceTask id="t_approve" name="Send Approval Notification" implementation="helix://hxbridge/notify">
      <incoming>f4</incoming>
      <outgoing>f6</outgoing>
    </serviceTask>
    <userTask id="t_review" name="Manual Review by Loan Officer" formKey="loan-manual-review-form">
      <incoming>f5</incoming>
      <outgoing>f7</outgoing>
    </userTask>
    <sequenceFlow id="f6" sourceRef="t_approve" targetRef="end"/>
    <sequenceFlow id="f7" sourceRef="t_review" targetRef="end"/>
    <endEvent id="end" name="Process Complete">
      <incoming>f6</incoming>
      <incoming>f7</incoming>
    </endEvent>
  </process>
</definitions>`;

export default function Modeler() {
  const hist = useHistory({ elements: [], connections: [] });
  const elements = hist.current.elements;
  const connections = hist.current.connections;
  const navigate = useNavigate();

  const setElements = (fn: (prev: BpmnElement[]) => BpmnElement[]) => {
    const newEls = fn(elements);
    hist.push({ elements: newEls, connections });
  };
  const setConnections = (fn: (prev: BpmnConnection[]) => BpmnConnection[]) => {
    const newConns = fn(connections);
    hist.push({ elements, connections: newConns });
  };
  const setBoth = (els: BpmnElement[], conns: BpmnConnection[]) => {
    hist.push({ elements: els, connections: conns });
  };

  const [selected, setSelected] = useState<string | null>(null);
  const [connecting, setConnecting] = useState<string | null>(null);
  const [processName, setProcessName] = useState("New Process");
  const [processId, setProcessId] = useState("new_process");
  const [deployStatus, setDeployStatus] = useState<string | null>(null);
  const [clipboard, setClipboard] = useState<BpmnElement[]>([]);
  const [showOpenModal, setShowOpenModal] = useState(false);
  const [availableForms, setAvailableForms] = useState<Array<{ id: string; name: string; version: string }>>([]);

  // Load forms once for the UserTask form key picker
  useEffect(() => {
    listForms().then((data: any) => {
      setAvailableForms(data.items ?? []);
    }).catch(() => {});
  }, []);

  // ── Load existing process for editing ────────────────────────
  const [searchParams] = useSearchParams();
  const editProcessId = searchParams.get("edit");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!editProcessId || loaded) return;
    setLoaded(true);
    getProcessDetail(editProcessId).then((data) => {
      if (data.bpmn_xml) {
        const parsed = parseBpmnXml(data.bpmn_xml);
        hist.push({ elements: parsed.elements, connections: parsed.connections });
        setProcessName(parsed.processName);
        setProcessId(parsed.processId);
      }
    }).catch((err) => console.error("Failed to load process:", err));
  }, [editProcessId]);

  const loadDemo = useCallback(() => {
    const parsed = parseBpmnXml(DEMO_LOAN_BPMN);
    setBoth(parsed.elements, parsed.connections);
    setProcessName(parsed.processName);
    setProcessId(parsed.processId);
  }, []);

  const svgRef = useRef<SVGSVGElement>(null);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(1);
  const [dragging, setDragging] = useState<{ id: string; offsetX: number; offsetY: number } | null>(null);
  const [panning, setPanning] = useState<{ startX: number; startY: number; panX: number; panY: number } | null>(null);

  const selectedElement = elements.find((e) => e.id === selected) || null;
  const selectedConnection = connections.find((c) => c.id === selected) || null;

  // ── Drop from palette ────────────────────────────────────────

  const handleCanvasDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const type = e.dataTransfer.getData("bpmn-type") as BpmnElementType;
    if (!type) return;
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const defaults = ELEMENT_DEFAULTS[type];
    const x = (e.clientX - rect.left - pan.x) / zoom - defaults.width / 2;
    const y = (e.clientY - rect.top - pan.y) / zoom - defaults.height / 2;
    const id = genId(type);
    const newEl: BpmnElement = {
      id, type, name: defaults.label,
      x: Math.round(x / 20) * 20, y: Math.round(y / 20) * 20,
      width: defaults.width, height: defaults.height, properties: {},
    };
    hist.push({ elements: [...elements, newEl], connections });
    setSelected(id);
  }, [pan, zoom, elements, connections, hist]);

  // ── Element dragging ─────────────────────────────────────────

  const handleElementMouseDown = useCallback((e: React.MouseEvent, el: BpmnElement) => {
    e.stopPropagation();
    if (connecting) {
      const sourceEl = elements.find((e) => e.id === connecting);
      if (connecting !== el.id && sourceEl) {
        if (sourceEl.type === "endEvent") { setConnecting(null); return; }
        if (el.type === "startEvent") { setConnecting(null); return; }
        const exists = connections.some((c) => c.sourceId === connecting && c.targetId === el.id);
        if (!exists) {
          const connId = genId("flow");
          hist.push({ elements, connections: [...connections, { id: connId, sourceId: connecting, targetId: el.id }] });
        }
      }
      setConnecting(null);
      return;
    }
    setSelected(el.id);
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    setDragging({
      id: el.id,
      offsetX: (e.clientX - rect.left - pan.x) / zoom - el.x,
      offsetY: (e.clientY - rect.top - pan.y) / zoom - el.y,
    });
  }, [connecting, pan, zoom, elements, connections, hist]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (dragging) {
      const rect = svgRef.current?.getBoundingClientRect();
      if (!rect) return;
      const x = (e.clientX - rect.left - pan.x) / zoom - dragging.offsetX;
      const y = (e.clientY - rect.top - pan.y) / zoom - dragging.offsetY;
      // Direct state mutation for smooth dragging (pushed to history on mouseUp)
      const el = elements.find((e) => e.id === dragging.id);
      if (el) { el.x = Math.round(x / 20) * 20; el.y = Math.round(y / 20) * 20; }
      // Force re-render
      hist.push({ elements: [...elements], connections });
    } else if (panning) {
      setPan({
        x: panning.panX + (e.clientX - panning.startX),
        y: panning.panY + (e.clientY - panning.startY),
      });
    }
  }, [dragging, panning, pan, zoom, elements, connections]);

  const handleMouseUp = useCallback(() => {
    if (dragging) {
      // Snapshot already pushed during drag
      setDragging(null);
    }
    setPanning(null);
  }, [dragging]);

  // ── Canvas interactions ──────────────────────────────────────

  const handleCanvasClick = useCallback((e: React.MouseEvent) => {
    if (e.target === svgRef.current || (e.target as Element)?.tagName === "rect") {
      if (!connecting) setSelected(null);
      setConnecting(null);
    }
  }, [connecting]);

  const handleCanvasMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.target === svgRef.current || (e.target as Element)?.tagName === "rect") {
      if (e.button === 0 && !connecting) {
        setPanning({ startX: e.clientX, startY: e.clientY, panX: pan.x, panY: pan.y });
      }
    }
  }, [connecting, pan]);

  // ── Keyboard shortcuts ───────────────────────────────────────

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const inInput = document.activeElement?.tagName === "INPUT" || document.activeElement?.tagName === "TEXTAREA";

      // Delete
      if ((e.key === "Delete" || e.key === "Backspace") && !inInput && selected) {
        const isConn = connections.some((c) => c.id === selected);
        if (isConn) {
          hist.push({ elements, connections: connections.filter((c) => c.id !== selected) });
        } else {
          hist.push({
            elements: elements.filter((el) => el.id !== selected),
            connections: connections.filter((c) => c.sourceId !== selected && c.targetId !== selected),
          });
        }
        setSelected(null);
      }

      // Undo
      if (e.key === "z" && (e.ctrlKey || e.metaKey) && !e.shiftKey) {
        e.preventDefault();
        hist.undo();
      }

      // Redo
      if (e.key === "z" && (e.ctrlKey || e.metaKey) && e.shiftKey) {
        e.preventDefault();
        hist.redo();
      }
      if (e.key === "y" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        hist.redo();
      }

      // Copy
      if (e.key === "c" && (e.ctrlKey || e.metaKey) && !inInput && selected) {
        const el = elements.find((e) => e.id === selected);
        if (el) setClipboard([el]);
      }

      // Paste
      if (e.key === "v" && (e.ctrlKey || e.metaKey) && !inInput && clipboard.length > 0) {
        const pasted = clipboard.map((el) => ({
          ...el,
          id: genId(el.type),
          x: el.x + 40,
          y: el.y + 40,
          properties: { ...el.properties },
        }));
        hist.push({ elements: [...elements, ...pasted], connections });
        setSelected(pasted[0].id);
      }

      // Escape
      if (e.key === "Escape") {
        setConnecting(null);
        setSelected(null);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [selected, elements, connections, hist, clipboard]);

  // ── Auto-layout ──────────────────────────────────────────────

  const autoLayout = useCallback(() => {
    if (elements.length === 0) return;

    // Topological sort based on connections
    const inDegree = new Map<string, number>();
    const adj = new Map<string, string[]>();
    elements.forEach((el) => { inDegree.set(el.id, 0); adj.set(el.id, []); });
    connections.forEach((c) => {
      adj.get(c.sourceId)?.push(c.targetId);
      inDegree.set(c.targetId, (inDegree.get(c.targetId) || 0) + 1);
    });

    // BFS layers
    const layers: string[][] = [];
    let queue = [...inDegree.entries()].filter(([, d]) => d === 0).map(([id]) => id);
    const visited = new Set<string>();

    while (queue.length > 0) {
      layers.push(queue);
      const next: string[] = [];
      for (const id of queue) {
        visited.add(id);
        for (const target of adj.get(id) || []) {
          inDegree.set(target, (inDegree.get(target) || 0) - 1);
          if (inDegree.get(target) === 0 && !visited.has(target)) next.push(target);
        }
      }
      queue = next;
    }

    // Add disconnected elements to last layer
    const remaining = elements.filter((el) => !visited.has(el.id));
    if (remaining.length > 0) layers.push(remaining.map((el) => el.id));

    // Position elements
    const xGap = 200;
    const yGap = 100;
    const startX = 80;
    const startY = 80;

    const newElements = elements.map((el) => {
      for (let layerIdx = 0; layerIdx < layers.length; layerIdx++) {
        const posInLayer = layers[layerIdx].indexOf(el.id);
        if (posInLayer !== -1) {
          const layerSize = layers[layerIdx].length;
          return {
            ...el,
            x: startX + layerIdx * xGap,
            y: startY + posInLayer * yGap - ((layerSize - 1) * yGap) / 2 + 200,
          };
        }
      }
      return el;
    });

    hist.push({ elements: newElements, connections });
    // Reset view
    setPan({ x: 0, y: 0 });
    setZoom(1);
  }, [elements, connections, hist]);

  // ── Deploy ───────────────────────────────────────────────────

  const handleDeploy = async () => {
    const xml = generateBpmnXml(processId, processName, elements, connections);
    setDeployStatus("deploying");
    try {
      await deployProcess(xml, processName);
      setDeployStatus("success");
      setTimeout(() => setDeployStatus(null), 3000);
    } catch (err: any) {
      setDeployStatus(`error: ${err.message}`);
      setTimeout(() => setDeployStatus(null), 5000);
    }
  };

  // ── Update selected element ──────────────────────────────────

  const updateElement = useCallback((updates: Partial<BpmnElement>) => {
    if (!selected) return;
    hist.push({
      elements: elements.map((el) => (el.id === selected ? { ...el, ...updates } : el)),
      connections,
    });
  }, [selected, elements, connections, hist]);

  return (
    <div style={{ display: "flex", height: "100%", overflow: "hidden" }}>
      <Palette onConnect={() => selected && setConnecting(selected)} connecting={!!connecting} />

      <div style={{ flex: 1, position: "relative", overflow: "hidden" }}
        onDragOver={(e) => e.preventDefault()} onDrop={handleCanvasDrop}>

        {/* Toolbar */}
        <div style={{ position: "absolute", top: "var(--space-md)", left: "var(--space-md)", right: "var(--space-md)", display: "flex", justifyContent: "space-between", alignItems: "center", zIndex: 10, pointerEvents: "none" }}>
          <div style={{ display: "flex", gap: 8, pointerEvents: "auto" }}>
            <input value={processName}
              onChange={(e) => { setProcessName(e.target.value); setProcessId(e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "_")); }}
              style={{ padding: "8px 14px", background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", fontFamily: "var(--font-display)", fontWeight: 600, fontSize: 14, width: 200 }}
            />
            <span style={{ alignSelf: "center", fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-muted)" }}>{processId}</span>
          </div>

          <div style={{ display: "flex", gap: 6, alignItems: "center", pointerEvents: "auto" }}>
            {deployStatus && (
              <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: deployStatus === "success" ? "var(--status-completed)" : deployStatus === "deploying" ? "var(--status-running)" : "var(--status-failed)" }}>
                {deployStatus === "success" ? "✓ Deployed" : deployStatus === "deploying" ? "Deploying…" : deployStatus}
              </span>
            )}
            <Button variant="ghost" size="sm" onClick={hist.undo} disabled={!hist.canUndo}>↩ Undo</Button>
            <Button variant="ghost" size="sm" onClick={hist.redo} disabled={!hist.canRedo}>↪ Redo</Button>
            <Button variant="secondary" size="sm" onClick={autoLayout} disabled={elements.length === 0}>⊞ Layout</Button>
            <Button variant="secondary" size="sm" onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); }}>Reset</Button>
            <Button variant="secondary" size="sm" onClick={() => setShowOpenModal(true)}>📂 Open</Button>
            <Button variant="secondary" size="sm" onClick={loadDemo}>⚡ Demo</Button>
            <Button size="sm" onClick={handleDeploy} disabled={elements.length === 0}>Deploy</Button>
          </div>
        </div>

        {/* SVG Canvas */}
        <svg ref={svgRef}
          style={{ width: "100%", height: "100%", background: "var(--canvas-bg)", cursor: connecting ? "crosshair" : panning ? "grabbing" : "default" }}
          onClick={handleCanvasClick} onMouseDown={handleCanvasMouseDown}
          onMouseMove={handleMouseMove} onMouseUp={handleMouseUp}
          onWheel={(e) => { const d = e.deltaY > 0 ? 0.9 : 1.1; setZoom((z) => Math.min(3, Math.max(0.3, z * d))); }}>
          <defs>
            <pattern id="grid-small" width="20" height="20" patternUnits="userSpaceOnUse">
              <path d="M 20 0 L 0 0 0 20" fill="none" stroke="var(--canvas-grid)" strokeWidth="0.5" />
            </pattern>
            <pattern id="grid-large" width="100" height="100" patternUnits="userSpaceOnUse">
              <rect width="100" height="100" fill="url(#grid-small)" />
              <path d="M 100 0 L 0 0 0 100" fill="none" stroke="var(--canvas-grid-major)" strokeWidth="1" />
            </pattern>
          </defs>
          <g transform={`translate(${pan.x},${pan.y}) scale(${zoom})`}>
            <rect x="-5000" y="-5000" width="10000" height="10000" fill="url(#grid-large)" />
            {connections.map((conn) => {
              const s = elements.find((e) => e.id === conn.sourceId);
              const t = elements.find((e) => e.id === conn.targetId);
              if (!s || !t) return null;
              return <ConnectionLine key={conn.id} source={s} target={t} selected={selected === conn.id}
                onClick={(e) => { e.stopPropagation(); setSelected(conn.id); }} />;
            })}
            {elements.map((el) => (
              <BpmnShape key={el.id} element={el} selected={selected === el.id} connecting={connecting === el.id}
                onMouseDown={(e) => handleElementMouseDown(e, el)} />
            ))}
          </g>
        </svg>

        {/* Status bar */}
        <div style={{ position: "absolute", bottom: "var(--space-md)", left: "var(--space-md)", fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-muted)", background: "var(--bg-panel)", padding: "4px 10px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)" }}>
          {elements.length} elements · {connections.length} flows · {Math.round(zoom * 100)}%
          {connecting && " · Click target to connect"} · Ctrl+Z undo · Ctrl+C/V copy/paste
        </div>
      </div>

      <PropertiesPanel element={selectedElement} connection={selectedConnection}
        availableForms={availableForms}
        onUpdate={updateElement}
        onStartConnect={() => selected && setConnecting(selected)}
        onDelete={() => {
          if (!selected) return;
          const isConn = connections.some((c) => c.id === selected);
          if (isConn) {
            hist.push({ elements, connections: connections.filter((c) => c.id !== selected) });
          } else {
            hist.push({
              elements: elements.filter((e) => e.id !== selected),
              connections: connections.filter((c) => c.sourceId !== selected && c.targetId !== selected),
            });
          }
          setSelected(null);
        }}
        onUpdateConnection={(updates) => {
          if (!selected) return;
          hist.push({
            elements,
            connections: connections.map((c) => c.id === selected ? { ...c, ...updates } : c),
          });
        }}
      />

      {showOpenModal && (
        <OpenProcessModal
          onClose={() => setShowOpenModal(false)}
          onOpen={(pid) => {
            setShowOpenModal(false);
            navigate(`/modeler?edit=${pid}`);
            setLoaded(false);
          }}
        />
      )}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Palette
   ═══════════════════════════════════════════════════════════════════ */

const PALETTE_GROUPS = [
  { label: "Events", items: [{ type: "startEvent" as const, label: "Start" }, { type: "endEvent" as const, label: "End" }] },
  { label: "Tasks", items: [{ type: "userTask" as const, label: "User Task" }, { type: "serviceTask" as const, label: "Service Task" }, { type: "scriptTask" as const, label: "Script Task" }, { type: "sendTask" as const, label: "Send Task" }] },
  { label: "Gateways", items: [{ type: "exclusiveGateway" as const, label: "Exclusive (XOR)" }, { type: "parallelGateway" as const, label: "Parallel (AND)" }, { type: "inclusiveGateway" as const, label: "Inclusive (OR)" }] },
];

function Palette({ onConnect, connecting }: { onConnect: () => void; connecting: boolean }) {
  return (
    <div style={{ width: 180, minWidth: 180, background: "var(--bg-panel)", borderRight: "1px solid var(--border-subtle)", padding: "var(--space-md)", paddingTop: 60, overflow: "auto" }}>
      {PALETTE_GROUPS.map((group) => (
        <div key={group.label} style={{ marginBottom: "var(--space-lg)" }}>
          <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: "var(--space-sm)" }}>{group.label}</div>
          {group.items.map((item) => (
            <div key={item.type} draggable onDragStart={(e) => e.dataTransfer.setData("bpmn-type", item.type)}
              style={{ padding: "8px 10px", fontSize: 12, color: "var(--text-secondary)", cursor: "grab", borderRadius: "var(--radius-sm)", marginBottom: 2, transition: "background 0.1s", display: "flex", alignItems: "center", gap: 8 }}
              onMouseEnter={(e) => (e.currentTarget.style.background = "var(--bg-card-hover)")}
              onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
              <MiniIcon type={item.type} />{item.label}
            </div>
          ))}
        </div>
      ))}
      <div style={{ borderTop: "1px solid var(--border-subtle)", paddingTop: "var(--space-md)" }}>
        <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: "var(--space-sm)" }}>Tools</div>
        <Button variant={connecting ? "primary" : "secondary"} size="sm" onClick={onConnect} style={{ width: "100%" }}>
          {connecting ? "Click target…" : "→ Connect"}
        </Button>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Open Process Modal
   ═══════════════════════════════════════════════════════════════════ */

function OpenProcessModal({ onClose, onOpen }: { onClose: () => void; onOpen: (pid: string) => void }) {
  const [processes, setProcesses] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    listProcesses().then((data: any) => {
      setProcesses(data.processes || []);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, []);

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", width: 480, maxHeight: "70vh", display: "flex", flexDirection: "column" }}>
        <div style={{ padding: "var(--space-lg)", borderBottom: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between", alignItems: "center", flexShrink: 0 }}>
          <div style={{ fontFamily: "var(--font-display)", fontWeight: 600, fontSize: 15 }}>Open Process</div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 18 }}>✕</button>
        </div>
        <div style={{ flex: 1, overflowY: "auto", overflowX: "hidden", padding: "var(--space-md)", minHeight: 0 }}>
          {loading ? (
            <div style={{ padding: "var(--space-xl)", color: "var(--text-muted)" }}>Loading...</div>
          ) : processes.length === 0 ? (
            <div style={{ padding: "var(--space-xl)", color: "var(--text-muted)", fontSize: 13 }}>No deployed processes. Deploy a process first.</div>
          ) : (
            processes.map((p: any) => (
              <div key={p.process_id}
                onClick={() => onOpen(p.process_id)}
                style={{ padding: "var(--space-md)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)", marginBottom: "var(--space-sm)", cursor: "pointer", transition: "background 0.1s" }}
                onMouseEnter={e => (e.currentTarget.style.background = "var(--bg-card-hover)")}
                onMouseLeave={e => (e.currentTarget.style.background = "transparent")}
              >
                <div style={{ fontWeight: 600, fontSize: 14, color: "var(--text-primary)" }}>{p.name || p.process_id}</div>
                <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                  {p.process_id} · v{p.version} · {p.element_count} elements
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   SVG Shapes
   ═══════════════════════════════════════════════════════════════════ */

function BpmnShape({ element: el, selected, connecting, onMouseDown }: {
  element: BpmnElement; selected: boolean; connecting: boolean; onMouseDown: (e: React.MouseEvent) => void;
}) {
  const isEvent = el.type === "startEvent" || el.type === "endEvent";
  const isGateway = el.type.includes("Gateway");
  return (
    <g onMouseDown={onMouseDown} style={{ cursor: "pointer" }}>
      {isEvent ? <EventShape el={el} selected={selected} /> :
       isGateway ? <GatewayShape el={el} selected={selected} /> :
       <TaskShape el={el} selected={selected} />}
    </g>
  );
}

function EventShape({ el, selected }: { el: BpmnElement; selected: boolean }) {
  const cx = el.x + el.width / 2, cy = el.y + el.height / 2, r = el.width / 2;
  return (<>
    <circle cx={cx} cy={cy} r={r} fill={selected ? "var(--accent-dim)" : "var(--bpmn-event-fill)"}
      stroke={selected ? "var(--bpmn-selected)" : "var(--bpmn-event-stroke)"} strokeWidth={el.type === "endEvent" ? 3 : 2} />
    <text x={cx} y={cy + r + 16} textAnchor="middle" fontSize="11" fill="var(--text-secondary)" fontFamily="var(--font-body)">{el.name}</text>
  </>);
}

function TaskShape({ el, selected }: { el: BpmnElement; selected: boolean }) {
  const icons: Record<string, string> = { userTask: "👤", serviceTask: "⚙", scriptTask: "📜", sendTask: "✉" };
  return (<>
    <rect x={el.x} y={el.y} width={el.width} height={el.height} rx={8}
      fill={selected ? "var(--accent-dim)" : "var(--bpmn-task-fill)"}
      stroke={selected ? "var(--bpmn-selected)" : "var(--bpmn-task-stroke)"} strokeWidth={selected ? 2 : 1.5} />
    <text x={el.x + 10} y={el.y + 16} fontSize="12" fill="var(--text-muted)">{icons[el.type] || ""}</text>
    <text x={el.x + el.width / 2} y={el.y + el.height / 2 + 5} textAnchor="middle" fontSize="12" fontWeight="500"
      fill="var(--text-primary)" fontFamily="var(--font-body)">{el.name.length > 14 ? el.name.slice(0, 14) + "…" : el.name}</text>
  </>);
}

function GatewayShape({ el, selected }: { el: BpmnElement; selected: boolean }) {
  const cx = el.x + el.width / 2, cy = el.y + el.height / 2, s = el.width / 2;
  const sym: Record<string, string> = { exclusiveGateway: "✕", parallelGateway: "+", inclusiveGateway: "○" };
  return (<>
    <polygon points={`${cx},${cy - s} ${cx + s},${cy} ${cx},${cy + s} ${cx - s},${cy}`}
      fill={selected ? "var(--accent-dim)" : "var(--bpmn-gateway-fill)"}
      stroke={selected ? "var(--bpmn-selected)" : "var(--bpmn-gateway-stroke)"} strokeWidth={selected ? 2 : 1.5} />
    <text x={cx} y={cy + 5} textAnchor="middle" fontSize="16" fontWeight="700" fill="var(--bpmn-gateway-stroke)" fontFamily="var(--font-mono)">{sym[el.type] || "?"}</text>
    <text x={cx} y={cy + s + 16} textAnchor="middle" fontSize="11" fill="var(--text-secondary)" fontFamily="var(--font-body)">{el.name}</text>
  </>);
}

function ConnectionLine({ source, target, selected, onClick }: {
  source: BpmnElement; target: BpmnElement; selected: boolean; onClick: (e: React.MouseEvent) => void;
}) {
  const sx = source.x + source.width / 2, sy = source.y + source.height / 2;
  const tx = target.x + target.width / 2, ty = target.y + target.height / 2;
  const dx = tx - sx, dy = ty - sy, dist = Math.sqrt(dx * dx + dy * dy);
  if (dist === 0) return null;
  const nx = dx / dist, ny = dy / dist;
  const startX = sx + nx * (source.width / 2), startY = sy + ny * (source.height / 2);
  const endX = tx - nx * (target.width / 2), endY = ty - ny * (target.height / 2);
  const aSize = 8, angle = Math.atan2(endY - startY, endX - startX);
  const a1x = endX - aSize * Math.cos(angle - 0.4), a1y = endY - aSize * Math.sin(angle - 0.4);
  const a2x = endX - aSize * Math.cos(angle + 0.4), a2y = endY - aSize * Math.sin(angle + 0.4);

  return (
    <g onClick={onClick} style={{ cursor: "pointer" }}>
      <line x1={startX} y1={startY} x2={endX} y2={endY} stroke="transparent" strokeWidth="16" />
      <line x1={startX} y1={startY} x2={endX} y2={endY}
        stroke={selected ? "var(--bpmn-selected)" : "var(--bpmn-flow-stroke)"} strokeWidth={selected ? 2.5 : 1.5} />
      <polygon points={`${endX},${endY} ${a1x},${a1y} ${a2x},${a2y}`}
        fill={selected ? "var(--bpmn-selected)" : "var(--bpmn-flow-stroke)"} />
    </g>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Properties Panel
   ═══════════════════════════════════════════════════════════════════ */

function PropertiesPanel({ element, connection, availableForms, onUpdate, onStartConnect, onDelete, onUpdateConnection }: {
  element: BpmnElement | null; connection: BpmnConnection | null;
  availableForms: Array<{ id: string; name: string; version: string }>;
  onUpdate: (u: Partial<BpmnElement>) => void; onStartConnect: () => void; onDelete: () => void;
  onUpdateConnection: (u: Partial<BpmnConnection>) => void;
}) {
  if (connection) {
    return (
      <div style={{ width: 260, minWidth: 260, background: "var(--bg-panel)", borderLeft: "1px solid var(--border-subtle)", padding: "var(--space-lg)", paddingTop: 60, overflow: "auto" }}>
        <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: "var(--space-md)" }}>Connection</div>
        <PropField label="ID" value={connection.id} disabled />
        <PropField label="From" value={connection.sourceId} disabled />
        <PropField label="To" value={connection.targetId} disabled />
        <PropField label="Name" value={connection.name || ""} onChange={(v) => onUpdateConnection({ name: v })} placeholder="Flow label" />
        <PropField label="Condition" value={connection.condition || ""} onChange={(v) => onUpdateConnection({ condition: v })} placeholder="amount > 1000" />
        <div style={{ marginTop: "var(--space-lg)" }}>
          <Button variant="danger" size="sm" onClick={onDelete} style={{ width: "100%" }}>Delete Connection</Button>
        </div>
      </div>
    );
  }

  if (!element) {
    return (
      <div style={{ width: 260, minWidth: 260, background: "var(--bg-panel)", borderLeft: "1px solid var(--border-subtle)", padding: "var(--space-lg)", paddingTop: 60, color: "var(--text-muted)", fontSize: 13, display: "flex", alignItems: "center", justifyContent: "center" }}>
        Select an element or connection
      </div>
    );
  }

  return (
    <div style={{ width: 260, minWidth: 260, background: "var(--bg-panel)", borderLeft: "1px solid var(--border-subtle)", padding: "var(--space-lg)", paddingTop: 60, overflow: "auto" }}>
      <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: "var(--space-md)" }}>Properties</div>
      <PropField label="ID" value={element.id} disabled />
      <PropField label="Type" value={element.type} disabled />
      <PropField label="Name" value={element.name} onChange={(v) => onUpdate({ name: v })} />
      {(element.type === "serviceTask" || element.type === "sendTask") && (
        <ServiceTaskPanel
          implementation={element.properties.implementation || ""}
          onChange={(v) => onUpdate({ properties: { ...element.properties, implementation: v } })}
        />
      )}
      {element.type === "userTask" && (
        <FormKeyPicker
          value={element.properties.formKey || ""}
          forms={availableForms}
          onChange={(v) => onUpdate({ properties: { ...element.properties, formKey: v } })}
        />
      )}
      {element.type === "scriptTask" && (<>
        <div style={{ marginBottom: "var(--space-md)" }}>
          <label style={labelStyle}>Language</label>
          <select
            value={element.properties.language || "python"}
            onChange={(e) => onUpdate({ properties: { ...element.properties, language: e.target.value } })}
            style={{ ...inputStyle, cursor: "pointer" }}
          >
            <option value="python">Python</option>
            <option value="javascript">JavaScript</option>
            <option value="groovy">Groovy</option>
          </select>
        </div>
        <div style={{ marginBottom: "var(--space-md)" }}>
          <label style={labelStyle}>Script</label>
          <textarea
            value={element.properties.script || ""}
            onChange={(e) => onUpdate({ properties: { ...element.properties, script: e.target.value } })}
            rows={6}
            maxLength={65536}
            style={{
              ...inputStyle,
              fontFamily: "var(--font-mono)",
              resize: "vertical",
              borderColor: (element.properties.script || "").length > 65000
                ? "var(--status-failed)"
                : (element.properties.script || "").length > 16384
                ? "#f59e0b"
                : undefined,
            }}
          />
          <div style={{
            fontSize: 10,
            color: (element.properties.script || "").length > 65000
              ? "var(--status-failed)"
              : (element.properties.script || "").length > 16384
              ? "#f59e0b"
              : "var(--text-muted)",
            marginTop: 4,
            fontFamily: "var(--font-mono)",
          }}>
            {(element.properties.script || "").length}/65536 chars
            {(element.properties.script || "").length > 16384 && (element.properties.script || "").length <= 65536
              ? " — over 16 KB, consider a Service Task"
              : ""}
          </div>
        </div>
        <div style={{ padding: "8px 10px", background: "rgba(245,158,11,0.1)", border: "1px solid rgba(245,158,11,0.3)", borderRadius: "var(--radius-sm)", marginBottom: "var(--space-md)", fontSize: 11, color: "#f59e0b", lineHeight: 1.5 }}>
          Scripts run server-side. Only trusted process designers should have deploy access.
        </div>
      </>)}
      <div style={{ display: "flex", gap: 8, marginTop: "var(--space-lg)" }}>
        <Button variant="secondary" size="sm" onClick={onStartConnect} style={{ flex: 1 }}>Connect →</Button>
        <Button variant="danger" size="sm" onClick={onDelete} style={{ flex: 1 }}>Delete</Button>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Form Key Picker — selects from live Form Builder forms
   ═══════════════════════════════════════════════════════════════════ */

function FormKeyPicker({ value, forms, onChange }: {
  value: string;
  forms: Array<{ id: string; name: string; version: string }>;
  onChange: (v: string) => void;
}) {
  const [manual, setManual] = useState(false);

  // Determine if current value matches a known form id
  const selectedForm = forms.find(f => f.id === value);

  if (manual || forms.length === 0) {
    return (
      <div style={{ marginBottom: "var(--space-md)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
          <label style={labelStyle}>Form Key</label>
          {forms.length > 0 && (
            <button onClick={() => setManual(false)} style={{ background: "none", border: "none", color: "var(--accent)", fontSize: 11, cursor: "pointer", fontFamily: "var(--font-mono)" }}>
              ← pick from list
            </button>
          )}
        </div>
        <input
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder="Paste form UUID"
          style={inputStyle}
        />
        <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4, fontFamily: "var(--font-mono)" }}>
          {forms.length === 0 ? "No forms found — create one in Form Builder first." : "Pasting a form UUID from elsewhere."}
        </div>
      </div>
    );
  }

  return (
    <div style={{ marginBottom: "var(--space-md)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
        <label style={labelStyle}>Form</label>
        <button onClick={() => setManual(true)} style={{ background: "none", border: "none", color: "var(--text-muted)", fontSize: 11, cursor: "pointer", fontFamily: "var(--font-mono)" }}>
          manual entry
        </button>
      </div>
      <select
        value={value}
        onChange={e => onChange(e.target.value)}
        style={{ ...inputStyle, cursor: "pointer" }}
      >
        <option value="">— select a form —</option>
        {forms.map(f => (
          <option key={f.id} value={f.id}>{f.name} (v{f.version})</option>
        ))}
      </select>
      {selectedForm && (
        <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4, fontFamily: "var(--font-mono)", wordBreak: "break-all" }}>
          ID: {value}
        </div>
      )}
      {!selectedForm && value && (
        <div style={{ fontSize: 10, color: "#f59e0b", marginTop: 4, fontFamily: "var(--font-mono)" }}>
          ⚠ Unknown form ID — not found in Form Builder
        </div>
      )}
      <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4, lineHeight: 1.4 }}>
        Process pauses here until the form is submitted.
      </div>
    </div>
  );
}

const HELIX_SERVICE_PRESETS = [
  { label: "HxNexus — AI inference", value: "helix://hxnexus/infer" },
  { label: "HxNexus — Credit score", value: "helix://hxnexus/credit-score" },
  { label: "HxNexus — Classify document", value: "helix://hxnexus/classify" },
  { label: "HxBridge — Send notification", value: "helix://hxbridge/notify" },
  { label: "HxBridge — POST to connector", value: "helix://hxbridge/post" },
  { label: "Case Service — Create case", value: "helix://case-service/cases" },
  { label: "Case Service — Update step", value: "helix://case-service/steps/complete" },
  { label: "External HTTP — GET", value: "https://api.example.com/endpoint" },
  { label: "External HTTP — POST", value: "https://api.example.com/endpoint" },
];

function ServiceTaskPanel({ implementation, onChange }: { implementation: string; onChange: (v: string) => void }) {
  const [showPresets, setShowPresets] = useState(false);
  return (
    <div style={{ marginBottom: "var(--space-md)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
        <label style={labelStyle}>Implementation</label>
        <button onClick={() => setShowPresets(s => !s)} style={{ background: "none", border: "none", color: "var(--accent)", fontSize: 11, cursor: "pointer", fontFamily: "var(--font-mono)" }}>
          {showPresets ? "▲ hide" : "▼ presets"}
        </button>
      </div>
      <input value={implementation} onChange={e => onChange(e.target.value)}
        placeholder="helix://service/endpoint or https://..."
        style={inputStyle} />
      {showPresets && (
        <div style={{ border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", marginTop: 4, overflow: "hidden", maxHeight: 220, overflowY: "auto" }}>
          {HELIX_SERVICE_PRESETS.map((p, i) => (
            <div key={i} onClick={() => { onChange(p.value); setShowPresets(false); }}
              style={{ padding: "6px 10px", fontSize: 11, color: "var(--text-secondary)", cursor: "pointer", borderBottom: i < HELIX_SERVICE_PRESETS.length - 1 ? "1px solid var(--border-subtle)" : "none" }}
              onMouseEnter={e => (e.currentTarget.style.background = "var(--bg-card-hover)")}
              onMouseLeave={e => (e.currentTarget.style.background = "transparent")}
            >
              <div style={{ fontWeight: 500, color: "var(--text-primary)" }}>{p.label}</div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--text-muted)", marginTop: 1 }}>{p.value}</div>
            </div>
          ))}
        </div>
      )}
      <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4, fontFamily: "var(--font-mono)", lineHeight: 1.4 }}>
        helix:// → Helix internal service · https:// → External HTTP POST
      </div>
    </div>
  );
}

function PropField({ label, value, onChange, disabled, placeholder }: {
  label: string; value: string; onChange?: (v: string) => void; disabled?: boolean; placeholder?: string;
}) {
  return (
    <div style={{ marginBottom: "var(--space-md)" }}>
      <label style={labelStyle}>{label}</label>
      <input value={value} onChange={onChange ? (e) => onChange(e.target.value) : undefined} disabled={disabled} placeholder={placeholder}
        style={{ ...inputStyle, opacity: disabled ? 0.5 : 1, cursor: disabled ? "not-allowed" : "text" }} />
    </div>
  );
}

const labelStyle: React.CSSProperties = { display: "block", fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em" };
const inputStyle: React.CSSProperties = { width: "100%", padding: "8px 10px", background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", fontSize: 13, fontFamily: "var(--font-body)" };

function MiniIcon({ type }: { type: BpmnElementType }) {
  const s = 14;
  if (type === "startEvent") return <svg width={s} height={s} viewBox="0 0 14 14"><circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--bpmn-event-stroke)" strokeWidth="1.5" /></svg>;
  if (type === "endEvent") return <svg width={s} height={s} viewBox="0 0 14 14"><circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--bpmn-event-stroke)" strokeWidth="2.5" /></svg>;
  if (type.includes("Gateway")) return <svg width={s} height={s} viewBox="0 0 14 14"><polygon points="7,1 13,7 7,13 1,7" fill="none" stroke="var(--bpmn-gateway-stroke)" strokeWidth="1.5" /></svg>;
  return <svg width={s} height={s} viewBox="0 0 14 14"><rect x="1" y="2" width="12" height="10" rx="2" fill="none" stroke="var(--bpmn-task-stroke)" strokeWidth="1.5" /></svg>;
}

/* ═══════════════════════════════════════════════════════════════════
   BPMN XML Generator
   ═══════════════════════════════════════════════════════════════════ */

function generateBpmnXml(pid: string, pname: string, elements: BpmnElement[], connections: BpmnConnection[]): string {
  const L: string[] = [
    `<?xml version="1.0" encoding="UTF-8"?>`,
    `<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">`,
    `  <process id="${pid}" name="${esc(pname)}" isExecutable="true">`,
  ];
  for (const el of elements) {
    const tag = el.type;
    const a = [`id="${el.id}"`, `name="${esc(el.name)}"`];
    if ((el.type === "serviceTask" || el.type === "sendTask") && el.properties.implementation) a.push(`implementation="${esc(el.properties.implementation)}"`);
    if (el.type === "userTask" && el.properties.formKey) a.push(`formKey="${esc(el.properties.formKey)}"`);
    if (el.type === "scriptTask" && el.properties.script) {
      a.push(`scriptFormat="${el.properties.language || "python"}"`);
      L.push(`    <${tag} ${a.join(" ")}>`);
      L.push(`      <script>${esc(el.properties.script)}</script>`);
      for (const c of connections.filter((c) => c.targetId === el.id)) L.push(`      <incoming>${c.id}</incoming>`);
      for (const c of connections.filter((c) => c.sourceId === el.id)) L.push(`      <outgoing>${c.id}</outgoing>`);
      L.push(`    </${tag}>`);
      continue;
    }
    const inc = connections.filter((c) => c.targetId === el.id);
    const out = connections.filter((c) => c.sourceId === el.id);
    if (inc.length === 0 && out.length === 0) { L.push(`    <${tag} ${a.join(" ")}/>`); }
    else {
      L.push(`    <${tag} ${a.join(" ")}>`);
      for (const c of inc) L.push(`      <incoming>${c.id}</incoming>`);
      for (const c of out) L.push(`      <outgoing>${c.id}</outgoing>`);
      L.push(`    </${tag}>`);
    }
  }
  for (const c of connections) {
    const a = [`id="${c.id}"`, `sourceRef="${c.sourceId}"`, `targetRef="${c.targetId}"`];
    if (c.name) a.push(`name="${esc(c.name)}"`);
    if (c.condition) { L.push(`    <sequenceFlow ${a.join(" ")}>`); L.push(`      <conditionExpression>${esc(c.condition)}</conditionExpression>`); L.push(`    </sequenceFlow>`); }
    else L.push(`    <sequenceFlow ${a.join(" ")}/>`);
  }
  L.push(`  </process>`, `</definitions>`);
  return L.join("\n");
}

function esc(s: string): string { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
