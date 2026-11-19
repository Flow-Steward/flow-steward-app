/* eslint-disable testing-library/no-node-access, testing-library/no-container -- review fields are addressed by their data-field frame. */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";

import { OperatorJobPage } from "./job-page";
import type { JobReview, ReviewField } from "./review-model";
import type { OperatorSectionDetail } from "./types";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn(), fetchText: vi.fn() }));
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

const LIST = "f.state=awaiting_review&search=acme&page=2";

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
    expect(within(frame(container, "currency")).getByRole("combobox")).toBeInTheDocument();
    expect(within(frame(container, "lines")).getByRole("table")).toBeInTheDocument();
    // Not editable: shown, never changed.
    expect(input("confidence")).toBeDisabled();
    expect(input("supplier")).toBeEnabled();
    // The original: one file at a time, several paged; the step's own labels on the buttons.
    const original = screen.getByTestId("review-original");
    expect(within(original).getByText("File 1 of 2")).toBeInTheDocument();
    expect(within(original).getByRole("img", { name: "page-1.png" })).toHaveAttribute("src", "/api/jobs/j3-child/artifacts/art-1/preview");
    fireEvent.click(within(original).getByRole("button", { name: "Next file" }));
    expect(within(original).getByTitle("page-2.pdf")).toHaveAttribute("src", "/api/jobs/j3-child/artifacts/art-2/preview");
    expect(screen.getByRole("button", { name: "Export" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recognize again" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("operator-queue-position")).toHaveTextContent("3 of 47"));
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
    fireEvent.change(frame(container, "total").querySelector("input") as HTMLInputElement, { target: { value: "318,5О" } });

    fireEvent.click(screen.getByTestId("review-approve-next"));

    expect(await within(frame(container, "total")).findByText("Total must be a number")).toBeInTheDocument();
    expect(decisions()[0].body.values).toEqual({ total: "318,5О" });
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
    expect(screen.getByTestId("job-detail")).toHaveTextContent("j3");
  });

  it("leads back to the same list and keeps a job without review fields on Job Details", async () => {
    reviews.j3 = review("j3", "decide", { status: "completed", waiting: false, review_job_id: null, access: null, gate: null });
    renderPage();

    expect(await screen.findByTestId("job-detail")).toHaveTextContent("j3");
    const back = screen.getByRole("link", { name: "Back to Contracts" });
    expect(back).toHaveAttribute("href", `/projects/p1/workflows/wf.contracts/work?${LIST}`);
    await waitFor(() => expect(screen.getByTestId("operator-queue-position")).toHaveTextContent("3 of 47"));
    const around = mockedFetchJson.mock.calls
      .map(([input]) => new URL(String(input), "https://example.test"))
      .find((url) => url.searchParams.get("around") === "j3");
    expect(around?.searchParams.get("filters")).toBe(JSON.stringify({ state: ["awaiting_review"] }));
    expect(around?.searchParams.get("search")).toBe("acme");
    expect(around?.searchParams.has("page")).toBe(false);
  });

  it("loads the next job's review ahead", async () => {
    renderPage();
    await screen.findByTestId("operator-review");

    await waitFor(() => expect(mockedFetchJson.mock.calls.some(([input]) => String(input) === "/api/v2/jobs/j4/review")).toBe(true));
    await waitFor(() => expect(document.head.querySelector('link[rel="prefetch"][href="/api/jobs/j4-child/artifacts/art-1/preview"]')).not.toBeNull());
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

