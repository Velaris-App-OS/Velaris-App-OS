/**
 * Reusable sections and form versions per case in the builder (§5 step 6, 2026-10-29):
 * a linked section shows its fields but cannot be edited here; it can be
 * inserted, saved from a section, edited everywhere or unlinked; the per-case
 * version setting is off unless ticked.
 */
import React, { useState } from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listFormSections: vi.fn(),
  createFormSection: vi.fn(),
  getFormSection: vi.fn(),
  updateFormSection: vi.fn(),
}));
vi.mock("@shared/api/client", async orig => ({ ...(await orig<any>()), ...api }));

import FormBuilder, { FIELD_GROUPS, type FormDefinition } from "./FormBuilder";

const ADDRESS = { id: "sec-1", name: "Address", description: "", version: 3, platform: false, updated_at: null,
  section: { title: "Address", fields: [
    { id: "street", type: "text", label: "Street", field_key: "street", required: true },
    { id: "town", type: "text", label: "Town", field_key: "town", required: false }] } };

function Harness({ initial }: { initial: FormDefinition }) {
  const [def, setDef] = useState(initial);
  return <><FormBuilder definition={def} onChange={setDef} /><pre data-testid="def">{JSON.stringify(def)}</pre></>;
}
const current = (): FormDefinition => JSON.parse(screen.getByTestId("def").textContent!);

beforeEach(() => {
  localStorage.setItem("velaris.formbuilder.palette.open", JSON.stringify(FIELD_GROUPS));
  api.listFormSections.mockResolvedValue({ items: [ADDRESS] });
  api.getFormSection.mockResolvedValue({ ...ADDRESS, used_by: ["Claim"] });
});
afterEach(() => { vi.clearAllMocks(); localStorage.clear(); });

describe("reusable sections", () => {
  it("inserts a linked copy whose fields cannot be edited here", async () => {
    render(<Harness initial={{ sections: [{ id: "own", title: "Own", order: 0, fields: [] }] }} />);
    fireEvent.click(screen.getByRole("button", { name: /Reusable sections/ }));
    fireEvent.click(await screen.findByRole("button", { name: /🔗 Address/ }));
    const d = current();
    expect(d.sections[1].section_ref).toEqual({ id: "sec-1", version: 3 });
    expect(d.sections[1].fields.map(f => f.field_key)).toEqual(["street", "town"]);
    expect(screen.getByText(/linked · v3/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Edit section" })).toBeTruthy();
    // no quick-add buttons and no field cards for the linked section
    expect(screen.getAllByText("Street").length).toBe(1);
  });

  it("saves a section as reusable and links it", async () => {
    api.createFormSection.mockResolvedValue({ ...ADDRESS, id: "sec-9", version: 1 });
    render(<Harness initial={{ sections: [{ id: "s", title: "Bank", order: 0, fields: [
      { id: "iban", type: "iban", label: "IBAN", field_key: "iban", required: false } as any] }] }} />);
    fireEvent.click(screen.getByTitle("Section settings"));
    fireEvent.change(screen.getByLabelText("Reusable section name"), { target: { value: "Bank details" } });
    fireEvent.click(screen.getByRole("button", { name: "Save as reusable section" }));
    await waitFor(() => expect(current().sections[0].section_ref).toEqual({ id: "sec-9", version: 1 }));
    expect(api.createFormSection).toHaveBeenCalledWith({ name: "Bank details",
      section: { title: "Bank", fields: [expect.objectContaining({ field_key: "iban" })] } });
  });

  it("edits the section everywhere and this form's copy follows", async () => {
    api.updateFormSection.mockResolvedValue({ ...ADDRESS, version: 4, updated_forms: ["Claim"],
      section: { ...ADDRESS.section, fields: [...ADDRESS.section.fields, { id: "pc", type: "text", label: "Postcode", field_key: "postcode" }] } });
    render(<Harness initial={{ sections: [{ id: "l", title: "Address", order: 0, fields: ADDRESS.section.fields as any,
      section_ref: { id: "sec-1", version: 3 } }] }} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit section" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit reusable section" });
    await waitFor(() => expect(within(dialog).getByText(/linked by 1 form/)).toBeTruthy());
    fireEvent.click(within(dialog).getByRole("button", { name: "Save everywhere" }));
    expect(await within(dialog).findByText(/Updated form\(s\): Claim/)).toBeTruthy();
    expect(current().sections[0].section_ref).toEqual({ id: "sec-1", version: 4 });
    expect(current().sections[0].fields.map(f => f.field_key)).toEqual(["street", "town", "postcode"]);
  });

  it("unlinks: the fields stay, editable, no longer linked", () => {
    render(<Harness initial={{ sections: [{ id: "l", title: "Address", order: 0, fields: ADDRESS.section.fields as any,
      section_ref: { id: "sec-1", version: 3 } }] }} />);
    fireEvent.click(screen.getByRole("button", { name: "Unlink" }));
    expect(current().sections[0].section_ref).toBeUndefined();
    expect(current().sections[0].fields.length).toBe(2);
  });
});

describe("form versions per case", () => {
  it("is off unless ticked", () => {
    render(<Harness initial={{ sections: [{ id: "s", title: "S", order: 0, fields: [] }] }} />);
    const settingsToggle = screen.getAllByRole("button").find(b => /Form settings|one page/i.test(b.textContent || ""));
    if (settingsToggle) fireEvent.click(settingsToggle);
    const box = screen.getByLabelText(/Each case keeps the version/) as HTMLInputElement;
    expect(box.checked).toBe(false);
    fireEvent.click(box);
    expect(current().settings?.versioning).toBe("per_case");
    fireEvent.click(box);
    expect(current().settings?.versioning).toBeUndefined();
  });
});
