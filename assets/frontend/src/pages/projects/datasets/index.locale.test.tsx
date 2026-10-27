import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { ProjectDatasetsEditor } from "@/pages/projects/datasets";
import { fetchProjectDatasetCatalog } from "@/pages/registry/datasets/api";

vi.mock("@/pages/registry/datasets/api", () => ({ fetchProjectDatasetCatalog: vi.fn(), updateProjectDatasets: vi.fn() }));

it("localizes project dataset counts and its save action without translating dataset data", async () => {
  vi.mocked(fetchProjectDatasetCatalog).mockResolvedValue({ project_id: "project-1", dataset_ids: ["orders"], explicit_dataset_ids: ["orders"], available_datasets: [{ dataset_id: "orders", display_name: "Orders fixture", workspace_id: "workspace-1", connector_kind: "extension", runtime_kind: "local_extension", read_only: true, connector_id: "source-1", query_language: "sql", enabled: true, availability_mode: "specific_projects_only" }] });
  const messages: Record<string, string> = { "react.projects.dataset_linked_count": "Susieta: {count}", "react.projects.dataset_total_count": "Iš viso: {count}", "react.projects.save_dataset_links": "Išsaugoti duomenų rinkinių sąsajas", "react.shared.assignment_linked": "susieta", "react.shared.assignment_total": "iš viso" };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ProjectDatasetsEditor projectId="project-1" /></QueryClientProvider></MemoryRouter></I18nContext.Provider>);
  expect(await screen.findByText("Orders fixture")).toBeInTheDocument();
  expect(screen.getByText("Susieta: 1")).toBeInTheDocument();
  expect(screen.getByText("Iš viso: 1")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Išsaugoti duomenų rinkinių sąsajas" })).toBeInTheDocument();
  expect(screen.getByText("1 susieta")).toBeInTheDocument();
  expect(screen.getByText("1 iš viso")).toBeInTheDocument();
  expect(screen.queryByText("1 linked")).not.toBeInTheDocument();
  expect(screen.queryByText("1 total")).not.toBeInTheDocument();
});
