import { act, fireEvent, screen } from "@testing-library/react";
import { fetchSpy, mockedFindWorkflowLinkedProjectId, renderPage } from "./index.test-helpers";

describe("workflow form Test Run guard", () => {
  it.each(["dirty", "saving", "error"])("blocks a workflow test while workflow settings are %s", async (state) => {
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
    renderPage({ surface: "builder" });
    await screen.findByRole("button", { name: "Add step" });
    act(() => { globalThis.dispatchEvent(new CustomEvent("platform:workflow-builder-action", { detail: { action: "workflow-settings" } })); });
    await screen.findByRole("textbox", { name: "Display name" });
    // Save workflow settings rests until a setting changes, so every state starts from an edit.
    fireEvent.change(screen.getByRole("textbox", { name: "Display name" }), { target: { value: "Unsaved workflow" } });
    if (state !== "dirty") {
      const fallback = fetchSpy.getMockImplementation()!;
      fetchSpy.mockImplementation((input, init) => {
        if (String(input) === "/api/workflows/weekly_report" && init?.method === "PATCH") {
          if (state === "saving") return new Promise<Response>(() => {});
          return Promise.resolve(new Response(JSON.stringify({ detail: "Save failed" }), { status: 500, headers: { "content-type": "application/json" } }));
        }
        return fallback(input, init);
      });
      fireEvent.click(screen.getByRole("button", { name: "Save workflow settings" }));
      if (state === "error") await screen.findByText("Save failed");
    }
    fireEvent.click(screen.getByRole("link", { name: "Test Run" }));
    expect(await screen.findByTestId("workflow-test-run-blocked")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run workflow test run" })).toBeDisabled();
  });
});
