import { render, screen } from "@testing-library/react";
import extensionStyles from "@/pages/extensions/extensions.css?raw";

import {
  parseMarkdownBlocks,
  renderMarkdownBlocks,
  safeMarkdownHref,
} from "@/pages/extensions/extension-markdown";

function renderMarkdown(source: string) {
  return render(<div data-testid="markdown">{renderMarkdownBlocks(source)}</div>);
}

function markdownRoot(): HTMLElement {
  return screen.getByTestId("markdown");
}

function item(text: string) {
  return { text, children: [] };
}

describe("parseMarkdownBlocks", () => {
  it("reads each heading level", () => {
    const blocks = parseMarkdownBlocks("# One\n\n## Two\n\n###### Six");
    expect(blocks).toEqual([
      { kind: "heading", level: 1, text: "One" },
      { kind: "heading", level: 2, text: "Two" },
      { kind: "heading", level: 6, text: "Six" },
    ]);
  });

  it("joins the wrapped lines of one paragraph", () => {
    expect(parseMarkdownBlocks("first line\nsecond line\n\nnext")).toEqual([
      { kind: "paragraph", text: "first line second line" },
      { kind: "paragraph", text: "next" },
    ]);
  });

  it("reads unordered and ordered lists as separate blocks", () => {
    const blocks = parseMarkdownBlocks("- one\n- two\n\n1. first\n2. second");
    expect(blocks).toEqual([
      { kind: "list", ordered: false, items: [item("one"), item("two")] },
      { kind: "list", ordered: true, items: [item("first"), item("second")] },
    ]);
  });

  it("keeps an indented list under the item it belongs to", () => {
    expect(parseMarkdownBlocks("- one\n  - nested\n  - also nested\n- two")).toEqual([
      {
        kind: "list",
        ordered: false,
        items: [
          {
            text: "one",
            children: [
              { ordered: false, items: [item("nested"), item("also nested")] },
            ],
          },
          item("two"),
        ],
      },
    ]);
  });

  it("nests an ordered list under an unordered item", () => {
    expect(parseMarkdownBlocks("- steps\n  1. first\n  2. second")).toEqual([
      {
        kind: "list",
        ordered: false,
        items: [
          {
            text: "steps",
            children: [{ ordered: true, items: [item("first"), item("second")] }],
          },
        ],
      },
    ]);
  });

  it("returns to the outer list when the indentation goes back", () => {
    expect(parseMarkdownBlocks("- one\n  - nested\n- two")).toEqual([
      {
        kind: "list",
        ordered: false,
        items: [
          { text: "one", children: [{ ordered: false, items: [item("nested")] }] },
          item("two"),
        ],
      },
    ]);
  });

  it("nests three levels deep", () => {
    const blocks = parseMarkdownBlocks("- a\n  - b\n    - c");
    const top = blocks[0] as { items: Array<{ text: string; children: unknown[] }> };
    const second = top.items[0].children[0] as { items: Array<{ children: unknown[] }> };
    const third = second.items[0].children[0] as { items: Array<{ text: string }> };
    expect(third.items[0].text).toBe("c");
  });

  it("keeps a wrapped list item with its own bullet", () => {
    expect(parseMarkdownBlocks("- one that\ncontinues\n- two")).toEqual([
      { kind: "list", ordered: false, items: [item("one that continues"), item("two")] },
    ]);
  });

  it("reads a fenced code block and its language", () => {
    const blocks = parseMarkdownBlocks("```bash\nnpm run test\nsecond\n```");
    expect(blocks).toEqual([
      { kind: "code", language: "bash", code: "npm run test\nsecond" },
    ]);
  });

  it("does not treat markdown inside a fence as markdown", () => {
    const blocks = parseMarkdownBlocks("```\n# not a heading\n- not a list\n```");
    expect(blocks).toEqual([
      { kind: "code", language: "", code: "# not a heading\n- not a list" },
    ]);
  });

  it("tolerates an unterminated fence", () => {
    expect(parseMarkdownBlocks("```\nstill code")).toEqual([
      { kind: "code", language: "", code: "still code" },
    ]);
  });

  it("returns nothing for empty input", () => {
    expect(parseMarkdownBlocks("")).toEqual([]);
    expect(parseMarkdownBlocks("   \n\n  ")).toEqual([]);
  });
});

describe("safeMarkdownHref", () => {
  it("accepts https and app-relative links", () => {
    expect(safeMarkdownHref("https://developer.example.com/rest-api/")).toBe(
      "https://developer.example.com/rest-api/",
    );
    expect(safeMarkdownHref("/projects/1")).toBe("/projects/1");
  });

  it.each([
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "  javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "http://example.com",
    "//evil.example.com",
    "/\\evil.example.com",
    "",
    "   ",
  ])("refuses %s", (href) => {
    expect(safeMarkdownHref(href)).toBe("");
  });

  it("refuses a scheme hidden behind a control character", () => {
    expect(safeMarkdownHref("java\u0000script:alert(1)")).toBe("");
    expect(safeMarkdownHref("java\tscript:alert(1)")).toBe("");
    expect(safeMarkdownHref("java\nscript:alert(1)")).toBe("");
  });
});

/*
 * The renderer's contract *is* the DOM it produces: which elements appear, how
 * they nest, and — for the XSS cases — which elements must not exist at all.
 * That cannot be expressed through role and text queries alone.
 */
/* eslint-disable testing-library/no-node-access -- asserting the produced element tree is the point of these tests. */
describe("renderMarkdownBlocks", () => {
  it("renders headings as heading elements", () => {
    renderMarkdown("# Setup guide\n\n## Acme CMS");
    expect(screen.getByRole("heading", { level: 1, name: "Setup guide" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Acme CMS" })).toBeInTheDocument();
  });

  it("renders an unordered list as list items", () => {
    renderMarkdown("- first\n- second");
    const items = screen.getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual(["first", "second"]);
    expect(markdownRoot().querySelector("ul")).not.toBeNull();
    expect(markdownRoot().querySelector("ul")).toHaveClass("fs-markdown-list");
    expect(extensionStyles).toMatch(/ul\.fs-markdown-list\s*\{[^}]*list-style-type:\s*disc;/s);
  });

  it("renders an ordered list as an ordered list", () => {
    renderMarkdown("1. first\n2. second");
    expect(markdownRoot().querySelector("ol")).not.toBeNull();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(markdownRoot().querySelector("ol")).toHaveClass("fs-markdown-list");
    expect(extensionStyles).toMatch(/ol\.fs-markdown-list\s*\{[^}]*list-style-type:\s*decimal;/s);
  });

  it("renders bold and italic", () => {
    renderMarkdown("**bold** and *italic* and __also bold__ and _also italic_");
    expect(markdownRoot().querySelectorAll("strong")).toHaveLength(2);
    expect(markdownRoot().querySelectorAll("em")).toHaveLength(2);
  });

  it("renders inline code", () => {
    renderMarkdown("Use `GET /wp/v2/posts` for this.");
    const code = markdownRoot().querySelector("code");
    expect(code?.textContent).toBe("GET /wp/v2/posts");
  });

  it("renders a fenced code block verbatim", () => {
    renderMarkdown("```json\n{\n  \"a\": 1\n}\n```");
    const pre = markdownRoot().querySelector("pre");
    expect(pre?.textContent).toBe('{\n  "a": 1\n}');
    expect(pre?.getAttribute("data-language")).toBe("json");
  });

  it("renders a safe link that opens away from the app", () => {
    renderMarkdown("[Acme CMS](https://developer.example.com/rest-api/)");
    const link = screen.getByRole("link", { name: "Acme CMS" });
    expect(link).toHaveAttribute("href", "https://developer.example.com/rest-api/");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("renders an unsafe link as text and never as a link", () => {
    renderMarkdown("[click me](javascript:alert(1))");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(markdownRoot()).toHaveTextContent("click me");
  });

  it("never produces markup from raw HTML in the source", () => {
    renderMarkdown('<script>alert(1)</script><img src=x onerror="alert(1)">');
    expect(markdownRoot().querySelector("script")).toBeNull();
    expect(markdownRoot().querySelector("img")).toBeNull();
    expect(markdownRoot()).toHaveTextContent("<script>alert(1)</script>");
  });

  it("never produces markup from raw HTML inside a heading or list", () => {
    renderMarkdown("# <b>title</b>\n\n- <i>item</i>");
    expect(markdownRoot().querySelector("b")).toBeNull();
    expect(markdownRoot().querySelector("i")).toBeNull();
    expect(markdownRoot()).toHaveTextContent("<b>title</b>");
  });

  it("does not execute an event handler smuggled into a link label", () => {
    renderMarkdown('[<img src=x onerror=alert(1)>](https://example.com/safe)');
    expect(markdownRoot().querySelector("img")).toBeNull();
    expect(screen.getByRole("link")).toHaveAttribute("href", "https://example.com/safe");
  });

  it("renders a whole document with every supported element", () => {
    renderMarkdown(
      [
        "# Title",
        "",
        "A paragraph with **bold**, *italic*, `code` and a",
        "[link](https://example.com/docs).",
        "",
        "## Steps",
        "",
        "1. First step",
        "2. Second step",
        "",
        "- A bullet",
        "- Another bullet",
        "",
        "```bash",
        "flow-steward extensions validate",
        "```",
      ].join("\n"),
    );
    const root = markdownRoot();
    expect(screen.getByRole("heading", { level: 1, name: "Title" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Steps" })).toBeInTheDocument();
    expect(root.querySelectorAll("ol")).toHaveLength(1);
    expect(root.querySelectorAll("ul")).toHaveLength(1);
    expect(root.querySelectorAll("pre")).toHaveLength(1);
    expect(root.querySelectorAll("strong")).toHaveLength(1);
    expect(root.querySelectorAll("em")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "link" })).toBeInTheDocument();
  });

  it("leaves snake_case identifiers intact", () => {
    // Browser check caught this: `Global_styles, Global_styles_revisions` was
    // read as emphasis and rendered as "Globalstyles".
    renderMarkdown("Affects: Global_styles, Global_styles_revisions, Menu_items.");
    expect(markdownRoot().querySelectorAll("em")).toHaveLength(0);
    expect(markdownRoot()).toHaveTextContent(
      "Affects: Global_styles, Global_styles_revisions, Menu_items.",
    );
  });

  it("still renders underscore emphasis at a word boundary", () => {
    renderMarkdown("a _real_ emphasis and __real bold__ here");
    expect(markdownRoot().querySelector("em")?.textContent).toBe("real");
    expect(markdownRoot().querySelector("strong")?.textContent).toBe("real bold");
  });

  it("renders inline code inside bold", () => {
    // Browser check caught this too: the backticks stayed literal.
    renderMarkdown("**The official `/wp/v2` REST API** is used.");
    const strong = markdownRoot().querySelector("strong");
    expect(strong?.querySelector("code")?.textContent).toBe("/wp/v2");
    expect(markdownRoot()).not.toHaveTextContent("`");
  });

  it("renders a link inside bold and emphasis inside a link label", () => {
    renderMarkdown("**see [docs](https://example.com/d)** and [*that*](https://example.com/e)");
    expect(markdownRoot().querySelector("strong a")).not.toBeNull();
    expect(markdownRoot().querySelector("a em")?.textContent).toBe("that");
  });

  it("does not treat code span contents as markdown", () => {
    renderMarkdown("`**not bold** and _not italic_`");
    const code = markdownRoot().querySelector("code");
    expect(code?.textContent).toBe("**not bold** and _not italic_");
    expect(markdownRoot().querySelector("strong")).toBeNull();
    expect(markdownRoot().querySelector("em")).toBeNull();
  });

  it("renders a nested list inside its parent item", () => {
    renderMarkdown("- one\n  - nested\n- two");
    const root = markdownRoot();
    const topLevel = root.querySelector("ul");
    expect(topLevel?.children).toHaveLength(2);
    const firstItem = topLevel?.children[0] as HTMLElement;
    expect(firstItem.querySelector("ul")?.textContent).toBe("nested");
    // The nested entry must not become a sibling of the items around it.
    expect(topLevel?.children[1].textContent).toBe("two");
  });

  it("renders an ordered list nested under an unordered item", () => {
    renderMarkdown("- steps\n  1. first\n  2. second");
    const nested = markdownRoot().querySelector("ul > li > ol");
    expect(nested?.children).toHaveLength(2);
    expect(nested?.textContent).toBe("firstsecond");
  });

  it("renders nothing for empty content", () => {
    renderMarkdown("");
    expect(markdownRoot().childElementCount).toBe(0);
  });
});
/* eslint-enable testing-library/no-node-access */
