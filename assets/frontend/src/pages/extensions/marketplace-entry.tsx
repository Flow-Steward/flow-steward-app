import { useI18n } from "@/lib/i18n/useI18n";
/*
 * One extension's page, drawn in place of the Marketplace results: what it
 * adds and the permissions it asks for, read before the one main action next
 * to them. Install and Update keep the page's existing catalog install call
 * and states, and the browser-authority approval modal the page already owns.
 */
import { type RefObject, useEffect, useRef } from "react";

import { ExternalLink } from "lucide-react";
import { Link } from "react-router";

import { CatalogFact, CatalogPageHead } from "@/components/shared/catalog-browse";
import { DsAlert, DsBadge } from "@/components/shared/design-system";
import { EntryIcon, Publisher } from "@/pages/extensions/marketplace-card";
import type { ExtensionCatalogEntry, ExtensionCatalogInstallState } from "@/pages/extensions/catalog-api";
import {
  type BrowseContext,
  capabilityName,
  categoryName,
  dateLabel,
  mainAction,
  marketplaceBadges,
  orderedPermissions,
  permissionName,
  installLabel,
  publisherOf,
  updateLabel,
  sizeLabel,
  worksWith,
} from "@/pages/extensions/marketplace-model";

const NOTE_ID = "fs-marketplace-action-note";
const ADDED_ID = "fs-marketplace-added-permissions";



export function MarketplaceEntry({
  entry,
  context,
  install,
  canAdminister,
  installBlockedReason,
  headingRef,
  onBack,
  onInstall,
}: Readonly<{
  entry: ExtensionCatalogEntry;
  context: BrowseContext;
  install: ExtensionCatalogInstallState;
  canAdminister: boolean;
  /** Why this installation cannot install from here at all, or "". */
  installBlockedReason: string;
  headingRef: RefObject<HTMLHeadingElement | null>;
  onBack: () => void;
  onInstall: (entry: ExtensionCatalogEntry) => void;
}>) {
  const { t, locale } = useI18n();
  const action = mainAction(entry, {
    canAdminister,
    installBlockedReason,
    installationVersion: context.installationVersion,
  }, t);
  const openRef = useRef<HTMLAnchorElement | null>(null);
  // Focus moves to "Open installed extension" only after an install or an
  // update this page started, not when an installed extension's page is opened.
  const startedHereRef = useRef(false);
  useEffect(() => {
    if (install.phase !== "idle") startedHereRef.current = true;
  }, [install.phase]);
  useEffect(() => {
    const done = install.outcome === "installed" || install.outcome === "updated";
    if (startedHereRef.current && done && action.kind === "open") {
      startedHereRef.current = false;
      openRef.current?.focus();
    }
  }, [install.outcome, action.kind]);

  const busy = install.phase !== "idle";
  const badges = marketplaceBadges(entry, context.earliest, context.now, t);
  const paragraphs = (entry.description || entry.summary).split(/\n\s*\n/).filter(Boolean);
  const categories = entry.categories.map((id) => categoryName(context.labels, id)).join(", ");
  const updated = dateLabel(entry.updated_at, locale);
  const size = sizeLabel(entry.release_zip_bytes, t);
  const works = worksWith(entry, t);
  const failed = install.outcome === "failed" && install.status;
  const cancelled = install.outcome === "cancelled" && install.status;
  const superseded = install.outcome === "superseded" && install.status;
  const said = failed || cancelled || superseded ? "" : install.status;
  const addedPermissions = action.kind === "update" ? action.addedPermissions : [];
  const describedBy = [addedPermissions.length ? ADDED_ID : "", action.note ? NOTE_ID : ""].filter(Boolean).join(" ") || undefined;
  const openLink = (
    <Link
      aria-describedby={action.kind === "open" && action.note ? NOTE_ID : undefined}
      className="fs-button"
      ref={openRef}
      to={`/extensions/${encodeURIComponent(entry.extension_id)}#installed`}
    >
      {t("react.extensions.open_installed_extension", "Open installed extension")}</Link>
  );

  const canAct = canAdminister || action.kind === "open";
  const button =
    action.kind === "install" || action.kind === "update" ? (
      <button
        aria-busy={install.phase === "installing" || undefined}
        aria-describedby={describedBy}
        className="fs-button primary"
        data-marketplace-install={entry.extension_id}
        disabled={action.disabled || busy}
        onClick={() => onInstall(entry)}
        type="button"
      >
        {action.kind === "update" ? updateLabel(install.phase, entry.version, t) : installLabel(install.phase, t)}
      </button>
    ) : (
      openLink
    );
  // Who may not install sees no Install or Update button, only why (the
  // action-rights rule); an installed extension still opens for everyone.
  const actionBlock = (
    <>
      {/* The trust point of an update: what the newer release asks for
          that the installed one did not, read before the button. */}
      {addedPermissions.length ? (
        <p className="fs-marketplace-action-note" id={ADDED_ID}>
          {t("react.extensions.this_version_also_asks_for", "This version also asks for:")}{" "}{addedPermissions.map((id) => permissionName(context.labels, id)).join(", ")}.
        </p>
      ) : null}
      {canAct ? button : null}
      {action.kind === "update" ? openLink : null}
      {action.note ? (
        <p className="fs-marketplace-action-note" id={NOTE_ID}>
          {action.note}
        </p>
      ) : null}
    </>
  );
  const publisher = publisherOf(entry);

  return (
    <div className="fs-marketplace-entry" data-marketplace-selection={entry.extension_id}>
      <CatalogPageHead
        action={actionBlock}
        backLabel={t("react.extensions.marketplace", "Catalog")}
        byline={<Publisher entry={entry} pills={badges} version />}
        headingRef={headingRef}
        icon={<EntryIcon entry={entry} size="page" />}
        onBack={onBack}
        summary={entry.summary}
        title={entry.name}
      />
      {failed ? (
        <DsAlert title={action.kind === "update" ? t("react.extensions.the_extension_was_not_updated", "The extension was not updated") : t("react.extensions.the_extension_was_not_installed", "The extension was not installed")} tone="danger">
          {install.status}
        </DsAlert>
      ) : null}
      {cancelled ? <DsAlert title={install.status} /> : null}
      {/* The listing was reloaded: the button above now names the new release. */}
      {superseded ? <DsAlert title={install.status} tone="warning" /> : null}
      <span aria-live="polite" className="fs-sr-only">
        {said}
      </span>
      <div className="fs-catalog-details">
        <div className="fs-catalog-details-main">
          <section>
            <h3 className="fs-catalog-section-title">{t("registry.workflows.simulation.description", "Description")}</h3>
            {paragraphs.map((paragraph) => (
              <p className="fs-catalog-prose" key={paragraph}>
                {paragraph}
              </p>
            ))}
          </section>
          {entry.setup_summary ? (
            <section>
              <h3 className="fs-catalog-section-title">{t("react.extensions.before_you_enable_it", "Before you enable it")}</h3>
              <p className="fs-catalog-prose">{entry.setup_summary}</p>
            </section>
          ) : null}
          {entry.capabilities.length ? (
            <section aria-labelledby="fs-marketplace-adds">
              <h3 className="fs-catalog-section-title" id="fs-marketplace-adds">
                {t("react.extensions.what_it_adds", "What it adds")}</h3>
              <ul className="fs-catalog-chip-list">
                {entry.capabilities.map((id) => (
                  <li key={id}>{capabilityName(context.labels, id)}</li>
                ))}
              </ul>
            </section>
          ) : null}
          {entry.permissions_summary.length ? (
            <section aria-labelledby="fs-marketplace-asks">
              <h3 className="fs-catalog-section-title" id="fs-marketplace-asks">
                {t("react.extensions.permissions_it_asks_for", "Permissions it asks for")}</h3>
              <ul className="fs-catalog-connect-list">
                {orderedPermissions(entry).map((id) => (
                  <li className="fs-catalog-prose" key={id}>
                    <span>
                      {permissionName(context.labels, id)} <code className="fs-catalog-code">{id}</code>
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
        <aside aria-label={t("react.extensions.extension_facts", "Extension facts")} className="fs-catalog-aside">
          <dl className="fs-kv is-bare">
            {publisher ? (
              <CatalogFact term={t("react.extensions.publisher", "Publisher")}>
                <span className="fs-catalog-fact-line">
                  <a href={publisher.url} rel="noreferrer" target="_blank">
                    {publisher.name}
                    <span className="fs-sr-only"> {t("react.extensions.opens_in_a_new_tab", "(opens in a new tab)")}</span>
                  </a>
                  {publisher.verified ? <DsBadge tone="green">{t("react.extensions.verified_publisher", "Verified publisher")}</DsBadge> : null}
                </span>
              </CatalogFact>
            ) : null}
            {categories ? <CatalogFact term={t("trash.columns.category", "Category")}>{categories}</CatalogFact> : null}
            <CatalogFact term={t("react.extensions.marketplace_final_version", "Version")}>
              <span className="is-numeric">{entry.version}</span>
              {updated ? " " : ""}{updated ? t("react.extensions.updated_value0", "· Updated {value0}", { value0: updated }) : ""}
            </CatalogFact>
            {entry.installed_version ? (
              <CatalogFact term={t("react.extensions.installed", "Installed")}>
                <span className="is-numeric">{entry.installed_version}</span>
              </CatalogFact>
            ) : null}
            {size ? <CatalogFact term={t("react.extensions.marketplace_final_download_size", "Download size")}><span className="is-numeric">{size}</span></CatalogFact> : null}
            {entry.runtime_targets?.length ? <CatalogFact term={t("react.extensions.marketplace_final_servers", "Servers")}><code>{entry.runtime_targets.join(", ")}</code></CatalogFact> : null}
            {works ? <CatalogFact term={t("react.extensions.marketplace_final_works_with", "Works with")}>{works}</CatalogFact> : null}
          </dl>
          <div className="fs-catalog-links">
            <p className="fs-catalog-source">
              <a href={entry.repository_url} rel="noreferrer" target="_blank">
                {t("react.extensions.documentation", "Documentation")}<ExternalLink aria-hidden="true" />
                <span className="fs-sr-only"> {" "}{t("react.extensions.opens_in_a_new_tab", "(opens in a new tab)")}</span>
              </a>
            </p>
          </div>
        </aside>
      </div>
    </div>
  );
}
