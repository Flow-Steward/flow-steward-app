import type { PropsWithChildren } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";
import * as api from "./api";
import { useDatasetDetailState } from "./detail/dataset-detail-state";
import { useDatasetsState } from "./datasets-state";

vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: () => true }));
vi.mock("@/pages/registry/workspaces/api", () => ({ fetchWorkspaces: async () => [], fetchWorkspaceProjects: async () => [] }));
vi.mock("./api", async (original) => ({
  ...await original<typeof api>(),
  fetchDataset: vi.fn(), fetchDatasets: vi.fn(), fetchDatasetQueryTemplates: vi.fn(),
  fetchDataConnections: async () => [], fetchDatasetConnectors: async () => [],
  fetchDatasetProjectCatalog: async () => ({ dataset_id: "events", workspace_id: "", availability_mode: "specific_projects_only", project_ids: [], available_projects: [] }),
  updateDataset: vi.fn(), updateDatasetProjects: vi.fn(), toggleDataset: vi.fn(),
  updateDatasetQueryTemplate: vi.fn(), createDatasetQueryTemplate: vi.fn(),
  deleteDatasetQueryTemplate: vi.fn(), deleteDataset: vi.fn(),
}));

it.each([true, false])("refuses direct dataset/query mutations after ownership becomes immutable (stale: %s)", async (stale) => {
  const dataset = { dataset_id: "events", display_name: "Orders", workspace_id: "", availability_mode: "specific_projects_only" as const, connector_kind: "extension", runtime_kind: "local_extension", query_language: "sql", enabled: true, read_only: true, source_install_id: "install", source_release_id: "release" };
  const query = { query_template_id: "package-query", dataset_id: "events", display_name: "Package query", query_language: "sql", enabled: true, version: 1, source_install_id: "install" };
  vi.mocked(api.fetchDataset).mockResolvedValue(stale ? { ...dataset, source_install_id: undefined } : dataset);
  vi.mocked(api.fetchDatasets).mockResolvedValue([stale ? { ...dataset, source_install_id: undefined } : dataset]);
  vi.mocked(api.fetchDatasetQueryTemplates).mockResolvedValue([stale ? { ...query, source_install_id: undefined } : query]);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  function Wrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={client}><MemoryRouter initialEntries={["/datasets/events"]}><Routes><Route path="/datasets/:datasetId" element={children} /></Routes></MemoryRouter></QueryClientProvider>;
  }
  const { result: detailResult, unmount: unmountDetail } = renderHook(useDatasetDetailState, { wrapper: Wrapper });
  const { result: listingResult, unmount: unmountListing } = renderHook(useDatasetsState, { wrapper: Wrapper });
  await waitFor(() => expect(detailResult.current.savedQueries).toHaveLength(1));
  await waitFor(() => expect(listingResult.current.rows).toHaveLength(1));
  const payload = { display_name: "Changed", workspace_id: "ws", availability_mode: "specific_projects_only" as const, connection_id: "connection", enabled: false, read_only: true };
  const saveDataset = detailResult.current.saveMutation.mutateAsync;
  const saveAssignments = detailResult.current.saveAssignmentsMutation.mutateAsync;
  const toggleDataset = detailResult.current.toggleMutation.mutateAsync;
  const deleteQuery = detailResult.current.deleteQueryMutation.mutateAsync;
  const saveQuery = detailResult.current.saveQueryMutation.mutateAsync;
  const deleteDataset = listingResult.current.deleteMutation.mutateAsync;
  const operations = [
    () => saveDataset(payload),
    () => saveAssignments(),
    () => toggleDataset(),
    () => deleteQuery("package-query"),
    () => saveQuery({ ...query, query_definition_jsonb: { sql: "SELECT 1" } }),
    () => deleteDataset("events"),
  ];
  if (stale) {
    // The callbacks were captured while these records were editable.
    client.setQueryData(["registry", "datasets", "events"], dataset);
    client.setQueryData(["registry", "datasets"], [dataset]);
    client.setQueryData(["registry", "datasets", "events", "saved-queries"], [query]);
  }
  for (const operation of operations) {
    let failure: unknown;
    await act(async () => { try { await operation(); } catch (error) { failure = error; } });
    expect.soft(failure).toBeInstanceOf(Error);
  }
  for (const writer of [api.updateDataset, api.updateDatasetProjects, api.toggleDataset, api.deleteDatasetQueryTemplate, api.updateDatasetQueryTemplate, api.createDatasetQueryTemplate, api.deleteDataset]) expect.soft(writer).not.toHaveBeenCalled();
  unmountDetail(); unmountListing(); client.clear();
});
