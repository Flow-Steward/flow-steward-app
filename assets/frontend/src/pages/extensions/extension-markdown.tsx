import type { ReactNode } from "react";

/**
 * Safe Markdown rendering for the declarative extension UI.
 *
 * Extension pages are authored as Markdown in `type: markdown` components. This
 * module turns that text into React elements. It never produces raw HTML and
 * never uses `dangerouslySetInnerHTML`: every node is constructed here, so an
 * extension author cannot inject markup, and a link can only ever point at
 * https or at a path inside this app.
 *
 * Supported: headings, paragraphs, ordered and unordered lists, links, inline
 * code, fenced code blocks, bold and italic.
 */

/** One list entry, with any lists nested underneath it. */
export type MarkdownListItem = { text: string; children: MarkdownList[] };

export type MarkdownList = { ordered: boolean; items: MarkdownListItem[] };

export type MarkdownBlock =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; text: string }
  | ({ kind: "list" } & MarkdownList)
  | { kind: "code"; language: string; code: string };

function isWhitespace(character: string | undefined): boolean {
  return Boolean(character && /\s/u.test(character));
}

function fenceLanguage(line: string): string | null {
  const trimmed = line.trimStart();
  return trimmed.startsWith("```") ? trimmed.slice(3) : null;
}

function headingParts(line: string): { level: number; text: string } | null {
  let level = 0;
  while (level < 6 && line[level] === "#") {
    level += 1;
  }
  if (level === 0 || !isWhitespace(line[level])) {
    return null;
  }
  let textStart = level;
  while (isWhitespace(line[textStart])) {
    textStart += 1;
  }
  return { level, text: line.slice(textStart) };
}

function listItemParts(line: string): { indent: string; ordered: boolean; text: string } | null {
  let markerStart = 0;
  while (isWhitespace(line[markerStart])) {
    markerStart += 1;
  }
  let markerEnd = markerStart;
  let ordered = false;
  if (["-", "*", "+"].includes(line[markerStart] || "")) {
    markerEnd += 1;
  } else {
    while (line[markerEnd] >= "0" && line[markerEnd] <= "9") {
      markerEnd += 1;
    }
    ordered = markerEnd > markerStart && [".", ")"].includes(line[markerEnd] || "");
    if (!ordered) {
      return null;
    }
    markerEnd += 1;
  }
  if (!isWhitespace(line[markerEnd])) {
    return null;
  }
  let textStart = markerEnd;
  while (isWhitespace(line[textStart])) {
    textStart += 1;
  }
  return { indent: line.slice(0, markerStart), ordered, text: line.slice(textStart) };
}

/** Indent width in columns, counting a tab as four. */
function indentWidth(value: string): number {
  return Array.from(value).reduce((total, character) => total + (character === "\t" ? 4 : 1), 0);
}

/** A space or control character inside a URL is how a blocked scheme gets hidden. */
function hasControlOrSpace(value: string): boolean {
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0;
    if (code <= 0x20 || code === 0x7f) {
      return true;
    }
  }
  return false;
}

/**
 * A link may only address https or this app. Anything else — `javascript:`,
 * `data:`, `vbscript:`, a protocol-relative `//host` — is dropped rather than
 * rendered, so a page cannot smuggle a scheme past the renderer.
 */
export function safeMarkdownHref(raw: string): string {
  const href = String(raw ?? "").trim();
  if (!href || hasControlOrSpace(href)) {
    return "";
  }
  if (href.startsWith("//")) {
    return "";
  }
  if (href.startsWith("/")) {
    return href.startsWith("/\\") ? "" : href;
  }
  return /^https:\/\/\S+$/i.test(href) ? href : "";
}

type MarkdownParseState = {
  blocks: MarkdownBlock[];
  paragraph: string[];
  //: The open lists, outermost first. An indented item opens a list under the
  //: item above it; a less-indented one closes back to its own level.
  openLists: Array<{ indent: number; list: MarkdownList }>;
};

function flushMarkdownParagraph(state: MarkdownParseState) {
  if (!state.paragraph.length) return;
  state.blocks.push({ kind: "paragraph", text: state.paragraph.join(" ").trim() });
  state.paragraph = [];
}

function flushMarkdownList(state: MarkdownParseState) {
  const root = state.openLists[0]?.list;
  if (root?.items.length) {
    state.blocks.push({ kind: "list", ordered: root.ordered, items: root.items });
  }
  state.openLists = [];
}

function flushMarkdownState(state: MarkdownParseState) {
  flushMarkdownParagraph(state);
  flushMarkdownList(state);
}

function appendMarkdownCodeBlock(
  state: MarkdownParseState,
  lines: string[],
  openingIndex: number,
  language: string,
): number {
  const code: string[] = [];
  let index = openingIndex + 1;
  while (index < lines.length && fenceLanguage(lines[index]) === null) {
    code.push(lines[index]);
    index += 1;
  }
  state.blocks.push({ kind: "code", language: language.trim(), code: code.join("\n") });
  return index;
}

function startMarkdownList(state: MarkdownParseState, indent: number, ordered: boolean, text: string) {
  state.openLists = [{ indent, list: { ordered, items: [{ text, children: [] }] } }];
}

function appendMarkdownListItem(
  state: MarkdownParseState,
  item: NonNullable<ReturnType<typeof listItemParts>>,
) {
  flushMarkdownParagraph(state);
  const ordered = item.ordered;
  const indent = indentWidth(item.indent);
  const text = item.text.trim();
  while (state.openLists.length > 1 && indent < state.openLists.at(-1)!.indent) {
    state.openLists.pop();
  }
  const current = state.openLists.at(-1);
  if (!current) {
    startMarkdownList(state, indent, ordered, text);
    return;
  }
  if (indent > current.indent) {
    const parent = current.list.items.at(-1);
    if (parent) {
      const nested: MarkdownList = { ordered, items: [{ text, children: [] }] };
      parent.children.push(nested);
      state.openLists.push({ indent, list: nested });
      return;
    }
  }
  if (current.list.ordered === ordered) {
    current.list.items.push({ text, children: [] });
    return;
  }
  if (state.openLists.length === 1) {
    flushMarkdownList(state);
    startMarkdownList(state, indent, ordered, text);
    return;
  }
  const parent = state.openLists.at(-2)!.list.items.at(-1);
  const sibling: MarkdownList = { ordered, items: [{ text, children: [] }] };
  parent?.children.push(sibling);
  state.openLists[state.openLists.length - 1] = { indent, list: sibling };
}

function appendMarkdownListContinuation(state: MarkdownParseState, line: string): boolean {
  if (!state.openLists.length) return false;
  const items = state.openLists.at(-1)!.list.items;
  const previous = items.at(-1);
  if (previous) {
    previous.text = `${previous.text} ${line.trim()}`;
  }
  return true;
}

/** Split Markdown source into blocks. Pure, so it can be tested directly. */
export function parseMarkdownBlocks(content: string): MarkdownBlock[] {
  const lines = String(content ?? "")
    .replace(/\r\n?/g, "\n")
    .split("\n");
  const state: MarkdownParseState = { blocks: [], paragraph: [], openLists: [] };

  let skipUntil = -1;
  for (const [index, line] of lines.entries()) {
    if (index <= skipUntil) {
      continue;
    }
    const language = fenceLanguage(line);
    if (language !== null) {
      flushMarkdownState(state);
      skipUntil = appendMarkdownCodeBlock(state, lines, index, language);
      continue;
    }
    if (!line.trim()) {
      flushMarkdownState(state);
      continue;
    }
    const heading = headingParts(line);
    if (heading) {
      flushMarkdownState(state);
      state.blocks.push({ kind: "heading", level: heading.level, text: heading.text.trim() });
      continue;
    }
    const item = listItemParts(line);
    if (item) {
      appendMarkdownListItem(state, item);
      continue;
    }
    if (appendMarkdownListContinuation(state, line)) {
      continue;
    }
    state.paragraph.push(line.trim());
  }
  flushMarkdownState(state);
  return state.blocks;
}

// Underscore emphasis only applies at a word boundary. Without that,
// `wp_create_post` and `not_officially_documented` are read as emphasis and the
// text is mangled into "wpcreatepost".
const INLINE_PATTERNS = [
  /`[^`]+`/g,
  /\*\*[^*]+\*\*/g,
  /(?<!\w)__[^_]+__(?!\w)/g,
  /\*[^*]+\*/g,
  /(?<!\w)_[^_]+_(?!\w)/g,
];

type InlineMatch = {
  index: number;
  text: string;
};

function findNextMarkdownLink(text: string, cursor: number): InlineMatch | null {
  let openingIndex = text.indexOf("[", cursor);
  while (openingIndex >= 0) {
    const destinationStart = text.indexOf("](", openingIndex + 1);
    if (destinationStart < 0) return null;
    const closingIndex = text.indexOf(")", destinationStart + 2);
    if (closingIndex < 0) return null;
    const destination = text.slice(destinationStart + 2, closingIndex);
    if (!/\s/u.test(destination)) {
      return {
        index: openingIndex,
        text: text.slice(openingIndex, closingIndex + 1),
      };
    }
    openingIndex = text.indexOf("[", openingIndex + 1);
  }
  return null;
}

function markdownLinkParts(segment: string): { destination: string; label: string } | null {
  if (!segment.startsWith("[") || !segment.endsWith(")")) return null;
  const destinationStart = segment.indexOf("](", 1);
  if (destinationStart < 0) return null;
  return {
    label: segment.slice(1, destinationStart),
    destination: segment.slice(destinationStart + 2, -1),
  };
}

function splitInlineMarkdown(text: string): string[] {
  const segments: string[] = [];
  let cursor = 0;
  while (cursor < text.length) {
    let nextMatch: InlineMatch | null = findNextMarkdownLink(text, cursor);
    for (const pattern of INLINE_PATTERNS) {
      pattern.lastIndex = cursor;
      const match = pattern.exec(text);
      if (match && (!nextMatch || match.index < nextMatch.index)) {
        nextMatch = { index: match.index, text: match[0] };
      }
    }
    if (!nextMatch) {
      segments.push(text.slice(cursor));
      break;
    }
    if (nextMatch.index > cursor) {
      segments.push(text.slice(cursor, nextMatch.index));
    }
    segments.push(nextMatch.text);
    cursor = nextMatch.index + nextMatch.text.length;
  }
  return segments;
}

/** Render inline Markdown. Returns React nodes; never HTML. */
export function renderInlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  return splitInlineMarkdown(String(text ?? ""))
    .filter(Boolean)
    .map((segment, index) => {
      const key = `${keyPrefix}:${index}`;
      const code = /^`([^`]+)`$/.exec(segment);
      if (code) {
        return (
          <code
            className="fs-markdown-code"
            key={key}
          >
            {code[1]}
          </code>
        );
      }
      // Emphasis nests: `**a `code` b**` has to render the code span too.
      const strong = /^\*\*([^*]+)\*\*$/.exec(segment) ?? /^__([^_]+)__$/.exec(segment);
      if (strong) {
        return (
          <strong className="fs-markdown-strong" key={key}>
            {renderInlineMarkdown(strong[1], key)}
          </strong>
        );
      }
      const emphasis = /^\*([^*]+)\*$/.exec(segment) ?? /^_([^_]+)_$/.exec(segment);
      if (emphasis) {
        return <em key={key}>{renderInlineMarkdown(emphasis[1], key)}</em>;
      }
      const link = markdownLinkParts(segment);
      if (link) {
        const href = safeMarkdownHref(link.destination);
        const label = link.label || href || link.destination;
        if (href) {
          return (
            <a
              className="fs-link fs-markdown-link"
              href={href}
              key={key}
              rel="noreferrer"
              target="_blank"
            >
              {link.label ? renderInlineMarkdown(link.label, key) : label}
            </a>
          );
        }
        // An unsafe or unsupported URL renders as plain text, never as a link.
        return <span key={key}>{label}</span>;
      }
      return <span key={key}>{segment}</span>;
    });
}

type HeadingTag = "h1" | "h2" | "h3" | "h4" | "h5" | "h6";

/** Render one list and everything nested underneath it. */
function renderMarkdownList(list: MarkdownList, key: string): ReactNode {
  const items = list.items.map((item, itemIndex) => (
    <li className="fs-markdown-item" key={`${key}:${itemIndex}`}>
      {renderInlineMarkdown(item.text, `${key}:${itemIndex}`)}
      {item.children.map((child, childIndex) =>
        renderMarkdownList(child, `${key}:${itemIndex}:${childIndex}`),
      )}
    </li>
  ));
  return list.ordered ? (
    <ol className="fs-markdown-list" key={key}>
      {items}
    </ol>
  ) : (
    <ul className="fs-markdown-list" key={key}>
      {items}
    </ul>
  );
}

function renderBlock(block: MarkdownBlock, index: number): ReactNode {
  const key = `markdown-block:${index}`;
  if (block.kind === "heading") {
    const level = Math.min(Math.max(block.level, 1), 6);
    const Tag = `h${level}` as HeadingTag;
    return (
      <Tag className={`fs-markdown-heading h${level}`} key={key}>
        {renderInlineMarkdown(block.text, key)}
      </Tag>
    );
  }
  if (block.kind === "code") {
    return (
      <pre
        className="fs-markdown-pre"
        data-language={block.language || undefined}
        key={key}
      >
        <code>{block.code}</code>
      </pre>
    );
  }
  if (block.kind === "list") {
    return renderMarkdownList(block, key);
  }
  return (
    <p className="fs-markdown-paragraph" key={key}>
      {renderInlineMarkdown(block.text, key)}
    </p>
  );
}

/** Render a Markdown document as React nodes. */
export function renderMarkdownBlocks(content: string): ReactNode[] {
  return parseMarkdownBlocks(content).map((block, index) => renderBlock(block, index));
}
