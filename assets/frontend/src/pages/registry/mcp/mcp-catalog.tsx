import { McpCatalogIcon, ExternalAnchor } from "./mcp-catalog-presentation";
import { McpCatalogInstallDialog } from "./mcp-catalog-install-dialog";
import { signInLabel, itemQueryKey, documentationUrl, type McpProjectOption } from "./mcp-catalog-model";
import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { type ReactNode, type RefObject, useEffect, useMemo, useRef, useState } from "react";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, PlugZap, RefreshCw, Search, SearchX, TriangleAlert } from "lucide-react";
import { Link, useSearchParams } from "react-router";

import {
  CatalogByline,
  CatalogFact,
  CatalogFacetRail,
  CatalogItemCard,
  CatalogPageHead,
  CatalogPager,
  type CatalogPill,
  CatalogResultsHead,
  CatalogStateBlock,
  CatalogTableItem,
} from "@/components/shared/catalog-browse";
import { DsAlert, DsTable, type DsTableColumn } from "@/components/shared/design-system";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { useCompactCatalog } from "@/lib/react/use-compact-catalog";
import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  CATALOG_TABLE_PAGE_SIZES,
  type CatalogFacetDef,
  type CatalogFilterState,
  type CatalogView,
  catalogTextMatches,
  clearFacetValues,
  compareAddedNewestFirst,
  computeCatalogFacets,
  currentTime,
  earliestAddedTime,
  filterCatalogItems,
  isRecentlyAdded,
  toggleFacetValue,
} from "@/lib/utils/catalog-facets";
import {
  fetchMcpCatalog,
  fetchMcpCatalogAvailability,
  fetchMcpCatalogItem,
  MCP_CATALOG_QUERY_KEY,
  type McpCatalogCard,
  type McpCatalogInstallResult,
  type McpCatalogItem,
  type McpCatalogRemote,
} from "@/pages/registry/mcp/api";
import "@/pages/extensions/marketplace-catalog.css";

const PAGE_SIZE = 24;
const NOUN = ["server", "servers"] as const;
type FacetKey = "category" | "sign_in";
type Filters = CatalogFilterState<FacetKey>;
const EMPTY_FILTERS: Filters = { q: "", category: [], sign_in: [] };

type Translate = ReturnType<typeof useI18n>["t"];
type SortKey = "name" | "added";
const FACET_KEYS: readonly FacetKey[] = ["category", "sign_in"];

/** A–Z by name, or the most recently added entries first. */
function sortCards(items: readonly McpCatalogCard[], sort: SortKey): McpCatalogCard[] {
  const byName = (a: McpCatalogCard, b: McpCatalogCard) => a.title.localeCompare(b.title, undefined, { sensitivity: "base" });
  return [...items].sort((a, b) =>
    sort === "added" ? compareAddedNewestFirst(a.added_at, b.added_at) || byName(a, b) : byName(a, b),
  );
}

/**
 * The sorts the catalog supports: A–Z, and Recently added when the index dates
 * its entries. Without dates there is one option, and so no selector.
 */
function sortChoicesFor(items: readonly McpCatalogCard[], t: Translate): Array<{ value: SortKey; label: string }> {
  if (items.length < 2 || !items.some((item) => item.added_at)) return [];
  return [
    { value: "name", label: t("react.registry.mcp_catalog.sort_name", "A–Z") },
    { value: "added", label: t("react.registry.mcp_catalog.sort_added", "Recently added") },
  ];
}

/** What a card, a row and a page alike know of a server. */
type ServerTraits = Pick<McpCatalogCard, "preview_note" | "status" | "installed" | "official">;

/**
 * Preview, Deprecated, Connected and New: what a server's card, row and page
 * wear. New (`fresh`) only on a server that is not yet connected or deprecated.
 */
function serverPills(item: ServerTraits, t: Translate, { count = true, fresh = false } = {}): CatalogPill[] {
  const pills: CatalogPill[] = [];
  if (item.preview_note) pills.push({ tone: "neutral", label: t("react.registry.mcp_catalog.preview", "Preview") });
  if (item.status === "deprecated") pills.push({ tone: "orange", label: t("react.registry.mcp_catalog.deprecated", "Deprecated"), icon: "alert" });
  const installed = item.installed.length;
  if (installed) {
    pills.push({
      tone: "green",
      icon: "check",
      label:
        installed === 1 || !count
          ? t("react.registry.mcp_catalog.connected_one", "Connected")
          : t("react.registry.mcp_catalog.connected_many", "Connected ×{count}", { count: installed }),
    });
  }
  if (fresh && !installed && item.status !== "deprecated") {
    pills.push({ tone: "new", label: t("react.registry.mcp_catalog.new", "New") });
  }
  return pills;
}

/** "by Ahrefs · ✓ Official", or "Community" for a server its vendor did not publish. */
function ServerByline({ item, pills = [], publisher = true }: Readonly<{ item: ServerTraits; pills?: CatalogPill[]; publisher?: boolean }>) {
  return (
    <CatalogByline
      pills={pills}
      publisher={publisher ? (item.official?.vendor ?? "") : ""}
      trust={item.official ? "official" : "community"}
    />
  );
}

/** Connect, or Connect another once this project has one; only for whoever manages the project. */
function ConnectButton({ item, onInstall, primary = false }: Readonly<{ item: ServerTraits; onInstall: () => void; primary?: boolean }>) {
  const { t } = useI18n();
  return (
    <button className={primary ? "fs-button primary" : "fs-button sm secondary"} onClick={onInstall} type="button">
      <PlugZap aria-hidden="true" />
      {item.installed.length
        ? t("react.registry.mcp_catalog.connect_another", "Connect another")
        : t("react.registry.mcp_catalog.connect", "Connect")}
    </button>
  );
}

function CatalogCard({
  item,
  categoryLabel,
  canManage,
  fresh,
  onInstall,
  onOpen,
}: Readonly<{
  item: McpCatalogCard;
  categoryLabel: string;
  canManage: boolean;
  /** Added to the catalog within its New window. */
  fresh: boolean;
  onInstall: (item: McpCatalogCard) => void;
  onOpen: (item: McpCatalogCard) => void;
}>) {
  const { t } = useI18n();
  return (
    <CatalogItemCard
      action={canManage ? <ConnectButton item={item} onInstall={() => onInstall(item)} /> : undefined}
      byline={<ServerByline item={item} />}
      category={categoryLabel}
      chips={[]}
      data={{ "mcp-catalog-item": item.item_id }}
      icon={<McpCatalogIcon hasIcon={item.has_icon} itemId={item.item_id} title={item.title} />}
      id={`fs-mcp-catalog-${item.item_id}`}
      open={{ onOpen: () => onOpen(item) }}
      statuses={serverPills(item, t, { fresh })}
      summary={item.summary}
      title={item.title}
    />
  );
}

type CatalogTableProps = Readonly<{
  rows: readonly McpCatalogCard[];
  canManage: boolean;
  freshIds: ReadonlySet<string>;
  categoryLabel: (value: string) => string;
  onOpen: (item: McpCatalogCard) => void;
  onInstall: (item: McpCatalogCard) => void;
}>;

/**
 * Columns of the table view: Server (frozen), Category, Sign-in, Status, and
 * Connect frozen at the end for whoever manages the project; without that
 * right there is no Connect column and Status takes the room.
 */
function catalogColumns(
  { canManage, freshIds, categoryLabel, onOpen, onInstall }: Omit<CatalogTableProps, "rows">,
  t: I18nContextValue["t"] = fallbackContext.t,
): DsTableColumn<McpCatalogCard>[] {
  const columns: DsTableColumn<McpCatalogCard>[] = [
    {
      key: "server",
      header: t("react.registry.mcp_catalog.server", "Server"),
      width: 320,
      cell: (item) => (
        <CatalogTableItem
          icon={<McpCatalogIcon hasIcon={item.has_icon} itemId={item.item_id} size="row" title={item.title} />}
          id={`fs-mcp-catalog-row-${item.item_id}`}
          open={{ onOpen: () => onOpen(item) }}
          summary={item.summary}
          title={item.title}
        />
      ),
    },
    { key: "category", header: t("react.registry.mcp_catalog.category", "Category"), width: 144, cell: (item) => categoryLabel(item.category) },
    {
      key: "sign_in",
      header: t("react.registry.mcp_catalog.sign_in", "Sign-in"),
      width: 176,
      cell: (item) => (item.sign_in ?? []).map((mode) => signInLabel(mode, t)).join(", "),
    },
    {
      key: "badges",
      header: t("react.registry.mcp_catalog.status", "Status"),
      width: canManage ? 192 : undefined,
      cell: (item) => <ServerByline item={item} pills={serverPills(item, t, { count: false, fresh: freshIds.has(item.item_id) })} publisher={false} />,
    },
  ];
  if (canManage) {
    columns.push({
      key: "install",
      header: "",
      ariaLabel: t("react.registry.mcp_catalog.connect", "Connect"),
      width: 168,
      cell: (item) => <ConnectButton item={item} onInstall={() => onInstall(item)} />,
    });
  }
  return columns;
}

/** The table view: one row per server, the name opens its page, Connect stays at the end. */
function CatalogTable({ rows, ...actions }: CatalogTableProps) {
  const { t } = useI18n();
  const columns = catalogColumns(actions, t);
  return (
    <div className={`fs-card fs-table-card fs-catalog-list${actions.canManage ? " has-action" : ""}`}>
      <DsTable ariaLabel={t("react.registry.mcp_catalog.servers", "MCP servers")} columns={columns} rowKey={(item) => item.item_id} rows={rows} />
    </div>
  );
}

function registrationNote(remote: McpCatalogRemote, t: Translate): string {
  if (remote.auth.mode === "headers") {
    return t("react.registry.mcp_catalog.entry_key_note", "You paste a key or token from your account; it is stored encrypted.");
  }
  if (remote.auth.mode === "none") return t("react.registry.mcp_catalog.no_sign_in", "This server needs no sign-in.");
  if (remote.auth.client_registration === "user_supplied") {
    return t("react.registry.mcp_catalog.entry_own_client_note", "You sign in with your own OAuth client, created in the service's developer console.");
  }
  return t("react.registry.mcp_catalog.entry_oauth_note", "You sign in to the service in a new window; nothing to prepare.");
}

/**
 * One server's page, shown in place of the results: what it is, who documents
 * it, every way to connect and what each one asks for, and the connections this
 * project already made from it — all before anything is installed.
 */
function McpCatalogEntry({
  item,
  categoryLabel,
  canManage,
  fresh,
  projects,
  headingRef,
  onBack,
  onInstall,
}: Readonly<{
  item: McpCatalogItem;
  categoryLabel: string;
  canManage: boolean;
  /** Added to the catalog within its New window. */
  fresh: boolean;
  projects: readonly McpProjectOption[];
  headingRef: RefObject<HTMLHeadingElement | null>;
  onBack: () => void;
  onInstall: (item: McpCatalogItem) => void;
}>) {
  const { t } = useI18n();
  const projectName = (id: string) => projects.find((project) => project.id === id)?.name || id;
  const signIn = [...new Set(item.remotes.map((remote) => signInLabel(remote.auth.mode, t)))].join(", ");
  const docsUrl = documentationUrl(item);
  return (
    <div className="fs-marketplace-entry" data-mcp-catalog-entry={item.item_id}>
      <CatalogPageHead
        action={canManage ? <ConnectButton item={item} onInstall={() => onInstall(item)} primary /> : undefined}
        backLabel={t("react.registry.mcp_catalog.tab_catalog", "Catalog")}
        byline={<ServerByline item={item} pills={serverPills(item, t, { fresh })} />}
        headingRef={headingRef}
        icon={<McpCatalogIcon hasIcon={item.has_icon} itemId={item.item_id} size="page" title={item.title} />}
        onBack={onBack}
        summary={item.summary}
        title={item.title}
      />
      <div className="fs-catalog-details">
        <div className="fs-catalog-details-main">
          {item.description && item.description !== item.summary ? (
            <section>
              <h3 className="fs-catalog-section-title">{t("registry.workflows.simulation.description", "Description")}</h3>
              <p className="fs-catalog-prose">{item.description}</p>
            </section>
          ) : null}
          <section aria-labelledby="fs-mcp-entry-connect">
            <h3 className="fs-catalog-section-title" id="fs-mcp-entry-connect">
              {t("react.registry.mcp_catalog.entry_how_to_connect", "How you connect")}
            </h3>
            {item.preview_note ? <p className="fs-catalog-prose">{item.preview_note}</p> : null}
            <ul className="fs-catalog-connect-list">
              {item.remotes.map((remote) => (
                <li key={remote.remote_id}>
                  <p className="fs-catalog-prose">
                    <strong>{item.remotes.length > 1 ? remote.label : signInLabel(remote.auth.mode, t)}</strong>
                    {item.remotes.length > 1 ? ` · ${signInLabel(remote.auth.mode, t)}` : ""}
                    {" — "}
                    {registrationNote(remote, t)}
                  </p>
                  <code className="fs-catalog-code">{remote.url}</code>
                  {remote.user_defined_host ? (
                    <p className="fs-catalog-prose">{t("react.registry.mcp_catalog.user_defined_host", "You set this address. Flow Steward connects to the host you enter.")}</p>
                  ) : null}
                  {remote.auth.requested_scopes.length || remote.auth.setup_url ? (
                    <p className="fs-catalog-prose">
                      {remote.auth.requested_scopes.length ? (
                        <>
                          {t("react.registry.mcp_catalog.entry_scopes", "Asks for: {value0}", { value0: "" })}
                          <code>{remote.auth.requested_scopes.join(", ")}</code>
                        </>
                      ) : null}
                      {remote.auth.requested_scopes.length && remote.auth.setup_url ? " · " : null}
                      {remote.auth.setup_url ? (
                        <ExternalAnchor href={remote.auth.setup_url}>{t("react.registry.mcp_catalog.setup_guide", "Setup guide")}</ExternalAnchor>
                      ) : null}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          </section>
          {item.installed.length ? (
            <section aria-labelledby="fs-mcp-entry-connected">
              <h3 className="fs-catalog-section-title" id="fs-mcp-entry-connected">
                {t("react.registry.mcp_catalog.entry_connected_anywhere", "Connected")}
              </h3>
              <ul className="fs-marketplace-list">
                {item.installed.map((connector) => (
                  <li key={connector.connector_id}>
                    <Link
                      to={`/registry/mcp/${encodeURIComponent(connector.connector_id)}${connector.project_id ? `?project_id=${encodeURIComponent(connector.project_id)}` : ""}`}
                    >
                      {connector.display_name}
                    </Link>
                    {connector.project_id ? <span className="fs-marketplace-muted"> · {projectName(connector.project_id)}</span> : null}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
        <aside aria-label={t("react.registry.mcp_catalog.entry_facts", "Server facts")} className="fs-catalog-aside">
          <dl className="fs-kv is-bare">
            {categoryLabel ? <CatalogFact term={t("react.registry.mcp_catalog.category", "Category")}>{categoryLabel}</CatalogFact> : null}
            {signIn ? <CatalogFact term={t("react.registry.mcp_catalog.sign_in", "Sign-in")}>{signIn}</CatalogFact> : null}
            <CatalogFact term={t("react.registry.mcp_catalog.entry_server", "Server")}>
              <code>
                {item.server_name}
                {item.server_version ? ` ${item.server_version}` : ""}
              </code>
            </CatalogFact>
          </dl>
          {item.website_url || docsUrl ? (
            <div className="fs-catalog-links">
              {item.website_url ? <ExternalAnchor href={item.website_url}>{t("react.registry.mcp_catalog.entry_website", "Website")}</ExternalAnchor> : null}
              {docsUrl ? (
                <ExternalAnchor href={docsUrl}>{t("react.registry.mcp_catalog.entry_documentation", "Documentation")}</ExternalAnchor>
              ) : null}
            </div>
          ) : null}
        </aside>
      </div>
    </div>
  );
}

/**
 * The Catalog tab: the reviewed MCP servers. Install opens a pre-filled form; the result is an ordinary connector, and the
 * catalog never changes it afterwards.
 */
export function McpCatalogPanel({
  projectId,
  projects = [],
  canManage,
  onInstalled,
}: Readonly<{
  /** The project the page is filtered to, or "" for every project. */
  projectId: string;
  projects?: readonly McpProjectOption[];
  canManage: boolean;
  onInstalled: (result: McpCatalogInstallResult, item: McpCatalogItem, projectId: string) => void;
}>) {
  const { t } = useI18n();
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const [view, setView] = useState<CatalogView>("grid");
  // The table pages by its own Rows per page; the cards by PAGE_SIZE.
  const [tablePerPage, setTablePerPage] = useState<number>(CATALOG_TABLE_PAGE_SIZES[0]);
  const [sort, setSort] = useState<SortKey>("name");
  const [now] = useState(currentTime);
  const compact = useCompactCatalog();
  const countRef = useRef<HTMLHeadingElement | null>(null);
  // The id of the server whose install form is open; its full entry is fetched.
  const [installing, setInstalling] = useState<string | null>(null);
  const [params, setParams] = useSearchParams();
  const entryId = params.get("mcp_entry")?.trim() ?? "";
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const openEntry = (item: McpCatalogCard) => {
    const next = new URLSearchParams(params);
    next.set("mcp_entry", item.item_id);
    setParams(next);
  };
  const closeEntry = () => {
    const next = new URLSearchParams(params);
    next.delete("mcp_entry");
    setParams(next);
  };

  const availabilityQuery = useQuery({
    queryKey: [...MCP_CATALOG_QUERY_KEY, "availability"],
    queryFn: fetchMcpCatalogAvailability,
    retry: false,
  });
  const catalogQuery = useQuery({
    queryKey: [...MCP_CATALOG_QUERY_KEY, "all"],
    queryFn: () => fetchMcpCatalog(),
    // Only once the server says a catalog is configured: an unconfigured one is
    // not asked for at all.
    enabled: availabilityQuery.data?.configured === true,
    retry: false,
  });
  // Cards carry what a list needs; the page and the install form need every
  // endpoint and sign-in setting, fetched for the one server opened.
  const entryQuery = useQuery({
    queryKey: itemQueryKey("all", entryId),
    queryFn: () => fetchMcpCatalogItem(entryId),
    enabled: Boolean(entryId),
    retry: false,
  });
  // Focus the server's heading once its page has loaded (it is fetched on open).
  const loadedEntryId = entryQuery.data?.item_id ?? "";
  useEffect(() => {
    if (entryId && loadedEntryId === entryId) queueMicrotask(() => headingRef.current?.focus());
  }, [entryId, loadedEntryId]);
  const installQuery = useQuery({
    queryKey: itemQueryKey("all", installing ?? ""),
    queryFn: () => fetchMcpCatalogItem(installing ?? ""),
    enabled: Boolean(installing),
    retry: false,
  });

  const labels = useMemo(() => catalogQuery.data?.category_labels ?? {}, [catalogQuery.data]);
  const items = useMemo(() => catalogQuery.data?.items ?? [], [catalogQuery.data]);
  const categoryLabel = (value: string) => labels[value] ?? value;
  // New: added within 30 days, but not with the catalog's first batch.
  const freshIds = useMemo(() => {
    const earliest = earliestAddedTime(items.map((item) => item.added_at));
    return new Set(items.filter((item) => isRecentlyAdded(item.added_at, earliest, now)).map((item) => item.item_id));
  }, [items, now]);
  const sortChoices = useMemo(() => sortChoicesFor(items, t), [items, t]);
  const defs = useMemo<Array<CatalogFacetDef<McpCatalogCard, FacetKey>>>(
    () => [
      {
        key: "category",
        title: t("react.registry.mcp_catalog.category", "Category"),
        noun: t("react.registry.mcp_catalog.categories", "categories"),
        order: "total",
        valuesOf: (item) => (item.category ? [item.category] : []),
        label: (value) => labels[value] ?? value,
      },
      {
        key: "sign_in",
        title: t("react.registry.mcp_catalog.sign_in", "Sign-in"),
        noun: t("react.registry.mcp_catalog.sign_in_methods", "sign-in methods"),
        order: "fixed",
        fixed: ["oauth2", "headers", "none"],
        valuesOf: (item) => item.sign_in ?? [],
        label: (value) => signInLabel(value, t),
      },
    ],
    [labels, t],
  );
  // The part of a server name before "/" is its publisher's namespace; every
  // entry the catalog writes itself shares io.github.flow-steward, so searching
  // it ("github") matched them all. Search the name after the slash, and the entry's keywords:
  // the other names people know a service by ("Jira" finds Atlassian).
  const matchesSearch = (item: McpCatalogCard, needle: string) =>
    !needle ||
    catalogTextMatches(
      [item.title, item.summary, item.server_name.split("/").slice(1).join("/"), ...(item.keywords ?? [])],
      needle,
    );
  const facets = useMemo(() => computeCatalogFacets(items, filters, defs, matchesSearch), [items, filters, defs]);
  const visible = useMemo(
    () => sortCards(filterCatalogItems(items, filters, defs, matchesSearch), sort),
    [items, filters, defs, sort],
  );

  const retry = () => {
    ignoreAsyncResult(availabilityQuery.refetch());
    ignoreAsyncResult(catalogQuery.refetch());
  };
  const availability = availabilityQuery.data;

  if (availability && !availability.configured) {
    return (
      <CatalogStateBlock
        icon={<PlugZap aria-hidden="true" className="fs-catalog-state-icon" />}
        title={t("react.registry.mcp_catalog.not_configured", "The MCP catalog isn't set up on this server.")}
      >
        {t("react.registry.mcp_catalog.add_custom_instead", "You can still add any MCP server with New MCP.")}
      </CatalogStateBlock>
    );
  }
  if (catalogQuery.isError || availabilityQuery.isError || availability?.available === false) {
    return (
      <CatalogStateBlock
        actions={
          <button className="fs-button" onClick={retry} type="button">
            <RefreshCw aria-hidden="true" />
            {t("settings.watchdog.retry", "Retry")}
          </button>
        }
        icon={<TriangleAlert aria-hidden="true" className="fs-catalog-state-icon" />}
        title={t("react.registry.mcp_catalog.unavailable", "Couldn't load the MCP catalog.")}
      >
        {availability?.message || t("react.registry.mcp_catalog.existing_unaffected", "Connected servers are not affected.")}
      </CatalogStateBlock>
    );
  }
  if (catalogQuery.isLoading || availabilityQuery.isLoading) {
    return <PageLoadingState label={t("react.registry.mcp_catalog.loading", "Fetching the MCP catalog")} />;
  }

  const installItem = installing ? installQuery.data : undefined;
  const installDialog = installItem ? (
    <McpCatalogInstallDialog
      categoryLabel={categoryLabel(installItem.category)}
      item={installItem}
      onClose={() => setInstalling(null)}
      onInstalled={(result, item, installedProjectId) => {
        setInstalling(null);
        onInstalled(result, item, installedProjectId);
      }}
      projectId={projectId}
      projects={projects}
    />
  ) : null;
  if (entryId) {
    let page: ReactNode;
    if (entryQuery.isLoading) {
      page = <PageLoadingState label={t("react.registry.mcp_catalog.loading_entry", "Fetching the server's details")} />;
    } else if (!entryQuery.data) {
      page = (
        <CatalogStateBlock
          actions={
            <button className="fs-button" onClick={closeEntry} type="button">
              <ArrowLeft aria-hidden="true" />
              {t("react.registry.mcp_catalog.tab_catalog", "Catalog")}
            </button>
          }
          icon={<SearchX aria-hidden="true" className="fs-catalog-state-icon" />}
          title={t("react.registry.mcp_catalog.entry_missing", "This server is not in the catalog.")}
        />
      );
    } else {
      page = (
        <McpCatalogEntry
          canManage={canManage}
          categoryLabel={categoryLabel(entryQuery.data.category)}
          fresh={freshIds.has(entryQuery.data.item_id)}
          headingRef={headingRef}
          item={entryQuery.data}
          onBack={closeEntry}
          onInstall={(item) => setInstalling(item.item_id)}
          projects={projects}
        />
      );
    }
    return (
      <>
        {page}
        {installDialog}
      </>
    );
  }

  const tableView = view === "list" && !compact;
  const perPage = tableView ? tablePerPage : PAGE_SIZE;
  const pageCount = Math.max(1, Math.ceil(visible.length / perPage));
  const shownPage = Math.min(page, pageCount);
  const rows = visible.slice((shownPage - 1) * perPage, shownPage * perPage);
  const countLabel = (count: number) =>
    count === 1
      ? t("react.registry.mcp_catalog.count_one", "{count} server", { count })
      : t("react.registry.mcp_catalog.count_many", "{count} servers", { count });

  // On a phone the facets are the results head's one Add filter control.
  const railShown = !compact && facets.length > 0;
  let results: ReactNode;
  if (!items.length) {
    results = (
      <CatalogStateBlock
        icon={<PlugZap aria-hidden="true" className="fs-catalog-state-icon" />}
        title={t("react.registry.mcp_catalog.empty", "The catalog lists no servers yet.")}
      />
    );
  } else if (!visible.length) {
    results = (
      <CatalogStateBlock
        actions={
          <button className="fs-button" onClick={() => setFilters(EMPTY_FILTERS)} type="button">
            {t("react.extensions.clear_filters", "Clear filters")}
          </button>
        }
        icon={<SearchX aria-hidden="true" className="fs-catalog-state-icon" />}
        title={t("react.registry.mcp_catalog.no_match", "No servers match.")}
      />
    );
  } else {
    results = (
      <div className="fs-catalog-results-list">
        {tableView ? (
          <CatalogTable
            canManage={canManage}
            categoryLabel={categoryLabel}
            freshIds={freshIds}
            onInstall={(card) => setInstalling(card.item_id)}
            onOpen={openEntry}
            rows={rows}
          />
        ) : (
          <ul aria-label={t("react.registry.mcp_catalog.servers", "MCP servers")} className="fs-catalog-grid">
            {rows.map((item) => (
              <li key={item.item_id}>
                <CatalogCard
                  canManage={canManage}
                  categoryLabel={categoryLabel(item.category)}
                  fresh={freshIds.has(item.item_id)}
                  item={item}
                  onInstall={(card) => setInstalling(card.item_id)}
                  onOpen={openEntry}
                />
              </li>
            ))}
          </ul>
        )}
        <CatalogPager
          label={t("react.registry.mcp_catalog.pages", "MCP catalog pages")}
          onPage={(next) => {
            setPage(next);
            countRef.current?.focus();
          }}
          onPerPage={
            tableView
              ? (next) => {
                  setTablePerPage(next);
                  setPage(1);
                }
              : undefined
          }
          page={shownPage}
          perPage={perPage}
          total={visible.length}
        />
      </div>
    );
  }

  return (
    <div className={`fs-catalog-browse is-page fs-mcp-catalog${railShown ? "" : " is-single"}`}>
      {railShown ? (
        <CatalogFacetRail
          facets={facets}
          filters={filters}
          label={t("react.registry.mcp_catalog.filters", "MCP catalog filters")}
          noun={NOUN}
          onToggle={(key, value) => {
            setFilters((current) => toggleFacetValue(current, key, value));
            setPage(1);
          }}
          renderCountLabel={countLabel}
        />
      ) : null}
      <div aria-busy={catalogQuery.isFetching} className="fs-catalog-results">
        <label className="fs-search fs-marketplace-search">
          <Search aria-hidden="true" />
          <input
            aria-label={t("react.registry.mcp_catalog.search", "Search the MCP catalog")}
            onChange={(event) => {
              const q = event.target.value;
              setFilters((current) => ({ ...current, q }));
              setPage(1);
            }}
            placeholder={t("react.registry.mcp_catalog.search_placeholder", "Search services…")}
            type="search"
            value={filters.q}
          />
        </label>
        {items.length ? (
          <CatalogResultsHead
            compact={compact}
            countLabel={countLabel(visible.length)}
            countRef={countRef}
            facets={facets}
            filters={filters}
            notice={
              availability?.stale ? (
                <DsAlert title={t("react.registry.mcp_catalog.stale", "Showing the last catalog we could load. The latest refresh failed.")} tone="warning" />
              ) : null
            }
            onClearAll={() => {
              setFilters((current) => clearFacetValues(current, FACET_KEYS));
              setPage(1);
            }}
            onClearFacet={(key) => {
              setFilters((current) => clearFacetValues(current, [key]));
              setPage(1);
            }}
            onSort={(next) => {
              setSort(next);
              setPage(1);
            }}
            onToggle={(key, value) => {
              setFilters((current) => toggleFacetValue(current, key, value));
              setPage(1);
            }}
            onView={(next) => {
              setView(next);
              setPage(1);
            }}
            sort={sort}
            sortChoices={sortChoices}
            view={view}
          />
        ) : null}
        {results}
      </div>
      {installDialog}
    </div>
  );
}
