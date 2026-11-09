/**
 * The form values check gives the server's answer — both sides run the one
 * case table in services/_shared/velaris_shared/forms/validation_cases.json.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { safePattern, validateForm } from "./validate";

// vitest runs from studio/.
const table = JSON.parse(readFileSync(resolve(process.cwd(),
  "../services/_shared/velaris_shared/forms/validation_cases.json"), "utf8"));

describe("validateForm — shared case table", () => {
  it("has cases", () => expect(table.cases.length).toBeGreaterThan(20));

  for (const c of table.cases) {
    it(c.name, () => {
      const { clean, errors } = validateForm(
        [{ id: "s", title: "S", order: 0, fields: c.fields }], c.values, c.today);
      expect(errors).toEqual(c.errors);
      if (c.clean) expect(clean).toEqual(c.clean);
    });
  }
});

describe("safePattern — shared table", () => {
  for (const [pattern, expected] of Object.entries(table.safe_patterns as Record<string, boolean>)) {
    it(pattern.slice(0, 40), () => expect(safePattern(pattern)).toBe(expected));
  }
});


describe("built-in lists (country, currency, IBAN)", () => {
  it("are the server's lists", () => {
    const server = readFileSync(resolve(process.cwd(), "../services/_shared/velaris_shared/forms/codes.json"), "utf8");
    const studio = readFileSync(resolve(process.cwd(), "src/modules/form-builder/codes.json"), "utf8");
    expect(studio).toBe(server);
  });
});
