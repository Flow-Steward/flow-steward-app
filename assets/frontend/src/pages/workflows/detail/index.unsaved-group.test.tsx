import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { fetchSpy, mockedFetchWorkflowDetail, mockedFindWorkflowLinkedProjectId, renderPage, workflowDetailFixture } from "./index.test-helpers";

describe("group form Test Run guard", () => {
  it.each(["dirty", "saving", "error"])("retains a %s group form across Close and blocks workflow Test Run", async (state) => {
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5,
      groups: [{ group_id: "items", display_name: "Items", body: { kind: "sequence" } }],
      steps: [{ step_id: "done", step_kind: "stop_success" }],
    }));
    renderPage({ surface: "builder" });
    await waitFor(() => expect(window.openGroupDrawer).toBeTypeOf("function"));
    act(() => window.openGroupDrawer?.("items"));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue("Items"));
    // Save group rests until something changes, so every state starts from an edit.
    const expectedName = { dirty: "Unsaved group", saving: "Saving group name", error: "Failed group name" }[state] || "Unsaved group";
    fireEvent.change(screen.getByRole("textbox", { name: "Display name" }), { target: { value: expectedName } });
    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue(expectedName);
    if (state !== "dirty") {
      const fallback = fetchSpy.getMockImplementation()!;
      fetchSpy.mockImplementation((input, init) => {
        if (String(input).endsWith("/changes") && init?.method === "POST") {
          if (state === "saving") return new Promise<Response>(() => {});
          return Promise.resolve(new Response(JSON.stringify({ detail: "Group save failed" }), { status: 500, headers: { "content-type": "application/json" } }));
        }
        return fallback(input, init);
      });
      fireEvent.click(screen.getByRole("button", { name: "Save group" }));
      if (state === "error") await screen.findByText("Group save failed");
    }
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    act(() => window.openGroupDrawer?.("items"));
    expect(await screen.findByRole("textbox", { name: "Display name" })).toHaveValue(expectedName);
    fireEvent.click(screen.getByRole("link", { name: "Test Run" }));
    expect(await screen.findByTestId("workflow-test-run-blocked")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run workflow test run" })).toBeDisabled();
  }, 20_000);
});
