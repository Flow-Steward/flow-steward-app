import { createElement } from "react";
import type { ReactElement } from "react";

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { fetchChangelogHistory } from "@/pages/registry/changelog/api";
import {
  deleteMcpProjectedToolConfirmed,
  deleteRegistryEntity,
  fetchMcpProjectedToolDeletePlan,
  fetchRegistryDetail,
} from "@/pages/registry/shared/api";
import { RegistryDetailSurface } from "@/pages/registry/shared/catalog-detail-surface";
import { useHasCapability } from "@/lib/auth/capabilities";

vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn() }));

const mockedUseHasCapability = vi.mocked(useHasCapability);

vi.mock("@/pages/registry/shared/api", () => ({
  deleteMcpProjectedToolConfirmed: vi.fn(),
  deleteRegistryEntity: vi.fn(),
  fetchMcpProjectedToolDeletePlan: vi.fn(),
  fetchRegistryDetail: vi.fn(),
}));

vi.mock("@/pages/registry/changelog/api", () => ({
  fetchChangelogHistory: vi.fn(),
}));

const mockedFetchRegistryDetail = vi.mocked(fetchRegistryDetail);
const mockedFetchMcpProjectedToolDeletePlan = vi.mocked(fetchMcpProjectedToolDeletePlan);
const mockedDeleteMcpProjectedToolConfirmed = vi.mocked(deleteMcpProjectedToolConfirmed);
const mockedDeleteRegistryEntity = vi.mocked(deleteRegistryEntity);
const mockedFetchChangelogHistory = vi.mocked(fetchChangelogHistory);

function renderWithRoute(route: string, routePath: string, element: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  return render(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(
        MemoryRouter,
        { initialEntries: [route] },
        createElement(Routes, null, createElement(Route, { path: routePath, element })),
      ),
    ),
  );
}

describe("RegistryDetailSurface", () => {
  beforeEach(() => {
    mockedUseHasCapability.mockReturnValue(true);
  });

  it("keeps operator-readable detail surfaces read-only", () => {
    mockedUseHasCapability.mockReturnValue(false);
    renderWithRoute(
      "/registry/skills/skill.one",
      "/registry/skills/:skillId",
      createElement(RegistryDetailSurface, {
        endpoint: "/api/registry/skills",
        idKey: "skill_id",
        idParam: "skillId",
        listPath: "/registry/skills",
        title: "Skill",
      }),
    );

    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Enable|Disable/ })).not.toBeInTheDocument();
  });
  beforeEach(() => {
    mockedFetchRegistryDetail.mockReset();
    mockedFetchMcpProjectedToolDeletePlan.mockReset();
    mockedDeleteMcpProjectedToolConfirmed.mockReset();
    mockedDeleteRegistryEntity.mockReset();
    mockedFetchChangelogHistory.mockReset();
    mockedDeleteRegistryEntity.mockResolvedValue({ status: "deleted" });
    mockedDeleteMcpProjectedToolConfirmed.mockResolvedValue({ status: "deleted" });
  });

  it("renders agent-specific detail tabs instead of a generic three-tab view", async () => {
    mockedFetchRegistryDetail.mockResolvedValueOnce({
      agent_id: "writer",
      display_name: "Writer",
      prompts: { system: "Be precise." },
      skills: [{ skill_id: "s1", display_name: "Research" }],
      tools: [{ tool_id: "t1", display_name: "Search" }],
      expected_inputs: [{ name: "question" }],
      required_outputs: [{ name: "answer" }],
    });
    mockedFetchChangelogHistory.mockResolvedValueOnce([]);

    renderWithRoute(
      "/registry/agents/writer",
      "/registry/agents/:agentId",
      createElement(RegistryDetailSurface, {
        endpoint: "/api/registry/agents",
        idKey: "agent_id",
        idParam: "agentId",
        listPath: "/registry/agents",
        title: "Agents",
      }),
    );

    expect(await screen.findByText("Writer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Prompts" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Skills" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tools" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "I/O" })).toBeInTheDocument();
  });

  it("renders tool-specific tabs with config and consumer context", async () => {
    mockedFetchRegistryDetail.mockResolvedValueOnce({
      tool_id: "planner",
      display_name: "Planner",
      implementation: { runtime: "python" },
      prompt: "Use workflow context first.",
      using_agents: ["writer", "operator"],
    });
    mockedFetchChangelogHistory.mockResolvedValueOnce([]);

    renderWithRoute(
      "/registry/tools/planner",
      "/registry/tools/:toolId",
      createElement(RegistryDetailSurface, {
        endpoint: "/api/registry/tools",
        idKey: "tool_id",
        idParam: "toolId",
        listPath: "/registry/tools",
        title: "Tools",
      }),
    );

    expect(await screen.findByText("Planner")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Config" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Prompt" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Agents Using" })).toBeInTheDocument();
  });

  it("renders guardrail detail tabs with scope-specific controls", async () => {
    mockedFetchRegistryDetail.mockResolvedValueOnce({
      guardrail_id: "no-secrets",
      display_name: "No Secrets",
      enabled: true,
      config: { severity: "high" },
      agents: ["reviewer", "operator"],
    });
    mockedFetchChangelogHistory.mockResolvedValueOnce([]);

    renderWithRoute(
      "/registry/guardrails/no-secrets",
      "/registry/guardrails/:guardrailId",
      createElement(RegistryDetailSurface, {
        endpoint: "/api/registry/guardrails",
        idKey: "guardrail_id",
        idParam: "guardrailId",
        listPath: "/registry/guardrails",
        title: "Guardrails",
      }),
    );

    expect(await screen.findByText("No Secrets")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Config" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Agent Scope" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Changelog" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disable" })).toBeInTheDocument();
  });

  it("offers a detail-page delete action for registry entities", async () => {
    const user = userEvent.setup();
    mockedFetchRegistryDetail.mockResolvedValueOnce({
      agent_id: "writer",
      display_name: "Writer",
      enabled: true,
    });
    mockedFetchChangelogHistory.mockResolvedValueOnce([]);

    renderWithRoute(
      "/registry/agents/writer",
      "/registry/agents/:agentId",
      createElement(RegistryDetailSurface, {
        endpoint: "/api/registry/agents",
        idKey: "agent_id",
        idParam: "agentId",
        listPath: "/registry/agents",
        title: "Agents",
      }),
    );

    expect(await screen.findByText("Writer")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Delete" }).at(-1)!);

    expect(mockedDeleteRegistryEntity).toHaveBeenCalledWith("/api/registry/agents", "writer");
  });

  it("uses the MCP tool impact plan from the detail page", async () => {
    const user = userEvent.setup();
    mockedFetchRegistryDetail.mockResolvedValueOnce({
      tool_id: "fs.catalog.read",
      display_name: "Catalog Read",
      enabled: false,
      implementation: { type: "mcp", connector_id: "mcp_fs" },
    });
    mockedFetchChangelogHistory.mockResolvedValueOnce([]);
    mockedFetchMcpProjectedToolDeletePlan.mockResolvedValueOnce({
      tool_id: "fs.catalog.read",
      connector_id: "mcp_fs",
      affected: { workflows: [{ workflow_id: "wf_catalog", display_name: "Catalog Flow" }] },
      warnings: [],
      revision: "revision-2",
    });
    renderWithRoute(
      "/registry/tools/fs.catalog.read",
      "/registry/tools/:toolId",
      createElement(RegistryDetailSurface, {
        endpoint: "/api/tools",
        idKey: "tool_id",
        idParam: "toolId",
        listPath: "/registry/tools",
        profile: "tools",
        title: "Tools",
      }),
    );

    expect(await screen.findByText("Catalog Read")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(await screen.findByRole("dialog", { name: "Move MCP tool to Trash" })).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Move to Trash" }));

    expect(mockedDeleteMcpProjectedToolConfirmed).toHaveBeenCalledWith(
      "fs.catalog.read",
      "revision-2",
    );
    expect(mockedDeleteRegistryEntity).not.toHaveBeenCalled();
  });

  it("loads data contract history from the canonical changelog API", async () => {
    mockedFetchRegistryDetail.mockResolvedValueOnce({
      contract_id: "auto.available_checks.v1",
      display_name: "Available Checks",
    });
    mockedFetchChangelogHistory.mockResolvedValueOnce([]);

    renderWithRoute(
      "/registry/data-contracts/auto.available_checks.v1",
      "/registry/data-contracts/:contractId",
      createElement(RegistryDetailSurface, {
        endpoint: "/api/data-contracts",
        idKey: "contract_id",
        idParam: "contractId",
        listPath: "/registry/data-contracts",
        readOnly: true,
        readOnlyNotice: "Data contracts are generated and cannot be mutated directly.",
        title: "Data Contracts",
      }),
    );

    expect(await screen.findByText("Available Checks")).toBeInTheDocument();
    expect(screen.getByText("Data contracts are generated and cannot be mutated directly.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Enable" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disable" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    expect(mockedFetchChangelogHistory).toHaveBeenCalledWith(
      "data_contract",
      "auto.available_checks.v1",
    );
  });

  it("loads field history from the canonical changelog API", async () => {
    mockedFetchRegistryDetail.mockResolvedValueOnce({
      field_key: "architecture",
      display_name: "Architecture",
    });
    mockedFetchChangelogHistory.mockResolvedValueOnce([]);

    renderWithRoute(
      "/registry/fields/architecture",
      "/registry/fields/:fieldKey",
      createElement(RegistryDetailSurface, {
        endpoint: "/api/fields",
        idKey: "field_key",
        idParam: "fieldKey",
        listPath: "/registry/fields",
        readOnly: true,
        title: "Fields",
      }),
    );

    expect(await screen.findByText("Architecture")).toBeInTheDocument();
    expect(mockedFetchChangelogHistory).toHaveBeenCalledWith("field", "architecture");
  });
});
