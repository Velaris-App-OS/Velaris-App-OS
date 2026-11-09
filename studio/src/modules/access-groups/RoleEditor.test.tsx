/**
 * Access Roles — the cascading privilege editor (2026-10-14).
 *
 * Drives the real screen: Access Roles tab > Edit > side panel > pick a
 * resource > tick its actions > Save, and pins what reaches the server.
 *
 * Pinned: a row offers only its own resource's actions; the picker groups
 * case-level and platform resources and searches; new rows save as "*"; a row
 * saved with a narrower case type, or naming an action the catalog no longer
 * lists, goes back to the server exactly as it came.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AccessGroupAdmin from "./AccessGroupAdmin";

const CATALOG = {
  resources: [
    { id: "case", label: "Cases", actions: [
      { id: "create", label: "Create" }, { id: "read", label: "Read" },
      { id: "update", label: "Update" }, { id: "delete", label: "Delete" },
      { id: "assign", label: "Assign" },
    ] },
    { id: "document", label: "Documents", actions: [
      { id: "read", label: "View" }, { id: "export", label: "Export" },
    ] },
    { id: "legal.holds", label: "legal › holds", platform: true, actions: [
      { id: "place", label: "place", description: "Place a legal hold" },
      { id: "read", label: "read", description: "Read a legal hold" },
      { id: "release", label: "release", description: "Release a legal hold" },
    ] },
    { id: "identity.users", label: "identity › users", platform: true, actions: [
      { id: "read", label: "read" }, { id: "write", label: "write" },
    ] },
  ],
};

const ROLE = {
  id: "r1", name: "Compliance", description: "", tenant_id: "t1",
  privileges: [
    // saved with a narrower case type — must survive untouched
    { resource: "case", case_type_id: "ct-42", actions: ["read"] },
    // an action this server's catalog no longer lists — must not be dropped
    { resource: "document", case_type_id: "*", actions: ["read", "legacy_verb"] },
  ],
};

let calls: { url: string; method: string; body?: any }[] = [];

beforeEach(() => {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, opts: RequestInit = {}) => {
    const method = (opts.method || "GET").toUpperCase();
    calls.push({ url, method, body: opts.body ? JSON.parse(String(opts.body)) : undefined });
    const json = (d: unknown) => new Response(JSON.stringify(d), { status: 200 });
    if (url.endsWith("/access-roles/catalog")) return json(CATALOG);
    if (url.endsWith("/access-roles") && method === "GET") return json([ROLE]);
    if (url.endsWith("/access-groups")) return json([]);
    if (url.endsWith("/tenants")) return json([]);
    if (url.includes("/access-roles/r1") && method === "PATCH") return json(ROLE);
    return json([]);
  }));
});

afterEach(() => { vi.unstubAllGlobals(); });

async function openEditor() {
  render(<AccessGroupAdmin />);
  fireEvent.click(screen.getByRole("tab", { name: /Access Roles/ }));
  fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
  const panel = await screen.findByRole("dialog", { name: "Edit role" });
  await within(panel).findByText("Cases");          // catalog loaded
  return panel;
}

describe("Access role editor", () => {
  it("shows each row with only its own resource's actions", async () => {
    const panel = await openEditor();
    expect(within(panel).getByText("limited to 1 case type")).toBeTruthy();
    // Cases row: its five actions, not Documents' Export
    const casesRow = within(panel).getByText("Cases").closest("div")!.parentElement!;
    expect(within(casesRow).getByLabelText("Assign")).toBeTruthy();
    expect(within(casesRow).queryByLabelText("Export")).toBeNull();
    // the saved-but-uncatalogued action is still offered, and still ticked
    const legacy = within(panel).getByLabelText("Legacy verb") as HTMLInputElement;
    expect(legacy.checked).toBe(true);
  });

  it("groups and searches the resource picker", async () => {
    const panel = await openEditor();
    fireEvent.click(within(panel).getByRole("button", { name: "+ Add privilege" }));
    expect(within(panel).getByText("Platform › Legal")).toBeTruthy();
    expect(within(panel).getByText("Platform › Identity")).toBeTruthy();
    // Documents is already granted for all case types — not offered again
    expect(within(panel).queryByRole("button", { name: /^Documents/ })).toBeNull();
    fireEvent.change(within(panel).getByPlaceholderText("Search resources…"), { target: { value: "hold" } });
    expect(within(panel).queryByText("Platform › Identity")).toBeNull();
    expect(within(panel).getByRole("button", { name: /legal › holds/ })).toBeTruthy();
  });

  it("saves a new platform row as '*' and leaves saved rows exactly as they were", async () => {
    const panel = await openEditor();
    fireEvent.click(within(panel).getByRole("button", { name: "+ Add privilege" }));
    fireEvent.click(within(panel).getByRole("button", { name: /legal › holds/ }));
    fireEvent.click(within(panel).getByLabelText("Place"));
    fireEvent.click(within(panel).getByLabelText("Release"));
    fireEvent.click(within(panel).getByRole("button", { name: "Save Role" }));

    await waitFor(() => expect(calls.some(c => c.method === "PATCH")).toBe(true));
    const body = calls.find(c => c.method === "PATCH")!.body;
    expect(calls.find(c => c.method === "PATCH")!.url).toBe("/api/v1/access-roles/r1");
    expect(body.privileges).toEqual([
      { resource: "case", case_type_id: "ct-42", actions: ["read"] },
      { resource: "document", case_type_id: "*", actions: ["read", "legacy_verb"] },
      { resource: "legal.holds", case_type_id: "*", actions: ["place", "release"] },
    ]);
  });

  it("does not save a row with nothing ticked, and closes on Escape", async () => {
    const panel = await openEditor();
    fireEvent.click(within(panel).getByRole("button", { name: "+ Add privilege" }));
    fireEvent.click(within(panel).getByRole("button", { name: /identity › users/ }));
    expect(within(panel).getByText(/Nothing ticked/)).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "Save Role" }));
    await waitFor(() => expect(calls.some(c => c.method === "PATCH")).toBe(true));
    const saved = calls.find(c => c.method === "PATCH")!.body.privileges;
    expect(saved.some((p: any) => p.resource === "identity.users")).toBe(false);

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await screen.findByRole("dialog", { name: "Edit role" });
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit role" })).toBeNull());
  });

  it("shows the catalog error and keeps Raw JSON when the catalog fails", async () => {
    const real = globalThis.fetch as any;
    vi.stubGlobal("fetch", vi.fn(async (url: string, opts?: RequestInit) =>
      url.endsWith("/access-roles/catalog")
        ? new Response("boom", { status: 500 })
        : real(url, opts)));
    render(<AccessGroupAdmin />);
    fireEvent.click(screen.getByRole("tab", { name: /Access Roles/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const panel = await screen.findByRole("dialog", { name: "Edit role" });
    expect(await within(panel).findByText(/Raw JSON still works/)).toBeTruthy();
  });
});

describe("Add privilege picker", () => {
  it("opens inline and the button closes it again", async () => {
    const panel = await openEditor();
    const btn = within(panel).getByRole("button", { name: "+ Add privilege" });
    fireEvent.mouseDown(btn); fireEvent.click(btn);
    expect(within(panel).getByPlaceholderText("Search resources…")).toBeTruthy();
    fireEvent.mouseDown(btn); fireEvent.click(btn);
    expect(within(panel).queryByPlaceholderText("Search resources…")).toBeNull();
  });
});
