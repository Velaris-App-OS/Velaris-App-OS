/**
 * View Form — every field type the builder offers renders for the person
 * filling the form in, the real validation runs, and submitting saves nothing.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import FormPreview from "./FormPreview";
import { FIELD_TYPES } from "./FormBuilder";
import type { FormDefinition, FormFieldDef } from "./FormBuilder";

const calls: string[] = [];

beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal("fetch", vi.fn((url: string, opts: RequestInit = {}) => {
    calls.push(`${opts.method ?? "GET"} ${url}`);
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) } as Response);
  }));
});
afterEach(() => vi.unstubAllGlobals());

function everyType(): FormDefinition {
  const fields: FormFieldDef[] = FIELD_TYPES.map((t, i) => ({
    id: `f${i}`, type: t.type, label: `Label ${t.label}`, field_key: `k_${t.type}`,
    required: t.type === "text" || t.type === "email",
    ...(["dropdown", "radio", "multi_select"].includes(t.type)
      ? { options: [{ label: "One", value: "one" }, { label: "Two", value: "two" }] } : {}),
    ...(t.type === "json_display" ? { default_value: { tier: "gold" } } : {}),
    ...(t.type === "table_display" ? { default_value: [{ order: "A-1", total: 5 }] } : {}),
  }));
  return { sections: [{ id: "s1", title: "All types", order: 0, fields }] };
}

describe("View Form", () => {
  it("renders every field type with its label and the sample data of display fields", () => {
    render(<FormPreview name="Showcase" definition={everyType()} unsaved={false} onClose={() => {}} />);
    for (const t of FIELD_TYPES) {
      // a paragraph shows its text and a divider a line — neither has a label
      if (t.type === "paragraph" || t.type === "divider") continue;
      // the checkbox shows its label beside the box (once, as text)
      const label = `Label ${t.label}`.replace(/[()]/g, "\\$&");
      expect(screen.getAllByText(new RegExp(label)).length).toBeGreaterThan(0);
    }
    expect(screen.getByText(/"tier": "gold"/)).toBeTruthy();
    expect(screen.getByText("A-1")).toBeTruthy();
    expect(screen.getByText(new RegExp(`${FIELD_TYPES.length} fields`))).toBeTruthy();
  });

  it("runs the real validation, then shows what would be sent — and saves nothing", async () => {
    render(<FormPreview name="Showcase" definition={everyType()} unsaved onClose={() => {}} />);
    expect(screen.getByText(/includes unsaved changes/)).toBeTruthy();
    fireEvent.click(screen.getByText("Submit"));
    expect(await screen.findByText("Label Text is required")).toBeTruthy();
    expect(screen.getByText("Label Email is required")).toBeTruthy();

    const inputs = screen.getAllByRole("textbox") as HTMLInputElement[];
    fireEvent.change(inputs[0], { target: { value: "Jane" } });                 // text
    const email = document.querySelector('input[type="email"]') as HTMLInputElement;
    fireEvent.change(email, { target: { value: "jane@example.com" } });
    fireEvent.click(screen.getByText("Submit"));
    await waitFor(() => expect(screen.getByText(/passed validation/)).toBeTruthy());
    expect(screen.getByText(/"k_text": "Jane"/)).toBeTruthy();
    expect(calls.filter(c => !c.startsWith("GET"))).toEqual([]);              // nothing written

    fireEvent.click(screen.getByText("Fill in again"));
    expect(screen.getByText("Submit")).toBeTruthy();
  });

  it("switches widths and closes on Close and on Escape", () => {
    const onClose = vi.fn();
    render(<FormPreview name="Showcase" definition={everyType()} unsaved={false} onClose={onClose} />);
    fireEvent.click(screen.getByText("Phone"));
    expect(screen.getByText("Phone").getAttribute("aria-pressed")).toBe("true");
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.click(screen.getAllByText("Close")[0]);
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("says so when the form has no fields yet", () => {
    render(<FormPreview name="Empty" definition={{ sections: [] }} unsaved={false} onClose={() => {}} />);
    expect(screen.getByText(/no fields yet/)).toBeTruthy();
  });
});
