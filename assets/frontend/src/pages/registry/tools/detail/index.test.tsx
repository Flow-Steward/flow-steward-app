import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { RegistryToolsDetailPage } from "@/pages/registry/tools/detail";
import {
  fetchRegistryDetail,
  toggleRegistryEntity,
  updateRegistryEntity,
} from "@/pages/registry/shared/api";

vi.mock("@/pages/registry/shared/api", () => ({
  fetchRegistryDetail: vi.fn(),
  toggleRegistryEntity: vi.fn(),
  updateRegistryEntity: vi.fn(),
}));

vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

const mockedFetchJson = vi.mocked(fetchJson);
const mockedFetchRegistryDetail = vi.mocked(fetchRegistryDetail);
const mockedToggleRegistryEntity = vi.mocked(toggleRegistryEntity);
const mockedUpdateRegistryEntity = vi.mocked(updateRegistryEntity);

function renderPage(locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  const messages: Record<string, string> = locale === "lt" ? { "trash.columns.category": "Kategorija" } : {};
  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/registry/tools/sample_content_tools"]}>
        <Routes>
          <Route
            element={<RegistryToolsDetailPage />}
            path="/registry/tools/:toolId"
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider></I18nContext.Provider>,
  );
}

describe("RegistryToolsDetailPage", () => {
  beforeEach(() => {
    mockedFetchJson.mockReset();
    mockedFetchRegistryDetail.mockReset();
    mockedToggleRegistryEntity.mockReset();
    mockedUpdateRegistryEntity.mockReset();
    mockedFetchJson.mockResolvedValue([]);
  });

  it("does not render a false not-found state while the tool detail is loading", async () => {
    mockedFetchRegistryDetail.mockReturnValue(new Promise(() => undefined));

    renderPage();

    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("Loading tool detail");
    expect(
      within(screen.getByTestId("page-container")).getByRole("status"),
    ).toBe(status);
    expect(screen.queryByRole("heading", { name: "Tool not found" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Enable" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Config" })).not.toBeInTheDocument();
  });

  it("renders an inert not-found state for a purged tool deep link", async () => {
    mockedFetchRegistryDetail.mockRejectedValue(
      new HttpError({ payload: {}, status: 404, url: "/api/tools/sample_content_tools" }),
    );

    renderPage();

    const notFoundHeading = await screen.findByRole("heading", { name: "Tool not found" });
    expect(notFoundHeading).toBeInTheDocument();
    // The not-found branch is a branch assigned to pageBody above the
    // container; it must still land inside PageContainer rather than escape it.
    expect(
      within(screen.getByTestId("page-container")).getByRole("heading", { name: "Tool not found" }),
    ).toBe(notFoundHeading);
    expect(screen.getByText(/deleted or purged, or you may not have access/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to tools" })).toHaveAttribute(
      "href",
      "/registry/tools",
    );
    expect(screen.queryByRole("button", { name: "Enable" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Config" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Display Name")).not.toBeInTheDocument();
    expect(screen.queryByText("Changelog")).not.toBeInTheDocument();
  });

  it("keeps non-404 tool failures unavailable and retryable", async () => {
    const user = userEvent.setup();
    mockedFetchRegistryDetail.mockRejectedValue(
      new HttpError({ payload: {}, status: 500, url: "/api/tools/sample_content_tools" }),
    );

    renderPage();

    const unavailableHeading = await screen.findByRole("heading", { name: "Tool unavailable" });
    expect(unavailableHeading).toBeInTheDocument();
    expect(
      within(screen.getByTestId("page-container")).getByRole("heading", { name: "Tool unavailable" }),
    ).toBe(unavailableHeading);
    expect(screen.queryByRole("heading", { name: "Tool not found" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Enable" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Config" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mockedFetchRegistryDetail).toHaveBeenCalledTimes(2));
  });

  it("keeps the normal tool editor available after a successful load", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      tool_id: "sample_content_tools",
      display_name: "Demo Content Tools",
      enabled: true,
      interface: {},
    } as never);

    renderPage();

    expect(await screen.findByText("Demo Content Tools")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disable" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Config" })).toBeInTheDocument();
    expect(screen.getByLabelText("Display Name")).toHaveValue("Demo Content Tools");
  });

  it("localizes the tool category select and shows the saved category by its shared label", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({ tool_id: "sample_content_tools", display_name: "Owner tool", enabled: true, category: "sales", interface: {} } as never);
    mockedFetchJson.mockImplementation(async (url) => (url === "/api/catalog-categories" ? [{ id: "sales", label: "Sales" }, { id: "other", label: "Other" }] : []));
    renderPage("lt");
    const select = await screen.findByRole("combobox", { name: "Kategorija" });
    await waitFor(() => expect(select).toHaveTextContent("Sales"));
  });

  it("renders action contracts from tool interface functions", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      tool_id: "sample_content_tools",
      display_name: "Demo Content Tools",
      enabled: true,
      description: "Demo deterministic content helpers",
      interface: {
        functions: [
          {
            name: "demo_compose_content_brief",
            description: "Compose a compact content brief.",
            parameters: [
              { name: "topic", type: "string", required: true },
              { name: "tone", type: "string", default: "clear" },
            ],
            returns: { name: "content_brief", type: "json", required: true },
          },
        ],
      },
    } as never);

    renderPage();

    expect(await screen.findByText("Demo Content Tools")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Actions" }));

    expect(
      await screen.findByRole("heading", {
        name: "demo_compose_content_brief",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText("Compose a compact content brief.").length,
    ).toBeGreaterThan(0);
    expect(screen.getAllByText("topic").length).toBeGreaterThan(0);
    expect(screen.getAllByText("tone").length).toBeGreaterThan(0);
    expect(screen.getAllByText("content_brief").length).toBeGreaterThan(0);
    // The default is still on the page, and now in real text rather than folded
    // into a badge string: `default clear` was the chip's own wording, which
    // canon forbids, so the assertion follows the value to its new column.
    expect(screen.getByText(/Defaults to/)).toHaveClass("fs-signature-description");
    expect(screen.getByText("clear").tagName).toBe("CODE");
  });

  it("saves builder metadata for workflow authoring guidance", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      tool_id: "sample_content_tools",
      display_name: "Demo Content Tools",
      enabled: true,
      description: "Demo deterministic content helpers",
      builder_metadata_jsonb: {
        purpose: "Old purpose",
        use_cases: ["old case"],
        anti_use_cases: ["old anti case"],
        composition_role: "old_role",
        determinism_level: "hybrid",
        safe_for_auto_use: false,
      },
      interface: {},
    } as never);

    renderPage();

    expect(await screen.findByText("Demo Content Tools")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Builder" }));

    fireEvent.change(
      screen.getByPlaceholderText(
        "What this primitive is for in workflow authoring.",
      ),
      {
        target: { value: "Generate deterministic publish-ready content." },
      },
    );
    fireEvent.change(screen.getByPlaceholderText("reasoning_step"), {
      target: { value: "content_generation" },
    });
    fireEvent.click(screen.getByRole("combobox", { name: "Determinism" }));
    fireEvent.click(screen.getByRole("option", { name: "deterministic" }));
    fireEvent.change(screen.getByPlaceholderText("One use case per line"), {
      target: { value: "generate blog drafts\nprepare publish copy" },
    });
    fireEvent.change(
      screen.getByPlaceholderText(
        "When workflow authoring should avoid using this primitive",
      ),
      {
        target: { value: "human approval\nfree-form reasoning" },
      },
    );
    fireEvent.click(screen.getAllByRole("checkbox").at(-1)!);
    fireEvent.click(screen.getByRole("button", { name: "Save Builder metadata" }));

    await waitFor(() => {
      expect(mockedUpdateRegistryEntity).toHaveBeenCalledWith(
        "/api/tools",
        "sample_content_tools",
        expect.objectContaining({
          builder_metadata_jsonb: {
            purpose: "Generate deterministic publish-ready content.",
            use_cases: ["generate blog drafts", "prepare publish copy"],
            anti_use_cases: ["human approval", "free-form reasoning"],
            composition_role: "content_generation",
            determinism_level: "deterministic",
            safe_for_auto_use: true,
          },
        }),
      );
    });
  });

  it("shows an unavailable source reason without offering Enable", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      tool_id: "orphaned_tool",
      display_name: "Orphaned Tool",
      enabled: false,
      availability_status: "unavailable",
      availability_reason_code: "source_connector_removed",
      availability_reason_text: "Source MCP connector mcp_removed was deleted.",
      interface: {},
      implementation: { type: "mcp" },
    } as never);

    renderPage();

    expect(await screen.findByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("Source MCP connector mcp_removed was deleted.")).toBeInTheDocument();
    expect(screen.getByText("source_connector_removed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Enable" })).not.toBeInTheDocument();
    expect(mockedToggleRegistryEntity).not.toHaveBeenCalled();
  });

  it("sends the trimmed prompt and nothing else new", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      tool_id: "sample_content_tools",
      display_name: "Demo Content Tools",
      enabled: true,
      prompt: "old prompt",
      interface: {},
    } as never);

    renderPage();

    expect(await screen.findByText("Demo Content Tools")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Prompt" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Prompt" }), {
      target: { value: "  publish-ready copy  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save prompt" }));

    await waitFor(() => {
      expect(mockedUpdateRegistryEntity).toHaveBeenCalledWith(
        "/api/tools",
        "sample_content_tools",
        {
          tool_id: "sample_content_tools",
          display_name: "Demo Content Tools",
          enabled: true,
          interface: {},
          prompt: "publish-ready copy",
        },
      );
    });
  });

  it("encodes agent ids in the linked agent route", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      tool_id: "sample_content_tools",
      display_name: "Demo Content Tools",
      enabled: true,
      interface: {},
    } as never);
    mockedFetchJson.mockResolvedValue([{ agent_id: "team/reviewer agent" }]);

    renderPage();

    expect(await screen.findByText("Demo Content Tools")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Agents Using" }));

    expect(await screen.findByRole("link", { name: "Open" })).toHaveAttribute(
      "href",
      "/registry/agents/team%2Freviewer%20agent",
    );
  });

  it("keeps contract chip text on the badge's own foreground", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      tool_id: "sample_content_tools",
      display_name: "Demo Content Tools",
      enabled: true,
      interface: {
        function_name: "search",
        parameters: { properties: { topic: { type: "string" } }, required: ["topic"] },
      },
    } as never);

    renderPage();

    expect(await screen.findByText("Demo Content Tools")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Actions" }));

    const chip = await screen.findByText("topic");
    // The badge owns its foreground. Any ink role inside it re-declares a colour
    // meant for a page surface, which is invisible against an inverted one.
    expect(chip.className).not.toMatch(/fs-detail-value|fs-ink-/);
  });

  it("renders advanced details as a description list with no repeated heading", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      tool_id: "sample_content_tools",
      display_name: "Demo Content Tools",
      enabled: true,
      implementation: { kind: "mcp", server: "docs_server", connector_id: "" },
      interface: {},
    } as never);

    renderPage();

    expect(await screen.findByText("Demo Content Tools")).toBeInTheDocument();
    const summary = screen.getByText("Advanced runtime details");
    fireEvent.click(summary);

    // The summary row is the heading. Nothing inside repeats it.
    expect(screen.queryByRole("heading", { name: "Implementation details" })).not.toBeInTheDocument();
    // Real dl/dt/dd, so the pair survives without the visual column.
    expect(screen.getByText("kind").tagName).toBe("DT");
    expect(screen.getByText("mcp").tagName).toBe("DD");
    // An absent value says what is missing; canon forbids both a blank cell and
    // a bare dash, which is what the shared normalizePrimitive would have given.
    const empty = screen.getByText("Not set");
    expect(empty.tagName).toBe("DD");
    expect(empty).toHaveClass("is-empty");
  });

  it("renders parameter chips without an inverted fill", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      tool_id: "sample_content_tools",
      display_name: "Demo Content Tools",
      enabled: true,
      interface: {
        function_name: "search",
        parameters: { properties: { topic: { type: "string" } }, required: ["topic"] },
      },
    } as never);

    renderPage();

    expect(await screen.findByText("Demo Content Tools")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Actions" }));

    // The name lands in the published chip's own name slot. Badge would have put
    // it in `.fs-table-mono` inside `.fs-badge`, and the ink tone was downstream
    // of using Badge at all. The name is its own leaf inside that slot — an
    // exact-text match has to find `topic` and nothing else, which is what the
    // browser lane asserts — so the slot is matched by class and full text.
    expect(await screen.findByText("topic")).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) =>
          element?.className === "fs-param-name" && element.textContent === "topic*",
      ),
    ).toBeInTheDocument();
    // Requiredness is a marker with an accessible name, not a spelled-out word.
    expect(screen.getByLabelText("required")).toHaveClass("fs-param-required");
    // A middot reads the three facts as one string; the divider is a border.
    expect(screen.queryByText(/·\s*string/)).not.toBeInTheDocument();
    // Neither the word nor the default belongs on a chip.
    expect(screen.queryByText(/· required|· default/)).not.toBeInTheDocument();
    // One legend per group, never per chip.
    expect(screen.getAllByText(/unmarked parameters are optional/)).toHaveLength(1);
  });

  it("switches a parameter group to signature rows once a parameter needs prose", async () => {
    mockedFetchRegistryDetail.mockResolvedValue({
      tool_id: "sample_content_tools",
      display_name: "Demo Content Tools",
      enabled: true,
      interface: {
        function_name: "search",
        parameters: [
          { name: "topic", type: "string", required: true, description: "Subject to search for." },
          { name: "limit", type: "number", required: false, default: 10 },
        ],
      },
    } as never);

    renderPage();

    expect(await screen.findByText("Demo Content Tools")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Actions" }));

    // The description is real content, not a title attribute nobody can reach by
    // touch or keyboard.
    expect(await screen.findByText("Subject to search for.")).toHaveClass(
      "fs-signature-description",
    );
    // A default is prose too, and it belongs in the same column.
    expect(screen.getByText(/Defaults to/)).toHaveClass("fs-signature-description");
    // The whole group switched: the name is now a row cell, not a chip segment.
    expect(screen.getByText("topic")).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) =>
          element?.className === "fs-signature-name" && element.textContent === "topic*",
      ),
    ).toBeInTheDocument();
  });
});
