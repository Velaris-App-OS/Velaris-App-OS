// HELIX P34b — reusable component for Case Designer SLA tree link
// Use this inside PropertyPanel when editing an SLA policy.
//
// Usage:
//   <SLAPolicyTreeLink value={policy.escalation_tree_id}
//                      onChange={(id) => setPolicy({...policy, escalation_tree_id: id, use_v2: !!id})} />
import React, { useEffect, useState } from "react";

type Tree = { id: string; name: string; scope: string; is_active: boolean };

export function SLAPolicyTreeLink({
  value, onChange, caseTypeId,
}: {
  value: string | null | undefined;
  onChange: (treeId: string | null) => void;
  caseTypeId?: string;
}) {
  const [trees, setTrees] = useState<Tree[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const r = await fetch("/api/v1/escalation-trees?active_only=true", {
          headers: localStorage.getItem("helix_token") ? { Authorization: `Bearer ${localStorage.getItem("helix_token")}` } : {},
        });
        if (!r.ok) throw new Error(`${r.status}`);
        const all: Tree[] = await r.json();
        // Show global + this case type's own trees
        const filtered = all.filter(t =>
          t.scope === "global" || (caseTypeId && (t as any).case_type_id === caseTypeId)
        );
        setTrees(filtered);
      } catch (e: any) { setErr(e.message); }
    })();
  }, [caseTypeId]);

  return (
    <div style={{ marginTop: 8 }}>
      <label style={{ fontSize: 11, color: "#666", display: "block" }}>
        Escalation tree (overrides case-type default)
      </label>
      <select
        value={value || ""}
        onChange={(e) => onChange(e.target.value || null)}
        style={{ width: "100%", padding: "5px 8px", fontSize: 12, border: "1px solid #ccc", borderRadius: 3, marginTop: 2 }}
      >
        <option value="">— Use case-type default —</option>
        {trees.map(t => (
          <option key={t.id} value={t.id}>
            {t.name} ({t.scope})
          </option>
        ))}
      </select>
      {err && <div style={{ fontSize: 11, color: "#c33", marginTop: 4 }}>⚠ {err}</div>}
      <div style={{ fontSize: 11, color: "#888", marginTop: 4 }}>
        Linking a tree here automatically enables SLA v2 (escalation schedule + dynamic targets).
      </div>
    </div>
  );
}
