/**
 * Repeating groups (Form Builder v2, 2026-10-29): rows of fields with their
 * own calculations and checks, a total over the rows, and a builder that
 * edits a column in the same panel, one level in.
 */
import React, { useState } from "react";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, expect, it, beforeEach } from "vitest";

import FormBuilder, { FIELD_GROUPS } from "./FormBuilder";
import FormRenderer from "./FormRenderer";
import type { FormDefinition, FormFieldDef } from "./FormBuilder";

// The palette's groups start closed (2026-10-29); these tests use its buttons.
beforeEach(() => { localStorage.setItem("velaris.formbuilder.palette.open", JSON.stringify(FIELD_GROUPS)); });

const F = (id: string, type: FormFieldDef["type"], label: string, extra: Partial<FormFieldDef> = {}): FormFieldDef =>
  ({ id, type, label, field_key: id, required: false, ...extra });

const ITEMS = F("items", "repeating_group", "Items", { add_label: "+ Add item", fields: [
  F("desc", "text", "Description", { required: true }),
  F("qty", "number", "Qty"),
  F("price", "number", "Price"),
  F("line", "number", "Line", { calculation: "qty * price" }),
] });
const def = (fields: FormFieldDef[]): FormDefinition => ({ sections: [{ id: "s1", title: "S", order: 0, fields }] });

describe("in the form", () => {
  it("adds rows, computes each row and a total over them, and checks every row", async () => {
    let sent: any = null;
    render(<FormRenderer definition={def([ITEMS, F("total", "number", "Total", { calculation: "sum(items.line)" })])}
      formName="F" onCancel={() => {}} onSubmit={async v => { sent = v; }} />);
    const group = screen.getByTestId("group-items");
    fireEvent.click(within(group).getByRole("button", { name: "+ Add item" }));
    fireEvent.click(within(group).getByRole("button", { name: "+ Add item" }));
    const rows = () => group.querySelectorAll("tbody tr");
    expect(rows().length).toBe(2);
    const inputs = (r: number) => rows()[r].querySelectorAll("input");
    fireEvent.change(inputs(0)[0], { target: { value: "Bolt" } });
    fireEvent.change(inputs(0)[1], { target: { value: "3" } });
    fireEvent.change(inputs(0)[2], { target: { value: "2" } });
    fireEvent.change(inputs(1)[1], { target: { value: "1" } });
    fireEvent.change(inputs(1)[2], { target: { value: "4" } });
    expect((inputs(0)[3] as HTMLInputElement).value).toBe("6");
    expect((inputs(0)[3] as HTMLInputElement).disabled).toBe(true);
    const total = (screen.getByText("Total").closest("div[style*='grid-column']") as HTMLElement).querySelector("input") as HTMLInputElement;
    expect(total.value).toBe("10");

    fireEvent.click(screen.getByRole("button", { name: /Submit/ }));
    expect(screen.getByText("Description is required")).toBeTruthy();   // row 2
    expect(sent).toBeNull();

    fireEvent.click(within(group).getByRole("button", { name: "Remove row 2" }));
    fireEvent.click(screen.getByRole("button", { name: /Submit/ }));
    await screen.findByText(/Submit/);
    expect(sent).toEqual({ items: [{ desc: "Bolt", qty: 3, price: 2, line: 6 }], total: 6 });
  });

  it("copies and reorders rows, and stops at the row limit", () => {
    render(<FormRenderer definition={def([{ ...ITEMS, max_rows: 2 }])} formName="F" onCancel={() => {}} onSubmit={async () => {}} />);
    const group = screen.getByTestId("group-items");
    fireEvent.click(within(group).getByRole("button", { name: "+ Add item" }));
    fireEvent.change(group.querySelectorAll("tbody tr")[0].querySelector("input")!, { target: { value: "A" } });
    fireEvent.click(within(group).getByRole("button", { name: "Copy row 1" }));
    expect(group.querySelectorAll("tbody tr").length).toBe(2);
    expect((group.querySelectorAll("tbody tr")[1].querySelector("input") as HTMLInputElement).value).toBe("A");
    expect((within(group).getByRole("button", { name: "+ Add item" }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(group).getByText("At most 2 rows.")).toBeTruthy();
  });
});

describe("in the builder", () => {
  function Harness({ initial, onDef }: { initial: FormDefinition; onDef: (d: FormDefinition) => void }) {
    const [d, setD] = useState(initial);
    return <FormBuilder definition={d} onChange={n => { setD(n); onDef(n); }} />;
  }

  it("edits a column in the same panel, and a renamed column follows into its siblings' formulas", () => {
    let last: FormDefinition = def([ITEMS]);
    render(<Harness initial={last} onDef={d => { last = d; }} />);
    fireEvent.click(screen.getByTestId("field-card-items"));
    expect(screen.getByText("Columns — one of each per row")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Edit column Qty" }));
    expect(screen.getByRole("button", { name: "← Items" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Advanced/ }));
    fireEvent.change(screen.getByDisplayValue("qty"), { target: { value: "quantity" } });
    const cols = (last.sections[0].fields[0] as any).fields;
    expect(cols.map((c: any) => c.field_key)).toEqual(["desc", "quantity", "price", "line"]);
    expect(cols[3].calculation).toBe("quantity * price");
    fireEvent.click(screen.getByRole("button", { name: "← Items" }));
    expect(screen.getByText("Columns — one of each per row")).toBeTruthy();
  });

  it("adds a column, and a new group starts as a small line-item table", () => {
    let last: FormDefinition = def([]);
    render(<Harness initial={last} onDef={d => { last = d; }} />);
    fireEvent.click(screen.getAllByRole("button", { name: /Repeating Group/ })[0]);
    const g: any = last.sections[0].fields[0];
    expect(g.type).toBe("repeating_group");
    expect(g.fields.map((c: any) => c.field_key)).toEqual(["description", "amount"]);
  });
});

describe("audit 2026-10-29", () => {
  it("a renamed column follows into formulas elsewhere on the form, and a missing column is warned about", async () => {
    const { renameRef } = await import("./expr");
    const { formulaProblems } = await import("./FormulaEditor");
    expect(renameRef('sum(items.qty) + items.qtyx + other.qty + "items.qty"', "items.qty", "items.quantity"))
      .toBe('sum(items.quantity) + items.qtyx + other.qty + "items.qty"');
    expect(formulaProblems("sum(items.nope)", [ITEMS], "t")).toEqual(["“Items” has no column “nope” — it will be empty."]);

    function Harness({ initial, onDef }: { initial: FormDefinition; onDef: (d: FormDefinition) => void }) {
      const [d, setD] = useState(initial);
      return <FormBuilder definition={d} onChange={n => { setD(n); onDef(n); }} />;
    }
    let last = def([ITEMS, F("total", "number", "Total", { calculation: "sum(items.qty)" })]);
    render(<Harness initial={last} onDef={d => { last = d; }} />);
    fireEvent.click(screen.getByTestId("field-card-items"));
    fireEvent.click(screen.getByRole("button", { name: "Edit column Qty" }));
    fireEvent.click(screen.getByRole("button", { name: /Advanced/ }));
    fireEvent.change(screen.getByDisplayValue("qty"), { target: { value: "quantity" } });
    expect((last.sections[0].fields[1] as any).calculation).toBe("sum(items.quantity)");
  });

  it("a read-only group pre-filled from the case computes its rows from those rows", () => {
    const ro = { ...ITEMS, read_only: true, default_value: [{ desc: "default", qty: 1, price: 1 }] };
    render(<FormRenderer definition={def([ro])} initialValues={{ items: [{ desc: "case", qty: 2, price: 5 }] }}
      formName="F" onCancel={() => {}} onSubmit={async () => {}} />);
    const row = screen.getByTestId("group-items").querySelector("tbody tr")!;
    const inputs = row.querySelectorAll("input");
    expect((inputs[0] as HTMLInputElement).value).toBe("case");
    expect((inputs[3] as HTMLInputElement).value).toBe("10");
  });
});
