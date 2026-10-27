import { describe, expect, it, vi } from "vitest";
import { createElement, type ReactNode } from "react";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import * as simulateApi from "@/pages/workflows/shared/simulate-api";
import { HttpError } from "@/lib/http/errors";

import { workflowTestRunInputRow, useWorkflowTestRunController } from "@/pages/workflows/detail/detail-test-run-controller";

describe("Test Run inputs and configured defaults", () => {
  it("marks a field with a configured default as fixed", () => {
    // The backend refuses a caller value for these with
    // workflow.input.override_forbidden, so offering an editable box asks the
    // operator to type something that is guaranteed to be rejected.
    const row = workflowTestRunInputRow({
      name: "region",
      type: "string",
      required: true,
      default: "eu-west-1",
      default_configured: true,
    });

    expect(row.fixed).toBe(true);
    expect(row.required).toBe(false);
    expect(row.defaultPreview).toBe("eu-west-1");
  });

  it("leaves a field without a default editable and required", () => {
    const row = workflowTestRunInputRow({
      name: "region",
      type: "string",
      required: true,
    });

    expect(row.fixed).toBe(false);
    expect(row.required).toBe(true);
  });

  it("treats a configured boolean false as fixed", () => {
    const row = workflowTestRunInputRow({
      name: "dry_run",
      type: "boolean",
      default: false,
      default_configured: true,
    });

    expect(row.fixed).toBe(true);
    expect(row.defaultPreview).toBe("false");
  });

  it("shows a configured secret as set without showing anything", () => {
    const row = workflowTestRunInputRow({
      name: "login",
      type: "secret",
      required: true,
      default_configured: true,
    });

    expect(row.fixed).toBe(true);
    expect(row.defaultPreview).toBe("configured");
  });

  it("keeps an unconfigured secret editable so a caller can supply one", () => {
    const row = workflowTestRunInputRow({
      name: "login",
      type: "secret",
      required: true,
    });

    expect(row.fixed).toBe(false);
    expect(row.required).toBe(true);
  });

  it("shows the test file a file input runs with, or where to attach one", () => {
    const withFile = workflowTestRunInputRow({
      name: "invoice",
      type: "file",
      required: true,
      sample_file: { filename: "invoice.pdf" },
    });
    const without = workflowTestRunInputRow({ name: "invoice", type: "file", required: true });

    expect(withFile).toMatchObject({ fixed: true, testFile: true, required: false });
    expect(withFile.defaultPreview).toBe("Test file: invoice.pdf");
    expect(without).toMatchObject({ fixed: true, testFile: false, required: true });
    expect(without.defaultPreview).toContain("Attach one");
  });
});


describe("Test Run initial workflow summary locale", () => {
  it.each(["lt", "en"])("localizes the initial default in %s and preserves an edited summary on locale change and submission", async (locale) => {
    let activeLocale = locale;
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const ctx = { workflowId: "wf_summary_sentinel", selectedProjectId: "owner-project", workflowContractQuery: { data: {} }, stepContractPreviewQuery: { data: {} } };
    const wrapper = ({ children }: { children: ReactNode }) => createElement(I18nContext.Provider, {
      value: { ...fallbackContext, locale: activeLocale, t: (key, text, params) => fallbackContext.t(key, activeLocale === "lt" && key === "react.workflows.test_run_initial_summary" ? "Paleiskite šią darbo eigą iš kūrimo priemonės testavimo skydelio." : text, params) },
    }, createElement(QueryClientProvider, { client: queryClient }, children));
    const { result, rerender } = renderHook(() => useWorkflowTestRunController(ctx), { wrapper });
    expect(result.current.workflowTestRunSummary).toBe(locale === "lt" ? "Paleiskite šią darbo eigą iš kūrimo priemonės testavimo skydelio." : "Run this workflow from the builder test surface.");
    const authored = "Run this workflow Ąžuolas / owner-summary";
    act(() => result.current.setWorkflowTestRunSummary(authored));
    activeLocale = locale === "lt" ? "en" : "lt";
    rerender();
    expect(result.current.workflowTestRunSummary).toBe(authored);
    const submit = vi.spyOn(simulateApi, "testRunWorkflow").mockResolvedValue({ status: "completed", workflow_id: ctx.workflowId, needs_user_approval: false } as never);
    try {
      await act(async () => { await result.current.workflowTestRunMutation.mutateAsync(); });
      expect(submit).toHaveBeenCalledWith(expect.objectContaining({ taskSummary: authored, workflowId: ctx.workflowId, projectId: "owner-project" }));
      expect(result.current.workflowTestRunSummary).toBe(authored);
    } finally { submit.mockRestore(); }
  });
});


function renderDeclaredInputController(enabled: boolean, rows: Array<Record<string, unknown>>) {
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const context = {
    workflowId: "wf_native_form_guard", selectedProjectId: "owner-project", serverRevision: 7,
    workflowContractQuery: { data: { interface: { enabled, input_mode: "fields", expected_inputs: rows } } },
    stepContractPreviewQuery: { data: {} },
  };
  const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client: queryClient }, children);
  return renderHook(() => useWorkflowTestRunController(context), { wrapper });
}

describe("declared Test Run input guards before submission", () => {
  const invalidValues = [
    { type: "object", raw: "{", options: [] },
    { type: "object", raw: "[]", options: [] },
    { type: "array", raw: "[", options: [] },
    { type: "array", raw: "{}", options: [] },
    { type: "number", raw: "not-a-number", options: [] },
    { type: "boolean", raw: "neither", options: [] },
    { type: "select", raw: "unlisted", options: ["listed"] },
  ];

  it.each([false, true].flatMap((enabled) => invalidValues.map((value) => ({ enabled, ...value }))))(
    "refuses $type draft $raw before API submission when interface enabled=$enabled", async ({ enabled, type, raw, options }) => {
      const submit = vi.spyOn(simulateApi, "testRunWorkflow").mockResolvedValue({
        status: "queued", workflow_id: "wf_native_form_guard", job_id: "unexpected-job", thread_id: "unexpected-thread", needs_user_approval: false,
      });
      const { result, unmount } = renderDeclaredInputController(enabled, [{ name: "audit_data", type, required: true, options }]);
      try {
        act(() => {
          result.current.setWorkflowTestRunSummary("A valid controller audit Summary");
          result.current.setWorkflowTestRunInputs({ audit_data: raw });
        });
        let outcome = "unsettled";
        await act(async () => {
          outcome = await result.current.workflowTestRunMutation.mutateAsync().then(() => "submitted", () => "rejected");
        });

        expect(submit).not.toHaveBeenCalled();
        expect(outcome).toBe("rejected");
        expect(result.current.workflowTestRunInputs.audit_data).toBe(raw);
        expect(result.current.workflowTestRunSummary).toBe("A valid controller audit Summary");
        expect(result.current.workflowTestRunPayload).toBeNull();
        expect(result.current.workflowTestRunJobId).toBe("");
      } finally {
        unmount();
        submit.mockRestore();
      }
    },
  );

  it("preserves zero, false, fractional numbers and empty structured values in a valid typed submission", async () => {
    const submit = vi.spyOn(simulateApi, "testRunWorkflow").mockResolvedValue({ status: "completed", workflow_id: "wf_native_form_guard", needs_user_approval: false });
    const { result, unmount } = renderDeclaredInputController(false, [
      { name: "count", type: "number", required: true },
      { name: "fraction", type: "number", required: true },
      { name: "flag", type: "boolean", required: true },
      { name: "record", type: "object", required: true },
      { name: "items", type: "array", required: true },
    ]);
    try {
      act(() => {
        result.current.setWorkflowTestRunSummary("Valid typed boundary");
        result.current.setWorkflowTestRunInputs({ count: "0", fraction: "1.25", flag: "false", record: "{}", items: "[]" });
      });
      await act(async () => { await result.current.workflowTestRunMutation.mutateAsync(); });

      expect(submit).toHaveBeenCalledTimes(1);
      expect(submit).toHaveBeenCalledWith({
        workflowId: "wf_native_form_guard", projectId: "owner-project", taskSummary: "Valid typed boundary", dryRun: false, expectedRevision: 7,
        inputs: { count: 0, fraction: 1.25, flag: false, record: {}, items: [] },
      });
    } finally {
      unmount();
      submit.mockRestore();
    }
  });
});


describe("optional numeric and nested server-error controller boundaries", () => {
  it.each([false, true])("refuses an optional incomplete exponent directly when interface enabled=%s", async (enabled) => {
    const submit = vi.spyOn(simulateApi, "testRunWorkflow").mockResolvedValue({ status: "completed", workflow_id: "wf_native_form_guard", needs_user_approval: false });
    const { result, unmount } = renderDeclaredInputController(enabled, [{ name: "quantity", label: "Audit quantity", type: "number", required: false }]);
    try {
      act(() => {
        result.current.setWorkflowTestRunSummary("Optional numeric guard");
        result.current.setWorkflowTestRunInputs({ quantity: "1e" });
      });
      let outcome = "unsettled";
      await act(async () => { outcome = await result.current.workflowTestRunMutation.mutateAsync().then(() => "submitted", () => "rejected"); });

      expect(submit).not.toHaveBeenCalled();
      expect(outcome).toBe("rejected");
      expect(result.current.workflowTestRunInputs.quantity).toBe("1e");
      expect(result.current.workflowTestRunFieldErrors["inputs.quantity"]).toMatch(/number/i);
      expect(result.current.workflowTestRunHasValidationErrors).toBe(true);
      act(() => result.current.updateWorkflowTestRunInput("quantity", "1.25"));
      await act(async () => { await result.current.workflowTestRunMutation.mutateAsync(); });
      expect(submit).toHaveBeenCalledTimes(1);
      expect(submit).toHaveBeenCalledWith(expect.objectContaining({ inputs: { quantity: 1.25 }, expectedRevision: 7 }));
    } finally {
      unmount();
      submit.mockRestore();
    }
  });

  it.each([
    { path: "inputs.record.details.count", owner: "record.details" },
    { path: "inputs.items.0", owner: "items" },
    { path: "inputs.record", owner: "record" },
    { path: "inputs.recording.count", owner: "" },
    { path: "inputs.title.count", owner: "" },
    { path: "workflow.root", owner: "" },
  ])("maps $path to its exact or longest structured owner '$owner' and keeps unknown errors global", async ({ path, owner }) => {
    const message = "Workflow input schema validation failed: nested constraint was not satisfied";
    const submit = vi.spyOn(simulateApi, "testRunWorkflow").mockRejectedValue(new HttpError({
      status: 422, url: "/api/workflows/wf_native_form_guard/test-run", payload: {
        detail: { code: "workflow_input_validation_failed", errors: [{ code: "workflow.input.schema_validation_failed", path, message, severity: "error" }] },
      },
    }));
    const { result, unmount } = renderDeclaredInputController(true, [
      { name: "record", type: "object" }, { name: "record.details", type: "object" },
      { name: "items", type: "array" }, { name: "title", type: "string" },
    ]);
    try {
      act(() => {
        result.current.setWorkflowTestRunSummary("Nested field mapping");
        result.current.setWorkflowTestRunInputs({ record: "{}", "record.details": "{}", items: "[0]", title: "A title" });
      });
      await act(async () => { await result.current.workflowTestRunMutation.mutateAsync().catch(() => undefined); });

      expect(submit).toHaveBeenCalledTimes(1);
      expect(result.current.workflowTestRunFieldErrors).toEqual(owner ? { [`inputs.${owner}`]: message } : {});
      expect(result.current.workflowTestRunMessage).toBe(message);
      expect(result.current.workflowTestRunInputs).toEqual({ record: "{}", "record.details": "{}", items: "[0]", title: "A title" });
      expect(result.current.workflowTestRunPayload).toBeNull();
    } finally {
      unmount();
      submit.mockRestore();
    }
  });
});
