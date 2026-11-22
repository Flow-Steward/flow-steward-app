/* eslint-disable testing-library/no-node-access, testing-library/no-container -- review fields are addressed by their data-field frame. */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import { useHasCapability } from "@/lib/auth/capabilities";
import { fetchArrayBuffer, fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { runBuilderWorkflow } from "@/pages/workflows/shared/run-api";

import { OperatorJobPage } from "./job-page";
import type { JobReview, ReviewField } from "./review-model";
import type { OperatorSectionDetail } from "./types";

// A document original stays loading here; its previewer is covered by file-preview.test.tsx.
vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn(), fetchText: vi.fn(), fetchArrayBuffer: vi.fn(() => new Promise<ArrayBuffer>(() => undefined)) }));
vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn(() => false) }));
vi.mock("@/pages/workflows/shared/run-api", () => ({
  runBuilderWorkflow: vi.fn(),
  readWorkflowRunFile: (inputName: string, file: File) =>
    Promise.resolve({ input_name: inputName, filename: file.name, mime_type: file.type, content_base64: "eA==" }),
}));
vi.mock("@/pages/jobs/detail", () => ({
  JobsDetailPage: ({ jobId }: { jobId: string }) => <div data-testid="job-detail">{jobId}</div>,
}));

const mockedFetchJson = vi.mocked(fetchJson);

const FIELDS: ReviewField[] = [
  { name: "supplier", label: "Supplier", type: "string", required: true, editable: true },
  { name: "total", label: "Total", type: "number", required: true, editable: true },
  { name: "document_date", label: "Document date", type: "string", format: "date", editable: true },
  { name: "notes", label: "Notes", type: "paragraph", editable: true },
  { name: "paid", label: "Paid", type: "boolean", editable: true },
  { name: "currency", label: "Currency", type: "select", options: ["EUR", "USD"], editable: true },
  {
    name: "lines",
    label: "Lines",
    type: "array",
    editable: true,
    items: { type: "object", properties: [{ name: "item", type: "string" }, { name: "qty", type: "number" }] },
  },
  { name: "confidence", label: "Confidence", type: "number", editable: false },
];

function review(jobId: string, access: JobReview["access"] = "decide", overrides: Partial<JobReview> = {}): JobReview {
  return {
    job_id: jobId,
    status: "awaiting_child",
    waiting: true,
    review_job_id: `${jobId}-child`,
    access,
    gate: {
      job_id: `${jobId}-child`,
      thread_id: `thread-${jobId}`,
      approval_id: `approval-${jobId}`,
      allowed_actions: ["approve", "reject"],
      payload: { message: "Compare with the invoice", approve_label: "Export", reject_label: "Recognize again" },
      review: {
        fields: FIELDS,
        values: {
          supplier: "ACME",
          total: 31.85,
          document_date: "2026-11-03",
          notes: "",
          paid: false,
          currency: "EUR",
          lines: [{ item: "Paper", qty: 2 }],
          confidence: 0.62,
        },
        invalid: {},
        original: [
          { artifact_id: "art-1", filename: "page-1.png", content_type: "image/png", size_bytes: 100 },
          { artifact_id: "art-2", filename: "page-2.pdf", content_type: "application/pdf", size_bytes: 200 },
        ],
      },
    },
    ...overrides,
  };
}

const SECTION = {
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
    settings: null,
    form_fields: [],
    filters: [{ id: "state", source: "job", path: "status", type: "choice_multi", options: ["awaiting_review", "completed"] }],
    input_schema: {},
    output_schema: {},
    issues: [],
  },
} as unknown as OperatorSectionDetail;

let reviews: Record<string, JobReview>;
let decide: (body: Record<string, unknown>) => unknown;

function serve() {
  mockedFetchJson.mockImplementation(async (input, init) => {
    const url = new URL(String(input), "https://example.test");
    if (url.pathname === "/api/v2/projects/p1/workflows/wf.contracts/section") return SECTION as never;
    const reviewMatch = /^\/api\/v2\/jobs\/([^/]+)\/review$/.exec(url.pathname);
    if (reviewMatch) return reviews[reviewMatch[1]] as never;
    if (url.pathname === "/api/v2/jobs" && url.searchParams.get("around")) {
      const at = Number(url.searchParams.get("around")?.replace("j", ""));
      return {
        job_id: `j${at}`,
        in_list: true,
        position: at,
        total: 47,
        previous_job_id: at > 1 ? `j${at - 1}` : null,
        next_job_id: `j${at + 1}`,
      } as never;
    }
    if (url.pathname.startsWith("/api/approvals/")) return decide(JSON.parse(String(init?.body ?? "{}"))) as never;
    throw new Error(`unexpected ${url.pathname}`);
  });
}

function Where() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

const LIST = "f.state=awaiting_review&search=acme&source=manual_ui&page=2";

function renderPage(jobId = "j3", query = LIST) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/projects/p1/workflows/wf.contracts/work/jobs/${jobId}?${query}`]}>
        <Routes>
          <Route
            element={
              <>
                <OperatorJobPage />
                <Where />
              </>
            }
            path="/projects/:projectId/workflows/:workflowId/work/jobs/:jobId"
          />
          <Route element={<Where />} path="/projects/:projectId/workflows/:workflowId/work" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function frame(container: HTMLElement, name: string): HTMLElement {
  const element = container.querySelector(`[data-field="${name}"]`);
  if (!element) throw new Error(`no field ${name}`);
  return element as HTMLElement;
}

function decisions(): Array<{ url: string; body: Record<string, unknown> }> {
  return mockedFetchJson.mock.calls
    .filter(([input]) => String(input).startsWith("/api/approvals/"))
    .map(([input, init]) => ({ url: String(input), body: JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown> }));
}

beforeEach(() => {
  mockedFetchJson.mockReset();
  vi.mocked(useHasCapability).mockReturnValue(false);
  vi.mocked(fetchArrayBuffer).mockImplementation(() => new Promise<ArrayBuffer>(() => undefined));
  vi.mocked(runBuilderWorkflow).mockReset();
  reviews = Object.fromEntries(["j1", "j2", "j3", "j4", "j5", "j6"].map((id) => [id, review(id)]));
  decide = () => ({ status: "in_progress", actor: "olga" });
  serve();
});

describe("OperatorJobPage review", () => {
  it("shows the original beside the fields, each with the default widget of its type", async () => {
    const { container } = renderPage();
    await screen.findByTestId("operator-review");

    const input = (name: string, selector = "input") => frame(container, name).querySelector(selector) as HTMLInputElement;
    expect(input("supplier").type).toBe("text");
    expect(input("total").inputMode).toBe("decimal");
    expect(input("total").value).toBe("31.85");
    expect(input("document_date").type).toBe("date");
    expect(input("notes", "textarea")).toBeInTheDocument();
    expect(within(frame(container, "paid")).getByRole("switch")).toBeInTheDocument();
    // A few choices are radio buttons, all seen at once.
    const currency = within(frame(container, "currency")).getByRole("radiogroup");
    expect(within(currency).getAllByRole("radio").map((radio) => (radio as HTMLInputElement).value)).toEqual(["EUR", "USD"]);
    expect(within(frame(container, "lines")).getByRole("table")).toBeInTheDocument();
    // Not editable: shown, never changed.
    expect(input("confidence")).toBeDisabled();
    expect(input("supplier")).toBeEnabled();
    // The original: one file at a time, several paged; the step's own labels on the buttons.
    const original = screen.getByTestId("review-original");
    expect(within(original).getByText("File 1 of 2")).toBeInTheDocument();
    expect(within(original).getByRole("img", { name: "page-1.png" })).toHaveAttribute("src", "/api/jobs/j3-child/artifacts/art-1/preview");
    fireEvent.click(within(original).getByRole("button", { name: "Next file" }));
    // A PDF is drawn by the page itself, never in the browser's PDF viewer.
    expect(within(original).getByTestId("file-preview")).toHaveAttribute("data-kind", "pdf");
    expect(container.querySelector("iframe")).toBeNull();
    expect(screen.getByRole("button", { name: "Export" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recognize again" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("operator-queue-position")).toHaveTextContent("3 of 47"));
  });

  it("lays the review out as the original beside the panel: heading with the steps, cards, the decision in the footer", async () => {
    renderPage();
    const page = await screen.findByTestId("operator-review");

    const [original, check] = Array.from(page.children);
    expect(original).toHaveClass("fs-operator-review-original");
    expect(within(original as HTMLElement).getByTestId("review-original")).toHaveAttribute("data-layout", "pane");
    expect(check).toHaveClass("fs-operator-review-check");
    const head = (check as HTMLElement).querySelector(".fs-operator-review-head") as HTMLElement;
    expect(within(head).getByRole("heading", { level: 1 })).toHaveTextContent("Review");
    expect(within(head).getByTestId("operator-queue-steps")).toBeInTheDocument();
    // The old bar above the page is gone: the way back sits beside the decision.
    expect(screen.queryByTestId("operator-queue")).not.toBeInTheDocument();
    const foot = (check as HTMLElement).querySelector(".fs-operator-review-foot") as HTMLElement;
    expect(within(foot).getByRole("link", { name: "Back to Contracts" })).toHaveAttribute("href", `/projects/p1/workflows/wf.contracts/work?${LIST}`);
    // The primary decision is the last button, on the right.
    const buttons = within(foot).getAllByRole("button");
    expect(buttons.at(-1)).toHaveAttribute("data-testid", "review-approve-next");
    // The fields and the comment are cards of their own.
    const cards = Array.from((check as HTMLElement).querySelectorAll(".fs-card-title")).map((title) => title.textContent);
    expect(cards).toEqual(["Check and correct", "Comment"]);
  });

  it("marks a corrected field with its initial value and sends the number typed in the locale", async () => {
    const { container } = renderPage();
    await screen.findByTestId("operator-review");
    const total = frame(container, "total").querySelector("input") as HTMLInputElement;

    fireEvent.change(total, { target: { value: "318,50" } });

    expect(total.value).toBe("318,50");
    expect(within(frame(container, "total")).getByTestId("review-corrected-total")).toHaveTextContent("Corrected Was: 31.85");
    expect(within(frame(container, "supplier")).queryByText("Corrected")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    await waitFor(() => expect(decisions()).toHaveLength(1));
    expect(decisions()[0]).toEqual({
      url: "/api/approvals/thread-j3",
      body: { approval_id: "approval-j3", approved: true, comment: "", values: { total: 318.5 } },
    });
    expect(await screen.findByText("Approved with these values")).toBeInTheDocument();
    expect(screen.getByText(/Run status: in_progress · Decided by olga/)).toBeInTheDocument();
  });

  it("shows a refused value at its field and keeps the operator on the same job", async () => {
    decide = () => {
      throw new HttpError({
        status: 422,
        url: "/api/approvals/thread-j3",
        payload: { code: "approval_review_field_invalid", field: "total", message: "Total must be a number" },
      });
    };
    const { container } = renderPage();
    await screen.findByTestId("operator-review");
    const totalInput = () => frame(container, "total").querySelector("input") as HTMLInputElement;
    // Text that is not a number is named at its field and nothing is sent.
    fireEvent.change(totalInput(), { target: { value: "318,5О" } });
    fireEvent.click(screen.getByTestId("review-approve-next"));
    expect(await within(frame(container, "total")).findByText("Enter a number.")).toBeInTheDocument();
    expect(decisions()).toHaveLength(0);

    // A number the server still refuses is shown at its field from the server's answer.
    fireEvent.change(totalInput(), { target: { value: "318,50" } });
    fireEvent.click(screen.getByTestId("review-approve-next"));

    expect(await within(frame(container, "total")).findByText("Total must be a number")).toBeInTheDocument();
    expect(decisions()[0].body.values).toEqual({ total: 318.5 });
    expect(screen.getByTestId("location")).toHaveTextContent("/work/jobs/j3?");
  });

  it("approves and opens the next job at once, keeping the list in the address", async () => {
    renderPage();
    await screen.findByTestId("operator-review");

    fireEvent.click(screen.getByTestId("review-approve-next"));

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(`/projects/p1/workflows/wf.contracts/work/jobs/j4?${LIST}`));
    expect(decisions()).toHaveLength(1);
    expect(decisions()[0].body).toEqual({ approval_id: "approval-j3", approved: true, comment: "" });
  });

  it("approves and moves on with Ctrl+Enter, even from a field", async () => {
    const { container } = renderPage();
    await screen.findByTestId("operator-review");

    fireEvent.keyDown(frame(container, "supplier").querySelector("input") as HTMLInputElement, { key: "Enter", ctrlKey: true });

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/work/jobs/j4?"));
    expect(decisions()).toHaveLength(1);
  });

  it("steps with J and K, but not while typing in a field", async () => {
    const { container } = renderPage();
    await screen.findByTestId("operator-review");
    await waitFor(() => expect(screen.getByTestId("operator-queue-position")).toHaveTextContent("3 of 47"));

    fireEvent.keyDown(frame(container, "supplier").querySelector("input") as HTMLInputElement, { key: "j" });
    fireEvent.keyDown(frame(container, "notes").querySelector("textarea") as HTMLTextAreaElement, { key: "k" });
    expect(screen.getByTestId("location")).toHaveTextContent("/work/jobs/j3?");

    fireEvent.keyDown(document.body, { key: "k" });
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/work/jobs/j2?"));
    expect(decisions()).toHaveLength(0);
  });

  it("asks for a comment before a rejection", async () => {
    renderPage();
    await screen.findByTestId("operator-review");

    fireEvent.click(screen.getByRole("button", { name: "Recognize again" }));
    expect(await screen.findByText("A rejection comment is required for this review gate.")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("review-comment"), { target: { value: "Wrong supplier" } });
    fireEvent.click(screen.getByRole("button", { name: "Recognize again" }));

    await waitFor(() => expect(decisions()).toHaveLength(1));
    expect(decisions()[0].body).toEqual({ approval_id: "approval-j3", approved: false, comment: "Wrong supplier" });
  });

  it("shows a reader the fields and the original without inputs or buttons", async () => {
    reviews.j3 = review("j3", "read");
    const { container } = renderPage();
    await screen.findByTestId("operator-review");

    expect(frame(container, "supplier").querySelector("input")).toBeDisabled();
    expect(frame(container, "total").querySelector("input")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Export" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("review-approve-next")).not.toBeInTheDocument();
    expect(screen.queryByTestId("review-comment")).not.toBeInTheDocument();
    expect(screen.getByTestId("review-original")).toBeInTheDocument();
  });

  it("tells someone without access to the deep review only that it waits", async () => {
    reviews.j3 = review("j3", "none", { review_job_id: null, gate: null });
    renderPage();

    expect(await screen.findByText("This job waits for a decision by people who have access to it.")).toBeInTheDocument();
    expect(screen.queryByTestId("operator-review")).not.toBeInTheDocument();
    expect(screen.getByTestId("operator-job")).toBeInTheDocument();
    expect(screen.queryByTestId("job-detail")).not.toBeInTheDocument();
  });

  it("leads back to the same list and shows a job without review fields in the operator view", async () => {
    reviews.j3 = review("j3", "decide", { status: "completed", waiting: false, review_job_id: null, access: null, gate: null });
    renderPage();

    expect(await screen.findByTestId("operator-job")).toBeInTheDocument();
    expect(screen.queryByTestId("job-detail")).not.toBeInTheDocument();
    const back = screen.getByRole("link", { name: "Back to Contracts" });
    expect(back).toHaveAttribute("href", `/projects/p1/workflows/wf.contracts/work?${LIST}`);
    await waitFor(() => expect(screen.getByTestId("operator-queue-position")).toHaveTextContent("3 of 47"));
    const around = mockedFetchJson.mock.calls
      .map(([input]) => new URL(String(input), "https://example.test"))
      .find((url) => url.searchParams.get("around") === "j3");
    expect(around?.searchParams.get("filters")).toBe(JSON.stringify({ state: ["awaiting_review"] }));
    expect(around?.searchParams.get("task_source")).toBe("manual_ui");
    expect(around?.searchParams.get("search")).toBe("acme");
    expect(around?.searchParams.has("page")).toBe(false);
  });

  it("loads the next job's review ahead", async () => {
    renderPage();
    await screen.findByTestId("operator-review");

    await waitFor(() =>
      expect(mockedFetchJson.mock.calls.some(([input]) => String(input) === "/api/v2/jobs/j4/review?project_id=p1&workflow_id=wf.contracts")).toBe(true),
    );
    // Every review read names the section, so the server refuses a job of another one.
    const reads = mockedFetchJson.mock.calls.map(([input]) => new URL(String(input), "https://example.test")).filter((url) => url.pathname.endsWith("/review"));
    expect(reads.every((url) => url.searchParams.get("project_id") === "p1" && url.searchParams.get("workflow_id") === "wf.contracts")).toBe(true);
    await waitFor(() => expect(document.head.querySelector('link[rel="prefetch"][href="/api/jobs/j4-child/artifacts/art-1/preview"]')).not.toBeNull());
  });
});

describe("OperatorJobPage finished job", () => {
  const withForm = {
    ...SECTION,
    operator: {
      ...SECTION.operator,
      settings: {
        published: true,
        result: { order: ["total", "supplier"], fields: { total: { label: "Amount", decimals: 2 }, internal: { hidden: true } } },
      },
      form_fields: [
        { name: "doc_type", kind: "select", widget: "select", label: "Document type", options: ["invoice"], option_labels: { invoice: "Invoice" } },
        { name: "pages", kind: "file_list", widget: "files", label: "Pages" },
        { name: "ocr_model", kind: "string", widget: "text", label: "OCR model", hidden: true },
      ],
      output_schema: { properties: { supplier: { title: "Supplier" }, total: { type: "number" }, internal: { type: "string" } } },
    },
  } as unknown as OperatorSectionDetail;

  function finished(overrides: Partial<JobReview> = {}): JobReview {
    return {
      job_id: "j3",
      status: "completed",
      waiting: false,
      review_job_id: null,
      access: null,
      gate: null,
      job_values: {
        status: "completed",
        submitted_by: { id: "u1", name: "Olga" },
        created_at: "2026-11-19T10:42:00Z",
        duration_ms: 3100,
        inputs: { doc_type: "invoice", pages: [{ filename: "p1.jpg", artifact_id: "a1" }], ocr_model: "fast" },
        outputs: { total: 318.5, supplier: "ACME", internal: "x" },
      },
      decision: {
        access: "read",
        job_id: "j3-child",
        approved: true,
        actor: "Marta",
        timestamp: "2026-11-19T10:00:00Z",
        fields: [
          { name: "supplier", label: "Supplier", type: "string" },
          { name: "total", label: "Total", type: "number" },
        ],
        values: { supplier: "ACME Ltd", total: 318.5 },
        changes: [{ field: "total", before: 31.85, after: 318.5 }],
      },
      ...overrides,
    };
  }

  function serveFinished(detail: JobReview) {
    mockedFetchJson.mockImplementation(async (input) => {
      const url = new URL(String(input), "https://example.test");
      if (url.pathname === "/api/v2/projects/p1/workflows/wf.contracts/section") return withForm as never;
      if (url.pathname === "/api/v2/jobs/j3/review") return detail as never;
      if (url.pathname === "/api/v2/jobs") return { job_id: "j3", in_list: true, position: 3, total: 47, previous_job_id: null, next_job_id: null } as never;
      throw new Error(`unexpected ${url.pathname}`);
    });
  }

  it("shows what was submitted, the configured result and the decided review of the Invoke child, never the step graph", async () => {
    serveFinished(finished());
    renderPage();

    const submitted = await screen.findByTestId("operator-job-inputs");
    expect(within(submitted).getByText("Document type")).toBeInTheDocument();
    expect(within(submitted).getByText("Invoice")).toBeInTheDocument();
    expect(within(submitted).queryByText("OCR model")).not.toBeInTheDocument();
    // The uploaded pages are shown, not listed as links: the previewer above the values.
    expect(within(submitted).queryByText("Pages")).not.toBeInTheDocument();
    // The same layout as the review: the uploaded pages on the left, the panel on the right.
    const page = screen.getByTestId("operator-job");
    expect(page).toHaveClass("fs-operator-review");
    expect(page).toHaveAttribute("data-original", "shown");
    const [left, right] = Array.from(page.children) as HTMLElement[];
    expect(left).toHaveClass("fs-operator-review-original");
    const original = within(left).getByTestId("review-original");
    expect(original).toHaveAttribute("data-layout", "pane");
    expect(within(original).getByText("p1.jpg")).toBeInTheDocument();
    expect(within(original).getByRole("link", { name: /download/i })).toHaveAttribute("href", "/api/jobs/j3/artifacts/a1");
    expect(right).toHaveClass("fs-operator-review-check");
    // The cards in the panel: the result, the decided review, then the other submitted values.
    const cards = Array.from(right.querySelectorAll(".fs-card")).map((card) => card.getAttribute("data-testid"));
    expect(cards).toEqual(["operator-job-result", "operator-job-review", "operator-job-submitted"]);
    // The heading: the job named by its file, its status, when, who and how long, and the steps.
    const head = screen.getByTestId("operator-job-head");
    expect(within(head).getByRole("heading", { level: 1 })).toHaveTextContent("p1.jpg");
    expect(within(head).getByTestId("operator-job-meta")).toHaveTextContent(/Olga · 3\.1s$/);
    expect(within(head).getByTestId("operator-job-status")).toHaveTextContent("Completed");
    expect(within(head).getByTestId("operator-queue-steps")).toBeInTheDocument();
    expect(screen.queryByTestId("operator-queue")).not.toBeInTheDocument();
    // The way back sits in the footer, as on the review.
    const foot = right.querySelector(".fs-operator-review-foot") as HTMLElement;
    expect(within(foot).getByRole("link", { name: "Back to Contracts" })).toHaveAttribute("href", `/projects/p1/workflows/wf.contracts/work?${LIST}`);
    // Anyone who may not run it gets no Send again.
    expect(screen.queryByTestId("operator-job-send-again")).not.toBeInTheDocument();

    const result = screen.getByTestId("operator-job-outputs");
    const labels = within(result).getAllByRole("term").map((term) => term.textContent);
    expect(labels).toEqual(["Amount", "Supplier"]);
    expect(within(result).getByText(/318[.,]50/)).toBeInTheDocument();

    const decided = screen.getByTestId("operator-job-review");
    expect(within(decided).getByText(/Approved by Marta/)).toBeInTheDocument();
    expect(within(decided).getByText("ACME Ltd")).toBeInTheDocument();
    expect(within(decided).getByTestId("decision-corrected-total")).toHaveTextContent("Was: 31.85");
    expect(within(screen.getByTestId("operator-job-review-changes")).getByText(/Total/)).toBeInTheDocument();

    expect(screen.queryByTestId("job-detail")).not.toBeInTheDocument();
    expect(screen.queryByTestId("operator-job-details-link")).not.toBeInTheDocument();
  });

  it("renders a formatted result through the Markdown renderer and a list of objects as a table", async () => {
    const formatted = {
      ...withForm,
      operator: {
        ...withForm.operator,
        settings: { published: true, result: { order: ["notes", "lines"], fields: { notes: { label: "Notes", display: "wysiwyg" } } } },
        output_schema: {
          properties: {
            notes: { type: "string" },
            lines: {
              type: "array",
              title: "Lines",
              items: { type: "object", properties: { item: { type: "string", title: "Item" }, qty: { type: "number", title: "Qty" }, price: { type: "number", title: "Price" } } },
            },
          },
        },
      },
    } as unknown as OperatorSectionDetail;
    const detail = finished({
      job_values: {
        status: "completed",
        inputs: {},
        outputs: {
          notes: "## Checked\n\n- totals match\n- VAT ok\n\n<script>window.hacked = true</script><img src=x onerror=\"window.hacked = true\">",
          lines: [
            { item: "Paper", qty: 2, price: 3.5 },
            { item: "Ink", qty: 1, price: 20 },
          ],
        },
      },
      decision: {
        access: "read",
        job_id: "j3-child",
        approved: true,
        actor: "Marta",
        timestamp: "2026-11-19T10:00:00Z",
        fields: [{ name: "lines", label: "Lines", type: "array", items: { type: "object", properties: [{ name: "item", type: "string" }, { name: "qty", type: "number" }] } }],
        values: { lines: [{ item: "Paper", qty: 3 }] },
        changes: [],
      },
    });
    mockedFetchJson.mockImplementation(async (input) => {
      const url = new URL(String(input), "https://example.test");
      if (url.pathname === "/api/v2/projects/p1/workflows/wf.contracts/section") return formatted as never;
      if (url.pathname === "/api/v2/jobs/j3/review") return detail as never;
      if (url.pathname === "/api/v2/jobs") return { job_id: "j3", in_list: true, position: 3, total: 47, previous_job_id: null, next_job_id: null } as never;
      throw new Error(`unexpected ${url.pathname}`);
    });
    const { container } = renderPage();

    const result = await screen.findByTestId("operator-job-outputs");
    const text = within(result).getByTestId("operator-formatted-text");
    expect(within(text).getByRole("heading", { name: "Checked" })).toBeInTheDocument();
    expect(within(text).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["totals match", "VAT ok"]);
    // Raw HTML stays text: no element is created from it.
    expect(container.querySelector("script, img")).toBeNull();
    expect((window as unknown as { hacked?: boolean }).hacked).toBeUndefined();

    const lines = within(result).getByTestId("operator-structured-lines");
    const table = within(lines).getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent).filter(Boolean)).toEqual(["Item", "Qty", "Price"]);
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByDisplayValue("Paper")).toBeDisabled();
    // The table scrolls inside its own frame, so a narrow screen never scrolls the page.
    expect(table.parentElement).toHaveClass("fs-workflow-repeater-scroll");
    // Read-only: no row actions.
    expect(within(lines).queryByRole("button", { name: /add row/i })).toBeNull();
    expect(within(lines).queryByRole("button", { name: /remove row/i })).toBeNull();

    const decided = within(screen.getByTestId("operator-job-review")).getByTestId("operator-structured-lines");
    expect(within(decided).getByRole("table")).toBeInTheDocument();
    expect(within(decided).getByDisplayValue("3")).toBeInTheDocument();
  });

  it("previews a submitted PDF on the left and offers a result file with its preview and download", async () => {
    const withReport = {
      ...withForm,
      operator: {
        ...withForm.operator,
        settings: { published: true, result: { order: ["report"], fields: {} } },
        output_schema: { properties: { report: { title: "Report" } } },
      },
    } as unknown as OperatorSectionDetail;
    const detail = finished({
      job_values: {
        status: "completed",
        inputs: { pages: [{ filename: "contract.pdf", content_type: "application/pdf", size_bytes: 2048, artifact_id: "a1" }] },
        outputs: { report: { filename: "report.pdf", content_type: "application/pdf", size_bytes: 4096, artifact_id: "r1" } },
      },
      decision: null,
    });
    mockedFetchJson.mockImplementation(async (input) => {
      const url = new URL(String(input), "https://example.test");
      if (url.pathname === "/api/v2/projects/p1/workflows/wf.contracts/section") return withReport as never;
      if (url.pathname === "/api/v2/jobs/j3/review") return detail as never;
      if (url.pathname === "/api/v2/jobs") return { job_id: "j3", in_list: true, position: 3, total: 47, previous_job_id: null, next_job_id: null } as never;
      throw new Error(`unexpected ${url.pathname}`);
    });
    renderPage();

    const original = await screen.findByTestId("operator-page-original");
    expect(await within(original).findByTestId("file-preview")).toHaveAttribute("data-kind", "pdf");
    // Files only: no Submitted card saying nothing was sent.
    expect(screen.queryByTestId("operator-job-submitted")).not.toBeInTheDocument();

    const result = screen.getByTestId("operator-job-outputs");
    expect(within(result).getByText("report.pdf")).toBeInTheDocument();
    expect(within(result).getByRole("button", { name: "View" })).toBeInTheDocument();
    expect(within(result).getByRole("link", { name: /download/i })).toHaveAttribute("href", "/api/jobs/j3/artifacts/r1");
  });

  it("sends a job again with its values and files, then opens the new job", async () => {
    vi.mocked(useHasCapability).mockImplementation((capability) => capability === "run_workflows");
    vi.mocked(fetchArrayBuffer).mockImplementation(async (input) =>
      String(input) === "/api/jobs/j3/artifacts/a1" ? new ArrayBuffer(8) : new Promise<ArrayBuffer>(() => undefined),
    );
    vi.mocked(runBuilderWorkflow).mockResolvedValue({ job_id: "j9" });
    serveFinished(
      finished({
        job_values: {
          status: "failed",
          inputs: { doc_type: "invoice", pages: [{ filename: "contract.pdf", content_type: "application/pdf", artifact_id: "a1" }], ocr_model: "fast" },
          outputs: null,
        },
        decision: null,
      }),
    );
    renderPage();

    fireEvent.click(await screen.findByTestId("operator-job-send-again"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Send again" })).toBeInTheDocument();
    // The file was taken from the job: nothing to choose again.
    expect(await within(dialog).findByText("contract.pdf")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));

    await waitFor(() => expect(runBuilderWorkflow).toHaveBeenCalledTimes(1));
    const [workflowId, request] = vi.mocked(runBuilderWorkflow).mock.calls[0];
    expect(workflowId).toBe("wf.contracts");
    expect(request.inputs).toEqual({ doc_type: "invoice" });
    expect(request.files).toEqual([{ input_name: "pages", filename: "contract.pdf", mime_type: "application/pdf", content_base64: "eA==" }]);
    // The new run opens with the same list around it.
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(`/projects/p1/workflows/wf.contracts/work/jobs/j9?${LIST}`));
  });

  it("forgets a Send again started on a job the operator has left", async () => {
    vi.mocked(useHasCapability).mockImplementation((capability) => capability === "run_workflows");
    let release: (body: ArrayBuffer) => void = () => undefined;
    let signal: AbortSignal | undefined;
    vi.mocked(fetchArrayBuffer).mockImplementation((input, init) => {
      if (String(input) !== "/api/jobs/j3/artifacts/a1") return new Promise<ArrayBuffer>(() => undefined);
      signal = init?.signal ?? undefined;
      return new Promise<ArrayBuffer>((resolve) => {
        release = resolve;
      });
    });
    const job = (id: string) =>
      finished({
        job_id: id,
        job_values: { status: "failed", inputs: { doc_type: "invoice", pages: [{ filename: `${id}.pdf`, content_type: "application/pdf", artifact_id: "a1" }] }, outputs: null },
        decision: null,
      });
    mockedFetchJson.mockImplementation(async (input) => {
      const url = new URL(String(input), "https://example.test");
      if (url.pathname === "/api/v2/projects/p1/workflows/wf.contracts/section") return withForm as never;
      const match = /^\/api\/v2\/jobs\/([^/]+)\/review$/.exec(url.pathname);
      if (match) return job(match[1]) as never;
      if (url.pathname === "/api/v2/jobs") {
        const first = url.searchParams.get("around") === "j3";
        return { job_id: first ? "j3" : "j4", in_list: true, position: first ? 3 : 4, total: 47, previous_job_id: first ? null : "j3", next_job_id: first ? "j4" : null } as never;
      }
      throw new Error(`unexpected ${url.pathname}`);
    });
    renderPage();

    fireEvent.click(await screen.findByTestId("operator-job-send-again"));
    await waitFor(() => expect(signal).toBeDefined());
    const next = screen.getByRole("button", { name: "Next job (J)" });
    await waitFor(() => expect(next).toBeEnabled());
    fireEvent.click(next);
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/work/jobs/j4?"));
    expect(await screen.findByRole("heading", { level: 1, name: "j4.pdf" })).toBeInTheDocument();

    // The first job's load is aborted, and its late answer opens nothing on the second job.
    expect(signal?.aborted).toBe(true);
    release(new ArrayBuffer(8));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("operator-job-send-again")).not.toHaveAttribute("aria-busy");
  });

  it("does not send a shorter set of files without the operator's say", async () => {
    vi.mocked(useHasCapability).mockImplementation((capability) => capability === "run_workflows");
    vi.mocked(fetchArrayBuffer).mockImplementation(async (input) => {
      if (String(input) === "/api/jobs/j3/artifacts/a1") return new ArrayBuffer(8);
      if (String(input) === "/api/jobs/j3/artifacts/a2") throw new Error("gone");
      return new Promise<ArrayBuffer>(() => undefined);
    });
    vi.mocked(runBuilderWorkflow).mockResolvedValue({ job_id: "j9" });
    serveFinished(
      finished({
        job_values: {
          status: "failed",
          inputs: {
            doc_type: "invoice",
            pages: [
              { filename: "p1.pdf", content_type: "application/pdf", artifact_id: "a1" },
              { filename: "p2.pdf", content_type: "application/pdf", artifact_id: "a2" },
            ],
          },
          outputs: null,
        },
        decision: null,
      }),
    );
    renderPage();

    fireEvent.click(await screen.findByTestId("operator-job-send-again"));
    const dialog = await screen.findByRole("dialog");
    // The missing file is named inside the form, not under it on the page.
    expect(within(dialog).getByTestId("resend-missing-files")).toHaveTextContent("Pages: p2.pdf");
    fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));
    expect(await within(dialog).findByText("Add the missing files again, or confirm sending without them.")).toBeInTheDocument();
    expect(runBuilderWorkflow).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: "Send without these files" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(runBuilderWorkflow).toHaveBeenCalledTimes(1));
    expect(vi.mocked(runBuilderWorkflow).mock.calls[0][1].files.map((file) => file.filename)).toEqual(["p1.pdf"]);
  });

  it("shows a rejected review with who rejected it and why, and no values", async () => {
    serveFinished(
      finished({
        status: "failed",
        job_values: { status: "failed", inputs: { doc_type: "invoice" }, outputs: null },
        decision: {
          access: "read",
          job_id: "j3-child",
          approved: false,
          actor: "Marta",
          timestamp: "2026-11-19T10:00:00Z",
          comment: "Wrong supplier: the invoice is from Baltic Paper.",
          fields: [],
          values: {},
          changes: [],
        },
      }),
    );
    renderPage();

    const decided = await screen.findByTestId("operator-job-review");
    expect(within(decided).getByText(/Rejected by Marta/)).toBeInTheDocument();
    expect(within(decided).getByTestId("operator-job-review-comment")).toHaveTextContent("Reason: Wrong supplier: the invoice is from Baltic Paper.");
    expect(within(decided).queryByTestId("operator-job-review-values")).not.toBeInTheDocument();
  });

  it("gives a text result the wider column", async () => {
    const textOnly = {
      ...withForm,
      operator: {
        ...withForm.operator,
        settings: { published: true, result: { order: ["notes"], fields: { notes: { label: "Post", display: "wysiwyg" } } } },
        output_schema: { properties: { notes: { type: "string" } } },
      },
    } as unknown as OperatorSectionDetail;
    const detail = finished({ job_values: { status: "completed", inputs: { doc_type: "invoice" }, outputs: { notes: "## Coffee machines" } }, decision: null });
    mockedFetchJson.mockImplementation(async (input) => {
      const url = new URL(String(input), "https://example.test");
      if (url.pathname === "/api/v2/projects/p1/workflows/wf.contracts/section") return textOnly as never;
      if (url.pathname === "/api/v2/jobs/j3/review") return detail as never;
      if (url.pathname === "/api/v2/jobs") return { job_id: "j3", in_list: true, position: 3, total: 47, previous_job_id: null, next_job_id: null } as never;
      throw new Error(`unexpected ${url.pathname}`);
    });
    renderPage();

    expect(await screen.findByTestId("operator-formatted-text")).toBeInTheDocument();
    // No files: the panel alone, the whole width.
    expect(screen.getByTestId("operator-job")).toHaveAttribute("data-original", "none");
    expect(screen.queryByTestId("operator-page-original")).not.toBeInTheDocument();
  });

  it("shows no result before the run succeeded and only that a review was decided without access", async () => {
    serveFinished(finished({ status: "running", job_values: { status: "running", inputs: { doc_type: "invoice" }, outputs: null }, decision: { access: "none" } }));
    renderPage();

    expect(await screen.findByText("This job was reviewed by people who have access to it.")).toBeInTheDocument();
    expect(screen.queryByTestId("operator-job-result")).not.toBeInTheDocument();
    // The panel keeps the result's place and says where it will be.
    expect(screen.getByTestId("operator-job-result-pending")).toHaveTextContent(
      "The result appears here when the run finishes.",
    );
    expect(screen.queryByText("Marta")).not.toBeInTheDocument();
  });
});

describe("OperatorJobPage decisions the gate allows", () => {
  function withActions(actions: string[]): JobReview {
    const base = review("j3");
    return { ...base, gate: base.gate ? { ...base.gate, allowed_actions: actions } : null };
  }

  it("offers only approval when the gate allows only approval", async () => {
    reviews.j3 = withActions(["approve"]);
    renderPage();
    await screen.findByTestId("operator-review");

    expect(screen.getByRole("button", { name: "Export" })).toBeInTheDocument();
    expect(screen.getByTestId("review-approve-next")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Recognize again" })).not.toBeInTheDocument();
  });

  it("offers neither approval nor Ctrl+Enter when the gate allows only rejection", async () => {
    reviews.j3 = withActions(["reject"]);
    renderPage();
    await screen.findByTestId("operator-review");
    await waitFor(() => expect(screen.getByTestId("operator-queue-position")).toHaveTextContent("3 of 47"));

    expect(screen.queryByRole("button", { name: "Export" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("review-approve-next")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recognize again" })).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "Enter", ctrlKey: true });
    expect(decisions()).toHaveLength(0);
    expect(screen.getByTestId("location")).toHaveTextContent("/work/jobs/j3?");
  });

  it("leaves a gate with override or abort to the existing review panel", async () => {
    reviews.j3 = withActions(["approve", "override", "abort"]);
    renderPage();

    expect(await screen.findByTestId("job-detail")).toHaveTextContent("j3-child");
    expect(screen.queryByTestId("operator-review")).not.toBeInTheDocument();
    expect(screen.getByTestId("operator-queue")).toBeInTheDocument();
  });
});

