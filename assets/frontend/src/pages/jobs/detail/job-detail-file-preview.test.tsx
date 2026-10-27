import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

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
});
