/* Pre-fill a form from its case's variables (Case Variables Phase 3).
   A field's own `variable` binding wins, else a variable named like the
   field key. Reads are redacted server-side — masked values arrive as ***.
   One rule for Work Center and Case Manager (Case Manager had none until
   2026-10-28 — its forms always opened empty). */

import { getCaseVariables } from "@shared/api/client";
import { fieldKey } from "./validate";

export function prefillFrom(definition: any, variables: Record<string, any>): Record<string, any> {
  const out: Record<string, any> = {};
  for (const section of definition?.sections ?? []) {
    for (const field of section.fields ?? []) {
      const key = fieldKey(field);
      if (field.type === "consent") continue;   // agreeing is asked anew each time, never carried over
      const source = field.variable || key;
      if (variables[source] !== undefined && variables[source] !== null) out[key] = variables[source];
    }
  }
  return out;
}

/** The case's pre-fill for this form; {} when variables cannot be read. */
export async function loadPrefill(caseId: string, definition: any): Promise<Record<string, any>> {
  try {
    const { variables } = await getCaseVariables(caseId);
    return prefillFrom(definition, variables);
  } catch {
    return {};   // variables unavailable — open the form unfilled
  }
}
