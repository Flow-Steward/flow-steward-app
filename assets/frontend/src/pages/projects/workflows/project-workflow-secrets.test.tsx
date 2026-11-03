import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { HttpError } from "@/lib/http/errors";
import { fetchCatalogSecrets } from "@/pages/registry/secrets/api";
import {
  approveWorkflowSecretUse,
  bindWorkflowInputSecret,
  fetchWorkflowSecretApproval,
  type WorkflowSecretApprovalStatus,
} from "@/pages/workflows/shared/workflow-secret-catalog-api";

import { ProjectWorkflowSecrets } from "./project-workflow-secrets";

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

function status(overrides: Partial<WorkflowSecretApprovalStatus> = {}): WorkflowSecretApprovalStatus {
  return {
    workflow_id: "wf_one",
    secret_inputs: ["api_key"],
    status: "approval_required",
    fingerprint: "sha256:abc",
    secrets: [],
    destinations: [{ kind: "exact_host", scheme: "https", host: "api.partner.test", port: 443 }],
    dynamic_http_steps: [],
    approved_destinations: [],
    ...overrides,
  };
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProjectWorkflowSecrets
        projectId="p1"
        workflows={[
          { workflowId: "wf_one", displayName: "Send order" },
          { workflowId: "wf_plain", displayName: "No secrets" },
          { workflowId: "wf_unlinked", displayName: "Not saved yet" },
        ]}
      />
    </QueryClientProvider>,
  );
}

describe("ProjectWorkflowSecrets", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchCatalogSecrets).mockResolvedValue([
      { id: "s1", name: "Partner key", description: "", owner: "project", workspace_id: "w1", project_id: "p1" },
    ]);
    vi.mocked(fetchWorkflowSecretApproval).mockImplementation(async (_projectId, workflowId) => {
      if (workflowId === "wf_one") return status();
      if (workflowId === "wf_plain") return status({ workflow_id: "wf_plain", secret_inputs: [] });
      throw new HttpError({ status: 404, url: "/api", payload: { detail: "Workflow not found" } });
    });
  });

  it("binds and approves a linked workflow's secret input without the builder", async () => {
    vi.mocked(bindWorkflowInputSecret).mockResolvedValue(
      status({ secrets: [{ field: "api_key", secret_id: "s1" }] }),
    );
    vi.mocked(approveWorkflowSecretUse).mockResolvedValue(
      status({ status: "approved", secrets: [{ field: "api_key", secret_id: "s1" }] }),
    );
    renderPanel();

    const row = await screen.findByTestId("project-workflow-secrets-wf_one");
    expect(row).toHaveTextContent("Send order");
    // Only workflows with a saved secret input get a row.
    expect(screen.queryByTestId("project-workflow-secrets-wf_plain")).not.toBeInTheDocument();
    expect(screen.queryByTestId("project-workflow-secrets-wf_unlinked")).not.toBeInTheDocument();
    expect(await screen.findByText(/api\.partner\.test/)).toBeInTheDocument();

    expect(await screen.findByTestId("workflow-input-secret-binding-api_key")).toBeInTheDocument();
    const approve = await screen.findByRole("button", { name: /Approve/ });
    fireEvent.click(approve);
    await waitFor(() =>
      expect(approveWorkflowSecretUse).toHaveBeenCalledWith("p1", "wf_one", "sha256:abc", []),
    );
  });
});
