import { fetchDatasets, fetchDataset, fetchDatasetQueryTemplates, previewDatasetQueryTemplate, validateDatasetQueryTemplate } from "./api";
import { fetchRegistryList } from "@/pages/registry/shared/api";
import { fetchWorkflows, fetchWorkflowStats } from "@/pages/workflows/shared/api";
import { fetchWorkflowTestRunState } from "@/pages/workflows/shared/simulate-api";
import { hasPendingRequests } from "@/lib/http/request-activity";
import type { FetchJsonOptions } from "@/lib/http/client";

afterEach(() => vi.restoreAllMocks());

it("preserves project-scoped connection resolution metadata from the dataset response", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ dataset_id: "projection.events", metadata_jsonb: { connection_resolution: "project_scope", projection_bridge: { immutable: true } } }), { status: 200, headers: { "content-type": "application/json" } }));
  const dataset = await fetchDataset("projection.events");
  expect(dataset.metadata_jsonb).toMatchObject({ connection_resolution: "project_scope", projection_bridge: { immutable: true } });
});

it.each(["preview", "validate"])("sends explicit project context with the %s query action", async (action) => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } }));
  if (action === "preview") await previewDatasetQueryTemplate("query one", { enabled: false }, 20, "project-alpha");
  else await validateDatasetQueryTemplate("query one", { enabled: false }, "project-alpha");
  expect(fetchMock.mock.calls[0]?.[0]).toBe(`/api/datasets/query-templates/query%20one/${action}`);
  expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({ params: { enabled: false }, project_id: "project-alpha", ...(action === "preview" ? { row_limit: 20 } : {}) });
});

it.each(["preview", "validate"])("preserves the fixed-connection %s request when no project is selected", async (action) => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } }));
  if (action === "preview") await previewDatasetQueryTemplate("fixed-query", {});
  else await validateDatasetQueryTemplate("fixed-query", {});
  expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({ params: {}, ...(action === "preview" ? { row_limit: 100 } : {}) });
});

const presentationReaders: Array<{ name: string; path: string; payload: unknown; read: (options?: FetchJsonOptions) => Promise<unknown> }> = [
  { name: "workflow catalog", path: "/api/workflows?enabled_only=true&search=business+name", payload: [], read: (options) => fetchWorkflows({ enabledOnly: true, search: "business name" }, options) },
  { name: "overview stats", path: "/api/workflows/workflow%20one/stats", payload: { workflow_id: "workflow one", total_runs: 2 }, read: (options) => fetchWorkflowStats("workflow one", options) },
  { name: "agent registry", path: "/api/agents", payload: [], read: (options) => fetchRegistryList("/api/agents", options) },
  { name: "project query catalog", path: "/api/datasets/query-templates?project_id=project+one", payload: { query_templates: [] }, read: (options) => fetchDatasetQueryTemplates({ projectId: "project one" }, options) },
  { name: "test-run state", path: "/api/workflows/test-runs/thread%20one", payload: { workflow_id: "workflow one", status: "completed", needs_user_approval: false }, read: (options) => fetchWorkflowTestRunState("thread one", options) },
];

it.each(presentationReaders)("keeps $name foreground by default and opts in locally without changing its endpoint", async ({ read, path, payload }) => {
  for (const options of [undefined, { presentation: "background" } as const]) {
    let resolve!: (response: Response) => void;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockReturnValue(new Promise((finish) => { resolve = finish; }));
    const request = read(options);
    try {
      expect(hasPendingRequests()).toBe(options === undefined);
      expect(fetchMock.mock.calls[0][0]).toBe(path);
      expect(fetchMock.mock.calls[0][1]).not.toHaveProperty("presentation");
      expect(fetchMock.mock.calls[0][1]?.credentials).toBe("same-origin");
      resolve(new Response(JSON.stringify(payload), { headers: { "content-type": "application/json" } }));
      await request;
    } finally {
      resolve(new Response(JSON.stringify(payload), { headers: { "content-type": "application/json" } }));
      await request;
      fetchMock.mockRestore();
    }
    expect(hasPendingRequests()).toBe(false);
  }
});


it("retains host install ownership on dataset and query responses independently of data read mode", async () => {
  const packaged = { dataset_id: "extension.install.orders", display_name: "Orders", source_install_id: "install", source_release_id: "release", read_only: true, metadata_jsonb: { connection_resolution: "project_scope" } };
  const ordinary = { dataset_id: "extension.user.orders", read_only: true, workspace_id: "", metadata_jsonb: { connection_resolution: "project_scope" } };
  const query = { query_template_id: "extension.install.query", dataset_id: packaged.dataset_id, source_install_id: "install", source_release_id: "release" };
  const respond = (payload: unknown) => new Response(JSON.stringify(payload), { headers: { "content-type": "application/json" } });
  vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
    if (String(url).includes("query-templates")) return respond({ query_templates: [query, { query_template_id: "user-query", dataset_id: packaged.dataset_id }] });
    return String(url) === "/api/datasets" ? respond([packaged, ordinary]) : respond(packaged);
  });
  const [detail, listing, queries] = await Promise.all([fetchDataset(packaged.dataset_id), fetchDatasets(), fetchDatasetQueryTemplates()]);
  expect.soft(detail).toMatchObject({ source_install_id: "install", source_release_id: "release" });
  expect.soft(listing[0]).toMatchObject({ source_install_id: "install", source_release_id: "release" });
  expect.soft(queries[0]).toMatchObject({ source_install_id: "install", source_release_id: "release" });
  expect.soft(listing[1]).not.toHaveProperty("source_install_id", "install");
  expect.soft(queries[1]).not.toHaveProperty("source_install_id", "install");
});
