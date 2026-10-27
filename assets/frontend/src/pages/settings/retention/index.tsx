import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { DsAlert, DsCard } from "@/components/shared/design-system";
import {
  fetchRetentionSettingsRecord,
  saveRetentionSettingsRecord,
} from "@/pages/settings/shared/api";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

type Entity = Record<string, unknown>;

function objectOrEmpty(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Entity) : {};
}

function readPositiveInt(value: unknown, fallback: number): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallback;
  }
  return Math.floor(parsed);
}

export function SettingsRetentionPage() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [actionLogsDays, setActionLogsDays] = useState("30");
  const [jobLogsDays, setJobLogsDays] = useState("30");
  const [trashDays, setTrashDays] = useState("30");
  const [terminalJobCheckpointsDays, setTerminalJobCheckpointsDays] = useState("30");
  const [revisionsKeep, setRevisionsKeep] = useState("5");
  const [message, setMessage] = useState("");
  const [saveSucceeded, setSaveSucceeded] = useState(false);
  const [saving, setSaving] = useState(false);

  const retentionQuery = useQuery({
    queryKey: ["settings", "retention"],
    queryFn: fetchRetentionSettingsRecord,
    refetchInterval: 30_000,
  });

  const payload = objectOrEmpty(retentionQuery.data);
  const settings = objectOrEmpty(payload.settings);
  const defaults = objectOrEmpty(payload.defaults);
  const available = payload.available !== false;

  const defaultActionLogs = readPositiveInt(defaults.action_logs_days, 30);
  const defaultJobLogs = readPositiveInt(defaults.job_logs_days, 30);
  const defaultTrash = readPositiveInt(defaults.trash_days, 30);
  const defaultTerminalJobCheckpoints = readPositiveInt(
    defaults.terminal_job_checkpoints_days,
    30,
  );
  const defaultRevisionsKeep = readPositiveInt(defaults.revisions_keep, 5);

  useEffect(() => {
    setActionLogsDays(stringFromUnknownValue(readPositiveInt(settings.action_logs_days, defaultActionLogs)));
    setJobLogsDays(stringFromUnknownValue(readPositiveInt(settings.job_logs_days, defaultJobLogs)));
    setTrashDays(stringFromUnknownValue(readPositiveInt(settings.trash_days, defaultTrash)));
    setTerminalJobCheckpointsDays(
      stringFromUnknownValue(readPositiveInt(
          settings.terminal_job_checkpoints_days,
          defaultTerminalJobCheckpoints,
        )),
    );
    setRevisionsKeep(stringFromUnknownValue(readPositiveInt(settings.revisions_keep, defaultRevisionsKeep)));
  }, [
    defaultActionLogs,
    defaultJobLogs,
    defaultRevisionsKeep,
    defaultTerminalJobCheckpoints,
    defaultTrash,
    settings.action_logs_days,
    settings.job_logs_days,
    settings.revisions_keep,
    settings.terminal_job_checkpoints_days,
    settings.trash_days,
  ]);

  async function handleSubmit(event: React.SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage("");
    setSaveSucceeded(false);
    const actionLogs = readPositiveInt(actionLogsDays, 0);
    const jobLogs = readPositiveInt(jobLogsDays, 0);
    const trash = readPositiveInt(trashDays, 0);
    const terminalJobCheckpoints = readPositiveInt(terminalJobCheckpointsDays, 0);
    const revisions = readPositiveInt(revisionsKeep, 0);
    if (!actionLogs || !jobLogs || !trash || !terminalJobCheckpoints || !revisions) {
      setMessage(t("settings.retention.validation.positive_integer", "All retention values must be whole numbers greater than 0."));
      return;
    }
    setSaving(true);
    try {
      await saveRetentionSettingsRecord({
        settings: {
          action_logs_days: actionLogs,
          job_logs_days: jobLogs,
          trash_days: trash,
          terminal_job_checkpoints_days: terminalJobCheckpoints,
          revisions_keep: revisions,
        },
      });
      setMessage(t("settings.retention.saved", "Retention settings saved."));
      setSaveSucceeded(true);
      await queryClient.invalidateQueries({
        queryKey: ["settings", "retention"],
      });
    } catch (error) {
      setSaveSucceeded(false);
      setMessage(error instanceof Error ? error.message : t("settings.retention.save_failed", "Failed to save retention settings."));
    } finally {
      setSaving(false);
    }
  }

  const messageTone = saveSucceeded ? "success" : "danger";

  return (
    <>
      {!available ? (
        <DsAlert title={t("react.settings.runtime_settings_storage_is_unavailable", "Runtime settings storage is unavailable")} tone="warning">
          {t("react.settings.you_can_review_default_values_but_saving_may_fail_until_persistence_is_available", "You can review default values, but saving may fail until persistence is available.")} </DsAlert>
      ) : null}

      {retentionQuery.isError ? (
        <DsAlert title={t("react.settings.retention_policy_could_not_be_loaded", "Retention policy could not be loaded")} tone="danger">
          {retentionQuery.error instanceof Error ? retentionQuery.error.message : t("react.settings.failed_to_load_retention_policy", "Failed to load retention policy.")}
        </DsAlert>
      ) : null}

      <DsCard
        description={t("react.settings.configure_cleanup_windows_for_action_logs_job_logs_and_soft_deleted_items_and_how_many_revisions_each_item_kee", "Configure cleanup windows for action logs, job logs, and soft-deleted items, and how many revisions each item keeps. Environment defaults: action logs {value1}d, job logs {value2}d, trash {value3}d, terminal job checkpoints {value4}d, revisions kept per item {value5}.", { value1: String(defaultActionLogs), value2: String(defaultJobLogs), value3: String(defaultTrash), value4: String(defaultTerminalJobCheckpoints), value5: String(defaultRevisionsKeep) })}
        footer={
          <div className="fs-footer-actions">
            <button
              className="fs-button primary"
              disabled={saving || retentionQuery.isLoading}
              form="retention-settings-form"
              type="submit"
            >
              {saving ? t("project_edit.security.saving", "Saving...") : t("react.settings.save_retention_settings", "Save retention settings")}
            </button>
          </div>
        }
        title={t("react.settings.retention_policy", "Retention policy")}
      >
        <form
          className="fs-form-grid"
          id="retention-settings-form"
          noValidate
          onSubmit={(event) => ignoreAsyncResult(handleSubmit(event))}
        >
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="retention-action">
                {t("react.settings.action_logs_days", "Action logs (days)")} </label>
              <input
                className="fs-input"
                id="retention-action"
                min="1"
                onChange={(event) => setActionLogsDays(event.target.value)}
                step="1"
                type="number"
                value={actionLogsDays}
              />
            </div>
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="retention-jobs">
                {t("react.settings.job_logs_days", "Job logs (days)")} </label>
              <input
                className="fs-input"
                id="retention-jobs"
                min="1"
                onChange={(event) => setJobLogsDays(event.target.value)}
                step="1"
                type="number"
                value={jobLogsDays}
              />
            </div>
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="retention-trash">
                {t("react.settings.trash_days", "Trash (days)")} </label>
              <input
                className="fs-input"
                id="retention-trash"
                min="1"
                onChange={(event) => setTrashDays(event.target.value)}
                step="1"
                type="number"
                value={trashDays}
              />
            </div>
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="retention-terminal-checkpoints">
                {t("react.settings.terminal_job_checkpoints_days", "Terminal job checkpoints (days)")} </label>
              <input
                className="fs-input"
                id="retention-terminal-checkpoints"
                min="1"
                onChange={(event) => setTerminalJobCheckpointsDays(event.target.value)}
                step="1"
                type="number"
                value={terminalJobCheckpointsDays}
              />
            </div>
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="retention-revisions">
                {t("react.settings.revisions_kept_per_item", "Revisions kept per item")} </label>
              <input
                aria-describedby="retention-revisions-hint"
                className="fs-input"
                id="retention-revisions"
                max="100"
                min="1"
                onChange={(event) => setRevisionsKeep(event.target.value)}
                step="1"
                type="number"
                value={revisionsKeep}
              />
              <p className="fs-field-helper" id="retention-revisions-hint">
                {t("react.settings.older_revisions_of_each_agent_tool_skill_workflow_and_project_settings_are_deleted_for_good", "Older revisions of each agent, tool, skill, workflow and project settings are deleted for good.")} </p>
            </div>
        </form>
        {message ? <DsAlert title={message} tone={messageTone} /> : null}
      </DsCard>
    </>
  );
}
