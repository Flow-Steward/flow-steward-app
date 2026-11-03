import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { HttpError } from "@/lib/http/errors";
import {
  approveConnectionSecretUse,
  bindConnectionFieldSecret,
  fetchConnectionSecretApproval,
  unbindConnectionFieldSecret,
  type SecretApprovalStatus,
} from "@/pages/projects/connections/api";
import { createCatalogSecret, fetchCatalogSecrets } from "@/pages/registry/secrets/api";
import { ConnectionSecretApprovalStatus, ConnectionSecretFieldBinding } from "./extension-connection-secret-catalog";
import type { ManifestRenderContext } from "./extension-manifest-render-model";
import { renderManifestComponent } from "./extension-manifest-renderer";

vi.mock("@/pages/projects/connections/api", () => ({
  fetchConnectionSecretApproval: vi.fn(),
  approveConnectionSecretUse: vi.fn(),
  bindConnectionFieldSecret: vi.fn(),
  unbindConnectionFieldSecret: vi.fn(),
}));
vi.mock("@/pages/registry/secrets/api", () => ({
  catalogSecretsQueryKey: (projectId: string) => ["secrets", "project", projectId],
  fetchCatalogSecrets: vi.fn(),
  createCatalogSecret: vi.fn(),
}));

const mockedStatus = vi.mocked(fetchConnectionSecretApproval);
const mockedApprove = vi.mocked(approveConnectionSecretUse);
const mockedBind = vi.mocked(bindConnectionFieldSecret);
const mockedUnbind = vi.mocked(unbindConnectionFieldSecret);
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

describe("ConnectionSecretFieldBinding", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSecrets.mockResolvedValue([
      { id: "s1", name: "Shop token", description: "", owner: "project", workspace_id: "w1", project_id: "p1" },
      { id: "s2", name: "Shared token", description: "", owner: "workspace", workspace_id: "w1", project_id: null },
    ]);
    mockedBind.mockResolvedValue(approval());
    mockedUnbind.mockResolvedValue(approval({ status: "not_required", secrets: [] }));
  });

  it("asks for a saved connection before a stored secret can be chosen", () => {
    render(<ConnectionSecretFieldBinding field="api_token" fieldLabel="API token" projectId="p1" row={null} />);

    expect(screen.getByText(/After the connection is saved/)).toBeInTheDocument();
  });

  it("asks the catalog nothing until the person chooses to use an existing secret, then binds it", async () => {
    renderWithClient(<ConnectionSecretFieldBinding field="api_token" fieldLabel="API token" projectId="p1" row={unboundRow} />);
    expect(mockedSecrets).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Use an existing secret" }));
    const choose = await screen.findByRole("combobox", { name: "Stored secret for API token" });
    await waitFor(() => expect(choose).toBeEnabled());
    fireEvent.click(choose);
    fireEvent.click(await screen.findByRole("option", { name: /^Shared token/ }));
    fireEvent.click(screen.getByRole("button", { name: "Use this secret" }));

    await waitFor(() => expect(mockedBind).toHaveBeenCalledWith("p1", "c1", "api_token", "s2"));
    expect(screen.getByRole("link", { name: /Manage stored secrets/ })).toHaveAttribute("href", "/registry/secrets");
  });

  it("creates a project secret in place for a connection field and binds it", async () => {
    mockedCreate.mockResolvedValue({ id: "s9", name: "New token", description: "", owner: "project", workspace_id: "w1", project_id: "p1" });
    renderWithClient(<ConnectionSecretFieldBinding field="api_token" fieldLabel="API token" projectId="p1" row={unboundRow} />);
    fireEvent.click(screen.getByRole("button", { name: "Use an existing secret" }));

    fireEvent.change(await screen.findByLabelText("Secret name"), { target: { value: "New token" } });
    fireEvent.change(screen.getByLabelText("Value of the new secret for API token"), { target: { value: "typed-value" } });
    const create = screen.getByRole("button", { name: "Create and use" });
    await waitFor(() => expect(create).toBeEnabled());
    fireEvent.click(create);

    await waitFor(() =>
      expect(mockedCreate).toHaveBeenCalledWith({ name: "New token", description: "", value: "typed-value", project_id: "p1" }),
    );
    await waitFor(() => expect(mockedBind).toHaveBeenCalledWith("p1", "c1", "api_token", "s9"));
  });

  it("shows the bound secret and removes the binding", async () => {
    renderWithClient(<ConnectionSecretFieldBinding field="api_token" fieldLabel="API token" projectId="p1" row={boundRow} />);

    expect(await screen.findByText("Shop token")).toBeInTheDocument();
    expect(screen.getByText(/replaces this stored secret's value everywhere/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop using the stored secret for API token" }));

    await waitFor(() => expect(mockedUnbind).toHaveBeenCalledWith("p1", "c1", "api_token"));
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

    expect(await screen.findByText("stored secrets need approval")).toBeInTheDocument();
    expect(screen.getByText("api_token → Shop token")).toBeInTheDocument();
    expect(screen.getByText("https://api.example.test:443")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Approve use of secrets for Main shop" }));

    await waitFor(() => expect(mockedApprove).toHaveBeenCalledWith("p1", "c1", "sha256:one"));
  });

  it("shows an approved chip without an approve action", async () => {
    mockedStatus.mockResolvedValue(approval({ status: "approved" }));
    renderWithClient(<ConnectionSecretApprovalStatus projectId="p1" row={boundRow} />);

    expect(await screen.findByText("stored secrets approved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve use of secrets/ })).not.toBeInTheDocument();
  });

  it("reports a conflict and reloads the status when the connection changed", async () => {
    mockedStatus.mockResolvedValue(approval());
    mockedApprove.mockRejectedValue(new HttpError({ status: 409, url: "/api", payload: { detail: "changed" } }));
    renderWithClient(<ConnectionSecretApprovalStatus projectId="p1" row={boundRow} />);

    fireEvent.click(await screen.findByRole("button", { name: /Approve use of secrets/ }));

    expect(await screen.findByText("The connection changed after it was shown")).toBeInTheDocument();
    await waitFor(() => expect(mockedStatus).toHaveBeenCalledTimes(2));
  });
});

describe("connection form with stored secrets", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSecrets.mockResolvedValue([
      { id: "s1", name: "Shop token", description: "", owner: "project", workspace_id: "w1", project_id: "p1" },
    ]);
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

    expect(await screen.findByText("stored secrets need approval")).toBeInTheDocument();
    expect(screen.getByTestId("connection-secret-binding-api_token")).toBeInTheDocument();
    expect(screen.queryByTestId("connection-secret-binding-display_name")).not.toBeInTheDocument();
    expect(await screen.findByText("Shop token")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("API token"), { target: { value: "typed" } });
    expect(onFieldChange).toHaveBeenCalledWith("shop", "api_token", "typed");
  });
});
