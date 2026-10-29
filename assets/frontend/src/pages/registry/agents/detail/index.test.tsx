import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { fetchJson } from "@/lib/http/client";
import {
  replaceAgentRelationships,
} from "@/pages/registry/agents/detail/api";
import { fetchRegistryList } from "@/pages/registry/shared/api";
import {
  fetchRegistryDetail,
  patchRegistryEntity,
  toggleRegistryEntity,
} from "@/pages/registry/shared/api";
import { RegistryAgentsDetailPage } from "@/pages/registry/agents/detail";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import configPatchFixture from "@/test/fixtures/agent-config-patch.json";

vi.mock("@/pages/registry/shared/api", () => ({
  fetchRegistryDetail: vi.fn(),
  fetchRegistryList: vi.fn(),
  patchRegistryEntity: vi.fn(),
  toggleRegistryEntity: vi.fn(),
}));
vi.mock("@/pages/registry/agents/detail/api", () => ({
  replaceAgentRelationships: vi.fn(),
}));
vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

const mockedFetchJson = vi.mocked(fetchJson);
const mockedReplaceAgentRelationships = vi.mocked(replaceAgentRelationships);
const mockedFetchRegistryDetail = vi.mocked(fetchRegistryDetail);
const mockedFetchRegistryList = vi.mocked(fetchRegistryList);
const mockedPatchRegistryEntity = vi.mocked(patchRegistryEntity);
const mockedToggleRegistryEntity = vi.mocked(toggleRegistryEntity);

function renderPage(locale = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, t: (key, fallback = "", params) => fallbackContext.t(key, locale === "lt" ? ({
      "react.registry.identifier": "Identifikatorius",
      "settings.mcp.status.enabled": "Įjungta",
      "registry.data_contracts.column.version": "Versija",
      "react.workflows.updated": "Atnaujinta",
      "extensions.repeater.yes": "Taip",
      "extensions.repeater.no": "Ne",
      "trash.columns.category": "Kategorija",
      "react.registry.standard_tool_actions_connector_only": "Standartiniai įrankių veiksmai pasiekiami tik per jungčių darbo eigos žingsnius",
    } as Record<string, string>)[key] || fallback : fallback, params) }}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/registry/agents/sample_launch_copywriter"]}>
        <Routes>
          <Route
            element={<RegistryAgentsDetailPage />}
            path="/registry/agents/:agentId"
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider></I18nContext.Provider>,
  );
}

/**
 * `DsSelect` and `SingleCombobox` render a button plus a listbox, so there is no
 * `value`, no `.options` and nothing to `fireEvent.change`. Options exist only
 * while the menu is open, and the trigger's text is the selected option's label.
 */
function openSelect(name: string): HTMLElement {
  const trigger = screen.getByRole("combobox", { name });
  fireEvent.focus(trigger);
  fireEvent.click(trigger);
  return trigger;
}

function chooseOption(name: string, option: string | RegExp) {
  openSelect(name);
  fireEvent.click(screen.getByRole("option", { name: option }));
}

function selectedLabel(name: string): string {
  return (screen.getByRole("combobox", { name }).textContent || "").trim();
}

function optionLabels(name: string): string[] {
  const trigger = openSelect(name);
  const labels = screen.getAllByRole("option").map((option) => (option.textContent || "").trim());
  fireEvent.click(trigger);
  return labels;
}

describe("RegistryAgentsDetailPage", () => {
  beforeEach(() => {
    mockedFetchRegistryDetail.mockReset();
    mockedFetchRegistryList.mockReset();
    mockedReplaceAgentRelationships.mockReset();
    mockedPatchRegistryEntity.mockReset();
    mockedToggleRegistryEntity.mockReset();
    mockedFetchJson.mockReset();
    mockedFetchRegistryList.mockResolvedValue([]);
    mockedReplaceAgentRelationships.mockResolvedValue({ status: "updated" });
    mockedFetchJson.mockResolvedValue({
      provider_options: [
        {
          provider_id: "llmext_acme_llm_deepseek_abc123",
          label: "DeepSeek (Acme LLM)",
          source_kind: "extension_llm_provider",
        },
      ],
      all_models: [
        { model_id: "gpt-5" },
        { model_id: "claude-sonnet-4-6" },
        { model_id: "deepseek/deepseek-chat", provider_id: "llmext_acme_llm_deepseek_abc123" },
      ],
      provider_models: {
        anthropic: [{ model_id: "claude-sonnet-4-6" }],
        openai: [{ model_id: "gpt-5" }],
        llmext_acme_llm_deepseek_abc123: [
          { model_id: "deepseek/deepseek-chat", label: "DeepSeek Chat" },
        ],
      },
    });
  });

  it.each([[true, "Taip"], [false, "Ne"]])("localizes agent facts without changing the agent identifier when enabled is %s", async (enabled, enabledLabel) => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled,
      version: "user-version_42",
      config: {},
      prompts: {},
      skills: [],
      tools: [],
    } as never);

    renderPage("lt");
    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));

    for (const label of ["Identifikatorius", "Įjungta", "Versija", "Atnaujinta"]) {
      expect(screen.getByText(label, { selector: "dt" })).toBeInTheDocument();
    }
    expect(screen.getByText(enabledLabel, { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByText("sample_launch_copywriter", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByText("user-version_42", { selector: "dd" })).toBeInTheDocument();
  });

  it("localizes the category placeholder without changing the agent's category value", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({ agent_id: "sample_launch_copywriter", display_name: "Owner agent", enabled: true, category: "owner-category", config: {}, prompts: {}, skills: [], tools: [] } as never);
    renderPage("lt");
    await screen.findByText("Owner agent");
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));
    expect(screen.getByLabelText("Kategorija")).toHaveAttribute("placeholder", "Kategorija");
    expect(screen.getByLabelText("Kategorija")).toHaveValue("owner-category");
  });

  it("persists agent skills through relationship endpoints", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      base_skill_id: "copywriting",
      config: {},
      prompts: {},
      skills: [{ skill_id: "copywriting", display_name: "Copywriting" }],
      tools: [],
    } as never);
    mockedFetchRegistryList.mockImplementation(async (endpoint) =>
      endpoint === "/api/skills"
        ? [
            { skill_id: "copywriting", display_name: "Copywriting" },
            { skill_id: "brand_voice", display_name: "Brand Voice" },
          ]
        : [],
    );

    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Skills" }));
    fireEvent.click(screen.getByRole("checkbox", { name: new RegExp("Brand Voice") }));
    fireEvent.click(screen.getByRole("button", { name: "Save skills" }));

    await waitFor(() => {
      expect(mockedReplaceAgentRelationships).toHaveBeenCalledWith(
        "sample_launch_copywriter",
        { skills: ["copywriting"], tools: [] },
        { skills: ["copywriting", "brand_voice"], tools: [] },
      );
    });
    expect(mockedPatchRegistryEntity).not.toHaveBeenCalled();
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("reverts an unpersisted skill selection after a relationship save fails", async () => {
    mockedReplaceAgentRelationships.mockRejectedValue(new Error("Skill is incompatible with this agent."));
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: false,
      base_skill_id: "copywriting",
      config: {},
      prompts: {},
      skills: [{ skill_id: "copywriting", display_name: "Copywriting" }],
      tools: [],
    } as never);
    mockedFetchRegistryList.mockImplementation(async (endpoint) =>
      endpoint === "/api/skills"
        ? [
            { skill_id: "copywriting", display_name: "Copywriting" },
            { skill_id: "brand_voice", display_name: "Brand Voice" },
          ]
        : [],
    );

    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Skills" }));
    const secondary = screen.getByRole("checkbox", { name: new RegExp("Brand Voice") });
    fireEvent.click(secondary);
    expect(secondary).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Save skills" }));

    expect(await screen.findByText("Skill is incompatible with this agent.")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: new RegExp("Brand Voice") })).not.toBeChecked();
    // A rejected save is an error: announced, in the danger alert under the
    // header, with the server's full reason. It used to be a neutral badge in
    // the header's action slot, where a long reason squeezed the title.
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Changes were not saved");
    expect(alert).toHaveTextContent("Skill is incompatible with this agent.");
  });

  it("does not duplicate the base skill in secondary skills", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      base_skill: "copywriting",
      config: {},
      prompts: {},
      skills: [
        { skill_id: "copywriting", display_name: "Copywriting" },
        { skill_id: "brand_voice", display_name: "Brand Voice" },
      ],
      tools: [],
    } as never);
    mockedFetchRegistryList.mockImplementation(async (endpoint) =>
      endpoint === "/api/skills"
        ? [
            { skill_id: "copywriting", display_name: "Copywriting" },
            { skill_id: "brand_voice", display_name: "Brand Voice" },
          ]
        : [],
    );
    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Skills" }));
    expect(screen.queryByLabelText("Secondary skill Copywriting")).not.toBeInTheDocument();
    // SingleCombobox is a text input plus a listbox, so the option is chosen by
    // clicking it — the same pattern the Determinism select already uses.
    chooseOption("Base skill", "Brand Voice");
    fireEvent.click(screen.getByRole("button", { name: "Save skills" }));

    await waitFor(() => {
      expect(mockedReplaceAgentRelationships).toHaveBeenCalledWith(
        "sample_launch_copywriter",
        { skills: ["copywriting", "brand_voice"], tools: [] },
        { skills: ["brand_voice"], tools: [] },
      );
    });
  });

  it("shows a retryable model-catalog error without calling a saved route unavailable", async () => {
    mockedFetchJson.mockRejectedValue(new Error("catalog offline"));
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      config: { provider: "openai", model: "gpt-5" },
      prompts: {},
      skills: [],
      tools: [],
    } as never);
    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));
    expect(await screen.findByText("Could not load model options.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry model options" })).toBeInTheDocument();
    expect(screen.queryByText(/agent's model isn't verified/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save config" })).toBeDisabled();
  });

  it("persists agent tools through relationship endpoints", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      config: {},
      prompts: {},
      skills: [],
      tools: [{ tool_id: "workflow_ops", display_name: "Workflow Operations" }],
    } as never);
    mockedFetchRegistryList.mockImplementation(async (endpoint) =>
      endpoint === "/api/tools"
        ? [
            { tool_id: "workflow_ops", display_name: "Workflow Operations" },
            { tool_id: "sheet_ops", display_name: "Sheet Operations" },
          ]
        : [],
    );

    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
    fireEvent.click(screen.getByRole("checkbox", { name: new RegExp("Sheet Operations") }));
    fireEvent.click(screen.getByRole("button", { name: "Save tools" }));

    await waitFor(() => {
      expect(mockedReplaceAgentRelationships).toHaveBeenCalledWith(
        "sample_launch_copywriter",
        { skills: [], tools: ["workflow_ops"] },
        { skills: [], tools: ["sheet_ops", "workflow_ops"] },
      );
    });
    expect(mockedPatchRegistryEntity).not.toHaveBeenCalled();
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("shows virtual tools as unavailable and prevents new assignment", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: false,
      config: {},
      prompts: {},
      skills: [],
      tools: [],
    } as never);
    mockedFetchRegistryList.mockImplementation(async (endpoint) =>
      endpoint === "/api/tools"
        ? [
            {
              tool_id: "extension.install-1.catalog_tool",
              display_name: "Catalog Tool",
              enabled: true,
              agent_relationship_assignable: false,
              agent_relationship_reason_code: "tool_not_assignable_in_current_scope",
              agent_relationship_reason_text:
                "This tool is unavailable for assignment in the current project or tenant scope.",
            },
            {
              tool_id: "stable_tool",
              display_name: "Stable Tool",
              enabled: true,
              agent_relationship_assignable: true,
            },
            {
              tool_id: "manual_tool",
              display_name: "Manual Tool",
              enabled: true,
              agent_relationship_assignable: true,
            },
          ]
        : [],
    );

    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
    const projected = screen.getByRole("checkbox", {
      name: /Catalog Tool.*unavailable for assignment/i,
    });
    expect(projected).toBeDisabled();
    expect(
      screen.getByText(
        "This tool is unavailable for assignment in the current project or tenant scope.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Manual Tool/ })).toBeEnabled();
  });

  it.each([
    ["en", "Standard Tool Actions are available only through connector workflow steps"],
    ["lt", "Standartiniai įrankių veiksmai pasiekiami tik per jungčių darbo eigos žingsnius"],
  ])("shows the localized connector-only assignment reason in %s", async (locale, reason) => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: false,
      config: {},
      prompts: {},
      skills: [],
      tools: [],
    } as never);
    mockedFetchRegistryList.mockImplementation(async (endpoint) =>
      endpoint === "/api/tools"
        ? [{
            tool_id: "std.numbers",
            display_name: "Numbers",
            enabled: true,
            agent_relationship_assignable: false,
            agent_relationship_reason_code: "tool_connector_only",
            agent_relationship_reason_text:
              "Standard Tool Actions are available only through connector workflow steps",
          }]
        : [],
    );

    renderPage(locale);

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
    expect(screen.getByRole("checkbox", { name: /Numbers.*unavailable for assignment/i })).toBeDisabled();
    expect(screen.getByText(reason)).toBeInTheDocument();
    expect(screen.queryByText(/current project or tenant scope/i)).not.toBeInTheDocument();
  });

  it("lets an agent remove an existing unsupported projected tool", async () => {
    const projectedToolId = "extension.install-1.catalog_tool";
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: false,
      config: {},
      prompts: {},
      skills: [],
      tools: [
        { tool_id: projectedToolId, display_name: "Catalog Tool" },
        { tool_id: "stable_tool", display_name: "Stable Tool" },
      ],
    } as never);
    mockedFetchRegistryList.mockImplementation(async (endpoint) =>
      endpoint === "/api/tools"
        ? [
            {
              tool_id: projectedToolId,
              display_name: "Catalog Tool",
              enabled: true,
              agent_relationship_assignable: false,
              agent_relationship_reason_text:
                "This tool is unavailable for assignment in the current project or tenant scope.",
            },
            {
              tool_id: "manual_tool",
              display_name: "Manual Tool",
              enabled: true,
              agent_relationship_assignable: true,
            },
          ]
        : [],
    );

    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
    const projected = screen.getByRole("checkbox", {
      name: /Catalog Tool.*unavailable for assignment/i,
    });
    expect(projected).toBeChecked();
    expect(projected).toBeEnabled();
    fireEvent.click(projected);
    fireEvent.click(screen.getByRole("checkbox", { name: /Manual Tool/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save tools" }));

    await waitFor(() => {
      expect(mockedReplaceAgentRelationships).toHaveBeenCalledWith(
        "sample_launch_copywriter",
        { skills: [], tools: [projectedToolId, "stable_tool"] },
        { skills: [], tools: ["stable_tool", "manual_tool"] },
      );
    });
  });

  it("does not offer disabled skills or tools as new agent relationships", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: false,
      config: {},
      prompts: {},
      skills: [],
      tools: [],
    } as never);
    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/skills") {
        return [
          { skill_id: "copywriting", display_name: "Copywriting", enabled: true },
          { skill_id: "retired_skill", display_name: "Retired Skill", enabled: false },
        ];
      }
      if (endpoint === "/api/tools") {
        return [
          { tool_id: "workflow_ops", display_name: "Workflow Operations", enabled: true },
          { tool_id: "retired_tool", display_name: "Retired Tool", enabled: false },
        ];
      }
      return [];
    });

    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Skills" }));
    // A disabled catalog entry is not offered as a base skill and not offered as
    // a secondary one either.
    const baseSkillOptions = optionLabels("Base skill");
    expect(baseSkillOptions).toContain("Copywriting");
    expect(baseSkillOptions).not.toContain("Retired Skill");
    expect(
      screen.queryByRole("checkbox", { name: /Retired Skill/ }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
    expect(
      screen.getByRole("checkbox", {
        name: new RegExp("Workflow Operations"),
      }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Allowed tool Retired Tool")).not.toBeInTheDocument();
  });

  it("saves builder metadata for agent authoring guidance", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      description: "Writes launch assets",
      prompts: {
        system: "You write launch content.",
      },
      builder_metadata_jsonb: {
        purpose: "Old purpose",
        use_cases: ["old case"],
        anti_use_cases: ["old anti case"],
        composition_role: "old_role",
        determinism_level: "hybrid",
        safe_for_auto_use: false,
      },
      _skills: [],
      _tools: [],
      _inputs: [],
      _outputs: [],
    } as never);

    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Builder" }));

    fireEvent.change(
      screen.getByPlaceholderText(
        "What this primitive is for in workflow authoring.",
      ),
      {
        target: { value: "Generate audience-specific launch messaging." },
      },
    );
    fireEvent.change(screen.getByPlaceholderText("reasoning_step"), {
      target: { value: "content_generation" },
    });
    fireEvent.click(screen.getByRole("combobox", { name: "Determinism" }));
    fireEvent.click(screen.getByRole("option", { name: "agentic" }));
    fireEvent.change(screen.getByPlaceholderText("One use case per line"), {
      target: { value: "write launch emails\nwrite social captions" },
    });
    fireEvent.change(
      screen.getByPlaceholderText(
        "When workflow authoring should avoid using this primitive",
      ),
      {
        target: { value: "fixed numeric transforms\nsimple routing" },
      },
    );
    fireEvent.click(screen.getAllByRole("checkbox").at(-1)!);
    fireEvent.click(screen.getByRole("button", { name: "Save Builder metadata" }));

    await waitFor(() => {
      expect(mockedPatchRegistryEntity).toHaveBeenCalledWith(
        "/api/agents",
        "sample_launch_copywriter",
        expect.objectContaining({
          builder_metadata_jsonb: {
            purpose: "Generate audience-specific launch messaging.",
            use_cases: ["write launch emails", "write social captions"],
            anti_use_cases: ["fixed numeric transforms", "simple routing"],
            composition_role: "content_generation",
            determinism_level: "agentic",
            safe_for_auto_use: true,
          },
        }),
      );
    });
  });

  it("persists config category and one canonical agent type field", async () => {
    const agent = {
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      category: "marketing",
      agent_type_key: "copywriter",
      enabled: false,
      config: { timeout_minutes: 12 },
      prompts: {},
      builder_metadata_jsonb: {},
      _skills: [],
      _tools: [],
      _inputs: [],
      _outputs: [],
    };
    mockedFetchRegistryDetail.mockImplementation(async () => ({ ...agent }));
    mockedPatchRegistryEntity.mockImplementation(async (_endpoint, _id, patch) => {
      // API read rows resolve the canonical type key alongside its stored ID.
      Object.assign(agent, patch, { agent_type_key: patch.agent_type_id });
      return { status: "updated" };
    });

    const view = renderPage();
    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));
    fireEvent.change(screen.getByLabelText("Display Name"), {
      target: { value: ` ${configPatchFixture.display_name} ` },
    });
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: ` ${configPatchFixture.description} ` },
    });
    fireEvent.change(screen.getByLabelText("Category"), { target: { value: "quality" } });
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "checker" } });
    fireEvent.click(screen.getByRole("button", { name: "Save config" }));

    await waitFor(() => expect(mockedPatchRegistryEntity).toHaveBeenCalled());
    const payload = mockedPatchRegistryEntity.mock.calls[0]?.[2];
    // Shared with actual AgentPatch validation, not just a permissive API mock.
    expect(payload).toEqual(configPatchFixture);
    expect(payload).not.toHaveProperty("agent_type_key");
    expect(payload).not.toHaveProperty("builder_metadata_jsonb");
    expect(payload).not.toHaveProperty("enabled");
    expect(payload).not.toHaveProperty("skills");
    expect(payload).not.toHaveProperty("tools");
    expect(mockedToggleRegistryEntity).not.toHaveBeenCalled();
    expect(mockedReplaceAgentRelationships).not.toHaveBeenCalled();
    await waitFor(() => expect(mockedFetchRegistryDetail.mock.calls.length).toBeGreaterThan(1));
    expect(agent.enabled).toBe(false);
    expect(agent._skills).toEqual([]);
    expect(agent._tools).toEqual([]);
    view.unmount();
    renderPage();
    expect(await screen.findByText(configPatchFixture.display_name)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));
    expect(screen.getByLabelText("Display Name")).toHaveValue(configPatchFixture.display_name);
    expect(screen.getByLabelText("Description")).toHaveValue(configPatchFixture.description);
    expect(screen.getByLabelText("Category")).toHaveValue(configPatchFixture.category);
    expect(screen.getByLabelText("Type")).toHaveValue(configPatchFixture.agent_type_id);
  });

  it("renders agent I/O rows in a horizontal detail layout", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "writer",
      display_name: "Writer",
      enabled: true,
      description: "Analyzes requirements",
      prompts: {
        system: "You analyze requirements.",
      },
      builder_metadata_jsonb: {},
      _skills: [],
      _tools: [],
      _inputs: [
        {
          name: "summary_doc",
          type: "text",
          description: "Implementation-ready requirements",
          source: "state.summary_doc",
        },
      ],
      _outputs: [
        {
          name: "design_output",
          type: "json",
          description: "Architecture design output",
          source: "artifacts.design_output",
        },
      ],
    } as never);

    renderPage();

    expect(await screen.findByText("Writer")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "I/O" }));

    const requirementsDoc = await screen.findByText("summary_doc");
    const designOutput = await screen.findByText("design_output");

    // `.fs-signature-name` carries the ellipsis itself, so the utility is gone.
    expect(requirementsDoc).toHaveClass("fs-signature-name");
    expect(designOutput).toHaveClass("fs-signature-name");
    expect(screen.getAllByText("Description")[0]).toBeInTheDocument();
    expect(screen.getAllByTestId("contract-detail-row")[0]).toHaveClass("fs-signature-row");
    // The three canonical cells, asserted through their own text rather than by
    // walking the row: name, type and description each carry their published role.
    expect(screen.getByText("text")).toHaveClass("fs-signature-type");
    expect(screen.getByText(/Implementation-ready requirements/)).toHaveClass(
      "fs-signature-description",
    );
  });

  it("saves per-agent provider and model config from the config tab", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      description: "Writes launch assets",
      config: {
        provider: "openai",
        model: "gpt-5",
        timeout_minutes: 12,
      },
      prompts: {
        system: "You write launch content.",
      },
      builder_metadata_jsonb: {},
      _skills: [],
      tools: [
        {
          tool_id: "propose_workflow",
          display_name: "Propose workflow",
        },
      ],
      _tools: [],
      _inputs: [],
      _outputs: [],
    } as never);

    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));

    chooseOption("Provider", "anthropic");
    chooseOption("Model", "claude-sonnet-4-6");
    fireEvent.click(screen.getByRole("button", { name: "Save config" }));

    await waitFor(() => {
      expect(mockedPatchRegistryEntity).toHaveBeenCalledWith(
        "/api/agents",
        "sample_launch_copywriter",
        expect.objectContaining({
          config: {
            provider: "anthropic",
            model: "claude-sonnet-4-6",
            timeout_minutes: 12,
          },
        }),
      );
    });
    expect(mockedPatchRegistryEntity.mock.calls[0]?.[2]).not.toHaveProperty("tools");
    expect(mockedPatchRegistryEntity.mock.calls[0]?.[2]).not.toHaveProperty("_tools");
  });

  it("clears an incompatible model and blocks a partial agent route", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      description: "Writes launch assets",
      config: {
        provider: "openai",
        model: "gpt-5",
        timeout_minutes: 12,
      },
      prompts: {
        system: "You write launch content.",
      },
      builder_metadata_jsonb: {},
      _skills: [],
      _tools: [],
      _inputs: [],
      _outputs: [],
    } as never);

    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));

    chooseOption("Provider", "anthropic");
    // The trigger reads the selected option's label, and the empty option is a
    // real選 selectable entry rather than a placeholder.
    expect(selectedLabel("Model")).toBe("Project default");
    expect(screen.getByRole("button", { name: "Save config" })).toBeDisabled();
    expect(mockedPatchRegistryEntity).not.toHaveBeenCalled();
  });

  it("sends explicit empty route fields when returning to Project default", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      config: {
        provider: "openai",
        model: "gpt-5.6-sol",
        timeout_minutes: 12,
      },
      prompts: { system: "You write launch content." },
      builder_metadata_jsonb: {},
      _skills: [],
      _tools: [],
      _inputs: [],
      _outputs: [],
    } as never);

    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));
    chooseOption("Provider", "Project default");
    fireEvent.click(screen.getByRole("button", { name: "Save config" }));

    await waitFor(() => {
      expect(mockedPatchRegistryEntity).toHaveBeenCalledWith(
        "/api/agents",
        "sample_launch_copywriter",
        expect.objectContaining({
          config: {
            provider: "",
            model: "",
            timeout_minutes: 12,
          },
        }),
      );
    });
  });

  it("saves a per-agent extension provider/model pair with its display label", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      description: "Writes launch assets",
      config: { provider: "openai", model: "gpt-5", timeout_minutes: 12 },
      prompts: { system: "You write launch content." },
      builder_metadata_jsonb: {},
      _skills: [],
      _tools: [],
      _inputs: [],
      _outputs: [],
    } as never);

    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));

    expect(optionLabels("Provider")).toContain("DeepSeek (Acme LLM)");
    chooseOption("Provider", "DeepSeek (Acme LLM)");
    expect(optionLabels("Model")).toContain("DeepSeek Chat");
    chooseOption("Model", "DeepSeek Chat");
    fireEvent.click(screen.getByRole("button", { name: "Save config" }));

    await waitFor(() => {
      expect(mockedPatchRegistryEntity).toHaveBeenCalledWith(
        "/api/agents",
        "sample_launch_copywriter",
        expect.objectContaining({
          config: expect.objectContaining({
            provider: "llmext_acme_llm_deepseek_abc123",
            model: "deepseek/deepseek-chat",
          }),
        }),
      );
    });
  });

  it("does not expose explicit models while the agent provider is inherited", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      config: {},
      prompts: { system: "You write launch content." },
      builder_metadata_jsonb: {},
      _skills: [],
      _tools: [],
      _inputs: [],
      _outputs: [],
    } as never);

    renderPage();

    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));
    // Only the project-default entry: no explicit model is offered.
    expect(optionLabels("Model")).toEqual(["Project default"]);
  });

  it("marks a persisted agent model outside routable options as unavailable", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      config: { provider: "openai", model: "gpt-4o" },
      prompts: { system: "You write launch content." },
      builder_metadata_jsonb: {},
      _skills: [],
      _tools: [],
      _inputs: [],
      _outputs: [],
    } as never);
    mockedFetchJson.mockResolvedValue({
      provider_options: [{ provider_id: "openai", label: "OpenAI" }],
      all_models: [{ model_id: "gpt-5.6-sol" }],
      provider_models: { openai: [{ model_id: "gpt-5.6-sol" }] },
    });

    renderPage();
    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));

    expect(optionLabels("Model")).toContain("gpt-4o (unavailable)");
    expect(screen.getByText(/agent's model isn't verified/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save config" })).toBeDisabled();
  });

  it("names an extension provider whose models all await verification by its label", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      config: { provider: "llmext_acme_default_abc", model: "acme-large:120b" },
      prompts: { system: "You write launch content." },
      builder_metadata_jsonb: {},
      _skills: [],
      _tools: [],
      _inputs: [],
      _outputs: [],
    } as never);
    mockedFetchJson.mockResolvedValue({
      provider_options: [{ provider_id: "openai", label: "OpenAI" }],
      provider_labels: { openai: "OpenAI", llmext_acme_default_abc: "Acme LLM" },
      all_models: [{ model_id: "gpt-5.6-sol" }],
      provider_models: { openai: [{ model_id: "gpt-5.6-sol" }] },
    });

    renderPage();
    expect(await screen.findByText("Launch Copywriter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));

    expect(optionLabels("Provider")).toContain("Acme LLM (unavailable)");
    expect(optionLabels("Provider").join(" ")).not.toContain("llmext_acme_default_abc");
    expect(screen.getByText(/agent's model isn't verified/i)).toBeInTheDocument();
  });

  it("blocks config save when a provider is selected without a model", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      agent_id: "sample_launch_copywriter",
      display_name: "Launch Copywriter",
      enabled: true,
      config: {},
      prompts: { system: "You write launch content." },
      builder_metadata_jsonb: {},
      _skills: [],
      _tools: [],
      _inputs: [],
      _outputs: [],
    } as never);

    renderPage();
    await screen.findByText("Launch Copywriter");
    fireEvent.click(screen.getByRole("tab", { name: "Config" }));
    chooseOption("Provider", "DeepSeek (Acme LLM)");

    expect(selectedLabel("Model")).toBe("Project default");
    expect(screen.getByRole("button", { name: "Save config" })).toBeDisabled();
  });
});
