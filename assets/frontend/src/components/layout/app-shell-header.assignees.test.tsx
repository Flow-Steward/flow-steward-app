import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { AppShellHeader } from "@/components/layout/app-shell-header";
import type { AwaitingApprovalJob } from "@/components/layout/app-shell-helpers";

function renderDrawer(approvalJobs: AwaitingApprovalJob[]) {
  render(
    <MemoryRouter>
      <AppShellHeader
        approvalError=""
        approvalJobs={approvalJobs}
        approvalLoaded
        approvalLoading={false}
        approvalsOpen
        breadcrumbs={[]}
        onOpenApprovalJob={vi.fn()}
        onOpenSearch={vi.fn()}
        onRetryApprovals={vi.fn()}
        onToggleApprovals={vi.fn()}
        onToggleRail={vi.fn()}
        pendingApprovals={approvalJobs.length}
        railExpanded
      />
    </MemoryRouter>,
  );
}

it("lists a waiting job as before when the row says nothing about who decides", () => {
  renderDrawer([{ id: "job-1", task_summary: "Invoice 42" }]);
  expect(screen.getByTestId("approval-job-link")).toBeInTheDocument();
  expect(screen.queryByTestId("approval-job-waiting-for")).not.toBeInTheDocument();
});

it("says who a waiting job is assigned to when the person may not decide it", () => {
  renderDrawer([
    { id: "job-1", task_summary: "Invoice 42", can_decide: false, approvers: [{ username: "ann", display_name: "Ann Example" }] },
    { id: "job-2", task_summary: "Invoice 43", can_decide: true, approvers: [] },
  ]);
  const waiting = screen.getAllByTestId("approval-job-waiting-for");
  expect(waiting).toHaveLength(1);
  expect(waiting[0]).toHaveTextContent("Waiting for Ann Example to decide.");
});
