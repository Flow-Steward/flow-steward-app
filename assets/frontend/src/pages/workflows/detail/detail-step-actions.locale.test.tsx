import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HttpError } from "@/lib/http/errors";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { useWorkflowStepActions } from "@/pages/workflows/detail/detail-step-actions";

import { definitionFromWorkflow } from "@/pages/workflows/builder/change-set";
import { useWorkflowChangeSet } from "./detail-change-set";

const applyWorkflowChanges = vi.hoisted(() => vi.fn());

vi.mock("@/pages/workflows/builder/api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "@/pages/workflows/builder/api",
  );
  return { ...actual, applyWorkflowChanges };
});

const source = "state.inputs.not_declared";
const message = `transform input.source '${source}' is not produced by workflow inputs or upstream steps`;
const key = "react.workflows.transform_input_source_unavailable";
const lithuanian = "Transformavimo įvesties šaltinis '{source}' nėra sukurtas ankstesnių darbo eigos įvesčių ar žingsnių.";

function setup(locale: "lt" | "en", existing: boolean) {
  const messages: Record<string, string> = { [key]: locale === "lt" ? lithuanian : message, "react.workflows.transform_input_source_required": locale === "lt" ? "Transformavimui būtinas įvesties šaltinis (input.source)." : "transform requires input.source" };
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <I18nContext.Provider value={{
        ...fallbackContext,
        locale,
        messages,
        t: (messageKey, text = "", params) => interpolate(messages[messageKey] || text, params),
      }}>
        {children}
      </I18nContext.Provider>
    );
  }
  const ctx = {
    refetchWorkflowSurfaces: vi.fn().mockResolvedValue(undefined),
    selectedStep: existing ? {
      raw: { id: "step-db-1", step_id: "owner_transform", override: {} },
      local: false, runtimeId: "owner_transform", stepKind: "transform",
    } : null,
    serverRevision: 7,
    createAsEntry: false,
    stepCreateContext: existing ? null : {},
    setCreateAsEntry: vi.fn(),
    setStepCreateContext: vi.fn(),
    setSaveError: vi.fn(),
    setSaveNotice: vi.fn(),
    setSavingWorkflowSurface: vi.fn(),
    setSelectedStepId: vi.fn(),
    setStepOverrideDraft: vi.fn(),
    stepAgentId: "",
    stepBlockKey: "",
    stepDisplayName: "Owner transform Ąžuolas",
    stepKindDraft: "transform",
    stepOptional: false,
    stepOverrideDraft: JSON.stringify({ transform: { input: { source }, mode: "object", result_target: "state.outputs.owner_result", fields: [] } }),
    workflowId: "wf_demo",
  };
  const definition = definitionFromWorkflow({ workflow_id: "wf_demo", revision: 7, steps: [], groups: [] });
  const view = renderHook(() => {
    const changeSet = useWorkflowChangeSet({
      workflowId: "wf_demo", serverRevision: 7, serverDefinition: definition,
      refetchWorkflowSurfaces: ctx.refetchWorkflowSurfaces,
    });
    return { ...useWorkflowStepActions({ ...ctx, changeSet }), changeSet };
  }, { wrapper: Wrapper });

  async function save(override?: Record<string, unknown>) {
    await act(async () => { await view.result.current.handleSaveStep(override); });
    if (!existing) {
      const operation = view.result.current.changeSet.operations[0];
      expect(operation.op).toBe("create_step");
      if (operation.op !== "create_step") throw new Error("Expected staged creation");
      if (!ctx.selectedStep) {
        expect(applyWorkflowChanges).not.toHaveBeenCalled();
        expect(ctx.setSelectedStepId).toHaveBeenCalledWith(operation.step.step_id);
        ctx.selectedStep = {
          local: true, runtimeId: operation.step.step_id, stepKind: "transform",
          raw: { id: "", step_id: operation.step.step_id, override: {} },
        };
        view.rerender();
        ctx.setSelectedStepId.mockClear();
      }
      await act(async () => { await view.result.current.changeSet.apply(); });
    }
  }
  const visibleError = () => existing
    ? ctx.setSaveError.mock.lastCall?.[0]
    : view.result.current.changeSet.applyError;
  const visibleNotice = () => existing
    ? ctx.setSaveNotice.mock.lastCall?.[0]
    : view.result.current.changeSet.notice;
  return { view, ctx, save, visibleError, visibleNotice };
}

describe("Save Step validation error locale", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    applyWorkflowChanges.mockReset();
    applyWorkflowChanges.mockResolvedValue({ workflow_id: "wf_demo", revision: 8, valid: true, issues: [], workflow: {} });
  });

  afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

  it.each([
    { locale: "lt", existing: false },
    { locale: "lt", existing: true },
    { locale: "en", existing: false },
    { locale: "en", existing: true },
  ] as const)("renders nested HTTP 400 for $locale / existing=$existing and allows the next save", async ({ locale, existing }) => {
    // Rejected canonical change set retains structured issues and immutable error metadata.
    const stepId = existing ? "owner_transform" : "owner_transform_uolas";
    const issue = {
      code: "workflow.step_io.error",
      message,
      path: `workflow.steps.${stepId}.override.transform.input.source`,
      severity: "error",
      agent_id: "",
      input_name: "input.source",
      issue_type: "transform_input_source_unavailable",
      message_key: key,
      message_params: { source },
      skill_id: "",
      source,
      step_id: stepId,
    };
    const payload = { detail: {
      error: "Contract validation failed",
      entity_type: "workflow",
      entity_id: "wf_demo",
      action: "apply_changes",
      messages: [message],
      issues: [issue],
      validation: { valid: false, errors: [issue], warnings: [] },
    } };
    const originalPayload = JSON.stringify(payload);
    const error = new HttpError({ status: 400, url: "/api/workflows/wf_demo/changes", payload });
    applyWorkflowChanges.mockRejectedValueOnce(error);
    const { view, ctx, save, visibleError, visibleNotice } = setup(locale, existing);

    await save();

    expect(visibleError()).toBe(locale === "lt" ? interpolate(lithuanian, { source }) : message);
    expect(error.message).toBe(message);
    expect(JSON.stringify(payload)).toBe(originalPayload);
    expect(ctx.refetchWorkflowSurfaces).not.toHaveBeenCalled();
    expect(ctx.setSelectedStepId).not.toHaveBeenCalled();
    if (existing) {
      expect(ctx.setSaveNotice).toHaveBeenLastCalledWith("");
      expect(ctx.setSavingWorkflowSurface).toHaveBeenLastCalledWith("");
      expect(view.result.current.stepFormSaveState).toBe("error");
    } else {
      expect(view.result.current.changeSet.status).toBe("error");
      expect(view.result.current.changeSet.applyIssues).toEqual([issue]);
      expect(view.result.current.changeSet.operations).toHaveLength(1);
      expect(ctx.setSavingWorkflowSurface).not.toHaveBeenCalled();
    }
    const [workflowId, request] = applyWorkflowChanges.mock.calls[0];
    expect(workflowId).toBe("wf_demo");
    expect(request.expected_revision).toBe(7);
    expect(request.operations).toHaveLength(1);
    expect(request.operations[0].op).toBe(existing ? "update_step_config" : "create_step");
    if (existing) expect(request.operations[0].step_id).toBe("owner_transform");
    const saved = existing ? request.operations[0].patch : request.operations[0].step;
    expect(saved.display_name).toBe("Owner transform Ąžuolas");
    expect(saved.override.transform.input.source).toBe(source);

    const repaired = { transform: { input: { source: "state.inputs.payload" }, mode: "object", result_target: "state.outputs.owner_result", fields: [] } };
    await save(repaired);
    expect(applyWorkflowChanges).toHaveBeenCalledTimes(2);
    expect(ctx.refetchWorkflowSurfaces).toHaveBeenCalledTimes(1);
    expect(visibleNotice()).toBe(existing ? "Step parameters saved." : "Changes applied.");
    const retried = applyWorkflowChanges.mock.calls[1][1].operations[0];
    expect((existing ? retried.patch : retried.step).override.transform.input.source).toBe("state.inputs.payload");
    if (!existing) expect(view.result.current.changeSet.operations).toEqual([]);
  });

  it.each([
    { locale: "lt", missingInput: false },
    { locale: "lt", missingInput: true },
    { locale: "en", missingInput: false },
    { locale: "en", missingInput: true },
  ] as const)("localizes missing transform source for $locale / missingInput=$missingInput and preserves raw contract on next save", async ({ locale, missingInput }) => {
    const issue = { code: missingInput ? "workflow.step_transform_input_missing" : "workflow.step_transform_input_source_missing", message: "transform requires input.source", path: "workflow.steps.owner_transform.override.transform.input" + (missingInput ? "" : ".source"), severity: "error", message_key: "react.workflows.transform_input_source_required", message_params: {} };
    const payload = { detail: { error: "Contract validation failed", entity_type: "workflow", entity_id: "wf_demo", action: "apply_changes", messages: [issue.message], issues: [issue], validation: { valid: false, errors: [issue], warnings: [] } } };
    const raw = JSON.stringify(payload);
    const error = new HttpError({ status: 400, url: "/api/workflows/wf_demo/changes", payload });
    applyWorkflowChanges.mockRejectedValueOnce(error);
    const { ctx, save, visibleError, visibleNotice } = setup(locale, true);
    await save();
    expect(visibleError()).toBe(locale === "lt" ? "Transformavimui būtinas įvesties šaltinis (input.source)." : issue.message);
    expect(error.message).toBe(issue.message);
    expect(JSON.stringify(payload)).toBe(raw);
    expect(ctx.refetchWorkflowSurfaces).not.toHaveBeenCalled();
    const repaired = { transform: { input: { source: "state.workflow_started" }, mode: "object", result_target: "state.transform_result", fields: [{ from: "message", to: "echo", type: "text" }] } };
    await save(repaired);
    expect(applyWorkflowChanges).toHaveBeenCalledTimes(2);
    expect(ctx.refetchWorkflowSurfaces).toHaveBeenCalledTimes(1);
    expect(visibleNotice()).toBe("Step parameters saved.");
    expect(applyWorkflowChanges.mock.calls[1][1].operations[0].patch.override.transform.input.source).toBe("state.workflow_started");
  });

  it("preserves unkeyed owner errors and the fallback for non-Error failures", async () => {
    const { save, visibleError } = setup("lt", false);
    const ownerMessage = "Owner diagnostic: state.inputs.payload / Ąžuolas";
    applyWorkflowChanges.mockRejectedValueOnce(new HttpError({ status: 400, url: "/api/workflows/wf_demo/changes", payload: { detail: { validation: { errors: [{ message: ownerMessage }] } } } }));
    await save();
    expect(visibleError()).toBe(ownerMessage);
    applyWorkflowChanges.mockRejectedValueOnce(null);
    await save();
    expect(visibleError()).toBe("The changes were not applied.");
  });
});
