import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { ProjectConnectionsEditor } from "@/pages/projects/connections";
import { fetchProjectConnectionsList } from "@/pages/projects/connections/api";

vi.mock("@/pages/projects/connections/api", () => ({
  fetchProjectConnectionsList: vi.fn(),
}));

const mockedFetchProjectConnectionsList = vi.mocked(fetchProjectConnectionsList);

function renderEditor() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ProjectConnectionsEditor projectId="7" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ProjectConnectionsEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetchProjectConnectionsList.mockResolvedValue([]);
  });

  it("lists each connection read-only with a link to the extension that owns it", async () => {
    mockedFetchProjectConnectionsList.mockResolvedValue([
      {
        connection_id: "conn_shop",
        display_name: "Main shop",
        extension_id: "ext-shop",
        connection_kind: "extension",
        connection_type: "admin_api",
        environment: "production",
        health_status: "healthy",
        last_seen_ok_at: "2026-10-20T08:00:00Z",
        last_error: "Token expires soon",
        enabled: true,
      },
    ]);

    renderEditor();

    expect(await screen.findByText("Main shop")).toBeInTheDocument();
    expect(screen.getByText("ext-shop")).toBeInTheDocument();
    expect(screen.getByText("admin_api")).toBeInTheDocument();
    expect(screen.getByText("healthy")).toBeInTheDocument();
    expect(screen.getByText(/^last OK /)).toBeInTheDocument();
    expect(screen.getByText("Token expires soon")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the extension of Main shop" })).toHaveAttribute(
      "href",
      "/extensions/ext-shop?project_id=7",
    );
  });

  it("offers no way to create, edit or delete a connection here", async () => {
    mockedFetchProjectConnectionsList.mockResolvedValue([
      { connection_id: "conn_shop", display_name: "Main shop", extension_id: "ext-shop", enabled: true },
    ]);

    renderEditor();

    await screen.findByText("Main shop");
    expect(screen.queryByRole("button", { name: "Create new" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("links nowhere for a connection no extension owns", async () => {
    mockedFetchProjectConnectionsList.mockResolvedValue([
      { connection_id: "conn_mcp", display_name: "Docs connector", connection_kind: "mcp_connector", enabled: false },
    ]);

    renderEditor();

    expect(await screen.findByText("Docs connector")).toBeInTheDocument();
    expect(screen.getByText("disabled")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("says where connections come from when the project has none", async () => {
    renderEditor();

    expect(
      await screen.findByText("Connections appear here when an extension connects one for this project."),
    ).toBeInTheDocument();
  });

  it("shows a failed list request instead of the empty state", async () => {
    mockedFetchProjectConnectionsList.mockRejectedValue(new Error("Connections are unavailable"));

    renderEditor();

    expect(await screen.findByText("Connections are unavailable")).toBeInTheDocument();
    expect(
      screen.queryByText("Connections appear here when an extension connects one for this project."),
    ).not.toBeInTheDocument();
  });
});
