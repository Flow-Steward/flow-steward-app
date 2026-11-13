import { act, screen, waitFor } from "@testing-library/react";
import { mockedFetchWorkflowDetail, renderPage } from "./index.test-helpers";
import type * as PageShell from "./detail-page-shell";

const shellLoad = vi.hoisted(() => {
  let release: () => void = () => undefined;
  const promise = new Promise<void>((resolve) => { release = resolve; });
  return { promise, release, ready: false };
});

vi.mock("./detail-page-shell", async (importOriginal) => {
  const actual = await importOriginal<typeof PageShell>();
  return {
    ...actual,
    WorkflowDetailPageShell: (props: Parameters<typeof actual.WorkflowDetailPageShell>[0]) => {
      if (!shellLoad.ready) throw shellLoad.promise;
      return <actual.WorkflowDetailPageShell {...props} />;
    },
  };
});

it("loads workflow data while the editor presentation is suspended", async () => {
  const view = renderPage({ surface: "builder" });
  try {
    // A slow presentation chunk must not postpone the parent's query effects.
    await waitFor(() => expect(mockedFetchWorkflowDetail).toHaveBeenCalledWith("weekly_report"));
    await waitFor(() => expect(screen.getByText("Preparing the workflow")).toBeVisible());
    await act(async () => {
      shellLoad.ready = true;
      shellLoad.release();
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Add step" })).toBeVisible());
  } finally {
    await act(async () => {
      shellLoad.ready = true;
      shellLoad.release();
    });
    view.unmount();
  }
});
