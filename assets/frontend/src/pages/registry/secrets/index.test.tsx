import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { fetchProjectsList } from "@/pages/projects/list/api";
import {
  createCatalogSecret,
  deleteCatalogSecret,
  fetchCatalogSecret,
  fetchCatalogSecrets,
  replaceCatalogSecretValue,
} from "@/pages/registry/secrets/api";
import { RegistrySecretsPage } from "@/pages/registry/secrets";

vi.mock("@/pages/projects/list/api", () => ({ fetchProjectsList: vi.fn() }));
vi.mock("@/pages/registry/secrets/api", () => ({
  fetchCatalogSecrets: vi.fn(),
  fetchCatalogSecret: vi.fn(),
  createCatalogSecret: vi.fn(),
  updateCatalogSecret: vi.fn(),
  replaceCatalogSecretValue: vi.fn(),
  deleteCatalogSecret: vi.fn(),
}));

const secret = {
  id: "s1",
  name: "Shop token",
  description: "",
  owner: "project" as const,
  workspace_id: "w1",
  project_id: "p1",
  usage_count: 1,
};

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <RegistrySecretsPage />
    </QueryClientProvider>,
  );
}

describe("RegistrySecretsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchProjectsList).mockResolvedValue({
      projects: [{ id: "p1", name: "Alpha", workspace_id: "w1" }],
      page: 1,
      per_page: 100,
      total: 1,
      total_pages: 1,
    });
    vi.mocked(fetchCatalogSecrets).mockResolvedValue([secret]);
    vi.mocked(createCatalogSecret).mockResolvedValue(secret);
    vi.mocked(replaceCatalogSecretValue).mockResolvedValue(secret);
  });

  it("creates a project secret and never shows a stored value", async () => {
    vi.mocked(fetchCatalogSecret).mockResolvedValue({ ...secret, usages: [] });
    renderPage();
    await screen.findByRole("option", { name: "Alpha" });
    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "p1" } });

    expect(await screen.findByText("Shop token")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Name"), { target: { value: "New key" } });
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "typed-value" } });
    fireEvent.click(screen.getByRole("button", { name: /Create secret/ }));

    await waitFor(() =>
      expect(createCatalogSecret).toHaveBeenCalledWith({
        name: "New key",
        description: "",
        value: "typed-value",
        project_id: "p1",
      }),
    );
    expect(screen.queryByDisplayValue("typed-value")).not.toBeInTheDocument();
  });

  it("blocks deleting a secret that is in use and replaces its value in one place", async () => {
    vi.mocked(fetchCatalogSecret).mockResolvedValue({
      ...secret,
      usages: [{ consumer_kind: "project_connection", connection_id: "conn_1", field: "api_token" }],
    });
    renderPage();
    await screen.findByRole("option", { name: "Alpha" });
    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "p1" } });

    expect(await screen.findByText(/conn_1 \/ api_token/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /In use/ })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("New value"), { target: { value: "rotated" } });
    fireEvent.click(screen.getByRole("button", { name: /Replace value/ }));

    await waitFor(() => expect(replaceCatalogSecretValue).toHaveBeenCalledWith("s1", "rotated"));
    expect(deleteCatalogSecret).not.toHaveBeenCalled();
  });
});
