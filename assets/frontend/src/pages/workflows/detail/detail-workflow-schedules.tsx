import { useI18n } from "@/lib/i18n/useI18n";
import { useId, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Clock, Plus } from "lucide-react";

import { ConfirmActionDialog } from "@/components/shared/confirm-action-dialog";
import { DsAlert, DsSelect } from "@/components/shared/design-system";
import { DsLoader } from "@/components/shared/ds-loader";
import { DsSwitch } from "@/components/shared/ds-switch";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";
import { browserTimezone, useUserTimezone } from "@/lib/time/user-timezone";
import { fetchWorkflowContract, type WorkflowContractField } from "@/pages/projects/detail/api";
import { fetchProjectWorkflowBindings } from "@/pages/projects/workflows/api";
import { ProjectWorkflowInputFields } from "@/pages/projects/detail/project-workflow-input-fields";
import { buildFriendlyCronExpr, cronDescription, emptyFieldValue, getWeekdayOptions, objectOrEmpty, parseWorkflowInputs, timezoneOptions, workflowIdList } from "@/pages/projects/detail/project-detail-helpers";
import {
  createProjectScheduledJob, deleteProjectScheduledJob, fetchProjectScheduledJobs, toggleProjectScheduledJob,
  updateProjectScheduledJob, type ScheduledJob, type ScheduledJobCreateInput,
} from "@/pages/projects/scheduled-jobs/api";
import { textOrEmpty } from "@/pages/workflows/detail/detail-helpers";
import { DrawerFooter, DrawerFooterSpacer } from "./detail-drawer-footer";
import {
  cronHasFiveFields, formatScheduleRun, friendlyScheduleFromCron, nextCronRuns, type ScheduleRepeat,
} from "./detail-workflow-schedule-cron";

import "@/pages/dashboard/dashboard-segmented.css";
import "./detail-workflow-schedules.css";

type Scope = { projectId: string; workflowId: string };
type Translate = ReturnType<typeof useI18n>["t"];
type SchedulesProps = Scope & {
  /** The linked project's name, for "Schedules belong to this workflow in …". */
  projectName?: string;
  /** Opens Smart setup at Trigger channels; without it the alert alone explains what to do. */
  onTurnOnScheduleChannel?: () => void;
};

const NEXT_RUN_COUNT = 3;

export function WorkflowSchedules({ projectId, workflowId, projectName, onTurnOnScheduleChannel }: Readonly<SchedulesProps>) {
  const { t } = useI18n();
  if (!projectId || !workflowId) return <DsAlert title={t("project_detail.scheduled.fields.schedule", "Schedule")}>{t("react.workflows.schedule_requires_a_linked_project_context_open_this_workflow_from_a_project_to_manage_its_schedules", "Schedule requires a linked project context. Open this workflow from a project to manage its schedules.")}</DsAlert>;
  return <ProjectWorkflowSchedules key={`${projectId}:${workflowId}`} projectId={projectId} workflowId={workflowId}
    projectName={projectName} onTurnOnScheduleChannel={onTurnOnScheduleChannel} />;
}

/** The schedule in words: "Every day at 09:00 (Europe/Vilnius)", or a custom cron schedule. */
function scheduleSummary(expression: string, timezone: string, t: Translate): string {
  const friendly = friendlyScheduleFromCron(expression);
  return friendly
    ? cronDescription(friendly.mode, friendly.time, friendly.weekday, friendly.monthDay, timezone, t)
    : cronDescription("custom", "", "", "", timezone, t);
}

/** "Next Mon, 5 Oct · 09:00", or Paused: the second line of a schedule row. */
function scheduleRowState(job: ScheduledJob, expression: string, timezone: string, t: Translate, locale: string): string {
  if (!job.enabled) return t("react.workflows.paused", "Paused");
  const clientNext = nextCronRuns(expression, timezone, 1)?.[0];
  const serverNext = textOrEmpty(job.next_run_at_user_local) || textOrEmpty(job.next_run_at_utc);
  const next = clientNext ? formatScheduleRun(clientNext, locale) : serverNext;
  return next ? t("react.workflows.schedule_next_run", "Next {time}", { time: next }) : t("react.workflows.schedule_on", "On");
}

function ProjectWorkflowSchedules({ projectId, workflowId, projectName, onTurnOnScheduleChannel }: Readonly<SchedulesProps>) {
  const { t, locale } = useI18n();
  const [editing, setEditing] = useState<ScheduledJob | null | undefined>(undefined);
  const [deleting, setDeleting] = useState<{ job: ScheduledJob; title: string } | null>(null);
  const [rowError, setRowError] = useState("");
  const queryClient = useQueryClient();
  const scheduledJobsKey = ["projects", "scheduled-jobs", projectId];
  const bindings = useQuery({ queryKey: ["projects", "workflow-bindings", projectId], queryFn: () => fetchProjectWorkflowBindings(projectId), retry: false });
  const contract = useQuery({ queryKey: ["workflows", "contract", workflowId], queryFn: () => fetchWorkflowContract(workflowId), retry: false });
  const linked = workflowIdList(bindings.data).includes(workflowId);
  const schedules = useQuery({ queryKey: scheduledJobsKey, queryFn: () => fetchProjectScheduledJobs(projectId), enabled: linked, retry: false });
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => toggleProjectScheduledJob(projectId, id, enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: scheduledJobsKey }),
    onError: (error) => setRowError(error.message),
  });
  const remove = useMutation({
    mutationFn: (id: string) => deleteProjectScheduledJob(projectId, id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: scheduledJobsKey });
      setDeleting(null);
    },
  });
  const loadingVisible = useLoaderDelay(bindings.isPending || contract.isPending || (linked && schedules.isPending));
  const place = projectName || projectId;
  if (bindings.isError || contract.isError || schedules.isError) return <DsAlert title={t("react.workflows.schedules_could_not_be_loaded", "Schedules could not be loaded")} tone="danger">{String(bindings.error?.message || contract.error?.message || schedules.error?.message)}</DsAlert>;
  if (bindings.isPending || contract.isPending) return loadingVisible ? <DsLoader variant="wide-block" label={t("react.workflows.loading_schedule_context", "Loading schedule context…")} /> : null;
  if (!linked) return <DsAlert title={t("project_detail.scheduled.fields.schedule", "Schedule")}>{t("react.workflows.this_workflow_is_not_linked_to_the_selected_project", "This workflow is not linked to the selected project.")}</DsAlert>;
  const fields = contract.data?.interface?.expected_inputs || [];
  const channelOn = Boolean(contract.data?.interface?.enabled && contract.data?.interface?.trigger_channels?.includes("schedule"));
  const available = Boolean(contract.data?.enabled) && channelOn;
  const jobs = (schedules.data?.jobs || []).filter((job) => job.workflow_id === workflowId);

  if (editing !== undefined) {
    return <ScheduleEditor
      key={editing?.id || "new"} projectId={projectId} workflowId={workflowId} job={editing} fields={fields} place={place}
      onCancel={() => setEditing(undefined)} onSaved={async () => {
        await queryClient.invalidateQueries({ queryKey: scheduledJobsKey });
        setEditing(undefined);
      }} />;
  }

  let list = <p className="fs-empty-copy">{t("react.workflows.no_schedules_in_project", "No schedules for this workflow in {project} yet.", { project: place })}</p>;
  if (schedules.isPending) list = loadingVisible ? <DsLoader variant="wide-block" label={t("react.workflows.loading_schedules", "Loading schedules…")} /> : <></>;
  else if (jobs.length) list = <ul className="workflow-schedule-list">{jobs.map((job) => {
    const expression = textOrEmpty(job.cron_expr);
    const timezone = textOrEmpty(job.schedule_timezone) || browserTimezone();
    const title = scheduleSummary(expression, timezone, t);
    return <li className="workflow-schedule-row" data-enabled={job.enabled ? "true" : "false"} key={String(job.id)}>
      <div className="workflow-schedule-row-main">
        <span className="workflow-schedule-row-title">{title}</span>
        <span className="workflow-schedule-row-sub"><code>{expression}</code> · {scheduleRowState(job, expression, timezone, t, locale)}</span>
      </div>
      <DsSwitch
        checked={Boolean(job.enabled)}
        disabled={!available || toggle.isPending}
        label={t("react.workflows.schedule_enabled_for", "Enabled: {title}", { title })}
        onCheckedChange={(enabled) => { setRowError(""); toggle.mutate({ id: String(job.id), enabled }); }}
      />
      <Button aria-label={t("react.workflows.edit_schedule_named", "Edit schedule: {title}", { title })} disabled={!available} onClick={() => setEditing(job)} size="sm" type="button" variant="ghost">
        {t("extensions.repeater.edit", "Edit")}</Button>
      <button aria-label={t("react.workflows.delete_schedule_named", "Delete schedule: {title}", { title })} className="fs-button sm ghost-danger" onClick={() => { remove.reset(); setDeleting({ job, title }); }} type="button">
        {t("react.workflows.delete", "Delete")}</button>
    </li>;
  })}</ul>;

  let footerAction = null;
  if (!channelOn) {
    footerAction = onTurnOnScheduleChannel ? <Button onClick={onTurnOnScheduleChannel} type="button" variant="primary">
      {t("react.workflows.turn_on_schedule", "Turn on Schedule")}</Button> : null;
  } else {
    footerAction = <Button disabled={!available} onClick={() => setEditing(null)} type="button" variant="primary">
      <Plus aria-hidden="true" />{t("react.workflows.new_schedule", "New schedule")}</Button>;
  }

  return <section aria-label={t("react.workflows.workflow_schedules", "Workflow schedules")} className="workflow-schedules">
    <p className="fs-field-helper">{t("react.workflows.schedules_belong_to_project", "Schedules belong to this workflow in {project}. Saving workflow settings does not create or enable a schedule.", { project: place })}</p>
    {channelOn ? null : <DsAlert title={t("react.workflows.schedule_turned_off_title", "Schedule is turned off for this workflow")} tone="warning">
      <p>{t("react.workflows.schedule_turned_off_body", "Turn on Schedule in Trigger channels and save the workflow settings. Until then no schedule starts a run.")}</p>
    </DsAlert>}
    {channelOn && !available ? <DsAlert title={t("project_detail.scheduled.fields.schedule", "Schedule")} tone="warning">
      <p>{t("react.workflows.the_saved_workflow_must_allow_schedule_before_a_schedule_can_be_created_or_updated", "The saved workflow must allow Schedule before a schedule can be created or updated.")}</p>
    </DsAlert> : null}
    {rowError ? <DsAlert title={t("react.workflows.schedule_could_not_be_saved", "Schedule could not be saved")} tone="danger"><p>{rowError}</p></DsAlert> : null}
    {list}
    {footerAction ? <DrawerFooter testId="workflow-schedules-footer">
      <DrawerFooterSpacer />
      {footerAction}
    </DrawerFooter> : null}
    <ConfirmActionDialog
      confirmLabel={t("react.workflows.delete_schedule", "Delete schedule")}
      consequence={t("react.workflows.delete_schedule_consequence", "No run starts from this schedule again.")}
      error={remove.error?.message || ""}
      eyebrow={t("react.workflows.schedules", "Schedules")}
      onCancel={() => { if (!remove.isPending) setDeleting(null); }}
      onConfirm={() => { if (deleting?.job.id) remove.mutate(String(deleting.job.id)); }}
      open={Boolean(deleting)}
      pending={remove.isPending}
      pendingLabel={t("react.workflows.deleting_schedule", "Deleting schedule…")}
      title={t("react.workflows.delete_schedule_question", "Delete {title}?", { title: deleting?.title || "" })}
    />
  </section>;
}

type ScheduleEditorProps = Scope & {
  job: ScheduledJob | null;
  fields: WorkflowContractField[];
  place: string;
  onCancel: () => void;
  onSaved: () => Promise<void>;
};

function ScheduleEditor({ projectId, workflowId, job, fields, place, onCancel, onSaved }: Readonly<ScheduleEditorProps>) {
  const { t, locale } = useI18n();
  const formId = useId();
  const userTimezone = useUserTimezone();
  // A saved expression reopens in the Repeat shape that wrote it; anything else is Custom.
  const initial = useMemo(() => {
    const saved = textOrEmpty(job?.cron_expr);
    const fallback = { mode: (saved ? "custom" : "daily") as ScheduleRepeat, time: "09:00", weekday: "1", monthDay: "1" };
    return (saved && friendlyScheduleFromCron(saved)) || fallback;
  }, [job]);
  const [mode, setMode] = useState<ScheduleRepeat>(initial.mode);
  const [time, setTime] = useState(initial.time);
  const [weekday, setWeekday] = useState(initial.weekday);
  const [monthDay, setMonthDay] = useState(initial.monthDay);
  const [cron, setCron] = useState(textOrEmpty(job?.cron_expr) || "0 9 * * *");
  const [timezone, setTimezone] = useState(() => job ? textOrEmpty(job.schedule_timezone) || browserTimezone() : userTimezone);
  const [enabled, setEnabled] = useState(job?.enabled ?? false);
  const [notice, setNotice] = useState("");
  const [inputValues, setInputValues] = useState<Record<string, string>>(() => Object.fromEntries(Object.entries(objectOrEmpty(job?.inputs)).map(([name, value]) => [name, typeof value === "string" ? value : JSON.stringify(value)])));
  const [inputErrors, setInputErrors] = useState<Record<string, string>>({});
  const callerFields = fields.filter((field) => !field.default_configured);
  const requiredFields = callerFields.filter((field) => field.required);
  const optionalFields = callerFields.filter((field) => !field.required);
  const fixedNames = fields.filter((field) => field.default_configured).map((field) => field.name);
  const draftError = callerFields.map((field) => inputErrors[field.name]).find(Boolean);
  const values = Object.fromEntries(callerFields.map((field) => [field.name, inputValues[field.name] ?? emptyFieldValue(field)]));
  const expression = buildFriendlyCronExpr(mode, time, weekday, monthDay, cron);
  const cronInvalid = mode === "custom" && !cronHasFiveFields(cron);
  const monthDayNumber = Number(monthDay);
  const monthDayInvalid = mode === "monthly" && !(Number.isInteger(monthDayNumber) && monthDayNumber >= 1 && monthDayNumber <= 31);
  const timingInvalid = cronInvalid || monthDayInvalid;
  const save = useMutation({ mutationFn: (input: ScheduledJobCreateInput) => job?.id ? updateProjectScheduledJob(projectId, String(job.id), input) : createProjectScheduledJob(projectId, input), onSuccess: onSaved, onError: (error) => setNotice(error.message) });
  const savingLoader = useLoaderDelay(save.isPending);
  // Next runs follow every change, read on the schedule's own timezone.
  const nextRuns = timingInvalid ? null : nextCronRuns(expression, timezone, NEXT_RUN_COUNT);
  let nextText = t("react.workflows.schedule_next_after_save", "Flow Steward shows the next runs for this expression after you save.");
  if (timingInvalid) nextText = t("react.workflows.schedule_fix_timing", "Fix the timing to see the next runs.");
  else if (nextRuns?.length) nextText = t("react.workflows.schedule_next_runs", "Next: {runs}", { runs: nextRuns.map((run) => formatScheduleRun(run, locale)).join("; ") });
  const optionalSet = optionalFields.filter((field) => {
    const value = inputValues[field.name];
    return value !== undefined && value !== "" && value !== "false";
  }).length;
  const repeatOptions: Array<{ value: ScheduleRepeat; label: string }> = [
    { value: "daily", label: t("project_detail.schedule.daily", "Daily") },
    { value: "weekly", label: t("project_detail.schedule.weekly", "Weekly") },
    { value: "monthly", label: t("project_detail.schedule.monthly", "Monthly") },
    { value: "custom", label: t("react.workflows.schedule_repeat_custom", "Custom") },
  ];
  const inputsHelp = (callerFields.length
    ? t("react.workflows.schedule_inputs_values_help", "Values each scheduled run passes to the workflow.")
    : t("react.workflows.schedule_inputs_none_help", "This workflow needs no values from a schedule."))
    + (fixedNames.length ? ` ${t("react.workflows.schedule_inputs_fixed_help", "{names} use their fixed defaults.", { names: fixedNames.join(", ") })}` : "");

  function chooseRepeat(next: ScheduleRepeat) {
    if (next === mode) return;
    // Custom starts from the timing already chosen, so nothing is lost.
    if (next === "custom") setCron(expression);
    setMode(next);
  }

  function submit(event: React.SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    if (draftError) {
      setNotice(draftError);
      return;
    }
    if (timingInvalid) {
      setNotice(t("react.workflows.schedule_fix_timing", "Fix the timing to see the next runs."));
      return;
    }
    const parsed = parseWorkflowInputs(callerFields, values, t);
    const inputError = Object.values(inputErrors).find(Boolean);
    if (!expression.trim() || !timezone.trim() || parsed.error || inputError) {
      setNotice(inputError || parsed.error || t("react.workflows.display_schedule_required_fields", "Cron expression and timezone are required."));
      return;
    }
    setNotice("");
    const declaredNames = new Set(fields.map((field) => field.name));
    const extraInputs = Object.fromEntries(Object.entries(objectOrEmpty(job?.inputs)).filter(([name]) => !declaredNames.has(name)));
    save.mutate({ workflow_id: workflowId, cron_expr: expression, schedule_timezone: timezone, enabled,
      inputs: { ...extraInputs, ...parsed.inputs } });
  }

  const inputFieldProps = {
    effectiveWorkflowInputValues: values,
    setWorkflowInputValues: setInputValues,
    onValidationChange: (name: string, error: string) => setInputErrors((current) => ({ ...current, [name]: error })),
  };
  const repeatLabelId = `${formId}-repeat`;
  const timezoneLabelId = `${formId}-timezone`;
  const dayLabelId = `${formId}-day`;
  const cronHelpId = `${formId}-cron-help`;
  const cronErrorId = `${formId}-cron-error`;
  const enabledHelpId = `${formId}-enabled-help`;

  return <form aria-label={t("react.workflows.schedule_editor", "Schedule editor")} className="workflow-schedules" id={formId} onSubmit={submit}>
    <div>
      <p className="fs-drawer-label">{job ? t("react.workflows.edit_schedule", "Edit schedule") : t("react.workflows.new_schedule", "New schedule")}</p>
      <p className="fs-field-helper">{t("react.workflows.schedule_runs_in_project", "Runs this workflow in {project}.", { project: place })}</p>
    </div>
    <div className="fs-field">
      <span className="fs-field-label" id={repeatLabelId}>{t("react.projects.repeat", "Repeat")}</span>
      <div aria-labelledby={repeatLabelId} className="fs-seg workflow-schedule-repeat" role="group">
        {repeatOptions.map((option) => <button aria-pressed={option.value === mode} className="fs-seg__item" key={option.value} onClick={() => chooseRepeat(option.value)} type="button">{option.label}</button>)}
      </div>
    </div>
    <div className="grid gap-3 sm:grid-cols-2">
      {mode === "custom" ? <label className="fs-field">
        <span className="fs-field-label">{t("react.projects.cron_expression", "Cron expression")}</span>
        <input aria-describedby={cronInvalid ? cronErrorId : cronHelpId} aria-invalid={cronInvalid || undefined} autoComplete="off" className="fs-input fs-code-value" onChange={(event) => setCron(event.target.value)} placeholder="0 9 * * 1-5" value={cron} />
        {cronInvalid ? <span className="fs-field-helper error" id={cronErrorId}>{t("react.workflows.schedule_cron_five_fields", "Enter five fields: minute, hour, day of month, month, day of week.")}</span> : null}
      </label> : <label className="fs-field">
        <span className="fs-field-label">{t("events.fields.time", "Time")}</span>
        <input className="fs-input" onChange={(event) => setTime(event.target.value)} type="time" value={time} />
      </label>}
      <div className="fs-field">
        <span className="fs-field-label" id={timezoneLabelId}>{t("account.timezone.title", "Timezone")}</span>
        <DsSelect
          ariaLabel={t("account.timezone.title", "Timezone")}
          labelledBy={timezoneLabelId}
          onChange={setTimezone}
          options={timezoneOptions(timezone).map((option) => ({ value: option, label: option }))}
          searchable
          value={timezone}
        />
      </div>
    </div>
    {mode === "custom" ? <p className="fs-field-helper" id={cronHelpId}>{t("react.workflows.schedule_cron_help", "Five fields: minute, hour, day of month, month, day of week. 0 9 * * 1-5 runs at 09:00 on weekdays.")}</p> : null}
    {mode === "weekly" ? <div className="fs-field">
      <span className="fs-field-label" id={dayLabelId}>{t("react.projects.day", "Day")}</span>
      <DsSelect ariaLabel={t("react.projects.day", "Day")} labelledBy={dayLabelId} onChange={setWeekday}
        options={getWeekdayOptions(t).map((day) => ({ value: day.value, label: day.label }))} value={weekday} />
    </div> : null}
    {mode === "monthly" ? <label className="fs-field">
      <span className="fs-field-label">{t("react.projects.day_of_month", "Day of month")}</span>
      <input aria-invalid={monthDayInvalid || undefined} className="fs-input workflow-schedule-day-of-month" max={31} min={1} onChange={(event) => setMonthDay(event.target.value)} type="number" value={monthDay} />
      {monthDayInvalid ? <span className="fs-field-helper error">{t("react.workflows.schedule_day_of_month_range", "Choose a day from 1 to 31.")}</span> : null}
      {!monthDayInvalid && monthDayNumber > 28 ? <span className="fs-field-helper">{t("react.workflows.schedule_short_months_skipped", "Months without day {day} are skipped.", { day: monthDayNumber })}</span> : null}
    </label> : null}
    <div className="workflow-schedule-when">
      <Clock aria-hidden="true" />
      <div aria-live="polite" className="workflow-schedule-when-text">
        <span className="workflow-schedule-when-summary">{cronDescription(mode, time, weekday, monthDay, timezone, t)}</span>
        <span className="workflow-schedule-when-next" data-testid="schedule-next-runs">{nextText}</span>
      </div>
    </div>
    <div>
      <span className="workflow-schedule-switch">
        <DsSwitch checked={enabled} describedBy={enabledHelpId} label={t("monitoring.flags.enabled", "Enabled")} onCheckedChange={setEnabled} />
        <span aria-hidden="true">{t("monitoring.flags.enabled", "Enabled")}</span>
      </span>
      <p className="fs-field-helper" id={enabledHelpId}>{enabled
        ? t("react.workflows.schedule_enabled_help_on", "Runs start at the next time above once you save.")
        : t("react.workflows.schedule_enabled_help_off", "Off: the schedule is saved but does not run until you turn it on.")}</p>
    </div>
    {requiredFields.length || !optionalFields.length ? <section className="space-y-2">
      <p className="fs-drawer-label">{t("react.workflows.schedule_inputs", "Inputs")}</p>
      <p className="fs-field-helper">{inputsHelp}</p>
      <ProjectWorkflowInputFields workflowInputFields={requiredFields} {...inputFieldProps} />
    </section> : null}
    {optionalFields.length ? <details className="fs-settings-section" open={!requiredFields.length || undefined}>
      <summary>
        <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
        {t("react.workflows.schedule_optional_inputs", "Optional inputs")}
        <span className="fs-settings-section-note">{optionalSet
          ? t("react.workflows.schedule_optional_set", "{count} of {total} set", { count: optionalSet, total: optionalFields.length })
          : t("react.workflows.schedule_optional_none_set", "None set")}</span>
      </summary>
      <div className="fs-settings-section-body">
        {requiredFields.length ? null : <p className="fs-field-helper">{inputsHelp}</p>}
        <ProjectWorkflowInputFields workflowInputFields={optionalFields} {...inputFieldProps} />
      </div>
    </details> : null}
    {notice ? <DsAlert title={t("react.workflows.schedule_could_not_be_saved", "Schedule could not be saved")} tone="danger">{notice}</DsAlert> : null}
    <DrawerFooter testId="workflow-schedule-editor-footer">
      <Button disabled={save.isPending} onClick={onCancel} type="button" variant="secondary">{t("extensions.repeater.cancel", "Cancel")}</Button>
      <DrawerFooterSpacer />
      <Button aria-busy={save.isPending || undefined} disabled={save.isPending || Boolean(draftError)} form={formId} type="submit" variant="primary">
        {savingLoader ? <DsLoader variant="orbit-inline" /> : null}
        {job ? t("react.workflows.save_schedule", "Save schedule") : t("react.workflows.create_schedule", "Create schedule")}
      </Button>
    </DrawerFooter>
  </form>;
}
