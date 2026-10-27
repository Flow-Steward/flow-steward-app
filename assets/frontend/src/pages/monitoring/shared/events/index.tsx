import { useI18n } from "@/lib/i18n/useI18n";

import { useEffect, useState, type ReactNode } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ChevronRight, X } from "lucide-react";

import {
  DsAlert,
  DsBadge,
  DsCard,
  DsDrawer,
  DsSelect,
  Pagination,
} from "@/components/shared/design-system";

import { PeriodFilter } from "@/components/shared/period-filter";

import { queryKeys } from "@/lib/query/keys";

import { todayIn } from "@/lib/time/calendar-days";

import { expandPeriod, periodLabel, type PeriodValue } from "@/lib/time/period";

import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";

import {
  fetchEventDetail,
  fetchEventsList,
  redriveEvent,
  type EventCommandRecord,
  type EventDispatchRecord,
  type EventFacetOption,
  type EventListItem,
} from "@/pages/monitoring/shared/events/api";

import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  facetOptions,
  type ActiveFilterChip, type DrawerFeedback, type Entity,
  eventRef,
  eventWorkflowId,
  eventWorkflowLabel,
  eventTypeLabel,
  statusTone,
  normalizeStatus,
  eventStatusLabel,
  workflowPath,
  backendLabelOrCode,
  commandTypeLabel,
  commandStatusLabel,
  normalizeValue,
  canRedrive,
  objectOrEmpty,
  readableText,
  createActiveFilterChips,
} from "./event-display-helpers";

import "../../monitoring.css";

function FacetSelect({
  ariaLabel,
  value,
  options,
  placeholder,
  disabled = false,
  onChange,
}: Readonly<{
  ariaLabel: string;
  value: string;
  options: EventFacetOption[];
  placeholder: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}>) {
  return (
    <DsSelect
      ariaLabel={ariaLabel}
      disabled={disabled}
      onChange={onChange}
      options={facetOptions(options, placeholder)}
      placeholder={placeholder}
      value={value}
    />
  );
}

function DetailRow({ label, value }: Readonly<{ label: string; value: ReactNode }>) {
  return (
    <div className="fs-detail-row">
      <span>{label}</span>
      <span className="fs-detail-value">{value}</span>
    </div>
  );
}

function EventsFiltersCard({
  workflowId,
  eventFamily,
  eventCategory,
  eventType,
  status,
  period,
  workflowOptions,
  familyOptions,
  categoryOptions,
  typeOptions,
  statusOptions,
  refreshing,
  onWorkflowChange,
  onFamilyChange,
  onCategoryChange,
  onTypeChange,
  onStatusChange,
  onPeriodChange,
  onRefresh,
  onClear,
}: Readonly<{
  workflowId: string;
  eventFamily: string;
  eventCategory: string;
  eventType: string;
  status: string;
  period: PeriodValue;
  workflowOptions: EventFacetOption[];
  familyOptions: EventFacetOption[];
  categoryOptions: EventFacetOption[];
  typeOptions: EventFacetOption[];
  statusOptions: EventFacetOption[];
  refreshing: boolean;
  onWorkflowChange: (value: string) => void;
  onFamilyChange: (value: string) => void;
  onCategoryChange: (value: string) => void;
  onTypeChange: (value: string) => void;
  onStatusChange: (value: string) => void;
  onPeriodChange: (value: PeriodValue) => void;
  onRefresh: () => void;
  onClear: () => void;
}>) {
  const { t } = useI18n();
  return (
    <DsCard
      description={t("react.monitoring.filter_by_workflow_event_family_subtype_and_exact_event_type_before_drilling_into_dispatch_details", "Filter by workflow, event family, subtype, and exact event type before drilling into dispatch details.")}
      title={t("react.monitoring.events_and_audit_trail", "Events and audit trail")}
    >
      <div className="fs-monitoring-filters">
        <FacetSelect
          ariaLabel={t("react.monitoring.workflow_filter", "Workflow filter")}
          onChange={onWorkflowChange}
          options={workflowOptions}
          placeholder={t("settings.access.limits.all_workflows", "All workflows")}
          value={workflowId}
        />
        <FacetSelect
          ariaLabel={t("react.monitoring.event_family_filter", "Event family filter")}
          onChange={onFamilyChange}
          options={familyOptions}
          placeholder={t("react.monitoring.all_event_families", "All event families")}
          value={eventFamily}
        />
        <FacetSelect
          ariaLabel={t("react.monitoring.event_category_filter", "Event category filter")}
          disabled={!eventFamily}
          onChange={onCategoryChange}
          options={categoryOptions}
          placeholder={eventFamily ? t("react.monitoring.all_subtypes", "All subtypes") : t("react.monitoring.pick_family_first", "Pick family first")}
          value={eventCategory}
        />
        <FacetSelect
          ariaLabel={t("react.monitoring.exact_event_type_filter", "Exact event type filter")}
          disabled={!typeOptions.length}
          onChange={onTypeChange}
          options={typeOptions}
          placeholder={typeOptions.length ? t("react.monitoring.all_exact_event_types", "All exact event types") : t("react.monitoring.no_exact_types_yet", "No exact types yet")}
          value={eventType}
        />
        <FacetSelect
          ariaLabel={t("react.monitoring.dispatch_status_filter", "Dispatch status filter")}
          onChange={onStatusChange}
          options={statusOptions}
          placeholder={t("react.monitoring.all_dispatch_statuses", "All dispatch statuses")}
          value={status}
        />
        <PeriodFilter ariaLabel={t("react.monitoring.event_period", "Event period")} onChange={onPeriodChange} value={period} />
        <div className="fs-monitoring-status">
          <button className="fs-button" disabled={refreshing} onClick={onRefresh} type="button">
            {refreshing ? t("react.monitoring.refreshing", "Refreshing...") : t("account.tokens.refresh", "Refresh")}
          </button>
          <button className="fs-button ghost" onClick={onClear} type="button">
            {t("react.monitoring.clear_filters", "Clear filters")} </button>
        </div>
      </div>
    </DsCard>
  );
}

function ActiveFilterChips({ chips }: Readonly<{ chips: ActiveFilterChip[] }>) {
  const { t } = useI18n();
  if (!chips.length) return null;
  return (
    <div className="fs-monitoring-status" data-testid="events-active-filters">
      {chips.map((chip) => (
        <span className="fs-applied-filter" key={chip.key}>
          {chip.href ? (
            <a href={chip.href} title={t("react.monitoring.open_value1", "Open {value1}", { value1: String(chip.label) })}>
              {chip.label}
            </a>
          ) : (
            <span>{chip.label}</span>
          )}
          <button aria-label={t("react.monitoring.remove_value1_filter", "Remove {value1} filter", { value1: String(chip.label) })} onClick={chip.clear} type="button">
            <X aria-hidden="true" />
          </button>
        </span>
      ))}
    </div>
  );
}

function EventRow({
  row,
  selectedRef,
  userTimezone,
  onSelect,
}: Readonly<{
  row: EventListItem;
  selectedRef: string;
  userTimezone: string;
  onSelect: (eventRef: string) => void;
}>) {
  const { t } = useI18n();
  const ref = eventRef(row);
  const active = Boolean(ref) && ref === selectedRef;
  const workflowId = eventWorkflowId(row);
  const workflowLabel = eventWorkflowLabel(row);
  return (
    <li className="fs-monitoring-event" data-selected={active || undefined}>
      <button className="fs-monitoring-event-main" onClick={() => onSelect(ref)} type="button">
        <span className="fs-monitoring-event-head">
          <strong>{eventTypeLabel(row, t)}</strong>
          <DsBadge tone={statusTone(normalizeStatus(row))}>{eventStatusLabel(row, t)}</DsBadge>
        </span>
        <span className="fs-monitoring-event-meta">
          {ref || t("react.monitoring.no_event_ref", "(no event ref)")} • {firstStringFromUnknownValue([row.project_name, row.project_id, "—"])} •{" "}
          {formatUserDateTime(row.created_at, userTimezone)}
        </span>
      </button>
      {workflowId ? (
        <a
          className="fs-link"
          data-testid="event-workflow-link"
          href={workflowPath(workflowId)}
          onClick={(event) => event.stopPropagation()}
          title={t("react.monitoring.workflow_id_value1", "Workflow ID: {value1}", { value1: String(workflowId) })}
        >
          {t("project_detail.task.preview.workflow", "Workflow:")} {workflowLabel}
        </a>
      ) : null}
    </li>
  );
}

function EventsListStatus({
  loading,
  error,
  empty,
}: Readonly<{ loading: boolean; error: unknown; empty: boolean }>) {
  const { t } = useI18n();
  if (loading) {
    return <p className="fs-empty-copy">{t("react.monitoring.loading_events", "Loading events…")}</p>;
  }
  if (error) {
    return (
      <DsAlert title={t("react.monitoring.events_could_not_be_loaded", "Events could not be loaded")} tone="danger">
        {error instanceof Error ? error.message : t("events.error.load", "Failed to load events.")}
      </DsAlert>
    );
  }
  if (empty) {
    return (
      <div className="fs-table-state">
        <strong>{t("react.monitoring.no_events_found", "No events found.")}</strong>
        <span>{t("react.monitoring.refine_by_workflow_and_event_hierarchy_above", "Refine by workflow and event hierarchy above.")}</span>
      </div>
    );
  }
  return null;
}

function EventsListCard({
  events,
  total,
  page,
  totalPages,
  perPage,
  selectedRef,
  userTimezone,
  chips,
  loading,
  error,
  onSelect,
  onPageChange,
  onPerPageChange,
}: Readonly<{
  events: EventListItem[];
  total: number;
  page: number;
  totalPages: number;
  perPage: number;
  selectedRef: string;
  userTimezone: string;
  chips: ActiveFilterChip[];
  loading: boolean;
  error: unknown;
  onSelect: (eventRef: string) => void;
  onPageChange: (page: number) => void;
  onPerPageChange: (perPage: number) => void;
}>) {
  const { t } = useI18n();
  return (
    <DsCard
      description={t("react.monitoring.showing_value1_rows_total_value2_page_value3_of_value4", "Showing {value1} rows (total {value2}). Page {value3} of {value4}.", { value1: String(events.length), value2: String(total), value3: String(page), value4: String(totalPages) })}
      title={t("react.monitoring.events_list", "Events list")}
    >
      <ActiveFilterChips chips={chips} />
      <div aria-hidden="true" className="hidden" id="events-grid" />
      <EventsListStatus empty={!events.length} error={error} loading={loading} />
      {events.length ? (
        <ul className="fs-monitoring-event-list">
          {events.map((row) => (
            <EventRow
              key={eventRef(row) || JSON.stringify(row)}
              onSelect={onSelect}
              row={row}
              selectedRef={selectedRef}
              userTimezone={userTimezone}
            />
          ))}
        </ul>
      ) : null}
      <Pagination
        onPageChange={onPageChange}
        onPerPageChange={onPerPageChange}
        page={page}
        perPage={perPage}
        perPageOptions={[25, 50, 100, 200]}
        total={total}
      />
    </DsCard>
  );
}

function EventAdvancedDiagnostics({
  dispatches,
  commands,
  linkedCommands,
  metadata,
  userTimezone,
}: Readonly<{
  dispatches: EventDispatchRecord[];
  commands: EventCommandRecord[];
  linkedCommands: EventCommandRecord[];
  metadata: Entity;
  userTimezone: string;
}>) {
  const { t } = useI18n();
  const allCommands = [...commands, ...linkedCommands];
  return (
    <details className="fs-disclosure" data-testid="event-advanced-diagnostics">
      <summary>
        <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
        {t("react.monitoring.advanced_event_diagnostics", "Advanced event diagnostics")} </summary>
      <div className="fs-disclosure-body fs-panel-stack">
        <div className="fs-panel-group">
          <div className="fs-panel-heading">
            <div>
              <h4 className="fs-panel-title">{t("events.dispatches", "Dispatches")}</h4>
              <p>{t("react.monitoring.entries_count", "{count} entries", { count: dispatches.length })}</p>
            </div>
          </div>
          {dispatches.length ? (
            <div className="fs-detail-rows">
              {dispatches.map((dispatch, index) => (
                <DetailRow
                  key={firstStringFromUnknownValue([dispatch.dispatch_id, dispatch.id, index])}
                  label={backendLabelOrCode(dispatch.status_label, dispatch.status || dispatch.state, "unknown", 120)}
                  value={formatUserDateTime(dispatch.attempted_at || dispatch.created_at, userTimezone)}
                />
              ))}
            </div>
          ) : (
            <p className="fs-empty-copy">{t("react.monitoring.no_dispatch_records", "No dispatch records.")}</p>
          )}
        </div>
        <div className="fs-panel-group">
          <div className="fs-panel-heading">
            <div>
              <h4 className="fs-panel-title">{t("react.monitoring.commands", "Commands")}</h4>
              <p>{t("react.monitoring.entries_count", "{count} entries", { count: allCommands.length })}</p>
            </div>
          </div>
          {allCommands.length ? (
            <div className="fs-detail-rows">
              {allCommands.map((command, index) => (
                <DetailRow
                  key={firstStringFromUnknownValue([command.command_id, command.id, index])}
                  label={commandTypeLabel(command, t)}
                  value={commandStatusLabel(command, t)}
                />
              ))}
            </div>
          ) : (
            <p className="fs-empty-copy">{t("react.monitoring.no_command_records", "No command records.")}</p>
          )}
        </div>
        {Object.keys(metadata).length ? (
          <div className="fs-panel-group">
            <h4 className="fs-panel-title">{t("events.fields.metadata", "Metadata")}</h4>
            <div className="fs-detail-rows">
              {Object.entries(metadata).map(([key, value]) => (
                <DetailRow key={key} label={key} value={normalizeValue(value)} />
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </details>
  );
}

function EventDetailContent({
  detail,
  summary,
  workflowId,
  dispatches,
  commands,
  linkedCommands,
  metadata,
  userTimezone,
  redrivePending,
  onRedrive,
}: Readonly<{
  detail: Entity;
  summary: string;
  workflowId: string;
  dispatches: EventDispatchRecord[];
  commands: EventCommandRecord[];
  linkedCommands: EventCommandRecord[];
  metadata: Entity;
  userTimezone: string;
  redrivePending: boolean;
  onRedrive: () => void;
}>) {
  const { t } = useI18n();
  const facts = [
    [t("react.monitoring.event_detail_event", "Event"), eventTypeLabel(detail, t)],
    [t("events.columns.status", "Status"), eventStatusLabel(detail, t)],
    [t("events.columns.severity", "Severity"), detail.severity],
    [t("events.columns.project", "Project"), detail.project_name || detail.project_id],
    [t("events.columns.job", "Job"), detail.job_id],
    [t("events.columns.agent", "Agent"), detail.agent || detail.agent_id],
    [t("registry.workflow_detail.step.drawer_title", "Step"), detail.step_id],
    [t("events.columns.group", "Group"), detail.group_id],
    [t("react.monitoring.event_detail_recorded", "Recorded"), formatUserDateTime(detail.created_at, userTimezone)],
  ];
  return (
    <>
      {canRedrive(normalizeStatus(detail)) ? (
        <div>
          <button className="fs-button" disabled={redrivePending} onClick={onRedrive} type="button">
            {redrivePending ? t("react.monitoring.requesting_redrive", "Requesting redrive...") : t("react.monitoring.redrive_failed_dispatches", "Redrive failed dispatches")}
          </button>
        </div>
      ) : null}
      <div className="fs-panel-group">
        <h3 className="fs-panel-title">{t("jobs.columns.summary", "Summary")}</h3>
        <p className="fs-ink-secondary">{summary || t("react.monitoring.no_human_readable_summary_was_recorded", "No human-readable summary was recorded.")}</p>
      </div>
      <div className="fs-detail-rows">
        {facts.map(([label, value]) => (
          <DetailRow
            key={stringFromUnknownValue(label)}
            label={stringFromUnknownValue(label)}
            value={normalizeValue(value)}
          />
        ))}
        <DetailRow
          label={t("jobs.columns.workflow", "Workflow")}
          value={
            workflowId ? (
              <a
                className="fs-link"
                data-testid="event-workflow-link"
                href={workflowPath(workflowId)}
                title={t("react.monitoring.workflow_id_value1", "Workflow ID: {value1}", { value1: String(workflowId) })}
              >
                {eventWorkflowLabel(detail)}
              </a>
            ) : (
              t("react.monitoring.workflow_not_recorded", "Workflow not recorded")
            )
          }
        />
      </div>
      <EventAdvancedDiagnostics
        commands={commands}
        dispatches={dispatches}
        linkedCommands={linkedCommands}
        metadata={metadata}
        userTimezone={userTimezone}
      />
    </>
  );
}

function EventDetailDrawer({
  open,
  selectedRef,
  detailMatchesSelection,
  detailTitle,
  detailSummary,
  detail,
  detailWorkflowId,
  dispatches,
  commands,
  linkedCommands,
  metadata,
  userTimezone,
  feedback,
  loading,
  fetching,
  error,
  redrivePending,
  onClose,
  onRedrive,
}: Readonly<{
  open: boolean;
  selectedRef: string;
  detailMatchesSelection: boolean;
  detailTitle: string;
  detailSummary: string;
  detail: Entity;
  detailWorkflowId: string;
  dispatches: EventDispatchRecord[];
  commands: EventCommandRecord[];
  linkedCommands: EventCommandRecord[];
  metadata: Entity;
  userTimezone: string;
  feedback: DrawerFeedback | null;
  loading: boolean;
  fetching: boolean;
  error: unknown;
  redrivePending: boolean;
  onClose: () => void;
  onRedrive: () => void;
}>) {
  const { t } = useI18n();
  const waitingForSelection = fetching && !detailMatchesSelection;
  return (
    <DsDrawer
      closeLabel={t("react.monitoring.close_event_detail", "Close event detail")}
      onClose={onClose}
      open={open}
      testId="event-detail-inspector"
      title={detailMatchesSelection ? detailTitle : t("react.monitoring.event_detail", "Event detail")}
    >
      <div className="fs-panel-stack">
        <p className="fs-ink-secondary">
          {waitingForSelection ? t("react.monitoring.loading_event_value1", "Loading event {value1}...", { value1: String(selectedRef) }) : t("react.monitoring.selected_event_value1", "Selected event {value1}", { value1: String(selectedRef) })}
        </p>
        {feedback ? <DsAlert title={feedback.message} tone={feedback.tone} /> : null}
        {loading || waitingForSelection ? <p className="fs-empty-copy">{t("react.monitoring.loading_detail", "Loading detail…")}</p> : null}
        {error ? (
          <DsAlert title={t("react.monitoring.event_detail_could_not_be_loaded", "Event detail could not be loaded")} tone="danger">
            {error instanceof Error ? error.message : t("react.monitoring.failed_to_load_event_detail", "Failed to load event detail.")}
          </DsAlert>
        ) : null}
        {detailMatchesSelection ? (
          <EventDetailContent
            commands={commands}
            detail={detail}
            dispatches={dispatches}
            linkedCommands={linkedCommands}
            metadata={metadata}
            onRedrive={onRedrive}
            redrivePending={redrivePending}
            summary={detailSummary}
            userTimezone={userTimezone}
            workflowId={detailWorkflowId}
          />
        ) : null}
      </div>
    </DsDrawer>
  );
}

export function EventsAuditTrail() {
  const { t, locale } = useI18n();
  const queryClient = useQueryClient();
  const userTimezone = useUserTimezone();
  const [workflowId, setWorkflowId] = useState("");
  const [eventFamily, setEventFamily] = useState("");
  const [eventCategory, setEventCategory] = useState("");
  const [eventType, setEventType] = useState("");
  const [status, setStatus] = useState("");
  const [period, setPeriod] = useState<PeriodValue>({ kind: "all" });
  // Expanded on every render, so a preset keeps meaning "the last N days" as
  // the days pass; today is the user's configured zone, not the browser's.
  const { fromDate, toDate } = expandPeriod(period, todayIn(userTimezone));
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(25);
  const [selectedRef, setSelectedRef] = useState("");
  const [feedback, setFeedback] = useState<DrawerFeedback | null>(null);

  const listQuery = useQuery({
    queryKey: queryKeys.eventsList({
      workflowId,
      eventFamily,
      eventCategory,
      eventType,
      status,
      fromDate,
      toDate,
      page,
      perPage,
    }),
    queryFn: () =>
      fetchEventsList({
        workflowId,
        eventFamily,
        eventCategory,
        eventType,
        status,
        fromDate,
        toDate,
        page,
        perPage,
      }),
    refetchInterval: 20_000,
  });

  const detailQuery = useQuery({
    queryKey: queryKeys.eventDetail(selectedRef),
    queryFn: () => fetchEventDetail(selectedRef),
    enabled: Boolean(selectedRef),
  });

  const redriveMutation = useMutation({
    mutationFn: redriveEvent,
    onSuccess: async () => {
      setFeedback({ message: t("react.monitoring.redrive_requested_successfully", "Redrive requested successfully."), tone: "success" });
      await queryClient.invalidateQueries({
        queryKey: queryKeys.eventsList({
          workflowId,
          eventFamily,
          eventCategory,
          eventType,
          status,
          fromDate,
          toDate,
          page,
          perPage,
        }),
      });
      if (selectedRef) {
        await queryClient.invalidateQueries({
          queryKey: queryKeys.eventDetail(selectedRef),
        });
      }
    },
    onError: (error) => {
      setFeedback({ message: error instanceof Error ? error.message : t("react.monitoring.failed_to_redrive_event", "Failed to redrive event."), tone: "danger" });
    },
  });

  const events = listQuery.data?.events || [];
  const facets = listQuery.data?.facets || {};
  const workflowOptions = Array.isArray(facets.workflows) ? facets.workflows : [];
  const familyOptions = Array.isArray(facets.event_families) ? facets.event_families : [];
  const categoryOptions = Array.isArray(facets.event_categories)
    ? facets.event_categories
    : [];
  const typeOptions = Array.isArray(facets.event_types) ? facets.event_types : [];
  const statusOptions = Array.isArray(facets.statuses) ? facets.statuses : [];
  const workflowOptionLabels = new Map(
    workflowOptions.map((option) => [firstStringFromUnknownValue([option.value, ""]).trim(), firstStringFromUnknownValue([option.label, ""]).trim()]),
  );
  const familyOptionLabels = new Map(
    familyOptions.map((option) => [firstStringFromUnknownValue([option.value, ""]).trim(), firstStringFromUnknownValue([option.label, ""]).trim()]),
  );
  const categoryOptionLabels = new Map(
    categoryOptions.map((option) => [firstStringFromUnknownValue([option.value, ""]).trim(), firstStringFromUnknownValue([option.label, ""]).trim()]),
  );
  const typeOptionLabels = new Map(
    typeOptions.map((option) => [firstStringFromUnknownValue([option.value, ""]).trim(), firstStringFromUnknownValue([option.label, ""]).trim()]),
  );
  const statusOptionLabels = new Map(
    statusOptions.map((option) => [firstStringFromUnknownValue([option.value, ""]).trim(), firstStringFromUnknownValue([option.label, ""]).trim()]),
  );
  const total = Number(listQuery.data?.total || 0);
  const totalPages = Math.max(1, Math.ceil(total / Math.max(1, perPage)));
  const rawDetail = objectOrEmpty(detailQuery.data);
  const detailRef = eventRef(rawDetail as EventListItem);
  const detailMatchesSelection = Boolean(selectedRef) && detailRef === selectedRef;
  const detail = detailMatchesSelection ? rawDetail : {};
  const dispatches = (Array.isArray(detail.dispatches) ? detail.dispatches : []) as EventDispatchRecord[];
  const commands = (Array.isArray(detail.commands) ? detail.commands : []) as EventCommandRecord[];
  const linkedCommands = (Array.isArray(detail.linked_commands) ? detail.linked_commands : []) as EventCommandRecord[];
  const metadata = objectOrEmpty(detail.metadata || detail.payload || detail.attributes);
  const detailWorkflowId = eventWorkflowId(detail);
  const detailTitle = readableText(detail.title, 160) || eventTypeLabel(detail, t) || t("react.monitoring.event_detail", "Event detail");
  const detailSummary = readableText(
    detail.summary || detail.message || detail.detail || metadata.summary || metadata.message,
    520,
  );
  const clearSelection = () => {
    setSelectedRef("");
    setFeedback(null);
  };
  const resetPage = () => setPage(1);
  const changeWorkflow = (value: string) => {
    setWorkflowId(value);
    resetPage();
  };
  const changeFamily = (value: string) => {
    setEventFamily(value);
    setEventCategory("");
    setEventType("");
    setStatus("");
    resetPage();
  };
  const changeCategory = (value: string) => {
    setEventCategory(value);
    setEventType("");
    setStatus("");
    resetPage();
  };
  const changeType = (value: string) => {
    setEventType(value);
    setStatus("");
    resetPage();
  };
  const changeStatus = (value: string) => {
    setStatus(value);
    resetPage();
  };
  const changePeriod = (value: PeriodValue) => {
    setPeriod(value);
    resetPage();
  };
  const clearFilters = () => {
    setWorkflowId("");
    setEventFamily("");
    setEventCategory("");
    setEventType("");
    setStatus("");
    setPeriod({ kind: "all" });
    resetPage();
  };
  const selectEvent = (ref: string) => {
    if (ref) setSelectedRef(ref);
    setFeedback(null);
  };
  const changePerPage = (value: number) => {
    setPerPage(value);
    resetPage();
  };
  const refresh = () => {
    ignoreAsyncResult(
      queryClient.invalidateQueries({
        queryKey: queryKeys.eventsList({
          workflowId,
          eventFamily,
          eventCategory,
          eventType,
          status,
          fromDate,
          toDate,
          page,
          perPage,
        }),
      }),
    );
  };
  const requestRedrive = () => {
    if (selectedRef) {
      ignoreAsyncResult(redriveMutation.mutateAsync(selectedRef));
    }
  };
  // All events means the filter is off, so it carries no chip.
  const periodChips: ActiveFilterChip[] =
    period.kind === "all"
      ? []
      : [{ key: "period", label: periodLabel(period, t("react.monitoring.all_events", "All events"), locale, {
        today: t("react.shared.period_today", "Today"),
        last7: t("react.dashboard.last_7_days", "Last 7 days"),
        last30: t("react.dashboard.last_30_days", "Last 30 days"),
      }), clear: () => changePeriod({ kind: "all" }) }];
  const activeFilterChips = periodChips.concat(createActiveFilterChips(
    { workflowId, eventFamily, eventCategory, eventType, status },
    {
      workflows: workflowOptionLabels,
      families: familyOptionLabels,
      categories: categoryOptionLabels,
      types: typeOptionLabels,
      statuses: statusOptionLabels,
    },
    {
      workflow: () => changeWorkflow(""),
      family: () => changeFamily(""),
      category: () => changeCategory(""),
      type: () => changeType(""),
      status: () => changeStatus(""),
    },
  ));

  useEffect(() => {
    if (!selectedRef) return undefined;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") clearSelection();
    };
    globalThis.addEventListener("keydown", handleEscape);
    return () => globalThis.removeEventListener("keydown", handleEscape);
  }, [selectedRef]);

  return (
    <section className="fs-panel-stack">
      <EventsFiltersCard
        categoryOptions={categoryOptions}
        eventCategory={eventCategory}
        eventFamily={eventFamily}
        eventType={eventType}
        familyOptions={familyOptions}
        onCategoryChange={changeCategory}
        onClear={clearFilters}
        onFamilyChange={changeFamily}
        onPeriodChange={changePeriod}
        onRefresh={refresh}
        onStatusChange={changeStatus}
        onTypeChange={changeType}
        onWorkflowChange={changeWorkflow}
        refreshing={listQuery.isFetching}
        status={status}
        statusOptions={statusOptions}
        period={period}
        typeOptions={typeOptions}
        workflowId={workflowId}
        workflowOptions={workflowOptions}
      />
      <EventsListCard
        chips={activeFilterChips}
        error={listQuery.isError ? listQuery.error : null}
        events={events}
        loading={listQuery.isLoading}
        onPageChange={setPage}
        onPerPageChange={changePerPage}
        onSelect={selectEvent}
        page={page}
        perPage={perPage}
        selectedRef={selectedRef}
        total={total}
        totalPages={totalPages}
        userTimezone={userTimezone}
      />
      <EventDetailDrawer
        commands={commands}
        detail={detail}
        detailMatchesSelection={detailMatchesSelection}
        detailSummary={detailSummary}
        detailTitle={detailTitle}
        detailWorkflowId={detailWorkflowId}
        dispatches={dispatches}
        error={detailQuery.isError ? detailQuery.error : null}
        feedback={feedback}
        fetching={detailQuery.isFetching}
        linkedCommands={linkedCommands}
        loading={detailQuery.isLoading}
        metadata={metadata}
        onClose={clearSelection}
        onRedrive={requestRedrive}
        open={Boolean(selectedRef)}
        redrivePending={redriveMutation.isPending}
        selectedRef={selectedRef}
        userTimezone={userTimezone}
      />
    </section>
  );
}
