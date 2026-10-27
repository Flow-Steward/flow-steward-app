import { Marked } from "marked";

import { textOrEmpty } from "@/pages/workflows/detail/detail-helpers";

export type TemplateInputRow = {
  name: string;
  type: string;
  required: boolean;
  source: string;
};

export function templateInputRows(value: unknown): TemplateInputRow[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((row) => row && typeof row === "object")
    .map((row: Record<string, unknown>) => ({
      name: textOrEmpty(row.name),
      type: textOrEmpty(row.type) || "text",
      required: row.required !== false,
      source: textOrEmpty(row.source),
    }));
}

export type TemplateSegment = { text: string; kind: "plain" | "expression" | "tag" };

const RAW_OPEN = /^\{%-?\s*raw\s*-?%\}$/;
const RAW_CLOSE = /^\{%-?\s*endraw\s*-?%\}$/;

/** The index just past the next `close` from `from`, or -1 when it never closes. */
function closingIndex(source: string, from: number, close: string): number {
  const at = source.indexOf(close, from);
  return at < 0 ? -1 : at + close.length;
}

/**
 * Split a template into plain text and complete `{{ … }}` / `{% … %}` pieces,
 * only to colour them. A `raw` block stays plain, and an unfinished `{{` is plain
 * text until it is closed: nothing here is parsed or evaluated. One left-to-right
 * pass, so its cost is linear in the length of the text.
 */
export function templateSegments(source: string): TemplateSegment[] {
  const segments: TemplateSegment[] = [];
  let plainStart = 0;
  let cursor = 0;
  const pushPlain = (end: number) => {
    if (end > plainStart) segments.push({ text: source.slice(plainStart, end), kind: "plain" });
  };
  while (cursor < source.length) {
    const open = source.indexOf("{", cursor);
    if (open < 0) break;
    const opener = source.slice(open, open + 2);
    if (opener !== "{{" && opener !== "{%") {
      cursor = open + 1;
      continue;
    }
    const end = closingIndex(source, open + 2, opener === "{{" ? "}}" : "%}");
    if (end < 0) break;
    const token = source.slice(open, end);
    if (opener === "{%" && RAW_OPEN.test(token)) {
      // Everything up to the matching endraw (or the end of the text) is literal.
      let rawEnd = source.length;
      let search = end;
      while (search < source.length) {
        const tagStart = source.indexOf("{%", search);
        if (tagStart < 0) break;
        const tagEnd = closingIndex(source, tagStart + 2, "%}");
        if (tagEnd < 0) break;
        if (RAW_CLOSE.test(source.slice(tagStart, tagEnd))) {
          rawEnd = tagEnd;
          break;
        }
        search = tagEnd;
      }
      cursor = rawEnd;
      continue;
    }
    pushPlain(open);
    segments.push({ text: token, kind: opener === "{{" ? "expression" : "tag" });
    plainStart = end;
    cursor = end;
  }
  pushPlain(source.length);
  return segments;
}

function escapeHtml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// Markdown is shown as Markdown: raw HTML inside it is printed as text, not run.
const markdown = new Marked({
  gfm: true,
  renderer: {
    html(token) {
      return escapeHtml(token.text);
    },
  },
});

/**
 * The HTML the preview frame shows. Markdown becomes HTML with any raw HTML in
 * it printed as text; an HTML template is shown as it is, inside a page that
 * runs none of its scripts (see TemplatePreviewFrame).
 */
export function templatePreviewBody(text: string, format: string): string {
  return format === "html" ? text : (markdown.parse(text, { async: false }) as string);
}
