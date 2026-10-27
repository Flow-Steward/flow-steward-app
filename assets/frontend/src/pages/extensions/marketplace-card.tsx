import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
/*
 * One extension in the catalog results: a card in the grid, a row on a phone,
 * a row of the table. Each names one link to the extension's page; a plain
 * click opens the page in place, a click that asks for a new tab keeps the
 * browser's behaviour. Install sits beside the link (never inside it), so a
 * user who knows what they want installs straight from the results; who may
 * not install sees no button, and the extension's state takes its place.
 */
import { type MouseEvent, type ReactNode } from "react";

import { useHref } from "react-router";

import {
  CatalogByline,
  CatalogChipRow,
  CatalogIcon,
  CatalogItemCard,
  type CatalogOpen,
  CatalogPills,
  CatalogTableItem,
} from "@/components/shared/catalog-browse";
import { DsTable, type DsTableColumn } from "@/components/shared/design-system";
import type { ExtensionCatalogEntry, ExtensionCatalogInstallState } from "@/pages/extensions/catalog-api";
import {
  type BrowseContext,
  categoryName,
  describeEntry,
  entryPath,
  incompatibleReason,
  installLabel,
  installNotice,
  lifecycleBadge,
  mainAction,
  publisherOf,
  updateLabel,
  canInstallFromResults,
  marketplaceBadges,
  orderedCapabilities,
  stateBadge,
  versionText,
} from "@/pages/extensions/marketplace-model";

export type ResultContext = BrowseContext & {
  /** Capability ids an active filter asked for; they come first in the chips. */
  activeCapabilities: readonly string[];
  onOpen: (extensionId: string) => void;
  /** Installing from the results, with the same rules as the extension's page. */
  install?: {
    canAdminister: boolean;
    installBlockedReason: string;
    stateById: Readonly<Record<string, ExtensionCatalogInstallState>>;
    onInstall: (entry: ExtensionCatalogEntry) => void;
  };
};

/**
 * Install or Update for one entry, when the page's main action would be one
 * and is allowed; nothing otherwise (the page explains why it is not).
 */
function quickAction(entry: ExtensionCatalogEntry, context: ResultContext, t: I18nContextValue["t"]) {
  const install = context.install;
  if (!install) return null;
  const action = mainAction(entry, {
    canAdminister: install.canAdminister,
    installBlockedReason: install.installBlockedReason,
    installationVersion: context.installationVersion,
  }, t);
  if ((action.kind !== "install" && action.kind !== "update") || action.disabled) return null;
  return action.kind;
}

function QuickInstall({ entry, context }: Readonly<{ entry: ExtensionCatalogEntry; context: ResultContext }>) {
  const { t } = useI18n();
  const kind = quickAction(entry, context, t);
  const install = context.install;
  if (!kind || !install) return null;
  const phase = install.stateById[entry.extension_id]?.phase ?? "idle";
  const label = kind === "update" ? updateLabel(phase, entry.version, t) : installLabel(phase, t);
  return (
    <button
      aria-busy={phase === "installing" || undefined}
      aria-label={`${label}: ${entry.name}`}
      className="fs-button sm secondary"
      data-marketplace-quick-install={entry.extension_id}
      disabled={phase !== "idle"}
      onClick={(event) => {
        event.stopPropagation();
        install.onInstall(entry);
      }}
      type="button"
    >
      {label}
    </button>
  );
}

function categoryText(entry: ExtensionCatalogEntry, context: ResultContext) {
  return entry.categories.map((id) => categoryName(context.labels, id)).join(", ");
}

/** The extension's icon.png the catalog serves; without one, the Lucide package, whoever publishes it. */
export function EntryIcon({ entry, size }: Readonly<{ entry: ExtensionCatalogEntry; size?: "row" | "card" | "page" }>) {
  return <CatalogIcon kind="extension" name={entry.name} size={size} src={entry.icon_url} />;
}

/** "by Flow-Steward [Verified]": who made it, so two extensions of one name are told apart. */
export function Publisher({
  entry,
  version = false,
  pills = [],
}: Readonly<{ entry: ExtensionCatalogEntry; version?: boolean; pills?: ReturnType<typeof marketplaceBadges> }>) {
  const publisher = publisherOf(entry);
  return (
    <CatalogByline
      pills={pills}
      publisher={publisher?.name ?? ""}
      trust={publisher?.verified ? "verified" : "none"}
      version={version ? versionText(entry) : ""}
    />
  );
}

/** The link to the extension's page, with what a screen reader hears after the name. */
function useEntryOpen(entry: ExtensionCatalogEntry, context: ResultContext): CatalogOpen {
  const { t } = useI18n();
  return {
    href: useHref(entryPath(entry.extension_id)),
    onOpen: () => context.onOpen(entry.extension_id),
    description: describeEntry(entry, context, t),
    data: { "marketplace-entry": entry.extension_id },
  };
}

function ReasonLine({ entry, context }: Readonly<{ entry: ExtensionCatalogEntry; context: ResultContext }>) {
  const { t } = useI18n();
  const reason = incompatibleReason(entry, context.installationVersion, t);
  return reason ? <span className="fs-marketplace-reason">{reason.short}</span> : null;
}

/**
 * An install or update started from the results that did not happen, drawn on
 * the card, the phone row and the table row alike, where the button was
 * pressed; the results announce it once (`InstallAnnouncements`).
 */
function InstallNotice({ entry, context }: Readonly<{ entry: ExtensionCatalogEntry; context: ResultContext }>) {
  const notice = installNotice(context.install?.stateById[entry.extension_id]);
  if (!notice) return null;
  return (
    <span
      className={`fs-marketplace-install-notice${notice.failed ? " is-failed" : ""}`}
      data-marketplace-install-notice={entry.extension_id}
    >
      {notice.text}
    </span>
  );
}

/**
 * The screen-reader side of `InstallNotice`: a card's own text sits inside its
 * link, which is named by its title, so the outcome is announced here instead.
 */
export function InstallAnnouncements({
  entries,
  stateById,
}: Readonly<{
  entries: readonly ExtensionCatalogEntry[];
  stateById: Readonly<Record<string, ExtensionCatalogInstallState>>;
}>) {
  const lines = entries
    .map((entry) => ({ entry, notice: installNotice(stateById[entry.extension_id]) }))
    .filter((line) => line.notice !== null);
  // Inserted with its text, as DsAlert is: an alert that appears is announced.
  if (!lines.length) return null;
  return (
    <div className="fs-sr-only" role="alert">
      {lines.map(({ entry, notice }) => (
        <p key={entry.extension_id}>{`${entry.name}: ${notice?.text ?? ""}`}</p>
      ))}
    </div>
  );
}

function Notes({ entry, context }: Readonly<{ entry: ExtensionCatalogEntry; context: ResultContext }>) {
  const { t } = useI18n();
  const reason = incompatibleReason(entry, context.installationVersion, t);
  const notice = installNotice(context.install?.stateById[entry.extension_id]);
  if (!reason && !notice) return null;
  return (
    <span className="fs-catalog-card-notes">
      <ReasonLine context={context} entry={entry} />
      <InstallNotice context={context} entry={entry} />
    </span>
  );
}

/**
 * Tile, name and version, byline, two lines of summary, capability chips with
 * New or Deprecated on the right, then category and Install, Update — or the
 * state in its place: Installed, Disabled, Incompatible, Update available.
 */
export function MarketplaceCard({ entry, context }: Readonly<{ entry: ExtensionCatalogEntry; context: ResultContext }>) {
  const { t } = useI18n();
  const open = useEntryOpen(entry, context);
  const lifecycle = lifecycleBadge(entry, context.earliest, context.now, t);
  const action = quickAction(entry, context, t);
  return (
    <CatalogItemCard
      action={<QuickInstall context={context} entry={entry} />}
      byline={<Publisher entry={entry} />}
      category={categoryText(entry, context)}
      chips={orderedCapabilities(entry, context.activeCapabilities, context.labels)}
      data={{ "marketplace-card": entry.extension_id }}
      icon={<EntryIcon entry={entry} />}
      id={`fs-marketplace-card-${entry.extension_id}`}
      notes={<Notes context={context} entry={entry} />}
      open={open}
      state={action ? null : stateBadge(entry, t)}
      statuses={lifecycle ? [lifecycle] : []}
      summary={entry.summary}
      title={entry.name}
      version={versionText(entry)}
    />
  );
}

/** The link a phone row is. */
function EntryLink({
  entry,
  context,
  className,
  children,
}: Readonly<{ entry: ExtensionCatalogEntry; context: ResultContext; className: string; children: (titleId: string) => ReactNode }>) {
  const open = useEntryOpen(entry, context);
  const titleId = `fs-marketplace-row-title-${entry.extension_id}`;
  const descriptionId = `fs-marketplace-row-description-${entry.extension_id}`;
  return (
    <a
      aria-describedby={descriptionId}
      aria-labelledby={titleId}
      className={className}
      data-marketplace-entry={entry.extension_id}
      href={open.href}
      onClick={(event: MouseEvent<HTMLAnchorElement>) => {
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        open.onOpen();
      }}
    >
      {children(titleId)}
      <span className="fs-sr-only" id={descriptionId}>
        {open.description}
      </span>
    </a>
  );
}

/** The phone's row: tile, name with its pills, byline, two lines of summary, category and chips. */
export function MarketplaceRow({ entry, context }: Readonly<{ entry: ExtensionCatalogEntry; context: ResultContext }>) {
  const { t } = useI18n();
  return (
    <EntryLink className="fs-catalog-row" context={context} entry={entry}>
      {(titleId) => (
        <>
          <span className="fs-catalog-icon-lockup">
            <EntryIcon entry={entry} size="row" />
            <span className="fs-catalog-icon-lockup-text">
              <span className="fs-catalog-row-title">
                <span className="fs-catalog-row-name" id={titleId}>
                  {entry.name}
                </span>
                <span className="fs-catalog-card-version">{versionText(entry)}</span>
                <CatalogPills pills={marketplaceBadges(entry, context.earliest, context.now, t)} />
              </span>
              <Publisher entry={entry} />
            </span>
          </span>
          <span className="fs-catalog-card-summary">{entry.summary}</span>
          <span className="fs-catalog-card-meta">
            <span className="fs-catalog-card-category">{categoryText(entry, context)}</span>
            <CatalogChipRow labels={orderedCapabilities(entry, context.activeCapabilities, context.labels)} />
          </span>
          <ReasonLine context={context} entry={entry} />
          <InstallNotice context={context} entry={entry} />
        </>
      )}
    </EntryLink>
  );
}

function ExtensionCell({ entry, context }: Readonly<{ entry: ExtensionCatalogEntry; context: ResultContext }>) {
  const open = useEntryOpen(entry, context);
  return (
    <CatalogTableItem
      byline={<Publisher entry={entry} />}
      icon={<EntryIcon entry={entry} size="row" />}
      id={`fs-marketplace-list-${entry.extension_id}`}
      open={open}
      summary={entry.summary}
      title={entry.name}
      version={versionText(entry)}
    />
  );
}

/**
 * The table's columns: Extension (frozen) with the row's one link, Status
 * second so it stays in view where the columns scroll, Category,
 * Capabilities with its full list for assistive technology, and Install
 * frozen at the end — only for whoever may install.
 */
function marketplaceColumns(context: ResultContext, t: I18nContextValue["t"] = fallbackContext.t): DsTableColumn<ExtensionCatalogEntry>[] {
  const canInstall = canInstallFromResults(context.install);
  const columns: DsTableColumn<ExtensionCatalogEntry>[] = [
    {
      key: "extension",
      header: t("extension_page.extension_fallback", "Extension"),
      width: 320,
      cell: (entry) => <ExtensionCell context={context} entry={entry} />,
    },
    {
      key: "status",
      header: t("extensions.runtime.status", "Status"),
      width: canInstall ? 192 : undefined,
      cell: (entry) => {
        const pills = marketplaceBadges(entry, context.earliest, context.now, t);
        return (
          <span className="fs-marketplace-status-cell">
            {pills.length ? <CatalogPills pills={pills} /> : <span className="fs-catalog-list-muted">{t("react.extensions.not_installed", "Not installed")}</span>}
            <ReasonLine context={context} entry={entry} />
          </span>
        );
      },
    },
    { key: "category", header: t("trash.columns.category", "Category"), width: 144, cell: (entry) => categoryText(entry, context) },
    {
      key: "capabilities",
      header: t("react.extensions.capabilities", "Capabilities"),
      width: 240,
      cell: (entry) => {
        const labels = orderedCapabilities(entry, context.activeCapabilities, context.labels);
        return (
          <>
            <span aria-hidden="true">
              <CatalogChipRow labels={labels} />
            </span>
            <span className="fs-sr-only">{labels.join(", ")}</span>
          </>
        );
      },
    },
  ];
  if (canInstall) {
    columns.push({
      key: "install",
      header: "",
      ariaLabel: t("react.extensions.install", "Install"),
      width: 168,
      cell: (entry) => (
        <span className="fs-marketplace-install-cell">
          <QuickInstall context={context} entry={entry} />
          <InstallNotice context={context} entry={entry} />
        </span>
      ),
    });
  }
  return columns;
}

/** The table view: the Advanced data table; a pointer may click anywhere on a row. */
export function MarketplaceTable({
  rows,
  context,
}: Readonly<{ rows: readonly ExtensionCatalogEntry[]; context: ResultContext }>) {
  const { t } = useI18n();
  return (
    <div className={`fs-card fs-table-card fs-catalog-list fs-marketplace-table${canInstallFromResults(context.install) ? " has-action" : ""}`}>
      <DsTable
        ariaLabel={t("settings.tab.extensions", "Extensions")}
        columns={marketplaceColumns(context, t)}
        onRowClick={(entry) => context.onOpen(entry.extension_id)}
        rowClickFocusable={false}
        rowKey={(entry) => entry.extension_id}
        rows={rows}
      />
    </div>
  );
}
