import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type * as WorkflowApi from "@/pages/workflows/shared/api";

import { objectOrEmpty } from "./detail-helpers";
import {
  canvasGraphFixture,
  dryRunChangeSets,
  fetchSpy,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDetail,
  mockedFindWorkflowLinkedProjectId,
  postedChangeSets,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";

const stepId = "dataset_timeout";
const initialLabel = "Dataset timeout step";
const serverLabel = "Dataset server readback";
const baseConfig = {
  query_template_id: "query-1", params: {}, row_limit: 100,
  result_target: "state.initial_rows",
};
type ReadPhase = "initial" | "post-save" | "fresh";

function response(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200, headers: { "content-type": "application/json" },
  });
}

function gate() {
  let release: () => void = () => undefined;
  const promise = new Promise<void>((resolve) => { release = resolve; });
  return { promise, release };
}

// Real fetchWorkflowDetail/fetchJson GET and real changes POST; only HTTP is
// substituted. A distinct response is not a claim about backend normalization.
async function installDatasetTransport(config: Record<string, unknown>) {
  const actual = await vi.importActual<typeof WorkflowApi>("@/pages/workflows/shared/api");
  const previousFetch = fetchSpy.getMockImplementation();
  const previousDetail = mockedFetchWorkflowDetail.getMockImplementation();
  const previousGraph = mockedFetchWorkflowCanvasGraph.getMockImplementation();
  const requests: Array<Record<string, unknown>> = [];
  const readStarted: ReadPhase[] = [];
  const parsedReads: Array<{ phase: ReadPhase; config: Record<string, unknown> }> = [];
  const pendingReads: Array<ReturnType<typeof actual.fetchWorkflowDetail>> = [];
  const postRead = gate();
  const freshRead = gate();
  let phase: ReadPhase = "initial";
  let stored = structuredClone(config);
  let revision = 7;
  const currentConfig = () => phase === "initial" ? structuredClone(stored) : {
    ...structuredClone(stored), result_target: "state.server_rows",
  };
  const currentLabel = () => phase === "initial" ? initialLabel : serverLabel;
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  mockedFetchWorkflowDetail.mockImplementation((id) => {
    const pending = actual.fetchWorkflowDetail(id);
    pendingReads.push(pending);
    return pending;
  });
  mockedFetchWorkflowCanvasGraph.mockImplementation(async () => canvasGraphFixture({
    enabled: false, entry: { kind: "step", id: stepId },
    steps: [{
      id: stepId, step_id: stepId, kind: "dataset_query", display_name: currentLabel(),
      config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }],
    }],
  }));
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === "/api/datasets/query-templates") return response([{
      query_template_id: "query-1", dataset_id: "dataset-1", display_name: "Saved query",
      params_schema_jsonb: { type: "object", properties: {}, additionalProperties: true },
    }]);
    if (url.pathname === "/api/workflows/weekly_report" && (!init?.method || init.method === "GET")) {
      const requestedPhase = phase;
      const returnedConfig = currentConfig();
      const detail = workflowDetailFixture({
        revision, enabled: false,
        steps: [{
          id: "11111111-1111-1111-1111-111111111099", step_id: stepId,
          display_name: currentLabel(), step_kind: "dataset_query",
          override: { dataset_query: returnedConfig },
        }],
      });
      readStarted.push(requestedPhase);
      if (requestedPhase === "post-save") await postRead.promise;
      if (requestedPhase === "fresh") await freshRead.promise;
      const result = response(detail);
      const originalJson = result.json.bind(result);
      result.json = async () => {
        const body = await originalJson();
        parsedReads.push({ phase: requestedPhase, config: structuredClone(returnedConfig) });
        return body;
      };
      return result;
    }
    if (url.pathname === "/api/workflows/weekly_report/changes" && init?.method === "POST") {
      const body = objectOrEmpty(JSON.parse(String(init.body)));
      if (body.dry_run === true) return response({
        workflow_id: "weekly_report", revision: body.expected_revision,
        valid: true, applied: false, issues: [], blocking_issues: [], workflow: {},
      });
      const operations = body.operations as Array<Record<string, unknown>>;
      expect(operations).toHaveLength(1);
      expect(operations[0]).toMatchObject({ op: "update_step_config", step_id: stepId });
      requests.push(structuredClone(body));
      stored = structuredClone(objectOrEmpty(objectOrEmpty(objectOrEmpty(operations[0].patch).override).dataset_query));
      revision += 1;
      phase = "post-save";
      return response({ workflow_id: "weekly_report", revision, applied: true, valid: true, issues: [] });
    }
    if (!previousFetch) throw new Error("Missing ordinary HTTP fixture");
    return previousFetch(input, init);
  });
  return {
    requests, readStarted, parsedReads,
    returned: currentConfig,
    releasePostRead: postRead.release,
    startFreshRead: () => { phase = "fresh"; },
    releaseFreshRead: freshRead.release,
    async restore() {
      postRead.release(); freshRead.release();
      await Promise.allSettled(pendingReads);
      fetchSpy.mockReset();
      if (previousFetch) fetchSpy.mockImplementation(previousFetch);
      mockedFetchWorkflowDetail.mockReset();
      if (previousDetail) mockedFetchWorkflowDetail.mockImplementation(previousDetail);
      mockedFetchWorkflowCanvasGraph.mockReset();
      if (previousGraph) mockedFetchWorkflowCanvasGraph.mockImplementation(previousGraph);
    },
  };
}

type Boundary = Awaited<ReturnType<typeof installDatasetTransport>>;
async function openExisting() {
  fireEvent.click(await screen.findByRole("button", { name: new RegExp(initialLabel) }));
  await screen.findByRole("button", { name: "Save parameters" });
  return screen.getByLabelText("Timeout seconds");
}

async function rawDatasetConfig() {
  fireEvent.click(screen.getByRole("button", { name: "Advanced mode" }));
  fireEvent.click(await screen.findByRole("tab", { name: "Advanced" }));
  const raw = await screen.findByRole("textbox", { name: "Override JSON" });
  return objectOrEmpty(JSON.parse((raw as HTMLTextAreaElement).value).dataset_query);
}

async function saveAndReadFresh(
  boundary: Boundary,
  retireInitialOwner: () => void,
  expected: Record<string, unknown>,
) {
  fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(boundary.requests).toHaveLength(1));
  await waitFor(() => expect(boundary.readStarted).toContain("post-save"));
  expect(boundary.parsedReads.filter((read) => read.phase === "post-save")).toEqual([]);
  expect(screen.queryByText("Parameters saved")).not.toBeInTheDocument();
  expect(boundary.requests[0]).toMatchObject({
    expected_revision: 7,
    operations: [{ op: "update_step_config", step_id: stepId, patch: { override: { dataset_query: expected } } }],
  });
  const operations = boundary.requests[0].operations as Array<Record<string, unknown>>;
  expect(objectOrEmpty(objectOrEmpty(operations[0].patch).override).dataset_query).toEqual(expected);
  await act(async () => { boundary.releasePostRead(); });
  await screen.findByText("Parameters saved");
  await waitFor(() => expect(boundary.parsedReads).toContainEqual({ phase: "post-save", config: boundary.returned() }));
  retireInitialOwner();
  boundary.startFreshRead();
  // renderPage constructs a distinct QueryClient; held HTTP GET/body parsing
  // positively prevents an old local draft from acting as fresh readback.
  let fresh: ReturnType<typeof renderPage> | undefined;
  try {
    fresh = renderPage();
    await waitFor(() => expect(boundary.readStarted).toContain("fresh"));
    expect(boundary.parsedReads.filter((read) => read.phase === "fresh")).toEqual([]);
    expect(await screen.findByText("Preparing the workflow")).toBeVisible(); // the page loader paints after its 400 ms delay
    expect(screen.queryByRole("textbox", { name: "Step label" })).not.toBeInTheDocument();
    await act(async () => { boundary.releaseFreshRead(); });
    fireEvent.click(await screen.findByRole("button", { name: new RegExp(serverLabel) }));
    expect(await screen.findByRole("textbox", { name: "Step label" })).toHaveValue(serverLabel);
    expect(screen.getByLabelText("Result target")).toHaveValue("state.server_rows");
    if (typeof expected.timeout_seconds === "number" || expected.timeout_seconds === "") {
      expect(screen.getByLabelText("Timeout seconds")).toHaveDisplayValue(String(expected.timeout_seconds));
    }
    await waitFor(() => expect(boundary.parsedReads).toContainEqual({ phase: "fresh", config: boundary.returned() }));
    expect(await rawDatasetConfig()).toEqual({ ...expected, result_target: "state.server_rows" });
    expect(boundary.requests).toHaveLength(1);
    expect(postedChangeSets()).toHaveLength(1);
  } finally {
    fresh?.unmount();
  }
}

describe("Dataset timeout through public fields, Save and fresh HTTP readback", () => {
  it.each([
    { raw: "0", expected: 0 },
    { raw: "1.5", expected: 1.5 },
    { raw: "1e2", expected: 100 },
    { raw: "", expected: "" },
  ])("preserves the explicit numeric/blank edit $raw without replacing it by30", async ({ raw, expected }) => {
    const boundary = await installDatasetTransport({ ...baseConfig, timeout_seconds: 45 });
    let view: ReturnType<typeof renderPage> | undefined;
    try {
      view = renderPage();
      fireEvent.change(await openExisting(), { target: { value: raw } });
      await saveAndReadFresh(boundary, () => { view?.unmount(); view = undefined; }, { ...baseConfig, timeout_seconds: expected });
    } finally {
      view?.unmount();
      await boundary.restore();
    }
  }, 15_000);

  it("retains unfinished numeric text across Close/reopen without an automatic write", async () => {
    const boundary = await installDatasetTransport({ ...baseConfig, timeout_seconds: 45 });
    let view: ReturnType<typeof renderPage> | undefined;
    try {
      view = renderPage();
      const input = await openExisting();
      fireEvent.change(input, { target: { value: "1e" } });
      expect(input).toHaveDisplayValue("1e");
      fireEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
      const reopened = await openExisting();
      expect(reopened).toHaveDisplayValue("1e");
      expect(boundary.requests).toEqual([]);
      expect(postedChangeSets()).toEqual([]);
      // No Save is attempted: this is not a new backend/readiness veto claim.
    } finally {
      view?.unmount();
      await boundary.restore();
    }
  });

  it.each([
    { label: "omitted", timeout: {} },
    { label: "null", timeout: { timeout_seconds: null } },
    { label: "false", timeout: { timeout_seconds: false } },
    { label: "unsupported imported text", timeout: { timeout_seconds: "abc" } },
  ])("keeps untouched $label timeout through sibling edit and a fresh read owner", async ({ timeout }) => {
    const initial = { ...baseConfig, ...timeout };
    const boundary = await installDatasetTransport(initial);
    let view: ReturnType<typeof renderPage> | undefined;
    try {
      view = renderPage();
      await openExisting();
      fireEvent.change(screen.getByLabelText("Result target"), { target: { value: "state.edited_rows" } });
      await saveAndReadFresh(boundary, () => { view?.unmount(); view = undefined; }, { ...initial, result_target: "state.edited_rows" });
    } finally {
      view?.unmount();
      await boundary.restore();
    }
  }, 15_000);

  it("keeps the explicit new-Dataset creation default30 when staging without a durable write", async () => {
    const boundary = await installDatasetTransport({ ...baseConfig, timeout_seconds: 45 });
    let view: ReturnType<typeof renderPage> | undefined;
    try {
      view = renderPage();
      fireEvent.click(await screen.findByRole("button", { name: /^\+ Add Step$/ }));
      fireEvent.change(await screen.findByRole("textbox", { name: "Step label" }), { target: { value: "New Dataset" } });
      fireEvent.change(screen.getByRole("combobox", { name: "Step kind" }), { target: { value: "dataset_query" } });
      expect(await screen.findByLabelText("Timeout seconds")).toHaveDisplayValue("30");
      fireEvent.click(screen.getByRole("button", { name: "Add step to changes" }));
      await waitFor(() => expect(dryRunChangeSets().flatMap((body) => Array.isArray(body.operations) ? body.operations : [])).toEqual(expect.arrayContaining([
        expect.objectContaining({ op: "create_step", step: expect.objectContaining({
          step_kind: "dataset_query", override: expect.objectContaining({ dataset_query: expect.objectContaining({ timeout_seconds: 30 }) }),
        }) }),
      ])));
      expect(boundary.requests).toEqual([]);
      expect(postedChangeSets()).toEqual([]);
    } finally {
      view?.unmount();
      await boundary.restore();
    }
  }, 15_000);
});
