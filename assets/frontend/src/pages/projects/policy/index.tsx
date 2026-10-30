import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useMemo, useRef, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { DsSelect } from "@/components/shared/design-system";
import {
  fetchProjectPolicy,
  PROJECT_POLICY_AUTONOMY_MODES,
  saveProjectPolicy,
} from "@/pages/projects/policy/api";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

type Entity = Record<string, unknown>;

type PolicyForm = {
  autonomy_confidence_threshold: string;
  autonomy_mode: string;
  concurrency_cap: string;
  retry_backoff_initial_seconds: string;
  retry_backoff_max_seconds: string;
  retry_max_attempts: string;
  retry_strategy: string;
  tool_policy_mode: string;
};

const DEFAULT_POLICY_FORM: PolicyForm = {
  autonomy_confidence_threshold: "0.8",
  autonomy_mode: "supervised",
  concurrency_cap: "1",
  retry_backoff_initial_seconds: "2",
  retry_backoff_max_seconds: "120",
  retry_max_attempts: "2",
  retry_strategy: "bounded",
  tool_policy_mode: "registry_only",
};

type ProjectPolicyEditorProps = {
  projectId: string;
};

function objectOrEmpty(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Entity) : {};
}

function toNumber(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function ProjectPolicyEditor({ projectId }: Readonly<ProjectPolicyEditorProps>) {
  const { t } = useI18n();
  // Translate labels, while sourcing the saved values from the API's canonical enum.
  const autonomyModeOptions = PROJECT_POLICY_AUTONOMY_MODES.map((mode) => ({
    value: mode,
    label: t(`react.projects.autonomy_mode_${mode}`, mode),
  }));
  const queryClient = useQueryClient();
  const [form, setForm] = useState<PolicyForm>(DEFAULT_POLICY_FORM);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);

  const policyQuery = useQuery({
    queryKey: ["projects", projectId, "policy"],
    queryFn: () => fetchProjectPolicy(projectId),
    enabled: Boolean(projectId),
  });

  const payload = objectOrEmpty(policyQuery.data);
  const policy = objectOrEmpty(payload.policy);
  const retryPolicyError = useMemo(() => {
    const retries = Number(form.retry_max_attempts);
    const initial = Number(form.retry_backoff_initial_seconds);
    const maximum = Number(form.retry_backoff_max_seconds);
    if (
      !Number.isFinite(retries) ||
      !Number.isFinite(initial) ||
      !Number.isFinite(maximum) ||
      retries < 0 ||
      initial < 0 ||
      maximum < 0
    ) {
      return t("react.projects.retry_nonnegative", "Retry values must be nonnegative.");
    }
    if (!Number.isInteger(retries)) {
      return t("react.projects.retry_count_integer", "Maximum retries after the first attempt must be a whole number.");
    }
    if (initial > maximum) {
      return t("react.projects.retry_backoff_limit", "Initial retry backoff cannot exceed the maximum retry backoff.");
    }
    return "";
  }, [
    form.retry_backoff_initial_seconds,
    form.retry_backoff_max_seconds,
    form.retry_max_attempts,
    t,
  ]);

  /* The policy refetches every 20s. Seeding the form again on each poll wiped
     whatever the user was part-way through typing, so a touched form keeps what
     it has until a save commits it. */
  const touchedRef = useRef(false);

  useEffect(() => {
    if (touchedRef.current) {
      return;
    }
    setForm({
      autonomy_confidence_threshold: stringFromUnknownValue(policy.autonomy_confidence_threshold ?? "0.8"),
      autonomy_mode: PROJECT_POLICY_AUTONOMY_MODES.includes(
        stringFromUnknownValue(policy.autonomy_mode).trim() as (typeof PROJECT_POLICY_AUTONOMY_MODES)[number],
      )
        ? stringFromUnknownValue(policy.autonomy_mode).trim()
        : "supervised",
      concurrency_cap: stringFromUnknownValue(policy.concurrency_cap ?? "1"),
      retry_backoff_initial_seconds: stringFromUnknownValue(policy.retry_backoff_initial_seconds ?? "2"),
      retry_backoff_max_seconds: stringFromUnknownValue(policy.retry_backoff_max_seconds ?? "120"),
      retry_max_attempts: stringFromUnknownValue(policy.retry_max_attempts ?? "2"),
      retry_strategy: firstStringFromUnknownValue([policy.retry_strategy, "bounded"]),
      tool_policy_mode: firstStringFromUnknownValue([policy.tool_policy_mode, "registry_only"]),
    });
  }, [
    policy.autonomy_confidence_threshold,
    policy.autonomy_mode,
    policy.concurrency_cap,
    policy.retry_backoff_initial_seconds,
    policy.retry_backoff_max_seconds,
    policy.retry_max_attempts,
    policy.retry_strategy,
    policy.tool_policy_mode,
  ]);

  function updateField<K extends keyof PolicyForm>(key: K, value: PolicyForm[K]) {
    touchedRef.current = true;
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function handleSave() {
    setMessage("");
    if (retryPolicyError) {
      setMessage(retryPolicyError);
      return;
    }
    setSaving(true);
    try {
      await saveProjectPolicy(projectId, {
        policy: {
          autonomy_confidence_threshold: toNumber(form.autonomy_confidence_threshold, 0.8),
          autonomy_mode: form.autonomy_mode.trim() || "supervised",
          concurrency_cap: toNumber(form.concurrency_cap, 1),
          retry_backoff_initial_seconds: toNumber(form.retry_backoff_initial_seconds, 2),
          retry_backoff_max_seconds: toNumber(form.retry_backoff_max_seconds, 120),
          retry_max_attempts: toNumber(form.retry_max_attempts, 2),
          retry_strategy: form.retry_strategy.trim() || "bounded",
          tool_policy_mode: form.tool_policy_mode.trim() || "registry_only",
        },
      });
      setMessage(t("react.projects.project_policy_updated", "Project policy updated."));
      touchedRef.current = false;
      await queryClient.invalidateQueries({ queryKey: ["projects", projectId, "policy"] });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : t("react.projects.failed_to_update_project_policy", "Failed to update project policy."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="space-y-6">
      <Card className="fs-card">
        <CardHeader>
          <h2 className="fs-card-title">{t("react.projects.execution_policy", "Execution policy")}</h2>
        </CardHeader>
        <CardContent className="grid gap-3 lg:grid-cols-3">
          <div className="fs-field">
            <span className="fs-field-label">
              {t("react.projects.autonomy_mode", "Autonomy mode")} </span>
            <DsSelect
              ariaLabel={t("react.projects.autonomy_mode", "Autonomy mode")}
              onChange={(value) => updateField("autonomy_mode", value)}
              options={autonomyModeOptions}
              value={form.autonomy_mode}
            />
          </div>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="policy-autonomy-threshold">
              {t("react.projects.autonomy_confidence_threshold", "Autonomy confidence threshold")} </label>
            <input
              className="fs-input"
              id="policy-autonomy-threshold"
              onChange={(event) => updateField("autonomy_confidence_threshold", event.target.value)}
              step="0.01"
              type="number"
              value={form.autonomy_confidence_threshold}
            />
          </div>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="policy-concurrency-cap">
              {t("react.projects.concurrency_cap", "Concurrency cap")} </label>
            <input
              className="fs-input"
              id="policy-concurrency-cap"
              min="1"
              onChange={(event) => updateField("concurrency_cap", event.target.value)}
              step="1"
              type="number"
              value={form.concurrency_cap}
            />
          </div>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="policy-retry-strategy">
              {t("react.projects.retry_strategy", "Retry strategy")} </label>
            <input
              className="fs-input"
              id="policy-retry-strategy"
              onChange={(event) => updateField("retry_strategy", event.target.value)}
              value={form.retry_strategy}
            />
          </div>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="policy-retry-max">
              {t("react.projects.maximum_retries_after_the_first_attempt", "Maximum retries after the first attempt")} </label>
            <input
              aria-describedby="policy-retry-max-help"
              aria-invalid={Boolean(retryPolicyError)}
              className="fs-input"
              id="policy-retry-max"
              min="0"
              onChange={(event) => updateField("retry_max_attempts", event.target.value)}
              step="1"
              type="number"
              value={form.retry_max_attempts}
            />
            <p className="text-xs fs-ink-tertiary" id="policy-retry-max-help">
              {t("react.projects.a_value_of_2_allows_up_to_three_total_invocations", "A value of 2 allows up to three total invocations.")} </p>
          </div>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="policy-backoff-initial">
              {t("react.projects.retry_backoff_initial_seconds", "Retry backoff initial seconds")} </label>
            <input
              className="fs-input"
              id="policy-backoff-initial"
              min="0"
              onChange={(event) => updateField("retry_backoff_initial_seconds", event.target.value)}
              step="1"
              type="number"
              value={form.retry_backoff_initial_seconds}
            />
          </div>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="policy-backoff-max">
              {t("react.projects.retry_backoff_max_seconds", "Retry backoff max seconds")} </label>
            <input
              className="fs-input"
              id="policy-backoff-max"
              min="0"
              onChange={(event) => updateField("retry_backoff_max_seconds", event.target.value)}
              step="1"
              type="number"
              value={form.retry_backoff_max_seconds}
            />
          </div>
          <div className="fs-field">
            <label className="fs-field-label" htmlFor="policy-tool-mode">
              {t("react.projects.tool_policy_mode", "Tool policy mode")} </label>
            <input
              className="fs-input"
              id="policy-tool-mode"
              onChange={(event) => updateField("tool_policy_mode", event.target.value)}
              value={form.tool_policy_mode}
            />
          </div>
        </CardContent>
        {retryPolicyError ? (
          <p className="px-[var(--fs-card-padding)] pb-[var(--fs-card-padding)] text-sm fs-ink-attention">
            {retryPolicyError}
          </p>
        ) : null}
      </Card>

      <button
        className="fs-button primary"
        disabled={saving || Boolean(retryPolicyError)}
        onClick={() => ignoreAsyncResult(handleSave())}
        type="button"
      >
        {saving ? t("project_edit.security.saving", "Saving...") : t("settings.retention.save_policy", "Save policy")}
      </button>

      {policyQuery.isError ? (
        <Card className="border-selected fs-surface-attention">
          <CardContent className="text-sm fs-ink-attention">
            {policyQuery.error instanceof Error ? policyQuery.error.message : t("react.projects.failed_to_load_project_policy", "Failed to load project policy.")}
          </CardContent>
        </Card>
      ) : null}

      {message ? (
        <Card className="fs-card">
          <CardContent className="text-sm fs-ink-secondary">{message}</CardContent>
        </Card>
      ) : null}
    </section>
  );
}
