/* eslint-disable testing-library/no-node-access, testing-library/no-container -- form fields are addressed by their data-field frame. */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import type { FormFieldView } from "@/components/workflow-inputs/form-field";
import type { RunUploadLimits } from "@/lib/auth/AuthProvider";
import { useHasCapability } from "@/lib/auth/capabilities";
import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { runBuilderWorkflow } from "@/pages/workflows/shared/run-api";

import { OperatorSectionPage } from "./section-page";
import type { OperatorSectionDetail } from "./types";

let uploadLimits: RunUploadLimits | undefined;

vi.mock("@/lib/auth/useAuth", () => ({ useAuth: () => ({ session: { user: { roles: ["operator"] } } }) }));
vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn() }));
vi.mock("@/lib/auth/run-upload-limits", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/auth/run-upload-limits")>()),
  useRunUploadLimits: () => uploadLimits,
}));
vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));
vi.mock("@/pages/workflows/shared/run-api", () => ({
  runBuilderWorkflow: vi.fn(),
  readWorkflowRunFile: (inputName: string, file: File) =>
    Promise.resolve({ input_name: inputName, filename: file.name, mime_type: file.type, content_base64: "eA==" }),
}));

const mockedFetchJson = vi.mocked(fetchJson);
const mockedRun = vi.mocked(runBuilderWorkflow);

const formFields: FormFieldView[] = [
  { name: "doc_type", kind: "select", widget: "button_group", label: "Document type", options: ["invoice", "contract"], required: true },
  { name: "pages", kind: "file_list", widget: "files", label: "Pages", required: true, max_items: 10 },
  { name: "urgent", kind: "boolean", widget: "true_false", label: "Urgent" },
  {
    name: "urgent_reason",
    kind: "string",
    widget: "textarea",
    label: "Why urgent",
    conditional_logic: [[{ field: "urgent", operator: "==", value: true }]],
  },
  { name: "ocr_model", kind: "string", widget: "text", label: "OCR model", hidden: true },
];

function section(mode: "bulk" | "foreach", overrides: Partial<OperatorSectionDetail> = {}): OperatorSectionDetail {
  return {
    project_id: "p1",
    project_name: "Acme",
    workflow_id: "wf.contracts",
    workflow_name: "Contracts",
    title: "Contracts",
    runnable: true,
    blocked_reasons: [],
    workflow_revision: 7,
    effects_acknowledged: true,
    operator: {
      workflow_id: "wf.contracts",
      workflow_name: "Contracts",
      workflow_revision: 7,
      title: "Contracts",
      published: true,
      runnable: true,
      effects_acknowledged: true,
      settings: {
        published: true,
        submission: mode === "foreach" ? { mode, target_input: "pages" } : { mode },
        list: {
          columns: [
            { id: "input.pages", source: "input", path: "pages", label: "Files" },
            { id: "output.supplier", source: "output", path: "supplier", label: "Supplier" },
            { id: "output.total", source: "output", path: "total", label: "Total", format: "currency" },
          ],
        },
      },
      form_fields: formFields,
      filters: [
        { id: "output.supplier", source: "output", path: "supplier", type: "search", label: "Supplier" },
        { id: "output.total", source: "output", path: "total", type: "number_range", label: "Total" },
      ],
      input_schema: { type: "object", properties: {} },
      output_schema: { type: "object", properties: {} },
      issues: [],
    },
    ...overrides,
  };
}

const JOB_ROW = {
  job_id: "j1",
  project_id: "p1",
  selected_workflow: "wf.contracts",
  status: "completed",
  started_at: "2026-11-19T10:00:00Z",
  operator_values: {
    "input.pages": [
      { filename: "page-1.jpg", artifact_id: "a1", size_bytes: 10 },
      { filename: "page-2.jpg", artifact_id: "a2", size_bytes: 10 },
      { filename: "page-3.jpg", artifact_id: "a3", size_bytes: 10 },
    ],
    "output.supplier": "ACME Ltd",
    "output.total": 318.5,
  },
};

function serve(detail: OperatorSectionDetail) {
  mockedFetchJson.mockImplementation(async (input) => {
    const url = String(input);
    if (url.startsWith("/api/v2/projects/p1/workflows/wf.contracts/section")) return detail as never;
    if (url.startsWith("/api/v2/jobs")) return { jobs: [JOB_ROW], page: 1, per_page: 25, total: 1, total_pages: 1 } as never;
    throw new Error(`unexpected ${url}`);
  });
}

function jobRequests(): URL[] {
  return mockedFetchJson.mock.calls
    .map(([input]) => new URL(String(input), "https://example.test"))
    .filter((url) => url.pathname === "/api/v2/jobs");
}

function renderPage(entry = "/projects/p1/workflows/wf.contracts/work") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route element={<OperatorSectionPage />} path="/projects/:projectId/workflows/:workflowId/work" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openForm() {
  fireEvent.click(await screen.findByRole("button", { name: "New" }));
  return screen.findByRole("dialog");
}

function choosePages(dialog: HTMLElement, names: string[]) {
  const picker = dialog.querySelector('[data-field="pages"] input[type="file"]') as HTMLInputElement;
  fireEvent.change(picker, { target: { files: names.map((name) => new File([name], name, { type: "image/jpeg" })) } });
}

beforeEach(() => {
  mockedFetchJson.mockReset();
  mockedRun.mockReset();
  uploadLimits = { max_files: 10, max_file_bytes: 5_000_000, max_total_bytes: 50_000_000 };
  vi.mocked(useHasCapability).mockReturnValue(true);
  globalThis.localStorage.clear();
});

describe("OperatorSectionPage", () => {
  it("shows the section's list with its project and workflow fixed and the configured columns", async () => {
    serve(section("bulk"));
    renderPage();

    expect(await screen.findByRole("heading", { name: "Contracts" })).toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(await screen.findByText("ACME Ltd")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Files" })).toBeInTheDocument();
    expect(screen.getByText("318.50")).toBeInTheDocument();
    // Files are names with download links; past two they fold into "+N".
    expect(screen.getByRole("link", { name: "page-1.jpg" })).toHaveAttribute("href", "/api/jobs/j1/artifacts/a1");
    expect(screen.getByRole("link", { name: "page-2.jpg" })).toBeInTheDocument();
    expect(screen.getByText("+1")).toBeInTheDocument();
    // "+N" opens every file as a download link, reachable by keyboard.
    const more = screen.getByRole("button", { name: "All files (3)" });
    more.focus();
    fireEvent.click(more);
    const list = await screen.findByRole("list", { name: "All files (3)" });
    expect(within(list).getByRole("link", { name: "page-3.jpg" })).toHaveAttribute("href", "/api/jobs/j1/artifacts/a3");
    expect(within(list).getAllByRole("link")).toHaveLength(3);
    const request = jobRequests()[0];
    expect(request.searchParams.get("project_id")).toBe("p1");
    expect(request.searchParams.get("workflow_id")).toBe("wf.contracts");
    expect(request.searchParams.get("operator_view")).toBe("1");
    expect(request.searchParams.has("filters")).toBe(false);
  });

  it("offers Awaiting review as a built-in status and keeps the rows the server matched", async () => {
    mockedFetchJson.mockImplementation(async (input) => {
      const url = String(input);
      if (url.startsWith("/api/v2/projects/p1/workflows/wf.contracts/section")) return section("bulk") as never;
      if (url.startsWith("/api/v2/jobs")) {
        return { jobs: [{ ...JOB_ROW, status: "awaiting_child" }], page: 1, per_page: 25, total: 1, total_pages: 1 } as never;
      }
      throw new Error(`unexpected ${url}`);
    });
    renderPage("/projects/p1/workflows/wf.contracts/work?status=awaiting_review");

    // A row waiting on its Invoke child stays: the server read the chain.
    expect(await screen.findByText("ACME Ltd")).toBeInTheDocument();
    expect(jobRequests().at(-1)?.searchParams.getAll("status")).toEqual(["awaiting_review"]);
    expect(jobRequests().at(-1)?.searchParams.has("filters")).toBe(false);
    // The panel shows it as a built-in status option.
    expect(await screen.findByText("Status · Awaiting review")).toBeInTheDocument();
  });

  it("filters by how runs started, from the address", async () => {
    serve(section("bulk"));
    renderPage("/projects/p1/workflows/wf.contracts/work?source=manual_ui");
    await screen.findByText("ACME Ltd");

    await waitFor(() => expect(jobRequests().at(-1)?.searchParams.get("task_source")).toBe("manual_ui"));
    expect(jobRequests().at(-1)?.searchParams.get("operator_view")).toBe("1");
  });

  it("sends a configured filter's value as the filters JSON", async () => {
    serve(section("bulk"));
    renderPage();
    await screen.findByText("ACME Ltd");

    fireEvent.click(screen.getByRole("button", { name: "Add filter" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Total" }));
    const menu = await screen.findByRole("menu", { name: "Choose a filter" });
    fireEvent.change(within(menu).getByLabelText("From"), { target: { value: "100" } });
    fireEvent.click(within(menu).getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(jobRequests().at(-1)?.searchParams.get("filters")).toBe(JSON.stringify({ "output.total": { min: 100 } })));
    expect(screen.getByText("Total · ≥ 100")).toBeInTheDocument();
  });

  it("lets a person hide a configured column in this browser", async () => {
    serve(section("bulk"));
    renderPage();
    await screen.findByText("ACME Ltd");
    fireEvent.click(screen.getByRole("button", { name: "Columns" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: "Supplier" }));
    await waitFor(() => expect(screen.queryByRole("columnheader", { name: "Supplier" })).not.toBeInTheDocument());
    expect(JSON.parse(globalThis.localStorage.getItem("fs.operator.hidden-columns:p1:wf.contracts") ?? "[]")).toEqual(["output.supplier"]);
  });

  it("sends Bulk as one run with every file under the same input name", async () => {
    serve(section("bulk"));
    mockedRun.mockResolvedValue({ job_id: "new-1" });
    renderPage();
    const dialog = await openForm();

    expect(dialog.querySelector('[data-field="ocr_model"]')).toBeNull();
    expect(dialog.querySelector('[data-field="urgent_reason"]')).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "contract" }));
    choosePages(dialog, ["p1.jpg", "p2.jpg", "p3.jpg", "p4.jpg"]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));

    await waitFor(() => expect(mockedRun).toHaveBeenCalledTimes(1));
    const [workflowId, body, key] = mockedRun.mock.calls[0];
    expect(workflowId).toBe("wf.contracts");
    expect(body).toMatchObject({ project_id: "p1", expected_revision: 7, acknowledged: true, inputs: { doc_type: "contract" } });
    expect(body.inputs).not.toHaveProperty("ocr_model");
    expect(body.inputs).not.toHaveProperty("urgent_reason");
    expect(body.files.map((file) => [file.input_name, file.filename])).toEqual([
      ["pages", "p1.jpg"],
      ["pages", "p2.jpg"],
      ["pages", "p3.jpg"],
      ["pages", "p4.jpg"],
    ]);
    expect(key).toMatch(/^operator:p1:wf\.contracts:/);
  });

  it("sends For each as one run per file and reports progress and failures", async () => {
    serve(section("foreach"));
    mockedRun
      .mockResolvedValueOnce({ job_id: "a" })
      .mockRejectedValueOnce(new Error("Queue is full"))
      .mockResolvedValueOnce({ job_id: "c" });
    renderPage();
    const dialog = await openForm();

    fireEvent.click(within(dialog).getByRole("button", { name: "invoice" }));
    fireEvent.click(within(dialog).getByRole("switch", { name: "Urgent" }));
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Why urgent" }), { target: { value: "Due today" } });
    choosePages(dialog, ["r1.jpg", "r2.jpg", "r3.jpg"]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));

    await waitFor(() => expect(mockedRun).toHaveBeenCalledTimes(3));
    const calls = mockedRun.mock.calls;
    expect(calls.map(([, body]) => body.files.map((file) => file.filename))).toEqual([["r1.jpg"], ["r2.jpg"], ["r3.jpg"]]);
    calls.forEach(([, body]) => expect(body.inputs).toEqual({ doc_type: "invoice", urgent: true, urgent_reason: "Due today" }));
    expect(new Set(calls.map(([, , key]) => key)).size).toBe(3);
    await screen.findByText("2 of 3 accepted");

    mockedRun.mockResolvedValueOnce({ job_id: "b" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Retry 1 failed" }));
    await waitFor(() => expect(mockedRun).toHaveBeenCalledTimes(4));
    // The retry resends the same request under the same key.
    expect(mockedRun.mock.calls[3][2]).toBe(calls[1][2]);
  });

  it("never re-sends accepted items after a partial success; only failed ones or a new submission", async () => {
    serve(section("foreach"));
    mockedRun
      .mockResolvedValueOnce({ job_id: "a" })
      .mockRejectedValueOnce(new Error("Queue is full"))
      .mockRejectedValueOnce(new Error("Queue is full"));
    renderPage();
    const dialog = await openForm();
    fireEvent.click(within(dialog).getByRole("button", { name: "invoice" }));
    choosePages(dialog, ["r1.jpg", "r2.jpg", "r3.jpg"]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));
    await screen.findByText("1 of 3 accepted");
    const firstKeys = mockedRun.mock.calls.map(([, , key]) => key);

    // Send is gone and submitting the form does nothing: the accepted item stays sent once.
    expect(within(dialog).queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    fireEvent.submit(dialog.querySelector("form") as HTMLFormElement);
    expect(mockedRun).toHaveBeenCalledTimes(3);

    mockedRun.mockResolvedValueOnce({ job_id: "b" }).mockRejectedValueOnce(new Error("Queue is full"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Retry 2 failed" }));
    await screen.findByText("2 of 3 accepted");
    const retryKeys = mockedRun.mock.calls.slice(3).map(([, , key]) => key);
    expect(retryKeys).toEqual([firstKeys[1], firstKeys[2]]);
    expect(retryKeys).not.toContain(firstKeys[0]);

    // A new submission resets the form and starts a new id.
    fireEvent.click(within(dialog).getByRole("button", { name: "New submission" }));
    expect(await within(dialog).findByRole("button", { name: "Send" })).toBeInTheDocument();
    expect(within(dialog).queryByText(/accepted/)).not.toBeInTheDocument();
    mockedRun.mockResolvedValueOnce({ job_id: "d" });
    fireEvent.click(within(dialog).getByRole("button", { name: "invoice" }));
    choosePages(dialog, ["r9.jpg"]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(mockedRun).toHaveBeenCalledTimes(6));
    const newKey = mockedRun.mock.calls[5][2];
    expect([...firstKeys, ...retryKeys]).not.toContain(newKey);
    expect(mockedRun.mock.calls[5][1].files.map((file) => file.filename)).toEqual(["r9.jpg"]);
  });

  it("refuses a For each form whose target is not shown, without a request", async () => {
    const hiddenTarget = section("foreach");
    hiddenTarget.operator.form_fields = formFields.map((entry) => (entry.name === "pages" ? { ...entry, hidden: true } : entry));
    serve(hiddenTarget);
    renderPage();
    const dialog = await openForm();
    fireEvent.click(within(dialog).getByRole("button", { name: "invoice" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));

    expect(await within(dialog).findByText(/the input it sends one by one is not shown/)).toBeInTheDocument();
    expect(mockedRun).not.toHaveBeenCalled();
  });

  it("refuses files beyond the installation's limits before sending", async () => {
    uploadLimits = { max_files: 2, max_file_bytes: 5_000_000, max_total_bytes: 10_000_000 };
    serve(section("bulk"));
    renderPage();
    const dialog = await openForm();
    fireEvent.click(within(dialog).getByRole("button", { name: "invoice" }));
    choosePages(dialog, ["p1.jpg", "p2.jpg", "p3.jpg"]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));

    expect(await within(dialog).findByText(/more or larger than this installation accepts/)).toBeInTheDocument();
    expect(mockedRun).not.toHaveBeenCalled();
  });

  it("asks for required fields and shows the server's field error next to the field", async () => {
    serve(section("bulk"));
    mockedRun.mockRejectedValueOnce(
      new HttpError({ status: 400, url: "/api/workflows/wf.contracts/run", payload: { detail: "Workflow input validation failed: Workflow input 'doc_type' has invalid type" } }),
    );
    renderPage();
    const dialog = await openForm();
    fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));
    expect(await within(dialog).findByText("Fill in Document type.")).toBeInTheDocument();
    expect(mockedRun).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "invoice" }));
    choosePages(dialog, ["p1.jpg"]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));
    const frame = dialog.querySelector('[data-field="doc_type"]') as HTMLElement;
    expect(await within(frame).findByText(/Workflow input 'doc_type' has invalid type/)).toBeInTheDocument();
  });

  it("shows a viewer the list without the New button", async () => {
    vi.mocked(useHasCapability).mockReturnValue(false);
    serve(section("bulk"));
    renderPage();
    await screen.findByText("ACME Ltd");
    expect(screen.queryByRole("button", { name: "New" })).not.toBeInTheDocument();
  });

  it("keeps the list and explains why new submissions are paused", async () => {
    serve(
      section("bulk", {
        runnable: false,
        blocked_reasons: [{ code: "operator.effects_not_acknowledged", message: "Real effects are not acknowledged" }],
      }),
    );
    renderPage();
    expect(await screen.findByText("Real effects are not acknowledged")).toBeInTheDocument();
    expect(await screen.findByText("ACME Ltd")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New" })).not.toBeInTheDocument();
  });
});
