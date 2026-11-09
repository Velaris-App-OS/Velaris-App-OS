/**
 * Location field (Form Builder controls, 2026-10-29): a pin stored as
 * {lat, lng}. Without this install's own tiles there is no map — the person
 * types coordinates or uses the device's location — and nothing is fetched
 * from anywhere but /api/v1/map.
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import FormRenderer from "./FormRenderer";
import type { FormDefinition, FormFieldDef } from "./FormBuilder";
import { mapAvailability, resetMapAvailability } from "./LocationField";

const WHERE: FormFieldDef = { id: "where", type: "location", label: "Where", field_key: "where", required: true };
const def: FormDefinition = { sections: [{ id: "s1", title: "S", order: 0, fields: [WHERE] }] };

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  resetMapAvailability();
  fetchMock = vi.fn(async () => new Response(null, { status: 404 }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { vi.unstubAllGlobals(); });

function renderForm() {
  let sent: any = null;
  render(<FormRenderer definition={def} formName="F" onCancel={() => {}} onSubmit={async v => { sent = v; }} />);
  return () => sent;
}

describe("without tiles", () => {
  it("asks only this install for tiles, shows no map, and submits typed coordinates", async () => {
    const sent = renderForm();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    for (const [url] of fetchMock.mock.calls) expect(String(url)).toMatch(/^\/api\/v1\/map\//);
    expect(screen.queryByRole("application")).toBeNull();
    fireEvent.change(screen.getByLabelText("Latitude"), { target: { value: "51.5074" } });
    fireEvent.change(screen.getByLabelText("Longitude"), { target: { value: "-0.1278" } });
    fireEvent.click(screen.getByRole("button", { name: /Submit/ }));
    await waitFor(() => expect(sent()).toEqual({ where: { lat: 51.5074, lng: -0.1278 } }));
  });

  it("refuses half a pin and an empty required pin", async () => {
    const sent = renderForm();
    fireEvent.click(screen.getByRole("button", { name: /Submit/ }));
    expect(await screen.findByText("Where is required")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Latitude"), { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: /Submit/ }));
    expect(await screen.findByText("Enter both latitude and longitude")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Longitude"), { target: { value: "200" } });
    fireEvent.click(screen.getByRole("button", { name: /Submit/ }));
    expect(await screen.findByText("Longitude must be between -180 and 180")).toBeTruthy();
    expect(sent()).toBeNull();
  });

  it("fills the pin from the device's location", async () => {
    vi.stubGlobal("navigator", { ...navigator, geolocation: {
      getCurrentPosition: (ok: any) => ok({ coords: { latitude: 48.85837012, longitude: 2.29448123 } }),
    } });
    const sent = renderForm();
    fireEvent.click(screen.getByRole("button", { name: "Use my location" }));
    await waitFor(() => expect((screen.getByLabelText("Latitude") as HTMLInputElement).value).toBe("48.85837"));
    fireEvent.click(screen.getByRole("button", { name: /Submit/ }));
    await waitFor(() => expect(sent()).toEqual({ where: { lat: 48.85837, lng: 2.294481 } }));
  });
});

describe("availability", () => {
  it("offers the map only when the tiles answer, and names only with fonts too", async () => {
    fetchMock.mockImplementation(async (url: string) => new Response(null, { status: url.endsWith(".pmtiles") ? 200 : 404 }));
    expect(await mapAvailability()).toEqual({ tiles: true, fonts: false });
    resetMapAvailability();
    fetchMock.mockImplementation(async () => new Response(null, { status: 200 }));
    expect(await mapAvailability()).toEqual({ tiles: true, fonts: true });
    resetMapAvailability();
    fetchMock.mockImplementation(async () => { throw new TypeError("offline"); });
    expect(await mapAvailability()).toEqual({ tiles: false, fonts: false });
  });
});
