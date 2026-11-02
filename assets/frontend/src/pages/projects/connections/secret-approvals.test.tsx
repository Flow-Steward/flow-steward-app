import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { approveProjectSecretUse, fetchProjectSecretApprovals } from "@/pages/projects/connections/api";
import { ProjectSecretApprovals } from "@/pages/projects/connections/secret-approvals";

vi.mock("@/pages/projects/connections/api", () => ({
  fetchProjectSecretApprovals: vi.fn(),
  approveProjectSecretUse: vi.fn(),
}));

const mockedFetch = vi.mocked(fetchProjectSecretApprovals);
const mockedApprove = vi.mocked(approveProjectSecretUse);

function pendingRow(connectionId: string, fingerprint: string) {
  return {
    connection_id: connectionId,
    display_name: `Shop ${connectionId}`,
    status: "approval_required" as const,
    fingerprint,
    secrets: [{ field: "api_token", secret_id: "s1", name: "Shop token" }],
    destinations: [{ kind: "exact_host", scheme: "https", host: "api.example.test", port: 443 }],
    release: { release_id: "rel-2", descriptor_sha256: "abcdef0123456789" },
    previous_release: { release_id: "rel-1", descriptor_sha256: "0000" },
    changes: ["release"],
  };
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProjectSecretApprovals projectId="7" />
    </QueryClientProvider>,
  );
}

describe("ProjectSecretApprovals", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedApprove.mockResolvedValue({ connections: [] });
  });

  it("renders nothing when no connection needs approval", async () => {
    mockedFetch.mockResolvedValue([]);
    const { container } = renderPanel();
    await waitFor(() => expect(mockedFetch).toHaveBeenCalled());
    expect(container.querySelector(".fs-card")).toBeNull();
  });

  it("shows what changed without values and approves exactly the fingerprint shown", async () => {
    mockedFetch.mockResolvedValue([pendingRow("c1", "sha256:one")]);
    renderPanel();

    expect(await screen.findByText("Shop c1")).toBeInTheDocument();
    expect(screen.getByText("api_token → Shop token")).toBeInTheDocument();
    expect(screen.getByText("https://api.example.test:443")).toBeInTheDocument();
    expect(screen.getByText(/rel-1.*→.*rel-2/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Approve use of secrets/ }));

    await waitFor(() =>
      expect(mockedApprove).toHaveBeenCalledWith("7", { c1: "sha256:one" }, "rel-2"),
    );
  });

  it("approves a release for the selected connections only", async () => {
    mockedFetch.mockResolvedValue([pendingRow("c1", "sha256:one"), pendingRow("c2", "sha256:two"), pendingRow("c3", "sha256:three")]);
    renderPanel();
    await screen.findByText("Shop c1");

    fireEvent.click(screen.getByRole("checkbox", { name: /Shop c1/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Shop c3/ }));
    fireEvent.click(screen.getByRole("button", { name: /Approve release for 2 selected connections/ }));

    await waitFor(() =>
      expect(mockedApprove).toHaveBeenCalledWith("7", { c1: "sha256:one", c3: "sha256:three" }, "rel-2"),
    );
  });
});
