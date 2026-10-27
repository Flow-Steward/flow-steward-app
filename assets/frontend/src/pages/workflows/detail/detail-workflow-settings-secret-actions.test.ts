import { beforeEach, describe, expect, it, vi } from "vitest";

const setWorkflowInputSecret = vi.hoisted(() => vi.fn());
const clearWorkflowInputSecret = vi.hoisted(() => vi.fn());
const setWorkflowInputSampleFile = vi.hoisted(() => vi.fn());
const clearWorkflowInputSampleFile = vi.hoisted(() => vi.fn());

vi.mock("@/pages/workflows/shared/api", () => ({
  setWorkflowInputSecret,
  clearWorkflowInputSecret,
  setWorkflowInputSampleFile,
  clearWorkflowInputSampleFile,
}));

import {
  applyWorkflowInputSampleFileActions,
  applyWorkflowInputSecretActions,
} from "./detail-workflow-settings-controller";
import { createWorkflowInterfaceFieldDraft } from "./detail-interface";

describe("workflow settings secret actions", () => {
  beforeEach(() => {
    setWorkflowInputSecret.mockReset();
    clearWorkflowInputSecret.mockReset();
  });

  it("advances the returned revision across multiple secret mutations", async () => {
    setWorkflowInputSecret
      .mockResolvedValueOnce({ default_configured: true, revision: 12 })
      .mockResolvedValueOnce({ default_configured: true, revision: 13 });

    const result = await applyWorkflowInputSecretActions({
      workflowId: "orders",
      initialRevision: 11,
      rows: [
        createWorkflowInterfaceFieldDraft({
          name: "api_key",
          type: "secret",
          secretAction: "set",
          secretDraft: "one",
        }),
        createWorkflowInterfaceFieldDraft({
          name: "signing_key",
          type: "secret",
          secretAction: "set",
          secretDraft: "two",
        }),
      ],
    });

    expect(setWorkflowInputSecret.mock.calls.map(([request]) => request.expectedRevision)).toEqual([
      11, 12,
    ]);
    expect(result.revision).toBe(13);
    expect(result.failures).toEqual([]);
  });

  it("keeps the current revision after a rejected row and reports its detail", async () => {
    setWorkflowInputSecret.mockRejectedValueOnce(
      new Error("The workflow changed after it was loaded. Reload before saving."),
    );
    clearWorkflowInputSecret.mockResolvedValueOnce({
      default_configured: false,
      revision: 22,
    });

    const result = await applyWorkflowInputSecretActions({
      workflowId: "orders",
      initialRevision: 21,
      rows: [
        createWorkflowInterfaceFieldDraft({
          name: "api_key",
          type: "secret",
          secretAction: "set",
          secretDraft: "one",
        }),
        createWorkflowInterfaceFieldDraft({
          name: "old_key",
          type: "secret",
          secretAction: "clear",
        }),
      ],
    });

    expect(clearWorkflowInputSecret).toHaveBeenCalledWith({
      workflowId: "orders",
      name: "old_key",
      expectedRevision: 21,
    });
    expect(result.revision).toBe(22);
    expect(result.failures).toEqual([
      {
        name: "api_key",
        message: "The workflow changed after it was loaded. Reload before saving.",
      },
    ]);
  });

  it("uploads a picked test file and removes a cleared one, advancing the revision", async () => {
    setWorkflowInputSampleFile.mockReset().mockResolvedValueOnce({ revision: 31 });
    clearWorkflowInputSampleFile.mockReset().mockResolvedValueOnce({ revision: 32 });
    const file = new File(["%PDF-1.7"], "invoice.pdf", { type: "application/pdf" });

    const result = await applyWorkflowInputSampleFileActions({
      workflowId: "orders",
      initialRevision: 30,
      rows: [
        createWorkflowInterfaceFieldDraft({
          name: "invoice",
          type: "file",
          sampleFileAction: "set",
          sampleFileDraft: file,
        }),
        createWorkflowInterfaceFieldDraft({ name: "old", type: "file", sampleFileAction: "clear" }),
        createWorkflowInterfaceFieldDraft({ name: "untouched", type: "file" }),
        createWorkflowInterfaceFieldDraft({ name: "note", type: "string", sampleFileAction: "clear" }),
      ],
    });

    expect(setWorkflowInputSampleFile).toHaveBeenCalledWith({
      workflowId: "orders",
      name: "invoice",
      file,
      expectedRevision: 30,
    });
    expect(clearWorkflowInputSampleFile).toHaveBeenCalledWith({
      workflowId: "orders",
      name: "old",
      expectedRevision: 31,
    });
    expect(result).toEqual({ revision: 32, applied: ["invoice", "old"], failures: [] });
  });
});
