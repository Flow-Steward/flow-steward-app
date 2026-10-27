import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import {
  fetchExtensionCatalog,
  type ExtensionCatalogEntry,
  type ExtensionCatalogInstallState,
  type ExtensionCatalogListing,
} from "@/pages/extensions/catalog-api";
import { ExtensionMarketplaceCatalog } from "@/pages/extensions/marketplace-catalog";
import { fetchInstallationVersion } from "@/pages/workflows/catalog/api";

vi.mock("@/pages/extensions/catalog-api", async (importOriginal) => {
  const original = (await importOriginal()) as Record<string, unknown>;
  return { ...original, fetchExtensionCatalog: vi.fn() };
});

vi.mock("@/pages/workflows/catalog/api", () => ({ fetchInstallationVersion: vi.fn() }));

const mockedFetchCatalog = vi.mocked(fetchExtensionCatalog);
const mockedVersion = vi.mocked(fetchInstallationVersion);

function entry(overrides: Partial<ExtensionCatalogEntry> & { extension_id: string }): ExtensionCatalogEntry {
  return {
    version: "1.0.0",
    name: overrides.extension_id,
    summary: `Summary of ${overrides.extension_id}.`,
    description: "",
    categories: ["crm"],
    capabilities: ["tool"],
    repository_url: `https://github.example.test/${overrides.extension_id}`,
    release_zip_url: `https://catalog.example.test/${overrides.extension_id}.zip`,
    release_zip_bytes: 65_536,
    release_zip_sha256: "a".repeat(64),
    permissions_summary: ["extension:invoke"],
    setup_summary: "",
    publication_status: "published",
    verification_status: "verified",
    has_icon: true,
    icon_url: `/api/extensions/catalog/${overrides.extension_id}/icon?v=abc`,
    display_state: "install",
    compatible: true,
    installed_version: "",
    installed_install_id: "",
    installed_state: "",
    ...overrides,
  };
}

/** Made-up extensions: Core names no real one. */
const CRM = entry({
  extension_id: "acme.crm",
  name: "Acme CRM",
  summary: "Sync contacts with Acme.",
  description: "Keeps contacts in step.\n\nRuns on a schedule.",
  categories: ["crm"],
  capabilities: ["tool", "action", "query", "notify"],
  permissions_summary: ["extension:invoke", "artifact:read"],
  setup_summary: "Create one Acme connection first.",
  tags: ["contacts"],
  added_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-10-01T08:00:00Z",
  compatible_flow_steward: { min: "0.5.0" },
});
const MAIL = entry({
  extension_id: "acme.mail",
  name: "Acme Mail",
  categories: ["messaging"],
  capabilities: ["action"],
  display_state: "installed",
  installed_version: "1.0.0",
  added_at: "2026-09-02T00:00:00Z",
});
const NATIVE = entry({
  extension_id: "acme.native",
  name: "Acme Native",
  categories: ["data"],
  capabilities: ["query"],
  display_state: "incompatible",
  compatible: false,
  available_for_host: false,
  runtime_targets: ["linux-amd64"],
  added_at: "2026-09-03T00:00:00Z",
});

function listing(items: ExtensionCatalogEntry[], overrides: Partial<ExtensionCatalogListing> = {}): ExtensionCatalogListing {
  return {
    kind: "extension_catalog_listing",
    availability: {
      kind: "extension_catalog_availability",
      configured: true,
      available: true,
      item_count: items.length,
      stale: false,
    },
    catalog_revision: "r1",
    schema_version: "extension_catalog/v1",
    category_labels: { crm: "CRM", messaging: "Messaging", data: "Data" },
    capability_labels: { tool: "Agent tools", action: "Workflow actions", query: "Data queries", notify: "Notifications" },
    permission_labels: { "extension:invoke": "Run its own operations", "artifact:read": "Read artifacts" },
    items,
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.search}${location.hash}`}</output>;
}

type RenderOptions = {
  url?: string;
  canAdminister?: boolean;
  installBlockedReason?: string;
  installStateById?: Record<string, ExtensionCatalogInstallState>;
  onInstall?: (entry: ExtensionCatalogEntry) => void;
};

function renderMarketplace({
  url = "/extensions#marketplace",
  canAdminister = true,
  installBlockedReason = "",
  installStateById = {},
  onInstall = vi.fn(),
}: RenderOptions = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route
            element={
              <>
                <ExtensionMarketplaceCatalog
                  active
                  canAdminister={canAdminister}
                  installBlockedReason={installBlockedReason}
                  installStateById={installStateById}
                  onInstall={onInstall}
                />
                <LocationProbe />
              </>
            }
            path="/extensions"
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { onInstall };
}

const currentUrl = () => screen.getByTestId("location").textContent ?? "";

beforeEach(() => {
  vi.clearAllMocks();
  mockedVersion.mockResolvedValue("0.6.0");
});

test("each card names one link, with its chips, its state and the reason", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
  renderMarketplace();

  const card = await screen.findByRole("article", { name: "Acme CRM" });
  const crm = within(card).getByRole("link", { name: "Acme CRM" });
  expect(crm).toHaveAttribute("href", "/extensions?mk_entry=acme.crm#marketplace");
  expect(crm).toHaveAccessibleDescription(
    "Sync contacts with Acme. Category: CRM. Capabilities: Agent tools, Workflow actions, Data queries, Notifications. Not installed.",
  );
  // The link holds the name and the version only; the action sits beside it.
  expect(within(crm).queryByRole("button")).not.toBeInTheDocument();
  expect(within(crm).getByText("v1.0.0")).toBeInTheDocument();
  // Whole chips only, as many as the row holds, then "+N" naming the rest.
  expect(within(card).getByText("Agent tools")).toBeInTheDocument();
  const more = within(card).getByRole("button", { name: "3 more: Workflow actions, Data queries, Notifications" });
  expect(more).toHaveTextContent("+3");
  fireEvent.focus(more);
  expect(within(card).getByRole("tooltip")).toHaveTextContent("Workflow actions, Data queries, Notifications");
  fireEvent.blur(more);
  expect(within(card).queryByRole("tooltip")).not.toBeInTheDocument();
  // "by github.example.test" is no repository owner; a verified one is a pill.
  expect(within(card).getByText("CRM")).toBeInTheDocument();

  // The state takes the button's place.
  const mail = screen.getByRole("article", { name: "Acme Mail" });
  expect(within(mail).getByText("Installed")).toHaveClass("fs-badge", "green");
  expect(within(mail).queryByRole("button", { name: /^Install/ })).not.toBeInTheDocument();
  const native = screen.getByRole("article", { name: "Acme Native" });
  expect(within(native).getByText("Incompatible")).toHaveClass("fs-badge", "danger");
  expect(within(native).getByText("No package for this server.")).toBeInTheDocument();
  expect(within(native).queryByRole("button", { name: /^Install/ })).not.toBeInTheDocument();
});

test("the rail narrows with OR inside and AND between, and every change lives in the URL", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
  renderMarketplace();
  await screen.findByRole("heading", { name: "3 extensions" });

  const rail = screen.getByRole("navigation", { name: "Extension filters" });
  const capability = within(rail).getByRole("group", { name: "Capability" });
  fireEvent.click(within(capability).getByRole("checkbox", { name: /Workflow actions/ }));
  expect(await screen.findByRole("heading", { name: "2 extensions" })).toBeInTheDocument();
  const status = within(rail).getByRole("group", { name: "Status" });
  fireEvent.click(within(status).getByRole("checkbox", { name: /^Installed/ }));
  expect(await screen.findByRole("heading", { name: "1 extension" })).toBeInTheDocument();
  expect(currentUrl()).toBe("?mk_capability=action&mk_status=installed#marketplace");
  expect(screen.getByText("Capability · Workflow actions")).toBeInTheDocument();

  fireEvent.change(screen.getByRole("searchbox", { name: "Search extension catalog" }), { target: { value: "mail" } });
  fireEvent.click(screen.getByRole("button", { name: "Clear all" }));
  // Clear all leaves the search alone.
  await waitFor(() => expect(currentUrl()).toBe("?mk_q=mail#marketplace"));
});

test("two quick ticks in one turn keep both values", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
  renderMarketplace();
  await screen.findByRole("heading", { name: "3 extensions" });

  const category = screen.getByRole("group", { name: "Category" });
  const crm = within(category).getByRole("checkbox", { name: /CRM/ });
  const data = within(category).getByRole("checkbox", { name: /Data/ });
  // Both clicks land before React renders again, as two quick clicks in a
  // browser can: the second must start from the first, not the render's snapshot.
  act(() => {
    crm.click();
    data.click();
  });
  await waitFor(() => expect(currentUrl()).toBe("?mk_category=crm%2Cdata#marketplace"));
  expect(await screen.findByRole("heading", { name: "2 extensions" })).toBeInTheDocument();
});

test("the URL restores search, filters, sort, view and page, and search matches tags", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
  renderMarketplace({ url: "/extensions?mk_q=CONTACTS&mk_view=list&mk_sort=added#marketplace" });

  expect(await screen.findByRole("heading", { name: "1 extension" })).toBeInTheDocument();
  expect(screen.getByRole("searchbox", { name: "Search extension catalog" })).toHaveValue("CONTACTS");
  expect(screen.getByRole("button", { name: "Table" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("combobox", { name: "Sort" })).toHaveTextContent("Recently added");
  const table = screen.getByRole("table", { name: "Extensions" });
  expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent || cell.getAttribute("aria-label"))).toEqual([
    "Extension",
    "Status",
    "Category",
    "Capabilities",
    "Install",
  ]);
  expect(within(table).getByText("Not installed")).toBeInTheDocument();
});

test("the list view is one link per row and a click anywhere on the row opens it", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
  renderMarketplace({ url: "/extensions?mk_view=list#marketplace" });
  const table = await screen.findByRole("table", { name: "Extensions" });

  const rows = within(table).getAllByRole("row").slice(1);
  // The keyboard meets the link once, not the row and then the link.
  for (const row of rows) expect(row).not.toHaveAttribute("tabindex");
  expect(within(rows[2]).getByText("No package for this server.")).toBeInTheDocument();

  fireEvent.click(within(rows[1]).getByText("Messaging"));
  expect(await screen.findByRole("heading", { level: 2, name: /Acme Mail/ })).toBeInTheDocument();
  expect(currentUrl()).toBe("?mk_view=list&mk_entry=acme.mail#marketplace");
});

test("the count is announced after typing settles", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
  renderMarketplace();
  await screen.findByRole("heading", { name: "3 extensions" });

  fireEvent.change(screen.getByRole("searchbox", { name: "Search extension catalog" }), { target: { value: "acme m" } });
  // The heading changes at once; its live copy waits for typing to settle.
  expect(await screen.findByRole("heading", { name: "1 extension" })).toBeInTheDocument();
  expect(screen.getAllByText("1 extension")).toHaveLength(1);
  await waitFor(() => expect(screen.getAllByText("1 extension")).toHaveLength(2), { timeout: 1500 });
});

test("one extension has nothing to sort, and keeps the filter rail", async () => {
  // The rail stays so every catalog has one layout, however few entries.
  mockedFetchCatalog.mockResolvedValue(listing([entry({ extension_id: "acme.solo" })]));
  renderMarketplace();

  expect(await screen.findByRole("heading", { name: "1 extension" })).toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "Sort" })).not.toBeInTheDocument();
  expect(screen.getByRole("navigation", { name: "Extension filters" })).toBeInTheDocument();
});

test("more than 24 extensions page by the range and the arrows", async () => {
  const many = Array.from({ length: 30 }, (_value, index) =>
    entry({ extension_id: `acme.ext${String(index).padStart(2, "0")}`, name: `Acme ${String(index).padStart(2, "0")}` }),
  );
  mockedFetchCatalog.mockResolvedValue(listing(many));
  renderMarketplace();

  await screen.findByRole("heading", { name: "30 extensions" });
  expect(screen.getAllByRole("link", { name: /^Acme \d\d$/ })).toHaveLength(24);
  const pager = screen.getByRole("navigation", { name: "Extension pages" });
  expect(within(pager).getByText("1–24 of 30")).toBeInTheDocument();
  fireEvent.click(within(pager).getByRole("button", { name: "Next page" }));
  await waitFor(() => expect(screen.getAllByRole("link", { name: /^Acme \d\d$/ })).toHaveLength(6));
  expect(currentUrl()).toBe("?mk_page=2#marketplace");
  expect(within(pager).getByText("25–30 of 30")).toBeInTheDocument();
});

test("the table pages by its own Rows per page, 25 or 50", async () => {
  const many = Array.from({ length: 30 }, (_value, index) =>
    entry({ extension_id: `acme.ext${String(index).padStart(2, "0")}`, name: `Acme ${String(index).padStart(2, "0")}` }),
  );
  mockedFetchCatalog.mockResolvedValue(listing(many));
  renderMarketplace({ url: "/extensions?mk_view=list#marketplace" });

  const table = await screen.findByRole("table", { name: "Extensions" });
  expect(within(table).getAllByRole("row")).toHaveLength(26);
  const pager = screen.getByRole("navigation", { name: "Extension pages" });
  expect(within(pager).getByText("1–25 of 30")).toBeInTheDocument();
  fireEvent.click(within(pager).getByRole("combobox", { name: "Rows per page" }));
  fireEvent.click(await screen.findByRole("option", { name: "50" }));
  expect(await within(pager).findByText("1–30 of 30")).toBeInTheDocument();
  expect(within(screen.getByRole("table", { name: "Extensions" })).getAllByRole("row")).toHaveLength(31);
});

test("no results offers to clear filters and to suggest an extension", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
  renderMarketplace({ url: "/extensions?mk_q=nothing&mk_category=crm#marketplace" });

  expect(await screen.findByText("No extensions match.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Suggest one on GitHub/ })).toHaveAttribute(
    "href",
    "https://github.com/Flow-Steward/extensions",
  );
  fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  await waitFor(() => expect(currentUrl()).toBe("?mk_q=nothing#marketplace"));
});

test("an empty catalog says so", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([]));
  renderMarketplace();

  expect(await screen.findByText("The catalog lists no extensions yet.")).toBeInTheDocument();
});

test("a catalog that is not set up points to installing from a file", async () => {
  mockedFetchCatalog.mockResolvedValue(
    listing([], {
      availability: {
        kind: "extension_catalog_availability",
        configured: false,
        available: false,
        reason: "extension_catalog_not_configured",
        item_count: 0,
        stale: false,
      },
    }),
  );
  renderMarketplace();

  expect(await screen.findByText("The extension catalog isn't set up on this server.")).toBeInTheDocument();
  expect(screen.getByText(/Install extension from file/)).toBeInTheDocument();
  expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
});

test("an unavailable catalog shows the server's message and Retry repeats the request", async () => {
  mockedFetchCatalog.mockResolvedValueOnce(
    listing([], {
      availability: {
        kind: "extension_catalog_availability",
        configured: true,
        available: false,
        message: "The catalog host did not answer (HTTP 503).",
        item_count: 0,
        stale: false,
      },
    }),
  );
  mockedFetchCatalog.mockResolvedValueOnce(listing([CRM]));
  renderMarketplace();

  expect(await screen.findByText("Couldn't load the extension catalog.")).toBeInTheDocument();
  expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByRole("link", { name: "Acme CRM" })).toBeInTheDocument();
  expect(mockedFetchCatalog).toHaveBeenCalledTimes(2);
});

test("a stale catalog is still usable, with a warning and Retry", async () => {
  mockedFetchCatalog.mockResolvedValue(
    listing([CRM, MAIL], {
      availability: {
        kind: "extension_catalog_availability",
        configured: true,
        available: true,
        item_count: 2,
        stale: true,
        fetched_at: "2026-10-01T10:00:00Z",
      },
    }),
  );
  renderMarketplace();

  expect(
    await screen.findByText("Showing the catalog as of 1 Oct 2026. The latest refresh failed."),
  ).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Acme CRM" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(mockedFetchCatalog).toHaveBeenCalledTimes(2));
});

test("find opens the extension's page, and Marketplace then shows the whole catalog", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
  renderMarketplace({ url: "/extensions?find=acme.mail&mk_category=data#marketplace" });

  const heading = await screen.findByRole("heading", { level: 2, name: /Acme Mail/ });
  await waitFor(() => expect(currentUrl()).toBe("?mk_entry=acme.mail#marketplace"));
  await waitFor(() => expect(heading).toHaveFocus());

  fireEvent.click(screen.getByRole("button", { name: "Catalog" }));
  expect(await screen.findByRole("heading", { name: "3 extensions" })).toBeInTheDocument();
  expect(currentUrl()).toBe("#marketplace");
});

test("an unknown find id shows the catalog saying so, and Show all clears it", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
  renderMarketplace({ url: "/extensions?find=acme.unknown#marketplace" });

  expect(await screen.findByText("acme.unknown isn't in the catalog.")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "3 extensions" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Show all" }));
  await waitFor(() => expect(screen.queryByText("acme.unknown isn't in the catalog.")).not.toBeInTheDocument());
  expect(currentUrl()).toBe("#marketplace");
});

test("an open page whose extension left the catalog returns to the results", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM]));
  renderMarketplace({ url: "/extensions?mk_entry=acme.gone#marketplace" });

  expect(await screen.findByText("acme.gone isn't in the catalog.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Acme CRM" })).toBeInTheDocument();
});

test("a selected value the index no longer has is dropped from the URL", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
  renderMarketplace({ url: "/extensions?mk_category=crm,retired#marketplace" });

  await waitFor(() => expect(currentUrl()).toBe("?mk_category=crm#marketplace"));
  expect(await screen.findByRole("heading", { name: "1 extension" })).toBeInTheDocument();
});

test("opening and leaving a page keeps the filters and returns focus to the card", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
  renderMarketplace({ url: "/extensions?mk_category=crm#marketplace" });

  fireEvent.click(await screen.findByRole("link", { name: "Acme CRM" }));
  expect(await screen.findByRole("heading", { level: 2, name: /Acme CRM/ })).toBeInTheDocument();
  expect(currentUrl()).toBe("?mk_category=crm&mk_entry=acme.crm#marketplace");
  // The rail is hidden while a page is open.
  expect(screen.queryByRole("navigation", { name: "Extension filters" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Catalog" }));
  const card = await screen.findByRole("link", { name: "Acme CRM" });
  expect(currentUrl()).toBe("?mk_category=crm#marketplace");
  await waitFor(() => expect(card).toHaveFocus());
});

test("a click asking for a new tab keeps the browser's behaviour", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL]));
  renderMarketplace();

  const card = await screen.findByRole("link", { name: "Acme CRM" });
  // Seen after the page's own handler: the browser would still follow the
  // link. Cancelled here only because jsdom cannot open a tab.
  let keptByBrowser = false;
  const after = (event: Event) => {
    keptByBrowser = !event.defaultPrevented;
    event.preventDefault();
  };
  document.addEventListener("click", after);
  try {
    fireEvent.click(card, { ctrlKey: true });
  } finally {
    document.removeEventListener("click", after);
  }
  expect(keptByBrowser).toBe(true);
  expect(currentUrl()).toBe("#marketplace");
});

test("the page reads what it adds and asks for before its facts", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL]));
  renderMarketplace({ url: "/extensions?mk_entry=acme.crm#marketplace" });

  await screen.findByRole("heading", { level: 2, name: /Acme CRM/ });
  expect(screen.getByText("Keeps contacts in step.")).toBeInTheDocument();
  expect(screen.getByText("Runs on a schedule.")).toBeInTheDocument();
  expect(screen.getByText("Create one Acme connection first.")).toBeInTheDocument();
  const adds = screen.getByRole("region", { name: "What it adds" });
  expect(within(adds).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
    "Agent tools",
    "Workflow actions",
    "Data queries",
    "Notifications",
  ]);
  const asks = screen.getByRole("region", { name: "Permissions it asks for" });
  // Running its own operations is always last.
  expect(within(asks).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
    "Read artifacts artifact:read",
    "Run its own operations extension:invoke",
  ]);
  const facts = screen.getByRole("complementary", { name: "Extension facts" });
  expect(within(facts).getByText("CRM")).toBeInTheDocument();
  expect(within(facts).getByText(/Updated 1 Oct 2026/)).toBeInTheDocument();
  expect(within(facts).getByText("66 KB")).toBeInTheDocument();
  expect(within(facts).getByText("Flow Steward 0.5.0 or later")).toBeInTheDocument();
  expect(within(facts).getByRole("link", { name: /Documentation/ })).toHaveAttribute(
    "href",
    "https://github.example.test/acme.crm",
  );
});

type ActionCase = {
  name: string;
  item: ExtensionCatalogEntry;
  options?: RenderOptions;
  action: string;
  enabled?: boolean;
  /** Who may not act sees no button, only the note (the action-rights rule). */
  hidden?: boolean;
  note?: string;
};

const UPDATE = entry({ extension_id: "acme.up", display_state: "update_available", version: "1.0.1", installed_version: "1.0.0" });

test.each<ActionCase>([
  { name: "not installed, administrator", item: CRM, action: "Install", enabled: true },
  {
    name: "not installed, not administrator",
    item: CRM,
    options: { canAdminister: false },
    action: "Install",
    hidden: true,
    note: "Only administrators can install extensions.",
  },
  {
    name: "administrator, archive install unavailable",
    item: CRM,
    options: { installBlockedReason: "Extension archive changes are unavailable from this web interface." },
    action: "Install",
    enabled: false,
    note: "Extension archive changes are unavailable from this web interface.",
  },
  {
    name: "incompatible",
    item: NATIVE,
    action: "Install",
    enabled: false,
    note: "No package for this server. Available for linux-amd64.",
  },
  { name: "installed", item: MAIL, action: "Open installed extension" },
  {
    name: "installed newer",
    item: entry({ extension_id: "acme.new", display_state: "installed_newer" }),
    action: "Open installed extension",
  },
  {
    name: "disabled",
    item: entry({ extension_id: "acme.off", display_state: "installed_disabled" }),
    action: "Open installed extension",
    note: "Installed but turned off.",
  },
  {
    name: "update available",
    item: UPDATE,
    action: "Update to v1.0.1",
    enabled: true,
    note: "Version 1.0.1 is available; 1.0.0 is installed.",
  },
  {
    name: "update available, not administrator",
    item: UPDATE,
    options: { canAdminister: false },
    action: "Update to v1.0.1",
    hidden: true,
    note: "Version 1.0.1 is available; 1.0.0 is installed. Only administrators can update extensions.",
  },
  {
    name: "update available, archive install unavailable",
    item: UPDATE,
    options: { installBlockedReason: "Extension archive changes are unavailable from this web interface." },
    action: "Update to v1.0.1",
    enabled: false,
    note: "Version 1.0.1 is available; 1.0.0 is installed. Extension archive changes are unavailable from this web interface.",
  },
  {
    name: "update available for a turned-off install",
    item: { ...UPDATE, installed_state: "disabled" },
    action: "Update to v1.0.1",
    enabled: true,
    note: "Version 1.0.1 is available; 1.0.0 is installed. It's turned off and stays off after the update.",
  },
])("the main action when $name", async ({ item, options, action, enabled, hidden, note }) => {
  mockedFetchCatalog.mockResolvedValue(listing([item, CRM === item ? MAIL : CRM]));
  renderMarketplace({ ...options, url: `/extensions?mk_entry=${item.extension_id}#marketplace` });
  await screen.findByRole("heading", { level: 2 });

  if (hidden) {
    expect(screen.queryByRole("button", { name: action })).not.toBeInTheDocument();
    if (note) expect(screen.getByText(note)).toBeInTheDocument();
    if (action.startsWith("Update")) {
      expect(screen.getByRole("link", { name: "Open installed extension" })).toHaveAttribute("href", `/extensions/${item.extension_id}#installed`);
    }
  } else if (action.startsWith("Update")) {
    const button = screen.getByRole("button", { name: action });
    if (enabled) expect(button).toBeEnabled();
    else expect(button).toBeDisabled();
    if (note) expect(button).toHaveAccessibleDescription(note);
    // "Open installed extension" stays as the secondary action, and the old
    // "isn't available yet" text is gone.
    expect(screen.getByRole("link", { name: "Open installed extension" })).toHaveAttribute(
      "href",
      `/extensions/${item.extension_id}#installed`,
    );
    expect(screen.queryByText(/isn't available yet/)).not.toBeInTheDocument();
  } else if (action === "Install") {
    const button = screen.getByRole("button", { name: "Install" });
    if (enabled) expect(button).toBeEnabled();
    else expect(button).toBeDisabled();
    if (note) expect(button).toHaveAccessibleDescription(note);
  } else {
    const link = screen.getByRole("link", { name: action });
    expect(link).toHaveAttribute("href", `/extensions/${item.extension_id}#installed`);
    if (note) expect(link).toHaveAccessibleDescription(note);
    expect(screen.queryByRole("button", { name: /Install|Update|Uninstall/ })).not.toBeInTheDocument();
  }
});

test("an update lists the permissions the new version adds above the button", async () => {
  const update = { ...UPDATE, added_permissions: ["artifact:read"] };
  mockedFetchCatalog.mockResolvedValue(listing([update, CRM]));
  const { onInstall } = renderMarketplace({ url: "/extensions?mk_entry=acme.up#marketplace" });

  const button = await screen.findByRole("button", { name: "Update to v1.0.1" });
  expect(screen.getByText("This version also asks for: Read artifacts.")).toBeInTheDocument();
  expect(button).toHaveAccessibleDescription(
    "This version also asks for: Read artifacts. Version 1.0.1 is available; 1.0.0 is installed.",
  );
  fireEvent.click(button);
  expect(onInstall).toHaveBeenCalledWith(expect.objectContaining({ extension_id: "acme.up", version: "1.0.1" }));
});

test("cards and the page say who publishes an extension", async () => {
  mockedFetchCatalog.mockResolvedValue(
    listing([
      entry({ extension_id: "acme.crm", name: "CRM", repository_url: "https://github.com/Acme/ext-crm" }),
      entry({ extension_id: "other.crm", name: "CRM", repository_url: "https://github.com/Other/crm", verification_status: "unverified" }),
    ]),
  );
  renderMarketplace();

  expect(await screen.findByText("by Acme")).toBeInTheDocument();
  expect(screen.getByText("by Other")).toBeInTheDocument();
  // Only a publisher the catalog verified carries the Verified pill, beside its name.
  expect(screen.getAllByText("Verified")).toHaveLength(1);
  const [acme, other] = screen.getAllByRole("article", { name: "CRM" });
  expect(within(acme).getByText("Verified")).toHaveClass("fs-badge", "green");
  expect(within(other).queryByText("Verified")).not.toBeInTheDocument();
});

test("cards install and update straight from the results, beside the entry link", async () => {
  const onInstall = vi.fn();
  mockedFetchCatalog.mockResolvedValue(listing([UPDATE, CRM]));
  renderMarketplace({ onInstall });

  // Who may update sees the button in the state's place, not the badge.
  const update = await screen.findByRole("button", { name: /Update to v1.0.1: / });
  expect(update).toHaveTextContent("Update to v1.0.1");
  expect(within(screen.getByRole("article", { name: "acme.up" })).queryByText("Update available")).not.toBeInTheDocument();
  // Never inside the card's link: a button in a link is not reachable on its own.
  for (const link of screen.getAllByRole("link")) expect(within(link).queryByRole("button")).not.toBeInTheDocument();
  fireEvent.click(update);
  expect(onInstall).toHaveBeenCalledWith(expect.objectContaining({ extension_id: "acme.up" }));
});

test("results offer no install to someone who cannot install", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([UPDATE, CRM]));
  renderMarketplace({ canAdminister: false });

  // Update available takes the button's place; Not installed leaves the category alone.
  const update = await screen.findByRole("article", { name: "acme.up" });
  expect(within(update).getByText("Update available")).toHaveClass("fs-badge", "orange");
  expect(screen.queryByRole("button", { name: /^(Install|Update to)/ })).not.toBeInTheDocument();
  const crm = screen.getByRole("article", { name: "Acme CRM" });
  expect(crm).toHaveTextContent(/CRM$/);
});

test("the table has no Install column for someone who cannot install", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([UPDATE, CRM]));
  renderMarketplace({ canAdminister: false, url: "/extensions?mk_view=list#marketplace" });

  const table = await screen.findByRole("table", { name: "Extensions" });
  expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
    "Extension",
    "Status",
    "Category",
    "Capabilities",
  ]);
  expect(within(table).getByText("Update available")).toBeInTheDocument();
});

test("Install hands the whole entry to the page, which owns the call", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM]));
  const { onInstall } = renderMarketplace({ url: "/extensions?mk_entry=acme.crm#marketplace" });

  fireEvent.click(await screen.findByRole("button", { name: "Install" }));
  expect(onInstall).toHaveBeenCalledWith(
    expect.objectContaining({ extension_id: "acme.crm", version: "1.0.0", release_zip_sha256: "a".repeat(64) }),
  );
});

test.each<[ExtensionCatalogInstallState["phase"], string]>([
  ["installing", "Installing…"],
  ["awaiting_approval", "Awaiting approval"],
])("while %s the button says so and refuses a second click", async (phase, label) => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM]));
  const { onInstall } = renderMarketplace({
    url: "/extensions?mk_entry=acme.crm#marketplace",
    installStateById: { "acme.crm": { phase, status: "" } },
  });

  const button = await screen.findByRole("button", { name: label });
  expect(button).toBeDisabled();
  fireEvent.click(button);
  expect(onInstall).not.toHaveBeenCalled();
});

test("a failed install shows the server's reason under the header and Install again", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM]));
  renderMarketplace({
    url: "/extensions?mk_entry=acme.crm#marketplace",
    installStateById: {
      "acme.crm": { phase: "idle", status: "SHA-256 does not match the catalog entry", outcome: "failed" },
    },
  });

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("The extension was not installed");
  expect(alert).toHaveTextContent("SHA-256 does not match the catalog entry");
  expect(screen.getByRole("button", { name: "Install" })).toBeEnabled();
});

test("on a phone the results are rows and the facets one Add filter control", async () => {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  );
  try {
    mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
    renderMarketplace();

    await screen.findByRole("heading", { name: "3 extensions" });
    expect(screen.queryByRole("navigation", { name: "Extension filters" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Table" })).not.toBeInTheDocument();
    const row = screen.getByRole("link", { name: "Acme Native" });
    expect(within(row).getByText("Incompatible")).toBeInTheDocument();
    expect(within(row).getByText("No package for this server.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add filter" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Status" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Installed (1)" }));
    expect(await screen.findByRole("heading", { name: "1 extension" })).toBeInTheDocument();
    expect(screen.getByText("Status · Installed")).toBeInTheDocument();
  } finally {
    vi.unstubAllGlobals();
  }
});

test("cards, list rows and the extension's page show the logo Flow Steward serves", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL]));
  renderMarketplace();

  const card = await screen.findByRole("article", { name: "Acme CRM" });
  expect(within(card).getByTestId("catalog-icon")).toHaveClass("fs-catalog-icon-card");
  expect(within(card).getByRole("presentation")).toHaveAttribute("src", "/api/extensions/catalog/acme.crm/icon?v=abc");

  fireEvent.click(within(card).getByRole("link", { name: "Acme CRM" }));
  expect(await screen.findByRole("heading", { level: 2, name: /Acme CRM/ })).toBeInTheDocument();
  const [pageTile] = screen.getAllByTestId("catalog-icon").filter((tile) => tile.className.includes("fs-catalog-icon-page"));
  expect(within(pageTile).getByRole("presentation")).toHaveAttribute("src", "/api/extensions/catalog/acme.crm/icon?v=abc");
});

test("the list view shows each extension's logo in a row tile", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL]));
  renderMarketplace({ url: "/extensions?mk_view=list#marketplace" });
  const table = await screen.findByRole("table", { name: "Extensions" });

  const [, crmRow] = within(table).getAllByRole("row");
  expect(within(crmRow).getByTestId("catalog-icon")).toHaveClass("fs-catalog-icon-row");
  expect(within(crmRow).getByRole("presentation")).toHaveAttribute("src", "/api/extensions/catalog/acme.crm/icon?v=abc");
});

test("a logo that fails to load leaves the Package glyph in the tile", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM]));
  renderMarketplace();

  const card = await screen.findByRole("article", { name: "Acme CRM" });
  fireEvent.error(within(card).getByRole("presentation"));

  expect(within(card).queryByRole("presentation")).not.toBeInTheDocument();
  expect(within(card).getByTestId("catalog-icon")).toHaveAttribute("data-catalog-icon", "fallback");
  expect(within(card).getByTestId("catalog-icon")).toContainHTML("lucide-package");
});

test("an extension without an icon shows the package, whoever publishes it, never the Flow mark", async () => {
  const flowSteward = entry({
    extension_id: "flow-steward.files",
    name: "File Transfer",
    repository_url: "https://github.com/Flow-Steward/ext-file-transfer",
    icon_url: "",
    has_icon: false,
  });
  const other = entry({ extension_id: "acme.plain", name: "Plain", icon_url: "", has_icon: false });
  mockedFetchCatalog.mockResolvedValue(listing([flowSteward, other]));
  renderMarketplace();

  for (const name of ["File Transfer", "Plain"]) {
    const tile = within(await screen.findByRole("article", { name })).getByTestId("catalog-icon");
    expect(tile).toHaveAttribute("data-catalog-icon", "fallback");
    expect(tile).toContainHTML("lucide-package");
    expect(tile).not.toContainHTML("fs-catalog-icon-mark");
  }
});

test("a New extension wears the Iridescent pill; an installed one wears its state alone", async () => {
  const day = 24 * 60 * 60 * 1000;
  const added = (daysAgo: number) => new Date(Date.now() - daysAgo * day).toISOString();
  const launch = entry({ extension_id: "acme.old", name: "Acme Old", added_at: added(90) });
  const fresh = entry({ extension_id: "acme.fresh", name: "Acme Fresh", added_at: added(3) });
  const freshInstalled = entry({ extension_id: "acme.have", name: "Acme Have", added_at: added(3), display_state: "installed" });
  mockedFetchCatalog.mockResolvedValue(listing([launch, fresh, freshInstalled]));
  renderMarketplace();

  const card = await screen.findByRole("article", { name: "Acme Fresh" });
  expect(within(card).getByText("New")).toHaveClass("fs-badge", "new");
  expect(within(screen.getByRole("article", { name: "Acme Old" })).queryByText("New")).not.toBeInTheDocument();
  expect(within(screen.getByRole("article", { name: "Acme Have" })).queryByText("New")).not.toBeInTheDocument();
});

test("without the right to install a card shows no button and no text about it, in cards and in the table", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL]));
  renderMarketplace({ canAdminister: false });

  const card = await screen.findByRole("article", { name: "Acme CRM" });
  expect(within(card).queryByRole("button", { name: /Install/ })).not.toBeInTheDocument();
  expect(card).not.toHaveTextContent(/administrator|\bInstall\b/);
  expect(screen.queryByText(/Only administrators/)).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Table" }));
  const table = await screen.findByRole("table", { name: "Extensions" });
  expect(within(table).queryByRole("button", { name: /Install/ })).not.toBeInTheDocument();
  expect(table).not.toHaveTextContent(/administrator/i);
});

test("on the page of an extension nobody here may install, the header says why in place of Install", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL]));
  renderMarketplace({ canAdminister: false, url: "/extensions?mk_entry=acme.crm#marketplace" });

  await screen.findByRole("heading", { level: 2, name: /Acme CRM/ });
  expect(screen.queryByRole("button", { name: "Install" })).not.toBeInTheDocument();
  expect(screen.getByText("Only administrators can install extensions.")).toHaveClass("fs-marketplace-action-note");
});

test("the sort offers A–Z and Recently added only", async () => {
  mockedFetchCatalog.mockResolvedValue(listing([CRM, MAIL, NATIVE]));
  renderMarketplace();

  fireEvent.click(await screen.findByRole("combobox", { name: "Sort" }));
  expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["A–Z", "Recently added"]);
});
