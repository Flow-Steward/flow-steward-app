import { screen, waitFor, within } from "@testing-library/react";
import { jobPayload, mockedFetchJobUsageBreakdown, primeApi, renderPage } from "./index.test-helpers";

const extensionAgent = "extension.829a829e-1ba9-4538-87b9-caec56d32382.scraper_mapping_author";

describe("Model usage labels", () => {
  it.each(["projection", "raw"])("uses readable names in the %s telemetry path without changing token counts", async (source) => {
    const rows = [
      { agent: extensionAgent, agent_label: extensionAgent, model: "test-model", model_used: "test-model", tokens: 19, tokens_total: 19, calls: 1 },
      { agent: "RunnableSequence", agent_label: "Runnablesequence", model: "test-model", model_used: "test-model", tokens: 20, tokens_total: 20, calls: 1 },
      { agent: "workflow_routing", agent_label: "Workflow Routing", model: "test-model", model_used: "test-model", tokens: 30, tokens_total: 30, calls: 1 },
    ];
    primeApi(jobPayload({ status: "completed", usage: {}, sections: { usage_breakdown: { rows: source === "projection" ? rows : [] } } }));
    mockedFetchJobUsageBreakdown.mockResolvedValue({ breakdown: rows });
    renderPage();

    await waitFor(() => expect(within(screen.getByTestId("job-usage-breakdown")).getByText("Scraper Mapping Author")).toHaveAttribute("title", extensionAgent));
    const card = screen.getByTestId("job-usage-breakdown");
    expect(within(card).getByText("Model processing")).toBeVisible();
    expect(within(card).getByText("Workflow routing")).toBeVisible();
    expect(within(card).queryByText(extensionAgent, { exact: true })).not.toBeInTheDocument();
    expect(within(card).getByText(/19 tokens/)).toBeVisible();
  });

  it.each(["projection", "raw"])("preserves a recorded friendly display name in the %s path", async (source) => {
    const rows = [{ agent: extensionAgent, agent_label: extensionAgent, agent_display_name: "Web Scraper · Mapping author", model: "test-model", tokens: 7, tokens_total: 7, calls: 1 }];
    primeApi(jobPayload({ status: "completed", usage: {}, sections: { usage_breakdown: { rows: source === "projection" ? rows : [] } } }));
    mockedFetchJobUsageBreakdown.mockResolvedValue({ breakdown: rows });
    renderPage();
    await waitFor(() => expect(within(screen.getByTestId("job-usage-breakdown")).getByText("Web Scraper · Mapping author")).toBeVisible());
  });
  it.each(["projection", "raw"])("keeps backend-humanized UUIDs in diagnostics in the %s path", async (source) => {
    const agent = "829a829e-1ba9-4538-87b9-caec56d32382";
    const rows = [{ agent, agent_label: "829a829e 1ba9 4538 87b9 Caec56d32382", model: "test-model", tokens: 7, tokens_total: 7, calls: 1 }];
    primeApi(jobPayload({ status: "completed", usage: {}, sections: { usage_breakdown: { rows: source === "projection" ? rows : [] } } }));
    mockedFetchJobUsageBreakdown.mockResolvedValue({ breakdown: rows });
    renderPage();
    await waitFor(() => expect(within(screen.getByTestId("job-usage-breakdown")).getByText("Agent")).toHaveAttribute("title", agent));
    const card = screen.getByTestId("job-usage-breakdown");
    expect(within(card).queryByText(rows[0].agent_label)).not.toBeInTheDocument();
  });

});
