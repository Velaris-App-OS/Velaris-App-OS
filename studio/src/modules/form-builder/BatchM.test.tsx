/**
 * Controls batch M (2026-10-29): address, signature, pickers, rich text,
 * button and the tabs layout — each used the way a person would, and the
 * injection paths (a link, a script, a data URL) refused.
 */
import React from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import FormRenderer from "./FormRenderer";
import type { FormDefinition, FormFieldDef } from "./FormBuilder";
import { RichText, SignaturePad } from "./BatchM";

const F = (id: string, type: FormFieldDef["type"], label: string, extra: Record<string, any> = {}): FormFieldDef =>
  ({ id, type, label, field_key: id, required: false, ...extra });
const def = (fields: FormFieldDef[], settings?: any): FormDefinition =>
  ({ sections: [{ id: "s1", title: "S", order: 0, fields }], ...(settings ? { settings } : {}) } as any);

function renderForm(d: FormDefinition, props: Record<string, any> = {}) {
  let sent: any = null;
  render(<FormRenderer definition={d} formName="F" onCancel={() => {}} onSubmit={async v => { sent = v; }} {...props} />);
  return { sent: () => sent, submit: () => fireEvent.click(screen.getByRole("button", { name: /Submit/ })) };
}

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("address", () => {
  it("sends the parts, trimmed, and checks the postcode for the country", async () => {
    const f = renderForm(def([F("home", "address", "Home")]));
    fireEvent.change(screen.getByLabelText("Street and number"), { target: { value: " 10 Downing St " } });
    fireEvent.change(screen.getByLabelText("Town or city"), { target: { value: "London" } });
    fireEvent.change(screen.getByLabelText("Postcode"), { target: { value: "12345" } });
    fireEvent.change(screen.getByLabelText("Country"), { target: { value: "GB" } });
    f.submit();
    expect(await screen.findByText("Not a postcode used in GB")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Postcode"), { target: { value: "SW1A 2AA" } });
    f.submit();
    await waitFor(() => expect(f.sent()).toEqual({ home: { line1: "10 Downing St", city: "London", postcode: "SW1A 2AA", country: "GB" } }));
  });
});

describe("signature", () => {
  it("shows only a PNG data URL as an image", () => {
    const { container, rerender } = render(<SignaturePad value="data:image/png;base64,iVBORw0KGgo=" onChange={() => {}} readOnly />);
    expect(container.querySelector("img")?.getAttribute("src")).toMatch(/^data:image\/png;base64,/);
    rerender(<SignaturePad value="javascript:alert(1)" onChange={() => {}} readOnly />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("Not signed")).toBeTruthy();
  });
});

describe("pickers", () => {
  it("search the saved form's picker and send the chosen id", async () => {
    const fetchMock = vi.fn(async (_url: string, init: any) => {
      const body = JSON.parse(init.body);
      expect(body).toMatchObject({ form_id: "form-1", field_key: "approver", q: "an" });
      return new Response(JSON.stringify({ options: [{ value: "ana", label: "Ana Approver" }] }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const f = renderForm(def([F("approver", "user_picker", "Approver")]), { formId: "form-1" });
    const box = screen.getByRole("combobox", { name: "Approver" });
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "an" } });
    fireEvent.mouseDown(await screen.findByRole("option", { name: "Ana Approver" }));
    f.submit();
    await waitFor(() => expect(f.sent()).toEqual({ approver: "ana" }));
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/v1/forms/picker-options");
  });

  it("do nothing on a form that is not saved", () => {
    renderForm(def([F("approver", "user_picker", "Approver")]));
    expect((screen.getByRole("combobox", { name: "Approver" }) as HTMLInputElement).disabled).toBe(true);
  });
});

describe("rich text", () => {
  it("draws formatting as elements and never as HTML", () => {
    const { container } = render(<RichText text={"**Bold** *it*\n\n- one\n- two\n\n[safe](https://example.com) [bad](javascript:alert(1)) <script>x</script>"} />);
    expect(container.querySelector("strong")?.textContent).toBe("Bold");
    expect(container.querySelectorAll("li").length).toBe(2);
    const links = container.querySelectorAll("a");
    expect(links.length).toBe(1);
    expect(links[0].getAttribute("href")).toBe("https://example.com");
    expect(links[0].getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>x</script>");
    expect(container.textContent).toContain("bad (javascript:alert(1))");
  });
});

describe("button", () => {
  it("sets a field, never a consent, and opens only https pages", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const f = renderForm(def([
      F("note", "text", "Note"),
      F("ok", "consent", "Terms", { required: true }),
      F("go", "button", "Fill in", { actions: [
        { action: "set", target: "note", expression: '"filled"' },
        { action: "set", target: "ok", expression: "true" },
        { action: "open_link", url: "javascript:alert(1)" },
        { action: "open_link", url: "http://example.com" },
        { action: "open_link", url: "https://example.com/help" },
      ] }),
    ]));
    fireEvent.click(screen.getByRole("button", { name: "Fill in" }));
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("filled");
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
    expect(open).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith("https://example.com/help", "_blank", "noopener,noreferrer");
    f.submit();
    expect(await screen.findByText("Please agree to continue")).toBeTruthy();
  });
});

describe("tabs", () => {
  it("shows one section at a time and opens the tab with a problem on Submit", async () => {
    const d: any = { settings: { mode: "tabs" }, sections: [
      { id: "a", title: "About", order: 0, fields: [F("name", "text", "Name")] },
      { id: "b", title: "Details", order: 1, fields: [F("ref", "text", "Reference", { required: true })] },
    ] };
    const f = renderForm(d);
    const tabs = screen.getByRole("tablist", { name: "Form sections" });
    expect(within(tabs).getAllByRole("tab").map(t => t.textContent)).toEqual(["About", "Details"]);
    expect(screen.queryByText("Reference")).toBeNull();
    f.submit();
    expect(await screen.findByText("Reference is required")).toBeTruthy();
    expect(within(tabs).getByRole("tab", { name: /Details/ }).getAttribute("aria-selected")).toBe("true");
  });
});
