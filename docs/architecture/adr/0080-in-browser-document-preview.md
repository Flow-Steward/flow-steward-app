# ADR 0080 In-browser document preview

Status: Proposed

Date: 2026-11-20

## Decision

Core shows PDFs, spreadsheets and Word documents directly on the page, without
server-side conversion. The browser reads the file through the existing job
file preview route with the user's session, and a library that the page loads
only when a preview opens renders it. One shared preview component is used in
the job file list and for the original on the operator's review page
([ADR 0075](0075-project-user-sections.md#review-page)).

The browser's built-in PDF viewer is not used: it differs between browsers,
and the application's built-in browser downloads PDFs instead of showing them.

## Formats

| Format | Rendered with | What the user sees |
|---|---|---|
| PNG, JPEG, GIF, WebP | An `img` element, as before | The image |
| TXT, Markdown, JSON | Text as characters, as before | Text without markup |
| PDF | pdf.js, canvas | One page, paging, "fit width" zoom, "+" and "−" |
| XLSX, XLS, CSV | SheetJS Community Edition | A read-only table, sheet tabs, the first 200 rows and 50 columns with a "showing N of M" note |
| DOCX | docx-preview | The document in an isolated frame |

DOC, PPT, PPTX, ODT, RTF, TIFF and other formats get no preview and keep the
"Download" button. The file kind is determined by the declared type; the
extension is used only when no type is declared: a file declared with another
type is not shown based on its extension. This rule is the same in the
browser and on the server.

Spreadsheet formulas are not evaluated: the table shows the value stored in
the file, formatted with the file's own number format. The standard date
format is shown as `YYYY-MM-DD`. CSV is read as UTF-8 and values stay as
written; all rows of the file are counted, but only the shown rows are parsed.

## Libraries and licenses

Licenses were checked against package metadata and the LICENSE files in the
package archives. All versions are pinned exactly in
`assets/frontend/package.json` and in `package-lock.json` with a checksum.

| Library | Version | License | Source |
|---|---|---|---|
| pdf.js (`pdfjs-dist`) | 6.5.438 | Apache-2.0 | npm |
| SheetJS Community Edition (`xlsx`) | 0.20.3 | Apache-2.0 | `https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz` |
| docx-preview | 0.4.1 | Apache-2.0 | npm |
| JSZip (docx-preview dependency) | 3.10.2 | MIT or GPL-3.0-or-later, MIT used | npm |
| pako (inside the JSZip browser build) | 1.0.11 | MIT AND Zlib | npm |

SheetJS is installed from the official SheetJS CDN: the `xlsx` 0.18.5 package
in the npm registry is outdated and has known vulnerabilities. The archive is
pinned in the lockfile by URL and `sha512`, so `npm ci` verifies it the same
way as registry packages.

The frontend license check (`frontend-licenses`) reads SPDX expressions: with
`OR`, one allowed alternative is enough; with `AND`, all must be allowed. Zlib,
a permissive license compatible with Apache-2.0, is added to the allow list.
The packages are listed in `NOTICE`.

Only the pdf.js code, the worker and the JavaScript decoders for JBIG2
(PDFium, BSD-3-Clause) and OpenJPEG (BSD-2-Clause) go into the build. The
package's standard fonts (including Liberation under the GPL with a font
exception), CMap files and WebAssembly are not shipped: missing standard fonts
are replaced with system fonts.

## Security

- Nothing from the file is executed. The server serves XLSX, XLS, CSV and DOCX
  as `application/octet-stream` with `Content-Disposition: attachment`,
  `Content-Security-Policy: sandbox; default-src 'none'` and `nosniff`: the
  browser does not open them by itself; only the page's code reads them.
- A PDF is rendered only as an image on a canvas: no annotation or form layer,
  no text layer or links, no document JavaScript (the pdf.js scripting sandbox
  is not created), no XFA. Since version 5, pdf.js does not evaluate code from
  the document, so the `isEvalSupported` switch was removed from the library.
  WebAssembly is off (`useWasm: false`) because the application's CSP does not
  allow compiling it; the JavaScript decoders work instead.
- The pdf.js worker and decoders are part of the application build and are
  served by the application itself; no CDN is used at runtime. The decoders
  are kept in a folder named after their pdf.js version, because pdf.js loads
  them by name.
- Spreadsheet cell values are shown as text. Formulas, macros and external
  links are not processed.
- DOCX is protected by two independent measures:
  - The document is shown in an `iframe` with `sandbox="allow-same-origin"`
    without `allow-scripts`: nothing runs inside, and the frame opens no
    windows and does not navigate to application addresses.
    `allow-same-origin` is needed so the page itself can write the document
    into the frame; without `allow-scripts` this combination is safe. The
    document's styles stay inside the frame.
  - The nodes built by docx-preview are sanitized before display: only the
    HTML, SVG and MathML elements the document needs remain, `on*` handlers
    are removed, links keep only anchors inside the document (the external
    address is visible in a tooltip), and images keep only `data:image/…`
    read from the file itself. docx-preview runs without embedded HTML parts
    (`altChunk`), embedded fonts, comments and tracked changes.

  Both measures were chosen because each closes a gap in the other: a
  sanitizer bug cannot run a script in a frame without `allow-scripts`, and
  sanitization does not depend on the browser supporting `sandbox`. The
  application's CSP forbids inline `<style>`, and the frame inherits it, so the
  document's styles are applied as a constructed stylesheet
  (`adoptedStyleSheets`).

## Size limit

The limit is the existing `FS_FILE_PREVIEW_MAX_BYTES` (20 MiB by default). The
application bootstrap passes it to the signed-in user in
`limits.file_preview.max_bytes`, the same way as `limits.run_upload`. A file
over the limit is shown immediately with only the "Download" button and an
explanation, without a preview request. The server checks the same limit and
responds with 413 before reading the file.

## Lazy loading

Each library is loaded with a dynamic `import()` only when a preview of its
kind opens, and is built into a separate chunk (`pdf-preview-vendor`,
`spreadsheet-preview-vendor`, `docx-preview-vendor`). The main bundle and the
shared `vendor` chunk do not contain them. A file list without an open preview
loads neither file bytes nor library code.

## Converters only as extensions

Core does not depend on server-side document converters (Gotenberg,
LibreOffice and similar) and does not call them. If a preview of DOC, PPT, ODT
or RTF through conversion is ever needed, a separate extension with its own
service provides it. Core does not change and keeps showing such files with
the "Download" button.

## Existing owners

- [Shared preview component](../../../assets/frontend/src/components/file-preview/file-preview.tsx).
- [File kinds and size limit](../../../assets/frontend/src/components/file-preview/file-preview-model.ts).
- [DOCX sanitization](../../../assets/frontend/src/components/file-preview/docx-sanitize.ts).
- [Job file in the list](../../../assets/frontend/src/pages/jobs/detail/job-detail-file-preview.tsx).
- [Original on the review page](../../../assets/frontend/src/pages/operator/review-panel.tsx).
- [Job file preview route](../../../core/entrypoints/api/job_detail_routes.py).
- [Limit in bootstrap](../../../core/entrypoints/api/app_bootstrap.py).
- [Frontend license check](../../../core/application/tooling/quality/frontend_licenses.py).
