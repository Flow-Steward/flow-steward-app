import type * as SlotApi from "@/pages/workflows/shared/connection-slot-api";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { WorkflowConnectionSlotsPanel } from "@/pages/workflows/overview/workflow-connection-slots-panel";
import {
  declaresConnectionSlots,
  fetchWorkflowConnectionSlots,
  saveWorkflowConnectionSlot,
  type WorkflowConnectionSlots,
} from "@/pages/workflows/shared/connection-slot-api";

vi.mock("@/pages/workflows/shared/connection-slot-api", async (importOriginal) => ({
  ...(await importOriginal<typeof SlotApi>()),
  fetchWorkflowConnectionSlots: vi.fn(),
  saveWorkflowConnectionSlot: vi.fn(),
}));

const mockedFetch = vi.mocked(fetchWorkflowConnectionSlots);
const mockedSave = vi.mocked(saveWorkflowConnectionSlot);

function slots(overrides: Partial<WorkflowConnectionSlots["slots"][number]> = {}): WorkflowConnectionSlots {
  const slot = {
    slot: "store",
    label: "Store account",
    extension_id: "acme-store",
    connection_type_ids: ["api_token"],
    connection_id: null,
    choice: "none" as const,
    ready: false,
    reason: "This workflow needs a 'Store account' connection and the project has 2. Choose the one it uses.",
    options: [
      { connection_id: "c-1", display_name: "Main store", connection_type: "api_token" },
      { connection_id: "c-2", display_name: "Outlet", connection_type: "api_token" },
    ],
    ...overrides,
  };
  return { workflow_id: "extension.inst.sync", project_id: "p-1", ready: slot.ready, slots: [slot] };
}

function renderPanel(projects = [{ id: "p-1", name: "Shop" }]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <WorkflowConnectionSlotsPanel
          onProjectChange={vi.fn()}
          projectId="p-1"
          projects={projects as never}
          projectsError={null}
          projectsPending={false}
          workflowId="extension.inst.sync"
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("WorkflowConnectionSlotsPanel", () => {
  beforeEach(() => {
    mockedFetch.mockReset();
    mockedSave.mockReset();
  });

  it("says what the workflow needs and why it is not ready", async () => {
    mockedFetch.mockResolvedValue(slots());
    renderPanel();

    await waitFor(() => expect(screen.getByText("This workflow needs: Store account")).toBeInTheDocument());
    expect(screen.getByText(/the project has 2\. Choose the one it uses\./)).toBeInTheDocument();
    expect(screen.getByText("Not ready")).toBeInTheDocument();
    expect(screen.getByText("This workflow is not ready in this project")).toBeInTheDocument();
    expect(mockedFetch).toHaveBeenCalledWith("p-1", "extension.inst.sync");
  });

  it("shows a connection chosen automatically", async () => {
    mockedFetch.mockResolvedValue(
      slots({ connection_id: "c-1", choice: "automatic", ready: true, reason: "", options: [{ connection_id: "c-1", display_name: "Main store" }] }),
    );
    renderPanel();

    await waitFor(() => expect(screen.getByText("Chosen automatically")).toBeInTheDocument());
    expect(screen.getByRole("combobox", { name: /Connection for Store account/ })).toHaveTextContent("Main store");
    expect(screen.queryByText("This workflow is not ready in this project")).not.toBeInTheDocument();
  });

  it("saves the connection a person picks", async () => {
    mockedFetch.mockResolvedValue(slots());
    mockedSave.mockResolvedValue(slots({ connection_id: "c-2", choice: "chosen", ready: true, reason: "" }));
    renderPanel();

    fireEvent.click(await screen.findByRole("combobox", { name: /Connection for Store account/ }));
    fireEvent.click(await screen.findByRole("option", { name: /Outlet/ }));

    await waitFor(() => expect(mockedSave).toHaveBeenCalledWith("p-1", "extension.inst.sync", "store", "c-2"));
    await waitFor(() => expect(screen.getByText("Chosen")).toBeInTheDocument());
  });

  it("detects a declaration in the workflow settings", () => {
    expect(declaresConnectionSlots({ connection_slots: [{ slot: "store" }] })).toBe(true);
    expect(declaresConnectionSlots({ connection_slots: [] })).toBe(false);
    expect(declaresConnectionSlots(undefined)).toBe(false);
  });
});
