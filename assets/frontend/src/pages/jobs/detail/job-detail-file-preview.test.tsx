import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobFileItem } from "@/pages/jobs/detail/job-detail-file-preview";
import { filePreviewKind, filePreviewUrl } from "@/pages/jobs/detail/job-detail-file-preview-model";

describe("generated file previews", () => {
  it("previews only a job's own artifacts, and only images and PDFs", () => {
    expect(filePreviewUrl("/api/jobs/job-1/artifacts/art-1")).toBe("/api/jobs/job-1/artifacts/art-1/preview");
    expect(filePreviewUrl("https://storage.example/art-1")).toBe("");
    expect(filePreviewUrl("/api/jobs/job-1/attachments/7")).toBe("");

    expect(filePreviewKind({ content_type: "image/png" })).toBe("image");
    expect(filePreviewKind({ content_type: "image/jpeg; charset=binary" })).toBe("image");
    expect(filePreviewKind({ filename: "photo.webp" })).toBe("image");
    expect(filePreviewKind({ content_type: "application/pdf" })).toBe("pdf");
    expect(filePreviewKind({ content_type: "image/svg+xml", filename: "logo.svg" })).toBe("");
    expect(filePreviewKind({ content_type: "text/csv", filename: "a.png" })).toBe("");
  });

  it("shows a thumbnail for an image that opens larger", () => {
    render(
      <JobFileItem
        file={{
          filename: "cover.png",
          content_type: "image/png",
          size_bytes: 52_000,
          download_url: "/api/jobs/job-1/artifacts/art-1",
        }}
      />,
    );

    expect(screen.getByTestId("job-file-thumbnail")).toHaveAttribute("src", "/api/jobs/job-1/artifacts/art-1/preview");
    expect(screen.getByText("52 KB")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute("href", "/api/jobs/job-1/artifacts/art-1");

    fireEvent.click(screen.getByRole("button", { name: "Preview cover.png" }));
    expect(screen.getByRole("img", { name: "cover.png" })).toBeInTheDocument();
  });

  it("opens a PDF in a new tab and shows other files by name only", () => {
    const { rerender } = render(
      <JobFileItem
        file={{ filename: "report.pdf", content_type: "application/pdf", download_url: "/api/jobs/job-1/artifacts/art-2" }}
      />,
    );
    expect(screen.getByRole("link", { name: "Open" })).toHaveAttribute("href", "/api/jobs/job-1/artifacts/art-2/preview");

    rerender(
      <JobFileItem
        file={{ filename: "export.csv", content_type: "text/csv", download_url: "/api/jobs/job-1/artifacts/art-3" }}
      />,
    );
    expect(screen.queryByRole("link", { name: "Open" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("job-file-thumbnail")).not.toBeInTheDocument();
    expect(screen.getByText("export.csv")).toBeInTheDocument();
  });

  it("previews TXT, Markdown and JSON as text, by type first and by name only without one", () => {
    expect(filePreviewKind({ content_type: "text/plain" })).toBe("text");
    expect(filePreviewKind({ content_type: "text/markdown; charset=utf-8" })).toBe("text");
    expect(filePreviewKind({ content_type: "application/json; charset=UTF-8" })).toBe("text");
    expect(filePreviewKind({ filename: "notes.TXT" })).toBe("text");
    expect(filePreviewKind({ filename: "README.md" })).toBe("text");
    expect(filePreviewKind({ filename: "result.json" })).toBe("text");
    // A declared type the preview does not support is not overridden by the name.
    expect(filePreviewKind({ content_type: "application/octet-stream", filename: "notes.txt" })).toBe("");
    expect(filePreviewKind({ content_type: "text/html", filename: "page.md" })).toBe("");
  });
});

describe("text file preview", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function textFile(overrides: Record<string, unknown> = {}) {
    return {
      filename: "summary.md",
      content_type: "text/markdown",
      download_url: "/api/jobs/job-1/artifacts/art-9",
      ...overrides,
    };
  }

  it("shows the text as characters, with line breaks, and keeps the download", async () => {
    const body = "# Title\n<img src=x onerror=alert(1)>\nline two";
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(body, { status: 200, headers: { "content-type": "text/plain; charset=utf-8" } }));

    render(<JobFileItem file={textFile()} />);
    fireEvent.click(screen.getByRole("button", { name: "View" }));

    const shown = await screen.findByLabelText("summary.md");
    expect(shown.tagName).toBe("PRE");
    expect(shown).toHaveTextContent(body, { normalizeWhitespace: false });
    expect(within(shown).queryByRole("img")).not.toBeInTheDocument();
    expect(fetchSpy.mock.calls[0][0]).toBe("/api/jobs/job-1/artifacts/art-9/preview");
    expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute("href", "/api/jobs/job-1/artifacts/art-9");

    fireEvent.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByLabelText("summary.md")).not.toBeInTheDocument();
  });

  it.each([
    [413, "larger than the preview limit"],
    [422, "not UTF-8 text"],
    [500, "could not be loaded"],
  ])("says why a %i preview cannot be shown and keeps the download", async (status, reason) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: "refused" }), { status, headers: { "content-type": "application/json" } }),
    );

    render(<JobFileItem file={textFile({ filename: "big.json", content_type: "application/json" })} />);
    fireEvent.click(screen.getByRole("button", { name: "View" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(reason);
    expect(screen.getByRole("link", { name: "Download" })).toBeInTheDocument();
  });

  it("offers no text view for a file without a job preview", () => {
    render(<JobFileItem file={textFile({ download_url: "https://storage.example/art-9" })} />);
    expect(screen.queryByRole("button", { name: "View" })).not.toBeInTheDocument();
  });
});
