import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { ReactNode, RefObject } from "react";

import { ExternalLink, FilePlus2, Plug, RefreshCw } from "lucide-react";
import { useHref } from "react-router";

import { CatalogByline, CatalogChipRow, CatalogFact, CatalogPageHead, CatalogPills } from "@/components/shared/catalog-browse";
import { DsAlert, DsBadge, DsSelect, DsValueChip, OptionalLabel } from "@/components/shared/design-system";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import type { WorkflowCatalogCreateResult, WorkflowCatalogItem } from "@/pages/workflows/catalog/api";
import {
  categoryName,
  type CatalogLabels,
  integrationName,
  marketplaceHref,
  missingExtensions,
  requirementLabels,
  templatePill,
  TRIGGER_ICONS,
  triggerName,
} from "@/pages/workflows/catalog/catalog-model";
import { TemplateIcon, TriggerLabel } from "@/pages/workflows/catalog/catalog-browse";
import type { CatalogSetup } from "@/pages/workflows/catalog/catalog-setup-state";
import { DependencyResolutionList } from "@/pages/workflows/import/dependency-resolution";

type HeadingRef = RefObject<HTMLHeadingElement | null>;

function disabledDraftCount(count: number, t: I18nContextValue["t"] = fallbackContext.t): string {
  return count === 1
    ? t("react.workflows.catalog_disabled_draft_one", "{count} disabled draft", { count })
    : t("react.workflows.catalog_disabled_draft_many", "{count} disabled drafts", { count });
}

export function CatalogDetails({
  item,
  labels,
  installed = null,
  now = 0,
  headingRef,
  action,
  headingLevel = 3,
}: Readonly<{
  item: WorkflowCatalogItem;
  labels: CatalogLabels;
  /** 3 in the dialog, under its title; 2 on the Catalog tab of the page. */
  headingLevel?: 2 | 3;
  /** Extension ids this installation has, or null while unknown. */
  installed?: ReadonlySet<string> | null;
  now?: number;
  headingRef: HeadingRef;
  /** The main action beside the name, as an extension's or an MCP server's page has it. */
  action?: ReactNode;
}>) {
  const { t } = useI18n();
  const paragraphs = (item.description || item.summary).split(/\n\s*\n/).filter(Boolean);
  const min = item.compatible_flow_steward?.min || "";
  const category = item.categories[0];
  const includes = item.includes ?? [];
  const pill = templatePill(item, { installed, now }, t);
  const missing = new Set(missingExtensions(item, installed));
  return (
    <div className="fs-catalog-entry" data-catalog-selection={item.item_id}>
      <CatalogPageHead
        action={action}
        byline={<CatalogByline pills={pill ? [pill] : []} trigger={<TriggerLabel item={item} />} version={item.version ? `v${item.version}` : ""} />}
        headingLevel={headingLevel}
        headingRef={headingRef}
        icon={<TemplateIcon item={item} size="page" />}
        summary={item.summary}
        title={item.name}
      />
      <div className="fs-catalog-details">
        <div className="fs-catalog-details-main">
          <section>
            <h3 className="fs-catalog-section-title">{t("react.workflows.what_it_does", "What it does")}</h3>
            {paragraphs.map((paragraph) => (
              <p className="fs-catalog-prose" key={paragraph}>
                {paragraph}
              </p>
            ))}
          </section>
          {includes.length ? (
            <section data-catalog-includes="">
              <h3 className="fs-catalog-section-title">{t("react.workflows.includes", "Includes")}</h3>
              <p className="fs-field-helper">
                {t("react.workflows.catalog_created_children", "Created with it as {drafts}; it calls them.", { drafts: disabledDraftCount(includes.length, t) })}</p>
              <ul className="fs-catalog-list-plain">
                {includes.map((entry) => (
                  <li key={entry.item_id}>{entry.name}</li>
                ))}
              </ul>
            </section>
          ) : null}
          <p className="fs-catalog-source">
            <a href={item.workflow_yaml_url} rel="noreferrer" target="_blank">
              {t("react.workflows.view_yaml_on_github", "View YAML on GitHub")}<ExternalLink aria-hidden="true" />
              <span className="fs-sr-only"> {" "}{t("react.workflows.opens_in_a_new_tab", "(opens in a new tab)")}</span>
            </a>
          </p>
        </div>
        <aside aria-label={t("react.workflows.template_facts", "Template facts")} className="fs-catalog-aside">
          <section>
            <h4 className="fs-catalog-aside-title">{t("react.workflows.what_you_ll_need", "What you'll need")}</h4>
            {item.setup_requirements.length ? (
              <ul className="fs-catalog-needs">
                {item.setup_requirements.map((entry) => {
                  const absent = entry.kind === "extension" && Boolean(entry.ref) && missing.has(entry.ref ?? "");
                  return (
                    <li key={`${entry.kind}:${entry.ref ?? entry.label}`}>
                      <span className="fs-catalog-fact-line">
                        {entry.label} <span className="fs-catalog-list-muted">· {requirementLabels(t)[entry.kind] || entry.kind}</span>
                        {absent ? (
                          <CatalogPills pills={[{ tone: "orange", icon: "alert", label: t("react.extensions.not_installed", "Not installed") }]} />
                        ) : null}
                      </span>
                      {absent && entry.ref ? <MarketplaceLink extensionId={entry.ref} plain /> : null}
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="fs-catalog-prose">{t("react.workflows.nothing_to_set_up", "Nothing to set up.")}</p>
            )}
            <p className="fs-field-helper">{t("react.workflows.checked_against_this_installation_when_you_continue", "Checked against this installation when you continue.")}</p>
          </section>
          <dl className="fs-kv is-bare">
            {item.trigger_types.length ? (
              <CatalogFact term={t("react.workflows.trigger", "Trigger")}>
                <span className="fs-catalog-fact-line">
                  {item.trigger_types.map((id) => {
                    const Icon = TRIGGER_ICONS[id] ?? Plug;
                    return (
                      <span className="fs-catalog-trigger" key={id}>
                        <Icon aria-hidden="true" />
                        {triggerName(id, t)}
                      </span>
                    );
                  })}
                </span>
              </CatalogFact>
            ) : null}
            {category ? <CatalogFact term={t("trash.columns.category", "Category")}>{categoryName(labels, category)}</CatalogFact> : null}
            {item.integrations.length ? (
              <CatalogFact term={t("project_edit.tab.integrations", "Integrations")}>
                <CatalogChipRow labels={item.integrations.map((id) => integrationName(labels, id))} wrap />
              </CatalogFact>
            ) : null}
            <CatalogFact term={t("registry.data_contracts.column.version", "Version")}>
              <span className="is-numeric">{item.version}</span>
            </CatalogFact>
            {item.publication_status === "deprecated" ? (
              <CatalogFact term={t("extensions.runtime.status", "Status")}>{t("react.workflows.deprecated", "Deprecated")}</CatalogFact>
            ) : null}
            {min ? (
              <CatalogFact term={t("react.workflows.works_with", "Works with")}>
                Flow Steward {min} {" "}{t("react.workflows.or_later", "or later")}
              </CatalogFact>
            ) : null}
            <CatalogFact term={t("react.workflows.creates", "Creates")}>
              {includes.length ? disabledDraftCount(includes.length + 1, t) : t("react.workflows.a_disabled_draft", "A disabled draft")}
            </CatalogFact>
          </dl>
        </aside>
      </div>
    </div>
  );
}

function CheckingState() {
  const { t } = useI18n();
  const visible = useLoaderDelay();
  return (
    <div className="fs-table-state" role="status">
      {visible ? <span>{t("react.workflows.checking_this_installation", "Checking this installation…")}</span> : <span className="fs-sr-only">{t("react.workflows.checking_this_installation", "Checking this installation…")}</span>}
    </div>
  );
}

function extensionCopy(t: I18nContextValue["t"] = fallbackContext.t): Record<string, { badge: string; copy: (id: string) => string }> {
  return {
    missing: {
      badge: t("react.workflows.catalog_install_extension", "Install extension"),
      copy: (id) => t("react.workflows.catalog_extension_missing", "{id} isn't installed here.", { id }),
    },
    incompatible: {
      badge: t("react.workflows.catalog_update_extension", "Update extension"),
      copy: (id) => t("react.workflows.catalog_extension_incompatible", "The installed {id} is not a version this template works with.", { id }),
    },
    ambiguous: {
      badge: t("react.workflows.catalog_choose_install", "Choose an install"),
      copy: (id) => t("react.workflows.catalog_extension_ambiguous", "Several {id} installs are active. Choose the one this workflow should use below.", { id }),
    },
  };
}

/** Marketplace on one extension, through the router so the app's base path is kept. */
function MarketplaceLink({ extensionId, plain = false }: Readonly<{ extensionId: string; plain?: boolean }>) {
  const { t } = useI18n();
  const href = useHref(marketplaceHref(extensionId));
  return (
    <a className={plain ? "fs-catalog-needs-link" : "fs-button"} href={href} rel="noreferrer" target="_blank">
      {t("react.workflows.open_in_marketplace", "Open in Catalog")}<ExternalLink aria-hidden="true" />
      <span className="fs-sr-only"> {" "}{t("react.workflows.opens_in_a_new_tab", "(opens in a new tab)")}</span>
    </a>
  );
}

/** An extension's name as the template states it, then as the catalog does, then its id. */
function extensionName(item: WorkflowCatalogItem, labels: CatalogLabels, extensionId: string): string {
  const requirement = item.setup_requirements.find(
    (entry) => entry.kind === "extension" && entry.ref === extensionId,
  );
  return requirement?.label || labels.integrations[extensionId] || extensionId;
}

export function CatalogSetupStage({
  item,
  labels,
  setup,
  projects,
  headingRef,
}: Readonly<{
  item: WorkflowCatalogItem;
  labels: CatalogLabels;
  setup: CatalogSetup;
  projects: Array<{ id: string; name: string }>;
  headingRef: HeadingRef;
}>) {
  const { t } = useI18n();
  if (setup.phase === "checking" && !setup.preflight) return <CheckingState />;
  if (setup.phase === "error") {
    return (
      <div className="fs-catalog-setup">
        <h3 className="fs-card-title fs-catalog-stage-heading" ref={headingRef} tabIndex={-1}>
          {t("react.workflows.set_up", "Set up")}</h3>
        <DsAlert title={t("react.workflows.we_couldn_t_check_this_template", "We couldn't check this template")} tone="danger">
          {setup.checkError}
        </DsAlert>
        <div>
          <button className="fs-button" onClick={() => setup.checkAgain()} type="button">
            <RefreshCw aria-hidden="true" />
            {t("react.workflows.try_again", "Try again")}</button>
        </div>
      </div>
    );
  }
  const { blockedReasons, decisionRows, extensionIssues, resolution } = setup;
  const needsDecision = decisionRows.length > 0 || extensionIssues.length > 0;
  const idHelpId = "fs-catalog-setup-id-help";
  const project = projects.find((entry) => entry.id === setup.projectId);
  // Children reused from this installation are not created again.
  const created = Math.max(0, setup.draftCount - 1);
  const name = setup.preflight?.workflows[0]?.display_name || item.name;
  return (
    <div aria-busy={setup.phase === "checking"} className="fs-catalog-setup">
      <h3 className="fs-card-title fs-catalog-stage-heading" ref={headingRef} tabIndex={-1}>
        {needsDecision || blockedReasons.length ? t("react.workflows.set_up", "Set up") : t("react.workflows.review", "Review")}
      </h3>
      {setup.createError ? (
        <DsAlert title={t("react.workflows.the_workflow_was_not_created_variant_3eb3dfd4", "The workflow was not created")} tone="danger">
          {setup.createError}
        </DsAlert>
      ) : null}
      {blockedReasons.length ? (
        <DsAlert title={t("react.workflows.this_template_can_t_be_created_on_this_installation", "This template can't be created on this installation")} tone="danger">
          <ul>
            {blockedReasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </DsAlert>
      ) : null}
      <div className="fs-form-grid">
        <div className="fs-field">
          <OptionalLabel id="fs-catalog-setup-project-label">{t("trash.type.project", "Project")}</OptionalLabel>
          <DsSelect
            ariaLabel={t("trash.type.project", "Project")}
            labelledBy="fs-catalog-setup-project-label"
            onChange={setup.setProjectId}
            options={[{ value: "", label: t("react.workflows.no_project", "No project") }, ...projects.map((entry) => ({ value: entry.id, label: entry.name }))]}
            value={setup.projectId}
          />
          <span className="fs-field-helper">{t("react.workflows.project_connections_resolve_against_this_project", "Project connections resolve against this project.")}</span>
        </div>
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="fs-catalog-setup-id">
            {t("react.workflows.workflow_id", "Workflow ID")}</label>
          <input
            aria-describedby={idHelpId}
            aria-invalid={!setup.idValid || setup.rootConflict}
            className="fs-input"
            id="fs-catalog-setup-id"
            onChange={(event) => setup.setWorkflowId(event.target.value)}
            value={setup.workflowId}
          />
          <span className={`fs-field-helper${!setup.idValid || setup.rootConflict ? " error" : ""}`} id={idHelpId}>
            {setup.rootConflict ? t("react.workflows.value0_already_exists", "{value0} already exists.", { value0: setup.workflowId.trim() }) : t("react.workflows.catalog_id_rule", "Workflow ID must start with a lowercase letter and use only lowercase letters, numbers, and underscores.")}
          </span>
          {setup.suggestedId ? (
            <div>
              <button className="fs-button" onClick={() => setup.setWorkflowId(setup.suggestedId)} type="button">
                {t("react.workflows.use", "Use")}{" "}{setup.suggestedId}
              </button>
            </div>
          ) : null}
        </div>
      </div>
      {needsDecision ? (
        <section className="fs-catalog-setup-section">
          <h4 className="fs-field-label">{t("react.workflows.needs_your_decision", "Needs your decision")}</h4>
          {extensionIssues.map((entry) => {
            const copies = extensionCopy(t);
            const copy = copies[entry.outcome] ?? copies.missing;
            const extension = extensionName(item, labels, entry.extension_id);
            return (
              <div className="fs-surface-radius fs-surface-inset border border-subtle grid gap-2 p-3" data-catalog-extension={entry.extension_id} key={entry.extension_id}>
                <div className="fs-catalog-row-title">
                  <span className="font-medium">{extension}</span>
                  <DsBadge tone="orange">{copy.badge}</DsBadge>
                </div>
                <span className="fs-field-helper">{entry.message || copy.copy(extension)}</span>
                {entry.outcome === "ambiguous" ? null : (
                  <div className="fs-catalog-state-actions">
                    <MarketplaceLink extensionId={entry.extension_id} />
                  </div>
                )}
              </div>
            );
          })}
          {decisionRows.length ? (
            <div className="fs-catalog-resolution" data-catalog-resolution="">
              <DependencyResolutionList
                dependencies={decisionRows}
                dependencyOptions={setup.options}
                effectiveDependencies={setup.checkedCurrent ? setup.preflight?.dependencies : undefined}
                accepted={setup.checkedCurrent}
                resolutions={resolution.resolutions}
                selectedProjectId={setup.projectId}
                targetErrors={setup.targetErrors}
                updateResolution={resolution.updateResolution}
              />
            </div>
          ) : null}
          <div>
            <button
              className="fs-button"
              disabled={setup.phase === "checking" || setup.creating}
              onClick={() => setup.checkAgain()}
              type="button"
            >
              <RefreshCw aria-hidden="true" />
              {t("react.workflows.check_again", "Check again")}</button>
          </div>
        </section>
      ) : null}
      {!needsDecision && setup.phase === "ready" ? (
        <div>
          <button className="fs-button" disabled={setup.creating} onClick={() => setup.checkAgain()} type="button">
            <RefreshCw aria-hidden="true" />
            {t("react.workflows.check_again", "Check again")}
          </button>
        </div>
      ) : null}
      <p className="fs-catalog-review">
        {setup.draftCount === 1
          ? t("react.workflows.catalog_creates_one", "Creates {count} disabled draft:", { count: setup.draftCount })
          : t("react.workflows.catalog_creates_many", "Creates {count} disabled drafts:", { count: setup.draftCount })}{" "}<strong>{name}</strong> (
        {setup.workflowId.trim() || "…"})
        {created > 0 ? " " : ""}{created > 0 ? created === 1
          ? t("react.workflows.catalog_included_one", "and the {count} workflow it includes", { count: created })
          : t("react.workflows.catalog_included_many", "and the {count} workflows it includes", { count: created }) : ""}
        {setup.reusedIds.length ? " " : ""}{setup.reusedIds.length ? t("react.workflows.uses_the_existing_value0", "· Uses the existing {value0}", { value0: setup.reusedIds.join(", ") }) : ""}
        {setup.renames.length ? " " : ""}
        {setup.renames.length
          ? t("react.workflows.creates_value0", "· Creates {value0}", { value0: setup.renames.map((rename) => t("react.workflows.catalog_rename_pair", "{from} as {to}", { from: rename.from, to: rename.to })).join(", ") })
          : ""}
        {project ? " " : ""}{project ? t("react.workflows.project_value0", "· Project: {value0}", { value0: project.name }) : ""}.
        <br />
        {t("react.workflows.nothing_runs_until_you_enable_it", "Nothing runs until you enable it.")}</p>
    </div>
  );
}

export function CatalogDone({
  result,
  reusedDisabled = [],
  item,
  projectName,
  headingRef,
}: Readonly<{
  result: WorkflowCatalogCreateResult;
  reusedDisabled?: string[];
  item: WorkflowCatalogItem;
  projectName: string;
  headingRef: HeadingRef;
}>) {
  const { t } = useI18n();
  const created = result.imported_workflows.length ? result.imported_workflows : [{ workflow_id: result.workflow_id }];
  const children = [
    ...created.filter((entry) => entry.workflow_id !== result.workflow_id).map((entry) => entry.workflow_id),
    ...reusedDisabled,
  ];
  const count = Math.max(1, created.length);
  return (
    <div className="fs-catalog-done">
      <FilePlus2 aria-hidden="true" className="fs-catalog-state-icon" />
      <h3 className="fs-card-title fs-catalog-stage-heading" ref={headingRef} tabIndex={-1}>
        {count > 1 ? t("react.workflows.value0_drafts_created", "{value0} drafts created", { value0: count }) : t("react.workflows.draft_created", "Draft created")}
      </h3>
      <p className="fs-catalog-prose">
        {count > 1
          ? t("react.workflows.value0_and_the_workflows_it_includes_are_saved_as_disabled_drafts_they_won_t_run_until_you_enable_them", "{value0} and the workflows it includes are saved as disabled drafts. They won't run until you enable them.", { value0: item.name })
          : t("react.workflows.value0_is_saved_as_a_disabled_draft_it_won_t_run_until_you_enable_it", "{value0} is saved as a disabled draft. It won't run until you enable it.", { value0: item.name })}
      </p>
      <div className="fs-catalog-row-title">
        <DsBadge tone="neutral">{t("react.workflows.disabled", "Disabled")}</DsBadge>
        <DsValueChip>{result.workflow_id}</DsValueChip>
        {projectName ? <span className="fs-field-helper">· {projectName}</span> : null}
      </div>
      <section>
        <h4 className="fs-field-label">{t("react.workflows.before_you_enable_it", "Before you enable it")}</h4>
        <ul className="fs-catalog-checklist">
          <li>{t("react.workflows.review_the_steps_and_their_settings_in_the_builder", "Review the steps and their settings in the builder.")}</li>
          {children.length ? (
            <li data-catalog-enable-order="">
              {children.length === 1
                ? t("react.workflows.catalog_enable_child", "Enable the included workflow first ({ids}): a workflow cannot be enabled while a workflow it calls is disabled.", { ids: children.join(", ") })
                : t("react.workflows.catalog_enable_children", "Enable the included workflows first ({ids}): a workflow cannot be enabled while a workflow it calls is disabled.", { ids: children.join(", ") })}
            </li>
          ) : null}
          <li>{t("react.workflows.enable_the_workflow_from_its_overview_page", "Enable the workflow from its overview page.")}</li>
        </ul>
      </section>
    </div>
  );
}
