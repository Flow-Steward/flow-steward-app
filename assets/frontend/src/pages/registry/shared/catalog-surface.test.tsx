import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";

import { RegistryCatalogSurface } from "@/pages/registry/shared/catalog-surface";
import { fetchJson } from "@/lib/http/client";
import {
  createRegistryEntity,
  deleteMcpProjectedToolConfirmed,
  deleteRegistryEntity,
  fetchMcpProjectedToolDeletePlan,
  fetchRegistryList,
  toggleRegistryEntity,
} from "@/pages/registry/shared/api";
import { useHasCapability } from "@/lib/auth/capabilities";

// These page journeys take about 6 s when the quality gate shares the machine,
// over vitest's 5 s default. Per-test timeouts set below still take precedence.
vi.setConfig({ testTimeout: 10_000 });

vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn() }));

vi.mock("@/pages/registry/shared/api", () => ({
  createRegistryEntity: vi.fn(),
  deleteMcpProjectedToolConfirmed: vi.fn(),
  deleteRegistryEntity: vi.fn(),
  fetchMcpProjectedToolDeletePlan: vi.fn(),
  fetchRegistryList: vi.fn(),
  toggleRegistryEntity: vi.fn(),
}));

vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

const mockedCreateRegistryEntity = vi.mocked(createRegistryEntity);
const mockedDeleteMcpProjectedToolConfirmed = vi.mocked(deleteMcpProjectedToolConfirmed);
const mockedDeleteRegistryEntity = vi.mocked(deleteRegistryEntity);
const mockedFetchRegistryList = vi.mocked(fetchRegistryList);
const mockedFetchMcpProjectedToolDeletePlan = vi.mocked(fetchMcpProjectedToolDeletePlan);
const mockedToggleRegistryEntity = vi.mocked(toggleRegistryEntity);
const mockedFetchJson = vi.mocked(fetchJson);
const mockedUseHasCapability = vi.mocked(useHasCapability);

type RenderCatalogOptions = {
  locale?: "en" | "lt";
  createLabel?: string;
  description?: string;
  endpoint?: string;
  idKey?: string;
  listGridId?: string;
  listQueryKey?: string[];
  routerBasename?: string;
  routerEntry?: string;
  title?: string;
};


function renderCatalogSurface({
  createLabel = "Guardrail",
  description = "Guardrail catalog",
  endpoint = "/api/guardrails",
  idKey = "id",
  listGridId,
  listQueryKey = ["registry", "guardrails"],
  routerBasename,
  routerEntry = "/registry/guardrails",
  title = "Guardrails catalog",
  locale = "en",
}: RenderCatalogOptions = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  const view = render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, t: (key, text = "", params) => fallbackContext.t(key, locale === "lt" ? ({
      "settings.extensions.enabled": "įjungta",
      "settings.notifications.status.disabled": "išjungta",
      "settings.mcp.status.enabled": "Įjungta",
      "react.registry.disabled": "Išjungta",
      "extensions.runtime.status": "Būsena",
    } as Record<string, string>)[key] || text : text, params) }}><QueryClientProvider client={queryClient}>
      <MemoryRouter basename={routerBasename} initialEntries={[routerEntry]}>
        <RegistryCatalogSurface
          createLabel={createLabel}
          description={description}
          endpoint={endpoint}
          idKey={idKey}
          listGridId={listGridId}
          listQueryKey={listQueryKey}
          title={title}
        />
      </MemoryRouter>
    </QueryClientProvider></I18nContext.Provider>,
  );
  return { ...view, queryClient };
}

/** Three tool rows: two enabled, one disabled, all separately searchable. */
const TOOLBAR_ROWS = [
  { tool_id: "alpha_probe", display_name: "Alpha Probe", enabled: true },
  { tool_id: "beta_probe", display_name: "Beta Probe", enabled: false },
  { tool_id: "gamma_scan", display_name: "Gamma Scan", enabled: true },
];

function renderToolCatalog() {
  mockedFetchRegistryList.mockResolvedValue(TOOLBAR_ROWS);
  return renderCatalogSurface({
    createLabel: "Tool",
    description: "Tool catalog",
    endpoint: "/api/tools",
    idKey: "tool_id",
    listGridId: "tools-grid",
    listQueryKey: ["registry", "tools"],
    title: "Tools catalog",
  });
}

/** Picks a value from the published Select: open the trigger, then the option. */
async function chooseOption(comboboxName: string, optionName: string) {
  fireEvent.click(screen.getByRole("combobox", { name: comboboxName }));
  fireEvent.click(await screen.findByRole("option", { name: optionName }));
}

describe("guardrail Create paused enumeration", () => {
  it("blocks creation during an offline paused refetch while retaining cached selections", async () => {
    const agentRows = [{ agent_id: "writer", display_name: "Writer" }];
    const queryKey = ["registry", "guardrail-create-agents", undefined] as const;
    mockedCreateRegistryEntity.mockReset();
    mockedUseHasCapability.mockReturnValue(true);
    mockedFetchRegistryList.mockReset();
    mockedFetchRegistryList.mockImplementation(async (endpoint) =>
      endpoint === "/api/agents?usage=guardrail" ? agentRows : [],
    );
    const wasOnline = onlineManager.isOnline();
    const { queryClient } = renderCatalogSurface();
    try {
      onlineManager.setOnline(true);
      fireEvent.click(screen.getByRole("button", { name: "New Guardrail" }));
      fireEvent.change(screen.getByLabelText(/^Display Name/), { target: { value: "Paused scope" } });
      fireEvent.change(screen.getByLabelText("Pattern 1"), { target: { value: "FORBIDDEN" } });
      fireEvent.click(await screen.findByRole("checkbox", { name: /Toggle Writer/i }));
      await waitFor(() => expect(screen.getByRole("button", { name: /Create entry/i })).toBeEnabled());
      expect(queryClient.getQueryState(queryKey)).toMatchObject({
        status: "success", fetchStatus: "idle", data: agentRows,
      });
      const readsBeforePause = mockedFetchRegistryList.mock.calls.length;
      await act(async () => {
        onlineManager.setOnline(false);
        await queryClient.refetchQueries({ queryKey, exact: true });
      });
      expect(queryClient.getQueryState(queryKey)).toMatchObject({
        status: "success", fetchStatus: "paused", data: agentRows,
      });
      expect(mockedFetchRegistryList).toHaveBeenCalledTimes(readsBeforePause);
      expect(screen.getByText("writer", { exact: true })).toBeVisible();
      await waitFor(() => {
        expect(screen.getByRole("button", { name: /Create entry/i })).toBeDisabled();
        expect(screen.getByRole("checkbox", { name: /Toggle Writer/i })).toBeDisabled();
      });
      fireEvent.submit(screen.getByTestId("registry-create-form"));
      expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();
      expect(screen.getByText("writer", { exact: true })).toBeVisible();
    } finally {
      // A failed RED assertion must never leave the global query manager offline.
      onlineManager.setOnline(wasOnline);
      queryClient.clear();
    }
  });
});

describe("RegistryCatalogSurface", () => {
  it("keeps enabled tool filter membership when status labels are Lithuanian", async () => {
    mockedFetchRegistryList.mockResolvedValue(TOOLBAR_ROWS);
    renderCatalogSurface({ locale: "lt", createLabel: "Tool", endpoint: "/api/tools", idKey: "tool_id", listGridId: "tools-grid", listQueryKey: ["registry", "tools"], title: "Tools catalog" });
    await screen.findByRole("link", { name: "Alpha Probe" });
    await addCatalogFilter(userEvent.setup(), "Būsena", "Įjungta");
    expect(screen.getByRole("link", { name: "Alpha Probe" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Gamma Scan" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Beta Probe" })).not.toBeInTheDocument();
    expect(TOOLBAR_ROWS[0].enabled).toBe(true);
  });

  beforeEach(() => {
    mockedUseHasCapability.mockReturnValue(true);
    mockedCreateRegistryEntity.mockReset();
    mockedDeleteRegistryEntity.mockReset();
    mockedDeleteMcpProjectedToolConfirmed.mockReset();
    mockedFetchMcpProjectedToolDeletePlan.mockReset();
    mockedFetchRegistryList.mockReset();
    mockedToggleRegistryEntity.mockReset();
    mockedFetchJson.mockReset();
    mockedFetchJson.mockResolvedValue({
      provider_models: {},
      provider_options: [],
    });
    mockedDeleteMcpProjectedToolConfirmed.mockResolvedValue({ status: "deleted" });

    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/agents" || endpoint === "/api/agents?usage=guardrail") {
        return [
          { agent_id: "writer", display_name: "Writer" },
          { agent_id: "reviewer", display_name: "Reviewer" },
        ];
      }
      return [];
    });
    mockedCreateRegistryEntity.mockResolvedValue({
      guardrail_id: "no-secrets",
      display_name: "No Secrets",
    });
  });

  it("keeps operator-readable catalogs read-only", async () => {
    mockedUseHasCapability.mockReturnValue(false);
    renderCatalogSurface({ createLabel: "Skill", endpoint: "/api/skills" });

    expect(screen.queryByRole("button", { name: "+ New Skill" })).not.toBeInTheDocument();
    expect(await screen.findByTestId("registry-read-only-notice")).toBeInTheDocument();
  });

  it("includes the router basename in native grid detail links", async () => {
    renderCatalogSurface({
      createLabel: "Agent",
      description: "Agent catalog",
      endpoint: "/api/agents",
      idKey: "agent_id",
      listGridId: "agents-grid",
      listQueryKey: ["registry", "agents"],
      routerBasename: "/app",
      routerEntry: "/app/registry/agents",
      title: "Agents catalog",
    });

    // Read off the rendered link rather than off the config handed to a web
    // component: the basename has to survive all the way into the href.
    const link = await screen.findByRole("link", { name: "Writer" });
    expect(link).toHaveAttribute("href", "/app/registry/agents/writer");
  });

  it("uses an application confirmation dialog before deleting a catalog row", async () => {
    renderCatalogSurface({
      createLabel: "Agent",
      description: "Agent catalog",
      endpoint: "/api/agents",
      idKey: "agent_id",
      listGridId: "agents-grid",
      listQueryKey: ["registry", "agents"],
      title: "Agents catalog",
    });

    const user = userEvent.setup();
    const trigger = await screen.findByRole("button", { name: "Actions for writer" });
    await user.click(trigger);
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));

    expect(await screen.findByRole("alertdialog", { name: "Delete writer" })).toBeInTheDocument();
    expect(screen.getByText("This moves writer to Trash.")).toBeInTheDocument();
    expect(mockedDeleteRegistryEntity).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => {
      expect(mockedDeleteRegistryEntity).toHaveBeenCalledWith("/api/agents", "writer");
    });
  });

  it("returns focus to the delete action after cancelling the dialog", async () => {
    const user = userEvent.setup();
    renderCatalogSurface({
      createLabel: "Agent",
      description: "Agent catalog",
      endpoint: "/api/agents",
      idKey: "agent_id",
      listGridId: "agents-grid",
      listQueryKey: ["registry", "agents"],
      title: "Agents catalog",
    });

    const trigger = await screen.findByRole("button", { name: "Actions for writer" });
    await user.click(trigger);
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));

    expect(await screen.findByRole("alertdialog", { name: "Delete writer" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("opens a non-blocking clone dialog and validates the new agent id", async () => {
    mockedFetchJson.mockResolvedValue({ agent_id: "writer_copy" });
    renderCatalogSurface({
      createLabel: "Agent",
      description: "Agent catalog",
      endpoint: "/api/agents",
      idKey: "agent_id",
      listGridId: "agents-grid",
      listQueryKey: ["registry", "agents"],
      title: "Agents catalog",
    });

    const user = userEvent.setup();
    const trigger = await screen.findByRole("button", { name: "Actions for writer" });
    await user.click(trigger);
    await user.click(await screen.findByRole("menuitem", { name: "Clone" }));

    expect(await screen.findByRole("dialog", { name: "Clone writer" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("New ID"), { target: { value: "BAD ID" } });
    fireEvent.click(screen.getByRole("button", { name: "Clone" }));
    expect(await screen.findByText(/Agent ID must start with a lowercase letter/i)).toBeInTheDocument();
    expect(mockedFetchJson.mock.calls.some(([url]) => String(url).includes("/clone"))).toBe(false);
  });

  it("requires a registered type when cloning an agent with an unsupported role type", async () => {
    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/agents") {
        return [{ agent_id: "extension.install-1.source_specialist", display_name: "Source specialist", role_type: "source_specialist" }];
      }
      if (endpoint === "/api/agent-types") {
        return [{ key: "researcher", label: "Researcher", enabled: true }];
      }
      return [];
    });
    mockedFetchJson.mockResolvedValue({ agent_id: "source_specialist_copy" });
    renderCatalogSurface({
      createLabel: "Agent",
      description: "Agent catalog",
      endpoint: "/api/agents",
      idKey: "agent_id",
      listGridId: "agents-grid",
      listQueryKey: ["registry", "agents"],
      title: "Agents catalog",
    });

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for extension.install-1.source_specialist" }));
    await user.click(await screen.findByRole("menuitem", { name: "Clone" }));

    expect(await screen.findByText("The source agent type is missing or isn't in the supported catalog. Choose a type for the copy.")).toBeVisible();
    await user.type(screen.getByLabelText("New ID"), "source_specialist_copy");
    await user.click(screen.getByRole("button", { name: "Clone" }));
    expect(await screen.findByText("Select an agent type before cloning this source.")).toBeVisible();
    expect(mockedFetchJson.mock.calls.some(([url]) => String(url).includes("/clone"))).toBe(false);

    await chooseOption("Agent type for copy", "Researcher");
    await user.click(screen.getByRole("button", { name: "Clone" }));
    await waitFor(() => {
      expect(mockedFetchJson.mock.calls.some(([url]) => String(url).includes("/clone"))).toBe(true);
    });
    const cloneCall = mockedFetchJson.mock.calls.find(([url]) => String(url).includes("/clone"));
    expect(JSON.parse(String(cloneCall?.[1]?.body))).toEqual({
      agent_id: "source_specialist_copy",
      agent_type_id: "researcher",
    });
  });

  it("preserves the source type without requiring a clone-time type change", async () => {
    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/agents") {
        return [{ agent_id: "writer", display_name: "Writer", agent_type_key: "researcher" }];
      }
      if (endpoint === "/api/agent-types") {
        return [{ key: "researcher", label: "Researcher", enabled: true }];
      }
      return [];
    });
    mockedFetchJson.mockResolvedValue({ agent_id: "writer_copy" });
    renderCatalogSurface({
      createLabel: "Agent",
      description: "Agent catalog",
      endpoint: "/api/agents",
      idKey: "agent_id",
      listGridId: "agents-grid",
      listQueryKey: ["registry", "agents"],
      title: "Agents catalog",
    });

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for writer" }));
    await user.click(await screen.findByRole("menuitem", { name: "Clone" }));
    await user.type(screen.getByLabelText("New ID"), "writer_copy");
    await user.click(screen.getByRole("button", { name: "Clone" }));

    await waitFor(() => {
      expect(mockedFetchJson.mock.calls.some(([url]) => String(url).includes("/clone"))).toBe(true);
    });
    const cloneCall = mockedFetchJson.mock.calls.find(([url]) => String(url).includes("/clone"));
    expect(JSON.parse(String(cloneCall?.[1]?.body))).toEqual({ agent_id: "writer_copy" });
  });

  it("returns focus to the clone action after cancelling the dialog", async () => {
    const user = userEvent.setup();
    renderCatalogSurface({
      createLabel: "Agent",
      description: "Agent catalog",
      endpoint: "/api/agents",
      idKey: "agent_id",
      listGridId: "agents-grid",
      listQueryKey: ["registry", "agents"],
      title: "Agents catalog",
    });

    const trigger = await screen.findByRole("button", { name: "Actions for writer" });
    await user.click(trigger);
    await user.click(await screen.findByRole("menuitem", { name: "Clone" }));

    expect(await screen.findByRole("dialog", { name: "Clone writer" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("creates guardrails from supported rule choices without internal identifiers or raw JSON", async () => {
    renderCatalogSurface();

    fireEvent.click(screen.getByRole("button", { name: "New Guardrail" }));
    const form = screen.getByTestId("registry-create-form");
    expect(screen.queryByLabelText(/^ID/)).not.toBeInTheDocument();

    fireEvent.submit(form);
    expect(await screen.findAllByText("Display name is required.")).not.toHaveLength(0);
    expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/^Display Name/), {
      target: { value: "No Secrets" },
    });
    expect(screen.getByRole("combobox", { name: "Rule Type" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Config JSON")).not.toBeInTheDocument();
    expect(screen.getByText(/Python regular expression/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Pattern 1"), { target: { value: "secret|password" } });
    fireEvent.submit(form);

    expect(await screen.findAllByText("Select at least one agent.")).not.toHaveLength(0);
    expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByRole("checkbox", { name: "Select visible rows" }));
    fireEvent.submit(form);

    await waitFor(() => {
      expect(mockedCreateRegistryEntity).toHaveBeenCalledWith(
        "/api/guardrails",
        expect.objectContaining({
          display_name: "No Secrets",
          rule_type: "forbidden_patterns",
          config: { patterns: ["secret|password"] },
          agents: ["reviewer", "writer"],
        }),
      );
    });
    const payload = mockedCreateRegistryEntity.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(payload).not.toHaveProperty("id");
    expect(payload).not.toHaveProperty("guardrail_id");
  });

  it("switches guardrail create fields by mode and rule type", async () => {
    renderCatalogSurface();
    fireEvent.click(screen.getByRole("button", { name: "New Guardrail" }));

    await chooseOption("Rule Type", "Required sections");
    expect(screen.getByLabelText("Section 1")).toBeInTheDocument();
    expect(screen.queryByLabelText("Pattern 1")).not.toBeInTheDocument();

    await chooseOption("Mode", "Semantic");
    expect(screen.getByRole("combobox", { name: "Rule Type" })).toHaveTextContent("AI-evaluated condition");
    expect(screen.getByLabelText("AI condition")).toBeInTheDocument();
    expect(screen.queryByLabelText("Section 1")).not.toBeInTheDocument();
  });

  it("shows a field-specific agent id error before submitting malformed ids", async () => {
    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/agent-types") {
        return [{ key: "writer", label: "Writer" }];
      }
      return [];
    });
    renderCatalogSurface({
      createLabel: "Agent",
      description: "Agent catalog",
      endpoint: "/api/agents",
      idKey: "agent_id",
      listQueryKey: ["registry", "agents"],
      title: "Agents catalog",
    });

    fireEvent.click(screen.getByRole("button", { name: "New Agent" }));
    const form = screen.getByTestId("registry-create-form");

    fireEvent.change(screen.getByLabelText(/^AGENT_ID/), {
      target: { value: "BAD TEST bad id!@#" },
    });
    await chooseOption("Agent Type", "Writer");
    fireEvent.change(screen.getByLabelText(/^System Prompt/), {
      target: { value: "Analyze the request." },
    });
    fireEvent.submit(form);

    expect(
      await screen.findAllByText(
        "Agent ID must start with a lowercase letter and use only lowercase letters, numbers, and underscores.",
      ),
    ).not.toHaveLength(0);
    expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();
  });

  it("blocks agent creation and offers retry when model catalog loading fails", async () => {
    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/agent-types") {
        return [{ key: "writer", label: "Writer" }];
      }
      return [];
    });
    mockedFetchJson
      .mockRejectedValueOnce(new Error("catalog offline"))
      .mockResolvedValueOnce({
        provider_models: { openai: [{ model_id: "gpt-5.6-sol", label: "GPT-5.6 Sol" }] },
        provider_options: [{ provider_id: "openai", label: "OpenAI" }],
      });
    renderCatalogSurface({
      createLabel: "Agent",
      description: "Agent catalog",
      endpoint: "/api/agents",
      idKey: "agent_id",
      listQueryKey: ["registry", "agents"],
      title: "Agents catalog",
    });

    fireEvent.click(screen.getByRole("button", { name: "New Agent" }));

    expect(await screen.findByText("Model catalog is unavailable.")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Create entry" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Retry model catalog" }));

    // The retry re-enables the Select; its options only exist while it is open.
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Model" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("combobox", { name: "Model" }));
    expect(await screen.findByRole("option", { name: "GPT-5.6 Sol (OpenAI)" })).toBeInTheDocument();
    expect(screen.queryByText("Model catalog is unavailable.")).not.toBeInTheDocument();
  });

  it("requires tool id and display name before creating a tool", async () => {
    renderCatalogSurface({
      createLabel: "Tool",
      description: "Tool catalog",
      endpoint: "/api/tools",
      idKey: "tool_id",
      listQueryKey: ["registry", "tools"],
      title: "Tools catalog",
    });

    fireEvent.click(screen.getByRole("button", { name: "New Tool" }));
    const form = screen.getByTestId("registry-create-form");

    fireEvent.submit(form);
    expect(await screen.findAllByText("Tool ID is required.")).not.toHaveLength(0);
    expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/^TOOL_ID/), {
      target: { value: "BAD TEST bad id!@#" },
    });
    fireEvent.submit(form);
    expect(
      await screen.findAllByText(
        "Tool ID must start with a lowercase letter and use only lowercase letters, numbers, underscores, and dots between segments.",
      ),
    ).not.toHaveLength(0);
    expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/^TOOL_ID/), {
      target: { value: "std.review_tool" },
    });
    fireEvent.submit(form);
    expect(await screen.findAllByText("Display name is required.")).not.toHaveLength(0);
    expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/^Display Name/), {
      target: { value: "Review Tool" },
    });
    fireEvent.submit(form);

    await waitFor(() => {
      expect(mockedCreateRegistryEntity).toHaveBeenCalledWith(
        "/api/tools",
        expect.objectContaining({
          tool_id: "std.review_tool",
          display_name: "Review Tool",
        }),
      );
    });
  });

  it("hides unavailable tool reasons from the grid and does not invoke Enable", async () => {
    mockedFetchRegistryList.mockResolvedValue([
      {
        tool_id: "orphaned_tool",
        display_name: "Orphaned Tool",
        enabled: false,
        availability_status: "unavailable",
        availability_reason_code: "source_connector_removed",
        availability_reason_text: "Source MCP connector mcp_removed was deleted.",
      },
    ]);
    renderCatalogSurface({
      createLabel: "Tool",
      description: "Tool catalog",
      endpoint: "/api/tools",
      idKey: "tool_id",
      listGridId: "tools-grid",
      listQueryKey: ["registry", "tools"],
      title: "Tools catalog",
    });

    const user = userEvent.setup();
    expect(await screen.findByText("Orphaned Tool")).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Availability" })).not.toBeInTheDocument();
    expect(screen.queryByText("Source MCP connector mcp_removed was deleted.")).not.toBeInTheDocument();
    expect(screen.getByText("unavailable")).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: /^Actions for / }));
    expect(await screen.findByRole("menuitem", { name: "Delete" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Enable/ })).not.toBeInTheDocument();

    await waitFor(() => expect(mockedToggleRegistryEntity).not.toHaveBeenCalled());
  });

  it("keeps Enable available for legacy tool rows without availability metadata", async () => {
    mockedFetchRegistryList.mockResolvedValue([
      {
        tool_id: "available_tool",
        display_name: "Available Tool",
        enabled: false,
      },
    ]);
    renderCatalogSurface({
      createLabel: "Tool",
      description: "Tool catalog",
      endpoint: "/api/tools",
      idKey: "tool_id",
      listGridId: "tools-grid",
      listQueryKey: ["registry", "tools"],
      title: "Tools catalog",
    });

    const user = userEvent.setup();
    await screen.findByRole("table", { name: "Tool inventory" });
    await user.click(await screen.findByRole("button", { name: /^Actions for / }));
    await user.click(await screen.findByRole("menuitem", { name: /Enable/ }));

    await waitFor(() => {
      expect(mockedToggleRegistryEntity).toHaveBeenCalledWith("/api/tools", "available_tool");
    });
  });

  it("uses the revision-bound impact plan for MCP projected tool deletion", async () => {
    const user = userEvent.setup();
    mockedFetchRegistryList.mockResolvedValue([
      {
        tool_id: "fs.catalog.read",
        display_name: "Catalog Read",
        implementation: { type: "mcp", connector_id: "mcp_fs" },
      },
    ]);
    mockedFetchMcpProjectedToolDeletePlan.mockResolvedValue({
      tool_id: "fs.catalog.read",
      connector_id: "mcp_fs",
      affected: { workflows: [{ workflow_id: "wf_catalog", display_name: "Catalog Flow" }] },
      warnings: ["Workflow remains unchanged."],
      revision: "revision-1",
    });
    renderCatalogSurface({
      createLabel: "Tool",
      description: "Tool catalog",
      endpoint: "/api/tools",
      idKey: "tool_id",
      listGridId: "tools-grid",
      listQueryKey: ["registry", "tools"],
      title: "Tools catalog",
    });
    await screen.findByRole("table", { name: "Tool inventory" });

    await user.click(await screen.findByRole("button", { name: /^Actions for / }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    expect(await screen.findByRole("dialog", { name: "Move MCP tool to Trash" })).toBeInTheDocument();
    expect(screen.getByText("Catalog Flow")).toBeInTheDocument();
    expect(mockedDeleteRegistryEntity).not.toHaveBeenCalled();
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Move to Trash" }));

    await waitFor(() => {
      expect(mockedDeleteMcpProjectedToolConfirmed).toHaveBeenCalledWith(
        "fs.catalog.read",
        "revision-1",
      );
    });
  });

  it("requires skill id before creating a skill", async () => {
    renderCatalogSurface({
      createLabel: "Skill",
      description: "Skill catalog",
      endpoint: "/api/skills",
      idKey: "skill_id",
      listQueryKey: ["registry", "skills"],
      title: "Skills catalog",
    });

    fireEvent.click(screen.getByRole("button", { name: "New Skill" }));
    const form = screen.getByTestId("registry-create-form");
    fireEvent.submit(form);

    expect(await screen.findAllByText("Skill ID is required.")).not.toHaveLength(0);
    expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/^SKILL_ID/), {
      target: { value: "BAD TEST bad id!@#" },
    });
    fireEvent.submit(form);

    expect(
      await screen.findAllByText(
        "Skill ID must start with a lowercase letter and use only lowercase letters, numbers, underscores, and dots between segments.",
      ),
    ).not.toHaveLength(0);
    expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();
  });
  it("narrows the catalog to the rows matching the search", async () => {
    renderToolCatalog();
    const user = userEvent.setup();
    expect(await screen.findByText("Alpha Probe")).toBeInTheDocument();

    await user.type(
      screen.getByRole("searchbox", { name: "Search tool entries" }),
      "gamma",
    );

    await waitFor(() => {
      expect(screen.queryByText("Alpha Probe")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Gamma Scan")).toBeInTheDocument();
  });

  it("explains an empty result instead of leaving a bare table", async () => {
    renderToolCatalog();
    const user = userEvent.setup();
    await screen.findByText("Alpha Probe");

    await user.type(
      screen.getByRole("searchbox", { name: "Search tool entries" }),
      "no row carries this",
    );

    // The empty state has to count *visible* rows. Counting fetched ones would
    // leave a search that matches nothing looking like a broken table.
    expect(await screen.findByTestId("tools-grid")).toBeInTheDocument();
    expect(screen.queryByText("Alpha Probe")).not.toBeInTheDocument();
  });

  it("filters the catalog by status and restores every row when cleared", async () => {
    renderToolCatalog();
    const user = userEvent.setup();
    await screen.findByText("Alpha Probe");

    await user.click(screen.getByRole("button", { name: "Add filter" }));
    await user.click(await screen.findByRole("menuitem", { name: "Status" }));
    await user.click(await screen.findByRole("menuitem", { name: "Disabled" }));

    await waitFor(() => {
      expect(screen.queryByText("Alpha Probe")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Beta Probe")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear all" }));

    expect(await screen.findByText("Alpha Probe")).toBeInTheDocument();
    expect(screen.getByText("Gamma Scan")).toBeInTheDocument();
  });

  async function addCatalogFilter(user: ReturnType<typeof userEvent.setup>, field: string, value: string) {
    await user.click(screen.getByRole("button", { name: "Add filter" }));
    await user.click(await screen.findByRole("menuitem", { name: field }));
    await user.click(await screen.findByRole("menuitem", { name: value }));
  }

  function expectNoCatalogMutation() {
    expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();
    expect(mockedDeleteRegistryEntity).not.toHaveBeenCalled();
    expect(mockedDeleteMcpProjectedToolConfirmed).not.toHaveBeenCalled();
    expect(mockedToggleRegistryEntity).not.toHaveBeenCalled();
  }

  it("combines agent category, model and status over the complete catalog and clears them independently", async () => {
    const rows = [
      ...Array.from({ length: 27 }, (_, index) => ({
        agent_id: `filler_agent_${index}`,
        display_name: `Agent filler ${index + 1}`,
        enabled: true, category: "research", config: { model: "model-a" },
      })),
      { agent_id: "agent_selected", display_name: "Agent selected", enabled: true, category: "writing", config: { model: "model-b" } },
      { agent_id: "agent_disabled", display_name: "Agent disabled", enabled: false, category: "writing", config: { model: "model-b" } },
      { agent_id: "agent_other_model", display_name: "Agent other model", enabled: true, category: "writing", config: { model: "model-a" } },
      { agent_id: "agent_unclassified", display_name: "Agent unclassified", enabled: true, category: " ", config: {} },
    ];
    const originalRows = structuredClone(rows);
    mockedFetchRegistryList.mockImplementation(async (endpoint) => endpoint === "/api/agents" ? rows : []);
    renderCatalogSurface({
      createLabel: "Agent", endpoint: "/api/agents", idKey: "agent_id",
      listGridId: "agents-grid", listQueryKey: ["registry", "agents"], title: "Agents catalog",
    });
    const user = userEvent.setup();
    await screen.findByText("31 of 31");
    await addCatalogFilter(user, "Category", "writing");
    expect(await screen.findByText("3 of 31")).toBeInTheDocument();
    await addCatalogFilter(user, "Model", "model-b");
    expect(await screen.findByText("2 of 31")).toBeInTheDocument();
    await addCatalogFilter(user, "Status", "Enabled");
    expect(await screen.findByText("1 of 31")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Agent selected" })).toHaveAttribute("href", "/registry/agents/agent_selected");
    expect(screen.queryByRole("link", { name: "Agent disabled" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Agent other model" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Agent unclassified" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove Model filter" }));
    expect(await screen.findByText("2 of 31")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Agent other model" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear all" }));
    expect(await screen.findByText("31 of 31")).toBeInTheDocument();
    // Search after reset proves blank category/model rows were not dropped from
    // the underlying catalog. No invented category or model is persisted.
    await user.type(screen.getByRole("searchbox", { name: "Search agent entries" }), "Agent unclassified");
    expect(await screen.findByText("1 of 31")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Agent unclassified" })).toBeInTheDocument();
    expect(rows).toEqual(originalRows);
    expectNoCatalogMutation();
  });

  it.each([
    { implementation: "Internal", status: "Enabled", expectedName: "Internal integration", expectedId: "tool_internal" },
    { implementation: "MCP", status: "Enabled", expectedName: "MCP integration", expectedId: "tool_mcp" },
    { implementation: "Native", status: "Enabled", expectedName: "Native integration", expectedId: "tool_native" },
    { implementation: "MCP", status: "Disabled", expectedName: "Disabled MCP integration", expectedId: "tool_mcp_disabled" },
  ])("combines tool category, $implementation and $status without losing empty legacy rows", async ({ implementation, status, expectedName, expectedId }) => {
    const rows = [
      ...Array.from({ length: 27 }, (_, index) => ({
        tool_id: `filler_tool_${index}`,
        display_name: `Tool filler ${index + 1}`,
        enabled: true, category: "utility", implementation: { type: "internal" },
      })),
      { tool_id: "tool_internal", display_name: "Internal integration", enabled: true, category: "integration", implementation: { type: "internal" } },
      { tool_id: "tool_mcp", display_name: "MCP integration", enabled: true, category: "integration", implementation: { type: "mcp" } },
      { tool_id: "tool_native", display_name: "Native integration", enabled: true, category: "integration", implementation: { type: "native" } },
      { tool_id: "tool_mcp_disabled", display_name: "Disabled MCP integration", enabled: false, category: "integration", implementation: { type: "mcp" } },
      { tool_id: "tool_unclassified", display_name: "Tool unclassified", enabled: false, category: "", implementation: {} },
    ];
    const originalRows = structuredClone(rows);
    mockedFetchRegistryList.mockImplementation(async (endpoint) => endpoint === "/api/tools" ? rows : []);
    renderCatalogSurface({
      createLabel: "Tool", endpoint: "/api/tools", idKey: "tool_id",
      listGridId: "tools-grid", listQueryKey: ["registry", "tools"], title: "Tools catalog",
    });
    const user = userEvent.setup();
    await screen.findByText("32 of 32");
    expect(screen.queryByRole("columnheader", { name: "Cost Tier" })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Availability" })).not.toBeInTheDocument();
    for (const name of ["Tool", "Category", "Implementation", "Status"]) {
      expect(screen.getByRole("columnheader", { name })).toBeVisible();
    }
    await addCatalogFilter(user, "Category", "integration");
    expect(await screen.findByText("4 of 32")).toBeInTheDocument();
    await addCatalogFilter(user, "Implementation", implementation);
    await addCatalogFilter(user, "Status", status);
    expect(await screen.findByText("1 of 32")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: expectedName })).toHaveAttribute("href", `/registry/tools/${expectedId}`);
    for (const otherName of ["Internal integration", "MCP integration", "Native integration", "Disabled MCP integration", "Tool unclassified"]) {
      if (otherName !== expectedName) expect(screen.queryByRole("link", { name: otherName })).not.toBeInTheDocument();
    }
    await user.click(screen.getByRole("button", { name: "Clear all" }));
    expect(await screen.findByText("32 of 32")).toBeInTheDocument();
    await user.type(screen.getByRole("searchbox", { name: "Search tool entries" }), "Tool unclassified");
    expect(await screen.findByText("1 of 32")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Tool unclassified" })).toBeInTheDocument();
    expect(rows).toEqual(originalRows);
    expectNoCatalogMutation();
  });

  it("combines skill category and disabled status and preserves an empty legacy category after reset", async () => {
    const rows = [
      ...Array.from({ length: 27 }, (_, index) => ({
        skill_id: `filler_skill_${index}`,
        display_name: `Skill filler ${index + 1}`,
        enabled: true, category: "analysis",
      })),
      { skill_id: "skill_writing", display_name: "Writing skill", enabled: true, category: "writing" },
      { skill_id: "skill_disabled", display_name: "Disabled writing skill", enabled: false, category: "writing" },
      { skill_id: "skill_unclassified", display_name: "Skill unclassified", enabled: true, category: "" },
    ];
    const originalRows = structuredClone(rows);
    mockedFetchRegistryList.mockImplementation(async (endpoint) => endpoint === "/api/skills" ? rows : []);
    renderCatalogSurface({
      createLabel: "Skill", endpoint: "/api/skills", idKey: "skill_id",
      listGridId: "skills-grid", listQueryKey: ["registry", "skills"], title: "Skills catalog",
    });
    const user = userEvent.setup();
    await screen.findByText("30 of 30");
    await addCatalogFilter(user, "Category", "writing");
    expect(await screen.findByText("2 of 30")).toBeInTheDocument();
    await addCatalogFilter(user, "Status", "Disabled");
    expect(await screen.findByText("1 of 30")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Disabled writing skill" })).toHaveAttribute("href", "/registry/skills/skill_disabled");
    expect(screen.queryByRole("link", { name: "Writing skill" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Skill unclassified" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear all" }));
    expect(await screen.findByText("30 of 30")).toBeInTheDocument();
    await user.type(screen.getByRole("searchbox", { name: "Search skill entries" }), "Skill unclassified");
    expect(await screen.findByText("1 of 30")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Skill unclassified" })).toBeInTheDocument();
    expect(rows).toEqual(originalRows);
    expectNoCatalogMutation();
  });

  it("names the table through DsTable, not a heading in the toolbar", async () => {
    renderToolCatalog();
    await screen.findByRole("table", { name: "Tool inventory" });

    // No other list in the app puts a heading inside `.fs-table-toolbar`, and the
    // page title already says what the table lists.
    expect(
      screen.queryByRole("heading", { name: "Catalog entries" }),
    ).not.toBeInTheDocument();
  });
  it("creates guardrails only with the backend's guardrail-eligible agents, retaining installed durable choices", async () => {
    const manual = { agent_id: "writer", display_name: "Own Writer" };
    const installed = { agent_id: "extension.guard-install.bridge", display_name: "Installed Durable Writer", source_install_id: "guard-install" };
    const shared = { agent_id: "shared", display_name: "Shared Reader" };
    const virtual = { agent_id: "extension.guard-install.virtual", display_name: "Virtual Showcase Reader", source_install_id: "guard-install" };
    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/agents?usage=guardrail") return [manual, installed];
      if (endpoint === "/api/agents") return [manual, installed, shared, virtual];
      return [];
    });
    renderCatalogSurface();
    fireEvent.click(screen.getByRole("button", { name: "New Guardrail" }));
    fireEvent.change(screen.getByLabelText(/^Display Name/), { target: { value: "Owned policy" } });
    fireEvent.change(screen.getByLabelText("Pattern 1"), { target: { value: "FORBIDDEN" } });
    const writer = await screen.findByRole("checkbox", { name: /Toggle Own Writer/i });
    expect(screen.queryByRole("checkbox", { name: /Shared Reader/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Virtual Showcase Reader/i })).not.toBeInTheDocument();
    const bridge = screen.getByRole("checkbox", { name: /Toggle Installed Durable Writer/i });
    fireEvent.click(writer);
    fireEvent.click(bridge);
    fireEvent.submit(screen.getByTestId("registry-create-form"));
    await waitFor(() => expect(mockedCreateRegistryEntity).toHaveBeenCalled());
    expect(mockedFetchRegistryList).toHaveBeenCalledWith("/api/agents?usage=guardrail");
    const [endpoint, payload] = mockedCreateRegistryEntity.mock.calls[0];
    expect(endpoint).toBe("/api/guardrails");
    expect(payload).toMatchObject({ display_name: "Owned policy", config: { patterns: ["FORBIDDEN"] } });
    expect((payload as Record<string, unknown>).agents).toEqual(expect.arrayContaining(["writer", installed.agent_id]));
    expect((payload as Record<string, unknown>).agents).toHaveLength(2);
  });

  it("does not fall back to the ordinary catalog when guardrail eligibility cannot load", async () => {
    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/agents?usage=guardrail") throw new Error("Agent choices could not be loaded.");
      if (endpoint === "/api/agents") return [{ agent_id: "shared", display_name: "Shared Reader" }];
      return [];
    });
    renderCatalogSurface();
    fireEvent.click(screen.getByRole("button", { name: "New Guardrail" }));
    fireEvent.change(screen.getByLabelText(/^Display Name/), { target: { value: "Owned policy" } });
    fireEvent.change(screen.getByLabelText("Pattern 1"), { target: { value: "FORBIDDEN" } });
    await waitFor(() => expect(mockedFetchRegistryList).toHaveBeenCalledWith("/api/agents?usage=guardrail"));
    expect(screen.queryByRole("checkbox", { name: /Shared Reader/i })).not.toBeInTheDocument();
    fireEvent.submit(screen.getByTestId("registry-create-form"));
    expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();
    expect(screen.getAllByText(/could not be loaded|failed to load|unable to load/i).length).toBeGreaterThan(0);
  });

});


describe("guardrail Create enumeration authority", () => {
  it("retains a selection removed by refetch until explicit removal and blocks cached choices on error", async () => {
    let reads = 0;
    mockedCreateRegistryEntity.mockReset();
    mockedUseHasCapability.mockReturnValue(true);
    mockedFetchRegistryList.mockReset();
    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/agents?usage=guardrail") {
        reads += 1;
        if (reads === 1) return [{ agent_id: "writer", display_name: "Writer" }, { agent_id: "reviewer", display_name: "Reviewer" }];
        if (reads === 2) return [{ agent_id: "reviewer", display_name: "Reviewer" }];
        throw new Error("Agent choices could not be loaded.");
      }
      return [];
    });
    const { queryClient } = renderCatalogSurface();
    fireEvent.click(screen.getByRole("button", { name: "New Guardrail" }));
    fireEvent.change(screen.getByLabelText(/^Display Name/), { target: { value: "Retained create scope" } });
    fireEvent.change(screen.getByLabelText("Pattern 1"), { target: { value: "FORBIDDEN" } });
    fireEvent.click(await screen.findByRole("checkbox", { name: /Toggle Writer/i }));
    await queryClient.invalidateQueries({ queryKey: ["registry", "guardrail-create-agents"] });
    await waitFor(() => expect(screen.getByRole("button", { name: /Remove writer/i })).toBeVisible());
    expect(screen.getByRole("button", { name: /Create entry/i })).toBeDisabled();
    fireEvent.submit(screen.getByTestId("registry-create-form"));
    expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Remove writer/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Toggle Reviewer/i }));
    expect(screen.getByRole("button", { name: /Create entry/i })).toBeEnabled();
    await queryClient.invalidateQueries({ queryKey: ["registry", "guardrail-create-agents"] });
    await screen.findByText(/failed to load agent choices/i);
    expect(screen.getByRole("button", { name: /Create entry/i })).toBeDisabled();
    fireEvent.submit(screen.getByTestId("registry-create-form"));
    expect(mockedCreateRegistryEntity).not.toHaveBeenCalled();
  });
});
