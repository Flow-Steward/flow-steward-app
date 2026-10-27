import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { HttpError } from "@/lib/http/errors";
import { RegistryMcpPage } from "@/pages/registry/mcp";
import {
  fetchMcpCatalog,
  fetchMcpCatalogAvailability,
  fetchMcpCatalogItem,
  fetchMcpConnectors,
  installMcpCatalogItem,
  type McpCatalogCard,
  type McpCatalogItem,
  type McpCatalogRemote,
} from "@/pages/registry/mcp/api";
import { McpCatalogPanel } from "@/pages/registry/mcp/mcp-catalog";
import { documentationUrl } from "@/pages/registry/mcp/mcp-catalog-model";
import { keyValueObject, RESERVED_OAUTH_PARAMETER_IDENTITIES } from "@/pages/registry/mcp/key-value-rows";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { useHasCapability } from "@/lib/auth/capabilities";

vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn() }));
vi.mock("@/pages/projects/list/api", () => ({ fetchProjectsList: vi.fn() }));
vi.mock("@/pages/registry/mcp/api", () => ({
  MCP_CATALOG_QUERY_KEY: ["registry", "mcp", "catalog"],
  fetchMcpCatalog: vi.fn(),
  fetchMcpCatalogAvailability: vi.fn(),
  fetchMcpCatalogItem: vi.fn(),
  fetchMcpConnectors: vi.fn(),
  installMcpCatalogItem: vi.fn(),
  mcpCatalogIconUrl: (itemId: string) => `/api/mcp/catalog/${itemId}/icon`,
}));

const mockedCatalog = vi.mocked(fetchMcpCatalog);
const mockedAvailability = vi.mocked(fetchMcpCatalogAvailability);
const mockedInstall = vi.mocked(installMcpCatalogItem);
const mockedItem = vi.mocked(fetchMcpCatalogItem);

/** The list carries cards; the full entry comes from fetchMcpCatalogItem. */
function card(full: McpCatalogItem): McpCatalogCard {
  return {
    item_id: full.item_id,
    revision: full.revision,
    status: full.status,
    server_name: full.server_name,
    title: full.title,
    summary: full.summary,
    category: full.category,
    preview_note: full.preview_note,
    official: full.official,
    has_icon: full.has_icon,
    installed: full.installed,
    keywords: full.keywords,
    added_at: full.added_at,
    sign_in: [...new Set(full.remotes.map((remote) => remote.auth.mode))],
  };
}

function serve(items: McpCatalogItem[]) {
  mockedCatalog.mockResolvedValue({
    catalog_revision: "r1",
    category_labels: { productivity: "Productivity", email: "Email", other: "Other" },
    items: items.map(card),
  });
  mockedItem.mockImplementation(async (itemId: string) => {
    const found = items.find((candidate) => candidate.item_id === itemId);
    if (!found) throw new Error("not found");
    return found;
  });
}

const PROJECT = "00000000-0000-0000-0000-000000000001";

function remote(overrides: Partial<McpCatalogRemote> = {}): McpCatalogRemote {
  return {
    remote_id: "default",
    label: "Default",
    url: "https://mcp.example.test/mcp",
    allowed_hosts: ["mcp.example.test"],
    user_defined_host: false,
    auth: {
      mode: "oauth2",
      client_registration: "dcr",
      requested_scopes: [],
      authorization_parameters: {},
      refresh_token_required: false,
      setup_url: "",
    },
    variables: [],
    headers: [],
    ...overrides,
  };
}

function item(overrides: Partial<McpCatalogItem> = {}): McpCatalogItem {
  return {
    item_id: "notion",
    revision: 1,
    status: "active",
    server_name: "com.notion/mcp",
    server_version: "1.0.1",
    title: "Notion",
    summary: "Search and update Notion pages.",
    description: "Search, read, and update Notion pages and databases.",
    category: "productivity",
    website_url: "https://www.notion.com",
    help_url: "https://developers.notion.com/docs/get-started-with-mcp",
    preview_note: "",
    official: { vendor: "Notion", evidence_url: "https://developers.notion.com/docs/get-started-with-mcp" },
    has_icon: true,
    remotes: [remote()],
    installed: [],
    ...overrides,
  };
}

const GMAIL = item({
  item_id: "google-gmail",
  title: "Gmail",
  summary: "Read and draft mail.",
  category: "email",
  preview_note: "Developer Preview",
  has_icon: false,
  remotes: [
    remote({
      url: "https://gmailmcp.googleapis.com/mcp/v1",
      auth: {
        mode: "oauth2",
        client_registration: "user_supplied",
        requested_scopes: ["https://www.googleapis.com/auth/gmail.readonly"],
        authorization_parameters: { access_type: "offline" },
        refresh_token_required: true,
        setup_url: "https://developers.google.com/workspace/guides/configure-mcp-servers",
      },
    }),
  ],
});

const MONDAY = item({
  item_id: "monday",
  title: "monday.com",
  summary: "Boards and items.",
  official: null,
  remotes: [
    remote({ label: "Sign in with monday.com", url: "https://mcp.monday.com/mcp" }),
    remote({
      remote_id: "api-token",
      label: "monday.com API token",
      url: "https://mcp.monday.com/mcp",
      auth: {
        mode: "headers",
        client_registration: "",
        requested_scopes: [],
        authorization_parameters: {},
        refresh_token_required: false,
        setup_url: "",
      },
      headers: [{ name: "Authorization", description: "Your API token", required: false, secret: true }],
    }),
  ],
});

const OTHER_TOOL = item({
  item_id: "io-other-tool",
  server_name: "io.other/tool",
  title: "Other tool",
  summary: "Does one thing over MCP.",
  description: "Does one thing over MCP.",
  category: "other",
  official: null,
  has_icon: false,
  remotes: [remote({ url: "https://tool.other.io/mcp", label: "Other tool" })],
});

function showPanel(onInstalled = vi.fn(), { canManage = true }: { canManage?: boolean } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/registry/mcp?tab=catalog"]}>
        <McpCatalogPanel canManage={canManage} onInstalled={onInstalled} projectId={PROJECT} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return onInstalled;
}

function installResult(itemId: string, nextStep: "sync" | "oauth_connect" | "oauth_client") {
  return {
    connector: { connector_id: "mcp_0000000000000001", catalog_item_id: itemId } as never,
    next_step: nextStep,
    setup_url: "",
    client_error: "",
  };
}

describe("McpCatalogPanel", () => {
  beforeEach(() => {
    mockedAvailability.mockReset();
    mockedAvailability.mockResolvedValue({ configured: true, available: true });
    mockedCatalog.mockReset();
    mockedItem.mockReset();
    serve([
      item({ installed: [{ connector_id: "mcp_a", display_name: "Notion", project_id: PROJECT, remote_id: "default", catalog_item_revision: 1 }] }),
      GMAIL,
      MONDAY,
      OTHER_TOOL,
    ]);
    mockedInstall.mockReset();
  });

  it("lists reviewed servers with their icon, official badge and existing connections", async () => {
    showPanel();

    const notion = await screen.findByRole("article", { name: "Notion" });
    expect(within(notion).getByText("Official")).toBeInTheDocument();
    expect(within(notion).getByText("Connected")).toBeInTheDocument();
    expect(within(notion).getByRole("button", { name: "Connect another" })).toBeInTheDocument();
    expect(within(notion).getByRole("presentation")).toHaveAttribute("src", "/api/mcp/catalog/notion/icon");

    const gmail = screen.getByRole("article", { name: "Gmail" });
    // A preview note is the card's Preview pill; the note itself is read on the page.
    expect(within(gmail).getByText("Preview")).toBeInTheDocument();
    expect(within(gmail).queryByText("Developer Preview")).not.toBeInTheDocument();
    // No icon in the catalog: two letters stand in, nothing is fetched.
    expect(within(gmail).queryByRole("presentation")).not.toBeInTheDocument();
    expect(within(gmail).getByText("Gm")).toBeInTheDocument();
    // A server its vendor did not publish says so rather than leaving the byline empty.
    const monday = screen.getByRole("article", { name: "monday.com" });
    expect(monday).not.toHaveTextContent("Official");
    expect(within(monday).getByText("Community")).toBeInTheDocument();
  });

  it("opens a server's page from its card before anything is installed", async () => {
    const user = userEvent.setup();
    mockedInstall.mockResolvedValue(installResult("google-gmail", "oauth_client"));
    showPanel();

    await user.click(within(await screen.findByRole("article", { name: "Gmail" })).getByRole("button", { name: "Gmail" }));

    const heading = await screen.findByRole("heading", { name: "Gmail", level: 2 });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.getByText(/your own OAuth client, created in the service's developer console/u)).toBeInTheDocument();
    expect(screen.getByText(/Asks for:/u)).toHaveTextContent("Asks for: https://www.googleapis.com/auth/gmail.readonly");
    expect(screen.getByText("Developer Preview")).toBeInTheDocument();
    expect(screen.getByText("https://gmailmcp.googleapis.com/mcp/v1")).toBeInTheDocument();
    // The vendor's evidence page is the documentation link already shown: one link, not two.
    const documentation = screen.getAllByRole("link", { name: /^Documentation/u });
    expect(documentation).toHaveLength(1);
    expect(documentation[0]).toHaveAttribute("href", "https://developers.notion.com/docs/get-started-with-mcp");
    expect(screen.queryByText(/Documented by/u)).not.toBeInTheDocument();
    expect(mockedInstall).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Connect" }));
    expect(screen.getByRole("dialog", { name: "Gmail" })).toBeInTheDocument();
    await user.click(within(screen.getByRole("dialog", { name: "Gmail" })).getByRole("button", { name: "Cancel" }));

    await user.click(screen.getByRole("button", { name: "Catalog" }));
    expect(await screen.findByRole("article", { name: "Notion" })).toBeInTheDocument();
  });

  it("links one Documentation: the page's own, else the vendor's evidence page", async () => {
    const user = userEvent.setup();
    const evidence = "https://vendor.example.test/mcp-docs";
    const base = {
      website_url: "https://www.example.test",
      help_url: "https://help.example.test/mcp",
      official: { vendor: "Example", evidence_url: evidence },
    };
    serve([
      item({ ...base, item_id: "distinct", title: "Distinct" }),
      item({ ...base, item_id: "as-website", title: "As website", website_url: evidence }),
    ]);
    showPanel();

    await user.click(within(await screen.findByRole("article", { name: "Distinct" })).getByRole("button", { name: "Distinct" }));
    await screen.findByRole("heading", { name: "Distinct", level: 2 });
    const links = screen.getAllByRole("link", { name: /^(Website|Documentation)/u });
    expect(links.map((link) => [link.textContent?.trim(), link.getAttribute("href")])).toEqual([
      ["Website (opens in a new tab)", "https://www.example.test"],
      ["Documentation (opens in a new tab)", "https://help.example.test/mcp"],
    ]);
    expect(screen.queryByText(/Documented by/u)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Catalog" }));
    await user.click(within(await screen.findByRole("article", { name: "As website" })).getByRole("button", { name: "As website" }));
    await screen.findByRole("heading", { name: "As website", level: 2 });
    // The evidence page is the website already shown: no second link to it.
    const hrefs = screen.getAllByRole("link", { name: /^(Website|Documentation)/u }).map((link) => link.getAttribute("href"));
    expect(hrefs).toEqual([evidence, "https://help.example.test/mcp"]);
  });

  it("decides the one Documentation link: own help page, else vendor evidence, never an address already shown", () => {
    const evidence = "https://vendor.example.test/docs";
    const page = { ...item(), website_url: "https://www.example.test", help_url: "", official: { vendor: "V", evidence_url: evidence } };
    expect(documentationUrl(page)).toBe(evidence);
    expect(documentationUrl({ ...page, help_url: "https://help.example.test" })).toBe("https://help.example.test");
    expect(documentationUrl({ ...page, website_url: `${evidence}/` })).toBe("");
    // ActiveCampaign: its help page is its setup guide.
    expect(documentationUrl({ ...page, help_url: evidence, remotes: [remote({ auth: { ...remote().auth, setup_url: evidence } })] })).toBe("");
    expect(documentationUrl({ ...page, official: null })).toBe("");
  });

  it("lists the connections already made from a server on its page", async () => {
    const user = userEvent.setup();
    showPanel();

    await user.click(within(await screen.findByRole("article", { name: "Notion" })).getByRole("button", { name: "Notion" }));

    expect(await screen.findByRole("heading", { name: "Notion", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Notion" })).toHaveAttribute(
      "href",
      `/registry/mcp/mcp_a?project_id=${PROJECT}`,
    );
    expect(screen.getByRole("button", { name: "Connect another" })).toBeInTheDocument();
  });

  it("does not match every server by its publisher's namespace", async () => {
    const user = userEvent.setup();
    showPanel();
    await screen.findByRole("article", { name: "Other tool" });

    // OTHER_TOOL is io.other/tool; "other" is in its namespace and its title.
    await user.type(screen.getByRole("searchbox", { name: "Search the MCP catalog" }), "io.other");

    expect(screen.queryByRole("article", { name: "Other tool" })).not.toBeInTheDocument();
  });

  it("finds a service by one of its other names", async () => {
    const user = userEvent.setup();
    serve([
      item(),
      item({ item_id: "atlassian", title: "Atlassian", summary: "Work with issues and pages.", keywords: ["Jira", "Confluence"] }),
    ]);
    showPanel();
    await screen.findByRole("article", { name: "Atlassian" });

    await user.type(screen.getByRole("searchbox", { name: "Search the MCP catalog" }), "jira");

    expect(screen.getByRole("article", { name: "Atlassian" })).toBeInTheDocument();
    expect(screen.queryByRole("article", { name: "Notion" })).not.toBeInTheDocument();
  });

  it("shows the count, sorts by name and switches to the list view", async () => {
    const user = userEvent.setup();
    showPanel();
    await screen.findByRole("article", { name: "Notion" });

    expect(screen.getByRole("heading", { name: /\d+ servers/u })).toBeInTheDocument();
    const titles = screen.getAllByRole("article").map((card) => card.getAttribute("aria-labelledby") && within(card).getAllByRole("button")[0]?.textContent);
    expect(titles).toEqual([...titles].sort((a, b) => String(a).localeCompare(String(b), undefined, { sensitivity: "base" })));

    await user.click(screen.getByRole("button", { name: "Table" }));
    expect(screen.queryByRole("article", { name: "Notion" })).not.toBeInTheDocument();
    expect(screen.getByRole("table", { name: "MCP servers" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Notion" })).toBeInTheDocument();
  });

  it("offers no sort while the index dates none of its entries", async () => {
    showPanel();
    await screen.findByRole("article", { name: "Notion" });

    expect(screen.queryByRole("combobox", { name: "Sort" })).not.toBeInTheDocument();
    // A–Z, whatever the index order.
    const names = screen.getAllByRole("article").map((card) => within(card).getAllByRole("button")[0]?.textContent);
    expect(names).toEqual(["Gmail", "monday.com", "Notion", "Other tool"]);
  });

  describe("when the index dates its entries", () => {
    const day = 24 * 60 * 60 * 1000;
    const added = (daysAgo: number) => new Date(Date.now() - daysAgo * day).toISOString();

    beforeEach(() => {
      serve([
        item({ item_id: "old", title: "Old server", official: null, added_at: added(90) }),
        item({ item_id: "recent", title: "Recent server", official: null, added_at: added(40) }),
        item({ item_id: "fresh", title: "Fresh server", official: null, added_at: added(3) }),
        item({
          item_id: "fresh-connected",
          title: "Fresh and connected",
          official: null,
          added_at: added(2),
          installed: [{ connector_id: "mcp_a", display_name: "Fresh", project_id: PROJECT, remote_id: "default", catalog_item_revision: 1 }],
        }),
        item({ item_id: "undated", title: "Undated server", official: null }),
      ]);
    });

    const order = () => screen.getAllByRole("article").map((card) => within(card).getAllByRole("button")[0]?.textContent);

    it("sorts A–Z or by Recently added, and offers nothing else", async () => {
      const user = userEvent.setup();
      showPanel();
      await screen.findByRole("article", { name: "Old server" });
      expect(order()).toEqual(["Fresh and connected", "Fresh server", "Old server", "Recent server", "Undated server"]);

      await user.click(screen.getByRole("combobox", { name: "Sort" }));
      expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["A–Z", "Recently added"]);
      await user.click(screen.getByRole("option", { name: "Recently added" }));

      // Newest first; an entry without a date goes last.
      expect(order().map((name) => name?.replace(/v\d.*$/u, ""))).toEqual([
        "Fresh and connected",
        "Fresh server",
        "Recent server",
        "Old server",
        "Undated server",
      ]);
    });

    it("marks only a server added within 30 days, after the first batch, and not yet connected, as New", async () => {
      showPanel();

      const fresh = await screen.findByRole("article", { name: "Fresh server" });
      // New is the Iridescent pill.
      expect(within(fresh).getByText("New")).toHaveClass("fs-badge", "new");
      for (const name of ["Old server", "Recent server", "Undated server", "Fresh and connected"]) {
        expect(within(screen.getByRole("article", { name })).queryByText("New")).not.toBeInTheDocument();
      }
      expect(within(screen.getByRole("article", { name: "Fresh and connected" })).getByText("Connected")).toBeInTheDocument();
    });

    it("shows New in the table and on the server's page", async () => {
      const user = userEvent.setup();
      showPanel();
      await screen.findByRole("article", { name: "Fresh server" });

      await user.click(screen.getByRole("button", { name: "Table" }));
      const table = screen.getByRole("table", { name: "MCP servers" });
      const row = within(table).getAllByRole("row").find((candidate) => within(candidate).queryByRole("button", { name: "Fresh server" })) as HTMLElement;
      expect(within(row).getByText("New")).toHaveClass("fs-badge", "new");

      await user.click(within(row).getByRole("button", { name: "Fresh server" }));
      await screen.findByRole("heading", { name: "Fresh server", level: 2 });
      expect(screen.getByText("New")).toHaveClass("fs-badge", "new");
    });
  });

  it("draws the card as the design does: byline, Preview on the right, category beside Connect", async () => {
    showPanel();

    const notion = await screen.findByRole("article", { name: "Notion" });
    // "by Notion · ✓ Official" under the name; Connected keeps its count.
    expect(within(notion).getByText("by Notion")).toBeInTheDocument();
    expect(within(notion).getByText("Connected")).toHaveClass("fs-badge", "green");
    // The compact secondary button, after the category on the card's last line.
    expect(within(notion).getByRole("button", { name: "Connect another" })).toHaveClass("fs-button", "sm", "secondary");
    expect(notion).toHaveTextContent(/ConnectedProductivityConnect another$/);
    const gmail = screen.getByRole("article", { name: "Gmail" });
    expect(within(gmail).getByText("Preview")).toHaveClass("fs-badge");
    expect(within(gmail).getByRole("button", { name: "Connect" })).toBeInTheDocument();
  });

  it("without the right to manage the project there is no Connect: not on a card, in the table or on the page", async () => {
    const user = userEvent.setup();
    showPanel(vi.fn(), { canManage: false });

    const gmail = await screen.findByRole("article", { name: "Gmail" });
    expect(within(gmail).queryByRole("button", { name: /^Connect/u })).not.toBeInTheDocument();
    // The category stays bottom-left on its own: the name is the card's only control.
    expect(within(gmail).getAllByRole("button")).toHaveLength(1);
    expect(gmail).toHaveTextContent(/Email$/);
    // Nothing about rights where the button would be: no text either.
    expect(gmail).not.toHaveTextContent(/administrator|manage/iu);

    await user.click(screen.getByRole("button", { name: "Table" }));
    const table = screen.getByRole("table", { name: "MCP servers" });
    expect(within(table).getAllByRole("columnheader").map((header) => header.textContent)).toEqual(["Server", "Category", "Sign-in", "Status"]);
    expect(within(table).queryByRole("button", { name: /^Connect/u })).not.toBeInTheDocument();

    await user.click(within(table).getByRole("button", { name: "Gmail" }));
    await screen.findByRole("heading", { name: "Gmail", level: 2 });
    expect(screen.queryByRole("button", { name: /^Connect/u })).not.toBeInTheDocument();
  });

  it("the table ends in a Connect column for whoever manages the project", async () => {
    const user = userEvent.setup();
    showPanel();
    await screen.findByRole("article", { name: "Notion" });

    await user.click(screen.getByRole("button", { name: "Table" }));
    const table = screen.getByRole("table", { name: "MCP servers" });
    const headers = within(table).getAllByRole("columnheader");
    expect(headers.map((header) => header.textContent || header.getAttribute("aria-label"))).toEqual(["Server", "Category", "Sign-in", "Status", "Connect"]);
    const rowOf = (name: string) => within(table).getAllByRole("row").find((row) => within(row).queryByRole("button", { name })) as HTMLElement;
    expect(within(rowOf("Notion")).getByText("Official")).toBeInTheDocument();
    expect(within(rowOf("Notion")).getByRole("button", { name: "Connect another" })).toBeInTheDocument();
    expect(within(rowOf("Other tool")).getByText("Community")).toBeInTheDocument();
  });

  it("narrows by search text", async () => {
    const user = userEvent.setup();
    showPanel();
    await screen.findByRole("article", { name: "Notion" });

    await user.type(screen.getByRole("searchbox", { name: "Search the MCP catalog" }), "mail");

    expect(screen.getByRole("article", { name: "Gmail" })).toBeInTheDocument();
    expect(screen.queryByRole("article", { name: "Notion" })).not.toBeInTheDocument();
  });

  it("installs an OAuth server pinned to the revision it showed", async () => {
    const user = userEvent.setup();
    mockedInstall.mockResolvedValue(installResult("notion", "oauth_connect"));
    const onInstalled = showPanel();

    await user.click(within(await screen.findByRole("article", { name: "Notion" })).getByRole("button", { name: "Connect another" }));
    const dialog = screen.getByRole("dialog", { name: "Notion" });
    expect(within(dialog).getByText(/Already connected in this project: Notion/u)).toBeInTheDocument();
    expect(within(dialog).getByText(/choose Start Authorization on the connector page and sign in to Notion/u)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("OAuth client ID")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Install" }));

    await waitFor(() => expect(onInstalled).toHaveBeenCalled());
    expect(mockedInstall).toHaveBeenCalledWith(
      "notion",
      expect.objectContaining({ remote_id: "default", item_revision: 1, headers: {}, client_id: "" }),
      PROJECT,
    );
  });

  it("asks for the user's own OAuth client when the service has no client registration", async () => {
    const user = userEvent.setup();
    mockedInstall.mockResolvedValue(installResult("google-gmail", "oauth_connect"));
    showPanel();

    await user.click(within(await screen.findByRole("article", { name: "Gmail" })).getByRole("button", { name: "Connect" }));
    const dialog = screen.getByRole("dialog", { name: "Gmail" });
    expect(within(dialog).getByRole("link", { name: /Setup guide/u })).toHaveAttribute(
      "href",
      "https://developers.google.com/workspace/guides/configure-mcp-servers",
    );
    await user.type(within(dialog).getByLabelText("OAuth client ID"), "client.apps.googleusercontent.com");
    await user.type(within(dialog).getByLabelText("OAuth client secret"), "shh");
    await user.click(within(dialog).getByRole("button", { name: "Install" }));

    await waitFor(() =>
      expect(mockedInstall).toHaveBeenCalledWith(
        "google-gmail",
        expect.objectContaining({ client_id: "client.apps.googleusercontent.com", client_secret: "shh" }),
        PROJECT,
      ),
    );
  });

  it("switches to the API token endpoint and sends only its declared header", async () => {
    const user = userEvent.setup();
    mockedInstall.mockResolvedValue(installResult("monday", "sync"));
    showPanel();

    await user.click(within(await screen.findByRole("article", { name: "monday.com" })).getByRole("button", { name: "Connect" }));
    const dialog = screen.getByRole("dialog", { name: "monday.com" });
    // OAuth sign-in never asks for the credential header the record also declares.
    expect(within(dialog).queryByLabelText("Authorization")).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("combobox", { name: "Connect with" }));
    await user.click(screen.getByRole("option", { name: /monday.com API token/u }));
    await user.type(within(dialog).getByLabelText("Authorization"), "Bearer token-1");
    await user.click(within(dialog).getByRole("button", { name: "Install" }));

    await waitFor(() =>
      expect(mockedInstall).toHaveBeenCalledWith(
        "monday",
        expect.objectContaining({ remote_id: "api-token", headers: { Authorization: "Bearer token-1" } }),
        PROJECT,
      ),
    );
  });

  it("shows the catalog's sign-in settings filled in and sends them as edited", async () => {
    const user = userEvent.setup();
    mockedInstall.mockResolvedValue(installResult("google-gmail", "oauth_client"));
    showPanel();

    await user.click(within(await screen.findByRole("article", { name: "Gmail" })).getByRole("button", { name: "Connect" }));
    const dialog = screen.getByRole("dialog", { name: "Gmail" });
    const scope = within(dialog).getByLabelText("Requested scope 1");
    expect(scope).toHaveValue("https://www.googleapis.com/auth/gmail.readonly");
    expect(within(dialog).getByDisplayValue("access_type")).toBeInTheDocument();
    expect(within(dialog).getByDisplayValue("offline")).toBeInTheDocument();
    expect(within(dialog).getByRole("checkbox", { name: /Require a refresh token/u })).toBeChecked();

    await user.click(within(dialog).getByRole("button", { name: "Add requested scope" }));
    await user.type(within(dialog).getByLabelText("Requested scope 2"), "https://www.googleapis.com/auth/gmail.compose");
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /Require a refresh token/u }));
    await user.click(within(dialog).getByRole("button", { name: "Install" }));

    await waitFor(() =>
      expect(mockedInstall).toHaveBeenCalledWith(
        "google-gmail",
        expect.objectContaining({
          requested_scopes: ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose"],
          authorization_parameters: { access_type: "offline" },
          refresh_token_required: false,
        }),
        PROJECT,
      ),
    );
  });

  it("sends no OAuth settings for a header sign-in", async () => {
    const user = userEvent.setup();
    mockedInstall.mockResolvedValue(installResult("monday", "sync"));
    showPanel();

    await user.click(within(await screen.findByRole("article", { name: "monday.com" })).getByRole("button", { name: "Connect" }));
    const dialog = screen.getByRole("dialog", { name: "monday.com" });
    await user.click(within(dialog).getByRole("combobox", { name: "Connect with" }));
    await user.click(screen.getByRole("option", { name: /monday.com API token/u }));
    expect(within(dialog).queryByText("Sign-in settings")).not.toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Authorization"), "Bearer t");
    await user.click(within(dialog).getByRole("button", { name: "Install" }));

    await waitFor(() => expect(mockedInstall).toHaveBeenCalled());
    expect(mockedInstall.mock.calls[0]?.[1]).not.toHaveProperty("requested_scopes");
  });

  it("labels the fields with the reviewer's names and keeps the keys the URL uses", async () => {
    const user = userEvent.setup();
    mockedInstall.mockResolvedValue(installResult("freshdesk", "sync"));
    const headersAuth = {
      mode: "headers",
      client_registration: "",
      requested_scopes: [],
      authorization_parameters: {},
      refresh_token_required: false,
      setup_url: "",
    } satisfies McpCatalogRemote["auth"];
    serve([
      item({
        item_id: "freshdesk",
        title: "Freshdesk",
        remotes: [
          remote({
            url: "https://{subdomain}.freshdesk.com/{instance_path}",
            allowed_hosts: ["*.freshdesk.com"],
            auth: headersAuth,
            variables: [
              { name: "subdomain", description: "Before .freshdesk.com", required: true, choices: [], default: "", label: "Freshdesk subdomain" },
              { name: "instance_path", description: "", required: true, choices: [], default: "mcp" },
            ],
            headers: [{ name: "Authorization", description: "Profile settings", required: true, secret: true, label: "API key" }],
          }),
        ],
      }),
    ]);
    showPanel();

    await user.click(within(await screen.findByRole("article", { name: "Freshdesk" })).getByRole("button", { name: "Connect" }));
    const dialog = screen.getByRole("dialog", { name: "Freshdesk" });
    await user.type(within(dialog).getByLabelText(/Freshdesk subdomain/u), "acme");
    expect(within(dialog).getByLabelText(/Instance path/u)).toHaveValue("mcp"); // no label: the name, readable
    await user.type(within(dialog).getByLabelText(/API key/u), "key-1");
    await user.click(within(dialog).getByRole("button", { name: "Install" }));

    await waitFor(() =>
      expect(mockedInstall).toHaveBeenCalledWith(
        "freshdesk",
        expect.objectContaining({ variables: { subdomain: "acme", instance_path: "mcp" }, headers: { Authorization: "key-1" } }),
        PROJECT,
      ),
    );
  });

  it("shows the current entry when it changed while the form was open", async () => {
    const user = userEvent.setup();
    const changed = item({ revision: 2, summary: "Updated summary." });
    mockedInstall.mockRejectedValueOnce(
      new HttpError({ status: 409, url: "/api/mcp/catalog/notion/install", payload: { code: "mcp_catalog_item_changed", item: changed } }),
    );
    showPanel();

    await user.click(within(await screen.findByRole("article", { name: "Notion" })).getByRole("button", { name: "Connect another" }));
    const dialog = screen.getByRole("dialog", { name: "Notion" });
    await user.click(within(dialog).getByRole("button", { name: "Install" }));

    expect(await within(dialog).findByText(/This catalog entry changed while the form was open/u)).toBeInTheDocument();
    mockedInstall.mockResolvedValue(installResult("notion", "oauth_connect"));
    await user.click(within(dialog).getByRole("button", { name: "Install" }));
    await waitFor(() =>
      expect(mockedInstall).toHaveBeenLastCalledWith("notion", expect.objectContaining({ item_revision: 2 }), PROJECT),
    );
  });

  it("says so when the catalog is not configured", async () => {
    mockedAvailability.mockResolvedValue({ configured: false, available: false });
    showPanel();
    expect(await screen.findByText("The MCP catalog isn't set up on this server.")).toBeInTheDocument();
    expect(mockedCatalog).not.toHaveBeenCalled();
  });
});

describe("OAuth authorization parameters", () => {
  it("accept Dropbox's token_access_type and still refuse credential-like names", () => {
    const rows = [{ id: 1, key: "token_access_type", value: "offline" }];
    expect(keyValueObject(rows, "Authorization parameters", RESERVED_OAUTH_PARAMETER_IDENTITIES)).toEqual({ token_access_type: "offline" });
    expect(() =>
      keyValueObject([{ id: 1, key: "access_token", value: "x" }], "Authorization parameters", RESERVED_OAUTH_PARAMETER_IDENTITIES),
    ).toThrow(/reserved/u);
  });
});

describe("RegistryMcpPage catalog tab", () => {
  function showPage(entry = "") {
    vi.mocked(useHasCapability).mockReturnValue(true);
    vi.mocked(fetchProjectsList).mockResolvedValue({
      projects: [{ id: PROJECT, name: "Alpha" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    } as never);
    vi.mocked(fetchMcpConnectors).mockResolvedValue([]);
    mockedAvailability.mockResolvedValue({ configured: true, available: true });
    serve([item(), GMAIL]);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/registry/mcp?project_id=${PROJECT}&tab=catalog${entry}`]}>
          <Routes>
            <Route element={<RegistryMcpPage />} path="/registry/mcp" />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("goes back to the list when the Catalog tab is clicked on a server's page", async () => {
    const user = userEvent.setup();
    showPage("&mcp_entry=google-gmail");
    expect(await screen.findByRole("heading", { name: "Gmail" })).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Catalog" }));

    expect(await screen.findByRole("article", { name: "Notion" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Catalog" })).toHaveAttribute("aria-selected", "true");
  });

  it("opens the connector page after an install", async () => {
    const user = userEvent.setup();
    vi.mocked(useHasCapability).mockReturnValue(true);
    vi.mocked(fetchProjectsList).mockResolvedValue({
      projects: [{ id: PROJECT, name: "Alpha" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    } as never);
    vi.mocked(fetchMcpConnectors).mockResolvedValue([]);
    mockedAvailability.mockResolvedValue({ configured: true, available: true });
    serve([item()]);
    mockedInstall.mockResolvedValue(installResult("notion", "oauth_connect"));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/registry/mcp?project_id=${PROJECT}&tab=catalog`]}>
          <Routes>
            <Route element={<RegistryMcpPage />} path="/registry/mcp" />
            <Route element={<div>mcp detail page</div>} path="/registry/mcp/:connectorId" />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("tab", { name: "Catalog" })).toHaveAttribute("aria-selected", "true");
    await user.click(within(await screen.findByRole("article", { name: "Notion" })).getByRole("button", { name: "Connect" }));
    await user.click(within(screen.getByRole("dialog", { name: "Notion" })).getByRole("button", { name: "Install" }));

    expect(await screen.findByText("mcp detail page")).toBeInTheDocument();
  });
});
