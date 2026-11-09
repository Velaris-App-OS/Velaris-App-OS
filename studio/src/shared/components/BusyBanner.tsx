/**
 * BusyBanner — a thin bar while Studio retries requests the server refused as
 * busy (VEL-AUTH-CHECK-BUSY-503, see shared/api/busyRetry.ts).
 *
 * Retrying: "Server busy — retrying in N s…". If a request gives up after about
 * two minutes, its own screen shows the error; the bar then says so once and
 * can be dismissed. Nothing reloads, so unsaved input stays on screen.
 */
import React, { useEffect, useState } from "react";

type State = { kind: "idle" } | { kind: "retrying"; next: number } | { kind: "gaveup" };

export const BusyBanner: React.FC = () => {
  const [state, setState] = useState<State>({ kind: "idle" });

  useEffect(() => {
    const onBusy = (e: Event) => {
      const next = (e as CustomEvent).detail?.nextInSeconds ?? 5;
      setState({ kind: "retrying", next });
    };
    const onClear = () => setState((s) => (s.kind === "gaveup" ? s : { kind: "idle" }));
    const onGaveUp = () => setState({ kind: "gaveup" });
    window.addEventListener("velaris:busy", onBusy);
    window.addEventListener("velaris:busy-clear", onClear);
    window.addEventListener("velaris:busy-gaveup", onGaveUp);
    return () => {
      window.removeEventListener("velaris:busy", onBusy);
      window.removeEventListener("velaris:busy-clear", onClear);
      window.removeEventListener("velaris:busy-gaveup", onGaveUp);
    };
  }, []);

  // Count the wait down on screen.
  useEffect(() => {
    if (state.kind !== "retrying" || state.next <= 0) return;
    const t = setTimeout(() => setState({ kind: "retrying", next: state.next - 1 }), 1000);
    return () => clearTimeout(t);
  }, [state]);

  if (state.kind === "idle") return null;

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: "fixed", top: 0, left: "50%", transform: "translateX(-50%)", zIndex: 10000,
        display: "flex", alignItems: "center", gap: 10, marginTop: 8, padding: "6px 14px",
        background: "#fffbe6", border: "1px solid #ffe58f", borderRadius: 6,
        fontSize: 13, color: "#614700", boxShadow: "0 2px 8px rgba(0,0,0,0.12)",
      }}
    >
      {state.kind === "retrying" ? (
        <span>Server busy — retrying{state.next > 0 ? ` in ${state.next} s` : ""}…</span>
      ) : (
        <>
          <span>The server is still busy. Your last action did not go through — please try it again in a minute.</span>
          <button
            onClick={() => setState({ kind: "idle" })}
            aria-label="Dismiss"
            style={{ background: "none", border: "none", cursor: "pointer", fontSize: 16, color: "#614700", lineHeight: 1, padding: "0 4px" }}
          >
            ×
          </button>
        </>
      )}
    </div>
  );
};

export default BusyBanner;
