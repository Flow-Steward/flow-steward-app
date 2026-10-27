import { useI18n } from "@/lib/i18n/useI18n";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Search } from "lucide-react";
import { DsDialog, DsTabs } from "@/components/shared/design-system";
import { useCompactCatalog } from "@/lib/react/use-compact-catalog";
import { useLatestSearchParams } from "@/lib/routing/use-latest-search-params";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { currentTime } from "@/lib/utils/catalog-facets";
import { fetchExtensions } from "@/pages/extensions/api";
import { fetchProjectsList } from "@/pages/projects/list/api";
import {
  fetchInstallationVersion,
  fetchWorkflowCatalog,
  type WorkflowCatalogCreateResult,
} from "@/pages/workflows/catalog/api";
import { type BrowseState, CatalogBrowse } from "@/pages/workflows/catalog/catalog-browse";
import {
  type CatalogLabels,
  type CatalogUrlState,
  computeFacets,
  filterItems,
  incompatibleReason,
  readCatalogUrl,
  sortOptions,
  writeCatalogUrl,
} from "@/pages/workflows/catalog/catalog-model";
import { errorMessage, useCatalogSetup } from "@/pages/workflows/catalog/catalog-setup-state";
import { CatalogDetails, CatalogDone, CatalogSetupStage } from "@/pages/workflows/catalog/catalog-stages";
import { CreateWorkflowFields } from "@/pages/workflows/list/create-workflow-fields";
import type { CreateWorkflowForm } from "@/pages/workflows/list/create-workflow-state";

import "@/pages/extensions/marketplace-catalog.css";
import "./catalog-dialog.css";

export type CatalogTab = "template" | "scratch";

type Stage =
  | { kind: "setup"; itemId: string }
  | {
      kind: "done";
      itemId: string;
      result: WorkflowCatalogCreateResult;
      projectId: string;
      /** Reused children that are disabled, so they too must be enabled first. */
      reusedDisabled: string[];
    };

const NO_LABELS: CatalogLabels = { categories: {}, integrations: {} };

function formatDay(value: string | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

/**
 * Create workflow, starting from a template: browse the catalog, read one
 * template, set it up against this installation and create it as disabled
 * drafts, all in one dialog. The "From scratch" tab is the plain create form.
 * The dialog never navigates by itself; the result offers where to go next.
 */
export function WorkflowCatalogDialog({
  open,
  inline = false,
  resetSignal = 0,
  onStartScratch,
  tab,
  onTabChange,
  readOnly = false,
  configured = true,
  projectId = "",
  scratchForm,
  onScratchSubmit,
  onClose,
  onCreated,
  onOpenBuilder,
  onViewWorkflow,
}: Readonly<{
  open: boolean;
  /**
   * Drawn in the page (the workflow list's Catalog tab) instead of a dialog:
   * the same browse, template, setup and done steps, with their actions under
   * the step rather than in a dialog footer.
   */
  inline?: boolean;
  /** A new value returns to the results, closing an open template or setup. */
  resetSignal?: number;
  /** Start from scratch elsewhere (the page's Create workflow form) instead of this dialog's tab. */
  onStartScratch?: () => void;
  /** The tab the dialog opens on; the dialog keeps it in the URL from there. */
  tab: CatalogTab;
  onTabChange?: (tab: CatalogTab) => void;
  /** An operator browses; only an administrator creates. */
  readOnly?: boolean;
  /** False when this installation has no template catalog at all. */
  configured?: boolean;
  projectId?: string;
  scratchForm?: CreateWorkflowForm;
  onScratchSubmit?: () => void;
  onClose: () => void;
  onCreated?: (result: WorkflowCatalogCreateResult) => void;
  onOpenBuilder?: (workflowId: string, projectId: string) => void;
  onViewWorkflow?: (workflowId: string) => void;
}>) {
  const { t } = useI18n();
  const [params, updateParams] = useLatestSearchParams();
  const urlState = useMemo(() => readCatalogUrl(params), [params]);
  // The open template and the tab are state first and URL second: the URL
  // catches up a render later, and two quick actions (Escape twice, two
  // ticks) must each see the first one's result.
  const [detailId, setDetailId] = useState(() => params.get("tpl") || "");
  const [localTab, setLocalTab] = useState<CatalogTab>(tab);
  const [stage, setStage] = useState<Stage | null>(null);
  // Where Escape stands right now, written as each step happens: a second
  // Escape can arrive before React has rendered the first one's result.
  const escapeStageRef = useRef<"browse" | "details" | "setup" | "done" | null>(null);
  const [now] = useState(currentTime);
  const compact = useCompactCatalog();
  // The search on the template tab, the Workflow ID on the scratch tab: only
  // one is mounted at a time, so one stable ref is the dialog's first field.
  const firstFieldRef = useRef<HTMLInputElement | null>(null);
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const countRef = useRef<HTMLHeadingElement | null>(null);
  const returnToRef = useRef("");
  const showTabs = !readOnly && Boolean(scratchForm) && !inline;
  const shown = open || inline;
  const activeTab: CatalogTab = showTabs ? localTab : "template";

  // One request for the whole index; search, facets, sorting and paging all
  // happen in the browser.
  const catalogQuery = useQuery({
    queryKey: ["workflows", "catalog"],
    queryFn: fetchWorkflowCatalog,
    enabled: shown && configured,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
  const extensionsQuery = useQuery({
    queryKey: ["extensions", "all"],
    queryFn: () => fetchExtensions("all"),
    enabled: shown && configured,
    retry: false,
  });
  const versionQuery = useQuery({
    queryKey: ["health", "build-version"],
    queryFn: fetchInstallationVersion,
    enabled: shown && configured,
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
  });

  const items = useMemo(() => catalogQuery.data?.items ?? [], [catalogQuery.data]);
  const labels = useMemo<CatalogLabels>(
    () =>
      catalogQuery.data
        ? {
            categories: catalogQuery.data.category_labels ?? {},
            integrations: catalogQuery.data.integration_labels ?? {},
          }
        : NO_LABELS,
    [catalogQuery.data],
  );
  const installed = useMemo(
    () => (extensionsQuery.data ? new Set(extensionsQuery.data.map((entry) => entry.extension_id)) : null),
    [extensionsQuery.data],
  );
  const facets = useMemo(() => computeFacets(items, urlState.filters, labels, t), [items, urlState.filters, labels, t]);
  const visible = useMemo(
    () => filterItems(items, urlState.filters, labels, urlState.sort, t),
    [items, urlState.filters, labels, urlState.sort, t],
  );
  const sortChoices = useMemo(() => sortOptions(items, t), [items, t]);
  const detail = items.find((entry) => entry.item_id === (stage?.itemId ?? detailId)) ?? null;
  let stageKind: "browse" | "details" | "setup" | "done" = "browse";
  if (stage && detail) stageKind = stage.kind;
  else if (detail) stageKind = "details";

  const setup = useCatalogSetup(stageKind === "setup" || stageKind === "done" ? detail : null, projectId);
  const projectsQuery = useQuery({
    queryKey: ["workflows", "import", "project-picker"],
    queryFn: () => fetchProjectsList({ page: 1, perPage: 200, search: "" }),
    enabled: shown && stageKind === "setup",
    retry: false,
  });
  const projects = useMemo(
    () => (projectsQuery.data?.projects ?? []).map((entry) => ({ id: entry.id, name: entry.name })),
    [projectsQuery.data],
  );

  // Each write starts from the last one not yet in the URL, so quick updates
  // compose instead of the second one starting from a stale snapshot.
  const writeUrl = useCallback(
    (
      patch: Partial<CatalogUrlState> | ((latest: CatalogUrlState) => Partial<CatalogUrlState>),
      extra?: (next: URLSearchParams) => void,
    ) => {
      updateParams((current) => {
        const latest = readCatalogUrl(current);
        const next = writeCatalogUrl(current, { ...latest, ...(typeof patch === "function" ? patch(latest) : patch) });
        extra?.(next);
        return next;
      });
    },
    [updateParams],
  );
  const setDetail = useCallback(
    (itemId: string) => {
      setDetailId(itemId);
      writeUrl({}, (next) => {
        if (itemId) next.set("tpl", itemId);
        else next.delete("tpl");
      });
    },
    [writeUrl],
  );
  const selectTab = (next: CatalogTab) => {
    setLocalTab(next);
    writeUrl({}, (url) => url.set("create", next));
    onTabChange?.(next);
  };

  const openDetails = (itemId: string) => {
    escapeStageRef.current = "details";
    returnToRef.current = itemId;
    setStage(null);
    setDetail(itemId);
  };
  const backToBrowse = () => {
    escapeStageRef.current = "browse";
    setStage(null);
    setDetail("");
  };
  const startSetup = () => {
    if (!detail) return;
    escapeStageRef.current = "setup";
    setStage({ kind: "setup", itemId: detail.item_id });
    setup.start(detail);
  };
  const backToDetails = () => {
    escapeStageRef.current = "details";
    setStage(null);
  };
  const createAnother = () => {
    returnToRef.current = "";
    backToBrowse();
    selectTab("template");
  };
  const close = () => {
    escapeStageRef.current = null;
    setStage(null);
    setDetailId("");
    setLocalTab("template");
    onClose();
  };
  const startScratch = () => {
    backToBrowse();
    if (onStartScratch) {
      onStartScratch();
      return;
    }
    selectTab("scratch");
  };
  // A new reset signal returns to the results, adjusted while rendering (no
  // effect): the open template and the setup step close at once.
  const [seenReset, setSeenReset] = useState(resetSignal);
  if (resetSignal !== seenReset) {
    setSeenReset(resetSignal);
    setStage(null);
    setDetailId("");
  }

  // Use template from the results: straight to setting it up.
  const applyTemplate = (itemId: string) => {
    const item = items.find((entry) => entry.item_id === itemId);
    if (!item) return;
    escapeStageRef.current = "setup";
    returnToRef.current = itemId;
    setDetail(itemId);
    setStage({ kind: "setup", itemId });
    setup.start(item);
  };

  async function createDraft() {
    if (!detail) return;
    const result = await setup.create();
    if (!result) return;
    escapeStageRef.current = "done";
    setStage({
      kind: "done",
      itemId: detail.item_id,
      result,
      projectId: setup.projectId,
      reusedDisabled: setup.reusedIds.filter((id) => setup.existing?.get(id) !== true),
    });
    onCreated?.(result);
  }

  const onEscape = () => {
    const current = escapeStageRef.current ?? stageKind;
    if (current === "details") backToBrowse();
    else if (current === "setup") backToDetails();
    else close();
  };

  // Focus follows the stage: its heading on the way in, the card that was
  // opened on the way back, the search when browsing starts.
  useEffect(() => {
    if (!shown) return;
    if (stageKind === "browse") {
      const back = returnToRef.current;
      returnToRef.current = "";
      if (back) {
        queueMicrotask(() => {
          const marker = [...document.querySelectorAll<HTMLElement>("[data-catalog-item]")].find(
            (element) => element.dataset.catalogItem === back,
          );
          marker?.closest<HTMLElement>("button, tr")?.focus();
        });
      }
      return;
    }
    queueMicrotask(() => headingRef.current?.focus());
  }, [shown, stageKind, setup.phase]);

  let browseState: BrowseState = "ready";
  if (!configured) browseState = "not-configured";
  else if (catalogQuery.isLoading) browseState = "loading";
  else if (catalogQuery.isError) browseState = "unavailable";

  const availability = catalogQuery.data?.availability;
  const useReason = detail
    ? readOnly
      ? t("react.workflows.only_administrators_can_create_workflows", "Only administrators can create workflows.")
      : incompatibleReason(detail, versionQuery.data ?? "", t)
    : "";

  let body: ReactNode = null;
  let footer: ReactNode = null;
  if (stageKind === "browse" && activeTab === "scratch" && scratchForm) {
    body = (
      <div aria-labelledby="fs-catalog-tab-scratch" className="fs-catalog-stage" id="fs-catalog-panel-scratch" role="tabpanel">
        <p className="fs-field-helper">
          {t("react.workflows.the_workflow_starts_empty_and_opens_in_the_builder_where_steps_and_groups_are_added", "The workflow starts empty and opens in the builder, where steps and groups are added.")}</p>
        <CreateWorkflowFields form={scratchForm} idRef={firstFieldRef} />
      </div>
    );
    footer = (
      <>
        <button className="fs-button" disabled={scratchForm.creating} onClick={close} type="button">
          {t("extensions.repeater.cancel", "Cancel")}</button>
        <button
          className="fs-button primary"
          disabled={scratchForm.creating}
          onClick={() => onScratchSubmit?.()}
          type="button"
        >
          {scratchForm.creating ? t("react.workflows.creating_variant_def70944", "Creating...") : t("projects.modal.create", "Create")}
        </button>
      </>
    );
  } else if (stageKind === "browse") {
    // On the page the search heads the results column, beside the rail; in
    // the dialog it spans the dialog above both.
    const searchField = (
      <label className="fs-search">
        <Search aria-hidden="true" />
        <input
          aria-label={t("react.workflows.search_templates_and_integrations_variant_40297bca", "Search templates and integrations")}
          disabled={browseState === "unavailable" || browseState === "not-configured"}
          onChange={(event) => {
            const q = event.target.value;
            writeUrl((latest) => ({ filters: { ...latest.filters, q }, page: 1 }));
          }}
          placeholder={t("react.workflows.search_templates_and_integrations", "Search templates and integrations…")}
          ref={firstFieldRef}
          type="search"
          value={urlState.filters.q}
        />
      </label>
    );
    body = (
      <>
        {inline ? null : <div className="fs-catalog-searchrow">{searchField}</div>}
        <div
          aria-labelledby={showTabs ? "fs-catalog-tab-template" : undefined}
          className="fs-catalog-body"
          id="fs-catalog-panel-template"
          role={showTabs ? "tabpanel" : undefined}
        >
          <CatalogBrowse
            busy={catalogQuery.isFetching && !catalogQuery.isLoading}
            compact={compact}
            context={{ labels, installed, now }}
            countRef={countRef}
            errorText={errorMessage(catalogQuery.error, t("react.workflows.the_catalog_host_did_not_answer", "The catalog host did not answer."))}
            facets={facets}
            filters={urlState.filters}
            items={items}
            onFilters={(update) => writeUrl((latest) => ({ filters: update(latest.filters), page: 1 }))}
            onOpen={openDetails}
            onUse={applyTemplate}
            onPage={(page) => {
              writeUrl({ page });
              countRef.current?.focus();
            }}
            onRetry={() => ignoreAsyncResult(catalogQuery.refetch())}
            onSort={(sort) => writeUrl({ sort, page: 1 })}
            onStartScratch={startScratch}
            onView={(view) => writeUrl({ view, page: 1 })}
            search={inline ? searchField : undefined}
            page={urlState.page}
            readOnly={readOnly}
            sort={urlState.sort}
            sortChoices={sortChoices}
            stale={Boolean(availability?.stale)}
            staleSince={formatDay(availability?.fetched_at || catalogQuery.data?.published_at)}
            state={browseState}
            view={urlState.view}
            visible={visible}
          />
        </div>
      </>
    );
  } else if (stageKind === "details" && detail) {
    // Who may not create workflows sees no Use template, only why (the
    // action-rights rule); an incompatible template keeps its disabled button.
    const useButton = readOnly ? null : (
      <button
        aria-describedby={useReason ? "fs-catalog-use-reason" : undefined}
        className="fs-button primary"
        disabled={Boolean(useReason)}
        onClick={startSetup}
        type="button"
      >
        {t("react.workflows.use_this_template", "Use template")}</button>
    );
    const useAction = (
      <>
        {useButton}
        {useReason ? (
          <p className="fs-marketplace-action-note" id="fs-catalog-use-reason">
            {useReason}
          </p>
        ) : null}
      </>
    );
    body = (
      <div className="fs-catalog-stage">
        <CatalogDetails
          action={inline ? useAction : undefined}
          headingLevel={inline ? 2 : 3}
          headingRef={headingRef}
          installed={installed}
          item={detail}
          labels={labels}
          now={now}
        />
      </div>
    );
    footer = inline ? null : (
      <>
        {useReason ? (
          <span className="fs-catalog-footer-note" id="fs-catalog-use-reason">
            {useReason}
          </span>
        ) : null}
        {useButton}
      </>
    );
  } else if (stageKind === "setup" && detail) {
    body = (
      <div className="fs-catalog-stage">
        <CatalogSetupStage headingRef={headingRef} item={detail} labels={labels} projects={projects} setup={setup} />
      </div>
    );
    if (setup.phase !== "error" && setup.preflight) {
      const blocked = setup.blockedReasons.length > 0;
      footer = (
        <>
          {setup.blocker ? <span className="fs-catalog-footer-note">{setup.blocker}</span> : null}
          {blocked ? (
            <>
              <button className="fs-button" onClick={startScratch} type="button">
                {t("react.workflows.start_from_scratch", "Start from scratch")}</button>
              <button className="fs-button" onClick={backToBrowse} type="button">
                {t("react.workflows.back_to_templates", "Back to templates")}</button>
            </>
          ) : null}
          <button
            className="fs-button primary"
            disabled={Boolean(setup.blocker) || setup.creating || setup.phase !== "ready"}
            onClick={() => ignoreAsyncResult(createDraft())}
            type="button"
          >
            {setup.creating ? t("react.workflows.creating", "Creating…") : setup.draftCount > 1 ? t("react.workflows.create_value0_drafts", "Create {value0} drafts", { value0: setup.draftCount }) : t("react.workflows.create_draft", "Create draft")}
          </button>
        </>
      );
    }
  } else if (stageKind === "done" && detail && stage?.kind === "done") {
    const done = stage;
    const projectName = projects.find((entry) => entry.id === done.projectId)?.name ?? "";
    body = (
      <div className="fs-catalog-stage">
        <CatalogDone
          headingRef={headingRef}
          item={detail}
          projectName={projectName}
          result={done.result}
          reusedDisabled={done.reusedDisabled}
        />
      </div>
    );
    footer = (
      <>
        <button className="fs-button" onClick={createAnother} type="button">
          {t("react.workflows.create_another", "Create another")}</button>
        <span className="fs-catalog-footer-note" />
        <button className="fs-button" onClick={() => onViewWorkflow?.(done.result.workflow_id)} type="button">
          {t("react.workflows.view_workflow", "View workflow")}</button>
        <button
          className="fs-button primary"
          onClick={() => onOpenBuilder?.(done.result.workflow_id, done.projectId)}
          type="button"
        >
          {t("react.workflows.open_in_builder", "Open in builder")}</button>
      </>
    );
  }

  let toprow: ReactNode = null;
  if (stageKind === "browse" && showTabs) {
    toprow = (
      <div className="fs-catalog-toprow is-tabs">
        <DsTabs
          active={activeTab}
          ariaLabel={t("react.workflows.how_to_start", "How to start")}
          idPrefix="fs-catalog"
          items={[
            { key: "template", label: t("react.workflows.from_template", "From template") },
            { key: "scratch", label: t("react.workflows.from_scratch", "From scratch") },
          ]}
          onSelect={(key) => selectTab(key as CatalogTab)}
        />
      </div>
    );
  } else if (stageKind === "details" || stageKind === "setup") {
    toprow = (
      <div className="fs-catalog-toprow">
        <button
          className="fs-clear-filters fs-catalog-back"
          disabled={setup.creating}
          onClick={stageKind === "details" ? backToBrowse : backToDetails}
          type="button"
        >
          <ArrowLeft aria-hidden="true" />
          {stageKind === "details"
            ? inline
              ? t("react.workflows.tab_catalog", "Catalog")
              : t("react.workflows.back_to_templates", "Back to templates")
            : t("react.workflows.back_to_template", "Back to template")}
        </button>
      </div>
    );
  }

  if (inline) {
    return (
      <section aria-label={t("react.workflows.catalog_dialog_templates", "Workflow templates")} className="fs-workflow-catalog-inline">
        {toprow}
        {body}
        {footer ? <div className="fs-workflow-catalog-inline-actions">{footer}</div> : null}
      </section>
    );
  }

  const title = readOnly
    ? t("react.workflows.catalog_dialog_templates", "Workflow templates")
    : t("react.workflows.catalog_dialog_create", "Create workflow");
  return (
    <DsDialog
      busy={setup.creating || Boolean(scratchForm?.creating)}
      closeLabel={t("react.workflows.close_value0", "Close {value0}", { value0: title })}
      description={
        readOnly
          ? t("react.workflows.browse_ready_made_workflows_only_administrators_can_create_them", "Browse ready-made workflows. Only administrators can create them.")
          : t("react.workflows.start_from_a_template_or_build_your_own", "Start from a template or build your own.")
      }
      eyebrow={t("trash.category.registry", "Registry")}
      footer={footer}
      initialFocusRef={firstFieldRef}
      onClose={close}
      onEscape={onEscape}
      open={open}
      title={title}
      variant="catalog"
    >
      {toprow}
      {body}
    </DsDialog>
  );
}
