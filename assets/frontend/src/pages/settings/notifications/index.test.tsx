import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SettingsNotificationsPage } from "@/pages/settings/notifications";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { WORKSPACE_SELECTED_EVENT } from "@/lib/utils/workspace-selection";

vi.mock("@/pages/projects/list/api", () => ({ fetchProjectsList: vi.fn() }));
vi.mock("@/pages/projects/notifications", () => ({
  ProjectNotificationRoutesEditor: ({ projectId }: { projectId: string }) => (
    <div>routes:{projectId}</div>
  ),
}));

const mockedFetchProjectsList = vi.mocked(fetchProjectsList);

it("refetches notification project scope when the workspace changes", async () => {
  mockedFetchProjectsList
    .mockResolvedValueOnce({
      projects: [],
      page: 1,
      per_page: 200,
      total: 0,
      total_pages: 0,
    })
    .mockResolvedValueOnce({
      projects: [{ id: "project-default", name: "Default project" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <SettingsNotificationsPage />
    </QueryClientProvider>,
  );

  await waitFor(() => expect(mockedFetchProjectsList).toHaveBeenCalledTimes(1));
  globalThis.dispatchEvent(
    new CustomEvent(WORKSPACE_SELECTED_EVENT, {
      detail: { workspaceId: "workspace-default" },
    }),
  );

  await waitFor(() =>
    expect(mockedFetchProjectsList).toHaveBeenLastCalledWith({
      page: 1,
      perPage: 200,
      search: "",
      workspaceId: "workspace-default",
      includeAllWorkspaces: false,
    }),
  );
  // The subject of this test is the refetch. The editor stays closed until a
  // project is chosen, so assert that rather than the old implicit selection.
  expect(
    await screen.findByText(/select a project to configure/i),
  ).toBeInTheDocument();
  expect(screen.queryByText("routes:project-default")).not.toBeInTheDocument();
});

it("does not open notification routing for a project nobody chose", async () => {
  // A5-012: effectiveProjectId fell back to projects[0].id, so the routing
  // editor opened for the first project and the select displayed it as if it
  // had been picked. An operator could change the wrong project's routing.
  mockedFetchProjectsList.mockResolvedValue({
    projects: [
      { id: "project-first", name: "First project" },
      { id: "project-second", name: "Second project" },
    ],
    page: 1,
    per_page: 200,
    total: 2,
    total_pages: 1,
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <SettingsNotificationsPage />
    </QueryClientProvider>,
  );

  const user = userEvent.setup();
  const scope = await screen.findByRole("combobox", { name: "Project scope" });
  await waitFor(() => expect(scope).toBeEnabled());
  await user.click(scope);
  expect(await screen.findByRole("option", { name: "First project" })).toBeInTheDocument();
  expect(screen.queryByText("routes:project-first")).not.toBeInTheDocument();
  // Nothing is chosen: the placeholder option is the selected one.
  expect(screen.getByRole("option", { name: "Select a project" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByText(/select a project to configure/i)).toBeInTheDocument();
});

it("opens routing for the project the operator picks", async () => {
  mockedFetchProjectsList.mockResolvedValue({
    projects: [
      { id: "project-first", name: "First project" },
      { id: "project-second", name: "Second project" },
    ],
    page: 1,
    per_page: 200,
    total: 2,
    total_pages: 1,
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={queryClient}>
      <SettingsNotificationsPage />
    </QueryClientProvider>,
  );

  const scope = await screen.findByRole("combobox", { name: "Project scope" });
  await waitFor(() => expect(scope).toBeEnabled());
  await user.click(scope);
  await user.click(await screen.findByRole("option", { name: "Second project" }));

  expect(await screen.findByText("routes:project-second")).toBeInTheDocument();
});
