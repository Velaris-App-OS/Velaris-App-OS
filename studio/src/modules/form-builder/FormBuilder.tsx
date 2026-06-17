import React, { useState, useCallback } from "react";
import { Button, Card, EmptyState } from "@shared/components";

/* ═══════════════════════════════════════════════════════════════════
   FormBuilder — visual drag-and-drop form designer
   
   Produces a definition_json with structure:
   {
     sections: [{ id, title, order, fields: [{ id, type, label, ... }] }]
   }
   ═══════════════════════════════════════════════════════════════════ */

export type JSONSchema = Record<string, any>;

export interface ConnectorSourceDef {
  connector_id:           string;
  input_mapping:          Record<string, string>;
  response_path:          string;
  response_normalization?: "raw" | "postgres_safe" | "mongo_safe";
  target_fields?:         Record<string, string>;
  null_policy?:           "skip" | "error" | "default";
}

export interface FormFieldDef {
  id: string;
  type: "text" | "number" | "dropdown" | "checkbox" | "date" | "textarea" | "email" | "phone" | "file_upload"
      | "radio" | "multi_select" | "rating" | "slider" | "currency" | "datetime"
      | "connector_lookup" | "connector_result" | "json_display" | "table_display";
  data_type?:    JSONSchema;
  label: string;
  field_key: string;
  required: boolean;
  placeholder?: string;
  description?: string;
  options?: { label: string; value: string }[];
  validation?: Record<string, any>;
  default_value?: any;
  null_policy?:  "skip" | "error" | "default";
  source?:       ConnectorSourceDef;
  /** Case variable to pre-fill from (e.g. "crm.account_status" or "subject").
   *  Falls back to matching the field_key against case variables. */
  variable?: string;
}

export interface FormSectionDef {
  id: string;
  title: string;
  order: number;
  fields: FormFieldDef[];
}

export interface FormDefinition {
  sections: FormSectionDef[];
  definition_version?: number;
}

export const FORM_DEFINITION_VERSION = 2;

interface FormBuilderProps {
  definition: FormDefinition;
  onChange: (def: FormDefinition) => void;
}

const FIELD_TYPES: { type: FormFieldDef["type"]; label: string; icon: string }[] = [
  { type: "text",             label: "Text",             icon: "Aa" },
  { type: "textarea",         label: "Text Area",        icon: "¶"  },
  { type: "number",           label: "Number",           icon: "#"  },
  { type: "currency",         label: "Currency",         icon: "$"  },
  { type: "email",            label: "Email",            icon: "@"  },
  { type: "phone",            label: "Phone",            icon: "☎"  },
  { type: "date",             label: "Date",             icon: "📅" },
  { type: "datetime",         label: "Date & Time",      icon: "🕐" },
  { type: "dropdown",         label: "Dropdown",         icon: "▾"  },
  { type: "radio",            label: "Radio",            icon: "⊙"  },
  { type: "multi_select",     label: "Multi Select",     icon: "☰"  },
  { type: "checkbox",         label: "Checkbox",         icon: "☑"  },
  { type: "rating",           label: "Rating",           icon: "★"  },
  { type: "slider",           label: "Slider",           icon: "⇔"  },
  { type: "file_upload",      label: "File Upload",      icon: "📎" },
  { type: "connector_lookup", label: "Connector Lookup", icon: "⚡" },
  { type: "connector_result", label: "Connector Result", icon: "↩"  },
  { type: "json_display",     label: "JSON Display",     icon: "{}" },
  { type: "table_display",    label: "Table Display",    icon: "⊞"  },
];

export default function FormBuilder({ definition, onChange }: FormBuilderProps) {
  const [selectedFieldId, setSelectedFieldId] = useState<string | null>(null);
  const [dragOverSectionId, setDragOverSectionId] = useState<string | null>(null);

  const sections = definition.sections || [];

  const update = useCallback((sections: FormSectionDef[]) => {
    onChange({ ...definition, sections });
  }, [definition, onChange]);

  // ── Section ops ──────────────────────────────────────────────
  const addSection = () => {
    const id = `section-${Date.now()}`;
    update([...sections, { id, title: `Section ${sections.length + 1}`, order: sections.length, fields: [] }]);
  };

  const updateSection = (sectionId: string, partial: Partial<FormSectionDef>) => {
    update(sections.map(s => s.id === sectionId ? { ...s, ...partial } : s));
  };

  const deleteSection = (sectionId: string) => {
    update(sections.filter(s => s.id !== sectionId).map((s, i) => ({ ...s, order: i })));
  };

  const moveSection = (sectionId: string, dir: -1 | 1) => {
    const sorted = [...sections].sort((a, b) => a.order - b.order);
    const idx = sorted.findIndex(s => s.id === sectionId);
    const swapIdx = idx + dir;
    if (swapIdx < 0 || swapIdx >= sorted.length) return;
    [sorted[idx], sorted[swapIdx]] = [sorted[swapIdx], sorted[idx]];
    update(sorted.map((s, i) => ({ ...s, order: i })));
  };

  // ── Field ops ────────────────────────────────────────────────
  const addField = (sectionId: string, type: FormFieldDef["type"]) => {
    const id = `field-${Date.now()}`;
    const label = FIELD_TYPES.find(t => t.type === type)?.label || type;
    const newField: FormFieldDef = {
      id, type, label, field_key: id.replace(/-/g, "_"),
      required: false, placeholder: "",
      ...(["dropdown", "radio", "multi_select"].includes(type)
        ? { options: [{ label: "Option 1", value: "option_1" }, { label: "Option 2", value: "option_2" }] }
        : {}),
      ...(type === "slider"   ? { validation: { min: 0, max: 100, step: 1 } } : {}),
      ...(type === "rating"   ? { validation: { max_stars: 5 } } : {}),
      ...(type === "currency" ? { validation: { symbol: "$" } } : {}),
    };
    update(sections.map(s =>
      s.id === sectionId ? { ...s, fields: [...s.fields, newField] } : s
    ));
    setSelectedFieldId(id);
  };

  const updateField = (sectionId: string, fieldId: string, partial: Partial<FormFieldDef>) => {
    update(sections.map(s =>
      s.id === sectionId
        ? { ...s, fields: s.fields.map(f => f.id === fieldId ? { ...f, ...partial } : f) }
        : s
    ));
  };

  const deleteField = (sectionId: string, fieldId: string) => {
    update(sections.map(s =>
      s.id === sectionId ? { ...s, fields: s.fields.filter(f => f.id !== fieldId) } : s
    ));
    if (selectedFieldId === fieldId) setSelectedFieldId(null);
  };

  const moveField = (sectionId: string, fieldId: string, dir: -1 | 1) => {
    update(sections.map(s => {
      if (s.id !== sectionId) return s;
      const fields = [...s.fields];
      const idx = fields.findIndex(f => f.id === fieldId);
      const swapIdx = idx + dir;
      if (swapIdx < 0 || swapIdx >= fields.length) return s;
      [fields[idx], fields[swapIdx]] = [fields[swapIdx], fields[idx]];
      return { ...s, fields };
    }));
  };

  // Find selected field
  let selectedField: FormFieldDef | null = null;
  let selectedSectionId: string | null = null;
  if (selectedFieldId) {
    for (const s of sections) {
      const f = s.fields.find(f => f.id === selectedFieldId);
      if (f) { selectedField = f; selectedSectionId = s.id; break; }
    }
  }

  return (
    <div style={{ display: "flex", gap: "var(--space-lg)", height: "100%" }}>
      {/* Left: Canvas */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {sections.length === 0 ? (
          <EmptyState
            title="No sections yet"
            description="Add a section to start building your form."
            action={<Button onClick={addSection}>+ Add Section</Button>}
          />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
            {[...sections].sort((a, b) => a.order - b.order).map((section) => (
              <SectionCard
                key={section.id}
                section={section}
                selectedFieldId={selectedFieldId}
                isDragOver={dragOverSectionId === section.id}
                onSelectField={setSelectedFieldId}
                onUpdateTitle={(title) => updateSection(section.id, { title })}
                onDelete={() => deleteSection(section.id)}
                onMove={(dir) => moveSection(section.id, dir)}
                onAddField={(type) => addField(section.id, type)}
                onDeleteField={(fid) => deleteField(section.id, fid)}
                onMoveField={(fid, dir) => moveField(section.id, fid, dir)}
                onDragOver={() => setDragOverSectionId(section.id)}
                onDragLeave={() => setDragOverSectionId(null)}
                onDrop={(type) => { addField(section.id, type); setDragOverSectionId(null); }}
              />
            ))}
            <button onClick={addSection} style={{
              padding: "var(--space-md)", border: "2px dashed var(--border-default)",
              borderRadius: "var(--radius-md)", background: "transparent",
              color: "var(--text-muted)", cursor: "pointer", fontSize: 13,
              fontFamily: "var(--font-body)", transition: "all 0.15s",
            }}
              onMouseEnter={e => { e.currentTarget.style.borderColor = "var(--accent)"; e.currentTarget.style.color = "var(--accent)"; }}
              onMouseLeave={e => { e.currentTarget.style.borderColor = "var(--border-default)"; e.currentTarget.style.color = "var(--text-muted)"; }}
            >+ Add Section</button>
          </div>
        )}
      </div>

      {/* Right: Field palette + property editor */}
      <div style={{ width: 280, display: "flex", flexDirection: "column", gap: "var(--space-md)", overflow: "auto" }}>
        {/* Field palette */}
        <div>
          <div style={{
            fontSize: 10, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase",
            letterSpacing: "0.08em", fontFamily: "var(--font-mono)", marginBottom: "var(--space-sm)",
          }}>Field Types</div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 4 }}>
            {FIELD_TYPES.map(ft => (
              <div
                key={ft.type}
                draggable
                onDragStart={e => e.dataTransfer.setData("field_type", ft.type)}
                style={{
                  padding: "8px 4px", fontSize: 10,
                  fontFamily: "var(--font-mono)", color: "var(--text-secondary)",
                  background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
                  borderRadius: "var(--radius-sm)", cursor: "grab",
                  transition: "all 0.1s",
                }}
                onMouseEnter={e => { e.currentTarget.style.borderColor = "var(--accent)"; e.currentTarget.style.color = "var(--accent)"; }}
                onMouseLeave={e => { e.currentTarget.style.borderColor = "var(--border-subtle)"; e.currentTarget.style.color = "var(--text-secondary)"; }}
              >
                <div style={{ fontSize: 16, marginBottom: 2 }}>{ft.icon}</div>
                {ft.label}
              </div>
            ))}
          </div>
        </div>

        {/* Property editor */}
        {selectedField && selectedSectionId && (
          <FieldPropertyEditor
            field={selectedField}
            onUpdate={(partial) => updateField(selectedSectionId!, selectedField!.id, partial)}
            onClose={() => setSelectedFieldId(null)}
          />
        )}
      </div>
    </div>
  );
}

/* ── Section Card ─────────────────────────────────────────────── */

function SectionCard({ section, selectedFieldId, isDragOver, onSelectField, onUpdateTitle, onDelete, onMove, onAddField, onDeleteField, onMoveField, onDragOver, onDragLeave, onDrop }: {
  section: FormSectionDef;
  selectedFieldId: string | null;
  isDragOver: boolean;
  onSelectField: (id: string) => void;
  onUpdateTitle: (title: string) => void;
  onDelete: () => void;
  onMove: (dir: -1 | 1) => void;
  onAddField: (type: FormFieldDef["type"]) => void;
  onDeleteField: (id: string) => void;
  onMoveField: (id: string, dir: -1 | 1) => void;
  onDragOver: () => void;
  onDragLeave: () => void;
  onDrop: (type: FormFieldDef["type"]) => void;
}) {
  const [editingTitle, setEditingTitle] = useState(false);

  return (
    <div
      onDragOver={e => { e.preventDefault(); onDragOver(); }}
      onDragLeave={onDragLeave}
      onDrop={e => { e.preventDefault(); const t = e.dataTransfer.getData("field_type"); if (t) onDrop(t as any); }}
      style={{
        background: "var(--bg-card)", border: `1px solid ${isDragOver ? "var(--accent)" : "var(--border-subtle)"}`,
        borderRadius: "var(--radius-md)", overflow: "hidden",
        transition: "border-color 0.15s",
      }}
    >
      {/* Section header */}
      <div style={{
        padding: "var(--space-sm) var(--space-md)",
        background: "var(--bg-elevated)", borderBottom: "1px solid var(--border-subtle)",
        display: "flex", alignItems: "center", justifyContent: "space-between",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
          <span style={{ color: "var(--text-muted)", cursor: "pointer", fontSize: 12 }} onClick={() => onMove(-1)}>▲</span>
          <span style={{ color: "var(--text-muted)", cursor: "pointer", fontSize: 12 }} onClick={() => onMove(1)}>▼</span>
          {editingTitle ? (
            <input
              autoFocus
              value={section.title}
              onChange={e => onUpdateTitle(e.target.value)}
              onBlur={() => setEditingTitle(false)}
              onKeyDown={e => e.key === "Enter" && setEditingTitle(false)}
              style={{
                background: "var(--bg-input)", border: "1px solid var(--border-focus)",
                borderRadius: "var(--radius-sm)", padding: "2px 6px", fontSize: 13,
                fontWeight: 600, color: "var(--text-primary)", outline: "none",
                fontFamily: "var(--font-display)",
              }}
            />
          ) : (
            <span
              onDoubleClick={() => setEditingTitle(true)}
              style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", fontFamily: "var(--font-display)", cursor: "text" }}
            >{section.title}</span>
          )}
          <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
            ({section.fields.length} field{section.fields.length !== 1 ? "s" : ""})
          </span>
        </div>
        <button onClick={onDelete} style={{
          background: "transparent", border: "none", color: "var(--text-muted)",
          cursor: "pointer", fontSize: 14, padding: 2, opacity: 0.5,
        }}
          onMouseEnter={e => e.currentTarget.style.opacity = "1"}
          onMouseLeave={e => e.currentTarget.style.opacity = "0.5"}
        >×</button>
      </div>

      {/* Fields */}
      <div style={{ padding: "var(--space-sm) var(--space-md)" }}>
        {section.fields.length === 0 ? (
          <div style={{ padding: "var(--space-lg)", color: "var(--text-muted)", fontSize: 12 }}>
            Drag a field type here or click below to add
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {section.fields.map((field, idx) => (
              <FieldRow
                key={field.id}
                field={field}
                selected={selectedFieldId === field.id}
                onSelect={() => onSelectField(field.id)}
                onDelete={() => onDeleteField(field.id)}
                onMoveUp={() => onMoveField(field.id, -1)}
                onMoveDown={() => onMoveField(field.id, 1)}
                isFirst={idx === 0}
                isLast={idx === section.fields.length - 1}
              />
            ))}
          </div>
        )}

        {/* Quick-add buttons */}
        <div style={{
          display: "flex", gap: 4, marginTop: "var(--space-sm)", flexWrap: "wrap",
        }}>
          {["text", "number", "dropdown", "checkbox", "textarea"].map(type => (
            <button key={type} onClick={() => onAddField(type as any)} style={{
              padding: "3px 8px", fontSize: 10, fontFamily: "var(--font-mono)",
              background: "transparent", border: "1px dashed var(--border-default)",
              borderRadius: "var(--radius-sm)", color: "var(--text-muted)",
              cursor: "pointer",
            }}
              onMouseEnter={e => e.currentTarget.style.borderColor = "var(--accent)"}
              onMouseLeave={e => e.currentTarget.style.borderColor = "var(--border-default)"}
            >+ {type}</button>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ── Field Row ────────────────────────────────────────────────── */

function FieldRow({ field, selected, onSelect, onDelete, onMoveUp, onMoveDown, isFirst, isLast }: {
  field: FormFieldDef; selected: boolean;
  onSelect: () => void; onDelete: () => void;
  onMoveUp: () => void; onMoveDown: () => void;
  isFirst: boolean; isLast: boolean;
}) {
  const icon = FIELD_TYPES.find(t => t.type === field.type)?.icon || "?";

  return (
    <div
      onClick={onSelect}
      style={{
        display: "flex", alignItems: "center", gap: "var(--space-sm)",
        padding: "6px 8px", borderRadius: "var(--radius-sm)",
        background: selected ? "var(--accent-dim)" : "transparent",
        border: selected ? "1px solid var(--accent)" : "1px solid transparent",
        cursor: "pointer", transition: "all 0.1s",
      }}
      onMouseEnter={e => !selected && (e.currentTarget.style.background = "var(--bg-card-hover)")}
      onMouseLeave={e => !selected && (e.currentTarget.style.background = "transparent")}
    >
      <span style={{ fontSize: 12, width: 20, color: "var(--text-muted)", flexShrink: 0 }}>{icon}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 12, fontWeight: 500, color: "var(--text-primary)", display: "flex", alignItems: "center", gap: 4 }}>
          {field.label}
          {field.required && <span style={{ color: "var(--status-failed)", fontSize: 10 }}>*</span>}
        </div>
        <div style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
          {field.type} · {field.field_key}
        </div>
      </div>
      <div style={{ display: "flex", gap: 2, flexShrink: 0 }}>
        {!isFirst && <MiniBtn onClick={e => { e.stopPropagation(); onMoveUp(); }}>▲</MiniBtn>}
        {!isLast && <MiniBtn onClick={e => { e.stopPropagation(); onMoveDown(); }}>▼</MiniBtn>}
        <MiniBtn onClick={e => { e.stopPropagation(); onDelete(); }} danger>×</MiniBtn>
      </div>
    </div>
  );
}

function MiniBtn({ children, onClick, danger }: { children: React.ReactNode; onClick: (e: React.MouseEvent) => void; danger?: boolean }) {
  return (
    <button onClick={onClick} style={{
      width: 18, height: 18, display: "flex", alignItems: "center", justifyContent: "center",
      background: "transparent", border: "none", borderRadius: 2,
      color: danger ? "var(--status-failed)" : "var(--text-muted)",
      cursor: "pointer", fontSize: 10, opacity: 0.5,
    }}
      onMouseEnter={e => e.currentTarget.style.opacity = "1"}
      onMouseLeave={e => e.currentTarget.style.opacity = "0.5"}
    >{children}</button>
  );
}

/* ── Field Property Editor ────────────────────────────────────── */

function FieldPropertyEditor({ field, onUpdate, onClose }: {
  field: FormFieldDef;
  onUpdate: (partial: Partial<FormFieldDef>) => void;
  onClose: () => void;
}) {
  return (
    <div style={{
      background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
      borderRadius: "var(--radius-md)", padding: "var(--space-md)",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-md)" }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", fontFamily: "var(--font-mono)" }}>
          Field Properties
        </span>
        <button onClick={onClose} style={{
          background: "transparent", border: "none", color: "var(--text-muted)",
          cursor: "pointer", fontSize: 14, padding: 2,
        }}>×</button>
      </div>

      <PropField label="Label">
        <PropInput value={field.label} onChange={v => onUpdate({ label: v })} />
      </PropField>

      <PropField label="Field Key">
        <PropInput value={field.field_key} onChange={v => onUpdate({ field_key: v })} mono />
      </PropField>

      <PropField label="Type">
        <PropSelect value={field.type} onChange={v => onUpdate({ type: v as any })}
          options={FIELD_TYPES.map(t => t.type)} labels={FIELD_TYPES.map(t => t.label)} />
      </PropField>

      <PropField label="Placeholder">
        <PropInput value={field.placeholder || ""} onChange={v => onUpdate({ placeholder: v })} />
      </PropField>

      <PropField label="Description">
        <PropInput value={field.description || ""} onChange={v => onUpdate({ description: v })} />
      </PropField>

      <PropField label="Variable Source (pre-fill, e.g. crm.account_status)">
        <PropInput value={field.variable || ""} onChange={v => onUpdate({ variable: v })} mono />
      </PropField>

      <label style={{
        display: "flex", alignItems: "center", gap: 8, fontSize: 12,
        color: "var(--text-secondary)", cursor: "pointer", margin: "var(--space-sm) 0",
      }}>
        <input type="checkbox" checked={field.required}
          onChange={e => onUpdate({ required: e.target.checked })}
          style={{ accentColor: "var(--accent)" }} />
        Required
      </label>

      {/* Options editor — dropdown, radio, multi_select */}
      {["dropdown", "radio", "multi_select"].includes(field.type) && (
        <div style={{ marginTop: "var(--space-sm)" }}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginBottom: 4 }}>Options</div>
          {(field.options || []).map((opt, i) => (
            <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4 }}>
              <PropInput value={opt.label} onChange={v => {
                const opts = [...(field.options || [])];
                opts[i] = { label: v, value: v.toLowerCase().replace(/\s+/g, "_") };
                onUpdate({ options: opts });
              }} />
              <button onClick={() => onUpdate({ options: (field.options || []).filter((_, j) => j !== i) })}
                style={{ background: "transparent", border: "none", color: "var(--status-failed)", cursor: "pointer", fontSize: 12, flexShrink: 0 }}>×</button>
            </div>
          ))}
          <button onClick={() => {
            const n = (field.options?.length || 0) + 1;
            onUpdate({ options: [...(field.options || []), { label: `Option ${n}`, value: `option_${n}` }] });
          }} style={{
            padding: "3px 8px", fontSize: 10, fontFamily: "var(--font-mono)",
            background: "transparent", border: "1px dashed var(--border-default)",
            borderRadius: "var(--radius-sm)", color: "var(--text-muted)", cursor: "pointer",
          }}>+ Add Option</button>
        </div>
      )}

      {/* Rating — max stars */}
      {field.type === "rating" && (
        <div style={{ marginTop: "var(--space-sm)" }}>
          <PropField label="Max Stars">
            <PropSelect
              value={String(field.validation?.max_stars ?? 5)}
              onChange={v => onUpdate({ validation: { ...field.validation, max_stars: Number(v) } })}
              options={["3", "5", "10"]} labels={["3 stars", "5 stars", "10 stars"]}
            />
          </PropField>
        </div>
      )}

      {/* Slider — min, max, step */}
      {field.type === "slider" && (
        <div style={{ marginTop: "var(--space-sm)" }}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginBottom: 4 }}>Range</div>
          <div style={{ display: "flex", gap: 8 }}>
            <PropField label="Min">
              <PropInput value={String(field.validation?.min ?? 0)} onChange={v => onUpdate({ validation: { ...field.validation, min: Number(v) } })} />
            </PropField>
            <PropField label="Max">
              <PropInput value={String(field.validation?.max ?? 100)} onChange={v => onUpdate({ validation: { ...field.validation, max: Number(v) } })} />
            </PropField>
            <PropField label="Step">
              <PropInput value={String(field.validation?.step ?? 1)} onChange={v => onUpdate({ validation: { ...field.validation, step: Number(v) } })} />
            </PropField>
          </div>
        </div>
      )}

      {/* Currency — symbol */}
      {field.type === "currency" && (
        <div style={{ marginTop: "var(--space-sm)" }}>
          <PropField label="Currency Symbol">
            <PropSelect
              value={field.validation?.symbol ?? "$"}
              onChange={v => onUpdate({ validation: { ...field.validation, symbol: v } })}
              options={["$", "€", "£", "¥", "₹", "₩", "CHF"]}
              labels={["$ USD", "€ EUR", "£ GBP", "¥ JPY/CNY", "₹ INR", "₩ KRW", "CHF"]}
            />
          </PropField>
        </div>
      )}

      {/* Number validation */}
      {field.type === "number" && (
        <div style={{ marginTop: "var(--space-sm)" }}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginBottom: 4 }}>Validation</div>
          <div style={{ display: "flex", gap: 8 }}>
            <PropField label="Min"><PropInput value={String(field.validation?.min ?? "")} onChange={v => onUpdate({ validation: { ...field.validation, min: v ? Number(v) : undefined } })} /></PropField>
            <PropField label="Max"><PropInput value={String(field.validation?.max ?? "")} onChange={v => onUpdate({ validation: { ...field.validation, max: v ? Number(v) : undefined } })} /></PropField>
          </div>
        </div>
      )}

      {/* Text length validation */}
      {["text", "textarea", "email", "phone"].includes(field.type) && (
        <div style={{ marginTop: "var(--space-sm)" }}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginBottom: 4 }}>Validation</div>
          <div style={{ display: "flex", gap: 8 }}>
            <PropField label="Min Length"><PropInput value={String(field.validation?.min_length ?? "")} onChange={v => onUpdate({ validation: { ...field.validation, min_length: v ? Number(v) : undefined } })} /></PropField>
            <PropField label="Max Length"><PropInput value={String(field.validation?.max_length ?? "")} onChange={v => onUpdate({ validation: { ...field.validation, max_length: v ? Number(v) : undefined } })} /></PropField>
          </div>
        </div>
      )}

      {/* Connector source config */}
      {["connector_lookup", "connector_result", "json_display", "table_display"].includes(field.type) && (
        <div style={{ marginTop: "var(--space-sm)", borderTop: "1px solid var(--border-subtle)", paddingTop: "var(--space-sm)" }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", marginBottom: 6 }}>Connector Source</div>
          <PropField label="Connector ID">
            <PropInput mono value={field.source?.connector_id ?? ""} onChange={v => onUpdate({ source: { ...(field.source as any) ?? {}, connector_id: v, input_mapping: field.source?.input_mapping ?? {}, response_path: field.source?.response_path ?? "" } })} />
          </PropField>
          <PropField label="Response Path (dot-path, e.g. data.id)">
            <PropInput mono value={field.source?.response_path ?? ""} onChange={v => onUpdate({ source: { ...(field.source as any) ?? {}, response_path: v } })} />
          </PropField>
          <PropField label="Input Mapping (JSON: {param: field_key})">
            <PropInput mono value={JSON.stringify(field.source?.input_mapping ?? {})} onChange={v => { try { onUpdate({ source: { ...(field.source as any) ?? {}, input_mapping: JSON.parse(v) } }); } catch {} }} />
          </PropField>
          {field.type === "connector_lookup" && (
            <PropField label="Target Fields (JSON: {response.path: field_key})">
              <PropInput mono value={JSON.stringify(field.source?.target_fields ?? {})} onChange={v => { try { onUpdate({ source: { ...(field.source as any) ?? {}, target_fields: JSON.parse(v) } }); } catch {} }} />
            </PropField>
          )}
          <PropField label="On Null/Missing">
            <PropSelect
              value={field.null_policy ?? "skip"}
              onChange={v => onUpdate({ null_policy: v as any })}
              options={["skip", "error", "default"]}
              labels={["Skip (leave unchanged)", "Error (block save)", "Use default value"]}
            />
          </PropField>
        </div>
      )}
    </div>
  );
}

/* ── Mini form primitives ─────────────────────────────────────── */

function PropField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: "var(--space-xs)" }}>
      <label style={{ display: "block", fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginBottom: 2 }}>{label}</label>
      {children}
    </div>
  );
}

function PropInput({ value, onChange, mono }: { value: string; onChange: (v: string) => void; mono?: boolean }) {
  return (
    <input value={value} onChange={e => onChange(e.target.value)} style={{
      width: "100%", padding: "4px 8px", fontSize: 11,
      fontFamily: mono ? "var(--font-mono)" : "var(--font-body)",
      background: "var(--bg-input)", border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
      boxSizing: "border-box",
    }}
      onFocus={e => e.target.style.borderColor = "var(--border-focus)"}
      onBlur={e => e.target.style.borderColor = "var(--border-default)"}
    />
  );
}

function PropSelect({ value, onChange, options, labels }: {
  value: string; onChange: (v: string) => void; options: string[]; labels?: string[];
}) {
  return (
    <select value={value} onChange={e => onChange(e.target.value)} style={{
      width: "100%", padding: "4px 8px", fontSize: 11, fontFamily: "var(--font-body)",
      background: "var(--bg-input)", border: "1px solid var(--border-default)",
      borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
      boxSizing: "border-box",
    }}>
      {options.map((o, i) => <option key={o} value={o}>{labels?.[i] || o}</option>)}
    </select>
  );
}

export { FIELD_TYPES };
