/* eslint-disable @typescript-eslint/no-explicit-any */
import { useQuery } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useEffect, useId, useMemo, useState, type ReactNode } from "react";

import { DsAlert, DsBadge, DsCheckbox, DsRadio, DsSelect, DsTable, DsTabs } from "@/components/shared/design-system";
import { DsSwitch } from "@/components/shared/ds-switch";
import { Button } from "@/components/ui/button";
import { WorkflowInputFields } from "@/components/workflow-inputs/workflow-input-fields";
import type { FormFieldView, FormFiles, FormValues } from "@/components/workflow-inputs/form-field";
import type { I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import type { OperatorIssue } from "@/pages/operator/types";
import { fetchWorkflowLinkedProjects } from "@/pages/workflows/shared/project-context";
import { DrawerFooter, DrawerFooterSpacer } from "./detail-drawer-footer";
import { IssueLines, MoveButtons, OperatorFieldList, type FieldListState } from "./detail-operator-form-editor";
import { kindLabel } from "./detail-operator-labels";
import {
  filterTypesFor,
  isListLikeInput,
  issuesUnder,
  issueTarget,
  mergedOrder,
  moveAt,
  moveInOrder,
  OPERATOR_SETTINGS_PATH,
  previewFormFields,
  schemaFieldKind,
  schemaProperties,
  valuePathOptions,
  type OperatorColumnDraft,
  type OperatorEditorView,
  type OperatorFieldTab,
  type OperatorFilterDraft,
  type OperatorResultConfig,
  type OperatorSettingsDraft,
  type OperatorSubTab,
  type OperatorValueSource,
} from "./detail-operator-settings-model";
import { SaveStateButton } from "./detail-save-button";

type T = I18nContextValue["t"];
type Ctx = Record<string, any>;

function displayLabel(display: string, t: T): string {
  const labels: Record<string, string> = {
    auto: t("react.workflows.operator.display_auto", "Automatic"),
    text: t("react.workflows.operator.display_text", "Text"),
    number: t("react.workflows.operator.display_number", "Number"),
    date: t("react.workflows.operator.display_date", "Date"),
    datetime: t("react.workflows.operator.display_datetime", "Date and time"),
    time: t("react.workflows.operator.display_time", "Time"),
    wysiwyg: t("react.workflows.operator.display_wysiwyg", "Formatted text"),
    boolean: t("react.workflows.operator.display_boolean", "Yes / no"),
    file_links: t("react.workflows.operator.display_file_links", "File links"),
    table: t("react.workflows.operator.display_table", "Table"),
    list: t("react.workflows.operator.display_list", "List"),
    json: t("react.workflows.operator.display_json", "JSON"),
    currency: t("react.workflows.operator.display_currency", "Currency"),
    status: t("react.workflows.operator.display_status", "Status"),
    user: t("react.workflows.operator.display_user", "User"),
    count: t("react.workflows.operator.display_count", "Count"),
  };
  return labels[display] ?? display;
}

function jobColumnLabel(path: string, t: T): string {
  const labels: Record<string, string> = {
    job_id: t("react.workflows.operator.job_id", "Run ID"),
    status: t("react.workflows.operator.job_status", "Status"),
    created_at: t("react.workflows.operator.job_created_at", "Created"),
    started_at: t("react.workflows.operator.job_started_at", "Started"),
    finished_at: t("react.workflows.operator.job_finished_at", "Finished"),
    duration_ms: t("react.workflows.operator.job_duration", "Duration"),
    submitted_by: t("react.workflows.operator.job_submitted_by", "Submitted by"),
  };
  return labels[path] ?? path;
}

function filterTypeLabel(type: string, t: T): string {
  const labels: Record<string, string> = {
    search: t("react.workflows.operator.filter_search", "Text search"),
    number_range: t("react.workflows.operator.filter_number_range", "Number range"),
    date_range: t("react.workflows.operator.filter_date_range", "Date range"),
    boolean: t("react.workflows.operator.filter_boolean", "Yes / no"),
    choice: t("react.workflows.operator.filter_choice", "One choice"),
    choice_multi: t("react.workflows.operator.filter_choice_multi", "Several choices"),
  };
  return labels[type] ?? type;
}

function sourceLabel(source: OperatorValueSource, t: T): string {
  if (source === "job") return t("react.workflows.operator.source_job", "Run");
  if (source === "output") return t("react.workflows.operator.source_output", "Output");
  return t("react.workflows.operator.source_input", "Input");
}

/** The operator form with the draft's settings, filled in place; nothing is sent. */
function FormPreview({ view, draft }: Readonly<{ view: OperatorEditorView; draft: OperatorSettingsDraft }>) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<FormValues>({});
  const [files, setFiles] = useState<FormFiles>({});
  const fields = useMemo(() => previewFormFields(view.form_fields, draft, view.input_schema), [draft, view.form_fields, view.input_schema]);
  return (
    <section className="space-y-2 rounded-lg border border-default p-3" aria-label={t("react.workflows.operator.preview", "Preview")}>
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.operator.preview", "Preview")}</p>
        <Button aria-expanded={open} onClick={() => setOpen(!open)} size="sm" type="button" variant="ghost">
          {open ? t("react.workflows.hide", "Hide") : t("react.workflows.show", "Show")}
        </Button>
      </div>
      {open ? <>
        <p className="fs-field-helper">{t("react.workflows.operator.preview_help", "The form as an operator sees it with these settings. Nothing is sent from here.")}</p>
        <WorkflowInputFields fields={fields} files={files} onChange={(name, value) => setValues((current) => ({ ...current, [name]: value }))} onFiles={(name, next) => setFiles((current) => ({ ...current, [name]: next }))} values={values} />
      </> : null}
    </section>
  );
}

function ResultEditor({ view, draft, setDraft, issues }: Readonly<{ view: OperatorEditorView; draft: OperatorSettingsDraft; setDraft: (next: OperatorSettingsDraft) => void; issues: readonly OperatorIssue[] }>) {
  const { t } = useI18n();
  const nodes = schemaProperties(view.output_schema);
  const contractOrder = Object.keys(nodes);
  const order = mergedOrder(draft.result.order, contractOrder);
  const displays = view.compatibility?.result_displays ?? ["auto"];
  const update = (name: string, patch: Partial<OperatorResultConfig>) => {
    const current = draft.result.fields[name] ?? { label: "", display: "auto", hidden: false, decimals: null };
    setDraft({ ...draft, result: { ...draft.result, fields: { ...draft.result.fields, [name]: { ...current, ...patch } } } });
  };
  if (!order.length) return <p className="workflow-contract-empty">{t("react.workflows.operator.no_outputs", "The workflow declares no outputs or returned files.")}</p>;
  return (
    <div className="space-y-2" data-testid="operator-result-editor">
      <p className="fs-field-helper">{t("react.workflows.operator.result_help", "How a finished run shows its outputs and returned files to the operator.")}</p>
      <IssueLines issues={issuesUnder(issues, `${OPERATOR_SETTINGS_PATH}.result`)} />
      {order.map((name, index) => {
        const node = nodes[name] ?? {};
        const config = draft.result.fields[name] ?? { label: "", display: "auto", hidden: false, decimals: null };
        const label = config.label || String(node.title ?? "") || name;
        const kind = schemaFieldKind(node);
        return (
          <div className="grid items-end gap-2 rounded-lg border border-default p-2 sm:grid-cols-[minmax(0,1.2fr)_minmax(0,1.5fr)_minmax(0,1.2fr)_6rem_auto_auto]" data-testid={`operator-result-row-${name}`} key={name}>
            <span className="min-w-0 text-xs">
              <span className="block truncate font-medium">{label}</span>
              <span className="block truncate font-mono fs-ink-tertiary">{name} · {kindLabel(kind, t)}</span>
            </span>
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.label", "Label")}</span>
              <input className="fs-input mt-1 w-full" onChange={(event) => update(name, { label: event.target.value })} placeholder={String(node.title ?? "") || name} value={config.label} />
            </label>
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.operator.display", "Display")}</span>
              <DsSelect ariaLabel={t("react.workflows.operator.display_of", "Display of {name}", { name })} className="mt-1" onChange={(display) => update(name, { display })} options={displays.map((display) => ({ value: display, label: displayLabel(display, t) }))} value={config.display || "auto"} />
            </label>
            <label className="block text-xs fs-ink-secondary">
              <span>{t("react.workflows.operator.decimals", "Decimals")}</span>
              <input className="fs-input mt-1 w-full" disabled={kind !== "number"} max={10} min={0} onChange={(event) => {
                const value = event.target.value.trim();
                update(name, { decimals: value === "" ? null : Math.max(0, Math.min(10, Math.round(Number(value)))) });
              }} type="number" value={config.decimals ?? ""} />
            </label>
            <span className="flex items-center gap-2 pb-1 text-xs">
              <DsSwitch checked={config.hidden} label={t("react.workflows.operator.result_hidden", "Hide {name}", { name })} onCheckedChange={(hidden) => update(name, { hidden })} />
              <span>{t("react.workflows.operator.hidden_short", "Hidden")}</span>
            </span>
            <MoveButtons count={order.length} index={index} name={label} onMove={(delta) => setDraft({ ...draft, result: { ...draft.result, order: moveInOrder(order, name, delta) } })} />
          </div>
        );
      })}
    </div>
  );
}

function ListEditor({ view, draft, setDraft, issues }: Readonly<{ view: OperatorEditorView; draft: OperatorSettingsDraft; setDraft: (next: OperatorSettingsDraft) => void; issues: readonly OperatorIssue[] }>) {
  const { t } = useI18n();
  const compatibility = view.compatibility ?? {};
  const inputPaths = valuePathOptions(view.input_schema);
  const outputPaths = valuePathOptions(view.output_schema);
  const pathOptions = (source: OperatorValueSource) => {
    if (source === "job") return (compatibility.job_columns ?? []).map((path) => ({ value: path, label: jobColumnLabel(path, t) }));
    return (source === "input" ? inputPaths : outputPaths).map((option) => ({ value: option.path, label: option.title ? `${option.title} (${option.path})` : option.path }));
  };
  const formats = compatibility.column_formats ?? ["auto"];
  const columns = draft.list.columns;
  const filters = draft.list.filters;
  const setColumns = (next: OperatorColumnDraft[]) => setDraft({ ...draft, list: { ...draft.list, columns: next } });
  const setFilters = (next: OperatorFilterDraft[]) => setDraft({ ...draft, list: { ...draft.list, filters: next } });
  const updateColumn = (index: number, patch: Partial<OperatorColumnDraft>) => setColumns(columns.map((column, at) => at === index ? { ...column, ...patch } : column));
  // A filter names a top-level value with at least one filter type; files and groups have none.
  const filterValues = [
    ...Object.keys(compatibility.job_filters ?? {}).map((path) => ({ source: "job" as const, path })),
    ...Object.keys(schemaProperties(view.input_schema)).map((path) => ({ source: "input" as const, path })),
    ...Object.keys(schemaProperties(view.output_schema)).map((path) => ({ source: "output" as const, path })),
  ].filter(({ source, path }) => filterTypesFor(view, source, path).length > 0);
  // Source first, then a value of that source, as for columns; only sources with a filterable value.
  const filterSources = (["job", "input", "output"] as const).filter((source) => filterValues.some((value) => value.source === source));
  const setFilterValue = (index: number, source: OperatorValueSource, path: string) => {
    const nextTypes = filterTypesFor(view, source, path);
    setFilters(filters.map((row, at) => at === index ? { ...row, source, path, id: "", type: (nextTypes.includes(row.type) ? row.type : nextTypes[0] ?? "") as OperatorFilterDraft["type"] } : row));
  };
  const columnIssues = (index: number) => issuesUnder(issues, `${OPERATOR_SETTINGS_PATH}.list.columns[${index}]`);
  const filterIssues = (index: number) => issuesUnder(issues, `${OPERATOR_SETTINGS_PATH}.list.filters[${index}]`);
  return (
    <div className="space-y-4">
      <section className="space-y-2" data-testid="operator-list-columns">
        <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.operator.columns", "Columns")}</p>
        <p className="fs-field-helper">{t("react.workflows.operator.columns_help", "What the operator's list of runs shows: fields of the run, inputs and outputs. Files show as download links.")}</p>
        {columns.length ? null : <p className="workflow-contract-empty">{t("react.workflows.operator.no_columns", "No columns: the list shows the run, its status and when it was created.")}</p>}
        {columns.length ? (
          <DsTable
            ariaLabel={t("react.workflows.operator.columns", "Columns")}
            columns={[
              {
                key: "source",
                header: t("react.workflows.operator.source", "Source"),
                width: 140,
                cell: ({ column, number, index }) => <DsSelect ariaLabel={t("react.workflows.operator.column_source", "Column {number} source", { number })} onChange={(source) => updateColumn(index, { source: source as OperatorValueSource, path: "", id: "" })} options={(["job", "input", "output"] as const).map((source) => ({ value: source, label: sourceLabel(source, t) }))} value={column.source} />,
              },
              {
                key: "value",
                header: t("react.workflows.operator.value", "Value"),
                cell: ({ column, number, index }) => <DsSelect ariaLabel={t("react.workflows.operator.column_value", "Column {number} value", { number })} onChange={(path) => updateColumn(index, { path, id: "" })} options={pathOptions(column.source)} searchable value={column.path} />,
              },
              {
                key: "heading",
                header: t("react.workflows.operator.heading", "Heading"),
                cell: ({ column, number, index }) => <input aria-label={t("react.workflows.operator.column_heading", "Column {number} heading", { number })} className="fs-input w-full" onChange={(event) => updateColumn(index, { label: event.target.value })} value={column.label} />,
              },
              {
                key: "format",
                header: t("react.workflows.operator.format", "Format"),
                width: 170,
                cell: ({ column, number, index }) => <DsSelect ariaLabel={t("react.workflows.operator.column_format", "Column {number} format", { number })} onChange={(format) => updateColumn(index, { format })} options={formats.map((format) => ({ value: format, label: displayLabel(format, t) }))} value={column.format || "auto"} />,
              },
              {
                key: "actions",
                ariaLabel: "Actions",
                header: null,
                cell: ({ column, number, index }) => (
                  <span className="inline-flex items-center gap-1">
                    <MoveButtons count={columns.length} index={index} name={column.label || column.path || String(number)} onMove={(delta) => setColumns(moveAt(columns, index, delta))} />
                    <Button aria-label={t("react.workflows.operator.remove_column", "Remove column {number}", { number })} onClick={() => setColumns(columns.filter((_, at) => at !== index))} size="sm" type="button" variant="ghost-danger"><Trash2 aria-hidden="true" /></Button>
                  </span>
                ),
              },
            ]}
            embedded
            rowKey={({ index }) => `column-${index}`}
            rows={columns.map((column, index) => ({ column, index, number: index + 1 }))}
          />
        ) : null}
        <IssueLines issues={columns.flatMap((_, index) => columnIssues(index).map((issue) => ({ ...issue, message: t("react.workflows.operator.column_issue", "Column {number}: {message}", { number: index + 1, message: issue.message }) })))} />
        <Button onClick={() => setColumns([...columns, { id: "", source: "input", path: inputPaths[0]?.path ?? "", label: "", format: "auto" }])} size="sm" type="button" variant="secondary">
          <Plus aria-hidden="true" />{t("react.workflows.operator.add_column", "Add column")}
        </Button>
      </section>
      <section className="space-y-2" data-testid="operator-list-filters">
        <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.operator.filters", "Filters")}</p>
        <p className="fs-field-helper">{t("react.workflows.operator.filters_help", "Filters compare the full submitted values. Files cannot be filtered. Every list has the run status filter with the built-in Awaiting review status.")}</p>
        {filters.length ? (
          <DsTable
            ariaLabel={t("react.workflows.operator.filters", "Filters")}
            columns={[
              {
                key: "source",
                header: t("react.workflows.operator.source", "Source"),
                width: 140,
                cell: ({ filter, number, index }) => <DsSelect ariaLabel={t("react.workflows.operator.filter_source", "Filter {number} source", { number })} onChange={(source) => {
                  const first = filterValues.find((value) => value.source === source);
                  if (first) setFilterValue(index, first.source, first.path);
                }} options={filterSources.map((source) => ({ value: source, label: sourceLabel(source, t) }))} value={filter.source} />,
              },
              {
                key: "value",
                header: t("react.workflows.operator.value", "Value"),
                cell: ({ filter, number, index }) => <DsSelect ariaLabel={t("react.workflows.operator.filter_value", "Filter {number} value", { number })} onChange={(path) => setFilterValue(index, filter.source, path)} options={filterValues.filter(({ source }) => source === filter.source).map(({ source, path }) => ({ value: path, label: source === "job" ? jobColumnLabel(path, t) : path }))} searchable value={filter.path} />,
              },
              {
                key: "type",
                header: t("react.workflows.operator.filter_type", "Filter type"),
                width: 180,
                cell: ({ filter, number, index }) => <DsSelect ariaLabel={t("react.workflows.operator.filter_type_of", "Filter {number} type", { number })} onChange={(type) => setFilters(filters.map((row, at) => at === index ? { ...row, type: type as OperatorFilterDraft["type"] } : row))} options={filterTypesFor(view, filter.source, filter.path).map((type) => ({ value: type, label: filterTypeLabel(type, t) }))} value={filter.type} />,
              },
              {
                key: "label",
                header: t("react.workflows.label", "Label"),
                cell: ({ filter, number, index }) => <input aria-label={t("react.workflows.operator.filter_label", "Filter {number} label", { number })} className="fs-input w-full" onChange={(event) => setFilters(filters.map((row, at) => at === index ? { ...row, label: event.target.value } : row))} value={filter.label} />,
              },
              {
                key: "actions",
                ariaLabel: "Actions",
                header: null,
                cell: ({ number, index }) => <Button aria-label={t("react.workflows.operator.remove_filter", "Remove filter {number}", { number })} onClick={() => setFilters(filters.filter((_, at) => at !== index))} size="sm" type="button" variant="ghost-danger"><Trash2 aria-hidden="true" /></Button>,
              },
            ]}
            embedded
            rowKey={({ index }) => `filter-${index}`}
            rows={filters.map((filter, index) => ({ filter, index, number: index + 1 }))}
          />
        ) : null}
        <IssueLines issues={filters.flatMap((_, index) => filterIssues(index).map((issue) => ({ ...issue, message: t("react.workflows.operator.filter_issue", "Filter {number}: {message}", { number: index + 1, message: issue.message }) })))} />
        <Button disabled={!filterValues.length} onClick={() => {
          const first = filterValues.find(({ source, path }) => !filters.some((row) => row.source === source && row.path === path)) ?? filterValues[0];
          if (!first) return;
          setFilters([...filters, { id: "", source: first.source, path: first.path, type: (filterTypesFor(view, first.source, first.path)[0] ?? "") as OperatorFilterDraft["type"], label: "" }]);
        }} size="sm" type="button" variant="secondary">
          <Plus aria-hidden="true" />{t("react.workflows.operator.add_filter", "Add filter")}
        </Button>
      </section>
    </div>
  );
}

function acknowledgementText(view: OperatorEditorView, draft: OperatorSettingsDraft, t: T): string {
  if (draft.acknowledge) return t("react.workflows.operator.ack_pending", "Confirmed with the next save.");
  const ack = draft.effects_acknowledged;
  if (ack && view.effects_acknowledged) {
    const at = typeof ack.at === "string" && ack.at ? new Date(ack.at).toLocaleString() : "";
    return t("react.workflows.operator.ack_done", "Confirmed by {by} on {at}, revision {revision}.", { by: String(ack.by ?? ""), at, revision: String(ack.revision ?? "") });
  }
  if (ack) return t("react.workflows.operator.ack_stale", "The contract changed after the last confirmation. Confirm the real effects again.");
  return t("react.workflows.operator.ack_missing", "Not confirmed. Operators cannot run the workflow until you confirm.");
}

function PublicationEditor({ ctx, view, draft, setDraft, issues, onJump }: Readonly<{ ctx: Ctx; view: OperatorEditorView; draft: OperatorSettingsDraft; setDraft: (next: OperatorSettingsDraft) => void; issues: readonly OperatorIssue[]; onJump: (path: string) => void }>) {
  const { t } = useI18n();
  const radioName = useId();
  const channels: string[] = Array.isArray(ctx.workflowInterfaceTriggerChannelsDraft) ? ctx.workflowInterfaceTriggerChannelsDraft : [];
  const uiOn = ctx.workflowInterfaceEnabledDraft !== false && channels.includes("ui");
  const linked = useQuery({
    queryKey: ["workflows", String(ctx.workflowId), "hook-projects"],
    queryFn: () => fetchWorkflowLinkedProjects(String(ctx.workflowId)),
    enabled: Boolean(ctx.workflowId),
    retry: false,
  });
  const inputs = schemaProperties(view.input_schema);
  const targets = Object.entries(inputs).filter(([name, node]) => isListLikeInput(node) && view.form_fields.some((field) => field.name === name));
  const submissionIssues = issuesUnder(issues, `${OPERATOR_SETTINGS_PATH}.submission`);
  return (
    <div className="space-y-4" data-testid="operator-publication">
      <section className="space-y-2">
        <div className="flex items-center gap-2 text-sm">
          <DsSwitch checked={draft.published} disabled={!uiOn && !draft.published} label={t("react.workflows.operator.publish", "Show to operators")} onCheckedChange={(published) => setDraft({ ...draft, published })} />
          <span>{t("react.workflows.operator.publish", "Show to operators")}</span>
        </div>
        {uiOn ? (
          <p className="fs-field-helper">{t("react.workflows.operator.publish_help", "Operators and viewers of every project linked to this workflow see it in their menu.")}</p>
        ) : (
          <p className="fs-field-helper">
            {t("react.workflows.operator.publish_needs_ui", "Turn on the UI trigger channel in Smart setup first: the operator form starts runs through it.")}{" "}
            <Button onClick={() => ctx.setDrawerTab?.("smart-setup")} size="sm" type="button" variant="ghost">{t("react.workflows.operator.open_smart_setup", "Open Smart setup")}</Button>
          </p>
        )}
        <label className="block text-xs fs-ink-secondary">
          <span>{t("react.workflows.operator.menu_title", "Menu title")}</span>
          <input className="fs-input mt-1 w-full" maxLength={120} onChange={(event) => setDraft({ ...draft, menu_title: event.target.value })} placeholder={view.workflow_name} value={draft.menu_title} />
        </label>
        <div className="text-xs">
          <p className="fs-ink-secondary">{t("react.workflows.operator.appears_in", "Where it appears")}</p>
          {linked.isSuccess ? (
            linked.data.length ? <p>{linked.data.map((project) => project.name || project.id).join(", ")}</p>
              : <p className="fs-field-helper">{t("react.workflows.operator.no_linked_projects", "No project links this workflow yet. Link it to a project to show it there.")}</p>
          ) : null}
        </div>
      </section>
      <section className="space-y-2">
        <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.operator.submission", "Submission")}</p>
        <div aria-label={t("react.workflows.operator.submission", "Submission")} className="flex flex-col gap-2" role="radiogroup">
          <DsRadio checked={draft.submission.mode === "bulk"} name={radioName} onChange={() => setDraft({ ...draft, submission: { mode: "bulk", target_input: "" } })} value="bulk">
            {t("react.workflows.operator.mode_bulk", "Bulk: one run with everything the operator submits")}
          </DsRadio>
          <DsRadio checked={draft.submission.mode === "foreach"} name={radioName} onChange={() => setDraft({ ...draft, submission: { mode: "foreach", target_input: draft.submission.target_input || targets[0]?.[0] || "" } })} value="foreach">
            {t("react.workflows.operator.mode_foreach", "For each: one run per item of a list or file input; the other values repeat in each run")}
          </DsRadio>
        </div>
        {draft.submission.mode === "foreach" ? (
          <label className="block text-xs fs-ink-secondary">
            <span>{t("react.workflows.operator.foreach_target", "One run per item of")}</span>
            <DsSelect ariaLabel={t("react.workflows.operator.foreach_target", "One run per item of")} className="mt-1" onChange={(target_input) => setDraft({ ...draft, submission: { mode: "foreach", target_input } })} options={targets.map(([name, node]) => ({ value: name, label: String(node.title ?? "") || name }))} value={draft.submission.target_input} />
          </label>
        ) : null}
        <IssueLines issues={submissionIssues} />
      </section>
      <section className="space-y-2">
        <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.operator.effects", "Real effects")}</p>
        <p className="text-xs" data-testid="operator-ack-status">{acknowledgementText(view, draft, t)}</p>
        {/* Saved with the workflow settings: ticked confirms the current contract, cleared withdraws the confirmation. */}
        <DsCheckbox
          checked={draft.acknowledge || Boolean(draft.effects_acknowledged && view.effects_acknowledged)}
          onChange={(checked) => setDraft(checked ? { ...draft, acknowledge: true } : { ...draft, acknowledge: false, effects_acknowledged: null })}
        >
          {t("react.workflows.operator.acknowledge", "Confirm real effects")}
        </DsCheckbox>
        <p className="fs-field-helper">{t("react.workflows.operator.ack_help", "An operator's submission runs the workflow with its real effects. A change of the workflow contract clears the confirmation.")}</p>
      </section>
      <section className="space-y-2" data-testid="operator-issues">
        <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-secondary">{t("react.workflows.operator.issues", "Issues")}</p>
        {issues.length ? (
          <ul className="space-y-1">
            {issues.map((issue) => (
              <li className="flex flex-wrap items-center gap-2 text-xs" key={`${issue.code}:${issue.path}`}>
                <DsBadge tone={issue.severity === "warning" ? "orange" : "danger"}>{issue.severity === "warning" ? t("react.workflows.operator.warning", "Warning") : t("react.workflows.operator.error", "Error")}</DsBadge>
                <span>{issue.message}</span>
                {issue.path && issueTarget(issue.path).tab !== "publication" ? (
                  <Button onClick={() => onJump(issue.path ?? "")} size="sm" type="button" variant="ghost">{t("react.workflows.operator.show_issue", "Show")}</Button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : <p className="fs-field-helper">{t("react.workflows.operator.no_issues", "No issues.")}</p>}
      </section>
    </div>
  );
}

const NO_ISSUES: OperatorIssue[] = [];

function mergedIssues(saveIssues: readonly OperatorIssue[], viewIssues: readonly OperatorIssue[]): OperatorIssue[] {
  const seen = new Set(saveIssues.map((issue) => `${issue.code}:${issue.path}`));
  return [...saveIssues, ...viewIssues.filter((issue) => !seen.has(`${issue.code}:${issue.path}`))];
}

/**
 * Workflow Settings → For operators (ADR 0075): the form over the contract's
 * inputs, the result, the jobs list and the publication. One draft, saved with
 * the workflow settings; the server checks it and names each issue by path.
 */
export function OperatorSettingsSurface({ ctx }: Readonly<{ ctx: Ctx }>) {
  const { t } = useI18n();
  const idPrefix = useId();
  const [subTab, setSubTab] = useState<OperatorSubTab>("form");
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [fieldTabs, setFieldTabs] = useState<Record<string, OperatorFieldTab>>({});
  const view = ctx.workflowContractQuery?.data?.operator as OperatorEditorView | undefined;
  const draft = ctx.operatorSettingsDraft as OperatorSettingsDraft | null;
  const saveIssues: OperatorIssue[] = Array.isArray(ctx.operatorSaveIssues) ? ctx.operatorSaveIssues : NO_ISSUES;
  const issues = useMemo(() => mergedIssues(saveIssues, view?.issues ?? []), [saveIssues, view?.issues]);
  const jump = (path: string) => {
    const target = issueTarget(path);
    setSubTab(target.tab);
    if (target.field) {
      const key = target.field.join("/");
      setOpenKey(key);
      setFieldTabs((current) => {
        const next = { ...current, [key]: target.fieldTab ?? "general" };
        // Sub-fields are listed on their group's Presentation tab.
        for (let depth = 1; depth < (target.field?.length ?? 0); depth += 1) {
          next[target.field?.slice(0, depth).join("/") ?? ""] = "presentation";
        }
        return next;
      });
    }
  };
  // A refused save opens the first field it names.
  const firstSaveIssue = saveIssues.find((issue) => issue.severity !== "warning")?.path ?? "";
  useEffect(() => {
    if (firstSaveIssue) jump(firstSaveIssue);
    // jump only reads state setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [firstSaveIssue, saveIssues]);
  if (!view || !draft) {
    return <DsAlert title={t("react.workflows.operator.unavailable", "The operator form is not available")} tone="warning">
      {t("react.workflows.operator.unavailable_help", "The workflow contract could not be read for the operator form.")}
    </DsAlert>;
  }
  const setDraft = (next: OperatorSettingsDraft) => ctx.setOperatorSettingsDraft(next);
  const nodes = schemaProperties(view.input_schema);
  const byName = new Map(view.form_fields.map((field) => [field.name, field]));
  const contractOrder = Object.keys(nodes).filter((name) => byName.has(name));
  const order = mergedOrder(draft.form.order, contractOrder);
  const fields = order.map((name) => byName.get(name)).filter((field): field is FormFieldView => Boolean(field));
  const state: FieldListState = {
    openKey,
    setOpenKey,
    fieldTabs,
    setFieldTab: (key, tab) => setFieldTabs((current) => ({ ...current, [key]: tab })),
  };
  const errorCount = (prefix: string) => issuesUnder(issues, prefix).filter((issue) => issue.severity !== "warning").length;
  const tabLabel = (label: string, prefix: string) => {
    const count = errorCount(prefix);
    return count ? `${label} (${count})` : label;
  };
  const tabs = [
    { key: "form", label: tabLabel(t("react.workflows.operator.sub_form", "Form"), `${OPERATOR_SETTINGS_PATH}.form`) },
    { key: "result", label: tabLabel(t("react.workflows.operator.sub_result", "Result"), `${OPERATOR_SETTINGS_PATH}.result`) },
    { key: "list", label: tabLabel(t("react.workflows.operator.sub_list", "List"), `${OPERATOR_SETTINGS_PATH}.list`) },
    { key: "publication", label: t("react.workflows.operator.sub_publication", "Publication") },
  ];
  let panel: ReactNode;
  if (subTab === "result") panel = <ResultEditor draft={draft} issues={issues} setDraft={setDraft} view={view} />;
  else if (subTab === "list") panel = <ListEditor draft={draft} issues={issues} setDraft={setDraft} view={view} />;
  else if (subTab === "publication") panel = <PublicationEditor ctx={ctx} draft={draft} issues={issues} onJump={jump} setDraft={setDraft} view={view} />;
  else {
    panel = (
      <div className="space-y-3">
        <p className="fs-field-helper">{t("react.workflows.operator.form_help", "The fields come from the workflow contract in its order. Choose how each one is shown; add, remove or retype fields in Smart setup.")}</p>
        <IssueLines issues={issuesUnder(issues, `${OPERATOR_SETTINGS_PATH}.form.order`)} />
        <OperatorFieldList
          compatibility={view.compatibility ?? {}}
          configs={draft.form.fields}
          fields={fields}
          issues={issues}
          listUsage={(name) => ({
            column: draft.list.columns.some((column) => column.source === "input" && column.path.split(/[.[]/)[0] === name),
            filter: draft.list.filters.some((filter) => filter.source === "input" && filter.path === name),
          })}
          nodes={nodes}
          onConfig={(name, next) => setDraft({ ...draft, form: { ...draft.form, fields: { ...draft.form.fields, [name]: next } } })}
          onMove={(name, delta) => setDraft({ ...draft, form: { ...draft.form, order: moveInOrder(order, name, delta) } })}
          parents={[]}
          state={state}
        />
        <FormPreview draft={draft} view={view} />
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3" data-testid="workflow-operator-settings">
      <DsTabs active={subTab} ariaLabel={t("react.workflows.operator.tab", "For operators")} idPrefix={idPrefix} items={tabs} onSelect={(key) => setSubTab(key as OperatorSubTab)} />
      <div aria-labelledby={`${idPrefix}-tab-${subTab}`} id={`${idPrefix}-panel-${subTab}`} role="tabpanel">{panel}</div>
      <DrawerFooter>
        <DrawerFooterSpacer />
        <SaveStateButton
          dirty={Boolean(ctx.workflowFormDirty || ctx.workflowFormSaveFailed)}
          doneAnnouncement={t("react.workflows.workflow_settings_saved", "Workflow settings saved.")}
          doneLabel={t("react.workflows.save_done_saved", "Saved")}
          failed={Boolean(ctx.workflowFormSaveFailed)}
          label={t("react.workflows.save_workflow_settings", "Save workflow settings")}
          onClick={() => ignoreAsyncResult(ctx.handleSaveWorkflowSettings())}
          saving={ctx.savingWorkflowSurface === "workflow"}
          savingLabel={t("react.workflows.save_workflow_settings_saving", "Saving workflow settings…")}
        />
      </DrawerFooter>
    </div>
  );
}
