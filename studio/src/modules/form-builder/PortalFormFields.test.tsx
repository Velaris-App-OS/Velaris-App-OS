/** The portal draws a case type's form with the shared renderer (part 10). */
import React, { useState } from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import PortalFormFields from "./PortalFormFields";

const def = { settings: { columns: 2 as const }, sections: [{ id: "s", title: "About", order: 0, fields: [
  { id: "a", type: "dropdown" as const, label: "Kind", field_key: "kind", required: true,
    options: [{ label: "Car", value: "car" }, { label: "Home", value: "home" }] },
  { id: "b", type: "text" as const, label: "Plate", field_key: "plate", required: true,
    show_when: { rules: [{ field: "kind", op: "equals", value: "car" }] } },
] }] };

function H() {
  const [v, setV] = useState<Record<string, any>>({});
  return <PortalFormFields definition={def as any} values={v} errors={{ plate: "Plate is required" }} onChange={(k, x) => setV(p => ({ ...p, [k]: x }))} />;
}

describe("PortalFormFields", () => {
  it("draws a real dropdown, applies show/hide rules and shows errors", () => {
    render(<H />);
    const select = document.querySelector("select") as HTMLSelectElement;
    expect([...select.options].map(o => o.textContent)).toContain("Car");
    expect(screen.queryByText("Plate")).toBeNull();
    fireEvent.change(select, { target: { value: "car" } });
    expect(screen.getByText("Plate")).toBeTruthy();
    expect(screen.getByText("Plate is required")).toBeTruthy();
    const cell = screen.getByText("Kind").closest("div[style*='grid-column']") as HTMLElement;
    expect(cell.style.gridColumn).toBe("span 3");
  });

  it("computes totals, marks conditional required and runs on-change actions", () => {
    const calc = { sections: [{ id: "s", title: "S", order: 0, fields: [
      { id: "q", type: "number", label: "Qty", field_key: "qty", required: false,
        on_change: [{ action: "set", target: "note", expression: "\"n\" & qty" }] },
      { id: "t", type: "number", label: "Total", field_key: "total", required: false, calculation: "qty * 40" },
      { id: "r", type: "text", label: "Reason", field_key: "reason", required: false, required_when: { expression: "total > 100" } },
      { id: "n", type: "text", label: "Note", field_key: "note", required: false },
    ] }] };
    function C() {
      const [v, setV] = useState<Record<string, any>>({});
      return <PortalFormFields definition={calc as any} values={v} errors={{}} onChange={(k, x) => setV(p => ({ ...p, [k]: x }))} />;
    }
    render(<C />);
    const input = (l: string) => (screen.getByText(l).closest("div[style*='grid-column']") as HTMLElement).querySelector("input") as HTMLInputElement;
    fireEvent.change(input("Qty"), { target: { value: "3" } });
    expect(input("Total").value).toBe("120");
    expect(input("Total").disabled).toBe(true);
    expect(input("Note").value).toBe("n3");
    const reason = screen.getByText("Reason").closest("label, div") as HTMLElement;
    expect(reason.textContent).toContain("*");
  });
});
