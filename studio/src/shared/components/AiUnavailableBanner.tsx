/**
 * AiUnavailableBanner — shown inside AI-dependent modules when Ollama is down.
 *
 * Dismissible per-session (state lives in the component, not persisted).
 * Does NOT block the module — non-AI features remain usable.
 */
import React, { useState } from "react";
import { useAiStatus } from "@shared/hooks/useAiStatus";

interface Props {
  /** Name of the feature that requires AI, shown in the message */
  featureName?: string;
}

export const AiUnavailableBanner: React.FC<Props> = ({
  featureName = "AI features",
}) => {
  const { available, loading } = useAiStatus();
  const [dismissed, setDismissed] = useState(false);

  if (loading || available || dismissed) return null;

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "8px 14px",
        marginBottom: 12,
        background: "#fffbe6",
        border: "1px solid #ffe58f",
        borderRadius: 6,
        fontSize: 13,
        color: "#614700",
      }}
    >
      <span style={{ fontSize: 16 }}>⚠</span>
      <span>
        <strong>{featureName}</strong> are currently unavailable — the AI
        backend is unreachable. Other features on this page still work normally.
      </span>
      <button
        onClick={() => setDismissed(true)}
        style={{
          marginLeft: "auto",
          background: "none",
          border: "none",
          cursor: "pointer",
          fontSize: 16,
          color: "#614700",
          lineHeight: 1,
          padding: "0 4px",
        }}
        aria-label="Dismiss"
      >
        ×
      </button>
    </div>
  );
};
