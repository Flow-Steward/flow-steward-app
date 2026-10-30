import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { fetchProjectDetail, updateProject } from "@/pages/projects/detail/api";
import { useHasCapability } from "@/lib/auth/capabilities";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { ProjectsDetailPage } from "@/pages/projects/detail";
import { fetchWorkspaces } from "@/pages/registry/workspaces/api";
import { fetchProjectWorkflowBindings } from "@/pages/projects/workflows/api";
import {
  fetchWorkflowDetail,
  fetchWorkflows,
} from "@/pages/workflows/shared/api";

vi.mock("@/pages/projects/detail/api", () => ({
  fetchProjectDetail: vi.fn(),
  updateProject: vi.fn(),
}));

vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn() }));

vi.mock("@/pages/projects/workflows/api", () => ({
  fetchProjectWorkflowBindings: vi.fn(),
}));

vi.mock("@/pages/registry/workspaces/api", () => ({
  fetchWorkspaces: vi.fn(),
}));

vi.mock("@/pages/workflows/shared/api", () => ({
  fetchWorkflowDetail: vi.fn(),
  fetchWorkflows: vi.fn(),
}));

vi.mock("@/pages/projects/history", () => ({
  ProjectHistoryPanel: ({ projectId }: { projectId: string }) => <div>history-panel {projectId}</div>,
}));

vi.mock("@/pages/projects/connections", () => ({
  ProjectConnectionsEditor: () => <div>connections-editor</div>,
}));

vi.mock("@/pages/projects/datasets", () => ({
  ProjectDatasetsEditor: () => <div>datasets-editor</div>,
}));

vi.mock("@/pages/projects/notifications/overview", () => ({
  ProjectNotificationOverview: ({ projectId }: { projectId: string }) => <div>notification-overview {projectId}</div>,
}));

vi.mock("@/pages/projects/policy", () => ({
  ProjectPolicyEditor: () => <div>policy-editor</div>,
}));

const mockedFetchProjectDetail = vi.mocked(fetchProjectDetail);
const mockedUseHasCapability = vi.mocked(useHasCapability);
const mockedUpdateProject = vi.mocked(updateProject);
const mockedFetchProjectWorkflowBindings = vi.mocked(
  fetchProjectWorkflowBindings,
);
const mockedFetchWorkspaces = vi.mocked(fetchWorkspaces);
const mockedFetchWorkflows = vi.mocked(fetchWorkflows);
const mockedFetchWorkflowDetail = vi.mocked(fetchWorkflowDetail);

function renderProjectPage(route = "/projects/project-123?tab=integrations", locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  const messages: Record<string, string> = locale === "lt" ? {
    "react.projects.no_workflows_available": "Nėra darbo eigų.",
    "react.projects.no_workspace_data_connections": "Šiai darbo sričiai dar nėra duomenų ryšių. Sukurkite juos registre, tada priskirkite projektui, kad darbo eigos galėtų per juos skaityti duomenis.",
    "react.projects.no_agents_match_filters": "Nė vienas agentas neatitinka dabartinių filtrų.",
    "react.projects.referenced_by_linked_workflows": "Naudojamas susietose darbo eigose",
    "react.projects.assigned_connections_count": "Priskirta: {count}",
  } : {};
  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route
            element={<ProjectsDetailPage />}
            path="/projects/:projectId"
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
    </I18nContext.Provider>,
  );
}

/**
 * Select is a button plus a listbox, not a native control: a value is chosen
 * by opening the menu and clicking an option, and the trigger text is what
 * reports the current value.
 */
async function chooseOption(
  user: ReturnType<typeof userEvent.setup>,
  selectLabel: string,
  optionLabel: string,
) {
  await user.click(await screen.findByRole("combobox", { name: selectLabel }));
  await user.click(await screen.findByRole("option", { name: optionLabel }));
}

async function openSelect(user: ReturnType<typeof userEvent.setup>, selectLabel: string) {
  await user.click(await screen.findByRole("combobox", { name: selectLabel }));
}

describe("ProjectsDetailPage", () => {
  beforeEach(() => {
    mockedFetchProjectDetail.mockReset();
    mockedUpdateProject.mockReset();
    mockedUseHasCapability.mockReset();
    mockedUseHasCapability.mockReturnValue(true);
    mockedFetchProjectWorkflowBindings.mockReset();
    mockedFetchWorkspaces.mockReset();
    mockedFetchWorkflows.mockReset();
    mockedFetchWorkflowDetail.mockReset();

    mockedFetchProjectDetail.mockResolvedValue({
      id: "project-123",
      name: "Audit Project",
      description: "Audit project for UI/runtime verification",
      workspace_id: "ws-1",
      data_connection_ids: [],
      assigned_data_connections: [],
      available_data_connections: [],
      available_providers: [["openai", "OpenAI"]],
      available_models: ["gpt-4.1-nano", "gpt-5-mini"],
      providers_with_models: {
        provider_models: {
          openai: [
            { model_id: "gpt-4.1-nano", label: "GPT-4.1 Nano" },
            { model_id: "gpt-5-mini", label: "GPT-5 Mini" },
          ],
        },
      },
      default_agent_models: {
        planner: { provider: "openai", model: "gpt-4.1-nano" },
        reviewer: { provider: "openai", model: "gpt-5-mini" },
      },
      agent_model_overrides: {},
    } as never);
    mockedUpdateProject.mockResolvedValue({ ok: true } as never);
    mockedFetchProjectWorkflowBindings.mockResolvedValue({
      workflow_ids: [],
      operator_default_workflow_id: "",
      project_workflows: [],
    } as never);
    mockedFetchWorkspaces.mockResolvedValue([
      { id: "ws-1", display_name: "Test" },
    ] as never);
    mockedFetchWorkflows.mockResolvedValue([] as never);
    mockedFetchWorkflowDetail.mockResolvedValue({} as never);
  });

  it.each(["loading", "failed"] as const)(
    "blocks Save and direct submit while workflow links are %s",
    async (state) => {
      if (state === "failed") {
        mockedFetchProjectWorkflowBindings.mockRejectedValue(new Error("Workflow links unavailable"));
      } else {
        mockedFetchProjectWorkflowBindings.mockReturnValue(new Promise(() => undefined));
      }
      renderProjectPage("/projects/project-123");

      expect(await screen.findByRole("textbox", { name: "Name" })).toHaveValue("Audit Project");
      if (state === "failed") {
        expect(await screen.findByText("Workflow links unavailable")).toBeVisible();
      }
      const save = screen.getByRole("button", { name: "Save Project" });
      expect(save).toBeDisabled();
      fireEvent.submit(screen.getByRole("form", { name: "Project edit form" }));
      await act(async () => undefined);
      expect(mockedUpdateProject).not.toHaveBeenCalled();
    },
  );

  it.each(["loading", "failed"] as const)(
    "blocks Save and direct submit while the workflow catalog is %s",
    async (state) => {
      if (state === "failed") {
        mockedFetchWorkflows.mockRejectedValue(new Error("Workflow catalog unavailable"));
      } else {
        mockedFetchWorkflows.mockReturnValue(new Promise(() => undefined));
      }
      renderProjectPage("/projects/project-123");

      expect(await screen.findByRole("textbox", { name: "Name" })).toHaveValue("Audit Project");
      if (state === "failed") {
        expect(await screen.findByText("Workflow catalog unavailable")).toBeVisible();
      }
      const save = screen.getByRole("button", { name: "Save Project" });
      expect(save).toBeDisabled();
      fireEvent.submit(screen.getByRole("form", { name: "Project edit form" }));
      await act(async () => undefined);
      expect(mockedUpdateProject).not.toHaveBeenCalled();
    },
  );

  it("enables a no-op save after workflow links load and preserves all 39 links and the operator default", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const workflowIds = Array.from({ length: 39 }, (_, index) => `workflow-${index + 1}`);
    const projectWorkflows = workflowIds.map((workflowId) => ({
      workflow_id: workflowId,
      display_name: workflowId,
      enabled: true,
    }));
    const links = {
      project_id: "project-123",
      workflow_ids: workflowIds,
      operator_default_workflow_id: "workflow-17",
      project_workflows: projectWorkflows,
    };
    let resolveLinks!: (value: typeof links) => void;
    mockedFetchProjectWorkflowBindings.mockReturnValue(new Promise((resolve) => {
      resolveLinks = resolve;
    }));
    mockedFetchWorkflows.mockResolvedValue(projectWorkflows as never);
    renderProjectPage("/projects/project-123");

    expect(await screen.findByRole("textbox", { name: "Name" })).toHaveValue("Audit Project");
    const save = screen.getByRole("button", { name: "Save Project" });
    expect(save).toBeDisabled();
    await act(async () => { resolveLinks(links); });
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);

    await waitFor(() => expect(mockedUpdateProject).toHaveBeenCalledTimes(1));
    expect(mockedUpdateProject).toHaveBeenCalledWith("project-123", expect.objectContaining({
      name: "Audit Project",
      description: "Audit project for UI/runtime verification",
      workspace_id: "ws-1",
      workflow_ids: workflowIds,
      operator_default_workflow_id: "workflow-17",
    }));
  });

  it.each([
    { tab: "workflows", translated: "Nėra darbo eigų.", english: "No workflows available." },
    { tab: "data-connections", translated: "Šiai darbo sričiai dar nėra duomenų ryšių. Sukurkite juos registre, tada priskirkite projektui, kad darbo eigos galėtų per juos skaityti duomenis.", english: "No workspace data connections exist for this workspace yet. Create them in the registry, then assign them here so project workflows can read data through them." },
  ])("localizes the empty $tab catalog without fabricating entries", async ({ tab, translated, english }) => {
    renderProjectPage(`/projects/project-123?tab=${tab}`, "lt");
    expect(await screen.findByText(translated)).toBeInTheDocument();
    expect(screen.queryByText(english)).not.toBeInTheDocument();
  });

  it("is the project itself: no Configuration screen to open and no way back to leave for", async () => {
    renderProjectPage();

    await screen.findByRole("tablist", { name: "Project sections" });
    expect(screen.queryByRole("link", { name: "Configuration" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Back to project" })).not.toBeInTheDocument();
  });

  it("renders friendly model labels while persisting exact model IDs", async () => {
    // The published checkbox hides its native input behind the mark.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectPage();

    await chooseOption(user, "Default provider", "OpenAI");
    await openSelect(user, "Default model");
    expect(await screen.findByRole("option", { name: "GPT-4.1 Nano" })).toBeInTheDocument();

    await user.click(screen.getByRole("option", { name: "GPT-4.1 Nano" }));
    expect(screen.getByRole("combobox", { name: "Default model" })).toHaveTextContent("GPT-4.1 Nano");
  });

  it("reveals the override catalog and allows adding an agent override from the full catalog", async () => {
    // The published checkbox hides its native input behind the mark.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectPage();

    expect(
      await screen.findByRole("heading", { name: "Models" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Browse catalog" }));

    expect(
      await screen.findByRole("button", { name: "Hide catalog" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /No linked workflows are contributing project-relevant agents yet/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Provider filter" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Model filter" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /All 2/i }));
    await user.click(
      screen.getAllByRole("button", { name: "Add override" })[0],
    );

    expect(await screen.findByRole("button", { name: "Remove override" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /^Provider override for / })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /^Model override for / })).toBeInTheDocument();
  });

  it("localizes linked-workflow reference details without translating the referenced agent identifier", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchProjectWorkflowBindings.mockResolvedValue({ workflow_ids: ["owner_workflow"], operator_default_workflow_id: "", project_workflows: [{ workflow_id: "owner_workflow", enabled: true }] } as never);
    mockedFetchWorkflows.mockResolvedValue([{ workflow_id: "owner_workflow", display_name: "Owner workflow", enabled: true }] as never);
    mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "owner_workflow", enabled: true, steps: [{ step_id: "plan", agent_id: "planner" }] } as never);
    renderProjectPage("/projects/project-123?tab=integrations", "lt");
    await user.click(await screen.findByRole("button", { name: "Browse catalog" }));
    expect(await screen.findByText("Naudojamas susietose darbo eigose")).toBeInTheDocument();
    expect(screen.queryByText("Referenced by linked workflows")).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Toggle planner" })).toBeInTheDocument();
  });

  it("localizes the empty agent catalog state while preserving the typed filter", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchProjectDetail.mockResolvedValue({
      id: "project-123", name: "Owner project", workspace_id: "ws-1",
      data_connection_ids: [], assigned_data_connections: [], available_data_connections: [],
      available_providers: [], available_models: [], providers_with_models: { provider_models: {} },
      default_agent_models: {}, agent_model_overrides: {},
    } as never);
    renderProjectPage("/projects/project-123?tab=integrations", "lt");
    await user.click(await screen.findByRole("button", { name: "Browse catalog" }));
    const search = screen.getByPlaceholderText("Agent name, provider, or model");
    await user.type(search, "owner_unmatched_filter");
    expect(screen.getByText("Nė vienas agentas neatitinka dabartinių filtrų.")).toBeInTheDocument();
    expect(screen.queryByText("No agents match the current filters.")).not.toBeInTheDocument();
    expect(search).toHaveValue("owner_unmatched_filter");
    expect(mockedUpdateProject).not.toHaveBeenCalled();
  });

  it("blocks save when a provider is selected without a model", async () => {
    // The published checkbox hides its native input behind the mark.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectPage();

    await screen.findByRole("heading", { name: "Models" });
    await chooseOption(user, "Default provider", "OpenAI");

    expect(screen.getByRole("combobox", { name: "Default model" })).toHaveTextContent(
      "Use project/provider default",
    );
    expect(screen.getByRole("button", { name: "Save Project" })).toBeDisabled();
  });

  it("saves an extension provider/model pair as the project default", async () => {
    // The published checkbox hides its native input behind the mark.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchProjectDetail.mockResolvedValue({
      id: "project-123",
      name: "Audit Project",
      description: "Audit project",
      workspace_id: "ws-1",
      data_connection_ids: [],
      assigned_data_connections: [],
      available_data_connections: [],
      available_providers: [
        ["openai", "OpenAI"],
        ["llmext_acme_llm_deepseek_abc123", "DeepSeek (Acme LLM)"],
      ],
      available_models: ["gpt-4o", "deepseek/deepseek-chat"],
      providers_with_models: {
        provider_models: {
          openai: [{ model_id: "gpt-4o" }],
          llmext_acme_llm_deepseek_abc123: [
            { model_id: "deepseek/deepseek-chat", label: "DeepSeek Chat" },
          ],
        },
      },
      default_provider: "openai",
      default_model: "gpt-4o",
      default_agent_models: {},
      agent_model_overrides: {},
    } as never);
    renderProjectPage();

    expect(
      await screen.findByRole("heading", { name: "Models" }),
    ).toBeInTheDocument();
    // Extension provider group renders with its canonical display label.
    await chooseOption(user, "Default provider", "DeepSeek (Acme LLM)");
    await chooseOption(user, "Default model", "DeepSeek Chat");
    const routingSummary = screen.getByTestId("project-model-routing-summary");
    expect(
      within(routingSummary).getByText("DeepSeek (Acme LLM)"),
    ).toBeInTheDocument();
    expect(
      within(routingSummary).getByText("DeepSeek Chat"),
    ).toBeInTheDocument();
    expect(
      within(routingSummary).queryByText("deepseek/deepseek-chat"),
    ).not.toBeInTheDocument();
    expect(
      within(routingSummary).queryByText("llmext_acme_llm_deepseek_abc123"),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save Project" }));

    await waitFor(() => {
      expect(mockedUpdateProject).toHaveBeenCalled();
    });
    const [, payload] = mockedUpdateProject.mock.calls[0];
    expect(payload).toMatchObject({
      default_provider: "llmext_acme_llm_deepseek_abc123",
      default_model: "deepseek/deepseek-chat",
    });
  });

  it("does not expose explicit models while the project provider is inherited", async () => {
    mockedFetchProjectDetail.mockResolvedValue({
      id: "project-123",
      name: "Audit Project",
      workspace_id: "ws-1",
      data_connection_ids: [],
      assigned_data_connections: [],
      available_data_connections: [],
      available_providers: [["openai", "OpenAI"]],
      available_models: ["gpt-5-mini"],
      providers_with_models: {
        provider_models: { openai: [{ model_id: "gpt-5-mini" }] },
      },
      default_provider: "",
      default_model: "",
      default_agent_models: {},
      agent_model_overrides: {},
    } as never);

    renderProjectPage();

    // The published checkbox hides its native input behind the mark.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await user.click(await screen.findByRole("combobox", { name: "Default model" }));
    // Only the placeholder: an inherited provider exposes no explicit models.
    expect(screen.queryAllByRole("option")).toEqual([]);
  });

  it("marks a persisted project model outside routable options as unavailable", async () => {
    mockedFetchProjectDetail.mockResolvedValue({
      id: "project-123",
      name: "Audit Project",
      workspace_id: "ws-1",
      data_connection_ids: [],
      assigned_data_connections: [],
      available_data_connections: [],
      available_providers: [["openai", "OpenAI"]],
      providers_with_models: {
        provider_models: { openai: [{ model_id: "gpt-5.6-sol" }] },
      },
      default_provider: "openai",
      default_model: "gpt-4o",
      default_agent_models: {},
      agent_model_overrides: {},
    } as never);

    renderProjectPage();

    // The published checkbox hides its native input behind the mark.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await user.click(await screen.findByRole("combobox", { name: "Default model" }));
    expect(await screen.findByRole("option", { name: "gpt-4o (unavailable)" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.getByText(/saved project model route is unavailable/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save Project" })).toBeDisabled();
  });

  it("preserves both halves of a persisted route when its provider is unavailable", async () => {
    mockedFetchProjectDetail.mockResolvedValue({
      id: "project-123",
      name: "Audit Project",
      workspace_id: "ws-1",
      data_connection_ids: [],
      assigned_data_connections: [],
      available_data_connections: [],
      available_providers: [["openai", "OpenAI"]],
      providers_with_models: {
        provider_models: { openai: [{ model_id: "gpt-5.6-sol" }] },
      },
      default_provider: "custom_provider",
      default_model: "gpt-5.5",
      default_agent_models: {},
      agent_model_overrides: {},
    } as never);

    renderProjectPage();

    // The published checkbox hides its native input behind the mark.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const providerSelect = await screen.findByRole("combobox", { name: "Default provider" });
    expect(providerSelect).toHaveTextContent("custom_provider (unavailable)");

    await user.click(providerSelect);
    expect(
      await screen.findByRole("option", { name: "custom_provider (unavailable)" }),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");

    await user.click(screen.getByRole("combobox", { name: "Default model" }));
    expect(await screen.findByRole("option", { name: "gpt-5.5 (unavailable)" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Save Project" })).toBeDisabled();
  });

  it("preserves an unavailable provider used only by a persisted agent override", async () => {
    mockedFetchProjectDetail.mockResolvedValue({
      id: "project-123",
      name: "Audit Project",
      workspace_id: "ws-1",
      data_connection_ids: [],
      assigned_data_connections: [],
      available_data_connections: [],
      available_providers: [["openai", "OpenAI"]],
      providers_with_models: {
        provider_models: { openai: [{ model_id: "gpt-5.6-sol" }] },
      },
      default_provider: "openai",
      default_model: "gpt-5.6-sol",
      default_agent_models: {
        planner: { provider: "openai", model: "gpt-5.6-sol" },
      },
      agent_model_overrides: {
        planner: { provider: "llmext_removed", model: "removed/model" },
      },
    } as never);

    renderProjectPage();

    // The published checkbox hides its native input behind the mark.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const overrideProvider = await screen.findByRole("combobox", {
      name: /^Provider override for /,
    });
    expect(overrideProvider).toHaveTextContent("llmext_removed (unavailable)");
    await user.click(overrideProvider);
    expect(
      await screen.findByRole("option", { name: "llmext_removed (unavailable)" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save Project" })).toBeDisabled();
  });

  it("clears the previous project save error after a successful retry", async () => {
    // The published checkbox hides its native input behind the mark.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedUpdateProject
      .mockRejectedValueOnce(new Error("Unknown project configuration field: agent_model_override"))
      .mockResolvedValueOnce({ status: "updated" } as never);
    renderProjectPage("/projects/project-123?tab=overview");

    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save Project" }));
    expect(
      await screen.findByText("Unknown project configuration field: agent_model_override"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save Project" }));
    expect(await screen.findByText("Project updated successfully.")).toBeInTheDocument();
    expect(
      screen.queryByText("Unknown project configuration field: agent_model_override"),
    ).not.toBeInTheDocument();
    expect(mockedUpdateProject).toHaveBeenCalledTimes(2);
  });

  it("blocks save when project name is blank", async () => {
    // The published checkbox hides its native input behind the mark.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectPage("/projects/project-123?tab=overview");

    const nameInput = await screen.findByRole("textbox", { name: "Name" });
    await user.clear(nameInput);
    await user.click(screen.getByRole("button", { name: "Save Project" }));

    expect(
      await screen.findByText("Project name is required."),
    ).toBeInTheDocument();
    expect(mockedUpdateProject).not.toHaveBeenCalled();
  });

  it("clears an unlinked operator default instead of inventing a disabled replacement and saves", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    let bindings = {
      workflow_ids: ["enabled-a", "disabled-b"], operator_default_workflow_id: "enabled-a",
      project_workflows: [
        { workflow_id: "enabled-a", enabled: true, is_default: true },
        { workflow_id: "disabled-b", enabled: false, is_default: false },
      ],
    };
    mockedFetchProjectWorkflowBindings.mockImplementation(async () => bindings as never);
    mockedFetchWorkflows.mockResolvedValue([
      { workflow_id: "enabled-a", display_name: "Enabled A", enabled: true },
      { workflow_id: "disabled-b", display_name: "Disabled B", enabled: false },
    ] as never);
    mockedUpdateProject.mockImplementation(async (_projectId, payload) => {
      if (payload.operator_default_workflow_id) throw new Error("Disabled workflow cannot be operator default");
      bindings = {
        workflow_ids: ["disabled-b"], operator_default_workflow_id: "",
        project_workflows: [{ workflow_id: "disabled-b", enabled: false, is_default: false }],
      };
      return { status: "updated" };
    });
    renderProjectPage("/projects/project-123?tab=workflows");
    const enabled = await screen.findByRole("checkbox", { name: "Toggle Enabled A" });
    await waitFor(() => expect(enabled).toBeChecked());
    expect(screen.getByRole("checkbox", { name: "Toggle Disabled B" })).toBeChecked();
    expect(screen.getByRole("combobox", { name: "Operator default workflow" })).toHaveTextContent("Enabled A");
    await user.click(enabled);
    expect(screen.getByRole("combobox", { name: "Operator default workflow" })).toHaveTextContent("No operator default");
    expect(screen.getByRole("checkbox", { name: "Toggle Disabled B" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Save Project" }));
    expect(await screen.findByText("Project updated successfully.")).toBeVisible();
    expect(mockedUpdateProject.mock.calls[0]?.[1]).toMatchObject({
      workflow_ids: ["disabled-b"], operator_default_workflow_id: "",
    });
  });

  it("preserves the operator default when unlinking an unrelated workflow", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchProjectWorkflowBindings.mockResolvedValue({
      workflow_ids: ["enabled-a", "disabled-b"], operator_default_workflow_id: "enabled-a",
      project_workflows: [{ workflow_id: "enabled-a", enabled: true, is_default: true }, { workflow_id: "disabled-b", enabled: false }],
    } as never);
    mockedFetchWorkflows.mockResolvedValue([
      { workflow_id: "enabled-a", display_name: "Enabled A", enabled: true },
      { workflow_id: "disabled-b", display_name: "Disabled B", enabled: false },
    ] as never);
    renderProjectPage("/projects/project-123?tab=workflows");
    const unrelated = await screen.findByRole("checkbox", { name: "Toggle Disabled B" });
    await waitFor(() => expect(unrelated).toBeChecked());
    await user.click(unrelated);
    expect(screen.getByRole("combobox", { name: "Operator default workflow" })).toHaveTextContent("Enabled A");
    await user.click(screen.getByRole("button", { name: "Save Project" }));
    expect(await screen.findByText("Project updated successfully.")).toBeVisible();
    expect(mockedUpdateProject.mock.calls[0]?.[1]).toMatchObject({
      workflow_ids: ["enabled-a"], operator_default_workflow_id: "enabled-a",
    });
  });

  it("lets an operator explicitly choose No operator default without unlinking workflows", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchProjectWorkflowBindings.mockResolvedValue({
      workflow_ids: ["enabled-a"], operator_default_workflow_id: "enabled-a",
      project_workflows: [{ workflow_id: "enabled-a", enabled: true, is_default: true }],
    } as never);
    mockedFetchWorkflows.mockResolvedValue([{ workflow_id: "enabled-a", display_name: "Enabled A", enabled: true }] as never);
    renderProjectPage("/projects/project-123?tab=workflows");
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Operator default workflow" })).toHaveTextContent("Enabled A"));
    await openSelect(user, "Operator default workflow");
    const emptyOption = await screen.findByRole("option", { name: "No operator default" });
    emptyOption.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("checkbox", { name: "Toggle Enabled A" })).toBeChecked();
    expect(screen.getByRole("combobox", { name: "Operator default workflow" })).toHaveTextContent("No operator default");
    await user.click(screen.getByRole("button", { name: "Save Project" }));
    expect(await screen.findByText("Project updated successfully.")).toBeVisible();
    expect(mockedUpdateProject.mock.calls[0]?.[1]).toMatchObject({
      workflow_ids: ["enabled-a"], operator_default_workflow_id: "",
    });
  });

  it("drops a stale operator default workflow when saving new workflow bindings", async () => {
    // The checkbox contract visually hides the native input behind its
    // label, so the pointer-events guard has to stand down here.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchProjectWorkflowBindings.mockResolvedValue({
      workflow_ids: [],
      operator_default_workflow_id: "eval_legacy_agent_summary",
      project_workflows: [],
    } as never);
    mockedFetchWorkflows.mockResolvedValue([
      {
        workflow_id: "weekly_report",
        display_name: "Weekly Report",
        enabled: true,
      },
    ] as never);
    renderProjectPage("/projects/project-123?tab=workflows");

    expect(
      await screen.findByRole("heading", { name: "Workflows" }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("checkbox", { name: "Toggle Weekly Report" }),
    );
    expect(
      screen.getByRole("combobox", { name: /Operator default workflow/ }),
    ).toHaveValue("");

    await user.click(screen.getByRole("button", { name: "Save Project" }));

    await waitFor(() => {
      expect(mockedUpdateProject).toHaveBeenCalled();
    });
    const [, payload] = mockedUpdateProject.mock.calls[0];
    expect(payload).toMatchObject({
      workflow_ids: ["weekly_report"],
      operator_default_workflow_id: "",
    });
  });

  it(
    "saves and reloads all ten extension workflows through the full project update",
    async () => {
      // The published checkbox hides its native input behind the mark.
      const user = userEvent.setup({ pointerEventsCheck: 0 });
      const workflowIds = Array.from(
        { length: 10 },
        (_, index) => `extension.install-demo.demo_${index}`,
      );
      mockedFetchWorkflows.mockResolvedValue(
        workflowIds.map((workflowId, index) => ({
          workflow_id: workflowId,
          display_name: `Acme Showcase ${index}`,
          enabled: true,
        })) as never,
      );
      const utils = renderProjectPage(
        "/projects/project-123?tab=workflows",
      );

      expect(
        await screen.findByRole("heading", { name: "Workflows" }),
      ).toBeInTheDocument();
      for (let index = 0; index < workflowIds.length; index += 1) {
        await user.click(
          screen.getByRole("checkbox", {
            name: `Toggle Acme Showcase ${index}`,
          }),
        );
      }
      await chooseOption(user, "Operator default workflow", "Acme Showcase 4");
      await user.click(screen.getByRole("button", { name: "Save Project" }));

      await waitFor(() => expect(mockedUpdateProject).toHaveBeenCalledTimes(1));
      const [, payload] = mockedUpdateProject.mock.calls[0];
      expect(payload).toMatchObject({
        description: "Audit project for UI/runtime verification",
        workflow_ids: workflowIds,
        operator_default_workflow_id: workflowIds[4],
      });

      mockedFetchProjectWorkflowBindings.mockResolvedValue({
        workflow_ids: workflowIds,
        operator_default_workflow_id: workflowIds[4],
        project_workflows: workflowIds.map((workflowId, index) => ({
          workflow_id: workflowId,
          enabled: true,
          is_default: index === 4,
        })),
      } as never);
      utils.unmount();
      renderProjectPage("/projects/project-123?tab=workflows");

      for (let index = 0; index < workflowIds.length; index += 1) {
        expect(
          await screen.findByRole("checkbox", {
            name: `Toggle Acme Showcase ${index}`,
          }),
        ).toBeChecked();
      }
    },
    10_000,
  );

  it("blocks save and never requests detail while a stale workflow binding stays selected", async () => {
    // The published checkbox hides its native input behind the mark.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchProjectWorkflowBindings.mockResolvedValue({
      workflow_ids: ["weekly_report", "Acme Blog 1"],
      operator_default_workflow_id: "Acme Blog 1",
      project_workflows: [
        { workflow_id: "weekly_report", enabled: true },
        { workflow_id: "Acme Blog 1", enabled: true },
      ],
    } as never);
    mockedFetchWorkflows.mockResolvedValue([
      {
        workflow_id: "weekly_report",
        display_name: "Weekly Report",
        enabled: true,
      },
    ] as never);

    renderProjectPage("/projects/project-123?tab=workflows");

    expect(
      await screen.findByRole("heading", { name: "Workflows" }),
    ).toBeInTheDocument();
    // Stale binding renders as a still-selected row carrying a "missing" badge.
    expect(await screen.findByText("missing")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "Toggle Acme Blog 1" }),
    ).toBeChecked();

    // The stale operator default is cleared and never offered as an option.
    await waitFor(() => {
      expect(
        screen.getByRole("combobox", { name: /Operator default workflow/ }),
      ).toHaveValue("");
    });
    expect(
      screen.queryByRole("option", { name: "Acme Blog 1" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save Project" }));

    expect(mockedUpdateProject).not.toHaveBeenCalled();
    expect(
      await screen.findByText(
        'This project references missing workflow "Acme Blog 1". Remove it before saving project changes.',
      ),
    ).toBeInTheDocument();
    expect(mockedFetchWorkflowDetail).not.toHaveBeenCalledWith("Acme Blog 1");
  });

  it("allows save once the stale workflow row is unselected, sending only valid ids", async () => {
    // The checkbox contract visually hides the native input behind its
    // label, so the pointer-events guard has to stand down here.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchProjectWorkflowBindings.mockResolvedValue({
      workflow_ids: ["weekly_report", "Acme Blog 2"],
      operator_default_workflow_id: "Acme Blog 2",
      project_workflows: [
        { workflow_id: "weekly_report", enabled: true },
        { workflow_id: "Acme Blog 2", enabled: true },
      ],
    } as never);
    mockedFetchWorkflows.mockResolvedValue([
      {
        workflow_id: "weekly_report",
        display_name: "Weekly Report",
        enabled: true,
      },
    ] as never);

    renderProjectPage("/projects/project-123?tab=workflows");

    expect(
      await screen.findByRole("heading", { name: "Workflows" }),
    ).toBeInTheDocument();
    const staleToggle = await screen.findByRole("checkbox", {
      name: "Toggle Acme Blog 2",
    });
    expect(staleToggle).toBeChecked();
    await user.click(staleToggle);

    await user.click(screen.getByRole("button", { name: "Save Project" }));

    await waitFor(() => {
      expect(mockedUpdateProject).toHaveBeenCalled();
    });
    const [, payload] = mockedUpdateProject.mock.calls[0];
    expect(payload).toMatchObject({
      workflow_ids: ["weekly_report"],
      operator_default_workflow_id: "",
    });
    expect((payload as { workflow_ids: string[] }).workflow_ids).not.toContain(
      "Acme Blog 2",
    );
    expect(mockedFetchWorkflowDetail).not.toHaveBeenCalledWith("Acme Blog 2");
  });

  it("remembers the edited project for extension connection selection", async () => {
    renderProjectPage("/projects/a8ed4486-c855-4c08-a4d5-8c0851d7603b");

    expect(
      await screen.findByRole("heading", { name: "Overview" }),
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(localStorage.getItem("platform-last-project-id")).toBe("a8ed4486-c855-4c08-a4d5-8c0851d7603b");
    });
  });

  it("saves ordinary project settings without retired Extra Tools input", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectPage();

    await screen.findByRole("tablist", { name: "Project sections" });
    expect(screen.queryByRole("tab", { name: "Advanced" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Extra tools JSON" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save Project" }));
    await waitFor(() => expect(mockedUpdateProject).toHaveBeenCalled());
    const submitted = mockedUpdateProject.mock.calls.at(-1)?.[1];
    expect(submitted).not.toHaveProperty("extra_tools_json");
    expect(submitted).toHaveProperty("workspace_id");
  });

  it("falls back to Overview for an old Advanced URL and can save ordinary settings", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectPage("/projects/project-123?tab=advanced");

    await screen.findByRole("tablist", { name: "Project sections" });
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("tab", { name: "Advanced" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Extra tools JSON" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save Project" }));
    await waitFor(() => expect(mockedUpdateProject).toHaveBeenCalled());
    expect(mockedUpdateProject.mock.calls.at(-1)?.[1]).not.toHaveProperty("extra_tools_json");
  });

  it("lists exactly the project sections, in order, with History as a tab and no Scheduled jobs or Queue", async () => {
    renderProjectPage("/projects/project-123");

    const tablist = await screen.findByRole("tablist", { name: "Project sections" });
    const tabs = within(tablist).getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      "Overview",
      "Workflows",
      "Data Connections",
      "Policy",
      "Models",
      "Notifications",
      "Runtime Connections",
      "Datasets",
      "History",
    ]);
    expect(screen.queryByRole("tab", { name: "Scheduled jobs" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Queue" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Scheduled jobs" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Queue" })).not.toBeInTheDocument();
  });

  it("ends the Project summary without links back to the page it is on", async () => {
    renderProjectPage("/projects/project-123");

    expect(await screen.findByRole("heading", { name: "Project summary" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open project" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "View jobs" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open KB" })).not.toBeInTheDocument();
  });

  it("opens the History tab in the same tab set and offers no Save there", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectPage("/projects/project-123");

    await user.click(await screen.findByRole("tab", { name: "History" }));

    expect(screen.getByRole("tab", { name: "History" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("history-panel project-123")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save Project" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Project summary" })).not.toBeInTheDocument();
  });

  it.each([
    ["history", "History"],
    ["workflows", "Workflows"],
    ["data-connections", "Data Connections"],
    ["datasets", "Datasets"],
  ])("selects the %s tab from ?tab=", async (tab, label) => {
    renderProjectPage(`/projects/project-123?tab=${tab}`);

    expect(await screen.findByRole("tab", { name: label })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "false");
  });

  it("shows the read-only notification overview on the Notifications tab, with no Save", async () => {
    renderProjectPage("/projects/project-123?tab=notifications");

    expect(await screen.findByText("notification-overview project-123")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Notifications" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("button", { name: "Save Project" })).not.toBeInTheDocument();
  });

  it("shows a role without mutate_projects the data read-only, with no Save", async () => {
    mockedUseHasCapability.mockImplementation((capability) => capability !== "mutate_projects");
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectPage("/projects/project-123");

    const name = await screen.findByRole("textbox", { name: "Name" });
    expect(name).toHaveValue("Audit Project");
    expect(name).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Description" })).toBeDisabled();
    expect(screen.getByText("Your role can view this project but not change it.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save Project" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();

    // Every section stays readable: the tabs themselves are not form controls.
    await user.click(screen.getByRole("tab", { name: "Models" }));
    expect(screen.getByRole("tab", { name: "Models" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("combobox", { name: "Default provider" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save Project" })).not.toBeInTheDocument();
    fireEvent.submit(screen.getByRole("form", { name: "Project edit form" }));
    await act(async () => undefined);
    expect(mockedUpdateProject).not.toHaveBeenCalled();
  });

  it("discards unsaved changes in place on Cancel", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectPage("/projects/project-123");

    const name = await screen.findByRole("textbox", { name: "Name" });
    await user.clear(name);
    await user.type(name, "Renamed project");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(await screen.findByRole("textbox", { name: "Name" })).toHaveValue("Audit Project");
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(mockedUpdateProject).not.toHaveBeenCalled();
  });

  it("opens the data connections tab from a deep link", async () => {
    renderProjectPage("/projects/project-123?tab=data-connections");
    expect(
      await screen.findByRole("heading", { name: "Data Connections" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Project data connections/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "Data Connections" })).toHaveAttribute("aria-selected", "true");
      expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "false");
    });
  });

  it("keeps data connections editable and sends no code fields", async () => {
    // The published checkbox hides its native input behind the mark.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchProjectDetail.mockResolvedValue({
      id: "project-123",
      name: "General Project",
      description: "General-purpose project",
      workspace_id: "ws-1",
      data_connection_ids: ["warehouse-primary"],
      assigned_data_connections: [
        {
          connection_id: "warehouse-primary",
          display_name: "Warehouse primary",
          source_kind: "postgres",
          enabled: true,
          health_status: "healthy",
        },
      ],
      available_data_connections: [
        {
          connection_id: "warehouse-primary",
          display_name: "Warehouse primary",
          source_kind: "postgres",
          enabled: true,
          health_status: "healthy",
        },
      ],
      available_providers: [["openai", "OpenAI"]],
      available_models: ["gpt-4.1-nano", "gpt-5-mini"],
      providers_with_models: {
        provider_models: {
          openai: [{ model_id: "gpt-4.1-nano" }, { model_id: "gpt-5-mini" }],
        },
      },
      default_agent_models: {
        planner: { provider: "openai", model: "gpt-4.1-nano" },
      },
      agent_model_overrides: {},
    } as never);

    renderProjectPage("/projects/project-123");

    expect(await screen.findByRole("tab", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Data Connections" })).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Data Connections" }));
    await user.click(screen.getByRole("checkbox", { name: "Toggle Warehouse primary" }));
    await user.click(screen.getByRole("button", { name: "Save Project" }));

    await waitFor(() => expect(mockedUpdateProject).toHaveBeenCalled());
    const [, payload] = mockedUpdateProject.mock.calls[0];
    expect(payload).toMatchObject({
      data_connection_ids: [],
    });
    expect(payload).not.toHaveProperty("project_type");
    expect(payload).not.toHaveProperty("project_root");
    expect(payload).not.toHaveProperty("repo_url");
    expect(payload).not.toHaveProperty("auto_index_enabled");
  });

  it("localizes the assigned data connection count without changing owner identifiers", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const connection = {
      connection_id: "warehouse-primary",
      display_name: "Warehouse primary",
      source_kind: "postgres",
      enabled: true,
      health_status: "healthy",
    };
    mockedFetchProjectDetail.mockResolvedValue({
      id: "project-123",
      name: "Owner project",
      workspace_id: "ws-1",
      data_connection_ids: [connection.connection_id],
      assigned_data_connections: [connection],
      available_data_connections: [connection],
      available_providers: [],
      available_models: [],
      providers_with_models: { provider_models: {} },
      default_agent_models: {},
      agent_model_overrides: {},
    } as never);

    renderProjectPage("/projects/project-123?tab=data-connections", "lt");

    const toggle = await screen.findByRole("checkbox", { name: "Toggle Warehouse primary" });
    expect(await screen.findByText("Priskirta: 1")).toBeVisible();
    expect(toggle).toBeChecked();
    await user.click(toggle);
    expect(screen.getByText("Priskirta: 0")).toBeVisible();
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: "Save Project" }));

    await waitFor(() => expect(mockedUpdateProject).toHaveBeenCalledWith(
      "project-123",
      expect.objectContaining({ data_connection_ids: ["warehouse-primary"] }),
    ));
  });

  it("says on the Policy tab that the execution policy saves on its own", async () => {
    renderProjectPage("/projects/project-123?tab=policy");

    expect(await screen.findByText("policy-editor")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Save Project saves Project rules. The execution policy below saves with its own Save policy button.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByText("Saving applies changes from every section of this form."),
    ).not.toBeInTheDocument();
  });

  it("keeps the loading state inside the page container", async () => {
    mockedFetchProjectDetail.mockReturnValue(new Promise(() => undefined));

    renderProjectPage();

    const loadingText = await screen.findByText("Loading project settings…");
    expect(
      within(screen.getByTestId("page-container")).getByText("Loading project settings…"),
    ).toBe(loadingText);
  });
});
