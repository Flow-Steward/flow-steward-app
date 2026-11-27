import { render, screen } from "@testing-library/react";

import { OperatorReviewPanel } from "./review-panel";
import type { JobReview, ReviewGate } from "./review-model";

function review(extra: Partial<JobReview> & { gateExtra?: Partial<ReviewGate> } = {}): JobReview {
  const { gateExtra, ...rest } = extra;
  return {
    job_id: "job-1",
    status: "awaiting_approval",
    waiting: true,
    review_job_id: "job-1",
    access: "decide",
    gate: {
      job_id: "job-1",
      thread_id: "thread-1",
      approval_id: "gate-1",
      allowed_actions: ["approve", "reject"],
      payload: {},
      review: { fields: [], values: {}, invalid: {}, original: [] },
      ...gateExtra,
    },
    ...rest,
  };
}

it("keeps the decision buttons when the review says nothing about who decides", () => {
  render(<OperatorReviewPanel onApprovedNext={vi.fn()} review={review()} />);
  expect(screen.getByTestId("review-approve")).toBeInTheDocument();
  expect(screen.getByTestId("review-reject")).toBeInTheDocument();
  expect(screen.queryByTestId("review-waiting-for")).not.toBeInTheDocument();
});

it("hides the decision buttons and names the approvers when the step is assigned to others", () => {
  render(
    <OperatorReviewPanel
      onApprovedNext={vi.fn()}
      review={review({ gateExtra: { payload: { can_decide: false, approvers: [{ username: "ann", display_name: "Ann Example" }] } } })}
    />,
  );
  expect(screen.queryByTestId("review-approve")).not.toBeInTheDocument();
  expect(screen.queryByTestId("review-approve-next")).not.toBeInTheDocument();
  expect(screen.queryByTestId("review-reject")).not.toBeInTheDocument();
  expect(screen.getByTestId("review-waiting-for")).toHaveTextContent("Waiting for Ann Example to decide.");
});
