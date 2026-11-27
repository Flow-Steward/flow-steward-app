import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { HttpError } from "@/lib/http/errors";
import { approvalGateFromResponse, ReviewWorkspacePanel } from "@/pages/jobs/detail/job-detail-review-panels";

const workspace = { kind: "document_review" };

function panel(approvalGate: Record<string, unknown>, onSubmit = vi.fn().mockResolvedValue(undefined)) {
  render(
    <ReviewWorkspacePanel
      approvalGate={approvalGate}
      canMutate
      isLoading={false}
      onSubmit={onSubmit}
      submitting={false}
      workspace={workspace}
    />,
  );
  return onSubmit;
}

it("keeps the decision buttons when the gate says nothing about who decides", () => {
  panel({ approval_id: "gate-1", thread_id: "thread-1" });
  expect(screen.getByTestId("approval-approve-button")).toBeInTheDocument();
  expect(screen.getByTestId("approval-reject-button")).toBeInTheDocument();
  expect(screen.queryByText(/Waiting for/)).not.toBeInTheDocument();
});

it("keeps the decision buttons when the person may decide", () => {
  panel({ approval_id: "gate-1", thread_id: "thread-1", can_decide: true, approvers: [] });
  expect(screen.getByTestId("approval-approve-button")).toBeInTheDocument();
});

it("hides the buttons and names the approvers when the step is assigned to others", () => {
  panel(approvalGateFromResponse({
    approval_id: "gate-1",
    thread_id: "thread-1",
    payload: { approval_id: "gate-1", thread_id: "thread-1" },
    can_decide: false,
    approvers: [{ username: "ann", display_name: "Ann Example" }, { username: "bob", display_name: "" }],
  }));
  expect(screen.queryByTestId("approval-approve-button")).not.toBeInTheDocument();
  expect(screen.queryByTestId("approval-reject-button")).not.toBeInTheDocument();
  expect(screen.queryByTestId("approval-comment-input")).not.toBeInTheDocument();
  expect(screen.getByText("Waiting for Ann Example, bob to decide.")).toBeInTheDocument();
});

it("reads the assignment from the gate payload as well", () => {
  panel(approvalGateFromResponse({
    approval_id: "gate-1",
    payload: { thread_id: "thread-1", can_decide: false, approvers: [{ username: "ann", display_name: "Ann Example" }] },
  }));
  expect(screen.queryByTestId("approval-approve-button")).not.toBeInTheDocument();
  expect(screen.getByText("Waiting for Ann Example to decide.")).toBeInTheDocument();
});

it("explains a decision refused because the step is assigned to other people", async () => {
  const user = userEvent.setup();
  const refused = new HttpError({
    status: 403,
    url: "/api/approvals/thread-1/decide",
    payload: { code: "approval_not_assigned", message: "forbidden" },
  });
  panel({ approval_id: "gate-1", thread_id: "thread-1" }, vi.fn().mockRejectedValue(refused));
  await user.click(screen.getByTestId("approval-approve-button"));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Only the approvers assigned to this step can decide it."));
});

it("shows no decision buttons before the gate has loaded, then only what the loaded gate allows", () => {
  const props = { canMutate: true, onSubmit: vi.fn(), submitting: false, workspace };
  const view = render(<ReviewWorkspacePanel {...props} approvalGate={{ approval_id: "gate-1", thread_id: "thread-1" }} isLoading />);
  expect(screen.queryByTestId("approval-approve-button")).not.toBeInTheDocument();
  expect(screen.queryByTestId("approval-reject-button")).not.toBeInTheDocument();
  view.rerender(
    <ReviewWorkspacePanel
      {...props}
      approvalGate={{ approval_id: "gate-1", thread_id: "thread-1", can_decide: false, approvers: [{ username: "ann", display_name: "Ann Example" }] }}
      isLoading={false}
    />,
  );
  expect(screen.queryByTestId("approval-approve-button")).not.toBeInTheDocument();
  expect(screen.getByText("Waiting for Ann Example to decide.")).toBeInTheDocument();
});
