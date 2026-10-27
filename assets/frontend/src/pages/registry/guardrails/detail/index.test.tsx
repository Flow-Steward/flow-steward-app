import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

import { RegistryGuardrailsDetailPage } from "@/pages/registry/guardrails/detail";
import { fetchRegistryList } from "@/pages/registry/shared/api";
import { useRegistryEntityDetail } from "@/pages/registry/shared/detail-helpers";
import type * as DetailHelpers from "@/pages/registry/shared/detail-helpers";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

vi.mock("@/pages/registry/shared/api", () => ({
  fetchRegistryList: vi.fn(async () => []),
}));

vi.mock("@/pages/registry/shared/entity-changelog-panel", () => ({
  RegistryEntityChangelogPanel: () => <div>Changelog panel</div>,
}));

vi.mock("@/pages/registry/shared/detail-helpers", async () => {
  const actual = await vi.importActual<typeof DetailHelpers>(
    "@/pages/registry/shared/detail-helpers",
  );
  return {
    ...actual,
    useRegistryEntityDetail: vi.fn(),
  };
});

const mockedUseRegistryEntityDetail = vi.mocked(useRegistryEntityDetail);
const mockedFetchRegistryList = vi.mocked(fetchRegistryList);

function renderGuardrailDetail(entity: Record<string, unknown>, locale: "en" | "lt" = "en") {
  const saveEntityPatch = vi.fn(async (payload: Record<string, unknown>) => {
    void payload;
    return undefined;
  });
  const setSaveStatus = vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  mockedUseRegistryEntityDetail.mockReturnValue({
    detailQuery: {
      isError: false,
      refetch: vi.fn(async () => undefined),
    },
    entity,
    entityName: firstStringFromUnknownValue([entity.display_name, entity.name, "Guardrail"]),
    historyQuery: {
      data: [],
      isLoading: false,
      refetch: vi.fn(async () => undefined),
    },
    resolvedEntityId: firstStringFromUnknownValue([entity.id, "11"]),
    routeEntityId: firstStringFromUnknownValue([entity.id, "11"]),
    saveEntityPatch,
    saveStatus: "",
    saving: false,
    setSaveStatus,
    toggleEntityEnabled: vi.fn(async () => undefined),
  } as unknown as ReturnType<typeof useRegistryEntityDetail>);

  const messages: Record<string, string> = locale === "lt" ? { "react.registry.select_agent_before_saving": "Prieš išsaugodami pasirinkite bent vieną agentą." } : {};
  render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/registry/guardrails/11"]}>
        <RegistryGuardrailsDetailPage />
      </MemoryRouter>
    </QueryClientProvider></I18nContext.Provider>,
  );

  return { saveEntityPatch, setSaveStatus };
}

describe("RegistryGuardrailsDetailPage", () => {
  beforeEach(() => {
    mockedFetchRegistryList.mockReset();
    mockedFetchRegistryList.mockImplementation(async (endpoint) => {
      if (endpoint === "/api/agents") {
        return [
          { agent_id: "writer", display_name: "Writer" },
          { agent_id: "reviewer", display_name: "Reviewer" },
        ];
      }
      if (endpoint === "/api/projects") {
        return [{ id: "proj-1", name: "Demo Project" }];
      }
      return [];
    });
  });

  it("keeps forbidden pattern rows editable from draft state and saves the pattern list", async () => {
    const { saveEntityPatch } = renderGuardrailDetail({
      id: 11,
      display_name: "Blocked output",
      rule_type: "forbidden_patterns",
      enabled: true,
      config: {
        patterns: ["DROP TABLE"],
      },
    });

    expect(await screen.findByLabelText("Pattern 1")).toHaveValue("DROP TABLE");

    fireEvent.click(screen.getByRole("button", { name: /Add pattern/i }));

    const secondPatternInput = await screen.findByLabelText("Pattern 2");
    fireEvent.change(secondPatternInput, {
      target: { value: "private_key" },
    });

    expect(screen.getByLabelText("Pattern 1")).toHaveValue("DROP TABLE");
    expect(secondPatternInput).toHaveValue("private_key");

    fireEvent.click(screen.getByRole("button", { name: /Save config/i }));

    await waitFor(() => {
      expect(saveEntityPatch).toHaveBeenCalledWith(
        expect.objectContaining({
          config: {
            patterns: ["DROP TABLE", "private_key"],
          },
        }),
      );
    });
  });

  it("keeps generic config controls editable from draft state", async () => {
    const { saveEntityPatch } = renderGuardrailDetail({
      id: 12,
      display_name: "Secrets",
      rule_type: "secret_scan",
      enabled: true,
      config: {
        severity: "high",
      },
    });

    const severityInput = await screen.findByLabelText("severity");
    fireEvent.change(severityInput, {
      target: { value: "medium" },
    });

    expect(severityInput).toHaveValue("medium");

    fireEvent.click(screen.getByRole("button", { name: /Save config/i }));

    await waitFor(() => {
      expect(saveEntityPatch).toHaveBeenCalledWith(
        expect.objectContaining({
          config: {
            severity: "medium",
          },
        }),
      );
    });
  });

  it("keeps the fallback rule value editable when saved config is empty", async () => {
    const { saveEntityPatch } = renderGuardrailDetail({
      id: 13,
      display_name: "Empty config rule",
      rule_type: "custom_rule",
      enabled: true,
      config: {},
    });

    const ruleValueInput = await screen.findByLabelText(/Rule value/i);
    fireEvent.change(ruleValueInput, {
      target: { value: "demo-value" },
    });

    expect(ruleValueInput).toHaveValue("demo-value");

    fireEvent.click(screen.getByRole("button", { name: /Save config/i }));

    await waitFor(() => {
      expect(saveEntityPatch).toHaveBeenCalledWith(
        expect.objectContaining({
          config: {
            value: "demo-value",
          },
        }),
      );
    });
  });

  it("saves retired guardrail metadata fields that the React form must keep editable", async () => {
    const { saveEntityPatch } = renderGuardrailDetail({
      id: 14,
      display_name: "Secrets policy",
      rule_type: "secret_scan",
      mode: "deterministic",
      severity: "error",
      project_id: "",
      prompt_text: "",
      agent_prompt: "",
      enabled: true,
      config: {
        severity: "high",
      },
    });

    fireEvent.click(await screen.findByLabelText("Mode"));
    fireEvent.click(screen.getByRole("option", { name: "Semantic" }));
    fireEvent.click(screen.getByLabelText("Severity"));
    fireEvent.click(screen.getByRole("option", { name: "Warning" }));
    fireEvent.change(screen.getByLabelText("AI condition"), {
      target: { value: "Block secrets and ask for remediation" },
    });
    fireEvent.change(screen.getByLabelText(/Policy Guidance/i), {
      target: { value: "Never emit credentials in output." },
    });
    fireEvent.click(screen.getByLabelText("Project Scope"));
    fireEvent.click(screen.getByRole("option", { name: "Demo Project" }));

    fireEvent.click(screen.getByRole("button", { name: /Save config/i }));

    await waitFor(() => {
      expect(saveEntityPatch).toHaveBeenCalledWith(
        expect.objectContaining({
          mode: "semantic",
          severity: "warning",
          project_id: "proj-1",
          prompt_text: "Block secrets and ask for remediation",
          agent_prompt: "Never emit credentials in output.",
          config: expect.objectContaining({ severity: "high" }),
        }),
      );
    });
  });

  it("saves agent scope under the canonical agents field", async () => {
    const { saveEntityPatch } = renderGuardrailDetail({
      id: 15,
      display_name: "Scoped policy",
      rule_type: "forbidden_patterns",
      mode: "deterministic",
      severity: "error",
      enabled: true,
      _agents: ["writer"],
      config: {
        patterns: ["DROP TABLE"],
      },
    });

    fireEvent.click(screen.getByRole("tab", { name: "Agent Scope" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /Reviewer/i }));
    fireEvent.click(screen.getByRole("button", { name: /Save agent scope/i }));

    await waitFor(() => {
      expect(saveEntityPatch).toHaveBeenCalledWith({
        agents: ["reviewer", "writer"],
      });
    });
  });

  it("requires at least one explicitly selected agent", async () => {
    renderGuardrailDetail({
      id: 16,
      display_name: "Scoped policy",
      rule_type: "forbidden_patterns",
      mode: "deterministic",
      severity: "error",
      enabled: true,
      _agents: ["writer"],
      config: { patterns: ["DROP TABLE"] },
    });

    fireEvent.click(screen.getByRole("tab", { name: "Agent Scope" }));
    // Writer is the only selected agent, so unchecking it empties the scope.
    fireEvent.click(await screen.findByRole("checkbox", { name: /Toggle Writer/i }));

    expect(screen.getByText("Select at least one agent before saving.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save agent scope/i })).toBeDisabled();
  });

  it("localizes the empty guardrail scope message and does not save an invalid assignment", async () => {
    const { saveEntityPatch } = renderGuardrailDetail({ id: 16, display_name: "Owner policy", rule_type: "forbidden_patterns", mode: "deterministic", severity: "error", enabled: true, _agents: ["writer"], config: { patterns: ["DROP TABLE"] } }, "lt");
    fireEvent.click(screen.getByRole("tab", { name: "Agent Scope" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: /Toggle Writer/i }));
    expect(screen.getByText("Prieš išsaugodami pasirinkite bent vieną agentą.")).toBeInTheDocument();
    expect(screen.queryByText("Select at least one agent before saving.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save agent scope/i })).toBeDisabled();
    expect(saveEntityPatch).not.toHaveBeenCalled();
  });

  it("uses partial updates so saving Config preserves the reloaded agent scope", async () => {
    const { saveEntityPatch } = renderGuardrailDetail({
      id: 17,
      display_name: "Scoped forbidden patterns",
      description: "Original description",
      rule_type: "forbidden_patterns",
      mode: "deterministic",
      severity: "error",
      project_id: "",
      prompt_text: "Original prompt",
      agent_prompt: "Original guidance",
      enabled: true,
      _agents: ["writer"],
      config: { patterns: ["DROP TABLE"] },
    });

    expect(mockedUseRegistryEntityDetail).toHaveBeenCalledWith(
      expect.objectContaining({
        endpoint: "/api/guardrails",
        updateMode: "patch",
      }),
    );

    fireEvent.change(screen.getByLabelText(/Display Name/i), {
      target: { value: "Updated scoped policy" },
    });
    fireEvent.change(screen.getByLabelText(/Description/i), {
      target: { value: "Updated description" },
    });
    fireEvent.change(screen.getByLabelText("Pattern 1"), {
      target: { value: "private_key" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save config/i }));

    await waitFor(() => {
      expect(saveEntityPatch).toHaveBeenCalledWith(
        expect.objectContaining({
          display_name: "Updated scoped policy",
          description: "Updated description",
          config: { patterns: ["private_key"] },
        }),
      );
    });
    expect(saveEntityPatch.mock.calls[0]?.[0]).not.toHaveProperty("agents");
  });

  it("uses the shared supported rule editor while preserving a custom semantic identifier", async () => {
    renderGuardrailDetail({
      id: 31,
      display_name: "Customer policy",
      rule_type: "customer_specific_policy",
      mode: "semantic",
      severity: "warning",
      prompt_text: "Confirm the response follows the customer policy.",
      enabled: true,
      _agents: ["writer"],
      config: { custom_key: "preserved" },
    });

    expect(await screen.findByRole("combobox", { name: "Rule Type" })).toHaveTextContent(
      "Custom semantic rule",
    );
    expect(screen.getByLabelText("AI condition")).toHaveValue(
      "Confirm the response follows the customer policy.",
    );
    expect(
      (screen.getByLabelText("Advanced policy JSON") as HTMLTextAreaElement).value,
    ).toContain('"custom_key": "preserved"');
  });

  it("keeps a broken policy JSON draft out of the request and names the parser error on the field itself", async () => {
    const brokenDraft = '{ "patterns": ';
    let parserMessage = "";
    try {
      JSON.parse(brokenDraft);
    } catch (error) {
      parserMessage = error instanceof Error ? error.message : "";
    }
    expect(parserMessage).not.toBe("");

    const { saveEntityPatch, setSaveStatus } = renderGuardrailDetail({
      id: 29,
      display_name: "Required sections",
      rule_type: "required_sections",
      enabled: true,
      config: { sections: ["summary"] },
    });

    const jsonEditor = await screen.findByLabelText("Advanced policy JSON");
    fireEvent.change(jsonEditor, { target: { value: brokenDraft } });
    fireEvent.click(screen.getByRole("button", { name: /Save config/i }));

    // The error belongs to the field that failed. It used to land in `saveStatus`,
    // i.e. in the page header, styled the same as "Saved." — this pins it down so
    // a later refactor cannot quietly move it back.
    await waitFor(() => {
      expect(jsonEditor).toBeInvalid();
    });
    expect(jsonEditor).toHaveAccessibleDescription(parserMessage);
    expect(saveEntityPatch).not.toHaveBeenCalled();
    expect(setSaveStatus).not.toHaveBeenCalledWith(parserMessage);
  });
});
