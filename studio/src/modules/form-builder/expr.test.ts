/**
 * The form expression language gives the server's answer — both sides run
 * services/_shared/velaris_shared/forms/expression_cases.json.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { checkExpr, evaluate, references } from "./expr";

// vitest runs from studio/.
const table = JSON.parse(readFileSync(resolve(process.cwd(),
  "../services/_shared/velaris_shared/forms/expression_cases.json"), "utf8"));

describe("evaluate — shared case table", () => {
  it("has cases", () => expect(table.cases.length).toBeGreaterThan(100));
  for (const c of table.cases) {
    it(c.name, () => {
      const values = { ...table.values, ...(c.values ?? {}) };
      const lookup = (k: string) => values[k];
      if (c.error) {
        expect(checkExpr(c.expr)).not.toBeNull();
        expect(evaluate(c.expr, lookup, table.today)).toBeNull();
        return;
      }
      expect(checkExpr(c.expr)).toBeNull();
      expect(evaluate(c.expr, lookup, table.today)).toEqual(c.result);
    });
  }
});

describe("references", () => {
  it("lists the keys a formula reads", () => {
    expect([...references("qty * rec.amount + {not} + sum(a, b)")].sort()).toEqual(["a", "b", "not", "qty", "rec"]);
  });
});
