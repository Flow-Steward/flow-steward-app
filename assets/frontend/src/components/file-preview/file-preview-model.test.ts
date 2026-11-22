import { docxStyleText, sanitizeDocxNodes } from "@/components/file-preview/docx-sanitize";
import { filePreviewKind, isDocumentPreviewKind, previewTooLarge } from "@/components/file-preview/file-preview-model";
import { csvPreviewSlice } from "@/components/file-preview/spreadsheet-preview-model";

const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

describe("which files the page previews", () => {
  it.each([
    [{ content_type: "application/pdf" }, "pdf"],
    [{ content_type: XLSX }, "spreadsheet"],
    [{ content_type: "application/vnd.ms-excel" }, "spreadsheet"],
    [{ content_type: "text/csv; charset=utf-8" }, "spreadsheet"],
    [{ mime_type: DOCX }, "docx"],
    [{ filename: "scan.PDF" }, "pdf"],
    [{ filename: "totals.xlsx" }, "spreadsheet"],
    [{ filename: "legacy.xls" }, "spreadsheet"],
    [{ filename: "rows.csv" }, "spreadsheet"],
    [{ name: "contract.docx" }, "docx"],
  ])("previews %o as %s", (file, kind) => {
    expect(filePreviewKind(file)).toBe(kind);
  });

  it.each([
    [{ content_type: "application/msword", filename: "letter.doc" }],
    [{ filename: "letter.doc" }],
    [{ content_type: "application/vnd.ms-powerpoint", filename: "slides.ppt" }],
    [{ filename: "slides.ppt" }],
    [{ filename: "slides.pptx" }],
    [{ filename: "notes.odt" }],
    [{ filename: "notes.rtf" }],
    [{ content_type: "image/tiff", filename: "scan.tiff" }],
    // A declared type is never overridden by the name.
    [{ content_type: "application/octet-stream", filename: "totals.xlsx" }],
    [{ content_type: "application/zip", filename: "contract.docx" }],
  ])("offers %o as a download only", (file) => {
    expect(filePreviewKind(file)).toBe("");
  });

  it("reads the document kinds as bytes", () => {
    expect(isDocumentPreviewKind("pdf")).toBe(true);
    expect(isDocumentPreviewKind("spreadsheet")).toBe(true);
    expect(isDocumentPreviewKind("docx")).toBe(true);
    expect(isDocumentPreviewKind("image")).toBe(false);
    expect(isDocumentPreviewKind("text")).toBe(false);
    expect(isDocumentPreviewKind("")).toBe(false);
  });

  it("keeps a file over the preview limit to its download", () => {
    expect(previewTooLarge({ size_bytes: 2048 }, 1024)).toBe(true);
    expect(previewTooLarge({ size_bytes: 1024 }, 1024)).toBe(false);
    expect(previewTooLarge({ size_bytes: 2048 }, undefined)).toBe(false);
    expect(previewTooLarge({ preview_too_large: true }, undefined)).toBe(true);
    expect(previewTooLarge({}, 1024)).toBe(false);
  });
});

describe("CSV rows drawn", () => {
  it("counts every record and cuts after the limit, keeping line breaks inside quotes", () => {
    const text = 'name,note\n"Ann","two\nlines"\nBob,x\r\nCid,y\n';
    expect(csvPreviewSlice(text, 2)).toEqual({ text: 'name,note\n"Ann","two\nlines"\n', total: 4 });
    expect(csvPreviewSlice(text, 10)).toEqual({ text, total: 4 });
    expect(csvPreviewSlice("a,b\nc,d", 10).total).toBe(2);
    expect(csvPreviewSlice("", 10).total).toBe(0);
  });
});

describe("a rendered Word document's nodes", () => {
  function element(html: string): Element {
    const holder = document.createElement("div");
    holder.innerHTML = html;
    return holder.firstElementChild as Element;
  }

  it("keep text and layout and lose script, handlers and outside addresses", () => {
    const style = document.createElement("style");
    style.textContent = ".docx p { color: red; }";
    const section = element(
      '<section class="docx"><p onclick="steal()">Contract <a href="javascript:alert(1)">here</a>' +
        '<a href="https://example.test/terms">terms</a><a href="#part-2">part 2</a></p>' +
        '<img src="https://tracker.example/pixel.png" onerror="steal()" alt="pixel">' +
        '<img src="data:image/png;base64,iVBORw0KGgo=" alt="logo">' +
        "<script>steal()</script><iframe></iframe><form><input></form><object></object></section>",
    );
    const kept = sanitizeDocxNodes([style, section]);

    expect(kept).toEqual([section]);
    expect(section).toHaveTextContent("Contract here");
    expect(section.querySelector("script, iframe, form, input, object")).toBeNull();
    expect(section.querySelector("[onclick], [onerror]")).toBeNull();
    const [scriptLink, outsideLink, anchor] = Array.from(section.querySelectorAll("a"));
    expect(scriptLink).not.toHaveAttribute("href");
    expect(outsideLink).not.toHaveAttribute("href");
    expect(outsideLink).toHaveAttribute("title", "https://example.test/terms");
    expect(anchor).toHaveAttribute("href", "#part-2");
    const [tracker, logo] = Array.from(section.querySelectorAll("img"));
    expect(tracker).not.toHaveAttribute("src");
    expect(logo).toHaveAttribute("src", "data:image/png;base64,iVBORw0KGgo=");
    expect(docxStyleText([style, section])).toBe(".docx p { color: red; }");
  });

  it("keeps drawn shapes and formulas but not SVG links or animation", () => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.innerHTML = '<rect width="10" height="10"></rect><a href="javascript:alert(1)"><text>x</text></a><animate attributeName="x"></animate>';
    const math = document.createElementNS("http://www.w3.org/1998/Math/MathML", "math");
    math.innerHTML = "<mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow>";

    expect(sanitizeDocxNodes([svg, math])).toEqual([svg, math]);
    expect(svg.querySelector("rect")).not.toBeNull();
    expect(svg.querySelector("a, animate")).toBeNull();
    expect(math).toHaveTextContent(/^x\+1$/);
  });
});
