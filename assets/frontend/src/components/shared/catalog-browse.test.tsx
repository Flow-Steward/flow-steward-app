import { act, fireEvent, render, screen } from "@testing-library/react";

import catalogStyles from "@/components/shared/catalog-browse.css?raw";
import designSystemStyles from "@/components/shared/design-system.css?raw";

import {
  CatalogByline,
  CatalogChipRow,
  CatalogFacetRail,
  CatalogIcon,
  CatalogItemCard,
  CatalogPager,
} from "@/components/shared/catalog-browse";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { type CatalogFacetView, fittedChipCount } from "@/lib/utils/catalog-facets";

const facet: CatalogFacetView<"category"> = {
  key: "category",
  title: "Categories",
  noun: "kategorijos",
  order: "total",
  label: (value) => value,
  values: [
    { value: "one", label: "One", count: 1 },
    { value: "two", label: "Two", count: 2 },
    { value: "eleven", label: "Eleven", count: 11 },
    { value: "twenty-one", label: "Twenty-one", count: 21 },
    { value: "empty", label: "Empty", count: 0 },
    { value: "three", label: "Three", count: 3 },
    { value: "four", label: "Four", count: 4 },
    { value: "five", label: "Five", count: 5 },
  ],
};

describe("catalog facet locale rendering", () => {
  it("uses whole Lithuanian count labels without changing facet selection codes", () => {
    let selected: [string, string] | undefined;
    render(
      <I18nContext.Provider value={{ ...fallbackContext, locale: "lt" }}>
        <CatalogFacetRail
          facets={[facet]}
          filters={{ q: "", category: ["empty"] }}
          label="Template filters"
          noun={["šablonas", "šablonai"]}
          onToggle={(key, value) => { selected = [key, value]; }}
          renderCountLabel={(count) => `Šablonai: ${count}`}
        />
      </I18nContext.Provider>,
    );
    expect(screen.getByRole("checkbox", { name: /^One.*Šablonai: 1$/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /^Eleven.*Šablonai: 11$/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /^Twenty-one.*Šablonai: 21$/ })).toBeInTheDocument();
    const empty = screen.getByRole("checkbox", { name: /^Empty.*Šablonai: 0$/ });
    expect(empty).toBeChecked();
    expect(empty).toBeEnabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /^Two.*Šablonai: 2$/ }));
    expect(selected).toEqual(["category", "two"]);
  });

  it("renders a whole localized no-match message and preserves the search query", () => {
    const messages = { "react.shared.catalog_no_matching_facets": "Užklausai „{query}“ atitinkančių reikšmių nerasta ({noun})." };
    render(
      <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, fallback = "", params) => interpolate(messages[key as keyof typeof messages] || fallback || key, params) }}>
        <CatalogFacetRail facets={[facet]} filters={{ q: "", category: [] }} label="Template filters" noun={["šablonas", "šablonai"]} onToggle={() => undefined} />
      </I18nContext.Provider>,
    );
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter kategorijos" }), { target: { value: "no-match-42" } });
    expect(screen.getByText("Užklausai „no-match-42“ atitinkančių reikšmių nerasta (kategorijos).")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("retains the English singular and plural default when no renderer is supplied", () => {
    render(<CatalogFacetRail facets={[facet]} filters={{ q: "", category: [] }} label="Template filters" noun={["template", "templates"]} onToggle={() => undefined} />);
    expect(screen.getByRole("checkbox", { name: /^One.*1 template$/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /^Two.*2 templates$/ })).toBeInTheDocument();
  });
});

describe("CatalogIcon, the logo tile of an extension or MCP server", () => {
  it("shows the logo Flow Steward serves, sized for its place", () => {
    render(<CatalogIcon size="row" src="/api/extensions/acme.crm/icon?v=1" />);

    const tile = screen.getByTestId("catalog-icon");
    expect(tile).toHaveClass("fs-catalog-icon-row");
    expect(tile).toHaveAttribute("data-catalog-icon", "image");
    const image = screen.getByRole("presentation");
    expect(image).toHaveAttribute("src", "/api/extensions/acme.crm/icon?v=1");
    expect(image).toHaveAttribute("width", "32");
  });

  it.each([
    ["card", 40],
    ["page", 48],
    ["inline", 20],
  ] as const)("a %s tile is %i px", (size, px) => {
    render(<CatalogIcon size={size} src="/logo.png" />);
    expect(screen.getByTestId("catalog-icon")).toHaveClass(`fs-catalog-icon-${size}`);
    expect(screen.getByRole("presentation")).toHaveAttribute("height", String(px));
  });

  it("shows the Package glyph when there is no logo", () => {
    render(<CatalogIcon />);

    expect(screen.queryByRole("presentation")).not.toBeInTheDocument();
    const tile = screen.getByTestId("catalog-icon");
    expect(tile).toHaveClass("fs-catalog-icon-card");
    expect(tile).toHaveAttribute("data-catalog-icon", "fallback");
    expect(tile).toContainHTML("lucide-package");
  });

  it("falls back to the glyph when the logo fails to load, and tries a new logo again", () => {
    const { rerender } = render(<CatalogIcon src="/broken.png" />);
    fireEvent.error(screen.getByRole("presentation"));

    expect(screen.queryByRole("presentation")).not.toBeInTheDocument();
    expect(screen.getByTestId("catalog-icon")).toHaveAttribute("data-catalog-icon", "fallback");
    expect(screen.getByTestId("catalog-icon")).toContainHTML("lucide-package");

    rerender(<CatalogIcon src="/fixed.png" />);
    expect(screen.getByRole("presentation")).toHaveAttribute("src", "/fixed.png");
  });

  it("takes another fallback, an MCP server's initial", () => {
    render(<CatalogIcon fallback="N" />);
    const tile = screen.getByTestId("catalog-icon");
    expect(tile).toHaveTextContent("N");
    expect(tile).not.toContainHTML("lucide-package");
  });
});

describe("CatalogIcon placeholders, by kind and publisher", () => {
  it("an MCP server without a logo shows two letters of its name", () => {
    const { rerender } = render(<CatalogIcon kind="mcp" name="Ahrefs" />);
    expect(screen.getByTestId("catalog-icon")).toHaveTextContent("Ah");
    rerender(<CatalogIcon kind="mcp" name="ActiveCampaign" />);
    expect(screen.getByTestId("catalog-icon")).toHaveTextContent("AC");
  });

  it("a template shows the flow glyph, an extension without an icon the package, whoever publishes it", () => {
    const { rerender } = render(<CatalogIcon kind="workflow" name="Search a mailbox" />);
    expect(screen.getByTestId("catalog-icon")).toContainHTML("lucide-workflow");
    rerender(<CatalogIcon kind="extension" name="File Transfer" />);
    const tile = screen.getByTestId("catalog-icon");
    expect(tile).toContainHTML("lucide-package");
    // The Flow Mark is never a placeholder.
    expect(tile).not.toContainHTML("fs-catalog-icon-mark");
  });
});

describe("CatalogIcon tile: structure and styles", () => {
  it("draws a logo on a tile marked as an image and a placeholder on one marked as a fallback", () => {
    const { rerender } = render(<CatalogIcon src="/logo.png" />);
    const tile = screen.getByTestId("catalog-icon");
    expect(tile).toHaveClass("fs-catalog-icon", "fs-catalog-icon-card");
    expect(tile).toHaveAttribute("data-catalog-icon", "image");
    expect(tile).toContainElement(screen.getByRole("presentation"));
    expect(tile).not.toContainHTML("fs-catalog-icon-fallback");
    rerender(<CatalogIcon />);
    expect(screen.getByTestId("catalog-icon")).toHaveAttribute("data-catalog-icon", "fallback");
    expect(screen.getByTestId("catalog-icon")).toContainHTML("fs-catalog-icon-fallback");
    expect(screen.queryByRole("presentation")).not.toBeInTheDocument();
  });

  it("is 40px, radius 8, a 1px card border on the muted surface; the glyph is 16px", () => {
    const tile = /^\.fs-catalog-icon \{([^}]*)\}/m.exec(catalogStyles)?.[1] ?? "";
    expect(tile).toMatch(/block-size:\s*40px/);
    expect(tile).toMatch(/inline-size:\s*40px/);
    expect(tile).toMatch(/border-radius:\s*var\(--fs-radius-8\)/);
    expect(tile).toMatch(/border:\s*1px solid var\(--fs-card-border\)/);
    expect(tile).toMatch(/background:\s*var\(--fs-bg-surface-muted\)/);
    expect(tile).toMatch(/color:\s*var\(--fs-text-secondary\)/);
    const glyph = /^\.fs-catalog-icon-fallback svg \{([^}]*)\}/m.exec(catalogStyles)?.[1] ?? "";
    expect(glyph).toMatch(/block-size:\s*16px/);
    expect(glyph).toMatch(/inline-size:\s*16px/);
  });

  it("puts a logo, and only a logo, on a light surface in the dark theme", () => {
    const dark = /\[data-theme="dark"\] \.fs-catalog-icon\[data-catalog-icon="image"\] \{([^}]*)\}/.exec(catalogStyles)?.[1] ?? "";
    expect(dark).toMatch(/background:\s*var\(--fs-bg-inverse\)/);
    // No other dark rule touches the tile, so the placeholder keeps the muted surface.
    expect(catalogStyles.match(/\[data-theme="dark"\] \.fs-catalog-icon/g)).toHaveLength(1);
    expect(catalogStyles).not.toContain("fs-catalog-icon-mark");
  });
});

describe("CatalogChipRow: whole chips only, then +N", () => {
  it("fits the row's 220px with whole pills and names the rest on +N", () => {
    render(<CatalogChipRow labels={["Agent tools", "Dataset connectors", "Workflow actions", "Data queries"]} />);

    expect(screen.getByText("Agent tools")).toBeInTheDocument();
    expect(screen.queryByText("Data queries")).not.toBeInTheDocument();
    const more = screen.getByRole("button", { name: /more: .*Data queries$/ });
    expect(more).toHaveTextContent(/^\+\d$/);
    fireEvent.focus(more);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Data queries");
    fireEvent.keyDown(more, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("shows everything, with no +N, when it fits or when the row wraps", () => {
    const { rerender } = render(<CatalogChipRow labels={["Gmail"]} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    rerender(<CatalogChipRow labels={["Agent tools", "Dataset connectors", "Workflow actions", "Data queries"]} wrap />);
    expect(screen.getByText("Data queries")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("counts whole pills against the width it is given", () => {
    const labels = ["Agent tools", "Workflow actions"];
    expect(fittedChipCount(labels, 400)).toBe(2);
    expect(fittedChipCount(labels, 120)).toBe(1);
    expect(fittedChipCount(labels, 40)).toBe(0);
  });
});

describe("The +N overflow pill and its tooltip", () => {
  const labels = ["Agent tools", "Dataset connectors", "Workflow actions", "Data queries"];
  const hidden = labels.slice(fittedChipCount(labels, 0));
  const more = () => screen.getByRole("button", { name: /more:/ });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("is a button that names every hidden chip, and the tooltip lists them comma-separated", () => {
    render(<CatalogChipRow labels={labels} />);
    expect(more()).toHaveAttribute("type", "button");
    expect(more()).toHaveAttribute("aria-label", `${hidden.length} more: ${hidden.join(", ")}`);
    expect(more()).toHaveTextContent(`+${hidden.length}`);
    fireEvent.focus(more());
    expect(screen.getByRole("tooltip")).toHaveTextContent(hidden.join(", "));
    expect(more()).toHaveAttribute("aria-describedby", screen.getByRole("tooltip").id);
  });

  it("opens only after 500ms under the pointer, and closes the moment the pointer leaves", () => {
    vi.useFakeTimers();
    render(<CatalogChipRow labels={labels} />);
    fireEvent.mouseEnter(more());
    act(() => {
      vi.advanceTimersByTime(499);
    });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    fireEvent.mouseLeave(more());
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    // Leaving before the delay cancels it.
    fireEvent.mouseEnter(more());
    act(() => {
      vi.advanceTimersByTime(300);
    });
    fireEvent.mouseLeave(more());
    act(() => {
      vi.advanceTimersByTime(600);
    });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("opens at once on keyboard focus and closes on blur and on Escape", () => {
    render(<CatalogChipRow labels={labels} />);
    fireEvent.focus(more());
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    fireEvent.blur(more());
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.focus(more());
    fireEvent.keyDown(more(), { key: "Escape" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("does nothing on a click: a press does not open it at once, and the card never sees the click", () => {
    const outer = vi.fn();
    render(
      <div onClick={outer} role="presentation">
        <CatalogChipRow labels={labels} />
      </div>,
    );
    fireEvent.mouseDown(more());
    fireEvent.focus(more());
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.click(more());
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(outer).not.toHaveBeenCalled();
  });

  it("keeps Escape for the tooltip while it is open, so a dialog around the catalog stays open", () => {
    const outer = vi.fn();
    render(
      <div onKeyDown={outer} role="presentation">
        <CatalogChipRow labels={labels} />
      </div>,
    );
    fireEvent.keyDown(more(), { key: "Escape" });
    expect(outer).toHaveBeenCalledTimes(1);
    fireEvent.focus(more());
    fireEvent.keyDown(more(), { key: "Escape" });
    expect(outer).toHaveBeenCalledTimes(1);
  });

  describe("placement", () => {
    const rect = (left: number, top: number, width: number, height: number) =>
      ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) }) as DOMRect;
    const place = (anchorTop: number) =>
      vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function mocked(this: HTMLElement) {
        if (this.classList.contains("fs-catalog-card")) return rect(100, 0, 180, 220);
        if (this.classList.contains("fs-catalog-chip-tip")) return rect(0, 0, 120, 30);
        if (this.classList.contains("fs-catalog-chip-more-wrap")) return rect(250, anchorTop, 28, 20);
        return rect(0, 0, 0, 0);
      });
    const card = (
      <CatalogItemCard
        category="Developer tools"
        chips={labels}
        icon={<CatalogIcon kind="mcp" name="Axiom" />}
        id="card"
        open={{ onOpen: vi.fn() }}
        summary="Query Axiom logs."
        title="Axiom"
      />
    );

    it("is at most 240px and never wider than the card it sits in", () => {
      place(100);
      render(card);
      fireEvent.focus(more());
      // The card is 180px wide: narrower than the tooltip's 240px token.
      expect(screen.getByRole("tooltip")).toHaveStyle({ maxWidth: "180px" });
    });

    it("stays inside the card's right edge, and sits above the pill", () => {
      place(100);
      render(card);
      fireEvent.focus(more());
      const tip = screen.getByRole("tooltip");
      // Centred on the pill it would start at 204; the card ends at 280, the tooltip is 120 wide.
      expect(tip).toHaveStyle({ left: `${280 - 120 - 250}px` });
      expect(tip).not.toHaveStyle({ bottom: "auto" });
    });

    it("flips below the pill when there is no room above", () => {
      place(10);
      render(card);
      fireEvent.focus(more());
      const tip = screen.getByRole("tooltip");
      expect(tip).toHaveStyle({ bottom: "auto", top: "calc(100% + 8px)" });
    });
  });

  it("is styled to the design: tooltip tokens, 240px at most, the hover background on hover and focus", () => {
    const tip = /\.fs-catalog-chip-tip \{([^}]*)\}/.exec(catalogStyles)?.[1] ?? "";
    expect(tip).toMatch(/max-width:\s*var\(--fs-tooltip-max-width, 240px\)/);
    expect(tip).toMatch(/padding:\s*var\(--fs-tooltip-padding\)/);
    expect(tip).toMatch(/border-radius:\s*var\(--fs-tooltip-radius\)/);
    expect(tip).toMatch(/background:\s*var\(--fs-tooltip-bg\)/);
    expect(tip).toMatch(/color:\s*var\(--fs-tooltip-text\)/);
    expect(tip).toMatch(/font-size:\s*var\(--fs-tooltip-font-size\)/);
    expect(tip).toMatch(/bottom:\s*calc\(100% \+ var\(--fs-tooltip-offset, 8px\)\)/);
    expect(catalogStyles).toMatch(
      /--fs-badge-overflow-hover-bg:\s*color-mix\(in srgb, var\(--fs-bg-surface-muted\) 94%, var\(--fs-text-primary\)\)/,
    );
    expect(catalogStyles).toMatch(
      /\.fs-catalog-chip-more\.fs-badge:hover,\s*\.fs-catalog-chip-more\.fs-badge:focus-visible \{[^}]*var\(--fs-badge-overflow-hover-bg\)/,
    );
    // Whole pills only: no rule truncates a chip with an ellipsis.
    expect(/\.fs-catalog-chip[^{]*\{[^}]*text-overflow:\s*ellipsis/.test(catalogStyles)).toBe(false);
  });
});

describe("The New badge", () => {
  it("is the Iridescent pill: a gradient edge, the surface veil, primary ink", () => {
    const rule = /\.fs-badge\.new \{([^}]*)\}/.exec(designSystemStyles)?.[1] ?? "";
    expect(rule).toMatch(/border:\s*1px solid transparent/);
    expect(rule).toMatch(/linear-gradient\(var\(--fs-badge-new-veil\), var\(--fs-badge-new-veil\)\) padding-box/);
    expect(rule).toMatch(/var\(--fs-badge-new-gradient\) padding-box/);
    expect(rule).toMatch(/var\(--fs-badge-new-gradient\) border-box/);
    expect(rule).toMatch(/color:\s*var\(--fs-badge-new-text\)/);
  });

  it("is drawn with the new tone, beside the state pills", () => {
    render(<CatalogItemCard {...{ id: "c", icon: null, title: "Axiom", summary: "s", chips: [], category: "x", open: { onOpen: vi.fn() } }} statuses={[{ tone: "new", label: "New" }]} />);
    expect(screen.getByText("New")).toHaveClass("fs-badge", "new");
  });
});

describe("CatalogByline", () => {
  it("reads by, Official, Community, Verified, a trigger and a version as the design writes them", () => {
    const { rerender, container } = render(<CatalogByline publisher="Ahrefs" trust="official" />);
    expect(container).toHaveTextContent("by Ahrefs·Official");
    rerender(<CatalogByline trust="community" />);
    expect(container).toHaveTextContent(/^Community$/);
    rerender(<CatalogByline publisher="Flow-Steward" trust="verified" version="v1.0.0" />);
    // The Verified pill carries its own edge: no dot before it.
    expect(container).toHaveTextContent("by Flow-StewardVerified·v1.0.0");
    expect(screen.getByText("Verified")).toHaveClass("fs-badge", "green");
    rerender(<CatalogByline pills={[{ tone: "orange", label: "Needs extension", icon: "alert" }]} trigger={<span>Schedule</span>} version="v1.0.0" />);
    expect(container).toHaveTextContent("Schedule·v1.0.0Needs extension");
  });
});

describe("CatalogItemCard", () => {
  const base = {
    id: "card",
    icon: <CatalogIcon kind="mcp" name="Axiom" />,
    title: "Axiom",
    summary: "Query Axiom logs.",
    chips: [],
    category: "Developer tools",
    open: { onOpen: vi.fn() },
  };

  it("is an article named by its title, whose one control opens the item", () => {
    const onOpen = vi.fn();
    render(<CatalogItemCard {...base} open={{ onOpen }} version="v1.0.0" />);
    const card = screen.getByRole("article", { name: "Axiom" });
    const open = screen.getByRole("button", { name: "Axiom" });
    expect(open).toHaveTextContent("Axiomv1.0.0");
    fireEvent.click(open);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(card).toHaveTextContent(/Query Axiom logs\.Developer tools$/);
  });

  it("without an action the category stays alone; a state takes the action's place", () => {
    const { rerender } = render(<CatalogItemCard {...base} />);
    // Only the name is operable; the card ends in its category.
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("article", { name: "Axiom" })).toHaveTextContent(/Developer tools$/);
    rerender(
      <CatalogItemCard {...base} action={<button type="button">Install</button>} state={{ tone: "green", label: "Installed", icon: "check" }} />,
    );
    expect(screen.queryByRole("button", { name: "Install" })).not.toBeInTheDocument();
    expect(screen.getByText("Installed")).toHaveClass("fs-badge", "green");
    rerender(<CatalogItemCard {...base} action={<button type="button">Install</button>} />);
    expect(screen.getByRole("article", { name: "Axiom" })).toHaveTextContent(/Developer toolsInstall$/);
  });

  it("puts state pills on the right of the chip row", () => {
    render(<CatalogItemCard {...base} chips={["Gmail"]} statuses={[{ tone: "neutral", label: "Preview" }]} />);
    expect(screen.getByText("Preview")).toHaveClass("fs-badge");
    expect(screen.getByRole("article", { name: "Axiom" })).toHaveTextContent(/GmailPreviewDeveloper tools$/);
  });
});

describe("CatalogPager", () => {
  it("is drawn only past one page, as the range and the arrows", () => {
    const onPage = vi.fn();
    const { rerender } = render(<CatalogPager label="Pages" onPage={onPage} page={1} perPage={24} total={20} />);
    expect(screen.queryByRole("navigation", { name: "Pages" })).not.toBeInTheDocument();
    rerender(<CatalogPager label="Pages" onPage={onPage} page={1} perPage={24} total={30} />);
    expect(screen.getByText("1–24 of 30")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Page 2" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(onPage).toHaveBeenCalledWith(2);
  });

  it("offers Rows per page in the table, 25 or 50", () => {
    render(<CatalogPager label="Pages" onPage={vi.fn()} onPerPage={vi.fn()} page={1} perPage={50} total={40} />);
    expect(screen.getByRole("combobox", { name: "Rows per page" })).toBeInTheDocument();
    expect(screen.getByText("1–40 of 40")).toBeInTheDocument();
  });
});
