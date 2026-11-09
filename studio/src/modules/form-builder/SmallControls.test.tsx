/**
 * Small controls (Form Builder, 2026-10-29): each one filled in the way a
 * person would, and what reaches onSubmit is what the server stores.
 */
import React from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { describe, expect, it, beforeEach } from "vitest";

import FormBuilder, { FIELD_GROUPS } from "./FormBuilder";
import FormRenderer from "./FormRenderer";
import type { FormDefinition, FormFieldDef } from "./FormBuilder";

// The palette's groups start closed (2026-10-29); these tests use its buttons.
beforeEach(() => { localStorage.setItem("velaris.formbuilder.palette.open", JSON.stringify(FIELD_GROUPS)); });

const F = (id: string, type: FormFieldDef["type"], label: string, extra: Record<string, any> = {}): FormFieldDef =>
  ({ id, type, label, field_key: id, required: false, ...extra });
const def = (fields: FormFieldDef[]): FormDefinition => ({ sections: [{ id: "s1", title: "S", order: 0, fields }] });

function renderForm(fields: FormFieldDef[]) {
  let sent: any = null;
  const r = render(<FormRenderer definition={def(fields)} formName="F" onCancel={() => {}} onSubmit={async v => { sent = v; }} />);
  return { ...r, sent: () => sent, submit: () => fireEvent.click(screen.getByRole("button", { name: /Submit/ })) };
}

describe("the controls", () => {
  it("send what the server keeps", async () => {
    const f = renderForm([
      F("vip", "toggle", "VIP"),
      F("at", "time", "Time"),
      F("away", "date_range", "Away"),
      F("share", "percentage", "Share"),
      F("site", "url", "Site"),
      F("labels", "tags", "Labels"),
      F("land", "country", "Country"),
      F("cur", "currency_code", "Currency"),
      F("acct", "iban", "IBAN"),
    ]);
    fireEvent.click(screen.getByRole("switch", { name: "VIP" }));
    fireEvent.change(document.querySelector('input[type="time"]')!, { target: { value: "09:30" } });
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-11-02" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-11-06" } });
    fireEvent.change(document.querySelector('input[type="number"]')!, { target: { value: "12.5" } });
    fireEvent.change(document.querySelector('input[type="url"]')!, { target: { value: "https://example.com" } });
    const tag = screen.getByLabelText("Add a tag");
    fireEvent.change(tag, { target: { value: "urgent" } });
    fireEvent.keyDown(tag, { key: "Enter" });
    fireEvent.change(tag, { target: { value: "Urgent" } });      // the same word twice is one tag
    fireEvent.keyDown(tag, { key: "Enter" });
    fireEvent.change(tag, { target: { value: "vip" } });
    fireEvent.keyDown(tag, { key: "Enter" });
    const [country, currency] = screen.getAllByRole("combobox");
    fireEvent.focus(country);
    fireEvent.change(country, { target: { value: "Germany" } });
    fireEvent.mouseDown(screen.getByRole("option", { name: "Germany" }));
    fireEvent.focus(currency);
    fireEvent.change(currency, { target: { value: "EUR" } });
    fireEvent.mouseDown(screen.getByRole("option", { name: /^EUR/ }));
    fireEvent.change(screen.getByPlaceholderText(/GB82/), { target: { value: "gb82 west 1234 5698 7654 32" } });
    expect((screen.getByPlaceholderText(/GB82/) as HTMLInputElement).value).toBe("GB82 WEST 1234 5698 7654 32");
    f.submit();
    await waitFor(() => expect(f.sent()).toEqual({
      vip: true, at: "09:30", away: { start: "2026-11-02", end: "2026-11-06" }, share: 12.5,
      site: "https://example.com", labels: ["urgent", "vip"], land: "DE", cur: "EUR", acct: "GB82WEST12345698765432",
    }));
  });

  it("refuses a bad IBAN and a percentage over 100 before sending", async () => {
    const f = renderForm([F("acct", "iban", "IBAN"), F("share", "percentage", "Share")]);
    fireEvent.change(screen.getByPlaceholderText(/GB82/), { target: { value: "DE89370400440532013001" } });
    fireEvent.change(document.querySelector('input[type="number"]')!, { target: { value: "101" } });
    f.submit();
    expect(await screen.findByText("Not a valid IBAN (check digits)")).toBeTruthy();
    expect(screen.getByText("Must be <= 100")).toBeTruthy();
    expect(f.sent()).toBeNull();
  });

  it("asks for consent, shows the terms, and sends it once given", async () => {
    const f = renderForm([F("ok", "consent", "Terms", { required: true, terms: "You agree to everything.", placeholder: "I agree" })]);
    expect(screen.getByText("You agree to everything.")).toBeTruthy();
    f.submit();
    expect(await screen.findByText("Please agree to continue")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "I agree" }));
    f.submit();
    await waitFor(() => expect(f.sent()).toEqual({ ok: true }));
  });

  it("draws the static blocks and sends nothing for them", async () => {
    const f = renderForm([
      F("h", "heading", "About you"),
      F("p", "paragraph", "P", { text: "Tell us a little." }),
      F("d", "divider", "D"),
      F("c", "callout", "Careful", { text: "Check twice.", tone: "warning" }),
      F("name", "text", "Name"),
    ]);
    expect(screen.getByRole("heading", { name: "About you" })).toBeTruthy();
    expect(screen.getByText("Tell us a little.")).toBeTruthy();
    expect(within(screen.getByRole("note")).getByText("Check twice.")).toBeTruthy();
    expect(document.querySelector("hr")).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Ann" } });
    f.submit();
    await waitFor(() => expect(f.sent()).toEqual({ name: "Ann" }));
  });
});

describe("in the builder", () => {
  it("starts every palette group closed, and opens one on a click", () => {
    localStorage.removeItem("velaris.formbuilder.palette.open");
    render(<FormBuilder definition={def([])} onChange={() => {}} />);
    const layout = within(screen.getByRole("group", { name: "Layout" }));
    const toggle = layout.getByRole("button", { name: /Layout/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(layout.queryByText("Heading")).toBeNull();
    fireEvent.click(toggle);
    expect(layout.getByText("Heading")).toBeTruthy();
    expect(JSON.parse(localStorage.getItem("velaris.formbuilder.palette.open")!)).toEqual(["Layout"]);
  });

  it("shows the palette in groups", () => {
    render(<FormBuilder definition={def([])} onChange={() => {}} />);
    expect(screen.getAllByRole("group").slice(0, 2).map(g => g.getAttribute("aria-label"))).toEqual(["Layout", "Data"]);
    for (const g of ["Text", "Numbers", "Date & time", "Choices", "Special", "Data", "Layout"]) {
      expect(screen.getByRole("group", { name: g })).toBeTruthy();
    }
    expect(within(screen.getByRole("group", { name: "Layout" })).getByText("Heading")).toBeTruthy();
    expect(within(screen.getByRole("group", { name: "Special" })).getByText("IBAN")).toBeTruthy();
  });

  it("offers every new control", () => {
    render(<FormBuilder definition={def([])} onChange={() => {}} />);
    for (const label of ["Toggle", "Time", "Date Range", "Percentage", "Web Address", "Tags", "Country",
      "Currency Code", "IBAN", "Consent", "Heading", "Paragraph", "Divider", "Callout"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
  });
});
