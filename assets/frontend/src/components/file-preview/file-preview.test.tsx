/* eslint-disable testing-library/no-node-access -- the frame's document is read as DOM. */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";

import { FilePreview } from "@/components/file-preview/file-preview";
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { JobFileItem } from "@/pages/jobs/detail/job-detail-file-preview";

type RenderParams = { canvas: HTMLCanvasElement; viewport: { width: number; height: number }; annotationMode: number };

const pdf = vi.hoisted(() => {
  const render = vi.fn((_params: RenderParams) => ({ promise: Promise.resolve(), cancel: vi.fn() }));
  const page = {
    getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale }),
    render,
  };
  const loadedDocument = { numPages: 3, getPage: vi.fn(async (_page: number) => page) };
  const getDocument = vi.fn((_options: Record<string, unknown>) => ({
    promise: Promise.resolve(loadedDocument),
    destroy: vi.fn(async () => undefined),
  }));
  return { render, loadedDocument, getDocument, workerOptions: { workerSrc: "" } };
});

const docx = vi.hoisted(() => ({
  parseAsync: vi.fn(async (_data: unknown, _options: Record<string, unknown>) => ({})),
  renderDocument: vi.fn(async (_document: unknown, _options: Record<string, unknown>): Promise<Node[]> => []),
}));

vi.mock("pdfjs-dist", () => ({ AnnotationMode: { ENABLE: 1 }, GlobalWorkerOptions: pdf.workerOptions, getDocument: pdf.getDocument }));
vi.mock("@/components/file-preview/pdf-assets", () => ({
  PDF_JS_WORKER_URL: "/app/assets/pdf.worker-test.mjs",
  PDF_JS_DECODERS_URL: "http://localhost:3000/app/assets/pdfjs-test/",
}));
vi.mock("docx-preview", () => ({ parseAsync: docx.parseAsync, renderDocument: docx.renderDocument }));

function serveBytes() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(new Uint8Array([37, 80, 68, 70, 45])));
}

afterEach(() => {
  vi.restoreAllMocks();
  pdf.render.mockClear();
  pdf.getDocument.mockClear();
  pdf.loadedDocument.getPage.mockClear();
});

describe("PDF preview", () => {
  it("draws pages with pdf.js on a canvas, with paging and zoom, and no scripting", async () => {
    const fetchSpy = serveBytes();
    render(<FilePreview kind="pdf" name="scan.pdf" url="/api/jobs/job-1/artifacts/art-1/preview" />);

    await waitFor(() => expect(screen.getByText("Page 1 of 3")).toBeInTheDocument());
    expect(fetchSpy.mock.calls[0][0]).toBe("/api/jobs/job-1/artifacts/art-1/preview");
    expect(pdf.workerOptions.workerSrc).toBe("/app/assets/pdf.worker-test.mjs");
    const options = pdf.getDocument.mock.calls[0][0];
    expect(options).toMatchObject({ enableXfa: false, useWasm: false, wasmUrl: "http://localhost:3000/app/assets/pdfjs-test/" });
    expect(options).not.toHaveProperty("enableScripting");
    expect(options.data).toBeInstanceOf(Uint8Array);

    await waitFor(() => expect(pdf.render).toHaveBeenCalled());
    const drawn = pdf.render.mock.calls[0][0];
    expect(drawn.canvas).toBe(screen.getByRole("img", { name: "scan.pdf, page 1" }));
    expect(drawn.annotationMode).toBe(1);
    expect(drawn.viewport.width).toBe(600);
    // No browser viewer, no frame, no embedded object.
    expect(document.querySelector("iframe, embed, object")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(screen.getByText("Page 2 of 3")).toBeInTheDocument());
    expect(pdf.loadedDocument.getPage).toHaveBeenLastCalledWith(2);

    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    await waitFor(() => expect(screen.getByText("125%")).toBeInTheDocument());
    expect(pdf.render.mock.calls.at(-1)?.[0].viewport.width).toBe(750);
    expect(screen.getByRole("button", { name: "Fit to width" })).toHaveAttribute("aria-pressed", "false");
  });

  it("says a password-protected PDF needs its download", async () => {
    serveBytes();
    const locked = Object.assign(new Error("No password given"), { name: "PasswordException" });
    pdf.getDocument.mockImplementationOnce(() => ({ promise: Promise.reject(locked), destroy: vi.fn(async () => undefined) }));

    render(<FilePreview kind="pdf" name="locked.pdf" url="/api/jobs/job-1/artifacts/art-2/preview" />);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("protected with a password"));
  });
});

describe("DOCX preview", () => {
  it("shows the sanitized document inside a frame that runs no script", async () => {
    serveBytes();
    const style = document.createElement("style");
    style.textContent = ".docx p { margin: 0; }";
    const section = document.createElement("section");
    section.className = "docx";
    section.innerHTML =
      '<p onclick="steal()">Agreement text</p><script>steal()</script>' +
      '<a href="javascript:steal()">bad link</a><img src="https://tracker.example/p.png" onerror="steal()" alt="">';
    docx.renderDocument.mockResolvedValueOnce([style, section]);

    render(<FilePreview kind="docx" name="contract.docx" url="/api/jobs/job-1/artifacts/art-3/preview" />);

    await waitFor(() => expect(screen.getByTitle("contract.docx").tagName).toBe("IFRAME"));
    const frame = screen.getByTitle("contract.docx") as HTMLIFrameElement;
    expect(frame.getAttribute("sandbox")).toBe("allow-same-origin");
    expect(frame).not.toHaveAttribute("src");
    await waitFor(() => expect(frame.contentDocument?.body.textContent).toContain("Agreement text"));
    const body = frame.contentDocument?.body as HTMLElement;
    expect(body.querySelector("script, style")).toBeNull();
    expect(body.querySelector("[onclick], [onerror], [href^='javascript'], [src^='https']")).toBeNull();

    const options = docx.parseAsync.mock.calls[0][1];
    expect(options).toMatchObject({ renderAltChunks: false, ignoreFonts: true, renderComments: false, useBase64URL: true });
  });
});

function withPreviewLimit(maxBytes: number, children: ReactNode) {
  const value: AuthContextValue = {
    error: null,
    isAuthenticated: true,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount: vi.fn(async () => undefined),
    session: { authenticated: true, roles: ["operator"], file_preview_max_bytes: maxBytes } as AuthContextValue["session"],
    status: "authenticated",
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

describe("preview size limit", () => {
  it("offers a file over the session's limit as a download only and never reads it", () => {
    const fetchSpy = serveBytes();
    render(
      withPreviewLimit(
        1024,
        <JobFileItem file={{ filename: "big.xlsx", size_bytes: 4096, download_url: "/api/jobs/job-1/artifacts/art-4" }} />,
      ),
    );

    expect(screen.getByTestId("job-file-preview-too-large")).toHaveTextContent("larger than the preview limit");
    expect(screen.queryByRole("button", { name: "View" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute("href", "/api/jobs/job-1/artifacts/art-4");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("previews a file within the limit", () => {
    render(
      withPreviewLimit(
        8192,
        <JobFileItem file={{ filename: "small.xlsx", size_bytes: 4096, download_url: "/api/jobs/job-1/artifacts/art-5" }} />,
      ),
    );

    const item = screen.getByText("small.xlsx").closest("[data-preview-kind]") as HTMLElement;
    expect(item).toHaveAttribute("data-preview-kind", "spreadsheet");
    expect(within(item).getByRole("button", { name: "View" })).toBeInTheDocument();
  });

  it("says why when the server refuses a preview as too large", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async () => new Response(JSON.stringify({ error: "too large" }), { status: 413, headers: { "content-type": "application/json" } }),
    );

    render(<FilePreview kind="spreadsheet" name="big.csv" url="/api/jobs/job-1/artifacts/art-6/preview" />);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("larger than the preview limit"));
  });
});
