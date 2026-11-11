import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";
import { queryOutputFields, queryParameterNames, outputSchemaFromFields, outputSchemaWithField } from "./dataset-detail-helpers";
import { SavedQuerySchemaBuilders } from "./saved-query-schema-builders";
import { RegistryDatasetsDetailPage } from "./index";

const definition = { resource_id: "events", operation: "find", arguments: { sample: { values: [false, 0, null, { nested: [1, "x"] }] }, literal: ":ignored", filter: { enabled: { $param: "enabled" }, count: { $param: "count" }, absent: { $param: "absent" }, nested: [{ $param: "nested" }] } } };
const schema = { type: "object", properties: { enabled: { type: "boolean", default: false }, count: { type: "integer", default: 0, minimum: 0 }, absent: { type: "null", default: null }, nested: { type: "object", default: { values: [false, 0, null] }, "x-owner": "keep" } } };

it.each([false, true])("saves JSON exactly, recovers malformed input and previews typed params (create=%s)", async (create) => {
  let saved = { query_template_id: "q1", dataset_id: "events", display_name: "JSON query", query_language: "json", query_definition_jsonb: definition, params_schema_jsonb: schema, output_schema_jsonb: {}, enabled: true, version: 1 };
  const writes: Record<string, unknown>[] = [];
  const previews: Record<string, unknown>[] = [];
  const response = (data: unknown) => new Response(JSON.stringify(data), { headers: { "content-type": "application/json" } });
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = new URL(String(input), "http://localhost").pathname;
    if (path.endsWith("/preview")) { previews.push(JSON.parse(String(init?.body))); return response({ ok: true, result: { rows: [] } }); }
    if (path.includes("query-templates") && ["PUT", "POST"].includes(init?.method || "")) { const body = JSON.parse(String(init?.body)); writes.push(body); saved = { ...saved, ...body }; return response({ query_template_id: "q1" }); }
    if (path.endsWith("query-templates")) return response({ query_templates: create && !writes.length ? [] : [saved] });
    if (path.endsWith("connectors")) return response([{ connector_id: "mongo", query_language: "json" }]);
    if (path.endsWith("data-connections")) return response([{ connection_id: "conn", workspace_id: "w", query_capable: true, connector_ref_jsonb: { connector_id: "mongo" } }]);
    if (path.endsWith("projects")) return response({ project_ids: [], available_projects: [] });
    return response({ dataset_id: "events", display_name: "Events", workspace_id: "w", query_language: "sql", metadata_jsonb: { connection_id: "conn" }, schema_snapshot_jsonb: { resources: [{ resource_id: "events", columns: [] }] } });
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/registry/datasets/events?tab=saved-queries"]}><Routes><Route path="/registry/datasets/:datasetId" element={<RegistryDatasetsDetailPage />} /></Routes></MemoryRouter></QueryClientProvider>);
  await userEvent.click(await screen.findByRole("tab", { name: "Saved queries" }));
  if (create) await userEvent.click(await screen.findByRole("button", { name: "Add query" }));
  else { await userEvent.click(await screen.findByRole("button", { name: "Actions for JSON query" })); await userEvent.click(await screen.findByRole("menuitem", { name: "Edit" })); }
  const dialog = await screen.findByRole("dialog", { name: create ? "Add query" : "Edit query" });
  const editor = within(dialog).getByLabelText("JSON query definition");
  if (!create) expect(JSON.parse((editor as HTMLTextAreaElement).value)).toEqual(definition);
  fireEvent.change(within(dialog).getByLabelText("Query name"), { target: { value: "JSON query" } });
  fireEvent.change(editor, { target: { value: "{" } });
  await userEvent.click(within(dialog).getByRole("button", { name: "Save query" }));
  expect(await within(dialog).findByRole("alert")).toBeVisible();
  expect(writes).toHaveLength(0);
  fireEvent.change(editor, { target: { value: JSON.stringify(definition) } });
  expect(within(dialog).queryByText(":ignored")).not.toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole("button", { name: "Save query" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]?.query_language).toBe("json");
  expect(writes[0]?.query_definition_jsonb).toEqual(definition);
  if (!create) expect(writes[0]?.params_schema_jsonb).toEqual(schema);
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await userEvent.click(screen.getByRole("button", { name: "Actions for JSON query" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Preview" }));
  const preview = await screen.findByLabelText("Preview parameters");
  if (!create) expect(JSON.parse((preview as HTMLTextAreaElement).value)).toEqual({ enabled: false, count: 0, absent: null, nested: { values: [false, 0, null] } });
  await userEvent.click(screen.getByRole("button", { name: "Run preview" }));
  await waitFor(() => expect(previews).toHaveLength(1));
  if (!create) expect(previews[0]?.params).toEqual({ enabled: false, count: 0, absent: null, nested: { values: [false, 0, null] } });
  view.unmount(); client.clear(); vi.restoreAllMocks();
});

it("extracts only exact argument markers and retains structured output types for JSON", () => {
  expect(queryParameterNames(JSON.stringify({ ...definition, ignored: { $param: "outside" }, arguments: { literal: ":ignored", marker: { $param: "good" }, invalid: { $param: "bad-name" }, extra: { $param: "not_marker", other: true } } }), "json")).toEqual(["good"]);
  const resources = [{ resource_id: "events", columns: [{ name: "payload", value_type: "object" }, { name: "items", value_type: "array" }, { name: "empty", value_type: "null" }] }];
  expect(queryOutputFields(resources, "json").map(field => field.type)).toEqual(["object", "array", "null"]);
  expect(queryOutputFields(resources).map(field => field.type)).toEqual(["string", "string", "string"]);
});

it.each(["connectors", "data-connections", "failed-connectors"])("waits for installed language and recovers loading (%s)", async (delayed) => {
  let release: () => void = () => {};
  const pending = new Promise<void>(resolve => { release = resolve; });
  let requests = 0;
  const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
  vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
    const path = new URL(String(input), "http://localhost").pathname;
    if (path.endsWith("query-templates")) return response({ query_templates: [] });
    if (path.endsWith("connectors")) {
      requests += 1;
      if (delayed === "connectors") await pending;
      if (delayed === "failed-connectors" && requests === 1) return response({ detail: "Unavailable" }, 503);
      return response([{ connector_id: "mongo", query_language: "json" }]);
    }
    if (path.endsWith("data-connections")) {
      if (delayed === "data-connections") await pending;
      return response([{ connection_id: "conn", workspace_id: "w", query_capable: true, connector_ref_jsonb: { connector_id: "mongo" } }]);
    }
    if (path.endsWith("projects")) return response({ project_ids: [], available_projects: [] });
    return response({ dataset_id: "events", display_name: "Events", workspace_id: "w", query_language: "sql", metadata_jsonb: { connection_id: "conn" } });
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/registry/datasets/events?tab=saved-queries"]}><Routes><Route path="/registry/datasets/:datasetId" element={<RegistryDatasetsDetailPage />} /></Routes></MemoryRouter></QueryClientProvider>);
  const add = await screen.findByRole("button", { name: "Add query" });
  expect(add).toBeDisabled();
  await userEvent.click(add);
  expect(screen.queryByRole("dialog", { name: "Add query" })).not.toBeInTheDocument();
  if (delayed === "failed-connectors") await userEvent.click(await screen.findByRole("button", { name: "Retry connection details" }));
  else { expect(await screen.findByText("Loading connection details…")).toBeVisible(); release(); }
  await waitFor(() => expect(add).toBeEnabled());
  await userEvent.click(add);
  expect(await screen.findByLabelText("JSON query definition")).toBeVisible();
  view.unmount(); client.clear(); vi.restoreAllMocks();
});

it("uses unconstrained output schemas for introspected arbitrary JSON without changing SQL or saved unions", () => {
  const resources = [{ resource_id: "values", columns: [{ name: "value", value_type: "json" }] }];
  const fields = queryOutputFields(resources, "json");
  expect(fields[0]?.type).toBe("json");
  expect(outputSchemaFromFields(fields)).toEqual({ type: "object", properties: { value: {} } });
  expect(queryOutputFields(resources)[0]?.type).toBe("string");
  const union = { type: "object", properties: { value: { anyOf: [{ type: "object" }, { type: "null" }], description: "Keep" } } };
  expect(outputSchemaWithField(union, fields[0]!, true)).toEqual(union);
  const onOutputSchemaChange = vi.fn();
  render(<SavedQuerySchemaBuilders outputSchema={{}} paramsSchema={{}} resources={resources} queryLanguage="json" sql={JSON.stringify({ resource_id: "values", operation: "get", arguments: {} })} selectAllOutputByDefault onOutputSchemaChange={onOutputSchemaChange} onParamsSchemaChange={vi.fn()} />);
  expect(screen.getByText("JSON")).toBeVisible();
  expect(onOutputSchemaChange).toHaveBeenCalledWith({ type: "object", properties: { value: {} } });
});
