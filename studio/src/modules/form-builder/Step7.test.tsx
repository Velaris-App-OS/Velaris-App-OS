/**
 * Form Builder v2 §5 step 7 (2026-10-29), Studio side: translations (the
 * server's answer — both run translation_cases.json), field-level security
 * picker, test scenarios, the renderer's language switch and second action.
 */
import React, { useState } from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getFormAccessRoles: vi.fn(),
  runFormTests: vi.fn(),
}));
vi.mock("@shared/api/client", async orig => ({ ...(await orig<any>()), ...api }));

import { languages, pickLanguage, translate } from "./translate";
import { SecurityEditor, TestsPanel, TranslationsPanel, scenarioFrom } from "./Step7Editors";
import FormRenderer from "./FormRenderer";
import type { FormDefinition, FormFieldDef } from "./FormBuilder";

// vitest runs from studio/.
const cases = JSON.parse(readFileSync(resolve(process.cwd(),
  "../services/_shared/velaris_shared/forms/translation_cases.json"), "utf8"));

afterEach(() => vi.clearAllMocks());

describe("translate — the server's twin", () => {
  it("picks the language the server picks", () => {
    expect(languages(cases.definition)).toEqual(["de", "pt-BR"]);
    for (const c of cases.pick) expect(pickLanguage(cases.definition, c.accept)).toBe(c.lang);
  });
  it("translates exactly as the server does", () => {
    for (const c of cases.translate) expect(translate(cases.definition, c.lang)).toEqual(c.expect);
  });
  it("leaves a form without the language untouched", () => {
    expect(translate(cases.definition, null)).toBe(cases.definition);
    expect(translate(cases.definition, "xx")).toBe(cases.definition);
  });
});

const FORM: FormDefinition = {
  sections: [{ id: "s", title: "Claim", order: 0, fields: [
    { id: "a", type: "number", label: "Amount", field_key: "amount", required: true },
    { id: "b", type: "number", label: "Tax", field_key: "tax", required: false, calculation: "amount * 0.2" } as FormFieldDef,
  ] }],
};

function Harness({ initial, children }: { initial: FormDefinition; children: (d: FormDefinition, set: (d: FormDefinition) => void) => React.ReactNode }) {
  const [def, setDef] = useState(initial);
  return <>{children(def, setDef)}<pre data-testid="def">{JSON.stringify(def)}</pre></>;
}
const current = (): any => JSON.parse(screen.getByTestId("def").textContent!);

describe("field-level security", () => {
  it("limits who sees and who changes a field, by role name", async () => {
    api.getFormAccessRoles.mockResolvedValue([{ id: "1", name: "underwriter" }, { id: "2", name: "investigator" }]);
    function F() {
      const [f, setF] = useState<FormFieldDef>(FORM.sections[0].fields[0]);
      return <><SecurityEditor field={f} onUpdate={p => setF({ ...f, ...p })} /><pre data-testid="def">{JSON.stringify(f)}</pre></>;
    }
    render(<F />);
    const [, changeUw] = await screen.findAllByRole("button", { name: "underwriter" });
    fireEvent.click(changeUw);
    expect(current().security).toEqual({ edit: ["underwriter"] });
    expect(screen.getByText(/Only underwriter change it/)).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "investigator" })[0]);
    expect(current().security).toEqual({ view: ["investigator"], edit: ["underwriter"] });
    fireEvent.click(screen.getAllByRole("button", { name: "investigator" })[0]);
    fireEvent.click(screen.getAllByRole("button", { name: "underwriter" })[1]);
    expect(current().security).toBeUndefined();                 // back to everyone
  });
});

describe("translations panel", () => {
  it("adds a language and keeps only what was typed", () => {
    render(<Harness initial={FORM}>{(d, set) => <TranslationsPanel definition={d} onChange={set} />}</Harness>);
    fireEvent.click(screen.getByRole("button", { name: /Translations/ }));
    fireEvent.change(screen.getByLabelText("New language code"), { target: { value: "de" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(current().translations).toEqual({ de: {} });
    fireEvent.change(screen.getByLabelText("amount label"), { target: { value: "Betrag" } });
    expect(current().translations.de.fields).toEqual({ amount: { label: "Betrag" } });
    fireEvent.change(screen.getByLabelText("amount label"), { target: { value: "" } });
    expect(current().translations.de.fields).toEqual({});
    fireEvent.click(screen.getByRole("button", { name: /Remove/ }));
    expect(current().translations).toBeUndefined();
  });
});

describe("test scenarios", () => {
  it("records what the form does now, and runs on the server", async () => {
    const ok = scenarioFrom(FORM, { amount: 10, tax: 999 }, 1);
    expect(ok.expect).toEqual({ valid: true, values: { tax: 2 } });
    expect(ok.values).toEqual({ amount: 10 });                 // a calculated value is the server's
    expect(scenarioFrom(FORM, {}, 2).expect).toEqual({ valid: false, errors: ["amount"] });

    api.runFormTests.mockResolvedValue({ passed: 1, total: 2, results: [
      { name: "Scenario 1", passed: true, problems: [], errors: [], values: {} },
      { name: "Scenario 2", passed: false, problems: ["Expected it to be accepted"], errors: [{ field_key: "amount", message: "Amount is required" }], values: {} },
    ] });
    render(<Harness initial={{ ...FORM, tests: [ok, { ...scenarioFrom(FORM, {}, 2), expect: { valid: true } }] }}>
      {(d, set) => <TestsPanel definition={d} onChange={set} />}</Harness>);
    fireEvent.click(screen.getByRole("button", { name: /Test scenarios/ }));
    fireEvent.click(screen.getByRole("button", { name: /Run all/ }));
    await screen.findByText("Expected it to be accepted");
    const [def, tests] = api.runFormTests.mock.calls[0];
    expect(def.tests).toBeUndefined();
    expect(tests).toHaveLength(2);
    expect(screen.getByLabelText("passes")).toBeTruthy();
    expect(screen.getByLabelText("fails")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "refused" })[1]);
    fireEvent.click(screen.getByRole("button", { name: "Amount" }));
    expect(current().tests[1].expect).toEqual({ valid: false, errors: ["amount"] });
  });
});

describe("renderer", () => {
  it("draws the person's language and switches back", () => {
    const def = { ...FORM, translations: { de: { fields: { amount: { label: "Betrag" } }, sections: { s: { title: "Schaden" } } } } };
    vi.spyOn(navigator, "languages", "get").mockReturnValue(["de-DE", "en"]);
    render(<FormRenderer formName="F" definition={def as any} onSubmit={async () => {}} onCancel={() => {}} />);
    expect(screen.getByText("Betrag")).toBeTruthy();
    expect(screen.getByText("Schaden")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Language"), { target: { value: "" } });
    expect(screen.getByText("Amount")).toBeTruthy();
  });
  it("the second action takes the answers unchecked", async () => {
    const run = vi.fn().mockResolvedValue("Draft saved");
    render(<FormRenderer formName="F" definition={FORM as any} onSubmit={async () => {}} onCancel={() => {}}
      secondaryAction={{ label: "Save draft", run }} notice="Your draft is filled in." />);
    expect(screen.getByText("Your draft is filled in.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(run).toHaveBeenCalledWith({}));  // empty and required: still saved
    expect(await screen.findByText("Draft saved")).toBeTruthy();
    expect(screen.queryByLabelText("Language")).toBeNull();   // no translations, no switch
  });
});
