import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { PointerEventsCheckLevel } from "@testing-library/user-event";

// The published checkbox hides its native input and takes the click on the label;
// clicking the input directly is the same toggle, so the pointer check is off.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { addDays, todayIn } from "@/lib/time/calendar-days";
import { browserTimezone } from "@/lib/time/user-timezone";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { SettingsServiceAccountsPage } from "@/pages/settings/service-accounts";
import {
  createServiceAccount,
  createServiceAccountToken,
  deleteServiceAccount,
  deleteServiceAccountToken,
  fetchServiceAccountsCollection,
  fetchServiceAccountTokensCollection,
  revokeServiceAccountToken,
  updateServiceAccount,
  updateServiceAccountToken,
} from "@/pages/settings/shared/api";
import { fetchWorkflows } from "@/pages/workflows/shared/api";

// These page journeys take about 6 s when the quality gate shares the machine,
// over vitest's 5 s default. Per-test timeouts set below still take precedence.
vi.setConfig({ testTimeout: 10_000 });

vi.mock("@/pages/projects/list/api", () => ({
  fetchProjectsList: vi.fn(),
}));

vi.mock("@/lib/auth/use-is-admin", () => ({
  useIsAdmin: vi.fn(),
}));

vi.mock("@/pages/settings/shared/api", () => ({
  createServiceAccount: vi.fn(),
  createServiceAccountToken: vi.fn(),
  deleteServiceAccount: vi.fn(),
  deleteServiceAccountToken: vi.fn(),
  fetchServiceAccountsCollection: vi.fn(),
  fetchServiceAccountTokensCollection: vi.fn(),
  revokeServiceAccountToken: vi.fn(),
  updateServiceAccount: vi.fn(),
  updateServiceAccountToken: vi.fn(),
}));

vi.mock("@/pages/workflows/shared/api", () => ({
  fetchWorkflows: vi.fn(),
}));

const mockedCreateServiceAccount = vi.mocked(createServiceAccount);
const mockedCreateServiceAccountToken = vi.mocked(createServiceAccountToken);
const mockedDeleteServiceAccount = vi.mocked(deleteServiceAccount);
const mockedDeleteServiceAccountToken = vi.mocked(deleteServiceAccountToken);
const mockedFetchProjectsList = vi.mocked(fetchProjectsList);
const mockedFetchServiceAccountsCollection = vi.mocked(
  fetchServiceAccountsCollection,
);
const mockedFetchServiceAccountTokensCollection = vi.mocked(
  fetchServiceAccountTokensCollection,
);
const mockedRevokeServiceAccountToken = vi.mocked(revokeServiceAccountToken);
const mockedFetchWorkflows = vi.mocked(fetchWorkflows);
const mockedUpdateServiceAccount = vi.mocked(updateServiceAccount);
const mockedUpdateServiceAccountToken = vi.mocked(updateServiceAccountToken);
const mockedUseIsAdmin = vi.mocked(useIsAdmin);

function renderPage(basename = "", locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  const messages: Record<string, string> = locale === "lt" ? {
    "react.settings.account_key_required": "Paskyros raktas privalomas.", // pragma: allowlist secret - UI translation text, not a credential
    "react.settings.service_account_key_already_exists": "Tarnybinė paskyra „{key}“ jau yra. Atverkite ją, kad sukurtumėte kitą prieigos raktą.", // pragma: allowlist secret - UI translation text, not a credential
  } : {};
  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <div data-testid="app-root" id="root">
      <QueryClientProvider client={queryClient}>
        <MemoryRouter basename={basename} initialEntries={[`${basename}/settings/service-accounts`]}>
          <SettingsServiceAccountsPage />
        </MemoryRouter>
      </QueryClientProvider>
    </div></I18nContext.Provider>,
  );
}

describe("SettingsServiceAccountsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedUseIsAdmin.mockReturnValue(true);
    mockedFetchServiceAccountsCollection.mockResolvedValue([
      {
        service_account_key: "ci-bot",
        display_name: "CI Bot",
        description: "Build automation identity",
        is_active: true,
        scopes: ["jobs.read", "jobs.write"],
        allowed_project_ids: ["7"],
        allowed_workflow_ids: ["wf-build"],
      },
    ]);
    mockedFetchServiceAccountTokensCollection.mockResolvedValue([
      {
        id: "31",
        label: "Deploy token",
        revoked: false,
        scopes: ["jobs.read"],
        allowed_project_ids: ["7"],
        allowed_workflow_ids: ["wf-build"],
        expires_at: null,
        last_used_at: null,
      },
    ]);
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: "7", name: "Platform" }],
      total: 1,
      page: 1,
      per_page: 200,
      total_pages: 1,
    });
    mockedFetchWorkflows.mockResolvedValue([
      { workflow_id: "wf-build", display_name: "Build" },
    ]);
    mockedCreateServiceAccount.mockResolvedValue({ status: "ok" });
    mockedCreateServiceAccountToken.mockResolvedValue({
      token: {
        label: "service-token",
        token: "fs_svc_test.secret",
      },
    });
    mockedDeleteServiceAccount.mockResolvedValue({ status: "deleted" });
    mockedUpdateServiceAccount.mockResolvedValue({ status: "updated" });
    mockedUpdateServiceAccountToken.mockResolvedValue({ status: "updated" });
  });

  it("denies a non-admin before loading service-account authority inventories", async () => {
    mockedUseIsAdmin.mockReturnValue(false);

    renderPage();

    expect(
      await screen.findByText("Administrator access required"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "New service account" }),
    ).not.toBeInTheDocument();
    expect(mockedFetchServiceAccountsCollection).not.toHaveBeenCalled();
    expect(mockedFetchProjectsList).not.toHaveBeenCalled();
    expect(mockedFetchWorkflows).not.toHaveBeenCalled();
  });

  it("manages identities in one drawer and credentials inside the selected identity", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();

    expect(
      await screen.findByRole("button", { name: /CI Bot/i }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Account key")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "New service account" }),
    );
    expect(
      screen.getByRole("dialog", { name: "Service account drawer" }),
    ).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Account key"));
    await user.type(screen.getByLabelText("Account key"), "deploy-bot");
    // Scope is no longer prefilled; creation requires a deliberate choice.
    await user.click(screen.getByLabelText("jobs.read"));
    await user.click(
      screen.getByRole("button", { name: "Create service account" }),
    );

    await waitFor(() => {
      expect(mockedCreateServiceAccount).toHaveBeenCalledWith(
        expect.objectContaining({ service_account_key: "deploy-bot" }),
      );
    });

    await user.click(
      screen.getAllByLabelText("Close service account drawer")[1],
    );
    await user.click(screen.getByRole("button", { name: /CI Bot/i }));
    expect(
      screen.getByRole("dialog", { name: "Service account drawer" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Credentials")).toBeInTheDocument();
    await waitFor(() => {
      expect(mockedFetchServiceAccountTokensCollection).toHaveBeenCalledWith(
        "ci-bot",
      );
    });
    expect(
      await screen.findByRole("button", { name: /Deploy token/i }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New machine token" }));
    expect(
      screen.getByRole("dialog", { name: "Machine token drawer" }),
    ).toBeInTheDocument();
    const createScopeInputs = screen
      .getAllByRole("checkbox")
      .filter(
        (element) => element.getAttribute("name") === "machine_token_scopes",
      );
    expect(createScopeInputs).toHaveLength(12);
    expect(screen.getByLabelText("jobs.read")).toBeEnabled();
    expect(screen.getByLabelText("projects.read")).toBeDisabled();
    await user.click(screen.getByLabelText("jobs.read"));
    await user.click(
      screen.getByRole("button", { name: "Create machine token" }),
    );

    await waitFor(() => {
      expect(mockedCreateServiceAccountToken).toHaveBeenCalledWith(
        "ci-bot",
        expect.objectContaining({
          expires_in_days: 30,
          label: "service-token",
          scopes: ["jobs.read"],
        }),
      );
    });
    expect(
      screen.getByText("Authorization: Bearer $FLOW_STEWARD_TOKEN"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/FLOW_STEWARD_URL\/api\/\.\.\./u),
    ).toBeInTheDocument();

    await user.click(screen.getAllByLabelText("Close machine token drawer")[1]);

    await user.clear(screen.getByLabelText("Display name (optional)"));
    await user.type(screen.getByLabelText("Display name (optional)"), "CI Bot Updated");
    await user.click(screen.getByRole("button", { name: "Save identity" }));

    await waitFor(() => {
      expect(mockedUpdateServiceAccount).toHaveBeenCalledWith(
        "ci-bot",
        expect.objectContaining({
          display_name: "CI Bot Updated",
          is_active: true,
        }),
      );
    });

    await user.click(screen.getByRole("button", { name: "Disable identity" }));
    await waitFor(() => {
      expect(mockedUpdateServiceAccount).toHaveBeenLastCalledWith(
        "ci-bot",
        expect.objectContaining({
          is_active: false,
        }),
      );
    });

    await user.click(screen.getByRole("button", { name: /Deploy token/i }));
    expect(
      screen.getByRole("dialog", { name: "Machine token drawer" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Token ID")).toBeInTheDocument();
    const editScopeInputs = screen
      .getAllByRole("checkbox")
      .filter(
        (element) => element.getAttribute("name") === "machine_token_scopes",
      );
    expect(editScopeInputs).toHaveLength(12);
    expect(screen.getByLabelText("jobs.read")).toBeChecked();
    expect(screen.getByLabelText("jobs.write")).not.toBeChecked();
    await user.click(screen.getByLabelText("jobs.write"));

    await user.clear(screen.getByLabelText("Machine token label (optional)"));
    await user.type(
      screen.getByLabelText("Machine token label (optional)"),
      "Deploy token edited",
    );
    await user.click(screen.getByRole("button", { name: "Save token" }));

    await waitFor(() => {
      expect(mockedUpdateServiceAccountToken).toHaveBeenCalledWith(
        "ci-bot",
        "31",
        {
          label: "Deploy token edited",
          scopes: ["jobs.read", "jobs.write"],
        },
      );
    });

    await user.click(screen.getAllByLabelText("Close machine token drawer")[1]);
    await user.click(screen.getByRole("button", { name: "Delete" }));

    // Irreversible, so a blocking confirmation names it before anything is sent.
    expect(mockedDeleteServiceAccount).not.toHaveBeenCalled();
    const confirmation = screen.getByRole("alertdialog", { name: "Delete service account “CI Bot”?" });
    expect(confirmation).toHaveTextContent("Every machine token issued to this identity stops working immediately.");
    expect(confirmation).toHaveTextContent("This cannot be undone.");
    await user.click(within(confirmation).getByRole("button", { name: "Delete service account" }));

    await waitFor(() => {
      expect(mockedDeleteServiceAccount).toHaveBeenCalledWith("ci-bot");
    });
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(await screen.findByText("Service account ci-bot deleted.")).toBeInTheDocument();
  });

  it("allows catalog scopes covered by canonical service-account wildcards", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    mockedFetchServiceAccountsCollection.mockResolvedValueOnce([
      {
        service_account_key: "ci-bot",
        display_name: "CI Bot",
        description: "Build automation identity",
        is_active: true,
        scopes: ["jobs:*", "notify:*"],
        allowed_project_ids: [],
        allowed_workflow_ids: [],
      },
    ]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /CI Bot/i }));
    await user.click(screen.getByRole("button", { name: "New machine token" }));

    expect(screen.getByLabelText("jobs.read")).toBeEnabled();
    expect(screen.getByLabelText("jobs.write")).toBeEnabled();
    expect(screen.getByLabelText("notify.read")).toBeEnabled();
    expect(screen.getByLabelText("notify:dispatch")).toBeEnabled();
    expect(screen.getByLabelText("projects.read")).toBeDisabled();
  });

  it("opens New with nothing carried over from the account just viewed", async () => {
    // A5-006: openCreateAccountDrawer only set the mode, so New inherited the
    // viewed account's key, scopes, restrictions, description and active flag.
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();

    await user.click(await screen.findByRole("button", { name: /CI Bot/i }));
    expect(screen.getByLabelText("Account key")).toHaveValue("ci-bot");
    expect(screen.getByLabelText("jobs.read")).toBeChecked();

    await user.click(screen.getAllByLabelText("Close service account drawer")[1]);
    await user.click(
      screen.getByRole("button", { name: "New service account" }),
    );

    expect(screen.getByLabelText("Account key")).toHaveValue("");
    expect(screen.getByLabelText("jobs.read")).not.toBeChecked();
    expect(screen.getByLabelText("jobs.write")).not.toBeChecked();
  });

  it("uses the complete shared MCP-relevant token scope catalog", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: "New service account" }),
    );

    for (const scope of [
      "jobs.read", "jobs.write", "projects.read", "projects.write",
      "registry.read", "registry.write", "workflows.read", "workflows.write",
      "reports.read", "reports.write",
      "notify.read", "notify:dispatch",
    ]) {
      expect(screen.getByLabelText(scope)).toBeInTheDocument();
    }
  });

  it("will not create a service account with no scope", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: "New service account" }),
    );
    await user.type(screen.getByLabelText("Account key"), "scopeless-bot");
    await user.click(
      screen.getByRole("button", { name: "Create service account" }),
    );

    expect(await screen.findByText(/at least one scope/i)).toBeInTheDocument();
    expect(mockedCreateServiceAccount).not.toHaveBeenCalled();
  });

  it("names Account active as a checkbox choice without an optional field marker", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();
    await user.click(await screen.findByRole("button", { name: "New service account" }));
    const dialog = screen.getByRole("dialog", { name: "Service account drawer" });

    const active = within(dialog).getByRole("checkbox", { name: "Account active" });
    expect(active).not.toBeChecked();
    await user.click(active);
    expect(active).toBeChecked();
    expect(within(dialog).queryByRole("checkbox", { name: /Account active.*optional/u })).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText("Account key")).toBeRequired();
    expect(mockedCreateServiceAccount).not.toHaveBeenCalled();
  });

  it("exposes required account fields and focuses stable validation errors", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: "New service account" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Service account drawer" });
    const accountKey = within(dialog).getByLabelText("Account key");
    const authority = within(dialog).getByRole("group", { name: /Maximum authority/u });
    expect(accountKey).toBeRequired();
    // aria-required is the group-level contract; toBeRequired only supports form controls.
    // eslint-disable-next-line jest-dom/prefer-required
    expect(authority).toHaveAttribute("aria-required", "true");
    expect(within(dialog).getByText("Account key *")).toBeVisible();
    expect(within(dialog).getByText("Maximum authority *")).toBeVisible();
    expect(within(dialog).getByRole("textbox", { name: "Display name (optional)" })).toBeVisible();
    expect(within(dialog).getByRole("textbox", { name: "Description (optional)" })).not.toBeRequired();
    expect(within(dialog).getByRole("group", { name: "Allowed projects (optional)" })).toBeVisible();
    expect(within(dialog).getByRole("group", { name: "Allowed workflows (optional)" })).toBeVisible();

    await user.click(within(dialog).getByRole("button", { name: "Create service account" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Account key is required.",
    );
    expect(accountKey).toHaveFocus();

    await user.type(accountKey, "scopeless-bot");
    await user.click(within(dialog).getByRole("button", { name: "Create service account" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Select at least one scope.",
    );
    expect(within(dialog).getByLabelText("jobs.read")).toHaveFocus();
    expect(mockedCreateServiceAccount).not.toHaveBeenCalled();
  });

  it("localizes the missing account key error and keeps validation focus without submitting", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage("", "lt");
    await user.click(await screen.findByRole("button", { name: "New service account" }));
    const dialog = screen.getByRole("dialog", { name: "Service account drawer" });
    await user.click(within(dialog).getByRole("button", { name: "Create service account" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Paskyros raktas privalomas.");
    expect(within(dialog).queryByText("Account key is required.")).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText("Account key")).toHaveFocus();
    expect(mockedCreateServiceAccount).not.toHaveBeenCalled();
  });

  it("marks token expiry required and says what an empty scope selection means", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();

    await user.click(await screen.findByRole("button", { name: /CI Bot/i }));
    await user.click(screen.getByRole("button", { name: "New machine token" }));
    const dialog = screen.getByRole("dialog", { name: "Machine token drawer" });
    // No "No expiry" branch: a machine token always has a deadline.
    const expiry = within(dialog).getByRole("group", { name: "Expiration" });
    expect(within(expiry).getAllByRole("radio").map((radio) => radio.getAttribute("value"))).toEqual(["30", "90", "custom"]);
    expect(within(expiry).getByRole("radio", { name: "30 days" })).toBeChecked();
    // U2: "(optional)" read as "empty inherits the account's authority"; it never does.
    expect(within(dialog).getByText("Token scopes (none selected = no access)")).toBeVisible();
    expect(within(dialog).getByRole("status")).toHaveTextContent(
      "every API call it makes will be refused",
    );
    await user.click(within(dialog).getByRole("checkbox", { name: "jobs.read" }));
    expect(within(dialog).queryByRole("status")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("checkbox", { name: "jobs.read" }));

    await user.click(within(expiry).getByRole("radio", { name: "Custom" }));
    const expiresOn = within(dialog).getByLabelText("Expires on");
    await user.click(within(dialog).getByRole("button", { name: "Create machine token" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Choose the date the token expires.");
    expect(expiresOn).toHaveFocus();
    expect(mockedCreateServiceAccountToken).not.toHaveBeenCalled();
  });

  it("focuses, traps, dismisses, and restores the service-account drawer", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();
    const root = screen.getByTestId("app-root");
    const opener = await screen.findByRole("button", { name: "New service account" });
    opener.focus();

    await user.click(opener);
    screen.getByRole("dialog", { name: "Service account drawer" });
    const first = screen.getAllByLabelText("Close service account drawer")[1];
    await waitFor(() => expect(first).toHaveFocus());
    expect(root).toHaveAttribute("aria-hidden", "true");
    expect(root.inert).toBe(true);

    screen.getByRole("button", { name: "Create service account" }).focus();
    fireEvent.keyDown(screen.getByRole("button", { name: "Create service account" }), {
      key: "Tab",
    });
    expect(screen.getByLabelText("Account key")).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Service account drawer" })).not.toBeInTheDocument();
    await waitFor(() => expect(opener).toHaveFocus());
    expect(root).not.toHaveAttribute("aria-hidden");
    expect(root.inert).toBe(false);
  });

  it("restores machine-token focus to its opener inside the retained account drawer", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();

    await user.click(await screen.findByRole("button", { name: /CI Bot/i }));
    const opener = screen.getByRole("button", { name: "New machine token" });
    opener.focus();
    await user.click(opener);

    screen.getByRole("dialog", { name: "Machine token drawer" });
    const first = screen.getAllByLabelText("Close machine token drawer")[1];
    await waitFor(() => expect(first).toHaveFocus());
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "Machine token drawer" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Service account drawer" })).toBeInTheDocument();
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("keeps duplicate-name workflow restrictions separate without displaying their ids or auto-selecting", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    mockedFetchWorkflows.mockResolvedValueOnce([
      { workflow_id: "wf-audit-one", display_name: "AUDIT Control Flow Failure Workflow", description: "Inventory import" },
      { workflow_id: "wf-audit-two", display_name: "AUDIT Control Flow Failure Workflow", description: "Invoice reconciliation" },
    ]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New service account" }));
    const first = await screen.findByRole("checkbox", { name: "AUDIT Control Flow Failure Workflow Inventory import" });
    const second = screen.getByRole("checkbox", { name: "AUDIT Control Flow Failure Workflow Invoice reconciliation" });
    const choices = [first, second];
    expect(screen.getAllByRole("link", { name: /Inspect workflow/ }).map((link) => link.getAttribute("href"))).toEqual(["/registry/workflows/wf-audit-one", "/registry/workflows/wf-audit-two"]);
    expect(choices.map((choice) => choice.getAttribute("value"))).toEqual(["wf-audit-one", "wf-audit-two"]);
    expect(document.body).not.toHaveTextContent(/wf-audit-one/);
    expect(document.body).not.toHaveTextContent(/wf-audit-two/);
    expect(first).not.toBeChecked();
    expect(second).not.toBeChecked();

    await user.click(first);
    expect(first).toBeChecked();
    expect(second).not.toBeChecked();
  });

  it("keeps authority inspection destinations under the deployed app basename", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage("/app");
    await user.click(await screen.findByRole("button", { name: "New service account" }));
    expect(await screen.findByRole("link", { name: "Inspect workflow: Build" })).toHaveAttribute("href", "/app/registry/workflows/wf-build");
    expect(screen.getByRole("link", { name: "Inspect project: Platform" })).toHaveAttribute("href", "/app/projects/7");
  });

  it("keeps long authority ids out of labels without adding horizontal drawer scrolling", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    const longWorkflowId = `wf-${"long-stable-identifier-".repeat(8)}`;
    mockedFetchWorkflows.mockResolvedValueOnce([
      {
        workflow_id: longWorkflowId,
        display_name: "Workflow with a deliberately long stable identity",
      },
    ]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New service account" }));
    const dialog = screen.getByRole("dialog", { name: "Service account drawer" });
    const scrollRegion = screen.getByTestId("service-account-drawer-scroll-region");
    await within(dialog).findByRole("checkbox", {
      name: "Workflow with a deliberately long stable identity",
    });

    expect(dialog).toHaveClass("overflow-hidden");
    expect(scrollRegion).toHaveClass("overflow-x-hidden", "overflow-y-auto", "min-w-0");
    expect(dialog).not.toHaveTextContent(longWorkflowId);
    const authorityValues = within(dialog).getAllByRole("checkbox").map((choice) => choice.getAttribute("value"));
    expect(authorityValues).toContain(longWorkflowId);
    expect(within(dialog).getByRole("button", { name: "Create service account" })).toBeVisible();
  });

  it("shows MCP connection details only while the one-time secret is visible", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();

    await user.click(await screen.findByRole("button", { name: /CI Bot/i }));
    await user.click(screen.getByRole("button", { name: "New machine token" }));
    await user.click(screen.getByRole("button", { name: "Create machine token" }));

    expect(await screen.findByText("Connect an MCP client")).toBeVisible();
    expect(screen.getByText(`${globalThis.location.origin}/mcp`)).toBeVisible();
    expect(screen.getByText("Authorization: Bearer fs_svc_test.secret")).toBeVisible();
    expect(screen.getByRole("button", { name: "Copy MCP endpoint" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Copy MCP authorization header" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByText("Connect an MCP client")).not.toBeInTheDocument();
    expect(screen.queryByText(`${globalThis.location.origin}/mcp`)).not.toBeInTheDocument();
    expect(screen.queryByText("Authorization: Bearer fs_svc_test.secret")).not.toBeInTheDocument();
  });

  it("localizes the locally generated duplicate-account error and preserves the owner key", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage("", "lt");
    await user.click(await screen.findByRole("button", { name: "New service account" }));
    await user.type(screen.getByLabelText("Account key"), "ci-bot");
    await user.click(screen.getByLabelText("jobs.read"));
    await user.click(screen.getByRole("button", { name: "Create service account" }));
    expect(await screen.findByText("Tarnybinė paskyra „ci-bot“ jau yra. Atverkite ją, kad sukurtumėte kitą prieigos raktą.")).toBeInTheDocument();
    expect(screen.getByLabelText("Account key")).toHaveValue("ci-bot");
    expect(mockedCreateServiceAccount).not.toHaveBeenCalled();
  });

  it("rejects a duplicate key before creating anything", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: "New service account" }),
    );
    await user.type(screen.getByLabelText("Account key"), "ci-bot");
    await user.click(screen.getByLabelText("jobs.read"));
    await user.click(
      screen.getByRole("button", { name: "Create service account" }),
    );

    expect(await screen.findByText(/already exists/i)).toBeInTheDocument();
    expect(mockedCreateServiceAccount).not.toHaveBeenCalled();
  });

  it("says that an empty restriction list means every project", async () => {
    // Empty means unrestricted in the principal contract, and an empty checkbox
    // list is indistinguishable from "not decided yet" unless it says so.
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: "New service account" }),
    );

    expect(screen.getByText(/means ALL projects/i)).toBeInTheDocument();
    expect(screen.getByText(/means ALL workflows/i)).toBeInTheDocument();

    await user.click(screen.getByLabelText("Platform"));
    expect(screen.getByText(/Restricted to 1 project/i)).toBeInTheDocument();
  });

  it("requires explicit confirmation before revoking a machine token", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    mockedRevokeServiceAccountToken.mockResolvedValue({ status: "revoked" });
    renderPage();

    await user.click(await screen.findByRole("button", { name: /CI Bot/i }));
    const tokenRow = await screen.findByRole("button", { name: /Deploy token/i });
    expect(tokenRow).not.toHaveTextContent("id: 31");
    await user.click(
      await screen.findByRole("button", { name: /Deploy token/i }),
    );
    await user.click(screen.getByRole("button", { name: "Revoke" }));

    expect(mockedRevokeServiceAccountToken).not.toHaveBeenCalled();
    const confirmation = screen.getByRole("alertdialog", {
      name: "Revoke “Deploy token”?",
    });
    expect(confirmation).toHaveTextContent("Clients using this token lose access immediately.");
    // The irreversibility line stands on its own, per canon's Modal / Dialog contract.
    expect(within(confirmation).getByText("This cannot be undone.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(mockedRevokeServiceAccountToken).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Revoke" }));
    await user.click(screen.getByRole("button", { name: "Revoke token" }));
    await waitFor(() =>
      expect(mockedRevokeServiceAccountToken).toHaveBeenCalledWith(
        "ci-bot",
        "31",
      ),
    );
    expect((await screen.findAllByText("Token “Deploy token” revoked.")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Token 31 revoked.")).not.toBeInTheDocument();
  });

  it("labels unnamed authority rows without ids and sends only the deliberately selected ids", async () => {
    const accountIdentifier = "restricted-client";
    const projectId = "d93883db-a80e-46ad-ae32-8a02da34b766";
    const workflowId = "87e8d2b9-2509-4f0a-9e21-26fb2178f53a";
    mockedFetchProjectsList.mockResolvedValueOnce({
      projects: [{ id: projectId, name: "" }], total: 1, page: 1, per_page: 200, total_pages: 1,
    });
    mockedFetchWorkflows.mockResolvedValueOnce([{ workflow_id: workflowId, display_name: " " }]);
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();
    await user.click(await screen.findByRole("button", { name: "New service account" }));
    const dialog = screen.getByRole("dialog", { name: "Service account drawer" });
    const project = await within(dialog).findByRole("checkbox", { name: "Unnamed project" });
    const workflow = await within(dialog).findByRole("checkbox", { name: "Unnamed workflow" });
    expect(project).not.toBeChecked();
    expect(workflow).not.toBeChecked();
    expect(within(dialog).getByRole("link", { name: "Inspect project: Unnamed project" })).toHaveAttribute("href", `/projects/${projectId}`);
    expect(within(dialog).getByRole("link", { name: "Inspect workflow: Unnamed workflow" })).toHaveAttribute("href", `/registry/workflows/${workflowId}`);
    expect([project, workflow].map((choice) => choice.getAttribute("value"))).toEqual([projectId, workflowId]);
    expect(dialog).not.toHaveTextContent(projectId);
    expect(dialog).not.toHaveTextContent(workflowId);
    await user.type(within(dialog).getByLabelText("Account key"), accountIdentifier);
    await user.click(within(dialog).getByLabelText("jobs.read"));
    await user.click(project);
    await user.click(workflow);
    await user.click(within(dialog).getByRole("button", { name: "Create service account" }));
    await waitFor(() => expect(mockedCreateServiceAccount).toHaveBeenCalledWith(expect.objectContaining({
      service_account_key: accountIdentifier, scopes: ["jobs.read"],
      allowed_project_ids: [projectId], allowed_workflow_ids: [workflowId],
    })));
  });
  it("keeps a failed token deletion inside its dialog, with the drawer still behind it", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    mockedDeleteServiceAccountToken.mockRejectedValueOnce(new Error("Token is referenced by a running job."));
    renderPage();

    await user.click(await screen.findByRole("button", { name: /CI Bot/i }));
    await user.click(await screen.findByRole("button", { name: /Deploy token/i }));
    await user.click(screen.getByRole("button", { name: "Delete" }));
    const confirmation = screen.getByRole("alertdialog", { name: "Delete token “Deploy token”?" });
    await user.click(within(confirmation).getByRole("button", { name: "Delete token" }));

    // Canon: on failure the dialog stays open, restores its actions and says why.
    expect(await within(confirmation).findByRole("alert")).toHaveTextContent("Token is referenced by a running job.");
    expect(within(confirmation).getByRole("button", { name: "Delete token" })).toBeEnabled();
    expect(screen.getByRole("dialog", { name: "Machine token drawer", hidden: true })).toBeInTheDocument();

    // Escape belongs to the dialog while it is open: it closes the dialog only.
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(screen.getByRole("dialog", { name: "Machine token drawer" })).toBeInTheDocument();
  });

  it("moves focus to the Credentials heading when a deletion empties the list", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    mockedDeleteServiceAccountToken.mockResolvedValue({ status: "deleted" });
    renderPage();

    await user.click(await screen.findByRole("button", { name: /CI Bot/i }));
    await user.click(await screen.findByRole("button", { name: /Deploy token/i }));
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete token" }),
    );

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    // Never body. This identity held a single token, so there is no sibling row
    // and focus goes to the region's heading.
    await waitFor(() => expect(screen.getByRole("heading", { name: "Credentials" })).toHaveFocus());
  });

  // AUDIT-056 — remediation epic 2026-07-29, WS-02.
  // `Expires in days = 0` used to create a never-expiring machine token: the
  // handler treated every non-positive value as "no expiry supplied" and simply
  // omitted the field. The canon mode choice (30 / 90 / Custom) must keep that
  // closed: every branch sends a day count of at least 1.
  describe("machine token expiry", () => {
    const today = todayIn(browserTimezone());

    async function openTokenDrawer(user: ReturnType<typeof userEvent.setup>) {
      renderPage();
      await user.click(await screen.findByRole("button", { name: /CI Bot/i }));
      await user.click(
        screen.getByRole("button", { name: "New machine token" }),
      );
    }

    async function submitAndReadExpiry(user: ReturnType<typeof userEvent.setup>) {
      await user.click(screen.getByRole("button", { name: "Create machine token" }));
      await waitFor(() => expect(mockedCreateServiceAccountToken).toHaveBeenCalled());
      const [, payload] = mockedCreateServiceAccountToken.mock.calls[0];
      return (payload as Record<string, unknown>).expires_in_days;
    }

    it.each([["30 days", 30], ["90 days", 90]])("sends %s as a day count", async (label, days) => {
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      await openTokenDrawer(user);
      await user.click(screen.getByRole("radio", { name: label }));
      expect(screen.queryByLabelText("Expires on")).not.toBeInTheDocument();
      expect(await submitAndReadExpiry(user)).toBe(days);
    });

    it("converts a custom date into whole days from today in the user's zone", async () => {
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      await openTokenDrawer(user);
      await user.click(screen.getByRole("radio", { name: "Custom" }));
      const expiresOn = screen.getByLabelText("Expires on");
      expect(expiresOn).toHaveAttribute("min", addDays(today, 1));
      fireEvent.change(expiresOn, { target: { value: addDays(today, 45) } });
      expect(screen.getByText(/45 days from today\./)).toBeInTheDocument();
      expect(await submitAndReadExpiry(user)).toBe(45);
    });

    it.each([["today", 0], ["a past day", -3]])("refuses %s instead of minting a token", async (_name, offset) => {
      const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
      await openTokenDrawer(user);
      await user.click(screen.getByRole("radio", { name: "Custom" }));
      const expiresOn = screen.getByLabelText("Expires on");
      fireEvent.change(expiresOn, { target: { value: addDays(today, offset) } });
      expect(screen.getByRole("alert")).toHaveTextContent("is not in the future.");
      expect(expiresOn).toHaveAttribute("aria-invalid", "true");
      await user.click(screen.getByRole("button", { name: "Create machine token" }));
      expect(expiresOn).toHaveFocus();
      expect(mockedCreateServiceAccountToken).not.toHaveBeenCalled();
    });
  });

  it("lists each machine token with the scopes it actually holds", async () => {
    mockedFetchServiceAccountTokensCollection.mockResolvedValue([
      { id: "31", label: "Deploy token", revoked: false, scopes: ["jobs.read"], expires_at: null },
      { id: "32", label: "service-token", revoked: false, scopes: [], expires_at: null },
    ]);
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderPage();

    await user.click(await screen.findByRole("button", { name: /CI Bot/i }));

    expect(await screen.findByText("scopes: jobs.read")).toBeVisible();
    expect(screen.getByText("scopes: none (no access)")).toBeVisible();
  });
});
