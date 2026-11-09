// R5d 11b — "data as of": announced from the response header, shown only when old.
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import DataAsOfNotice from "@shared/components/DataAsOfNotice";
import { noteDataAsOf } from "./dataAsOf";

const res = (asOf?: string) => ({
  headers: new Headers(asOf ? { "x-velaris-data-as-of": asOf } : {}), url: "/api/v1/x",
} as unknown as Response);

describe("data as of", () => {
  it("reads the header and says how old the data is", () => {
    const now = Date.parse("2026-10-22T09:44:30Z");
    expect(noteDataAsOf(res("2026-10-22T09:44:00Z"), now)?.ageSeconds).toBe(30);
    expect(noteDataAsOf(res(), now)).toBeNull();
  });

  it("stays silent while figures are live and speaks once they are old", () => {
    render(<MemoryRouter><DataAsOfNotice /></MemoryRouter>);
    act(() => { noteDataAsOf(res(new Date(Date.now() - 2000).toISOString())); });
    expect(screen.queryByRole("status")).toBeNull();
    act(() => { noteDataAsOf(res(new Date(Date.now() - 45000).toISOString())); });
    expect(screen.getByRole("status").textContent).toMatch(/read copy and are 4[45] s old/);
  });
});
