import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { HttpError } from "@/lib/http/errors";
import {
  approveConnectionSecretUse,
  fetchConnectionSecretApproval,
  type SecretApprovalStatus,
} from "@/pages/projects/connections/api";
import { createCatalogSecret, fetchCatalogSecrets } from "@/pages/registry/secrets/api";
import { ConnectionSecretApprovalStatus, ConnectionStoredSecretField } from "./extension-connection-secret-catalog";
import type { ManifestRenderContext } from "./extension-manifest-render-model";
import { renderManifestComponent } from "./extension-manifest-renderer";

vi.mock("@/pages/projects/connections/api", () => ({
  fetchConnectionSecretApproval: vi.fn(),
  approveConnectionSecretUse: vi.fn(),
  bindConnectionFieldSecret: vi.fn(),
  unbindConnectionFieldSecret: vi.fn(),
}));
vi.mock("@/pages/registry/secrets/api", () => ({
  CATALOG_SECRETS_QUERY_KEY: ["secrets", "all"],
  fetchCatalogSecrets: vi.fn(),
  createCatalogSecret: vi.fn(),
}));

const mockedStatus = vi.mocked(fetchConnectionSecretApproval);
const mockedApprove = vi.mocked(approveConnectionSecretUse);
const mockedSecrets = vi.mocked(fetchCatalogSecrets);
const mockedCreate = vi.mocked(createCatalogSecret);

function approval(overrides: Partial<SecretApprovalStatus> = {}): SecretApprovalStatus {
  return {
    connection_id: "c1",
    status: "approval_required",
    fingerprint: "sha256:one",
    secrets: [{ field: "api_token", secret_id: "s1", name: "Shop token" }],
    destinations: [{ kind: "exact_host", scheme: "https", host: "api.example.test", port: 443 }],
    release: { release_id: "rel-1", descriptor_sha256: "abc" },
    changes: [],
    ...overrides,
  };
}

const unboundRow = { connection_id: "c1", display_name: "Main shop", secret_refs: { refs: {} } };
const boundRow = {
  connection_id: "c1",
  display_name: "Main shop",
  secret_refs: { refs: { api_token: "catalog/ref" }, catalog: { api_token: "s1" } },
};

function renderWithClient(node: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{node}</QueryClientProvider>);
}

describe("ConnectionStoredSecretField", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSecrets.mockResolvedValue([
      { id: "s1", name: "Shop token", description: "" },
      { id: "s2", name: "Shared token", description: "Mail" },
    ]);
  });

  it("offers the stored secrets in the field itself, on a new connection too", async () => {
    const onChoose = vi.fn();
    renderWithClient(
      <ConnectionStoredSecretField boundSecretId="" choice={undefined} field="api_token" fieldLabel="API token" onChoose={onChoose}>
        <input aria-label="API token" />
      </ConnectionStoredSecretField>,
    );
    expect(screen.getByLabelText("API token")).toBeInTheDocument();
    const choose = screen.getByRole("combobox", { name: "Stored secret for API token" });
    await waitFor(() => expect(choose).toBeEnabled());
    fireEvent.click(choose);
    // A long list is searchable: typing narrows it to the matching secrets.
    fireEvent.change(await screen.findByLabelText("Stored secret for API token Search"), { target: { value: "shared" } });
    await waitFor(() => expect(screen.queryByRole("option", { name: /^Shop token/ })).not.toBeInTheDocument());
    fireEvent.click(await screen.findByRole("option", { name: /^Shared token/ }));
    expect(onChoose).toHaveBeenCalledWith("s2");
  });

  it("hides the typed value while a stored secret is chosen and says it applies on save", async () => {
    const onChoose = vi.fn();
    renderWithClient(
      <ConnectionStoredSecretField boundSecretId="" choice="s1" field="api_token" fieldLabel="API token" onChoose={onChoose}>
        <input aria-label="API token" />
      </ConnectionStoredSecretField>,
    );
    expect(screen.queryByLabelText("API token")).not.toBeInTheDocument();
    expect(screen.getByText("Applied when you save the connection.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop using the stored secret for API token" }));
    expect(onChoose).toHaveBeenCalledWith("");
  });

  it("creates a secret in the standard dialog and chooses it", async () => {
    mockedCreate.mockResolvedValue({ id: "s9", name: "New token", description: "" });
    const onChoose = vi.fn();
    renderWithClient(
      <ConnectionStoredSecretField boundSecretId="" choice={undefined} field="api_token" fieldLabel="API token" onChoose={onChoose}>
        <input aria-label="API token" />
      </ConnectionStoredSecretField>,
    );
    fireEvent.click(screen.getByRole("button", { name: "New secret for API token" }));
    const dialog = await screen.findByRole("dialog", { name: "New secret" });
    fireEvent.change(within(dialog).getByLabelText("Value"), { target: { value: "typed-value" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create secret" }));

    await waitFor(() =>
      expect(mockedCreate).toHaveBeenCalledWith({ name: "API token", description: "", value: "typed-value" }),
    );
    await waitFor(() => expect(onChoose).toHaveBeenCalledWith("s9"));
  });
});

describe("ConnectionSecretApprovalStatus", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing and asks nothing for a connection that uses no stored secret", () => {
    const { container } = render(<ConnectionSecretApprovalStatus projectId="p1" row={unboundRow} />);

    expect(container).toBeEmptyDOMElement();
    expect(mockedStatus).not.toHaveBeenCalled();
  });

  it("shows the approval chip and approves exactly the fingerprint shown", async () => {
    mockedStatus.mockResolvedValue(approval());
    mockedApprove.mockResolvedValue(approval({ status: "approved" }));
    renderWithClient(<ConnectionSecretApprovalStatus projectId="p1" row={boundRow} />);

    await waitFor(() => expect(screen.getByText("stored secrets need approval")).toBeInTheDocument());
    expect(screen.getByText("api_token → Shop token")).toBeInTheDocument();
    expect(screen.getByText("https://api.example.test:443")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Approve use of secrets for Main shop" }));

    await waitFor(() => expect(mockedApprove).toHaveBeenCalledWith("p1", "c1", "sha256:one"));
  });

  it("shows an approved chip without an approve action", async () => {
    mockedStatus.mockResolvedValue(approval({ status: "approved" }));
    renderWithClient(<ConnectionSecretApprovalStatus projectId="p1" row={boundRow} />);

    await waitFor(() => expect(screen.getByText("stored secrets approved")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Approve use of secrets/ })).not.toBeInTheDocument();
  });

  it("reports a conflict and reloads the status when the connection changed", async () => {
    mockedStatus.mockResolvedValue(approval());
    mockedApprove.mockRejectedValue(new HttpError({ status: 409, url: "/api", payload: { detail: "changed" } }));
    renderWithClient(<ConnectionSecretApprovalStatus projectId="p1" row={boundRow} />);

    fireEvent.click(await screen.findByRole("button", { name: /Approve use of secrets/ }));

    await waitFor(() => expect(screen.getByText("The connection changed after it was shown")).toBeInTheDocument());
    await waitFor(() => expect(mockedStatus).toHaveBeenCalledTimes(2));
  });
});

describe("connection form with stored secrets", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSecrets.mockResolvedValue([{ id: "s1", name: "Shop token", description: "" }]);
    mockedStatus.mockResolvedValue(approval());
  });

  it("offers a stored secret beside a credential field, keeps typing working and shows the card's approval", async () => {
    const onFieldChange = vi.fn();
    const row = { ...boundRow, extension_id: "fixture.shop", connection_type: "shop_api" };
    const context = {
      busy: false, canManageProject: true, connectionCreationBlockedReason: "", callbacks: [], callbackStatuses: {},
      componentErrors: {}, componentQueryData: {}, componentQueryParams: {}, extensionId: "fixture.shop",
      extensionInstallId: "", extensionReleaseId: "", extensionDescriptorSha256: "",
      formValuesByComponent: { shop: { connection_id: "c1" } }, oauthConnectionTypes: [], projectConnections: [row],
      projectId: "p1", projectState: [], onCallbackSetup: vi.fn(), onComponentQueryParamsChange: vi.fn(),
      onConnectionDelete: vi.fn(), onConnectionOAuth: vi.fn(), onConnectionOAuthDisconnect: vi.fn(), onConnectionSave: vi.fn(),
      onConnectionSelect: vi.fn(), onFieldChange, onExternalResourcePickerBootstrap: async () => ({}), onManifestAction: vi.fn(),
      onProjectStateDelete: vi.fn(), onProjectStateSave: vi.fn(), onProjectStateSelect: vi.fn(),
    } satisfies ManifestRenderContext;
    renderWithClient(
      <>
        {renderManifestComponent(
          {
            component_id: "shop",
            type: "connection_form",
            title: "Shop connections",
            data: { connection_type: "shop_api", secret_fields: ["api_token"] },
            fields: [
              { path: "display_name", label: "Display name", widget: "text" },
              { path: "api_token", label: "API token", widget: "secret" },
            ],
          },
          context,
        )}
      </>,
    );

    await waitFor(() => expect(screen.getByText("stored secrets need approval")).toBeInTheDocument());
    expect(screen.getByTestId("connection-secret-binding-api_token")).toBeInTheDocument();
    expect(screen.queryByTestId("connection-secret-binding-display_name")).not.toBeInTheDocument();
    // The bound secret is shown as the field's choice; switching to typing is a form change.
    await waitFor(() => expect(screen.getByText("Shop token")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Stop using the stored secret for API token" }));
    expect(onFieldChange).toHaveBeenCalledWith("shop", "__stored_secret__:api_token", "");
  });

  it("types a value into a credential field that uses no stored secret", () => {
    const onFieldChange = vi.fn();
    const row = { ...unboundRow, extension_id: "fixture.shop", connection_type: "shop_api" };
    const context = {
      busy: false, canManageProject: true, connectionCreationBlockedReason: "", callbacks: [], callbackStatuses: {},
      componentErrors: {}, componentQueryData: {}, componentQueryParams: {}, extensionId: "fixture.shop",
      extensionInstallId: "", extensionReleaseId: "", extensionDescriptorSha256: "",
      formValuesByComponent: { shop: { connection_id: "c1" } }, oauthConnectionTypes: [], projectConnections: [row],
      projectId: "p1", projectState: [], onCallbackSetup: vi.fn(), onComponentQueryParamsChange: vi.fn(),
      onConnectionDelete: vi.fn(), onConnectionOAuth: vi.fn(), onConnectionOAuthDisconnect: vi.fn(), onConnectionSave: vi.fn(),
      onConnectionSelect: vi.fn(), onFieldChange, onExternalResourcePickerBootstrap: async () => ({}), onManifestAction: vi.fn(),
      onProjectStateDelete: vi.fn(), onProjectStateSave: vi.fn(), onProjectStateSelect: vi.fn(),
    } satisfies ManifestRenderContext;
    renderWithClient(
      <>
        {renderManifestComponent(
          {
            component_id: "shop",
            type: "connection_form",
            title: "Shop connections",
            data: { connection_type: "shop_api", secret_fields: ["api_token"] },
            fields: [{ path: "api_token", label: "API token", widget: "secret" }],
          },
          context,
        )}
      </>,
    );
    fireEvent.change(screen.getByLabelText("API token"), { target: { value: "typed" } });
    expect(onFieldChange).toHaveBeenCalledWith("shop", "api_token", "typed");
  });
});
