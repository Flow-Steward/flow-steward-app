import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { RegistryDatasetsDetailPage } from "@/pages/registry/datasets/detail";
import { SavedQuerySchemaBuilders } from "./saved-query-schema-builders";
import { outputSchemaWithField, outputSchemaWithFormat, paramsSchemaForSql, queryParameterFields } from "./dataset-detail-helpers";

function richSchema() {
  return {
    type: "object",
    additionalProperties: false,
    required: ["event"],
    properties: {
      event: {
        type: "string", format: "date", default: "2026-10-05", minLength: 1,
        maxLength: 100, pattern: "^2026", description: "Chosen event date",
        enum: ["2026-10-05", "2026-10-06"], "x-owner": { source: "operator" },
      },
      limit: { type: "integer", default: 10, minimum: 1, maximum: 100, multipleOf: 1 },
      payload: { type: "object", default: {}, properties: { enabled: { type: "boolean" } } },
      categories: { type: "array", default: [], items: { type: "string" }, minItems: 0 },
      absent: { type: "null", default: null, "x-purpose": "sentinel" },
    },
  };
}

const sql = "SELECT event FROM events WHERE event = :event";

describe("Saved query schema lossless helper", () => {
  it("preserves defaults, constraints, unsupported editor types and property extensions", () => {
    const schema = richSchema();
    const before = structuredClone(schema);
    expect(paramsSchemaForSql(sql, schema)).toEqual(before);
    expect(schema).toEqual(before);
  });

  it("does not erase unknown top-level keys to make a backend-rejected schema appear valid", () => {
    const schema = { ...richSchema(), "x-legacy-owner": { team: "ops" }, title: "Legacy schema" };
    expect(paramsSchemaForSql(sql, schema)).toEqual(schema);
  });
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/registry/datasets/events?tab=saved-queries"]}>
        <Routes>
          <Route element={<RegistryDatasetsDetailPage />} path="/registry/datasets/:datasetId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, dispose: () => { view.unmount(); client.clear(); } };
}

async function openEdit(name: string) {
  const tab = await screen.findByRole("tab", { name: "Saved queries" });
  await userEvent.click(tab);
  await userEvent.click(await screen.findByRole("button", { name: `Actions for ${name}` }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Edit" }));
  return screen.findByRole("dialog", { name: "Edit query" });
}

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status, headers: { "content-type": "application/json" },
  });
}

function httpStore(schema: Record<string, unknown>, rejectUnknownTopLevel = false, outputSchema: Record<string, unknown> = {}) {
  // HTTP-boundary double, NOT an API function mock. The production page,
  // helper, mutations, fetchJson and API normalization all run unchanged.
  let saved = {
    query_template_id: "query-1", dataset_id: "events", display_name: "Original query",
    description: "", query_language: "sql", query_definition_jsonb: { sql },
    params_schema_jsonb: structuredClone(schema), output_schema_jsonb: structuredClone(outputSchema),
    validation_result_jsonb: {}, enabled: true, version: 1,
  };
  const puts: Record<string, unknown>[] = [];
  let gets = 0;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method || "GET";
    if (url.pathname === "/api/datasets/query-templates/query-1" && method === "PUT") {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      puts.push(structuredClone(body));
      const incoming = body.params_schema_jsonb as Record<string, unknown>;
      // Existing backend validator rejects unknown TOP-LEVEL keys. Do not
      // pretend such a schema has production acceptance in this frontend test.
      if (rejectUnknownTopLevel && Object.hasOwn(incoming, "x-legacy-owner")) {
        return jsonResponse({ detail: "Unsupported parameter schema field" }, 422);
      }
      saved = structuredClone({ ...saved, ...body, version: saved.version + 1 }) as typeof saved;
      return jsonResponse({ status: "updated", query_template_id: "query-1" });
    }
    if (url.pathname === "/api/datasets/query-templates" && method === "GET") {
      gets += 1;
      return jsonResponse({ query_templates: [structuredClone(saved)] });
    }
    if (url.pathname === "/api/datasets/connectors") return jsonResponse([]);
    if (url.pathname === "/api/datasets/data-connections") return jsonResponse([]);
    if (url.pathname === "/api/datasets/events/projects") {
      return jsonResponse({ dataset_id: "events", workspace_id: "workspace-1", project_ids: [], available_projects: [] });
    }
    if (url.pathname === "/api/datasets/events") {
      return jsonResponse({
        dataset_id: "events", workspace_id: "workspace-1", display_name: "Events",
        availability_mode: "all_workspace_projects", connector_kind: "extension",
        runtime_kind: "local_extension", query_language: "sql", read_only: true, enabled: true,
        schema_snapshot_jsonb: { resources: [{ resource_id: "events", columns: [{ name: "event", value_type: "string" }] }] },
      });
    }
    throw new Error(`Unexpected HTTP operation ${method} ${url.pathname}`);
  });
  return { puts, read: () => structuredClone(saved), getCount: () => gets };
}

describe("Saved query page schema save and fresh readback", () => {
  afterEach(() => { vi.restoreAllMocks(); });

  it.each(["name-only", "one-format"])("preserves the complete schema after %s edit and fresh reopen", async (edit) => {
    const original = richSchema();
    const store = httpStore(original);
    const { dispose: disposeFirst } = renderPage();
    let dialog = await openEdit("Original query");
    fireEvent.change(within(dialog).getByLabelText("Query name"), { target: { value: "Renamed query" } });
    let expected = structuredClone(original);
    if (edit === "one-format") {
      await userEvent.click(within(dialog).getByRole("combobox", { name: "Parameter format for event" }));
      await userEvent.click(await screen.findByRole("option", { name: "Date and time" }));
      expected = { ...expected, properties: { ...expected.properties, event: { ...expected.properties.event, format: "date-time" } } };
    }
    await userEvent.click(within(dialog).getByRole("button", { name: "Save query" }));
    await waitFor(() => expect(store.puts).toHaveLength(1));
    expect(store.puts[0]?.params_schema_jsonb).toEqual(expected);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit query" })).not.toBeInTheDocument());
    expect(store.read().params_schema_jsonb).toEqual(expected);
    const previousGets = store.getCount();
    disposeFirst();

    // New QueryClient guarantees a fresh GET, not cached state from save.
    const { dispose: disposeSecond } = renderPage();
    dialog = await openEdit("Renamed query");
    expect(store.getCount()).toBeGreaterThan(previousGets);
    expect(within(dialog).getByLabelText("Query name")).toHaveValue("Renamed query");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save query" }));
    await waitFor(() => expect(store.puts).toHaveLength(2));
    expect(store.puts[1]?.params_schema_jsonb).toEqual(expected);
    expect(store.read().params_schema_jsonb).toEqual(expected);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit query" })).not.toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Actions for Renamed query" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Preview" }));
    const preview = await screen.findByRole("dialog", { name: "Renamed query" });
    // Defaults have an actual UI consumer: fresh-read preview values.
    expect(JSON.parse((within(preview).getByLabelText("Preview parameters") as HTMLTextAreaElement).value)).toEqual({
      event: "2026-10-05", limit: 10, payload: {}, categories: [], absent: null,
    });
    disposeSecond();
  });

  it("preserves legacy top-level keys and saved data when the backend rejects the edit", async () => {
    const original = { ...richSchema(), "x-legacy-owner": { team: "ops" } };
    const store = httpStore(original, true);
    const { dispose: disposeFirst } = renderPage();
    const dialog = await openEdit("Original query");
    fireEvent.change(within(dialog).getByLabelText("Query name"), { target: { value: "Must not persist" } });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save query" }));
    await waitFor(() => expect(store.puts).toHaveLength(1));
    expect(store.puts[0]?.params_schema_jsonb).toEqual(original);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Unsupported parameter schema field");
    expect(store.read().display_name).toBe("Original query");
    expect(store.read().params_schema_jsonb).toEqual(original);
    const previousGets = store.getCount();
    disposeFirst();
    const { dispose: disposeSecond } = renderPage();
    await openEdit("Original query");
    expect(store.getCount()).toBeGreaterThan(previousGets);
    expect(store.read().params_schema_jsonb).toEqual(original);
    disposeSecond();
  });
});

function richOutputSchema() {
  return {
    type: "object", title: "Selected event output", additionalProperties: false,
    required: ["event"], "x-output-owner": { team: "ops" },
    properties: {
      event: { type: "string", format: "date", default: "2026-10-05", minLength: 1, description: "Selected date", "x-column": "event" },
      category: { type: "string", default: "general", enum: ["general", "special"] },
    },
  };
}

describe("Saved query honest controls and output preservation", () => {
  afterEach(() => { vi.restoreAllMocks(); });

  it.each([
    { type: ["string", "null"], default: "kept" },
    { default: "kept" },
  ])("does not present a legacy unsupported raw type as an editable Text field (%j)", async (definition) => {
    // Presentation/preservation only: these definitions are not accepted by
    // the application's backend schema contract; no fake successful save.
    const schema = { type: "object", properties: { value: definition }, required: [] };
    const onChange = vi.fn();
    render(<SavedQuerySchemaBuilders outputSchema={{}} paramsSchema={schema} resources={[]} sql="SELECT :value"
      selectAllOutputByDefault={false} onOutputSchemaChange={vi.fn()} onParamsSchemaChange={onChange} />);
    expect(screen.queryByRole("combobox", { name: "Parameter type for value" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Parameter type for value")).toHaveTextContent("Unsupported type (read-only)");
    expect(screen.queryByRole("combobox", { name: "Parameter format for value" })).not.toBeInTheDocument();
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await user.click(screen.getByRole("checkbox", { name: "Required" }));
    expect(onChange).toHaveBeenCalledWith({ ...schema, required: ["value"] });
    expect(schema.required).toEqual([]);
    expect(schema.properties.value).toEqual(definition);
  });

  it("projects existing schema flags faithfully while keeping new SQL placeholders required", () => {
    const schema = { type: "object", properties: { event: { type: "string", default: "sample", minLength: 1 } }, required: [] };
    const before = structuredClone(schema);
    expect(queryParameterFields(sql, schema)[0]?.required).toBe(false);
    expect(queryParameterFields(sql, {})[0]?.required).toBe(true);
    expect(paramsSchemaForSql(sql, schema)).toEqual(schema);
    expect(schema).toEqual(before);
  });

  it("shows exact existing object, array and null types read-only, then saves them unchanged", async () => {
    const schema = richSchema();
    const store = httpStore(schema);
    const view = renderPage();
    const dialog = await openEdit("Original query");
    for (const [name, label] of [["payload", "Object"], ["categories", "Array"], ["absent", "Null"]]) {
      expect(within(dialog).queryByRole("combobox", { name: `Parameter type for ${name}` })).not.toBeInTheDocument();
      expect(within(dialog).getByLabelText(`Parameter type for ${name}`)).toHaveTextContent(`${label} (read-only)`);
      expect(within(dialog).queryByRole("combobox", { name: `Parameter format for ${name}` })).not.toBeInTheDocument();
    }
    await userEvent.click(within(dialog).getByRole("button", { name: "Save query" }));
    await waitFor(() => expect(store.puts).toHaveLength(1));
    expect(store.puts[0]?.params_schema_jsonb).toEqual(schema);
    expect(store.read().params_schema_jsonb).toEqual(schema);
    view.dispose();
  });

  it("keeps a real SQL property Required uncheck after save, fresh GET and reopen", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const schema = richSchema();
    const store = httpStore(schema);
    const { dispose: disposeFirst } = renderPage();
    let dialog = await openEdit("Original query");
    const required = within(dialog).getAllByRole("checkbox", { name: "Required" })[0];
    expect(required).toBeChecked();
    await user.click(required);
    expect(required).not.toBeChecked();
    await userEvent.click(within(dialog).getByRole("button", { name: "Save query" }));
    await waitFor(() => expect(store.puts).toHaveLength(1));
    const expected = { ...schema, required: [] };
    expect(store.puts[0]?.params_schema_jsonb).toEqual(expected);
    expect(store.read().params_schema_jsonb).toEqual(expected);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit query" })).not.toBeInTheDocument());
    const previousGets = store.getCount();
    disposeFirst();
    const { dispose: disposeSecond } = renderPage();
    dialog = await openEdit("Original query");
    expect(within(dialog).getAllByRole("checkbox", { name: "Required" })[0]).not.toBeChecked();
    expect(store.getCount()).toBeGreaterThan(previousGets);
    await userEvent.click(within(dialog).getByRole("button", { name: "Save query" }));
    await waitFor(() => expect(store.puts).toHaveLength(2));
    expect(store.puts[1]?.params_schema_jsonb).toEqual(expected);
    disposeSecond();
  });

  it("still permits a real schema requirement change for a non-SQL property without losing its definition", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const schema = richSchema();
    const store = httpStore(schema);
    const view = renderPage();
    const dialog = await openEdit("Original query");
    const required = within(dialog).getAllByRole("checkbox", { name: "Required" })[1];
    expect(required).not.toBeChecked();
    await user.click(required);
    expect(required).toBeChecked();
    await userEvent.click(within(dialog).getByRole("button", { name: "Save query" }));
    await waitFor(() => expect(store.puts).toHaveLength(1));
    expect(store.puts[0]?.params_schema_jsonb).toEqual({ ...schema, required: ["event", "limit"] });
    view.dispose();
  });

  it("preserves output schema metadata and existing definitions when editing one field", () => {
    const schema = richOutputSchema();
    const before = structuredClone(schema);
    expect(outputSchemaWithFormat(schema, "event", "date-time")).toEqual({
      ...schema, properties: { ...schema.properties, event: { ...schema.properties.event, format: "date-time" } },
    });
    expect(outputSchemaWithField(schema, { name: "event", resourceId: "events", type: "string", format: "" }, true)).toEqual(schema);
    expect(outputSchemaWithField(schema, { name: "amount", resourceId: "events", type: "integer", format: "" }, true)).toEqual({
      ...schema, properties: { ...schema.properties, amount: { type: "integer" } },
    });
    expect(outputSchemaWithField(schema, { name: "event", resourceId: "events", type: "string", format: "" }, false)).toEqual({
      ...schema, required: [], properties: { category: schema.properties.category },
    });
    expect(schema).toEqual(before);
  });

  it("saves one output format edit losslessly and keeps it after fresh GET and reopen", async () => {
    const schema = richOutputSchema();
    const params = richSchema();
    const store = httpStore(params, false, schema);
    const { dispose: disposeFirst } = renderPage();
    let dialog = await openEdit("Original query");
    await userEvent.click(await within(dialog).findByRole("combobox", { name: "Output format for event" }));
    await userEvent.click(await screen.findByRole("option", { name: "Date and time" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save query" }));
    const expected = { ...schema, properties: { ...schema.properties, event: { ...schema.properties.event, format: "date-time" } } };
    await waitFor(() => expect(store.puts).toHaveLength(1));
    expect(store.puts[0]?.output_schema_jsonb).toEqual(expected);
    expect(store.puts[0]?.params_schema_jsonb).toEqual(params);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit query" })).not.toBeInTheDocument());
    const previousGets = store.getCount();
    disposeFirst();
    const { dispose: disposeSecond } = renderPage();
    dialog = await openEdit("Original query");
    expect(store.getCount()).toBeGreaterThan(previousGets);
    expect(await within(dialog).findByRole("combobox", { name: "Output format for event" })).toHaveTextContent("Date and time");
    await userEvent.click(within(dialog).getByRole("button", { name: "Save query" }));
    await waitFor(() => expect(store.puts).toHaveLength(2));
    expect(store.puts[1]?.output_schema_jsonb).toEqual(expected);
    expect(store.read().output_schema_jsonb).toEqual(expected);
    disposeSecond();
  });
});
