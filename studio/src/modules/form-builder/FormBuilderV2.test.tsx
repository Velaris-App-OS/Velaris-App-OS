/**
 * Form Builder v2 (2026-10-28): calculations, conditional required / read-only,
 * on-change actions and cross-field checks — set in the field's own panel,
 * read back in words, and live in the form.
 */
import React, { useState } from "react";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, expect, it, beforeEach } from "vitest";

import FormBuilder, { renameField, FIELD_GROUPS } from "./FormBuilder";
import FormRenderer from "./FormRenderer";
import type { FormDefinition, FormFieldDef } from "./FormBuilder";
import { describeExpr, renameRef } from "./expr";
import { fieldSentences } from "./LogicEditors";
import { whenToFormula } from "./RulesEditor";

// The palette's groups start closed (2026-10-29); these tests use its buttons.
beforeEach(() => { localStorage.setItem("velaris.formbuilder.palette.open", JSON.stringify(FIELD_GROUPS)); });

const F = (id: string, type: FormFieldDef["type"], label: string, extra: Partial<FormFieldDef> = {}): FormFieldDef =>
  ({ id, type, label, field_key: id, required: false, ...extra });

function def(fields: FormFieldDef[]): FormDefinition {
  return { sections: [{ id: "s1", title: "S", order: 0, fields }] };
}

function Harness({ initial, onDef }: { initial: FormDefinition; onDef: (d: FormDefinition) => void }) {
  const [d, setD] = useState(initial);
  return <FormBuilder definition={d} onChange={n => { setD(n); onDef(n); }} />;
}
function setup(initial: FormDefinition) {
  let last = initial;
  render(<Harness initial={initial} onDef={d => { last = d; }} />);
  return { get: () => last };
}
const field = (d: FormDefinition, id: string) => d.sections.flatMap(s => s.fields).find(f => f.id === id)! as any;

describe("words and renames", () => {
  const label = (k: string) => ({ qty: "Quantity", price: "Unit price", dob: "Date of birth" } as any)[k] ?? k;
  it("reads a formula back with labels", () => {
    expect(describeExpr("qty * price", label)).toBe("Quantity × Unit price");
    expect(describeExpr("years_between(dob, today())", label)).toBe("full years from Date of birth to today");
    expect(describeExpr("amount > 1000 and country = \"DE\"", label)).toBe("(amount is more than 1000) and (country is “DE”)");
    expect(describeExpr("(1 +", label)).toBeNull();
  });
  it("renames a field inside formulas, but not in text or function names", () => {
    expect(renameRef('qty * 2 & "qty" + sum(qty, qtyx) + qty.a + {qty}', "qty", "count"))
      .toBe('count * 2 & "qty" + sum(count, qtyx) + count.a + {count}');
    expect(renameRef("sum(a)", "sum", "total")).toBe("sum(a)");
    expect(renameRef("a + 1", "a", "not")).toBe("{not} + 1");
  });
  it("renameField follows every place a key is used", () => {
    const f = renameField(F("t", "number", "T", {
      calculation: "a * 2", show_when: { rules: [{ field: "a", op: "gt", value: 1 }] },
      required_when: { expression: "a > 5" }, checks: [{ expression: "t > a", message: "m" }],
      on_change: [{ action: "set", target: "a", expression: "a + 1" }],
    }), "a", "b") as any;
    expect(f.calculation).toBe("b * 2");
    expect(f.show_when.rules[0].field).toBe("b");
    expect(f.required_when.expression).toBe("b > 5");
    expect(f.checks[0].expression).toBe("t > b");
    expect(f.on_change[0]).toEqual({ action: "set", target: "b", expression: "b + 1" });
  });
  it("turns picked conditions into the same formula", () => {
    expect(whenToFormula({ match: "any", rules: [{ field: "c", op: "equals", value: "DE" }, { field: "n", op: "gt", value: 3 }] }))
      .toBe('c = "DE" or n > 3');
  });
  it("describes everything a field does in sentences", () => {
    const fields = [F("qty", "number", "Quantity"), F("price", "number", "Unit price"),
      F("total", "number", "Total", { calculation: "qty * price", checks: [{ expression: "total < 1000" }] }),
      F("why", "text", "Why", { required_when: { expression: "total > 500" }, read_only_when: { rules: [{ field: "qty", op: "is_empty" }] } })];
    expect(fieldSentences(fields[2], fields)).toEqual(["Calculated: Quantity × Unit price.", "Must pass: Total is less than 1000."]);
    expect(fieldSentences(fields[3], fields)).toEqual(["Required when Total is more than 500.", "Read-only when is_empty(Quantity)."]
      .map(s => s.replace("is_empty(Quantity)", "Quantity is empty")));
  });
});

describe("in the builder", () => {
  it("a field is made Calculated from a preset, and the panel reads it back", () => {
    const h = setup(def([F("qty", "number", "Quantity"), F("price", "number", "Unit price"), F("total", "number", "Total")]));
    fireEvent.click(screen.getByTestId("field-card-total"));
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "Value" })).getByRole("radio", { name: "Calculated" }));
    fireEvent.change(screen.getByLabelText("Calculation type"), { target: { value: "multiply" } });
    fireEvent.change(screen.getByLabelText("First"), { target: { value: "qty" } });
    fireEvent.change(screen.getByLabelText("Second"), { target: { value: "price" } });
    fireEvent.click(screen.getByRole("button", { name: "Use this calculation" }));
    expect(field(h.get(), "total").calculation).toBe("qty * price");
    expect(screen.getByLabelText("What this field does").textContent).toContain("Calculated: Quantity × Unit price.");
    expect(screen.getByText("ƒ Calculated")).toBeTruthy();
  });

  it("a typed formula is checked as it is typed", () => {
    setup(def([F("a", "number", "A"), F("t", "number", "T", { calculation: "" })]));
    fireEvent.click(screen.getByTestId("field-card-t"));
    fireEvent.change(screen.getByLabelText("Calculation"), { target: { value: "a * (2" } });
    expect(screen.getByRole("alert").textContent).toMatch(/bracket is not closed/);
    fireEvent.change(screen.getByLabelText("Calculation"), { target: { value: "a * gone" } });
    expect(screen.getByRole("alert").textContent).toMatch(/“gone” is not a field/);
  });

  it("required only when — set in the Logic tab with the pickers", () => {
    const h = setup(def([F("country", "text", "Country"), F("tax", "text", "Tax ID")]));
    fireEvent.click(screen.getByTestId("field-card-tax"));
    fireEvent.click(screen.getByRole("tab", { name: "Logic" }));
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "Required" })).getByRole("radio", { name: "Only when…" }));
    fireEvent.click(screen.getByRole("button", { name: "+ Required when…" }));
    expect(field(h.get(), "tax").required_when).toMatchObject({ match: "all", rules: [{ field: "country", op: "equals" }] });
  });

  it("a cross-field check is added in the Validation tab", () => {
    const h = setup(def([F("start", "date", "Start"), F("end", "date", "End")]));
    fireEvent.click(screen.getByTestId("field-card-end"));
    fireEvent.click(screen.getByRole("tab", { name: "Validation" }));
    fireEvent.click(screen.getByRole("button", { name: "+ Add a check" }));
    fireEvent.change(screen.getByLabelText("Check"), { target: { value: "end >= start" } });
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "End must be after start" } });
    expect(field(h.get(), "end").checks).toEqual([{ expression: "end >= start", message: "End must be after start" }]);
  });

  it("renaming a key carries into other fields' formulas", () => {
    const h = setup(def([F("qty", "number", "Quantity"), F("t", "number", "T", { calculation: "qty * 2" })]));
    fireEvent.click(screen.getByTestId("field-card-qty"));
    fireEvent.click(screen.getByRole("button", { name: /Advanced/ }));
    fireEvent.change(screen.getByDisplayValue("qty"), { target: { value: "amount" } });
    expect(field(h.get(), "t").calculation).toBe("amount * 2");
  });
});

describe("in the form", () => {
  it("recomputes, requires and runs on-change actions as the person types", () => {
    const d = def([
      F("qty", "number", "Quantity", { on_change: [{ action: "set", target: "note", expression: "\"x\" & qty" }] }),
      F("price", "number", "Unit price", { default_value: 2.5 }),
      F("total", "number", "Total", { calculation: "qty * price" }),
      F("reason", "text", "Reason", { required_when: { expression: "total > 10" } }),
      F("note", "text", "Note"),
    ]);
    let sent: any = null;
    render(<FormRenderer definition={d} formName="F" onCancel={() => {}} onSubmit={async v => { sent = v; }} />);
    const input = (label: string) =>
      (screen.getByText(label).closest("div[style*='grid-column']") as HTMLElement).querySelector("input") as HTMLInputElement;
    fireEvent.change(input("Quantity"), { target: { value: "8" } });
    expect(input("Total").value).toBe("20");
    expect(input("Total").disabled).toBe(true);
    expect(input("Note").value).toBe("x8");
    fireEvent.click(screen.getByRole("button", { name: /Submit/ }));
    expect(screen.getByText("Reason is required")).toBeTruthy();
    expect(sent).toBeNull();
  });
});
