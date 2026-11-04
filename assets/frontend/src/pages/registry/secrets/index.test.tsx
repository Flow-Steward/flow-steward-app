import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import {
  createCatalogSecret,
  deleteCatalogSecret,
  fetchCatalogSecrets,
  fetchCatalogSecretDeletePlan,
  replaceCatalogSecretValue,
  updateCatalogSecret,
} from "@/pages/registry/secrets/api";
import { RegistrySecretsPage } from "@/pages/registry/secrets";

vi.mock("@/pages/registry/secrets/api", () => ({
  CATALOG_SECRETS_QUERY_KEY: ["secrets", "all"],
  fetchCatalogSecrets: vi.fn(),
  fetchCatalogSecretDeletePlan: vi.fn(),
  createCatalogSecret: vi.fn(),
  updateCatalogSecret: vi.fn(),
  replaceCatalogSecretValue: vi.fn(),
  deleteCatalogSecret: vi.fn(),
}));

const shopToken = {
  id: "s1",
  name: "Shop token",
  description: "Orders API",
  usage_count: 2,
};
const sharedKey = {
  id: "s2",
  name: "Mail key",
  description: "",
  usage_count: 0,
};

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <RegistrySecretsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

async function openRowAction(user: ReturnType<typeof userEvent.setup>, secretName: string, action: string) {
  await user.click(await screen.findByRole("button", { name: `Actions for ${secretName}` }));
  await user.click(screen.getByRole("menuitem", { name: action }));
}

describe("RegistrySecretsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchCatalogSecrets).mockResolvedValue([shopToken, sharedKey]);
    vi.mocked(createCatalogSecret).mockResolvedValue(shopToken);
    vi.mocked(updateCatalogSecret).mockResolvedValue(shopToken);
    vi.mocked(replaceCatalogSecretValue).mockResolvedValue(shopToken);
    vi.mocked(deleteCatalogSecret).mockResolvedValue({ deleted: true });
  });

  it("lists every secret of the installation with its use, and filters by use and search", async () => {
    const user = userEvent.setup();
    renderPage();
    const table = await screen.findByRole("table", { name: "Secrets" });
    const shopRow = await within(table).findByRole("row", { name: /Shop token/ });
    expect(within(shopRow).getByText("2 place(s)")).toBeInTheDocument();
    expect(within(table).getByText("Mail key")).toBeInTheDocument();
    // A secret belongs to the installation: no owner, no project to choose.
    expect(within(table).queryByRole("columnheader", { name: "Owner" })).not.toBeInTheDocument();

    const search = screen.getByRole("searchbox", { name: "Search secrets" });
    await user.type(search, "orders");
    expect(within(table).getByText("Shop token")).toBeInTheDocument();
    expect(within(table).queryByText("Mail key")).not.toBeInTheDocument();
    await user.clear(search);

    await user.click(screen.getByRole("button", { name: "Add filter" }));
    expect(screen.queryByRole("menuitem", { name: "Project" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "Use" }));
    await user.click(screen.getByRole("menuitem", { name: "Not used" }));
    expect(within(table).queryByText("Shop token")).not.toBeInTheDocument();
    expect(within(table).getByText("Mail key")).toBeInTheDocument();
  });

  it("creates a secret in a dialog and never shows the stored value again", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("table", { name: "Secrets" });
    await user.click(screen.getByRole("button", { name: "New secret" }));
    const dialog = await screen.findByRole("dialog", { name: "New secret" });
    await user.type(within(dialog).getByRole("textbox", { name: /Name/ }), "New key");
    await user.type(within(dialog).getByLabelText("Value"), "typed-value");
    expect(within(dialog).queryByRole("combobox")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Create secret" }));

    await waitFor(() =>
      expect(createCatalogSecret).toHaveBeenCalledWith({
        name: "New key",
        description: "",
        value: "typed-value",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "New secret" })).not.toBeInTheDocument());
    expect(screen.queryByDisplayValue("typed-value")).not.toBeInTheDocument();
  });

  it("refuses to create a secret without a value", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "New secret" }));
    const dialog = await screen.findByRole("dialog", { name: "New secret" });
    await user.type(within(dialog).getByRole("textbox", { name: /Name/ }), "New key");
    await user.click(within(dialog).getByRole("button", { name: "Create secret" }));
    expect(await within(dialog).findByText("Enter the value.")).toBeInTheDocument();
    expect(createCatalogSecret).not.toHaveBeenCalled();
  });

  it("edits the details and replaces the value from the row menu", async () => {
    const user = userEvent.setup();
    renderPage();
    await openRowAction(user, "Shop token", "Edit details");
    const edit = await screen.findByRole("dialog", { name: "Edit Shop token" });
    expect(within(edit).queryByLabelText("Value")).not.toBeInTheDocument();
    const name = within(edit).getByRole("textbox", { name: /Name/ });
    await user.clear(name);
    await user.type(name, "Shop API token");
    await user.click(within(edit).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(updateCatalogSecret).toHaveBeenCalledWith("s1", { name: "Shop API token", description: "Orders API" }),
    );

    await openRowAction(user, "Shop token", "Replace value");
    const replace = await screen.findByRole("dialog", { name: "Replace the value of Shop token" });
    await user.type(within(replace).getByLabelText("New value"), "rotated");
    await user.click(within(replace).getByRole("button", { name: "Replace value" }));
    await waitFor(() => expect(replaceCatalogSecretValue).toHaveBeenCalledWith("s1", "rotated"));
  });

  it("shows what uses a secret and deletes it with those uses only after confirmation", async () => {
    vi.mocked(fetchCatalogSecretDeletePlan).mockResolvedValue({
      secret: shopToken,
      revision: "rev-1",
      usages: [
        {
          consumer_kind: "project_connection",
          connection_id: "conn_1",
          connection_name: "Shop",
          project_id: "p1",
          project_name: "Alpha",
          field: "api_token",
        },
        {
          consumer_kind: "workflow_input",
          connection_id: null,
          workflow_id: "wf_one",
          workflow_name: "Send order",
          project_id: "p1",
          project_name: "Alpha",
          field: "api_key",
        },
      ],
    });
    const user = userEvent.setup();
    renderPage();
    await openRowAction(user, "Shop token", "Delete");
    const dialog = await screen.findByRole("alertdialog", { name: "Delete Shop token?" });
    expect(await within(dialog).findByRole("link", { name: "Connection Shop · api_token" })).toHaveAttribute(
      "href",
      "/projects/p1?tab=connections",
    );
    expect(within(dialog).getByRole("link", { name: "Workflow Send order · api_key" })).toHaveAttribute(
      "href",
      "/projects/p1?tab=workflows",
    );
    const confirm = within(dialog).getByRole("button", { name: "Delete and remove its uses" });
    expect(confirm).toBeDisabled();
    // The published checkbox hides its native input behind the mark; its label is the target.
    await user.click(within(dialog).getByText("Remove the secret from these places and delete it"));
    expect(within(dialog).getByRole("checkbox", { name: "Remove the secret from these places and delete it" })).toBeChecked();
    await user.click(confirm);
    await waitFor(() => expect(deleteCatalogSecret).toHaveBeenCalledWith("s1", "rev-1"));
  });

  it("deletes an unused secret after a plain confirmation", async () => {
    vi.mocked(fetchCatalogSecretDeletePlan).mockResolvedValue({ secret: sharedKey, revision: "rev-0", usages: [] });
    const user = userEvent.setup();
    renderPage();
    await openRowAction(user, "Mail key", "Delete");
    const dialog = await screen.findByRole("alertdialog", { name: "Delete Mail key?" });
    expect(await within(dialog).findByText("Nothing uses this secret.")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(deleteCatalogSecret).toHaveBeenCalledWith("s2", "rev-0"));
  });
});
