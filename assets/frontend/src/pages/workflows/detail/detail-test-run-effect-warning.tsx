import { useI18n } from "@/lib/i18n/useI18n";
export function TestRunEffectWarning() {
  const { t } = useI18n();
  return (
    <div
      aria-label={t("react.workflows.test_run_effects", "Test Run effects")}
      className="rounded-lg border border-[var(--fs-status-warning-border)] bg-[var(--fs-status-warning-bg)] p-3 text-xs text-[var(--fs-text-attention)]"
      role="note"
    >
      {t("react.workflows.test_run_is_not_an_external_effect_sandbox_http_steps_can_send_real_requests_including_writes_configured_model", "Test Run is not an external-effect sandbox. HTTP steps can send real requests, including writes. Configured model calls may incur provider costs. Extension effects marked for suppression are suppressed only through the host's effect-suppression path. Review the steps, target URLs, credentials, and inputs before running.")}</div>
  );
}
