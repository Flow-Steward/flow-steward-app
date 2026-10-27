import { expect, it } from "vitest";

import type { WorkflowCanvasIssue } from "@/pages/workflows/shared/api";
import { liveIssueResolved, liveValidationIssues, type LiveStepDraft } from "./detail-live-validation";

const serverIssue = (code: string, stepId = "fetch"): WorkflowCanvasIssue => ({
  scope: "step", scope_id: stepId, step_id: stepId, severity: "error", code, title: code,
});

const draft = (patch: Partial<LiveStepDraft>): LiveStepDraft => ({
  stepId: "fetch", savedKind: "http_request", kind: "http_request",
  overrideDraft: JSON.stringify({ http_request: { url: "", response_target: "state.response" } }),
  agentId: "", agentFromParticipant: false, connectionMissing: null, ...patch,
});

it("adds the issue a draft introduces and drops the one it fixes", () => {
  const saved = [serverIssue("workflow.step_http_request_url_missing"), serverIssue("workflow.other", "other")];
  expect(liveValidationIssues(saved, draft({})).map((issue) => issue.code)).toEqual([
    "workflow.step_http_request_url_missing", "workflow.other",
  ]);
  const fixed = draft({ overrideDraft: JSON.stringify({ http_request: { url: "https://x.test", response_target: "state.r" } }) });
  expect(liveValidationIssues(saved, fixed).map((issue) => issue.code)).toEqual(["workflow.other"]);
  const broken = draft({ overrideDraft: JSON.stringify({ http_request: { url: "https://x.test", response_target: "" } }) });
  expect(liveValidationIssues([], broken)).toEqual([
    expect.objectContaining({ code: "workflow.step_http_request_response_target_missing", scope: "step", scope_id: "fetch", severity: "error" }),
  ]);
});

it("keeps the server's word where the browser cannot decide", () => {
  const saved = [serverIssue("workflow.step_agent_missing", "ask")];
  const participant = draft({ stepId: "ask", savedKind: "agent", kind: "agent", overrideDraft: "{}", agentFromParticipant: true });
  expect(liveValidationIssues(saved, participant)).toBe(saved);
  const connection = [serverIssue("workflow.connection_required", "ship")];
  const loading = draft({ stepId: "ship", savedKind: "connector", kind: "connector", overrideDraft: JSON.stringify({ connector: { connector_id: "extension.dhl" } }) });
  expect(liveValidationIssues(connection, loading).map((issue) => issue.code)).toEqual(["workflow.connection_required"]);
  const bound = draft({ stepId: "ship", savedKind: "connector", kind: "connector", overrideDraft: JSON.stringify({ connector: { connector_id: "extension.dhl", connection_binding: { connection_id: "c1" } } }) });
  expect(liveValidationIssues(connection, bound)).toEqual([]);
});

it("does not judge malformed JSON or a pending kind change", () => {
  const saved = [serverIssue("workflow.step_http_request_url_missing")];
  expect(liveValidationIssues(saved, draft({ overrideDraft: "{" }))).toBe(saved);
  expect(liveValidationIssues(saved, draft({ kind: "wait" }))).toBe(saved);
});

it("retires a clicked issue the draft has fixed", () => {
  const issue = serverIssue("workflow.step_switch_selector_missing", "route");
  const fixed = draft({ stepId: "route", savedKind: "switch", kind: "switch", overrideDraft: JSON.stringify({ switch: { selector: "state.kind" } }) });
  expect(liveIssueResolved(issue, fixed)).toBe(true);
  expect(liveIssueResolved(issue, { ...fixed, overrideDraft: JSON.stringify({ switch: { selector: "" } }) })).toBe(false);
});

it.each([
  ["if", { if: { condition: { selector: "", operator: "equals", value: "" } } }, "workflow.step_if_condition_selector_missing"],
  ["switch", { switch: { selector: "", cases: [{ case_id: "case_1", when: "" }] } }, "workflow.step_switch_selector_missing"],
] as const)("keeps a blank %s selector incomplete until an authored value is supplied", (kind, override, code) => {
  const incomplete = draft({ stepId: "route", savedKind: kind, kind, overrideDraft: JSON.stringify(override) });
  expect(liveValidationIssues([], incomplete)).toEqual([
    expect.objectContaining({ code, scope: "step", scope_id: "route", severity: "error" }),
  ]);
  const filled = kind === "if"
    ? { if: { condition: { selector: "state.plan", operator: "equals", value: "ready" } } }
    : { switch: { selector: "state.plan", cases: [{ case_id: "case_1", when: "ready" }] } };
  expect(liveValidationIssues([], { ...incomplete, overrideDraft: JSON.stringify(filled) })).toEqual([]);
});
