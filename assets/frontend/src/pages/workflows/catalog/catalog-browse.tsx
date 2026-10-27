import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { type ReactNode, type RefObject, useState } from "react";

import { Plug, RefreshCw, SearchX, TriangleAlert } from "lucide-react";

import {
  CatalogByline,
  CatalogChipRow,
  CatalogFacetRail,
  CatalogIcon,
  CatalogItemCard,
  CatalogPager,
  CatalogPills,
  CatalogResultsHead,
  CatalogStateBlock,
  CatalogSuggestLine,
  CatalogTableItem,
} from "@/components/shared/catalog-browse";
import { DsAlert, DsTable, type DsTableColumn } from "@/components/shared/design-system";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { CATALOG_TABLE_PAGE_SIZES, clearFacetValues, toggleFacetValue } from "@/lib/utils/catalog-facets";
import type { WorkflowCatalogItem } from "@/pages/workflows/catalog/api";
import {
  categoryName,
  unavailableReason,
  type CatalogFilters,
  type CatalogLabels,
  type CatalogSort,
  type CatalogView,
  type Facet,
  FACET_KEYS,
  type FacetKey,
  integrationName,
  PAGE_SIZE,
  SUGGEST_TEMPLATE_URL,
  templatePill,
  TRIGGER_ICONS,
  triggerName,
} from "@/pages/workflows/catalog/catalog-model";

/** What the browse stage can show instead of results. */
export type BrowseState = "loading" | "unavailable" | "not-configured" | "ready";

export type BrowseContext = {
  labels: CatalogLabels;
  installed: ReadonlySet<string> | null;
  now: number;
};


export function TriggerLabel({ item }: Readonly<{ item: WorkflowCatalogItem }>) {
  const { t } = useI18n();
  const first = item.trigger_types[0];
  if (!first) return null;
  const Icon = TRIGGER_ICONS[first] ?? Plug;
  const more = item.trigger_types.length - 1;
  return (
    <span className="fs-catalog-trigger">
      <Icon aria-hidden="true" />
      <span>
        {triggerName(first, t)}
        {more > 0 ? ` +${more}` : ""}
      </span>
    </span>
  );
}

/** Integrations a filter asked for come first on a card. */
function orderedIntegrations(item: WorkflowCatalogItem, active: readonly string[], labels: CatalogLabels) {
  const first = item.integrations.filter((id) => active.includes(id));
  const rest = item.integrations.filter((id) => !active.includes(id));
  return [...first, ...rest].map((id) => integrationName(labels, id));
}

function includesLine(item: WorkflowCatalogItem, t: I18nContextValue["t"] = fallbackContext.t): string {
  const count = item.includes?.length ?? 0;
  if (!count) return "";
  return count === 1
    ? t("react.workflows.catalog_includes_workflow", "Includes {count} workflow", { count })
    : t("react.workflows.catalog_includes_workflows", "Includes {count} workflows", { count });
}

/** A template's tile: the flow glyph (the index names no logo). */
export function TemplateIcon({ item, size }: Readonly<{ item: WorkflowCatalogItem; size?: "row" | "card" | "page" }>) {
  return <CatalogIcon kind="workflow" name={item.name} size={size} />;
}

type CardProps = Readonly<{
  item: WorkflowCatalogItem;
  context: BrowseContext;
  activeIntegrations: readonly string[];
  onOpen: (itemId: string) => void;
  /** Use the template straight from the results; absent for someone who only browses. */
  onUse?: (itemId: string) => void;
}>;

/** "Use template" beside a card or in a table row, so a known template is one click away. */
function UseTemplateButton({ item, onUse }: Readonly<{ item: WorkflowCatalogItem; onUse?: (itemId: string) => void }>) {
  const { t } = useI18n();
  if (!onUse) return null;
  const unavailable = unavailableReason(item, t);
  return (
    <button
      aria-label={`${t("react.workflows.use_template", "Use template")}: ${item.name}`}
      className="fs-button sm secondary"
      data-catalog-use={item.item_id}
      disabled={Boolean(unavailable)}
      title={unavailable || undefined}
      onClick={(event) => {
        event.stopPropagation();
        onUse(item.item_id);
      }}
      type="button"
    >
      {t("react.workflows.use_template", "Use template")}
    </button>
  );
}

function TemplateCard({ item, context, activeIntegrations, onOpen, onUse }: CardProps) {
  const { t } = useI18n();
  const pill = templatePill(item, context, t);
  // A template this Core cannot import says why in its card, in place of the includes line.
  const unavailable = unavailableReason(item, t);
  const includes = unavailable || includesLine(item, t);
  return (
    <CatalogItemCard
      action={<UseTemplateButton item={item} onUse={onUse} />}
      byline={<CatalogByline trigger={<TriggerLabel item={item} />} />}
      category={categoryName(context.labels, item.categories[0] ?? "")}
      chips={orderedIntegrations(item, activeIntegrations, context.labels)}
      data={{ "catalog-card": item.item_id }}
      icon={<TemplateIcon item={item} />}
      id={`fs-catalog-card-${item.item_id}`}
      notes={
        includes ? (
          <span
            className="fs-catalog-card-includes"
            data-catalog-includes={unavailable ? undefined : ""}
            data-catalog-unavailable={unavailable ? "" : undefined}
          >
            {includes}
          </span>
        ) : null
      }
      open={{ onOpen: () => onOpen(item.item_id), data: { "catalog-item": item.item_id } }}
      statuses={pill ? [pill] : []}
      summary={item.summary}
      title={item.name}
      version={item.version ? `v${item.version}` : ""}
    />
  );
}

/** The phone layout's row: tile, name and its pill, trigger, two lines of summary, integrations. */
function TemplateRow({ item, context, activeIntegrations, onOpen }: CardProps) {
  const { t } = useI18n();
  const pill = templatePill(item, context, t);
  const includes = unavailableReason(item, t) || includesLine(item, t);
  return (
    <button
      className="fs-catalog-row"
      data-catalog-item={item.item_id}
      onClick={() => onOpen(item.item_id)}
      type="button"
    >
      <span className="fs-catalog-icon-lockup">
        <TemplateIcon item={item} size="row" />
        <span className="fs-catalog-icon-lockup-text">
          <span className="fs-catalog-row-title">
            <span className="fs-catalog-row-name">{item.name}</span>
            <CatalogPills pills={pill ? [pill] : []} />
          </span>
          <CatalogByline trigger={<TriggerLabel item={item} />} />
        </span>
      </span>
      <span className="fs-catalog-card-summary">{item.summary}</span>
      {includes ? <span className="fs-catalog-card-includes">{includes}</span> : null}
      <span className="fs-catalog-card-meta">
        <CatalogChipRow labels={orderedIntegrations(item, activeIntegrations, context.labels)} />
      </span>
    </button>
  );
}

/**
 * The table's columns: Template (frozen), Status, Integrations, Trigger, and
 * Use template frozen at the end for whoever may create workflows; without
 * that right there is no such column and Status takes the room.
 */
function listColumns(
  context: BrowseContext,
  activeIntegrations: readonly string[],
  onOpen: (itemId: string) => void,
  onUse?: (itemId: string) => void,
  t: I18nContextValue["t"] = fallbackContext.t,
): DsTableColumn<WorkflowCatalogItem>[] {
  const use: DsTableColumn<WorkflowCatalogItem>[] = onUse
    ? [{ key: "use", header: "", ariaLabel: t("react.workflows.use_template", "Use template"), width: 168, cell: (item) => <UseTemplateButton item={item} onUse={onUse} /> }]
    : [];
  return [
    {
      key: "template",
      header: t("react.workflows.catalog_column_template", "Template"),
      width: 320,
      cell: (item) => (
        <CatalogTableItem
          icon={<TemplateIcon item={item} size="row" />}
          id={`fs-catalog-row-${item.item_id}`}
          open={{ onOpen: () => onOpen(item.item_id), data: { "catalog-item": item.item_id } }}
          summary={item.summary}
          title={item.name}
        />
      ),
    },
    {
      key: "status",
      header: t("extensions.runtime.status", "Status"),
      width: onUse ? 192 : undefined,
      cell: (item) => {
        const pill = templatePill(item, context, t);
        return pill ? <CatalogPills pills={[pill]} /> : null;
      },
    },
    {
      key: "integrations",
      header: t("react.workflows.catalog_column_integrations", "Integrations"),
      width: 192,
      cell: (item) => <CatalogChipRow labels={orderedIntegrations(item, activeIntegrations, context.labels)} />,
    },
    { key: "trigger", header: t("react.workflows.trigger", "Trigger"), width: 144, cell: (item) => <TriggerLabel item={item} /> },
    ...use,
  ];
}

function SuggestLine() {
  const { t } = useI18n();
  return <CatalogSuggestLine href={SUGGEST_TEMPLATE_URL} prompt={t("react.workflows.catalog_suggest_prompt", "Don't see the template you need?")} />;
}

function SkeletonResults() {
  const { t } = useI18n();
  const visible = useLoaderDelay();
  return (
    <div role="status">
      <span className="fs-sr-only">{t("react.workflows.loading_templates", "Loading templates")}</span>
      {visible ? (
        <ul aria-hidden="true" className="fs-catalog-grid">
          {[0, 1, 2, 3, 4, 5].map((index) => (
            <li className="fs-card fs-catalog-card fs-skeleton-lines" key={index}>
              <span className="fs-skeleton fs-skeleton-line" style={{ width: "60%" }} />
              <span className="fs-skeleton fs-skeleton-line" />
              <span className="fs-skeleton fs-skeleton-line" style={{ width: "40%" }} />
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export type BrowseProps = Readonly<{
  state: BrowseState;
  errorText: string;
  stale: boolean;
  staleSince: string;
  busy: boolean;
  items: readonly WorkflowCatalogItem[];
  visible: readonly WorkflowCatalogItem[];
  facets: Facet[];
  filters: CatalogFilters;
  sort: CatalogSort;
  sortChoices: Array<{ value: CatalogSort; label: string }>;
  page: number;
  view: CatalogView;
  compact: boolean;
  readOnly: boolean;
  context: BrowseContext;
  countRef: RefObject<HTMLHeadingElement | null>;
  /** Takes an update of the latest filters, never a snapshot of them. */
  onFilters: (update: (current: CatalogFilters) => CatalogFilters) => void;
  onSort: (sort: CatalogSort) => void;
  onPage: (page: number) => void;
  onView: (view: CatalogView) => void;
  onOpen: (itemId: string) => void;
  /** Use a template straight from the results (grid and list); absent when browsing only. */
  onUse?: (itemId: string) => void;
  onRetry: () => void;
  onStartScratch: () => void;
  /**
   * The search field, when the catalog is a page tab: it then heads the
   * results column, and the rail is a column of the page, not a scroller.
   */
  search?: ReactNode;
}>;

export function CatalogBrowse(props: BrowseProps) {
  const { t } = useI18n();
  const { facets, filters, onFilters, compact, state } = props;
  const countLabel = (count: number) => count === 1
    ? t("react.workflows.catalog_template_count_one", "{count} template", { count })
    : t("react.workflows.catalog_template_count_many", "{count} templates", { count });
  const toggle = (key: FacetKey, value: string) => onFilters((current) => toggleFacetValue(current, key, value));
  const clearFacet = (key: FacetKey) => onFilters((current) => ({ ...current, [key]: [] }));
  const clearAll = () => onFilters((current) => clearFacetValues(current, FACET_KEYS));
  const railShown = !compact && state === "ready" && facets.length > 0;
  return (
    <div className={`fs-catalog-browse${props.search ? " is-page" : ""}${railShown ? "" : " is-single"}`}>
      {railShown ? (
        <CatalogFacetRail
          facets={facets}
          filters={filters}
          label={t("react.workflows.template_filters", "Template filters")}
          noun={[t("react.workflows.catalog_template_one", "template"), t("react.workflows.catalog_template_many", "templates")]}
          renderCountLabel={countLabel}
          onToggle={toggle}
        />
      ) : null}
      <div aria-busy={props.busy} className="fs-catalog-results" id="fs-catalog-results">
        {props.search ? <div className="fs-catalog-searchfield">{props.search}</div> : null}
        {state === "ready" ? (
          <CatalogResultsHead
            compact={compact}
            countLabel={countLabel(props.visible.length)}
            countRef={props.countRef}
            facets={facets}
            filters={filters}
            notice={
              props.stale ? (
                <DsAlert
                  title={
                    props.staleSince
                      ? t("react.workflows.showing_the_catalog_as_of_value0", "Showing the catalog as of {value0}", { value0: props.staleSince })
                      : t("react.workflows.showing_the_last_catalog_we_could_load", "Showing the last catalog we could load")
                  }
                  tone="warning"
                >
                  {t("react.workflows.the_last_refresh_failed_everything_here_still_works", "The last refresh failed. Everything here still works.")}</DsAlert>
              ) : null
            }
            onClearAll={clearAll}
            onClearFacet={clearFacet}
            onSort={props.onSort}
            onToggle={toggle}
            onView={props.onView}
            sort={props.sort}
            sortChoices={props.sortChoices}
            view={props.view}
          />
        ) : null}
        <Results {...props} clearAll={clearAll} toggle={toggle} />
      </div>
    </div>
  );
}

type ResultsExtras = {
  clearAll: () => void;
  toggle: (key: FacetKey, value: string) => void;
};

function NoResults({
  facets,
  filters,
  readOnly,
  onFilters,
  onStartScratch,
  clearAll,
  toggle,
}: BrowseProps & ResultsExtras) {
  const { t } = useI18n();
  const lastKey = [...FACET_KEYS].reverse().find((key) => filters[key].length);
  const lastValue = lastKey ? filters[lastKey].at(-1) : undefined;
  const lastFacet = facets.find((facet) => facet.key === lastKey);
  const lastLabel = lastValue && lastFacet ? lastFacet.label(lastValue) : "";
  const parts: string[] = [];
  if (filters.q.trim()) parts.push(`“${filters.q.trim()}”`);
  for (const facet of facets) {
    if (filters[facet.key].length) parts.push(filters[facet.key].map((value) => facet.label(value)).join(t("react.workflows.catalog_or_separator", " or ")));
  }
  const hasFacets = FACET_KEYS.some((key) => filters[key].length);
  return (
    <CatalogStateBlock
      actions={
        <>
          {lastKey && lastValue ? (
            <button className="fs-button" onClick={() => toggle(lastKey, lastValue)} type="button">
              {t("extensions.repeater.remove", "Remove")}{" "}{lastLabel}
            </button>
          ) : null}
          {filters.q ? (
            <button className="fs-button" onClick={() => onFilters((current) => ({ ...current, q: "" }))} type="button">
              {t("react.workflows.clear_search", "Clear search")}</button>
          ) : null}
          {hasFacets ? (
            <button className="fs-button" onClick={clearAll} type="button">
              {t("react.workflows.clear_all", "Clear all")}</button>
          ) : null}
          {readOnly ? null : (
            <button className="fs-button" onClick={onStartScratch} type="button">
              {t("react.workflows.start_from_scratch", "Start from scratch")}</button>
          )}
        </>
      }
      icon={<SearchX aria-hidden="true" className="fs-catalog-state-icon" />}
      title={t("react.workflows.no_templates_match", "No templates match")}
    >
      {parts.length ? t("react.workflows.nothing_matches_value0", "Nothing matches {value0}.", { value0: parts.join(t("react.workflows.catalog_with_separator", " with ")) }) : t("react.workflows.the_catalog_has_no_templates_yet", "The catalog has no templates yet.")}
    </CatalogStateBlock>
  );
}

function Results(props: BrowseProps & ResultsExtras) {
  const { t } = useI18n();
  const { state, visible, page, view, compact, context, filters, onOpen, onUse, onPage, readOnly } = props;
  // The table pages by its own Rows per page; the cards by PAGE_SIZE.
  const [tablePerPage, setTablePerPage] = useState<number>(CATALOG_TABLE_PAGE_SIZES[0]);
  if (state === "loading") return <SkeletonResults />;
  if (state === "not-configured") {
    return (
      <CatalogStateBlock
        actions={
          readOnly ? null : (
            <button className="fs-button" onClick={props.onStartScratch} type="button">
              {t("react.workflows.start_from_scratch", "Start from scratch")}</button>
          )
        }
        icon={<TriangleAlert aria-hidden="true" className="fs-catalog-state-icon" />}
        title={t("react.workflows.no_template_catalog_is_configured", "No template catalog is configured")}
      >
        {readOnly
          ? t("react.workflows.this_installation_has_no_template_catalog", "This installation has no template catalog.")
          : t("react.workflows.this_installation_has_no_template_catalog_starting_from_scratch_and_importing_yaml_still_work", "This installation has no template catalog. Starting from scratch and importing YAML still work.")}
      </CatalogStateBlock>
    );
  }
  if (state === "unavailable") {
    return (
      <CatalogStateBlock
        actions={
          <>
            <button className="fs-button" onClick={props.onRetry} type="button">
              <RefreshCw aria-hidden="true" />
              {t("settings.watchdog.retry", "Retry")}</button>
            {readOnly ? null : (
              <button className="fs-button" onClick={props.onStartScratch} type="button">
                {t("react.workflows.start_from_scratch", "Start from scratch")}</button>
            )}
          </>
        }
        icon={<TriangleAlert aria-hidden="true" className="fs-catalog-state-icon" />}
        title={t("react.workflows.the_template_catalog_is_unavailable_right_now", "The template catalog is unavailable right now")}
      >
        {props.errorText} {" "}{t("react.workflows.nothing_on_this_installation_is_affected", "Nothing on this installation is affected")}{readOnly ? t("react.workflows.templates_will_be_back_when_the_catalog_host_answers", "; templates will be back when the catalog host answers.") : t("react.workflows.you_can_still_start_from_scratch_or_import_yaml", "; you can still start from scratch or import YAML.")}
      </CatalogStateBlock>
    );
  }
  if (!visible.length) {
    return (
      <>
        <NoResults {...props} />
        <SuggestLine />
      </>
    );
  }
  const tableView = view === "list" && !compact;
  const perPage = tableView ? tablePerPage : PAGE_SIZE;
  const pageCount = Math.max(1, Math.ceil(visible.length / perPage));
  const safePage = Math.min(page, pageCount);
  const rows = visible.slice((safePage - 1) * perPage, safePage * perPage);
  const active = filters.integration;
  const use = readOnly ? undefined : onUse;
  let list: ReactNode;
  if (compact) {
    list = (
      <ul aria-label={t("react.workflows.templates", "Templates")} className="fs-catalog-rows">
        {rows.map((item) => (
          <li key={item.item_id}>
            <TemplateRow activeIntegrations={active} context={context} item={item} onOpen={onOpen} />
          </li>
        ))}
      </ul>
    );
  } else if (tableView) {
    list = (
      <div className={`fs-card fs-table-card fs-catalog-list${use ? " has-action" : ""}`}>
        <DsTable
          ariaLabel={t("react.workflows.templates", "Templates")}
          columns={listColumns(context, active, onOpen, use, t)}
          onRowClick={(item) => onOpen(item.item_id)}
          rowClickFocusable={false}
          rowKey={(item) => item.item_id}
          rows={rows}
        />
      </div>
    );
  } else {
    list = (
      <ul aria-label={t("react.workflows.templates", "Templates")} className="fs-catalog-grid">
        {rows.map((item) => (
          <li key={item.item_id}>
            <TemplateCard activeIntegrations={active} context={context} item={item} onOpen={onOpen} onUse={use} />
          </li>
        ))}
      </ul>
    );
  }
  return (
    <div className="fs-catalog-results-list">
      {list}
      <CatalogPager
        label={t("react.workflows.template_pages", "Template pages")}
        onPage={onPage}
        onPerPage={
          tableView
            ? (next) => {
                setTablePerPage(next);
                onPage(1);
              }
            : undefined
        }
        page={safePage}
        perPage={perPage}
        total={visible.length}
      />
      <SuggestLine />
    </div>
  );
}
