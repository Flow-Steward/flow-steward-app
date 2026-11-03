import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  catalogSecretsQueryKey: (projectId: string) => ["secrets", "project", projectId],
  fetchCatalogSecrets: vi.fn(),
  createCatalogSecret: vi.fn(),
}));

const mockedStatus = vi.mocked(fetchWorkflowSecretApproval);
const mockedApprove = vi.mocked(approveWorkflowSecretUse);
const mockedBind = vi.mocked(bindWorkflowInputSecret);
const mockedUnbind = vi.mocked(unbindWorkflowInputSecret);
const mockedSecrets = vi.mocked(fetchCatalogSecrets);
const mockedCreate = vi.mocked(createCatalogSecret);

function secret(id: string, name: string, owner: "project" | "workspace" = "project") {
  return { id, name, description: "", owner, workspace_id: "w1", project_id: owner === "project" ? "p1" : null };
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
    mockedSecrets.mockResolvedValue([secret("s1", "Shop token"), secret("s2", "Shared token", "workspace")]);
    mockedStatus.mockResolvedValue(status());
    mockedBind.mockResolvedValue(status({ secrets: [{ field: "api_token", secret_id: "s2" }] }));
    mockedUnbind.mockResolvedValue(status({ status: "not_required", secrets: [] }));
  });

  it("asks for a save first and requests nothing for an input the saved workflow does not declare", () => {
    renderWithClient(<WorkflowInputSecretBinding field="api_token" projectId="p1" saved={false} workflowId="wf1" />);

    expect(screen.getByText(/Save the workflow to choose a stored secret/)).toBeInTheDocument();
    expect(mockedStatus).not.toHaveBeenCalled();
    expect(mockedSecrets).not.toHaveBeenCalled();
  });

  it("shows the bound secret, binds another one and removes the binding through its own requests", async () => {
    renderWithClient(<WorkflowInputSecretBinding field="api_token" projectId="p1" saved workflowId="wf1" />);

    expect(await screen.findByText("Shop token")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Manage stored secrets/ })).toHaveAttribute("href", "/registry/secrets");

    await pick(/Stored secret for api_token/, /^Shared token/);
    fireEvent.click(screen.getByRole("button", { name: "Use instead" }));
    await waitFor(() => expect(mockedBind).toHaveBeenCalledWith("p1", "wf1", "api_token", "s2"));

    fireEvent.click(await screen.findByRole("button", { name: /Stop using the stored secret for api_token/ }));
    await waitFor(() => expect(mockedUnbind).toHaveBeenCalledWith("p1", "wf1", "api_token"));
  });

  it("creates a project secret in place and binds it", async () => {
    mockedStatus.mockResolvedValue(status({ status: "not_required", secrets: [] }));
    mockedCreate.mockResolvedValue(secret("s9", "New token"));
    renderWithClient(<WorkflowInputSecretBinding field="api_token" projectId="p1" saved workflowId="wf1" />);
    await waitFor(() => expect(mockedSecrets).toHaveBeenCalledWith("p1"));

    fireEvent.change(screen.getByLabelText("Secret name"), { target: { value: "New token" } });
    fireEvent.change(screen.getByLabelText("Value of the new secret for api_token"), { target: { value: "typed-value" } });
    const create = screen.getByRole("button", { name: "Create and use" });
    await waitFor(() => expect(create).toBeEnabled());
    fireEvent.click(create);

    await waitFor(() =>
      expect(mockedCreate).toHaveBeenCalledWith({ name: "New token", description: "", value: "typed-value", project_id: "p1" }),
    );
    await waitFor(() => expect(mockedBind).toHaveBeenCalledWith("p1", "wf1", "api_token", "s9"));
    expect(screen.getByLabelText("Value of the new secret for api_token")).toHaveValue("");
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
