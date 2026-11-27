import { render, screen } from "@testing-library/react";

import { ApprovalDecision } from "./detail-step-test-run-latest";
import type { TestRunApproval } from "./detail-test-run-evidence";

function approval(extra: Partial<TestRunApproval> = {}): TestRunApproval {
  return {
    stepId: "review",
    identity: "job-1:gate-1",
    stepLabel: "Review",
    message: "Check the invoice",
    shown: [],
    approvedTo: "",
    rejectedTo: "",
    pending: false,
    error: "",
    decide: vi.fn(),
    ...extra,
  };
}

it("keeps the decision when the gate says nothing about who decides", () => {
  render(<ApprovalDecision approval={approval()} focusRequest={false} />);
  expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
  expect(screen.queryByTestId("workflow-approval-waiting-for")).not.toBeInTheDocument();
});

it("shows who decides instead of the buttons when the step is assigned to others", () => {
  render(<ApprovalDecision approval={approval({ waitingFor: ["Ann Example"] })} focusRequest={false} />);
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  expect(screen.getByTestId("workflow-approval-waiting-for")).toHaveTextContent("Waiting for Ann Example to decide.");
});
