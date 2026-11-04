import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { HttpError } from "@/lib/http/errors";
import { createCatalogSecret, fetchCatalogSecrets } from "@/pages/registry/secrets/api";
import {
  approveWorkflowSecretUse,
  bindWorkflowInputSecret,
  fetchWorkflowSecretApproval,
  unbindWorkflowInputSecret,
  type WorkflowSecretApprovalStatus,
} from "@/pages/workflows/shared/workflow-secret-catalog-api";
import { WorkflowInputSecretBinding, WorkflowSecretApprovalPanel } from "./detail-workflow-secret-catalog";

vi.mock("@/pages/workflows/shared/workflow-secret-catalog-api", () => ({
  fetchWorkflowSecretApproval: vi.fn(),
  approveWorkflowSecretUse: vi.fn(),
  bindWorkflowInputSecret: vi.fn(),
  unbindWorkflowInputSecret: vi.fn(),
  workflowSecretApprovalQueryKey: (projectId: string, workflowId: string, revision?: number | null) => [
    "workflows",
    workflowId,
    "secret-approval",
    projectId,
    revision ?? null,
  ],
}));
vi.mock("@/pages/registry/secrets/api", () => ({
  CATALOG_SECRETS_QUERY_KEY: ["secrets", "all"],
  fetchCatalogSecrets: vi.fn(),
  createCatalogSecret: vi.fn(),
}));

const mockedStatus = vi.mocked(fetchWorkflowSecretApproval);
const mockedApprove = vi.mocked(approveWorkflowSecretUse);
const mockedBind = vi.mocked(bindWorkflowInputSecret);
const mockedUnbind = vi.mocked(unbindWorkflowInputSecret);
const mockedSecrets = vi.mocked(fetchCatalogSecrets);
const mockedCreate = vi.mocked(createCatalogSecret);

function secret(id: string, name: string) {
  return { id, name, description: "" };
}

function status(overrides: Partial<WorkflowSecretApprovalStatus> = {}): WorkflowSecretApprovalStatus {
  return {
    workflow_id: "wf1",
    status: "approval_required",
    fingerprint: "sha256:one",
    secrets: [{ field: "api_token", secret_id: "s1" }],
    destinations: [{ kind: "exact_host", scheme: "https", host: "api.example.test", port: 443 }],
    dynamic_http_steps: [],
    approved_destinations: [],
    ...overrides,
  };
}

function renderWithClient(node: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{node}</QueryClientProvider>);
}

async function pick(name: RegExp, option: RegExp) {
  const trigger = screen.getByRole("combobox", { name });
  await waitFor(() => expect(trigger).toBeEnabled());
  fireEvent.click(trigger);
  fireEvent.click(await screen.findByRole("option", { name: option }));
}

describe("WorkflowInputSecretBinding", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSecrets.mockResolvedValue([secret("s1", "Shop token"), secret("s2", "Shared token")]);
    mockedStatus.mockResolvedValue(status());
    mockedBind.mockResolvedValue(status({ secrets: [{ field: "api_token", secret_id: "s2" }] }));
    mockedUnbind.mockResolvedValue(status({ status: "not_required", secrets: [] }));
  });

  it("asks for a save first when the editor cannot hold a pending choice", () => {
    renderWithClient(<WorkflowInputSecretBinding field="api_token" projectId="p1" saved={false} workflowId="wf1" />);

    expect(screen.getByText(/Save the workflow to choose a stored secret/)).toBeInTheDocument();
    expect(mockedStatus).not.toHaveBeenCalled();
    expect(mockedSecrets).not.toHaveBeenCalled();
  });

  it("keeps a stored secret chosen for a new input until the save binds it", async () => {
    const onPendingChange = vi.fn();
    const { rerender } = renderWithClient(
      <WorkflowInputSecretBinding field="api_token" onPendingChange={onPendingChange} pendingSecretId="" projectId="p1" saved={false} workflowId="wf1" />,
    );
    await pick(/Stored secret for api_token/, /^Shared token/);
    expect(onPendingChange).toHaveBeenCalledWith("s2");
    expect(mockedBind).not.toHaveBeenCalled();
    expect(mockedStatus).not.toHaveBeenCalled();

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const chosenId = "s2";
    rerender(
      <QueryClientProvider client={queryClient}>
        <WorkflowInputSecretBinding field="api_token" onPendingChange={onPendingChange} pendingSecretId={chosenId} projectId="p1" saved={false} workflowId="wf1" />
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/once you save the workflow settings/)).toBeInTheDocument();
  });

  it("shows the bound secret of a saved input, binds another one and removes the binding", async () => {
    renderWithClient(<WorkflowInputSecretBinding field="api_token" projectId="p1" saved workflowId="wf1" />);

    expect(await screen.findByText("Shop token")).toBeInTheDocument();
    await pick(/Stored secret for api_token/, /^Shared token/);
    await waitFor(() => expect(mockedBind).toHaveBeenCalledWith("p1", "wf1", "api_token", "s2"));

    fireEvent.click(await screen.findByRole("button", { name: /Stop using the stored secret for api_token/ }));
    await waitFor(() => expect(mockedUnbind).toHaveBeenCalledWith("p1", "wf1", "api_token"));
  });

  it("hides the typed value while a stored secret is bound, like a connection field", async () => {
    renderWithClient(
      <WorkflowInputSecretBinding field="api_token" projectId="p1" saved workflowId="wf1">
        <input aria-label="Typed default" />
      </WorkflowInputSecretBinding>,
    );
    expect(await screen.findByText("Shop token")).toBeInTheDocument();
    expect(screen.queryByLabelText("Typed default")).not.toBeInTheDocument();
  });

  it("shows the typed value next to the choice while nothing is bound", async () => {
    mockedStatus.mockResolvedValue(status({ status: "not_required", secrets: [] }));
    renderWithClient(
      <WorkflowInputSecretBinding field="api_token" projectId="p1" saved workflowId="wf1">
        <input aria-label="Typed default" />
      </WorkflowInputSecretBinding>,
    );
    expect(await screen.findByLabelText("Typed default")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Stored secret for api_token" })).toBeInTheDocument();
  });

  it("creates a secret in the standard dialog and binds it to a saved input", async () => {
    mockedStatus.mockResolvedValue(status({ status: "not_required", secrets: [] }));
    mockedCreate.mockResolvedValue(secret("s9", "New token"));
    renderWithClient(<WorkflowInputSecretBinding field="api_token" projectId="p1" saved workflowId="wf1" />);
    const newSecret = screen.getByRole("button", { name: "New secret for api_token" });
    // The picker waits for the binding status before it offers anything.
    await waitFor(() => expect(newSecret).toBeEnabled());
    fireEvent.click(newSecret);
    const dialog = await screen.findByRole("dialog", { name: "New secret" });
    fireEvent.change(within(dialog).getByLabelText("Value"), { target: { value: "typed-value" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create secret" }));

    await waitFor(() =>
      expect(mockedCreate).toHaveBeenCalledWith({ name: "api_token", description: "", value: "typed-value" }),
    );
    await waitFor(() => expect(mockedBind).toHaveBeenCalledWith("p1", "wf1", "api_token", "s9"));
  });
});

describe("WorkflowSecretApprovalPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSecrets.mockResolvedValue([secret("s1", "Shop token")]);
  });

  it("renders nothing and asks nothing when the saved workflow has no secret input", () => {
    const { container } = renderWithClient(<WorkflowSecretApprovalPanel hasSecretInputs={false} projectId="p1" workflowId="wf1" />);

    expect(container).toBeEmptyDOMElement();
    expect(mockedStatus).not.toHaveBeenCalled();
  });

  it("renders nothing when no stored secret is bound", async () => {
    mockedStatus.mockResolvedValue(status({ status: "not_required", secrets: [] }));
    renderWithClient(<WorkflowSecretApprovalPanel hasSecretInputs projectId="p1" workflowId="wf1" />);

    await waitFor(() => expect(mockedStatus).toHaveBeenCalledWith("p1", "wf1"));
    expect(screen.queryByTestId("workflow-secret-approval")).not.toBeInTheDocument();
  });

  it("offers approval for stored secrets a parent passes in when nothing is bound here", async () => {
    mockedStatus.mockResolvedValue(status({ status: "delegation_optional", secrets: [] }));
    mockedApprove.mockResolvedValue(status({ status: "approved", secrets: [] }));
    renderWithClient(<WorkflowSecretApprovalPanel hasSecretInputs projectId="p1" workflowId="wf1" />);

    expect(await screen.findByText("not approved for passed-in secrets")).toBeInTheDocument();
    expect(screen.getByText(/A parent workflow may pass it a stored secret/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve use of secrets" }));
    await waitFor(() => expect(mockedApprove).toHaveBeenCalledWith("p1", "wf1", "sha256:one", []));
  });

  it("names every connector receiver a person approves", async () => {
    mockedStatus.mockResolvedValue(
      status({
        receivers: [
          { step_id: "ship", kind: "extension", install_id: "pi_1", release_id: "rel_1", descriptor_sha256: "sha256:abcdef1234567890" },
          { step_id: "ask", kind: "mcp", tool_id: "partner_mcp", endpoint: "https://mcp.partner.test/mcp", transport: "streamable_http", remote_tool: "ask" },
          { step_id: "fmt", kind: "host_tool", tool_id: "format", implementation: "core.tools.format" },
          { step_id: "raw", kind: "unpinned", connector_id: "x" },
        ],
      }),
    );
    renderWithClient(<WorkflowSecretApprovalPanel hasSecretInputs projectId="p1" workflowId="wf1" />);

    expect(await screen.findByText("ship → extension release rel_1 · abcdef123456")).toBeInTheDocument();
    expect(screen.getByText("ask → MCP ask · https://mcp.partner.test/mcp (streamable_http)")).toBeInTheDocument();
    expect(screen.getByText("fmt → built-in tool format (core.tools.format)")).toBeInTheDocument();
    expect(screen.getByText(/Connector steps do not receive stored secrets/)).toBeInTheDocument();
  });

  it("approves exactly the fingerprint shown, with the literal destinations listed", async () => {
    mockedStatus.mockResolvedValue(status());
    mockedApprove.mockResolvedValue(status({ status: "approved", approved_destinations: status().destinations }));
    renderWithClient(<WorkflowSecretApprovalPanel hasSecretInputs projectId="p1" workflowId="wf1" />);

    expect(await screen.findByText("approval required")).toBeInTheDocument();
    expect(await screen.findByText("api_token → Shop token")).toBeInTheDocument();
    expect(screen.getAllByText("https://api.example.test:443").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "Approve use of secrets" }));

    await waitFor(() => expect(mockedApprove).toHaveBeenCalledWith("p1", "wf1", "sha256:one", []));
    expect(await screen.findByText("approved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve use of secrets" })).not.toBeInTheDocument();
  });

  it("requires explicit exact-host rules for HTTP steps with a computed URL", async () => {
    mockedStatus.mockResolvedValue(status({ destinations: [], dynamic_http_steps: ["call_api"] }));
    mockedApprove.mockResolvedValue(status({ status: "approved" }));
    renderWithClient(<WorkflowSecretApprovalPanel hasSecretInputs projectId="p1" workflowId="wf1" />);

    expect(await screen.findByText("call_api")).toBeInTheDocument();
    const approve = screen.getByRole("button", { name: "Approve use of secrets" });
    expect(approve).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Host of rule 1"), { target: { value: "api.example.com" } });
    fireEvent.change(screen.getByLabelText("Port of rule 1"), { target: { value: "8443" } });
    fireEvent.change(screen.getByLabelText("Path prefix of rule 1"), { target: { value: "/v1" } });
    fireEvent.click(screen.getByRole("button", { name: "Add destination" }));
    await pick(/Scheme of rule 2/, /^http$/);
    fireEvent.change(screen.getByLabelText("Host of rule 2"), { target: { value: "legacy.example.com" } });
    fireEvent.click(approve);

    await waitFor(() =>
      expect(mockedApprove).toHaveBeenCalledWith("p1", "wf1", "sha256:one", [
        { kind: "exact_host", scheme: "https", host: "api.example.com", port: 8443, path_prefix: "/v1" },
        { kind: "exact_host", scheme: "http", host: "legacy.example.com" },
      ]),
    );
  });

  it("shows a conflict and reloads the status when the workflow changed after it was shown", async () => {
    mockedStatus.mockResolvedValueOnce(status()).mockResolvedValue(status({ fingerprint: "sha256:two" }));
    mockedApprove.mockRejectedValue(new HttpError({ status: 409, url: "/api", payload: { detail: "changed" } }));
    renderWithClient(<WorkflowSecretApprovalPanel hasSecretInputs projectId="p1" workflowId="wf1" />);

    fireEvent.click(await screen.findByRole("button", { name: "Approve use of secrets" }));

    expect(await screen.findByText("The workflow changed after it was shown")).toBeInTheDocument();
    await waitFor(() => expect(mockedStatus).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve use of secrets" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Approve use of secrets" }));
    await waitFor(() => expect(mockedApprove).toHaveBeenLastCalledWith("p1", "wf1", "sha256:two", []));
  });

  it("warns that unsaved edits are not part of the approval", async () => {
    mockedStatus.mockResolvedValue(status());
    renderWithClient(<WorkflowSecretApprovalPanel hasSecretInputs projectId="p1" unsavedChanges workflowId="wf1" />);

    expect(await screen.findByText("Approval covers the saved workflow")).toBeInTheDocument();
  });
});
