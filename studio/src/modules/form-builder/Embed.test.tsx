/**
 * Embed field (2026-10-29): a page is framed only from an approved https site,
 * never from Studio's own origin, and always locked down.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const settings = vi.hoisted(() => ({ allowed_origins: [] as string[], portal: false, allow_forms: true as boolean }));
vi.mock("@shared/api/client", async (orig) => ({
  ...(await orig<any>()),
  getEmbedSettings: vi.fn(async () => settings),
}));

import FormRenderer from "./FormRenderer";
import PortalFormFields from "./PortalFormFields";
import type { FormDefinition, FormFieldDef } from "./FormBuilder";
import { embedOrigin, embedRefusal, resetEmbedOrigins } from "./Embed";

const VIDEO = "https://www.youtube-nocookie.com/embed/abc";
const EMBED = (url: string, extra: Record<string, any> = {}): FormFieldDef =>
  ({ id: "v", type: "embed", label: "Intro video", field_key: "v", required: false, url, height: 315, ...extra } as any);
const def = (fields: FormFieldDef[]): FormDefinition => ({ sections: [{ id: "s", title: "S", order: 0, fields }] });

beforeEach(() => { resetEmbedOrigins(); settings.allowed_origins = []; settings.allow_forms = true; });
afterEach(() => vi.clearAllMocks());

describe("the origin rule (as the server's)", () => {
  it.each([
    ["https://www.youtube-nocookie.com/embed/x?y=1", "https://www.youtube-nocookie.com"],
    ["https://Example.COM:8443/a", "https://example.com:8443"],
    ["https://example.com:443/", "https://example.com"],
    ["http://example.com/", null],
    ["javascript:alert(1)", null],
    ["https://user:pw@example.com/", null],
    ["https://127.0.0.1/", null],
    ["https://localhost/", null],
    ["data:text/html,<script>", null],
  ])("%s", (url, origin) => expect(embedOrigin(url)).toBe(origin));

  it("never frames this platform's own pages", () => {
    expect(embedRefusal("https://studio.example.com/cases", ["https://studio.example.com"], "https://studio.example.com"))
      .toMatch(/own pages/);
  });
});

describe("in a form", () => {
  it("shows nothing from a site that is not approved", async () => {
    render(<FormRenderer definition={def([EMBED(VIDEO)])} formName="F" onCancel={() => {}} onSubmit={async () => {}} />);
    expect(await screen.findByText(/is not an approved site/)).toBeTruthy();
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("frames an approved site, locked down", async () => {
    settings.allowed_origins = ["https://www.youtube-nocookie.com"];
    render(<FormRenderer definition={def([EMBED(VIDEO, { permissions: ["forms", "top-navigation"] })])}
      formName="F" onCancel={() => {}} onSubmit={async () => {}} />);
    await waitFor(() => expect(document.querySelector("iframe")).toBeTruthy());
    const f = document.querySelector("iframe")!;
    expect(f.getAttribute("src")).toBe(VIDEO);
    expect(f.getAttribute("sandbox")).toBe("allow-scripts allow-same-origin allow-forms");   // an unknown grant is ignored
    expect(f.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(f.getAttribute("allow")).toBe("");
    expect(f.getAttribute("title")).toBe("Intro video");
  });

  it("names the outside site above every frame (Allowed sites, 2026-11-04)", async () => {
    settings.allowed_origins = ["https://www.youtube-nocookie.com"];
    render(<FormRenderer definition={def([EMBED(VIDEO)])} formName="F" onCancel={() => {}} onSubmit={async () => {}} />);
    await waitFor(() => expect(document.querySelector("iframe")).toBeTruthy());
    expect(screen.getByRole("note").textContent).toMatch(/External content from www\.youtube-nocookie\.com/);
    expect(screen.getByRole("note").textContent).toMatch(/Do not enter passwords/);
  });

  it("leaves typing off when the operator switched it off", async () => {
    settings.allowed_origins = ["https://www.youtube-nocookie.com"];
    settings.allow_forms = false;
    render(<FormRenderer definition={def([EMBED(VIDEO, { permissions: ["forms", "popups"] })])}
      formName="F" onCancel={() => {}} onSubmit={async () => {}} />);
    await waitFor(() => expect(document.querySelector("iframe")?.getAttribute("sandbox")).toBe("allow-scripts allow-same-origin allow-popups"));
  });

  it("on the portal uses only the sites that came with the form, never the signed-in list", async () => {
    settings.allowed_origins = ["https://www.youtube-nocookie.com"];
    const d: any = { ...def([EMBED(VIDEO)]), embed_origins: [] };
    render(<PortalFormFields definition={d} values={{}} onChange={() => {}} errors={{}} />);
    expect(await screen.findByText(/is not an approved site/)).toBeTruthy();
    const { getEmbedSettings } = await import("@shared/api/client");
    expect(getEmbedSettings).not.toHaveBeenCalled();
  });
});
