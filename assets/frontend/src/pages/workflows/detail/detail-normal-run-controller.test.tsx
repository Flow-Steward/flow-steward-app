import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useRef } from "react";
import { useWorkflowNormalRunController } from "./detail-normal-run-controller";

const contract = { interface: { enabled: true, trigger_channels: ["ui"], expected_inputs: [
  { name: "count", type: "number", required: true },
] } };
const defaults = { workflowId: "stock", projectId: "project-1", revision: 7, enabled: true, canRun: true,
  blockedReason: "", contract, contractReady: true, summary: "Import stock", inputs: { count: "4" },
  fieldErrors: {}, testBusy: false, open: true };
function Harness({ options = {} }: { options?: Partial<Parameters<typeof useWorkflowNormalRunController>[0]> }) {
  const launchLock = useRef<symbol | null>(null);
  const run = useWorkflowNormalRunController({ ...defaults, launchLock, ...options });
  return <>
    <input aria-label="Acknowledge" type="checkbox" checked={run.acknowledged} onChange={(event) => run.setAcknowledged(event.target.checked)} />
    <input aria-label="Actual file" type="file" onChange={(event) => run.setFile("source", event.target.files?.[0])} />
    <button disabled={run.blocked || run.busy || !run.acknowledged} onClick={() => { void run.start(); }}>Run workflow</button>
    <button onClick={() => { void run.start(); void run.start(); }}>Race submit</button>
    {run.jobId ? <a href={`/jobs/${run.jobId}`}>Job Details</a> : null}
    <p role="status">{run.message}</p>
  </>;
}
const response = (job_id = "normal-91") => new Response(JSON.stringify({ job_id }), { headers: { "content-type": "application/json" } });
let fetchSpy: ReturnType<typeof vi.spyOn>;
beforeEach(() => { fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(response()); });
afterEach(() => vi.restoreAllMocks());

it("posts only the saved ordinary request after acknowledgement and follows its exact job", async () => {
  render(<Harness />);
  expect(screen.getByRole("button", { name: "Run workflow" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
  expect(await screen.findByRole("link", { name: "Job Details" })).toHaveAttribute("href", "/jobs/normal-91");
  expect(fetchSpy).toHaveBeenCalledTimes(1);
  const [url, init] = fetchSpy.mock.calls[0];
  expect(url).toBe("/api/workflows/stock/run");
  expect(JSON.parse(String(init?.body))).toEqual({ project_id: "project-1", expected_revision: 7,
    acknowledged: true, summary: "Import stock", inputs: { count: 4 }, files: [] });
  expect(new Headers(init?.headers).get("Idempotency-Key")).toBeTruthy();
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  expect(localStorage.getItem("fs.workflow-run-job.stock")).toBeNull();
});

it.each([
  { enabled: false }, { canRun: false }, { projectId: "" }, { revision: null }, { revision: -1 }, { revision: 1.5 },
  { blockedReason: "Apply changes" }, { blockedReason: "Save failed" }, { testBusy: true },
  { contractReady: false }, { summary: " " }, { fieldErrors: { "inputs.count": "Required" } },
  { contract: { interface: { enabled: true, trigger_channels: ["mcp"], expected_inputs: [] } } },
])("refuses invalid or unavailable launch %j", async (options) => {
  render(<Harness options={options} />);
  fireEvent.click(screen.getByRole("checkbox"));
  expect(screen.getByRole("button", { name: "Run workflow" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Race submit" }));
  expect(fetchSpy).not.toHaveBeenCalled();
});

it.each([{ projectId: "other" }, { workflowId: "other" }, { revision: 8 }, { open: false }])("discards late responses and resets acknowledgement on owner change %j", async (options) => {
  let finish!: (value: Response) => void;
  fetchSpy.mockImplementation(() => new Promise<Response>((resolve) => { finish = resolve; }));
  const view = render(<Harness />);
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Race submit" }));
  await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
  view.rerender(<Harness options={options} />);
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  await act(async () => finish(response("stale-job")));
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
});

it("requires an actual file despite a saved sample and uploads its bytes", async () => {
  const fileContract = { interface: { enabled: true, trigger_channels: ["ui"], expected_inputs: [
    { name: "source", type: "file", required: true, sample_file: { filename: "sample.csv" } },
  ] } };
  render(<Harness options={{ contract: fileContract, inputs: {} }} />);
  fireEvent.click(screen.getByRole("checkbox"));
  expect(screen.getByRole("button", { name: "Run workflow" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Actual file"), { target: { files: [new File(["sku,A1"], "actual.csv", { type: "text/csv" })] } });
  fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
  await screen.findByRole("link");
  const body = JSON.parse(String(fetchSpy.mock.calls[0][1]?.body));
  expect(body.files).toEqual([{ input_name: "source", filename: "actual.csv", mime_type: "text/csv", content_base64: btoa("sku,A1") }]);
  expect(body.inputs).toEqual({});
  expect(JSON.stringify(body)).not.toContain("sample.csv");
});


it("refuses a normal start while the shared test launch slot is occupied", () => {
  render(<Harness options={{ launchLock: { current: Symbol("test-run") } }} />);
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Race submit" }));
  expect(fetchSpy).not.toHaveBeenCalled();
});
