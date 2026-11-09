/**
 * Form Builder canvas + settings panel (redesign part 2, 2026-10-28): the key
 * follows the label until the field is saved or set by hand, settings land in
 * the definition without typing JSON, fields move between sections, and the
 * width setting reaches the renderer.
 */
import React, { useState } from "react";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, expect, it, beforeEach } from "vitest";

import FormBuilder, { FIELD_GROUPS } from "./FormBuilder";
import FormRenderer from "./FormRenderer";
import { keyFromLabel } from "./FieldSettings";
import type { FormDefinition } from "./FormBuilder";

// The palette's groups start closed (2026-10-29); these tests use its buttons.
beforeEach(() => { localStorage.setItem("velaris.formbuilder.palette.open", JSON.stringify(FIELD_GROUPS)); });

function def(): FormDefinition {
  return {
    sections: [
      { id: "s1", title: "Applicant", order: 0, fields: [
        { id: "f1", type: "text", label: "Full name", field_key: "full_name", required: false },
        { id: "f2", type: "email", label: "Email", field_key: "email", required: false },
      ] },
      { id: "s2", title: "Address", order: 1, fields: [] },
    ],
  };
}

function Harness({ initial, saved, onDef }: { initial: FormDefinition; saved?: FormDefinition; onDef: (d: FormDefinition) => void }) {
  const [d, setD] = useState(initial);
  return <FormBuilder definition={d} savedDefinition={saved} onChange={n => { setD(n); onDef(n); }} />;
}

function setup(initial = def(), saved?: FormDefinition) {
  let last: FormDefinition = initial;
  render(<Harness initial={initial} saved={saved} onDef={d => { last = d; }} />);
  return { get: () => last };
}

const field = (d: FormDefinition, id: string) => d.sections.flatMap(s => s.fields).find(f => f.id === id)!;

describe("keyFromLabel", () => {
  it("makes a clean unique key", () => {
    expect(keyFromLabel("Date of birth", new Set())).toBe("date_of_birth");
    expect(keyFromLabel("Café owner", new Set())).toBe("cafe_owner");
  });
  it("never starts with a digit and never clashes", () => {
    expect(keyFromLabel("2nd address", new Set())).toBe("field_2nd_address");
    expect(keyFromLabel("Email", new Set(["email"]))).toBe("email_2");
    expect(keyFromLabel("Email", new Set(["email", "email_2"]))).toBe("email_3");
  });
});

describe("FormBuilder", () => {
  it("shows the palette until a field is selected, then its settings", () => {
    setup();
    expect(screen.getByText("Field Types")).toBeTruthy();
    fireEvent.click(screen.getByTestId("field-card-full_name"));
    expect(screen.getByRole("tab", { name: "General" })).toBeTruthy();
    expect(screen.queryByText("Field Types")).toBeNull();
  });

  it("an unsaved field's key follows its label; a saved field's does not", () => {
    const h = setup(def(), def());
    // f1 is in the saved form: its key stays.
    fireEvent.click(screen.getByTestId("field-card-full_name"));
    fireEvent.change(screen.getAllByRole("textbox")[0], { target: { value: "Legal name" } });
    expect(field(h.get(), "f1")).toMatchObject({ label: "Legal name", field_key: "full_name" });
  });

  it("a new field's key follows its label", () => {
    const h = setup();
    fireEvent.click(screen.getAllByText("+ Number")[0]);
    const added = h.get().sections[0].fields[2];
    expect(added.field_key).toBe("number");
    fireEvent.change(screen.getAllByRole("textbox")[0], { target: { value: "Annual income" } });
    expect(h.get().sections[0].fields[2].field_key).toBe("annual_income");
  });

  it("validation, logic and appearance settings land in the definition", () => {
    const h = setup();
    fireEvent.click(screen.getByTestId("field-card-full_name"));

    fireEvent.click(screen.getByRole("tab", { name: "Validation" }));
    fireEvent.click(screen.getByLabelText("Required"));
    fireEvent.change(screen.getAllByRole("textbox")[0], { target: { value: "We need your name" } });

    fireEvent.click(screen.getByRole("tab", { name: "Logic" }));
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "Read-only" })).getByRole("radio", { name: "Yes" }));

    fireEvent.click(screen.getByRole("tab", { name: "Appearance" }));
    fireEvent.click(screen.getByRole("button", { name: "Half" }));

    expect(field(h.get(), "f1")).toMatchObject({
      required: true, required_message: "We need your name", read_only: true, width: "half",
    });
  });

  it("a typed default value is stored as a number for a number field", () => {
    const h = setup();
    fireEvent.click(screen.getAllByText("+ Number")[0]);
    // The canvas draws the field too (disabled); the settings panel's box is the enabled one.
    const input = [...document.querySelectorAll('input[type="number"]')].find(i => !(i as HTMLInputElement).disabled) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "42" } });
    expect(h.get().sections[0].fields[2].default_value).toBe(42);
  });

  it("duplicates a field with a fresh key", () => {
    const h = setup();
    const card = screen.getByTestId("field-card-email");
    fireEvent.mouseEnter(card);
    fireEvent.click(within(card).getByRole("button", { name: "Duplicate" }));
    const keys = h.get().sections[0].fields.map(f => f.field_key);
    expect(keys).toEqual(["full_name", "email", "email_copy"]);
  });

  it("drags a field into another section", () => {
    const h = setup();
    const data: Record<string, string> = {};
    const dataTransfer = {
      setData: (k: string, v: string) => { data[k] = v; },
      getData: (k: string) => data[k] ?? "",
      effectAllowed: "",
    };
    fireEvent.dragStart(screen.getByTestId("field-card-email"), { dataTransfer });
    const empty = screen.getByText("Drag a field type here, or use the buttons below");
    fireEvent.dragOver(empty, { dataTransfer });
    fireEvent.drop(empty, { dataTransfer });
    expect(h.get().sections[0].fields.map(f => f.id)).toEqual(["f1"]);
    expect(h.get().sections[1].fields.map(f => f.id)).toEqual(["f2"]);
  });
});

describe("FormRenderer width", () => {
  it("a half-width field spans half the grid; no width = full, as before", () => {
    const d = def();
    d.sections[0].fields[0].width = "half";
    render(<FormRenderer formName="F" definition={d} onSubmit={async () => {}} onCancel={() => {}} />);
    const cell = (label: string) => screen.getByText(label).closest("div[style*='grid-column']") as HTMLElement;
    expect(cell("Full name").style.gridColumn).toBe("span 3");
    expect(cell("Email").style.gridColumn).toBe("span 6");
  });

  it("a hidden field is not drawn and a read-only one cannot be typed in", () => {
    const d = def();
    d.sections[0].fields[0].hidden = true;
    d.sections[0].fields[1].read_only = true;
    render(<FormRenderer formName="F" definition={d} onSubmit={async () => {}} onCancel={() => {}} />);
    expect(screen.queryByText("Full name")).toBeNull();
    expect((document.querySelector('input[type="email"]') as HTMLInputElement).disabled).toBe(true);
  });
});

describe("per-type settings (part 3)", () => {
  it("a new email field checks the full format and a new phone field the phone pattern", () => {
    const h = setup();
    // A palette click adds to the last section and opens the new field's settings;
    // closing them brings the palette back.
    const add = (label: string) => {
      fireEvent.click(screen.getAllByText(label).find(e => e.closest("[role=button]"))!);
      fireEvent.click(screen.getByRole("button", { name: "Close settings" }));
    };
    add("Email");
    add("Phone");
    const added = h.get().sections[1].fields;
    expect(added[0].validation).toEqual({ format: "strict" });
    expect(added[1].validation).toEqual({ pattern: "phone" });
  });

  it("date bounds, must-be-ticked and file rules land in validation", () => {
    const d = def();
    d.sections[0].fields.push(
      { id: "f3", type: "date", label: "Start", field_key: "start", required: false },
      { id: "f4", type: "file_upload", label: "Scan", field_key: "scan", required: false },
    );
    const h = setup(d);
    fireEvent.click(screen.getByTestId("field-card-start"));
    fireEvent.click(screen.getByRole("tab", { name: "Validation" }));
    fireEvent.click(screen.getByLabelText("No dates in the past"));
    fireEvent.change(screen.getAllByRole("textbox")[1], { target: { value: "today+30" } });
    expect(field(h.get(), "f3").validation).toEqual({ no_past: true, latest: "today+30" });

    fireEvent.click(screen.getByTestId("field-card-scan"));
    fireEvent.click(screen.getByRole("tab", { name: "Validation" }));
    fireEvent.change(screen.getAllByRole("textbox")[0], { target: { value: ".pdf, .png" } });
    fireEvent.click(screen.getByLabelText("Allow several files"));
    expect(field(h.get(), "f4").validation).toEqual({ accept: [".pdf", ".png"], multiple: true });
  });

  it("a custom pattern is edited as text and a bad one is flagged, not saved as an error for the person", () => {
    const h = setup();
    fireEvent.click(screen.getByTestId("field-card-full_name"));
    fireEvent.click(screen.getByRole("tab", { name: "Validation" }));
    fireEvent.change(screen.getByDisplayValue("Any text"), { target: { value: "custom" } });
    const pattern = screen.getByDisplayValue(".*");
    fireEvent.change(pattern, { target: { value: "([A-Z" } });
    expect(screen.getByText(/This pattern is not valid/)).toBeTruthy();
    expect(field(h.get(), "f1").validation?.pattern).toBe("([A-Z");
  });
});

describe("FormRenderer per type", () => {
  it("date bounds become the picker's min and max; file types its accept list", () => {
    const d: FormDefinition = { sections: [{ id: "s", title: "S", order: 0, fields: [
      { id: "a", type: "date", label: "D", field_key: "d", required: false, validation: { earliest: "2026-01-01", latest: "2026-12-31" } },
      { id: "b", type: "file_upload", label: "F", field_key: "f", required: false, validation: { accept: ["pdf"], multiple: true } },
    ] }] };
    render(<FormRenderer formName="F" definition={d} onSubmit={async () => {}} onCancel={() => {}} />);
    const date = document.querySelector('input[type="date"]') as HTMLInputElement;
    expect([date.min, date.max]).toEqual(["2026-01-01", "2026-12-31"]);
    const file = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(file.accept).toBe(".pdf");
    expect(file.multiple).toBe(true);
  });

  it("a counter and a unit are drawn when set", () => {
    const d: FormDefinition = { sections: [{ id: "s", title: "S", order: 0, fields: [
      { id: "a", type: "text", label: "T", field_key: "t", required: false, validation: { max_length: 20 }, display: { counter: true } },
      { id: "b", type: "number", label: "N", field_key: "n", required: false, validation: { unit: "kg" } },
    ] }] };
    render(<FormRenderer formName="F" definition={d} initialValues={{ t: "hello" }} onSubmit={async () => {}} onCancel={() => {}} />);
    expect(screen.getByText("5 / 20")).toBeTruthy();
    expect(screen.getByText("kg")).toBeTruthy();
  });
});

describe("choices (part 4)", () => {
  const withChoice = (extra: Partial<FormDefinition["sections"][0]["fields"][0]> = {}): FormDefinition => {
    const d = def();
    d.sections[0].fields.push({ id: "c", type: "dropdown", label: "Country", field_key: "country", required: false,
      options: [{ label: "Germany", value: "germany" }, { label: "France", value: "france" }], ...extra });
    return d;
  };

  it("label and value are separate; the value follows the label until set by hand", () => {
    const h = setup(withChoice());
    fireEvent.click(screen.getByTestId("field-card-country"));
    fireEvent.change(screen.getByDisplayValue("Germany"), { target: { value: "Deutschland" } });
    expect(field(h.get(), "c").options![0]).toEqual({ label: "Deutschland", value: "deutschland" });
    fireEvent.change(screen.getByDisplayValue("deutschland"), { target: { value: "DE" } });
    fireEvent.change(screen.getByDisplayValue("Deutschland"), { target: { value: "Germany" } });
    expect(field(h.get(), "c").options![0]).toEqual({ label: "Germany", value: "DE" });
  });

  it("pastes a list, label | value supported", () => {
    const h = setup(withChoice());
    fireEvent.click(screen.getByTestId("field-card-country"));
    fireEvent.click(screen.getByText("Paste a list"));
    fireEvent.change(screen.getByPlaceholderText(/One per line/), { target: { value: "Italy\nSpain | ES\n\n" } });
    fireEvent.click(screen.getByText("Add 2 options"));
    expect(field(h.get(), "c").options!.slice(2)).toEqual([{ label: "Italy", value: "italy" }, { label: "Spain", value: "ES" }]);
  });

  it("marks a default option and warns about duplicate values", () => {
    const h = setup(withChoice({ options: [{ label: "A", value: "x" }, { label: "B", value: "x" }] }));
    fireEvent.click(screen.getByTestId("field-card-country"));
    fireEvent.click(screen.getByLabelText("Default: A"));
    expect(field(h.get(), "c").default_value).toBe("x");
    expect(screen.getByText(/share the value "x"/)).toBeTruthy();
  });

  it("the renderer fills defaults, but pre-filled case data wins", () => {
    const d = withChoice({ default_value: "france" });
    d.sections[0].fields[0].default_value = "Jane";
    render(<FormRenderer formName="F" definition={d} initialValues={{ full_name: "From case" }} onSubmit={async () => {}} onCancel={() => {}} />);
    expect((screen.getByDisplayValue("From case"))).toBeTruthy();
    expect((document.querySelector("select") as HTMLSelectElement).value).toBe("france");
  });

  it("a long list gets a search box", () => {
    const options = Array.from({ length: 12 }, (_, i) => ({ label: `City ${i}`, value: `c${i}` }));
    render(<FormRenderer formName="F" definition={withChoice({ options })} onSubmit={async () => {}} onCancel={() => {}} />);
    const box = screen.getByRole("combobox");
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "city 11" } });
    expect(screen.getAllByRole("option").map(o => o.textContent)).toEqual(["City 11"]);
  });

  it("options from a connector are fetched through the saved form", async () => {
    const calls: any[] = [];
    const orig = globalThis.fetch;
    globalThis.fetch = (async (url: string, init: any) => {
      calls.push({ url, body: JSON.parse(init.body) });
      return { ok: true, json: async () => ({ ok: true, result: { data: { items: [{ code: "B1", title: "Berlin" }] } } }) } as any;
    }) as any;
    try {
      const d = withChoice({ options: [], options_source: { kind: "connector", connector_id: "conn-1", list_path: "data.items", label_path: "title", value_path: "code" } });
      render(<FormRenderer formName="F" formId="form-9" definition={d} onSubmit={async () => {}} onCancel={() => {}} />);
      expect(await screen.findByRole("option", { name: "Berlin" })).toBeTruthy();
      expect(calls[0].url).toBe("/api/v1/hxbridge/connectors/conn-1/form-lookup");
      expect(calls[0].body).toMatchObject({ form_id: "form-9", field_key: "country" });
    } finally {
      globalThis.fetch = orig;
    }
  });
});

describe("show/hide rules (part 5)", () => {
  const form = (): FormDefinition => ({ sections: [{ id: "s", title: "S", order: 0, fields: [
    { id: "a", type: "dropdown", label: "Country", field_key: "country", required: false,
      options: [{ label: "Germany", value: "de" }, { label: "France", value: "fr" }] },
    { id: "b", type: "text", label: "Tax id", field_key: "tax_id", required: true },
  ] }] });

  it("builds a rule from pickers", () => {
    const h = setup(form());
    fireEvent.click(screen.getByTestId("field-card-tax_id"));
    fireEvent.click(screen.getByRole("tab", { name: "Logic" }));
    fireEvent.click(screen.getByText("+ Show only when…"));
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "de" } });
    expect(field(h.get(), "b").show_when).toEqual({ match: "all", rules: [{ field: "country", op: "equals", value: "de" }] });
    expect(within(screen.getByTestId("field-card-tax_id")).getByText("Conditional")).toBeTruthy();
  });

  it("renaming a field's key keeps the rules that use it", () => {
    const d = form();
    d.sections[0].fields[1].show_when = { match: "all", rules: [{ field: "country", op: "equals", value: "de" }] };
    const h = setup(d);
    fireEvent.click(screen.getByTestId("field-card-country"));
    fireEvent.click(screen.getByText(/Advanced/));
    fireEvent.change(screen.getByDisplayValue("country"), { target: { value: "land" } });
    expect(field(h.get(), "b").show_when!.rules[0].field).toBe("land");
  });

  it("warns about a rule on a missing field and about a loop", () => {
    const d = form();
    d.sections[0].fields[0].show_when = { rules: [{ field: "tax_id", op: "is_empty" }] };
    d.sections[0].fields[1].show_when = { rules: [{ field: "country", op: "not_empty" }, { field: "gone", op: "is_empty" }] };
    setup(d);
    fireEvent.click(screen.getByTestId("field-card-tax_id"));
    fireEvent.click(screen.getByRole("tab", { name: "Logic" }));
    expect(screen.getByText(/go round in a circle/)).toBeTruthy();
    expect(screen.getByText(/"gone", which is no longer on the form/)).toBeTruthy();
  });

  it("the renderer shows the field only when the rule holds, and does not send it otherwise", async () => {
    const d = form();
    d.sections[0].fields[1].show_when = { match: "all", rules: [{ field: "country", op: "equals", value: "de" }] };
    let sent: any = null;
    render(<FormRenderer formName="F" definition={d} initialValues={{ tax_id: "left over" }}
      onSubmit={async v => { sent = v; }} onCancel={() => {}} />);
    expect(screen.queryByText("Tax id")).toBeNull();
    fireEvent.change(document.querySelector("select")!, { target: { value: "de" } });
    expect(screen.getByText("Tax id")).toBeTruthy();
    fireEvent.change(document.querySelector("select")!, { target: { value: "fr" } });
    fireEvent.click(screen.getByText("Submit"));
    await screen.findByText("Submit");
    expect(sent).toEqual({ country: "fr" });
  });
});

describe("pre-fill variable picker (part 6)", () => {
  it("lists the case variables of the case types using the form", async () => {
    const orig = globalThis.fetch;
    globalThis.fetch = (async () => ({
      ok: true, status: 200,
      json: async () => ({ case_types: [{ id: "ct", name: "Motor claim" }],
        variables: [{ full_key: "crm.account_status", namespace: "crm", label: "Account status", var_type: "string", case_types: ["Motor claim"] }] }),
    })) as any;
    try {
      let last: FormDefinition = def();
      function H() {
        const [d, setD] = useState(def());
        return <FormBuilder definition={d} formId="form-1" onChange={n => { setD(n); last = n; }} />;
      }
      render(<H />);
      fireEvent.click(screen.getByTestId("field-card-full_name"));
      fireEvent.click(screen.getByRole("tab", { name: "Data" }));
      const select = await screen.findByDisplayValue("— none —");
      fireEvent.change(select, { target: { value: "crm.account_status" } });
      expect(field(last, "f1").variable).toBe("crm.account_status");
    } finally {
      globalThis.fetch = orig;
    }
  });
});

describe("lookups (part 7)", () => {
  const lookupForm = (source: any = {}): FormDefinition => ({ sections: [{ id: "s", title: "S", order: 0, fields: [
    { id: "a", type: "text", label: "Customer id", field_key: "cust", required: false },
    { id: "b", type: "text", label: "Name", field_key: "name", required: false },
    { id: "c", type: "connector_lookup", label: "Find", field_key: "find", required: false, source },
  ] }] });

  function mockFetch(handler: (url: string, body: any) => any) {
    const orig = globalThis.fetch;
    const calls: any[] = [];
    globalThis.fetch = (async (url: string, init: any) => {
      const body = init?.body ? JSON.parse(init.body) : undefined;
      calls.push({ url, body });
      const data = handler(url, body);
      return { ok: true, status: 200, json: async () => data } as any;
    }) as any;
    return { calls, restore: () => { globalThis.fetch = orig; } };
  }

  it("is set up from pickers: connector, inputs, a test, and paths from its result", async () => {
    const f = mockFetch(url => url.includes("/hxbridge/connectors?")
      ? { connectors: [{ id: "k1", name: "CRM", connector_type: "http", enabled: true }] }
      : { ok: true, result: { customers: [{ name: "Ann", tier: "gold" }] } });
    try {
      const h = setup(lookupForm());
      fireEvent.click(screen.getByTestId("field-card-find"));
      fireEvent.click(screen.getByRole("tab", { name: "Data" }));
      fireEvent.change(await screen.findByLabelText("Connector"), { target: { value: "k1" } });
      fireEvent.click(screen.getByText("+ Add an input"));
      fireEvent.change(screen.getByLabelText("Parameter"), { target: { value: "customer_id" } });
      fireEvent.change(screen.getByLabelText("From field"), { target: { value: "cust" } });
      fireEvent.change(screen.getByLabelText("Test value customer_id"), { target: { value: "C-1" } });
      fireEvent.click(screen.getByText("▶ Run a test"));
      await screen.findByText(/"tier": "gold"/);
      expect(f.calls[f.calls.length - 1].body).toMatchObject({ input_data: { customer_id: "C-1" }, field_key: "find" });
      fireEvent.change(screen.getByLabelText("Record"), { target: { value: "customers" } });
      fireEvent.click(screen.getByText("+ Add a value"));
      fireEvent.change(screen.getByLabelText("Into field"), { target: { value: "name" } });
      expect(field(h.get(), "c").source).toMatchObject({
        connector_id: "k1", input_mapping: { customer_id: "cust" },
        response_path: "customers", target_fields: { name: "name" },
      });
    } finally { f.restore(); }
  });

  it("at run time: the record path, and the person chooses among several matches", async () => {
    const f = mockFetch(() => ({ ok: true, result: { customers: [{ name: "Ann" }, { name: "Bob" }] } }));
    try {
      const d = lookupForm({ connector_id: "k1", input_mapping: { customer_id: "cust" }, response_path: "customers",
        target_fields: { name: "name" }, on_many: "choose", button_label: "Find customer" });
      render(<FormRenderer formName="F" formId="form-1" definition={d} onSubmit={async () => {}} onCancel={() => {}} />);
      fireEvent.click(screen.getByText("⚡ Find customer"));
      fireEvent.click(await screen.findByRole("option", { name: "Bob" }));
      expect(screen.getByDisplayValue("Bob")).toBeTruthy();
    } finally { f.restore(); }
  });

  it("runs by itself once its inputs are filled", async () => {
    const f = mockFetch(() => ({ ok: true, result: { name: "Ann" } }));
    try {
      const d = lookupForm({ connector_id: "k1", input_mapping: { customer_id: "cust" }, target_fields: { name: "name" }, trigger: "change" });
      render(<FormRenderer formName="F" formId="form-1" definition={d} onSubmit={async () => {}} onCancel={() => {}} />);
      expect(f.calls.length).toBe(0);
      fireEvent.change(document.querySelectorAll("input")[0], { target: { value: "C-1" } });
      expect(await screen.findByDisplayValue("Ann", undefined, { timeout: 2000 })).toBeTruthy();
      expect(f.calls[0].body.input_data).toEqual({ customer_id: "C-1" });
    } finally { f.restore(); }
  });
});

describe("record details and tables (part 8)", () => {
  const sampleResult = { carts: [
    { id: 2, total: 50.5, discountedTotal: 45, products: [{ title: "Hat" }] },
    { id: 1, total: 13037.88, discountedTotal: 11510.81, products: [] },
  ] };
  const form = (extra: any = {}): FormDefinition => ({ sections: [{ id: "s", title: "S", order: 0, fields: [
    { id: "l", type: "connector_lookup", label: "Find", field_key: "find", required: false, default_value: sampleResult },
    { id: "t", type: "table_display", label: "Orders", field_key: "orders", required: false, ...extra },
  ] }] });

  it("a table is set up from pickers: data from a lookup, rows path, columns with headings and formats", () => {
    const h = setup(form());
    fireEvent.click(screen.getByTestId("field-card-orders"));
    fireEvent.click(screen.getByRole("tab", { name: "Data" }));
    fireEvent.change(screen.getByLabelText("Data from"), { target: { value: "find" } });
    fireEvent.change(screen.getByLabelText("Rows at"), { target: { value: "carts" } });
    fireEvent.click(screen.getByText("Add all from the sample"));
    expect(field(h.get(), "t").columns!.map(c => c.path)).toEqual(["id", "total", "discountedTotal"]);
    fireEvent.change(screen.getAllByLabelText("Heading")[1], { target: { value: "Amount" } });
    fireEvent.change(screen.getAllByLabelText("Format")[1], { target: { value: "currency" } });
    expect(field(h.get(), "t")).toMatchObject({ from: "find", rows_path: "carts" });
    expect(field(h.get(), "t").columns![1]).toEqual({ path: "total", label: "Amount", format: "currency" });
  });

  it("renders chosen columns, labelled and formatted, sorted and paged", () => {
    const d = form({ from: "find", rows_path: "carts", sortable: true, page_size: 1,
      columns: [{ path: "id", label: "Cart" }, { path: "total", label: "Amount", format: "currency", currency: "EUR" }] });
    render(<FormRenderer formName="F" definition={d} initialValues={{ find: sampleResult }} onSubmit={async () => {}} onCancel={() => {}} />);
    expect(screen.getByText("Cart")).toBeTruthy();
    expect(screen.queryByText("discountedTotal")).toBeNull();
    expect(screen.getByText("Page 1 of 2")).toBeTruthy();
    fireEvent.click(screen.getByText("Cart"));                 // sort by cart id
    expect(screen.getByText("1")).toBeTruthy();
    expect(screen.getByText(/13,037\.88/)).toBeTruthy();
  });

  it("with no columns chosen a table behaves as before", () => {
    const d = form();
    render(<FormRenderer formName="F" definition={d} initialValues={{ orders: [{ order: "A-1", total: 5 }] }} onSubmit={async () => {}} onCancel={() => {}} />);
    expect(screen.getByText("order")).toBeTruthy();
    expect(screen.getByText("A-1")).toBeTruthy();
  });

  it("record details shows chosen values, and a link only when it is a web link", () => {
    const d: FormDefinition = { sections: [{ id: "s", title: "S", order: 0, fields: [
      { id: "r", type: "record_details", label: "Customer", field_key: "cust", required: false,
        columns: [{ path: "name" }, { path: "vip", format: "yes_no" }, { path: "site", format: "link" }, { path: "bad", format: "link" }] },
    ] }] };
    render(<FormRenderer formName="F" definition={d}
      initialValues={{ cust: { name: "Ann", vip: true, site: "https://example.com", bad: "javascript:alert(1)" } }}
      onSubmit={async () => {}} onCancel={() => {}} />);
    expect(screen.getByText("Name")).toBeTruthy();
    expect(screen.getByText("Yes")).toBeTruthy();
    expect(screen.getByRole("link").getAttribute("href")).toBe("https://example.com");
    expect(screen.getAllByRole("link")).toHaveLength(1);
  });

  it("a legacy JSON display converts to record details and the palette no longer offers it", () => {
    const d: FormDefinition = { sections: [{ id: "s", title: "S", order: 0, fields: [
      { id: "j", type: "json_display", label: "Raw", field_key: "raw", required: false } ] }] };
    const h = setup(d);
    expect(screen.queryByText("JSON Display (legacy)")).toBeNull();
    fireEvent.click(screen.getByTestId("field-card-raw"));
    fireEvent.click(screen.getByRole("tab", { name: "Data" }));
    fireEvent.click(screen.getByText("Convert to Record details"));
    expect(field(h.get(), "j")).toMatchObject({ type: "record_details", show_raw: true, field_key: "raw" });
  });
});

describe("form level and step by step (part 9)", () => {
  const twoPages = (settings: any = {}): FormDefinition => ({ settings, sections: [
    { id: "p1", title: "About you", order: 0, description: "Who is asking", fields: [
      { id: "a", type: "text", label: "Name", field_key: "name", required: true } ] },
    { id: "p2", title: "Your request", order: 1, fields: [
      { id: "b", type: "dropdown", label: "Topic", field_key: "topic", required: true,
        options: [{ label: "Billing", value: "billing" }] } ] },
  ] });

  it("form settings land in the definition", () => {
    const h = setup();
    fireEvent.click(screen.getByText(/Form settings/));
    fireEvent.click(screen.getByText("Step by step"));
    fireEvent.click(screen.getByLabelText("End with a page to check the answers"));
    fireEvent.click(screen.getByRole("button", { name: "2" }));
    fireEvent.change(screen.getByLabelText("Submit button text"), { target: { value: "Send request" } });
    expect(h.get().settings).toEqual({ mode: "steps", review_page: true, columns: 2, submit_label: "Send request" });
  });

  it("section settings: description, can be closed, starts closed", () => {
    const h = setup();
    fireEvent.click(screen.getAllByRole("button", { name: "Section settings" })[0]);
    fireEvent.change(screen.getByLabelText("Section description"), { target: { value: "Tell us about you" } });
    fireEvent.click(screen.getByLabelText("The person can close it"));
    fireEvent.click(screen.getByLabelText("Starts closed"));
    expect(h.get().sections[0]).toMatchObject({ description: "Tell us about you", collapsible: true, collapsed: true });
  });

  it("step by step: Next checks only its page, a review lists the answers, then the own Submit text", async () => {
    let sent: any = null;
    render(<FormRenderer formName="F" definition={twoPages({ mode: "steps", review_page: true, submit_label: "Send request" })}
      onSubmit={async v => { sent = v; }} onCancel={() => {}} />);
    expect(screen.getByText(/Step 1 of 2/)).toBeTruthy();
    expect(screen.queryByText("Topic")).toBeNull();
    fireEvent.click(screen.getByText("Next"));
    expect(screen.getByText("Name is required")).toBeTruthy();
    fireEvent.change(document.querySelector("input")!, { target: { value: "Ann" } });
    fireEvent.click(screen.getByText("Next"));
    expect(screen.getByText("Topic")).toBeTruthy();
    fireEvent.change(document.querySelector("select")!, { target: { value: "billing" } });
    fireEvent.click(screen.getByText("Review"));
    expect(screen.getByText("Check your answers")).toBeTruthy();
    expect(screen.getByText("Billing")).toBeTruthy();
    fireEvent.click(screen.getByText("Send request"));
    await screen.findByText("Send request");
    expect(sent).toEqual({ name: "Ann", topic: "billing" });
  });

  it("a collapsible section that starts closed opens on a click; two fields per row", () => {
    const d = twoPages({ columns: 2 });
    d.sections[1].collapsible = true;
    d.sections[1].collapsed = true;
    render(<FormRenderer formName="F" definition={d} onSubmit={async () => {}} onCancel={() => {}} />);
    expect(screen.getByText("Who is asking")).toBeTruthy();
    expect(screen.queryByText("Topic")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Your request/ }));
    expect(screen.getByText("Topic")).toBeTruthy();
    const cell = screen.getByText("Name").closest("div[style*='grid-column']") as HTMLElement;
    expect(cell.style.gridColumn).toBe("span 3");
  });

  it("a read-only view shows every section at once", () => {
    render(<FormRenderer formName="F" definition={twoPages({ mode: "steps" })} readOnly initialValues={{ name: "Ann" }}
      onSubmit={async () => {}} onCancel={() => {}} />);
    expect(screen.getByText("Name")).toBeTruthy();
    expect(screen.getByText("Topic")).toBeTruthy();
  });
});

describe("closed sections and errors", () => {
  it("Submit opens a closed section that has an error", () => {
    const d: FormDefinition = { sections: [{ id: "s", title: "Hidden away", order: 0, collapsible: true, collapsed: true, fields: [
      { id: "a", type: "text", label: "Name", field_key: "name", required: true } ] }] };
    render(<FormRenderer formName="F" definition={d} onSubmit={async () => {}} onCancel={() => {}} />);
    expect(screen.queryByText("Name")).toBeNull();
    fireEvent.click(screen.getByText("Submit"));
    expect(screen.getByText("Name is required")).toBeTruthy();
  });
});
