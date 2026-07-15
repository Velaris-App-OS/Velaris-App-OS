import React, { useState } from "react";
import type { NavIconComponent } from "@/app/nav-icons";
import { NAV_ICON_SIZE } from "@/app/nav-icons";
import ErrorDocModal, { Octagon } from "@shared/errors/ErrorDocModal";

interface PageHeaderProps {
  title:        string;
  description?: string;
  icon?:        NavIconComponent;
  /** Nav path of the current page (e.g. "/documents"); drives the Error Documentation modal. */
  path?:        string;
}

export default function PageHeader({ title, description, icon: Icon, path }: PageHeaderProps) {
  const [errorsOpen, setErrorsOpen] = useState(false);
  // "/documents" → "documents"; "/" → "" (no component section, search still works).
  const componentKey = path ? path.replace(/^\//, "") : undefined;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "0 28px",
        height: 56,
        flexShrink: 0,
        background: "var(--bg-panel)",
        borderBottom: "1px solid var(--border-subtle)",
      }}
    >
      {/* Left: icon + name + description */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
        {Icon && (
          <div style={{ flexShrink: 0, display: "flex", alignItems: "center" }}>
            <Icon active size={NAV_ICON_SIZE} />
          </div>
        )}
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              fontSize: 15,
              fontWeight: 600,
              color: "var(--text-headline)",
              lineHeight: 1.25,
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {title}
          </div>
          {description && (
            <div
              style={{
                fontSize: 11,
                color: "var(--text-muted)",
                marginTop: 2,
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {description}
            </div>
          )}
        </div>
      </div>

      {/* Right: error docs + help */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
        {/* Error Documentation — red octagon, opens the modal */}
        <button
          onClick={() => setErrorsOpen(true)}
          title="Error Documentation"
          aria-label="Error Documentation"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: 28,
            height: 28,
            borderRadius: "var(--radius-sm)",
            border: "none",
            background: "transparent",
            cursor: "pointer",
            padding: 0,
          }}
        >
          <Octagon size={20} />
        </button>

        {/* Help (coming soon) */}
        <button
          disabled
          title="Help (coming soon)"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: 28,
            height: 28,
            borderRadius: "50%",
            border: "1px solid var(--border-subtle)",
            background: "var(--bg-elevated)",
            color: "var(--text-muted)",
            fontSize: 13,
            fontWeight: 700,
            cursor: "not-allowed",
            opacity: 0.55,
            flexShrink: 0,
            fontFamily: "var(--font-mono)",
          }}
        >
          ?
        </button>
      </div>

      <ErrorDocModal
        open={errorsOpen}
        onClose={() => setErrorsOpen(false)}
        componentKey={componentKey}
        componentLabel={title}
      />
    </div>
  );
}
