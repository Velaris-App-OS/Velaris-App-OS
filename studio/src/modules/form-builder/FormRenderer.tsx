import React, { useState, useCallback } from "react";
import { Button, Spinner } from "@shared/components";
import type { FormFieldDef, FormSectionDef } from "./FormBuilder";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

function useConnectorLookup(
  field: FormFieldDef,
  values: Record<string, any>,
  setValues: (fn: (prev: Record<string, any>) => Record<string, any>) => void,
  caseId?: string,
) {
  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState<{ text: string; ok: boolean } | null>(null);

  const trigger = useCallback(async () => {
    const src = field.source;
    if (!src?.connector_id) {
      setToast({ text: "No connector configured for this field", ok: false });
      return;
    }
    setLoading(true);
    setToast(null);
    try {
      const inputData: Record<string, any> = {};
      for (const [paramKey, fieldKey] of Object.entries(src.input_mapping ?? {})) {
        inputData[paramKey] = values[fieldKey] ?? "";
      }
      const body: any = { input_data: inputData, form_id: undefined, field_key: field.field_key };
      if (caseId) body.case_id = caseId;
      const r = await fetch(`/api/v1/hxbridge/connectors/${src.connector_id}/form-lookup`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ..._authHdr() },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok || !data.ok) {
        setToast({ text: data.error || "Lookup failed", ok: false });
        return;
      }
      // Auto-fill target fields from response
      const result = data.result ?? {};
      const filled: Record<string, any> = {};
      for (const [responsePath, targetKey] of Object.entries(src.target_fields ?? {})) {
        const val = responsePath.split(".").reduce((o: any, k) => (o && typeof o === "object" ? o[k] : undefined), result);
        if (val !== undefined) filled[targetKey as string] = val;
      }
      // Also store raw result in this field
      filled[field.field_key] = result;
      setValues(prev => ({ ...prev, ...filled }));
      const count = Object.keys(filled).length;
      setToast({ text: `${count} field${count !== 1 ? "s" : ""} populated`, ok: true });
    } catch (e: any) {
      setToast({ text: e.message || "Lookup error", ok: false });
    } finally {
      setLoading(false);
      setTimeout(() => setToast(null), 4000);
    }
  }, [field, values, caseId, setValues]);

  return { trigger, loading, toast };
}

/* ═══════════════════════════════════════════════════════════════════
   FormRenderer — runtime form rendering for step assignments

   Given a form definition_json, renders a dynamic form, validates
   inputs, and calls onSubmit with the collected values.
   ═══════════════════════════════════════════════════════════════════ */

interface FormRendererProps {
  formName: string;
  definition: { sections: FormSectionDef[] };
  initialValues?: Record<string, any>;
  onSubmit: (values: Record<string, any>) => Promise<void>;
  onCancel: () => void;
  readOnly?: boolean;
  caseId?: string;   // when provided, file_upload fields upload to this case
}

export default function FormRenderer({
  formName,
  definition,
  initialValues = {},
  onSubmit,
  onCancel,
  readOnly = false,
  caseId,
}: FormRendererProps) {
  const [values, setValues] = useState<Record<string, any>>(initialValues);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");

  const sections = definition.sections || [];

  const setValue = (key: string, val: any) => {
    setValues(prev => ({ ...prev, [key]: val }));
    // Clear error on change
    if (errors[key]) {
      setErrors(prev => { const next = { ...prev }; delete next[key]; return next; });
    }
  };

  const validate = (): boolean => {
    const newErrors: Record<string, string> = {};

    for (const section of sections) {
      for (const field of section.fields) {
        const key = field.field_key || field.id;
        const val = values[key];

        // multi_select value is string[] — empty array counts as missing
        if (field.type === "multi_select") {
          if (field.required && (!Array.isArray(val) || val.length === 0)) {
            newErrors[key] = `${field.label} is required`;
          }
          continue;
        }

        if (field.required && (val === undefined || val === null || val === "")) {
          newErrors[key] = `${field.label} is required`;
          continue;
        }

        if (val === undefined || val === null || val === "") continue;

        const v = field.validation || {};

        if (field.type === "email" && typeof val === "string" && !val.includes("@")) {
          newErrors[key] = "Invalid email address";
        }

        if (field.type === "number") {
          const num = Number(val);
          if (isNaN(num)) { newErrors[key] = "Must be a number"; continue; }
          if (v.min !== undefined && num < v.min) newErrors[key] = `Must be >= ${v.min}`;
          if (v.max !== undefined && num > v.max) newErrors[key] = `Must be <= ${v.max}`;
        }

        if (["text", "textarea", "email", "phone"].includes(field.type) && typeof val === "string") {
          if (v.min_length && val.length < v.min_length) newErrors[key] = `Must be at least ${v.min_length} chars`;
          if (v.max_length && val.length > v.max_length) newErrors[key] = `Must be at most ${v.max_length} chars`;
        }
      }
    }

    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const handleSubmit = async () => {
    if (!validate()) return;
    setSubmitting(true);
    setSubmitError("");
    try {
      await onSubmit(values);
    } catch (e: any) {
      setSubmitError(e?.message || "Submission failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{
      background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
      borderRadius: "var(--radius-md)", overflow: "hidden",
    }}>
      {/* Header */}
      <div style={{
        padding: "var(--space-md) var(--space-lg)",
        borderBottom: "1px solid var(--border-subtle)",
        background: "var(--bg-elevated)",
      }}>
        <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text-primary)", fontFamily: "var(--font-display)" }}>
          {formName}
        </div>
        {readOnly && (
          <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text-muted)", textTransform: "uppercase" }}>
            read only
          </span>
        )}
      </div>

      {/* Sections */}
      <div style={{ padding: "var(--space-lg)" }}>
        {sections.map((section, si) => (
          <div key={section.id} style={{ marginBottom: si < sections.length - 1 ? "var(--space-xl)" : 0 }}>
            <div style={{
              fontSize: 12, fontWeight: 600, color: "var(--text-primary)",
              fontFamily: "var(--font-display)", marginBottom: "var(--space-md)",
              paddingBottom: "var(--space-xs)", borderBottom: "1px solid var(--border-subtle)",
            }}>{section.title}</div>

            <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
              {section.fields.map(field => (
                <FieldRenderer
                  key={field.id}
                  field={field}
                  value={values[field.field_key || field.id]}
                  error={errors[field.field_key || field.id]}
                  onChange={val => setValue(field.field_key || field.id, val)}
                  readOnly={readOnly}
                  caseId={caseId}
                  allValues={values}
                  setAllValues={setValues}
                />
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* Actions */}
      {!readOnly && (
        <div style={{
          padding: "var(--space-md) var(--space-lg)",
          borderTop: "1px solid var(--border-subtle)",
          display: "flex", justifyContent: "space-between", alignItems: "center",
        }}>
          {submitError && (
            <span style={{ fontSize: 12, color: "var(--status-failed)" }}>{submitError}</span>
          )}
          <div style={{ display: "flex", gap: "var(--space-sm)", marginLeft: "auto" }}>
            <Button variant="secondary" onClick={onCancel}>Cancel</Button>
            <Button onClick={handleSubmit} disabled={submitting}>
              {submitting ? "Submitting…" : "Submit"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Individual Field Renderer ────────────────────────────────── */

function FieldRenderer({ field, value, error, onChange, readOnly, caseId, allValues, setAllValues }: {
  field: FormFieldDef;
  value: any;
  error?: string;
  onChange: (val: any) => void;
  readOnly: boolean;
  caseId?: string;
  allValues?: Record<string, any>;
  setAllValues?: (fn: (prev: Record<string, any>) => Record<string, any>) => void;
}) {
  const connectorLookup = useConnectorLookup(field, allValues ?? {}, setAllValues ?? (() => {}), caseId);
  const inputStyle: React.CSSProperties = {
    width: "100%", padding: "8px 12px", fontSize: 13,
    fontFamily: "var(--font-body)",
    background: readOnly ? "var(--bg-elevated)" : "var(--bg-input)",
    border: `1px solid ${error ? "var(--status-failed)" : "var(--border-default)"}`,
    borderRadius: "var(--radius-sm)", color: "var(--text-primary)",
    outline: "none", boxSizing: "border-box" as const,
  };

  const renderInput = () => {
    switch (field.type) {
      case "text":
      case "email":
      case "phone":
        return (
          <input
            type={field.type === "email" ? "email" : field.type === "phone" ? "tel" : "text"}
            value={value || ""}
            onChange={e => onChange(e.target.value)}
            placeholder={field.placeholder}
            disabled={readOnly}
            style={inputStyle}
            onFocus={e => !error && (e.target.style.borderColor = "var(--border-focus)")}
            onBlur={e => !error && (e.target.style.borderColor = "var(--border-default)")}
          />
        );

      case "number":
        return (
          <input
            type="number"
            value={value ?? ""}
            onChange={e => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
            placeholder={field.placeholder}
            disabled={readOnly}
            style={inputStyle}
            min={field.validation?.min}
            max={field.validation?.max}
            onFocus={e => !error && (e.target.style.borderColor = "var(--border-focus)")}
            onBlur={e => !error && (e.target.style.borderColor = "var(--border-default)")}
          />
        );

      case "textarea":
        return (
          <textarea
            value={value || ""}
            onChange={e => onChange(e.target.value)}
            placeholder={field.placeholder}
            disabled={readOnly}
            rows={4}
            style={{ ...inputStyle, resize: "vertical" as const }}
          />
        );

      case "dropdown":
        return (
          <select
            value={value || ""}
            onChange={e => onChange(e.target.value)}
            disabled={readOnly}
            style={inputStyle}
          >
            <option value="">{field.placeholder || "Select…"}</option>
            {(field.options || []).map(opt => (
              <option key={typeof opt === "string" ? opt : opt.value} value={typeof opt === "string" ? opt : opt.value}>
                {typeof opt === "string" ? opt : opt.label}
              </option>
            ))}
          </select>
        );

      case "checkbox":
        return (
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: readOnly ? "default" : "pointer" }}>
            <input
              type="checkbox"
              checked={!!value}
              onChange={e => onChange(e.target.checked)}
              disabled={readOnly}
              style={{ accentColor: "var(--accent)", width: 16, height: 16 }}
            />
            <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
              {field.placeholder || field.label}
            </span>
          </label>
        );

      case "date":
        return (
          <input
            type="date"
            value={value || ""}
            onChange={e => onChange(e.target.value)}
            disabled={readOnly}
            style={{ ...inputStyle, colorScheme: "dark" }}
          />
        );

      case "file_upload":
        return <FileUploadField value={value} onChange={onChange} readOnly={readOnly} caseId={caseId} />;

      case "datetime":
        return (
          <input type="datetime-local" value={value || ""} onChange={e => onChange(e.target.value)}
            disabled={readOnly} style={{ ...inputStyle, colorScheme: "dark" }} />
        );

      case "radio": {
        const opts = field.options || [];
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {opts.map(opt => (
              <label key={opt.value} style={{ display: "flex", alignItems: "center", gap: 8, cursor: readOnly ? "default" : "pointer", fontSize: 13, color: "var(--text-primary)" }}>
                <input type="radio"
                  name={field.field_key || field.id}
                  value={opt.value}
                  checked={value === opt.value}
                  onChange={() => !readOnly && onChange(opt.value)}
                  disabled={readOnly}
                  style={{ accentColor: "var(--accent)", width: 15, height: 15 }}
                />
                {opt.label}
              </label>
            ))}
          </div>
        );
      }

      case "multi_select": {
        const msValue: string[] = Array.isArray(value) ? value : [];
        const opts = field.options || [];
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {opts.map(opt => (
              <label key={opt.value} style={{ display: "flex", alignItems: "center", gap: 8, cursor: readOnly ? "default" : "pointer", fontSize: 13, color: "var(--text-primary)" }}>
                <input type="checkbox"
                  checked={msValue.includes(opt.value)}
                  onChange={e => {
                    if (readOnly) return;
                    onChange(e.target.checked ? [...msValue, opt.value] : msValue.filter(v => v !== opt.value));
                  }}
                  disabled={readOnly}
                  style={{ accentColor: "var(--accent)", width: 15, height: 15 }}
                />
                {opt.label}
              </label>
            ))}
          </div>
        );
      }

      case "rating": {
        const maxStars = field.validation?.max_stars || 5;
        const ratingValue = Number(value) || 0;
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 2 }}>
            {Array.from({ length: maxStars }, (_, i) => i + 1).map(star => (
              <button key={star} type="button" disabled={readOnly}
                onClick={() => !readOnly && onChange(star === ratingValue ? 0 : star)}
                style={{
                  background: "none", border: "none",
                  cursor: readOnly ? "default" : "pointer",
                  fontSize: 26, lineHeight: 1, padding: "0 1px",
                  color: star <= ratingValue ? "#f59e0b" : "var(--border-default)",
                  transition: "color 0.1s",
                }}>★</button>
            ))}
            {ratingValue > 0 && (
              <span style={{ fontSize: 12, color: "var(--text-muted)", marginLeft: 6, fontFamily: "var(--font-mono)" }}>
                {ratingValue}/{maxStars}
              </span>
            )}
          </div>
        );
      }

      case "slider": {
        const min = field.validation?.min ?? 0;
        const max = field.validation?.max ?? 100;
        const step = field.validation?.step ?? 1;
        const sliderVal = value !== undefined && value !== null && value !== "" ? Number(value) : min;
        return (
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
              <span>{min}</span>
              <span style={{ fontWeight: 700, color: "var(--text-primary)", fontFamily: "var(--font-mono)" }}>{sliderVal}</span>
              <span>{max}</span>
            </div>
            <input type="range" min={min} max={max} step={step} value={sliderVal}
              onChange={e => !readOnly && onChange(Number(e.target.value))}
              disabled={readOnly}
              style={{ width: "100%", accentColor: "var(--accent)", cursor: readOnly ? "default" : "pointer" }}
            />
          </div>
        );
      }

      case "currency": {
        const symbol = field.validation?.symbol || "$";
        return (
          <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
            <span style={{
              position: "absolute", left: 10, fontSize: 13, fontWeight: 600,
              color: "var(--text-muted)", fontFamily: "var(--font-mono)", pointerEvents: "none",
            }}>{symbol}</span>
            <input type="number" step="0.01" min="0"
              value={value ?? ""}
              onChange={e => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
              disabled={readOnly}
              placeholder={field.placeholder || "0.00"}
              style={{ ...inputStyle, paddingLeft: symbol.length > 2 ? 48 : 30 }}
              onFocus={e => !readOnly && !error && (e.target.style.borderColor = "var(--border-focus)")}
              onBlur={e => !error && (e.target.style.borderColor = "var(--border-default)")}
            />
          </div>
        );
      }

      case "connector_lookup": {
        const { trigger, loading: lookupLoading, toast } = connectorLookup;
        return (
          <div>
            <button
              type="button"
              disabled={lookupLoading || readOnly}
              onClick={trigger}
              style={{
                padding: "7px 16px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
                background: "var(--bg-elevated)", color: "var(--text-primary)", cursor: lookupLoading ? "wait" : "pointer",
                fontSize: 12, fontWeight: 600, display: "flex", alignItems: "center", gap: 6,
              }}
            >
              {lookupLoading ? "Fetching…" : `⚡ Fetch from ${field.source?.connector_id ? "Connector" : "(no connector set)"}`}
            </button>
            {toast && (
              <div style={{
                marginTop: 6, fontSize: 11, padding: "5px 10px", borderRadius: 4,
                background: toast.ok ? "#22c55e22" : "#ef444422",
                color: toast.ok ? "#22c55e" : "#ef4444",
              }}>{toast.text}</div>
            )}
          </div>
        );
      }

      case "connector_result": {
        if (!value) return <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic" }}>No data yet — trigger a connector lookup above.</div>;
        return (
          <ConnectorResultDisplay data={value} />
        );
      }

      case "json_display": {
        if (!value) return <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic" }}>—</div>;
        return <JsonTreeDisplay data={value} />;
      }

      case "table_display": {
        const rows = Array.isArray(value?.items) ? value.items : Array.isArray(value) ? value : [];
        return <TableDisplay rows={rows} page={value?.page} total={value?.total} />;
      }

      default:
        return <input type="text" value={value || ""} onChange={e => onChange(e.target.value)} style={inputStyle} />;
    }
  };

  return (
    <div>
      {field.type !== "checkbox" && (
        <label style={{
          display: "block", fontSize: 12, fontWeight: 500, color: "var(--text-secondary)",
          marginBottom: 4,
        }}>
          {field.label}
          {field.required && <span style={{ color: "var(--status-failed)", marginLeft: 2 }}>*</span>}
        </label>
      )}
      {field.description && (
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>{field.description}</div>
      )}
      {renderInput()}
      {error && (
        <div style={{ fontSize: 11, color: "var(--status-failed)", marginTop: 3 }}>{error}</div>
      )}
    </div>
  );
}

/* ── File Upload Field ────────────────────────────────────────── */

function FileUploadField({ value, onChange, readOnly, caseId }: {
  value: any;
  onChange: (val: any) => void;
  readOnly: boolean;
  caseId?: string;
}) {
  const [uploading, setUploading] = useState(false);
  const [uploadErr, setUploadErr] = useState<string | null>(null);

  // value shape after upload: { document_id, filename }
  const uploaded = value && typeof value === "object" && value.document_id ? value : null;

  async function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!caseId) {
      // No case context — store filename locally so the form can still submit
      onChange({ filename: file.name, local: true });
      return;
    }
    setUploading(true);
    setUploadErr(null);
    try {
      const fd = new FormData();
      fd.append("case_id", caseId);
      fd.append("file", file);
      const r = await fetch("/api/v1/documents/upload", {
        method: "POST", body: fd,
        headers: localStorage.getItem("helix_token") ? { Authorization: `Bearer ${localStorage.getItem("helix_token")}` } : {},
      });
      if (!r.ok) throw new Error(`Upload failed: ${r.status}`);
      const doc = await r.json();
      onChange({ document_id: doc.id, filename: file.name });
    } catch (err: any) {
      setUploadErr(err.message || "Upload failed");
    } finally {
      setUploading(false);
      e.target.value = "";
    }
  }

  function clear() { onChange(null); setUploadErr(null); }

  if (readOnly) {
    return (
      <div style={{ fontSize: 13, color: uploaded ? "var(--accent)" : "var(--text-muted)" }}>
        {uploaded ? `📎 ${uploaded.filename}` : "No file uploaded"}
      </div>
    );
  }

  return (
    <div>
      {uploaded ? (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{
            display: "flex", alignItems: "center", gap: 6,
            padding: "6px 12px", border: "1px solid var(--status-completed)",
            borderRadius: "var(--radius-sm)", background: "color-mix(in srgb, var(--status-completed) 8%, transparent)",
          }}>
            <span style={{ fontSize: 14 }}>📎</span>
            <span style={{ fontSize: 12, color: "var(--status-completed)", fontWeight: 500 }}>{uploaded.filename}</span>
            {uploaded.document_id && (
              <span style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                · saved
              </span>
            )}
          </div>
          <button onClick={clear} style={{
            background: "none", border: "none", cursor: "pointer",
            fontSize: 12, color: "var(--text-muted)",
          }}>✕ Remove</button>
        </div>
      ) : (
        <label style={{
          display: "inline-flex", alignItems: "center", gap: 8, cursor: uploading ? "wait" : "pointer",
          padding: "8px 16px", border: "2px dashed var(--border-default)",
          borderRadius: "var(--radius-sm)", fontSize: 12, color: "var(--text-secondary)",
          background: "var(--bg-elevated)", transition: "border-color 0.15s",
          width: "100%", boxSizing: "border-box" as const,
        }}
          onMouseEnter={(e) => (e.currentTarget.style.borderColor = "var(--accent)")}
          onMouseLeave={(e) => (e.currentTarget.style.borderColor = "var(--border-default)")}
        >
          <span style={{ fontSize: 18 }}>📎</span>
          {uploading ? "Uploading…" : "Click to choose file"}
          {!caseId && <span style={{ fontSize: 10, color: "var(--text-muted)", marginLeft: "auto" }}>(no case linked)</span>}
          <input type="file" style={{ display: "none" }} onChange={handleFile} disabled={uploading} />
        </label>
      )}
      {uploadErr && (
        <div style={{ fontSize: 11, color: "var(--status-failed)", marginTop: 4 }}>{uploadErr}</div>
      )}
    </div>
  );
}

/* ── Connector result display components ─────────────────────────── */

function ConnectorResultDisplay({ data }: { data: any }) {
  const [collapsed, setCollapsed] = useState(false);
  return (
    <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", overflow: "hidden" }}>
      <div
        onClick={() => setCollapsed(c => !c)}
        style={{ padding: "6px 10px", background: "var(--bg-elevated)", cursor: "pointer", fontSize: 11, fontWeight: 600, display: "flex", justifyContent: "space-between" }}
      >
        <span>Connector Result</span>
        <span>{collapsed ? "▶" : "▼"}</span>
      </div>
      {!collapsed && (
        <pre style={{ margin: 0, padding: "8px 12px", fontSize: 11, fontFamily: "var(--font-mono)", whiteSpace: "pre-wrap", maxHeight: 300, overflow: "auto", background: "var(--bg-card)", color: "var(--text-primary)" }}>
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  );
}

function JsonTreeDisplay({ data }: { data: any }) {
  const [collapsed, setCollapsed] = useState(false);
  return (
    <div style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm)", overflow: "hidden" }}>
      <div
        onClick={() => setCollapsed(c => !c)}
        style={{ padding: "5px 10px", background: "var(--bg-elevated)", cursor: "pointer", fontSize: 11, fontWeight: 600, display: "flex", justifyContent: "space-between" }}
      >
        <span>{Array.isArray(data) ? `Array [${data.length}]` : "Object {…}"}</span>
        <span>{collapsed ? "▶" : "▼"}</span>
      </div>
      {!collapsed && (
        <pre style={{ margin: 0, padding: "8px 12px", fontSize: 11, fontFamily: "var(--font-mono)", whiteSpace: "pre-wrap", maxHeight: 400, overflow: "auto", background: "var(--bg-card)", color: "var(--text-primary)" }}>
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  );
}

function TableDisplay({ rows, page, total }: { rows: any[]; page?: number; total?: number }) {
  const [currentPage, setCurrentPage] = useState(page ?? 1);
  if (rows.length === 0) return <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic" }}>No rows.</div>;
  const cols = Object.keys(rows[0] ?? {}).slice(0, 10);
  return (
    <div style={{ overflowX: "auto", fontSize: 12 }}>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ borderBottom: "2px solid var(--border-default)" }}>
            {cols.map(c => (
              <th key={c} style={{ padding: "5px 8px", textAlign: "left", fontSize: 10, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", whiteSpace: "nowrap" }}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} style={{ borderBottom: "1px solid var(--border-subtle)", background: i % 2 === 0 ? "transparent" : "var(--bg-elevated)" }}>
              {cols.map(c => (
                <td key={c} style={{ padding: "5px 8px", color: "var(--text-primary)", maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {typeof row[c] === "object" ? JSON.stringify(row[c]) : String(row[c] ?? "—")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {total !== undefined && (
        <div style={{ padding: "6px 8px", fontSize: 11, color: "var(--text-muted)", display: "flex", gap: 12, alignItems: "center" }}>
          <span>{total} total rows</span>
          {page && <span>Page {currentPage}</span>}
        </div>
      )}
    </div>
  );
}

