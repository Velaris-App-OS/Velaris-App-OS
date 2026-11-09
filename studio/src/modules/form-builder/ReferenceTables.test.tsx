/**
 * Reference tables in forms (Form Builder v2 step 3, 2026-10-29): options
 * through the saved field, a child list following its parent, a chosen row
 * filling other fields, and a table made inside the field's own panel.
 */
import React, { useState } from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const created: any[] = [];
vi.mock("@shared/api/client", async (orig) => ({
  ...(await orig() as any),
  listReferenceTables: vi.fn(async () => created.map(t => ({ ...t, row_count: 1, platform: false, description: "", updated_at: null }))),
  getReferenceTable: vi.fn(async () => ({ rows: [{ data: { code: "BER", name: "Berlin" }, is_active: true }] })),
  createReferenceTable: vi.fn(async (b: any) => { const t = { id: "t-new", ...b }; created.push(t); return t; }),
  replaceReferenceRows: vi.fn(async () => ({})),
}));

import FormRenderer from "./FormRenderer";
import FormBuilder from "./FormBuilder";
import type { FormDefinition, FormFieldDef } from "./FormBuilder";
import { RefFetcherContext, type RefRequest } from "./refOptions";
import { parseCsv, sheetToTable } from "./ReferenceEditor";
import * as client from "@shared/api/client";

const BRANCHES = [{ code: "BER", name: "Berlin", region: "east", fee: 10 }, { code: "DRS", name: "Dresden", region: "east", fee: 8 },
  { code: "MUC", name: "Munich", region: "south", fee: 12 }];
const F = (id: string, type: FormFieldDef["type"], label: string, extra: Partial<FormFieldDef> = {}): FormFieldDef =>
  ({ id, type, label, field_key: id, required: false, ...extra });
const def = (fields: FormFieldDef[]): FormDefinition => ({ sections: [{ id: "s1", title: "S", order: 0, fields }] });

describe("CSV", () => {
  it("parses quotes, tabs and blank lines, and makes columns from headings", () => {
    expect(parseCsv('a,b\n"x, y","say ""hi"""\n\n1,2')).toEqual([["a", "b"], ["x, y", 'say "hi"'], ["1", "2"]]);
    expect(parseCsv("a\tb\n1\t2")).toEqual([["a", "b"], ["1", "2"]]);
    const t = sheetToTable("Code,Branch name,Fee\nBER,Berlin,10\nMUC,Munich,12");
    expect(t.columns).toEqual([{ key: "code", label: "Code", type: "text" }, { key: "branch_name", label: "Branch name", type: "text" },
      { key: "fee", label: "Fee", type: "number" }]);
    expect(t.rows[1].data).toEqual({ code: "MUC", branch_name: "Munich", fee: "12" });
  });
});

describe("in the form", () => {
  it("a child list follows its parent, and a chosen row fills a read-only field", async () => {
    const asked: RefRequest[] = [];
    const fetcher = async (req: RefRequest) => {
      asked.push(req);
      return BRANCHES.filter(b => b.region === req.values.region).map(b => ({ label: b.name, value: b.code, row: { fee: b.fee } }));
    };
    const src = { kind: "reference", value_column: "code", label_column: "name",
      filters: [{ column: "region", field: "region" }], fill: [{ column: "fee", field: "fee" }] };
    const d = def([
      F("region", "dropdown", "Region", { options: [{ label: "East", value: "east" }, { label: "South", value: "south" }] }),
      F("branch", "dropdown", "Branch", { options_source: src } as any),
      F("fee", "number", "Fee", { read_only: true }),
      F("twice", "number", "Twice", { calculation: "fee * 2" }),
    ]);
    let sent: any = null;
    render(<RefFetcherContext.Provider value={fetcher}>
      <FormRenderer definition={d} formName="F" onCancel={() => {}} onSubmit={async v => { sent = v; }} />
    </RefFetcherContext.Provider>);
    const cell = (l: string) => screen.getByText(l).closest("div[style*='grid-column']") as HTMLElement;
    fireEvent.change(cell("Region").querySelector("select")!, { target: { value: "east" } });
    await waitFor(() => expect([...cell("Branch").querySelectorAll("option")].map(o => o.textContent)).toEqual(["Select…", "Berlin", "Dresden"]));
    expect(asked[asked.length - 1]).toMatchObject({ field_key: "branch", values: { region: "east" } });
    fireEvent.change(cell("Branch").querySelector("select")!, { target: { value: "DRS" } });
    expect((cell("Fee").querySelector("input") as HTMLInputElement).value).toBe("8");
    expect((cell("Twice").querySelector("input") as HTMLInputElement).value).toBe("16");
    fireEvent.change(cell("Region").querySelector("select")!, { target: { value: "south" } });
    await waitFor(() => expect([...cell("Branch").querySelectorAll("option")].map(o => o.textContent)).toEqual(["Select…", "Munich"]));
  });
});

describe("in the builder", () => {
  it("makes a table inside the field's panel and uses it", async () => {
    function Harness({ onDef }: { onDef: (d: FormDefinition) => void }) {
      const [d, setD] = useState(def([F("branch", "dropdown", "Branch", { options: [] })]));
      return <FormBuilder definition={d} onChange={n => { setD(n); onDef(n); }} />;
    }
    let last: FormDefinition | null = null;
    render(<Harness onDef={d => { last = d; }} />);
    fireEvent.click(screen.getByTestId("field-card-branch"));
    fireEvent.click(screen.getByRole("button", { name: "Reference table" }));
    fireEvent.click(screen.getByRole("button", { name: "+ New table" }));
    fireEvent.change(screen.getByLabelText("Table name"), { target: { value: "Branches" } });
    fireEvent.change(screen.getByLabelText("Rows"), { target: { value: "code,name\nBER,Berlin" } });
    fireEvent.click(screen.getByRole("button", { name: "Create and use" }));
    await waitFor(() => expect((last!.sections[0].fields[0] as any).options_source)
      .toEqual({ kind: "reference", table_id: "t-new", value_column: "code", label_column: "name", filters: [], fill: [] }));
    expect(client.replaceReferenceRows).toHaveBeenCalledWith("t-new", [{ data: { code: "BER", name: "Berlin" }, is_active: true }]);
  });
});
