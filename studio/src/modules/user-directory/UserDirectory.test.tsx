/**
 * User directory — the account delete and its undo (Nov_TODO "User deletion
 * v2"). A deleted account must read as deleted (with the date it goes for
 * good) and offer only the undo or a permanent delete; an admin who cannot
 * see sign-in accounts gets no delete action at all.
 */
import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import UserDirectory from "./UserDirectory";

const entry = (user_id: string, is_active = true) => ({
  user_id, email: `${user_id}@x.test`, display_name: user_id, manager_user_id: null,
  roles: [], access_group_ids: [], skills: [], timezone: "UTC", is_active,
});

function stub(accounts: unknown | null, calls: string[] = []) {
  vi.stubGlobal("fetch", vi.fn((url: string, opts: RequestInit = {}) => {
    calls.push(`${opts.method ?? "GET"} ${url}`);
    if (url.startsWith("/api/v1/user-directory")) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([entry("bob", false), entry("ann")]) } as Response);
    }
    if (url === "/api/v1/auth/real/users") {
      return accounts === null
        ? Promise.resolve({ ok: false, status: 403, json: () => Promise.resolve({ detail: "no" }) } as Response)
        : Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ users: accounts }) } as Response);
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) } as Response);
  }));
}

const ACCOUNTS = [
  { user_id: "11111111-1111-1111-1111-111111111111", username: "bob", deleted_at: "2026-10-23T10:00:00Z", purge_after: "2026-11-22T10:00:00Z" },
  { user_id: "22222222-2222-2222-2222-222222222222", username: "ann", deleted_at: null, purge_after: null },
];

afterEach(() => vi.unstubAllGlobals());

describe("UserDirectory — account delete", () => {
  it("shows a deleted account as deleted, with undo and delete-now only", async () => {
    stub(ACCOUNTS);
    render(<UserDirectory />);
    fireEvent.click(await screen.findByText(/^All/));
    const bob = (await screen.findByText(/Deleted — removed/)).parentElement as HTMLElement;
    expect(within(bob).getByText("Undo delete")).toBeTruthy();
    expect(within(bob).getByText("Delete now")).toBeTruthy();
    expect(within(bob).queryByText("Edit")).toBeNull();
  });

  it("offers Delete… on a live account and calls the account API on confirm", async () => {
    const calls: string[] = [];
    stub(ACCOUNTS, calls);
    vi.stubGlobal("confirm", () => true);
    render(<UserDirectory />);
    fireEvent.click(await screen.findByText("Delete…"));
    await waitFor(() => expect(calls).toContain("DELETE /api/v1/auth/real/users/22222222-2222-2222-2222-222222222222"));
  });

  it("shows no delete action to an admin who cannot see sign-in accounts", async () => {
    stub(null);
    render(<UserDirectory />);
    await screen.findAllByText("ann");
    expect(screen.queryByText("Delete…")).toBeNull();
  });
});
