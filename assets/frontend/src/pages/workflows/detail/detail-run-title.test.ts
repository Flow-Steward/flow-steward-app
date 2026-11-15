import { RUN_TITLE_TEMPLATE_MAX, runTitleInputNames, runTitleIssues } from "./detail-run-title";
import type { WorkflowInterfaceFieldDraft } from "./detail-interface";

const rows = [
  { name: "site_url", type: "string" },
  { name: "run_name", type: "string" },
  { name: "login", type: "secret" },
  { name: "api_token", type: "string" },
] as unknown as WorkflowInterfaceFieldDraft[];

it("offers only declared inputs that are not secret", () => {
  expect(runTitleInputNames(rows)).toEqual(["site_url", "run_name"]);
});

it("accepts a template over declared inputs and plain text", () => {
  expect(runTitleIssues("{{ inputs.run_name }} · {{inputs.site_url}}", rows)).toEqual([]);
  expect(runTitleIssues("Nightly import", rows)).toEqual([]);
  expect(runTitleIssues("   ", rows)).toEqual([]);
});

it("names each placeholder the server would refuse", () => {
  expect(runTitleIssues("{{ inputs.login }} {{ inputs.api_token }} {{ inputs.region }} {{ steps.a.output }}", rows)).toEqual([
    { kind: "secret", name: "login" },
    { kind: "secret", name: "api_token" },
    { kind: "unknown", name: "region" },
    { kind: "placeholder", expression: "steps.a.output" },
  ]);
});

it("does not guess undeclared names outside fields mode, but still refuses secrets", () => {
  expect(runTitleIssues("{{ inputs.anything }} {{ inputs.password }}", [], { inputMode: "passthrough" })).toEqual([
    { kind: "secret", name: "password" },
  ]);
});

it("refuses a template longer than the server stores", () => {
  expect(runTitleIssues("x".repeat(RUN_TITLE_TEMPLATE_MAX + 1), rows)).toEqual([{ kind: "too_long" }]);
});
