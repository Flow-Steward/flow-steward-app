import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useParams } from "react-router";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

import { useAuth } from "@/lib/auth/useAuth";
import { ProjectHistoryPanel } from "@/pages/projects/history";
import {
  fetchProjectHistory,
  fetchProjectHistoryDiff,
  restoreProjectHistoryRevision,
} from "@/pages/projects/history/api";

vi.mock("@/lib/auth/useAuth", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/pages/projects/history/api", () => ({
  fetchProjectHistory: vi.fn(),
  fetchProjectHistoryDiff: vi.fn(),
  restoreProjectHistoryRevision: vi.fn(),
}));

const mockedUseAuth = vi.mocked(useAuth);
const mockedFetchProjectHistory = vi.mocked(fetchProjectHistory);
const mockedFetchProjectHistoryDiff = vi.mocked(fetchProjectHistoryDiff);
const mockedRestoreProjectHistoryRevision = vi.mocked(restoreProjectHistoryRevision);

function authSessionForRole(role: string) {
  return {
    authenticated: true,
    local_auth_enabled: true,
    bootstrap_required: false,
    external_auth_enabled: false,
    roles: [role],
    permissions: {},
    account_memberships: [],
    i18n: {
      locale: "en",
      fallback_locale: "en",
      supported_locales: [{ code: "en", label: "English", native_label: "English" }],
      messages: {},
    },
    feature_flags: {},
    environment: {
      edition: "community",
    },
    settings_pages: [],
    registered_pages: [],
    shell_navigation: {},
    user: {
      id: `${role}-id`,
      email: `${role}@example.com`,
      username: role,
      display_name: role,
      roles: [role],
      account_id: "default",
      account_ids: ["default"],
      account_memberships: [],
      timezone: "UTC",
    },
  };
}

/** The History tab as the project page mounts it: under `/projects/:projectId`. */
function HistoryTab() {
  const { projectId = "" } = useParams();
  return <ProjectHistoryPanel projectId={projectId} />;
}

function renderHistoryPage(route = "/projects/project-123?tab=history&selected_revision_id=rev-2", locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  const messages: Record<string, string> = locale === "lt" ? { "react.projects.confirm_restore_revision": "Atkurti reviziją #{id}?" } : {};
  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/projects/:projectId" element={<HistoryTab />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider></I18nContext.Provider>,
  );
}

describe("ProjectHistoryPanel", () => {
  beforeEach(() => {
    mockedFetchProjectHistory.mockResolvedValue({
      revisions: [
        // Exactly the columns the list endpoint selects; `actor` and `summary`
        // were never part of the payload.
        {
          id: "rev-2",
          project_id: "project-123",
          author: "admin",
          action: "save",
          source: "ui",
          created_at: "2026-05-14T10:00:00Z",
        },
      ],
    });
    mockedFetchProjectHistoryDiff.mockResolvedValue({
      changes: [{ path: "name", change: "changed", from: "Demo", to: "Demo v2" }],
      summary: { total: 1 },
    });
    mockedRestoreProjectHistoryRevision.mockResolvedValue({ new_revision_id: "rev-3" });
    vi.spyOn(globalThis, "confirm").mockReturnValue(true);
  });

  it("hides restore for viewers while keeping the diff modal visible", async () => {
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(),
      refresh: vi.fn(),
      session: authSessionForRole("viewer"),
      status: "authenticated",
      switchAccount: vi.fn(),
    });

    renderHistoryPage();

    expect(await screen.findByRole("heading", { name: "History" })).toBeInTheDocument();
    expect(screen.getByText(/revision diff/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /restore this revision/i })).not.toBeInTheDocument();
    expect(screen.getByText("Viewer mode is read-only on project history.")).toBeInTheDocument();
  });

  it("keeps restore available for operators", async () => {
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(),
      refresh: vi.fn(),
      session: authSessionForRole("operator"),
      status: "authenticated",
      switchAccount: vi.fn(),
    });

    renderHistoryPage();

    expect(screen.getByText(/revision diff/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /restore this revision/i })).toBeInTheDocument();
  });

  it("localizes restore confirmation and does not mutate a declined owner revision", async () => {
    mockedUseAuth.mockReturnValue({ error: null, isAuthenticated: true, logout: vi.fn(), refresh: vi.fn(), session: authSessionForRole("operator"), status: "authenticated", switchAccount: vi.fn() });
    vi.mocked(globalThis.confirm).mockReturnValue(false);
    mockedRestoreProjectHistoryRevision.mockClear();
    renderHistoryPage("/projects/project-123?tab=history&selected_revision_id=rev-2", "lt");
    fireEvent.click(screen.getByRole("button", { name: /restore this revision/i }));
    expect(globalThis.confirm).toHaveBeenCalledWith("Atkurti reviziją #rev-2?");
    expect(mockedRestoreProjectHistoryRevision).not.toHaveBeenCalled();
  });

  it("restores revisions through the API instead of a form action", async () => {
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: true,
      logout: vi.fn(),
      refresh: vi.fn(),
      session: authSessionForRole("operator"),
      status: "authenticated",
      switchAccount: vi.fn(),
    });

    renderHistoryPage();

    const restoreButton = screen.getByRole("button", { name: /restore this revision/i });
    expect(restoreButton).toHaveAttribute("type", "button");
    fireEvent.click(restoreButton);

    await waitFor(() => {
      expect(mockedRestoreProjectHistoryRevision).toHaveBeenCalledWith("project-123", "rev-2");
    });
    expect(globalThis.confirm).toHaveBeenCalledWith("Restore revision #rev-2?");
    expect(await screen.findByText("Project settings restored from revision #rev-2.")).toBeInTheDocument();
  });

  it("renders the revision fields the endpoint returns and no placeholders for absent ones", async () => {
    renderHistoryPage();

    await screen.findByText("author: admin");

    // author and action come straight from the payload; before this they were
    // read under names the endpoint never sends, so both showed a fallback.
    expect(screen.getByText("author: admin")).toBeInTheDocument();
    expect(screen.getByText("save")).toBeInTheDocument();
    expect(screen.queryByText(/actor: system/i)).not.toBeInTheDocument();
    expect(screen.queryByText("update")).not.toBeInTheDocument();

    // project_settings_revisions has no summary column, so nothing may stand in
    // for one.
    expect(screen.queryByText(/no summary/i)).not.toBeInTheDocument();
  });
});
