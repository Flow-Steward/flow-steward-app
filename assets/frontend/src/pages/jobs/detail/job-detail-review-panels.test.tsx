import { collectEvidenceItems } from "./job-review-evidence";
import { StepInspectorPanel } from "./job-step-inspector-panel";
import { ReadableDataFields } from "./readable-data-fields";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { fallbackContext } from "@/lib/i18n/context";

import { fetchJobStepOutputValue } from "@/pages/jobs/detail/api";

import {
  approvalGateWithThreadFallback,
  approvalGateFromResponse,
  approvalResponseFromStream,
  ReviewWorkspacePanel,
  resolveReviewWorkspace,
} from "@/pages/jobs/detail/job-detail-review-panels";

import { buildCanonicalJobDetailView } from "@/pages/jobs/detail/job-detail-view-model";

vi.mock("@/pages/jobs/detail/api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  fetchJobStepOutputValue: vi.fn(),
}));

describe("collectEvidenceItems", () => {
  it("keeps user-facing evidence while hiding duplicated technical metadata", () => {
    const items = collectEvidenceItems({
      evidence: [
        { title: "Step ID", summary: "workflow_start" },
        { title: "Assigned", summary: "workflow_data" },
        { title: "Error detail", summary: "input_source_missing" },
        { title: "Review verdict", summary: "Passed all validations" },
      ],
    });

    expect(items).toEqual([
      expect.objectContaining({
        label: "Review verdict",
        summary: "Passed all validations",
      }),
    ]);
  });
});

describe("StepInspectorPanel", () => {
  it("localizes an explicitly keyed runtime step type without translating owner text", () => {
    const translator = vi.spyOn(fallbackContext, "t").mockImplementation((key, fallback) =>
      key === "react.workflows.transform" ? "Transformacija" : (fallback || key),
    );
    try {
      render(<StepInspectorPanel selectedStep={{ title: "Owner step", step_type_label: "Transform",
        step_type_label_key: "react.workflows.transform", summary: "Owner diagnostic" }} testId="step-inspector" />);
      expect(screen.getByText("Transformacija")).toBeInTheDocument();
      expect(screen.getByText("Owner step")).toBeInTheDocument();
      expect(screen.queryByText("Transform")).not.toBeInTheDocument();
    } finally {
      translator.mockRestore();
    }
  });
  it("shows a step type instead of an agent when no agent ran the step", () => {
    render(
      <StepInspectorPanel
        selectedStep={{
          title: "Start: Control Flow",
          status_label: "Completed",
          agent: "control_flow_step",
          agent_label: "",
          skill_label: "Control Flow",
          step_type_label: "Control flow",
        }}
        testId="step-inspector"
      />,
    );

    expect(screen.getByText("Step type")).toBeInTheDocument();
    expect(screen.getByText("Control flow")).toBeInTheDocument();
    expect(screen.queryByText("Agent")).not.toBeInTheDocument();
    expect(screen.queryByText("Tool")).not.toBeInTheDocument();
  });

  it("shows an extension as the tool and its operation as the action", () => {
    render(
      <StepInspectorPanel
        selectedStep={{
          title: "Sync: Shopify Products",
          step_type_label: "Extension action",
          tool_label: "shopify",
          operation_label: "Update inventory",
        }}
        testId="step-inspector"
      />,
    );

    expect(screen.getByText("Tool")).toBeInTheDocument();
    expect(screen.getByText("shopify")).toBeInTheDocument();
    expect(screen.getByText("Action")).toBeInTheDocument();
    expect(screen.getByText("Update inventory")).toBeInTheDocument();
  });

  it("shows an agent with its skill", () => {
    render(
      <StepInspectorPanel
        selectedStep={{ title: "Write: Product Description", agent_label: "SEO copywriter", skill_label: "Product description" }}
        testId="step-inspector"
      />,
    );

    expect(screen.getByText("Agent")).toBeInTheDocument();
    expect(screen.getByText("SEO copywriter")).toBeInTheDocument();
    expect(screen.getByText("Skill")).toBeInTheDocument();
    expect(screen.queryByText("Step type")).not.toBeInTheDocument();
  });

  it("shows the concrete operation, honest timing, and downloadable file roles", () => {
    render(
      <StepInspectorPanel
        selectedStep={{
          title: "Extract document",
          status_label: "Completed",
          operation_id: "convert_document",
          operation_label: "Convert document",
          duration_ms: null,
          provider_processing_time: 1.538,
          provider_processing_time_unit: null,
          inputs: { connection_ref: "conn-internal", do_ocr: false },
          outputs: { next_cursor: "opaque", pages: 4 },
          input_artifacts: [
            {
              artifact_id: "art-input",
              filename: "invoice.pdf",
              content_type: "application/pdf",
              size_bytes: 2048,
              available: true,
              download_url: "/api/jobs/job-1/artifacts/art-input",
            },
          ],
          output_artifacts: [
            {
              artifact_id: "art-output",
              filename: "invoice.md",
              content_type: "text/markdown",
              size_bytes: 4096,
              available: true,
              download_url: "/api/jobs/job-1/artifacts/art-output",
            },
          ],
        }}
        testId="step-inspector"
      />,
    );

    expect(screen.getByText("Convert document")).toBeInTheDocument();
    expect(screen.getByText("Not recorded")).toBeInTheDocument();
    expect(screen.getByText("1.538 (unit not declared)")).toBeInTheDocument();
    expect(screen.getByText("invoice.pdf")).toBeInTheDocument();
    expect(screen.getByText("invoice.md")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Download" })).toHaveLength(2);
    expect(screen.queryByText("conn-internal")).not.toBeInTheDocument();
    expect(screen.queryByText("opaque")).not.toBeInTheDocument();
  });
});

describe("ReviewWorkspacePanel", () => {
  it("replaces a stale root gate id atomically from a sequential approval frame", () => {
    const next = approvalResponseFromStream(
      {
        job_id: "job-1",
        approval_id: "gate-a",
        payload: {
          approval_id: "gate-a",
          kind: "document_review",
          content: { title: "Gate A" },
          show_outputs: ["summary"],
        },
        review_workspace: { kind: "document_review", title: "Gate A" },
      },
      { approval_id: "gate-b", interrupt_id: "gate-b", stage: "Review B" },
      "job-1",
    );

    expect(next).toMatchObject({
      approval_id: "gate-b",
      payload: { approval_id: "gate-b", interrupt_id: "gate-b", stage: "Review B" },
    });
    expect(next.payload).not.toHaveProperty("kind");
    expect(next.payload).not.toHaveProperty("content");
    expect(next.payload).not.toHaveProperty("show_outputs");
    expect(next).not.toHaveProperty("review_workspace");
  });

  it("clears a stale gate id when a frame has no immutable identity", () => {
    const merged = approvalResponseFromStream(
      { job_id: "job-1", approval_id: "gate-a", payload: { approval_id: "gate-a" } },
      { stage: "Review B" },
      "job-1",
    );

    expect(approvalGateFromResponse(merged).approval_id).toBeUndefined();
  });

  it("keeps the root approval id in the projected gate", () => {
    expect(
      approvalGateFromResponse({
        approval_id: "gate-root",
        payload: { thread_id: "thread-7" },
      }),
    ).toMatchObject({ approval_id: "gate-root", thread_id: "thread-7" });
  });

  it("keeps the canonical job thread while a sequential stream gate replaces REST data", () => {
    expect(
      approvalGateWithThreadFallback(
        {
          approval_id: "gate-next",
          payload: { approval_id: "gate-next", stage: "Next review" },
        },
        "thread-stable",
      ),
    ).toMatchObject({ approval_id: "gate-next", thread_id: "thread-stable" });
  });

  it("keeps an ordinary raw approval optional on approve without enabling overrides", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const approvalPayload = { job_id: "job-normal", approval_id: "gate-raw-normal", thread_id: "thread-raw-normal",
      payload: { stage: "Review", content: "Original document" } };
    const workspace = resolveReviewWorkspace({ approvalPayload, canMutate: true,
      canonicalDetail: buildCanonicalJobDetailView({ detailEntity: { status: "awaiting_approval" } }) });
    render(<ReviewWorkspacePanel approvalGate={approvalGateFromResponse(approvalPayload)} canMutate
      isLoading={false} onSubmit={onSubmit} submitting={false} workspace={workspace} />);
    expect(screen.getByRole("textbox", { name: "Comment for rejection" })).not.toBeRequired();
    expect(screen.queryByText("*")).not.toBeInTheDocument();
    expect(screen.queryByTestId("approval-override-input")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith("thread-raw-normal", {
      approval_id: "gate-raw-normal", approved: true, comment: "",
    });
  });

  it("submits the current immutable approval gate id", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);

    render(
      <ReviewWorkspacePanel
        approvalGate={{ approval_id: "gate-current", thread_id: "thread-7" }}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "document_review" }}
      />,
    );

    await user.click(screen.getByTestId("approval-approve-button"));

    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith("thread-7", {
        approval_id: "gate-current",
        approved: true,
        comment: "",
      }),
    );
  });

  it("submits an approval id the detail response carries but the REST gate lacks", async () => {
    // AC17-R30: the identity is present in the job detail response the page already
    // reads. Deriving it from only two places left Approve inert on the live stand.
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const gate = approvalGateWithThreadFallback({}, "", [
      { approval_id: "gate-from-detail", thread_id: "thread-detail" },
    ]);

    render(
      <ReviewWorkspacePanel
        approvalGate={gate}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "document_review" }}
      />,
    );

    await user.click(screen.getByTestId("approval-approve-button"));

    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith("thread-detail", {
        approval_id: "gate-from-detail",
        approved: true,
        comment: "",
      }),
    );
  });

  it.each([
    { thread_id: "thread-7" },
    { approval_id: "gate-current" },
  ])("waits for both approval identities before exposing a decision: %j", (gate) => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <ReviewWorkspacePanel
        approvalGate={gate}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "document_review", title: "Unverified generic gate" }}
      />,
    );

    expect(screen.getByText("Loading approval workspace…")).toBeInTheDocument();
    expect(screen.queryByText("Unverified generic gate")).not.toBeInTheDocument();
    expect(screen.queryByTestId("approval-approve-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("approval-reject-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("approval-comment-input")).not.toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("submits the first decision after an identityless stream gate receives canonical data", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const generic = approvalResponseFromStream(
      { job_id: "job-1", approval_id: "gate-old", thread_id: "thread-7" },
      { stage: "Review next" },
      "job-1",
    );
    const { rerender } = render(
      <ReviewWorkspacePanel
        approvalGate={approvalGateFromResponse(generic)}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "document_review", title: "Generic review" }}
      />,
    );
    expect(screen.getByText("Loading approval workspace…")).toBeInTheDocument();
    expect(screen.queryByTestId("approval-approve-button")).not.toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();

    rerender(
      <ReviewWorkspacePanel
        approvalGate={{ approval_id: "gate-current", thread_id: "thread-7" }}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "document_review", title: "Canonical approval request" }}
      />,
    );
    expect(screen.getByText("Canonical approval request")).toBeInTheDocument();
    expect(screen.queryByText("Loading approval workspace…")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("approval-approve-button"));
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith("thread-7", {
      approval_id: "gate-current", approved: true, comment: "",
    });
    expect(screen.queryByText("This approval gate has no approval ID recorded. Refresh before deciding.")).not.toBeInTheDocument();
  });

  it("resets comment and override content when approval_id changes", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <ReviewWorkspacePanel
        approvalGate={{ approval_id: "gate-a", thread_id: "thread-7" }}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "document_review", override_content_supported: true }}
      />,
    );

    await user.type(screen.getByTestId("approval-comment-input"), "old comment");
    await user.type(screen.getByTestId("approval-override-input"), "old override");

    rerender(
      <ReviewWorkspacePanel
        approvalGate={{ approval_id: "gate-b", thread_id: "thread-7" }}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "document_review", override_content_supported: true }}
      />,
    );

    expect(screen.getByTestId("approval-comment-input")).toHaveValue("");
    expect(screen.getByTestId("approval-override-input")).toHaveValue("");
  });

  it("resets a clarifying answer when approval_id changes", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <ReviewWorkspacePanel
        approvalGate={{ approval_id: "gate-a", thread_id: "thread-7" }}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "clarifying_answer" }}
      />,
    );

    await user.type(screen.getByTestId("approval-answer-input"), "old answer");

    rerender(
      <ReviewWorkspacePanel
        approvalGate={{ approval_id: "gate-b", thread_id: "thread-7" }}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "clarifying_answer" }}
      />,
    );

    expect(screen.getByTestId("approval-answer-input")).toHaveValue("");
  });

  it("replaces reject with an explicit cancel action when rework is exhausted", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);

    render(
      <ReviewWorkspacePanel
        approvalGate={{
          approval_id: "gate-exhausted",
          thread_id: "thread-7",
          allowed_actions: ["approve", "abort"],
          rework_budget_exhausted: true,
        }}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "document_review", comment_required_on_reject: true }}
      />,
    );

    expect(screen.queryByTestId("approval-reject-button")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("approval-abort-button"));

    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith("thread-7", {
        approval_id: "gate-exhausted",
        approved: false,
        action: "abort",
        comment: "",
      }),
    );
    expect(screen.getByRole("textbox", { name: "Comment" })).toBeInTheDocument();
    expect(screen.queryByText("Comment for rejection")).not.toBeInTheDocument();
    expect(screen.queryByText("Required when rejecting. Optional when approving.")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Comment" })).not.toHaveAttribute("aria-describedby");
  });

  it("requires a comment for an available rejection before submitting its decision", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);

    render(
      <ReviewWorkspacePanel
        approvalGate={{
          approval_id: "gate-review",
          thread_id: "thread-7",
          allowed_actions: ["approve", "reject"],
        }}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "document_review", comment_required_on_reject: true }}
      />,
    );

    const comment = screen.getByRole("textbox", { name: "Comment for rejection" });
    const requirement = screen.getByText("Required when rejecting. Optional when approving.");
    expect(requirement).toBeVisible();
    expect(requirement).toHaveAttribute("id");
    expect(comment).toHaveAttribute("aria-describedby", requirement.getAttribute("id"));
    expect(comment).toHaveAccessibleDescription("Required when rejecting. Optional when approving.");
    expect(comment).not.toBeRequired();
    await user.click(screen.getByTestId("approval-reject-button"));
    expect(screen.getByRole("alert")).toHaveTextContent("A rejection comment is required for this review gate.");
    expect(screen.getByText("A rejection comment is required for this review gate.")).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
    await user.type(screen.getByRole("textbox", { name: "Comment for rejection" }), "Needs another pass");
    await user.click(screen.getByTestId("approval-reject-button"));

    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith("thread-7", {
        approval_id: "gate-review",
        approved: false,
        comment: "Needs another pass",
      }),
    );
  });

  it("explains the rejection-only comment requirement while allowing approval with no comment", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <ReviewWorkspacePanel
        approvalGate={{ approval_id: "gate-approve-no-comment", thread_id: "thread-7", allowed_actions: ["approve", "reject"] }}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "document_review", comment_required_on_reject: true }}
      />,
    );

    const comment = screen.getByRole("textbox", { name: "Comment for rejection" });
    expect(comment).toHaveValue("");
    expect(comment).toHaveAccessibleDescription("Required when rejecting. Optional when approving.");
    expect(comment).not.toBeRequired();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith("thread-7", {
      approval_id: "gate-approve-no-comment", approved: true, comment: "",
    }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not announce a rejection comment requirement when the public workspace does not require one", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <ReviewWorkspacePanel
        approvalGate={{ approval_id: "gate-comment-optional", thread_id: "thread-7", allowed_actions: ["approve", "reject"] }}
        canMutate
        isLoading={false}
        onSubmit={onSubmit}
        submitting={false}
        workspace={{ kind: "document_review", comment_required_on_reject: false }}
      />,
    );

    const comment = screen.getByRole("textbox", { name: "Comment" });
    expect(comment).not.toBeRequired();
    expect(comment).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByText("Required when rejecting. Optional when approving.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith("thread-7", {
      approval_id: "gate-comment-optional", approved: false, comment: "",
    }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("ReadableDataFields", () => {
  it("shows request parameters and a response summary instead of field counts", () => {
    render(
      <ReadableDataFields
        value={{
          query_params: { name: "Vilnius", count: "5" },
          body: {
            results: [
              { name: "Vilnius" },
              { name: "Kyviskes" },
              { _flow_steward_projection: { truncated: true, omitted_items: 2 } },
            ],
            generationtime_ms: 0.25,
          },
        }}
      />,
    );

    expect(screen.getByText("Vilnius")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByText("results: 4 items · generationtime_ms: 0.25")).toBeInTheDocument();
    expect(screen.queryByText(/recorded field/)).not.toBeInTheDocument();
    expect(screen.getByText(/… 2 more items/)).toBeInTheDocument();
    expect(screen.queryByText(/_flow_steward_projection/)).not.toBeInTheDocument();
  });

  it("counts a cut list by its full length", () => {
    render(
      <ReadableDataFields
        value={{ rows: [{ sku: "A" }, { sku: "B" }, "… 23 more items (25 in total)"] }}
      />,
    );

    expect(screen.getByText("25 items")).toBeInTheDocument();
  });

  it("counts a cut list of plain values by its full length", () => {
    render(
      <ReadableDataFields
        value={{ skus: ["A", "B", "C", "D", "E", "F", "G", "… 18 more items (25 in total)"] }}
      />,
    );

    expect(screen.getByText("A, B, C, D, E, F … (25 in total)")).toBeInTheDocument();
  });
});

describe("StepInspectorPanel full text", () => {
  it("loads an output the preview cut short", async () => {
    const full = "Kaunas is Lithuania's second city. ".repeat(40);
    vi.mocked(fetchJobStepOutputValue).mockResolvedValue({ field: "brief", value: full });
    render(
      <StepInspectorPanel
        jobId="job-1"
        selectedStep={{
          id: "message-m1",
          message_id: "m1",
          title: "Write: Write brief",
          status: "completed",
          outputs: { brief: `${full.slice(0, 500)}… (${full.length} characters in total)` },
        }}
        testId="inspector"
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Show full text" }));

    expect(fetchJobStepOutputValue).toHaveBeenCalledWith("job-1", "m1", "brief");
    expect(await screen.findByText((_text, node) => node?.tagName === "PRE" && node.textContent === full)).toBeInTheDocument();
  });

  it("offers nothing for a value that was not cut", () => {
    render(
      <StepInspectorPanel
        jobId="job-1"
        selectedStep={{ id: "message-m2", message_id: "m2", title: "Step", outputs: { city: "Vilnius" } }}
        testId="inspector"
      />,
    );

    expect(screen.queryByRole("button", { name: "Show full text" })).not.toBeInTheDocument();
  });
});
