/**
 * Collapsible sidebar sections (2026-10-29): every section starts closed; a
 * closed one keeps the page you are on; what is opened is remembered in this
 * browser, and unreadable storage just means everything starts closed.
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/auth", () => ({ useAuth: () => ({ user: { roles: ["case_worker"], is_case_worker: true, username: "james" } }) }));
vi.mock("@/auth/PermissionsContext", () => ({ usePermissions: () => ({ isRouteAllowed: () => true }) }));
vi.mock("@/app/FeatureFlagsContext", () => ({ useFeatureFlags: () => ({ isEnabled: () => true, loading: false }) }));

import { Sidebar } from "./AppLayout";

const KEY = "velaris.nav.open";
beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
});
afterEach(() => vi.unstubAllGlobals());

const show = (at = "/") => render(
  <MemoryRouter initialEntries={[at]}><Sidebar onOpenSearch={() => {}} onOpenProfile={() => {}} /></MemoryRouter>);
const section = (name: string) => screen.getByRole("button", { name: new RegExp(`^${name}`, "i") });

describe("sidebar sections", () => {
  it("start closed, open and close again, and are remembered", () => {
    show();
    const ws = section("Workspace");
    expect(ws.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("link", { name: /^Cases$/ })).toBeNull();
    fireEvent.click(ws);
    expect(ws.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("link", { name: /Cases/ })).toBeTruthy();
    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual(["Workspace"]);
    fireEvent.click(ws);
    expect(screen.queryByRole("link", { name: /^Cases$/ })).toBeNull();
  });

  it("keeps the current page visible in a closed section", () => {
    show("/cases");
    expect(section("Workspace").getAttribute("aria-expanded")).toBe("false");
    expect(screen.getByRole("link", { name: /Cases/ })).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Dashboard/ })).toBeNull();
  });

  it("reopens what was opened before", () => {
    localStorage.setItem(KEY, JSON.stringify(["Workspace"]));
    show();
    expect(section("Workspace").getAttribute("aria-expanded")).toBe("true");
  });

  it("starts closed when the stored value is unreadable", () => {
    localStorage.setItem(KEY, "{not json");
    show();
    expect(section("Workspace").getAttribute("aria-expanded")).toBe("false");
  });
});
