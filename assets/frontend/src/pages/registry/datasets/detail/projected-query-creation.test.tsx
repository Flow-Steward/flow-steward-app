import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { RegistryDatasetsDetailPage } from "./index";

function projectedFixture(language = "json") {
  return {
    dataset: {
      dataset_id: "extension.install-1.orders", account_id: "account-1", workspace_id: "",
      display_name: "Package orders", availability_mode: "specific_projects_only",
      connector_kind: "extension", connector_id: "orders_connector", runtime_kind: "local_extension",
      query_language: language === "json" ? "sql" : "json", read_only: true, enabled: true,
      source_install_id: "install-1", source_release_id: "release-1",
      metadata_jsonb: {
        account_id: "account-1", connection_resolution: "project_scope",
        projection_bridge: { immutable: true, install_id: "install-1", release_id: "release-1", artifact_kind: "dataset", artifact_id: "orders" },
      },
    },
    connectors: [{
      connector_id: "extension.install-1.orders_connector", local_connector_id: "orders_connector",
      display_name: "Orders connector", connector_kind: "extension", runtime_kind: "local_extension",
      query_language: language, source_install_id: "install-1", source_release_id: "release-1", account_id: "account-1",
    }],
  };
}

function renderProjected(fixture = projectedFixture(), options: { delayed?: "connectors" | "dataset"; failCatalog?: boolean } = {}) {
  const writes: Record<string, unknown>[] = [];
  let release: () => void = () => {};
  const pending = new Promise<void>(resolve => { release = resolve; });
  let catalogRequests = 0;
  const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = new URL(String(input), "http://localhost").pathname;
    if (path.endsWith("query-templates")) {
      if (init?.method === "POST") {
        writes.push(JSON.parse(String(init.body)));
        return response({ query_template_id: "user-query" });
      }
      return response({ query_templates: [] });
    }
    if (path.endsWith("connectors")) {
      catalogRequests += 1;
      if (options.delayed === "connectors") await pending;
      if (options.failCatalog && catalogRequests === 1) return response({ detail: "Unavailable" }, 503);
      return response(fixture.connectors);
    }
    if (path.endsWith("data-connections")) return response([]);
    if (path.endsWith("projects")) return response({ project_ids: [], available_projects: [] });
    if (options.delayed === "dataset") await pending;
    return response(fixture.dataset);
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/registry/datasets/extension.install-1.orders?tab=saved-queries"]}><Routes><Route path="/registry/datasets/:datasetId" element={<RegistryDatasetsDetailPage />} /></Routes></MemoryRouter></QueryClientProvider>);
  return { writes, release, cleanup: () => { view.unmount(); client.clear(); vi.restoreAllMocks(); } };
}

it.each(["sql", "json"])("creates a user-owned %s query from the active projected connector without a permanent connection", async language => {
  const fixture = projectedFixture(language);
  const utils = renderProjected(fixture);
  try {
    const add = await screen.findByRole("button", { name: "Add query" });
    await waitFor(() => expect(add).toBeEnabled());
    await userEvent.click(add);
    const dialog = within(await screen.findByRole("dialog", { name: "Add query" }));
    fireEvent.change(dialog.getByLabelText("Query name"), { target: { value: "My orders" } });
    const definition = language === "json" ? { resource_id: "orders", operation: "find", arguments: { enabled: false, count: 0, missing: null, values: [] } } : { sql: "SELECT * FROM orders" };
    const editor = dialog.getByLabelText(language === "json" ? "JSON query definition" : "SQL query");
    expect(editor).toHaveValue(language === "json" ? "{}" : "");
    fireEvent.change(editor, { target: { value: language === "json" ? JSON.stringify(definition) : definition.sql } });
    await userEvent.click(dialog.getByRole("button", { name: "Save query" }));
    await waitFor(() => expect(utils.writes).toHaveLength(1));
    expect(utils.writes[0]).toMatchObject({ dataset_id: "extension.install-1.orders", query_language: language, query_definition_jsonb: definition });
    expect(utils.writes[0]).not.toHaveProperty("source_install_id");
    expect(utils.writes[0]).not.toHaveProperty("connection_id");
    expect(fixture.dataset.metadata_jsonb).not.toHaveProperty("connection_id");
  } finally { utils.cleanup(); }
});

it.each([
  "missing", "foreign-install", "foreign-account", "ambiguous", "noncanonical-connector",
  "foreign-handle", "mutable-bridge", "wrong-artifact", "missing-account", "conflicting-account", "missing-language",
])("blocks projected query creation for %s ownership or catalog data", async invalid => {
  const fixture = projectedFixture();
  if (invalid === "missing") fixture.connectors = [];
  if (invalid === "foreign-install") fixture.connectors[0].source_install_id = "install-2";
  if (invalid === "foreign-account") fixture.connectors[0].account_id = "account-2";
  if (invalid === "ambiguous") fixture.connectors.push({ ...fixture.connectors[0] });
  if (invalid === "noncanonical-connector") fixture.connectors[0].connector_id = "orders_connector";
  if (invalid === "foreign-handle") fixture.dataset.dataset_id = "extension.install-2.orders";
  if (invalid === "mutable-bridge") fixture.dataset.metadata_jsonb.projection_bridge.immutable = false;
  if (invalid === "wrong-artifact") fixture.dataset.metadata_jsonb.projection_bridge.artifact_kind = "saved_query";
  if (invalid === "missing-account") { fixture.dataset.account_id = ""; fixture.dataset.metadata_jsonb.account_id = ""; }
  if (invalid === "conflicting-account") fixture.dataset.account_id = "account-2";
  if (invalid === "missing-language") fixture.connectors[0].query_language = "";
  const utils = renderProjected(fixture);
  try {
    const add = await screen.findByRole("button", { name: "Add query" });
    await screen.findByRole("button", { name: "Retry connection details" });
    expect(add).toBeDisabled();
    await userEvent.click(add);
    expect(screen.queryByRole("dialog", { name: "Add query" })).not.toBeInTheDocument();
    expect(utils.writes).toHaveLength(0);
  } finally { utils.cleanup(); }
});

it.each(["connectors", "dataset", "failed-catalog"] as const)("waits for verified projected context and recovers %s loading", async delayed => {
  const utils = renderProjected(projectedFixture(), delayed === "failed-catalog" ? { failCatalog: true } : { delayed });
  try {
    const add = await screen.findByRole("button", { name: "Add query" });
    expect(add).toBeDisabled();
    await userEvent.click(add);
    expect(screen.queryByRole("dialog", { name: "Add query" })).not.toBeInTheDocument();
    if (delayed === "failed-catalog") await userEvent.click(await screen.findByRole("button", { name: "Retry connection details" }));
    else utils.release();
    await waitFor(() => expect(add).toBeEnabled());
    await userEvent.click(add);
    expect(await screen.findByLabelText("JSON query definition")).toHaveValue("{}");
  } finally { utils.release(); utils.cleanup(); }
});
