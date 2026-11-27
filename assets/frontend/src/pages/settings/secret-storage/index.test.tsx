import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { SettingsSecretStoragePage } from "@/pages/settings/secret-storage";
import {
  checkSecretStorageConfig,
  createSecretStorageConfig,
  fetchSecretStorage,
  updateSecretStorageConfig,
} from "@/pages/settings/secret-storage/api";
import type * as StorageApi from "@/pages/settings/secret-storage/api";

vi.mock("@/pages/settings/secret-storage/api", async (importOriginal) => {
  const actual = await importOriginal<typeof StorageApi>();
  return {
    ...actual,
    fetchSecretStorage: vi.fn(),
    createSecretStorageConfig: vi.fn(),
    updateSecretStorageConfig: vi.fn(),
    checkSecretStorageConfig: vi.fn(),
  };
});

const vaultKind = {
  kind: "acme.vault",
  label: "Company vault",
  feature: "acme.vault",
  capabilities: ["write"],
  entitled: true,
  config_fields: [
    { key: "vault", type: "text", label: "Vault address", required: true, help: "As shown in the vault." },
    {
      key: "region",
      type: "select",
      label: "Region",
      options: [
        { value: "eu", label: "Europe" },
        { value: "us", label: "United States" },
      ],
      default: "eu",
    },
    { key: "us_zone", type: "text", label: "US zone", visible_when: { field: "region", contains: "us" } },
    { key: "timeout", type: "number", label: "Wait for an answer (seconds)", advanced: true, default: 5 },
  ],
};
const mainVault = {
  id: "c1",
  backend: "acme.vault",
  backend_label: "Company vault",
  name: "Main vault",
  config: { vault: "https://vault.example", region: "eu", timeout: 5 },
  enabled: true,
  installed: true,
};

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <SettingsSecretStoragePage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("SettingsSecretStoragePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(checkSecretStorageConfig).mockResolvedValue({ config_id: "c1", healthy: true, code: "" });
    vi.mocked(createSecretStorageConfig).mockResolvedValue({ ...mainVault, id: "c2", name: "Second vault" });
    vi.mocked(updateSecretStorageConfig).mockResolvedValue({ ...mainVault, name: "Prod vault" });
  });

  it("explains that external storage is an add-on and shows nothing else without one", async () => {
    vi.mocked(fetchSecretStorage).mockResolvedValue({ backends: [], configs: [], secrets: [] });
    renderPage();

    await waitFor(() => expect(screen.getByText(/Keeping them in an external secret manager is an add-on/)).toBeInTheDocument());
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add storage" })).not.toBeInTheDocument();
  });

  it("lists the account's storages with their kind, state and connection, and checks again", async () => {
    vi.mocked(fetchSecretStorage).mockResolvedValue({ backends: [vaultKind], configs: [mainVault], secrets: [] });
    const user = userEvent.setup();
    renderPage();

    const table = await screen.findByRole("table", { name: "Secret storage" });
    const row = within(table).getByRole("row", { name: /Main vault/ });
    expect(within(row).getByText("Company vault")).toBeInTheDocument();
    // The kind id is never shown.
    expect(within(row).queryByText("acme.vault")).not.toBeInTheDocument();
    expect(within(row).getByText("On")).toBeInTheDocument();
    await waitFor(() => expect(within(row).getByText("Connected")).toBeInTheDocument());

    vi.mocked(checkSecretStorageConfig).mockResolvedValue({ config_id: "c1", healthy: false, code: "unreachable" });
    await user.click(within(row).getByRole("button", { name: "Check connection" }));
    await waitFor(() => expect(within(row).getByText("Cannot connect")).toBeInTheDocument());
    expect(within(row).queryByText("unreachable")).not.toBeInTheDocument();
    expect(checkSecretStorageConfig).toHaveBeenCalledTimes(2);
  });

  it("adds a storage with the add-on's form: one kind shown as text, advanced settings folded", async () => {
    vi.mocked(fetchSecretStorage).mockResolvedValue({ backends: [vaultKind], configs: [], secrets: [] });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Add storage" }));
    const form = screen.getByRole("form", { name: "Add storage" });
    const kind = within(form).getByLabelText("Kind of storage");
    expect(kind).toHaveValue("Company vault");
    expect(kind).toHaveAttribute("readonly");
    expect(within(form).getByText("As shown in the vault.")).toBeInTheDocument();
    // Shown only for the region it belongs to.
    expect(within(form).queryByLabelText("US zone")).not.toBeInTheDocument();
    expect(within(form).getByText("Advanced settings")).toBeInTheDocument();

    await user.type(within(form).getByRole("textbox", { name: /Name/ }), "Second vault");
    await user.type(within(form).getByRole("textbox", { name: /Vault address/ }), "https://vault.example");
    await user.click(within(form).getByRole("button", { name: "Add storage" }));

    await waitFor(() =>
      expect(createSecretStorageConfig).toHaveBeenCalledWith({
        backend: "acme.vault",
        name: "Second vault",
        enabled: true,
        config: { vault: "https://vault.example", region: "eu", timeout: 5 },
      }),
    );
    await waitFor(() => expect(screen.getByText("Second vault added.")).toBeInTheDocument());
  });

  it("refuses to add a storage without its required settings", async () => {
    vi.mocked(fetchSecretStorage).mockResolvedValue({ backends: [vaultKind], configs: [], secrets: [] });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Add storage" }));
    const form = screen.getByRole("form", { name: "Add storage" });
    await user.type(within(form).getByRole("textbox", { name: /Name/ }), "Second vault");
    await user.click(within(form).getByRole("button", { name: "Add storage" }));

    await waitFor(() => expect(within(form).getByText("Vault address is required.")).toBeInTheDocument());
    expect(createSecretStorageConfig).not.toHaveBeenCalled();
  });

  it("renames a storage without resending settings it did not change", async () => {
    vi.mocked(fetchSecretStorage).mockResolvedValue({ backends: [vaultKind], configs: [mainVault], secrets: [] });
    const user = userEvent.setup();
    renderPage();

    const table = await screen.findByRole("table", { name: "Secret storage" });
    await user.click(within(table).getByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Edit Main vault" });
    expect(within(form).queryByLabelText("Kind of storage")).not.toBeInTheDocument();
    const name = within(form).getByRole("textbox", { name: /Name/ });
    await user.clear(name);
    await user.type(name, "Prod vault");
    await user.click(within(form).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(updateSecretStorageConfig).toHaveBeenCalledWith("c1", { name: "Prod vault", enabled: true }));
  });

  it("does not offer to add a storage the account is not entitled to", async () => {
    vi.mocked(fetchSecretStorage).mockResolvedValue({
      backends: [{ ...vaultKind, entitled: false }],
      configs: [],
      secrets: [],
    });
    renderPage();

    await waitFor(() => expect(screen.getByRole("button", { name: "Add storage" })).toBeDisabled());
    expect(screen.getByText("The installed secret storage add-on is not available for this account.")).toBeInTheDocument();
  });
});
