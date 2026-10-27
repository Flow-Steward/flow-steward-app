import { executionStepStatusLabel } from "./job-detail-execution";
import { jobProjectionText } from "./job-detail-view-model";
import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { X } from "lucide-react";
import { DsCard } from "@/components/shared/design-system";
import { fetchJobStepOutputValue } from "./api";
import { CopyValueButton, firstReadableText, humanizeDiagnosticLabel, listFromUnknown, normalizeTimestamp, objectOrEmpty, type Entity } from "./job-detail-core";
import { ReadableDataFields, ReadableFileList } from "./readable-data-fields";
import { collectEvidenceItems } from "./job-review-evidence";

function providerProcessingTimeLabel(value: string, unit: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (!value) return "";
  return unit ? `${value} ${unit}` : t("react.jobs.value1_unit_not_declared", "{value1} (unit not declared)", { value1: String(value) });
}

function formatStepDuration(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "";
  }
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds) || milliseconds < 0) {
    return firstReadableText(value);
  }
  if (milliseconds < 1000) {
    return `${Math.round(milliseconds)}ms`;
  }
  if (milliseconds < 60_000) {
    return `${Math.round(milliseconds / 100) / 10}s`;
  }
  const minutes = Math.floor(milliseconds / 60_000);
  const seconds = Math.round((milliseconds % 60_000) / 1000);
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

const TECHNICAL_STEP_FIELD_KEYS = new Set([
  "connection_ref",
  "continuation_token",
  "cursor",
  "next_cursor",
  "page_token",
]);

function userFacingStepFields(value: unknown): Entity {
  return Object.fromEntries(
    Object.entries(objectOrEmpty(value)).filter(([key]) => {
      const normalized = key.trim().toLowerCase();
      return !TECHNICAL_STEP_FIELD_KEYS.has(normalized) && !normalized.endsWith("_cursor");
    }),
  );
}

export function InspectorField({
  copy = false,
  label,
  testId,
  value,
}: Readonly<{
  copy?: boolean;
  label: string;
  testId?: string;
  value: string;
}>) {
  if (!value) {
    return null;
  }
  return (
    <div className="min-w-0 fs-surface-radius border border-subtle fs-surface-inset px-3 py-2 text-sm fs-ink-primary">
      <p className="text-xs uppercase tracking-[0.14em] fs-ink-tertiary">{label}</p>
      <div className="mt-1 flex min-w-0 items-start gap-2">
        <p className="min-w-0 flex-1 break-words" data-testid={testId}>
          {value}
        </p>
        {copy ? <CopyValueButton label={label} value={value} /> : null}
      </div>
    </div>
  );
}

export function StepInspectorPanel({
  jobId = "",
  onClose,
  selectedStep,
  testId,
}: Readonly<{
  /** Enables "Show full text" for outputs the preview cut short. */
  jobId?: string;
  onClose?: () => void;
  selectedStep: Entity | null;
  testId: string;
}>) {
  const { t } = useI18n();
  if (!selectedStep) {
    return (
      <DsCard
        className="min-w-0"
        description={t("react.jobs.select_a_step_from_the_execution_waterfall", "Select a step from the execution waterfall.")}
        testId={testId}
        title={t("react.jobs.step_inspector", "Step inspector")}
      >
        <p className="fs-empty-copy">{t("react.jobs.no_execution_step_is_selected", "No execution step is selected.")}</p>
      </DsCard>
    );
  }

  const stepMessageId = firstReadableText(selectedStep.message_id);
  const title = firstReadableText(
    selectedStep.title,
    selectedStep.display_name,
    selectedStep.step_name,
    selectedStep.name,
    t("registry.workflow_detail.step.drawer_title", "Step"),
  );
  const status = executionStepStatusLabel(selectedStep, t);
  const groupId = firstReadableText(selectedStep.group_id);
  const group = groupId ? humanizeDiagnosticLabel(groupId) : "";
  // A step run by its own kind (control flow, transform, an extension action)
  // has no agent: it shows its step type, and a tool and action when it used
  // one. An agent step shows the agent and its skill.
  const stepType = jobProjectionText(selectedStep, "step_type_label", t);
  const agent = stepType
    ? ""
    : firstReadableText(
        selectedStep.agent_label,
        selectedStep.agent,
        selectedStep.agent_id,
        selectedStep.agent_display_name,
      );
  const skill = agent ? firstReadableText(selectedStep.skill_label, selectedStep.skill) : "";
  const tool = firstReadableText(selectedStep.tool_label, selectedStep.tool, selectedStep.tool_name);
  const operation = firstReadableText(selectedStep.operation_label, selectedStep.operation_id);
  const model = firstReadableText(selectedStep.model, selectedStep.model_name, selectedStep.model_used);
  const attempt = firstReadableText(selectedStep.attempt, selectedStep.attempt_no, selectedStep.retry_no);
  const started = firstReadableText(selectedStep.started_at, selectedStep.started);
  const finished = firstReadableText(selectedStep.finished_at, selectedStep.completed_at, selectedStep.finished);
  const duration = firstReadableText(selectedStep.duration_display, selectedStep.duration_ms, selectedStep.elapsed_ms);
  const providerProcessingTime = firstReadableText(selectedStep.provider_processing_time);
  const providerProcessingTimeUnit = firstReadableText(selectedStep.provider_processing_time_unit);
  const summary = firstReadableText(jobProjectionText(selectedStep, "summary", t), selectedStep.description);
  const result = firstReadableText(selectedStep.result, selectedStep.output_summary);
  const message = firstReadableText(selectedStep.message, selectedStep.content_preview);
  const failure = firstReadableText(
    selectedStep.error,
    selectedStep.error_text,
    selectedStep.failure_reason,
    selectedStep.last_error,
  );
  const nextAction = firstReadableText(selectedStep.next, selectedStep.next_action);
  const evidenceItems = collectEvidenceItems(selectedStep, t);
  const inputs = userFacingStepFields(
    selectedStep.inputs || selectedStep.input || selectedStep.input_payload,
  );
  const outputs = userFacingStepFields(
    selectedStep.outputs || selectedStep.output || selectedStep.output_payload,
  );
  const inputArtifacts = listFromUnknown(selectedStep.input_artifacts);
  const outputArtifacts = listFromUnknown(selectedStep.output_artifacts);

  return (
    <article className="fs-card min-w-0" data-testid={testId}>
      <header className="fs-card-header">
        <div className="flex min-w-0 items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="fs-card-title break-words">{title}</h2>
            <p className="fs-card-description break-words">
              {firstReadableText(summary, status)}
            </p>
          </div>
          {onClose ? (
            <button
              aria-label={t("react.jobs.close_step_inspector", "Close step inspector")}
              className="fs-icon-button"
              onClick={onClose}
              type="button"
            >
              <X aria-hidden="true" />
            </button>
          ) : null}
        </div>
      </header>
      <div className="fs-card-body space-y-3">
        <div className="grid min-w-0 gap-2 sm:grid-cols-2">
          <InspectorField label={t("account_settings.card.status", "Status")} value={status} />
          <InspectorField label={t("events.columns.group", "Group")} value={group} />
          <InspectorField label={t("react.jobs.step_type", "Step type")} value={stepType} />
          <InspectorField label={t("events.columns.agent", "Agent")} value={agent} />
          <InspectorField label={t("registry.skills.title_single", "Skill")} value={skill} />
          <InspectorField label={t("registry.tools.title_single", "Tool")} value={tool} />
          <InspectorField label={t("registry.agent_detail.changelog.action", "Action")} value={operation} />
          <InspectorField label={t("project_edit.ai_models.model", "Model")} value={model} />
          <InspectorField label={t("react.jobs.attempt", "Attempt")} value={attempt} />
          <InspectorField label={t("project_detail.jobs.grid.started", "Started")} value={started ? normalizeTimestamp(started) : ""} />
          <InspectorField label={t("react.jobs.finished_2", "Finished")} value={finished ? normalizeTimestamp(finished) : ""} />
          <InspectorField label={t("jobs.columns.duration", "Duration")} value={formatStepDuration(duration) || t("react.jobs.not_recorded", "Not recorded")} />
          <InspectorField
            label={t("react.jobs.provider_processing_time", "Provider processing time")}
            value={providerProcessingTimeLabel(providerProcessingTime, providerProcessingTimeUnit, t)}
          />
        </div>

        <section className="space-y-2" aria-labelledby={`${testId}-inputs-heading`}>
          <h3 className="text-sm font-semibold fs-ink-primary" id={`${testId}-inputs-heading`}>
            {t("registry.agent_detail.io.inputs", "Inputs")} </h3>
          <ReadableDataFields emptyText={t("react.jobs.no_step_inputs", "No step inputs were recorded.")} fieldLabelKeys={objectOrEmpty(selectedStep.input_field_label_keys)} value={inputs} />
        </section>

        {inputArtifacts.length ? (
          <section className="space-y-2" aria-labelledby={`${testId}-input-files-heading`}>
            <h3 className="text-sm font-semibold fs-ink-primary" id={`${testId}-input-files-heading`}>
              {t("react.jobs.input_files", "Input files")} </h3>
            <ReadableFileList files={inputArtifacts} />
          </section>
        ) : null}

        <section className="space-y-2" aria-labelledby={`${testId}-outputs-heading`}>
          <h3 className="text-sm font-semibold fs-ink-primary" id={`${testId}-outputs-heading`}>
            {t("registry.agent_detail.io.outputs", "Outputs")} </h3>
          <ReadableDataFields
            emptyText={t("react.jobs.no_step_outputs", "No step outputs were recorded.")}
            fieldLabelKeys={objectOrEmpty(selectedStep.output_field_label_keys)}
            loadFullValue={stepMessageId && jobId
              ? async (field) => (await fetchJobStepOutputValue(jobId, stepMessageId, field)).value
              : undefined}
            value={outputs}
          />
        </section>

        {outputArtifacts.length ? (
          <section className="space-y-2" aria-labelledby={`${testId}-output-files-heading`}>
            <h3 className="text-sm font-semibold fs-ink-primary" id={`${testId}-output-files-heading`}>
              {t("react.jobs.generated_files", "Generated files")} </h3>
            <ReadableFileList files={outputArtifacts} />
          </section>
        ) : null}

        {[
          [t("jobs.columns.summary", "Summary"), summary],
          [t("jobs.detail.inspector.result", "Result"), result],
          [t("registry.workflow_detail.field.message", "Message"), message],
          [t("jobs.detail.next_action.title", "Next action"), nextAction],
        ]
          .filter(([, value]) => Boolean(value))
          .map(([label, value]) => (
            <div
              className="min-w-0 fs-surface-radius border border-subtle fs-surface-inset px-3 py-2 text-sm fs-ink-primary"
              key={label}
            >
              <p className="text-xs uppercase tracking-[0.14em] fs-ink-tertiary">{label}</p>
              <p className="mt-1 break-words">{value}</p>
            </div>
          ))}

        {failure ? (
          <section className="space-y-2" aria-labelledby={`${testId}-errors-heading`}>
            <h3 className="text-sm font-semibold fs-ink-primary" id={`${testId}-errors-heading`}>
              {t("extension_page.validation.errors", "Errors")} </h3>
            <div className="fs-surface-radius border border-danger fs-card-danger px-3 py-2 text-sm fs-ink-primary">
              <p className="break-words">{failure}</p>
            </div>
          </section>
        ) : null}

        {evidenceItems.length ? (
          <div className="min-w-0 fs-surface-radius border border-subtle fs-surface-inset px-3 py-2 text-sm fs-ink-primary">
            <p className="text-xs uppercase tracking-[0.14em] fs-ink-tertiary">{t("react.jobs.artifacts", "Artifacts")}</p>
            <ul className="mt-2 space-y-2">
              {evidenceItems.map((item) => (
                <li className="min-w-0" key={item.key}>
                  <p className="font-medium fs-ink-primary">{item.label}</p>
                  {item.href ? (
                    <a className="fs-link mt-1 block break-all" href={item.href}>
                      {item.summary}
                    </a>
                  ) : (
                    <p className="mt-1 break-words fs-ink-secondary">{item.summary}</p>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </article>
  );
}
