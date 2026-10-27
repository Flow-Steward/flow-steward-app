import type { ReactNode } from "react";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { JsonCollectionWorkbench } from "@/components/shared/json-collection-workbench";
import { JsonRecordWorkbench } from "@/components/shared/json-record-workbench";

function renderWithQuery(ui: ReactNode, route = "/") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  return render(
    <MemoryRouter initialEntries={[route]}>
      <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("JSON workbench components", () => {
  it("saves selected collection entity via update callback", async () => {
    const fetchList = vi.fn(async () => [{ agent_id: "writer", display_name: "Writer" }]);
    const fetchDetail = vi.fn(async () => ({ agent_id: "writer", display_name: "Writer", enabled: false }));
    const updateItem = vi.fn(async () => ({ status: "ok" }));

    renderWithQuery(
      <JsonCollectionWorkbench
        description="Registry entities"
        eyebrow="Registry"
        fetchDetail={fetchDetail}
        fetchList={fetchList}
        idKey="agent_id"
        listQueryKey={["registry", "agents"]}
        title="Agents"
        updateItem={updateItem}
      />,
    );

    await screen.findByText("Writer");
    fireEvent.click(screen.getByRole("button", { name: /Writer/i }));

    await waitFor(() => {
      expect(fetchDetail).toHaveBeenCalledWith("writer");
    });

    await waitFor(() => {
      expect(screen.getByRole("textbox")).toHaveValue(
        JSON.stringify(
          {
            agent_id: "writer",
            display_name: "Writer",
            enabled: false,
          },
          null,
          2,
        ),
      );
    });
    fireEvent.click(screen.getByRole("button", { name: /Save selected/i }));

    await waitFor(() => {
      expect(updateItem).toHaveBeenCalledWith(
        "writer",
        expect.objectContaining({
          enabled: false,
        }),
      );
    });
  });

  it("hydrates selected collection entity from URL query", async () => {
    const fetchList = vi.fn(async () => [{ agent_id: "writer", display_name: "Writer" }]);
    const fetchDetail = vi.fn(async () => ({ agent_id: "writer", enabled: true }));

    renderWithQuery(
      <JsonCollectionWorkbench
        description="Registry entities"
        eyebrow="Registry"
        fetchDetail={fetchDetail}
        fetchList={fetchList}
        idKey="agent_id"
        listQueryKey={["registry", "agents"]}
        title="Agents"
      />,
      "/registry/agents?selected=writer",
    );

    await waitFor(() => {
      expect(fetchDetail).toHaveBeenCalledWith("writer");
    });
  });

  it("shows validation message for non-object JSON payloads", async () => {
    const createItem = vi.fn(async () => ({ status: "created" }));

    renderWithQuery(
      <JsonCollectionWorkbench
        createItem={createItem}
        description="Registry entities"
        eyebrow="Registry"
        fetchList={async () => []}
        idKey="agent_id"
        listQueryKey={["registry", "agents"]}
        title="Agents"
      />,
    );

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "[]" } });
    fireEvent.click(screen.getByRole("button", { name: /Create/i }));

    await screen.findByText("JSON payload must be an object.");
    expect(createItem).not.toHaveBeenCalled();
  });

  it("saves record payload through the save callback", async () => {
    const saveRecord = vi.fn(async () => ({ status: "ok" }));
    const payload = {
      display_name: "Admin Updated",
      timezone: "Europe/Vilnius",
    };

    renderWithQuery(
      <JsonRecordWorkbench
        description="Account settings"
        eyebrow="Settings"
        fetchRecord={async () => payload}
        queryKey={["settings", "account", "profile"]}
        saveRecord={saveRecord}
        title="Account settings"
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole("textbox")).toHaveValue(JSON.stringify(payload, null, 2));
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    await waitFor(() => {
      expect(saveRecord).toHaveBeenCalledWith(expect.objectContaining(payload));
    });
  });
});
