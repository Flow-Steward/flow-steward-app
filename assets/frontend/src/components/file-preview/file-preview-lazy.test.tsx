import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { JobFileItem } from "@/pages/jobs/detail/job-detail-file-preview";

// Each library records when it is first imported: only opening a preview may do that.
const loaded = vi.hoisted(() => [] as string[]);

vi.mock("pdfjs-dist", () => {
  loaded.push("pdfjs-dist");
  const page = {
    getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale }),
    render: () => ({ promise: Promise.resolve(), cancel: () => undefined }),
  };
  return {
    AnnotationMode: { ENABLE: 1 },
    GlobalWorkerOptions: { workerSrc: "" },
    getDocument: () => ({ promise: Promise.resolve({ numPages: 1, getPage: async () => page }), destroy: async () => undefined }),
  };
});
vi.mock("@/components/file-preview/pdf-assets", () => ({ PDF_JS_WORKER_URL: "/app/assets/pdf.worker.mjs", PDF_JS_DECODERS_URL: "" }));
vi.mock("xlsx", () => {
  loaded.push("xlsx");
  return { read: () => ({ SheetNames: [], Sheets: {} }), utils: {} };
});
vi.mock("docx-preview", () => {
  loaded.push("docx-preview");
  return { parseAsync: async () => ({}), renderDocument: async () => [] };
});

describe("document previewers load on demand", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("imports a library only when a preview of its kind opens, and nothing for a listed file", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(new Uint8Array([37, 80, 68, 70])));
    render(
      <>
        <JobFileItem file={{ filename: "scan.pdf", content_type: "application/pdf", download_url: "/api/jobs/job-1/artifacts/art-1" }} />
        <JobFileItem file={{ filename: "totals.xlsx", download_url: "/api/jobs/job-1/artifacts/art-2" }} />
        <JobFileItem file={{ filename: "contract.docx", download_url: "/api/jobs/job-1/artifacts/art-3" }} />
      </>,
    );

    expect(screen.getAllByRole("button", { name: "View" })).toHaveLength(3);
    expect(loaded).toEqual([]);
    expect(fetchSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getAllByRole("button", { name: "View" })[0]);

    await waitFor(() => expect(screen.getByText("Page 1 of 1")).toBeInTheDocument());
    expect(loaded).toEqual(["pdfjs-dist"]);
    expect(fetchSpy.mock.calls.map(([input]) => String(input))).toEqual(["/api/jobs/job-1/artifacts/art-1/preview"]);
  });
});
