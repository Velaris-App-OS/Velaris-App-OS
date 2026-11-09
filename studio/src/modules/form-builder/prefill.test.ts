/** One pre-fill rule for Work Center and Case Manager. */
import { describe, expect, it } from "vitest";
import { prefillFrom } from "./prefill";

describe("prefillFrom", () => {
  const def = { sections: [{ fields: [
    { id: "a", field_key: "status", variable: "crm.account_status" },
    { id: "b", field_key: "subject" },
    { id: "c", key: "employee-name" },
    { id: "d", field_key: "missing" },
  ] }] };
  it("the field's own binding wins, else a variable named like the key", () => {
    expect(prefillFrom(def, { "crm.account_status": "gold", status: "ignored", subject: "Car", "employee-name": "Ann", missing: null }))
      .toEqual({ status: "gold", subject: "Car", "employee-name": "Ann" });
  });
});
