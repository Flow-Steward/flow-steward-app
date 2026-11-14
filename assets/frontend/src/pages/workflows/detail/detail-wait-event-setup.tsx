import { RequiredLabel } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";
import { numericDraftFromInput, numericInputValue } from "./detail-step-numeric-fields";

type WaitEventSetupProps = Readonly<{
  config: Record<string, unknown>;
  errorId: string;
  errors: Partial<Record<string, string>>;
  inputClass: string;
  onChange: (patch: Record<string, unknown>) => void;
}>;

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** The fields of a Wait that ends on an extension's event (ADR 0071). */
export function WaitEventSetup({ config, errorId, errors, inputClass, onChange }: WaitEventSetupProps) {
  const { t } = useI18n();
  const event = record(config.event);
  const mapping = record(config.output_mapping);
  const eventTypes = Array.isArray(event.event_types) ? event.event_types.map((item) => text(item)).join(", ") : "";
  const setEvent = (field: string, value: unknown) => onChange({ event: { ...event, [field]: value } });
  return (
    <div className="space-y-3" data-testid="wait-event-setup">
      <label className="block text-xs text-[var(--fs-text-secondary)]">
        <RequiredLabel>{t("react.workflows.wait_event_source", "Extension that sends the event")}</RequiredLabel>
        <input
          aria-label={t("react.workflows.wait_event_source", "Extension that sends the event")}
          className={inputClass}
          onChange={(change) => setEvent("source", change.target.value.trim())}
          placeholder="acme.crawler"
          value={text(event.source)}
        />
      </label>
      <label className="block text-xs text-[var(--fs-text-secondary)]">
        <RequiredLabel>{t("react.workflows.wait_event_types", "Event types (comma separated)")}</RequiredLabel>
        <input
          aria-label={t("react.workflows.wait_event_types", "Event types (comma separated)")}
          className={inputClass}
          onChange={(change) =>
            setEvent(
              "event_types",
              change.target.value.split(",").map((item) => item.trim()).filter(Boolean),
            )
          }
          placeholder="run_completed, run_failed"
          defaultValue={eventTypes}
        />
      </label>
      <label className="block text-xs text-[var(--fs-text-secondary)]">
        <RequiredLabel>{t("react.workflows.wait_event_correlation_key", "Correlation key")}</RequiredLabel>
        <input
          aria-label={t("react.workflows.wait_event_correlation_key", "Correlation key")}
          className={inputClass}
          onChange={(change) => setEvent("correlation_key", change.target.value)}
          placeholder="state.run.run_id"
          value={text(event.correlation_key)}
        />
      </label>
      <label className="block text-xs text-[var(--fs-text-secondary)]">
        <RequiredLabel>{t("react.workflows.wait_event_timeout_ms", "Timeout, milliseconds")}</RequiredLabel>
        <input
          aria-describedby={errors.timeout_ms ? `${errorId}-timeout` : undefined}
          aria-invalid={errors.timeout_ms ? true : undefined}
          aria-label={t("react.workflows.wait_event_timeout_ms", "Timeout, milliseconds")}
          className={inputClass}
          data-step-numeric-field="timeout_ms"
          max={31_536_000_000}
          min={1}
          onChange={(change) => onChange({ timeout_ms: numericDraftFromInput(change.target.value) })}
          required
          type="number"
          value={numericInputValue(config.timeout_ms)}
        />
        {errors.timeout_ms ? <span className="fs-field-helper error block" id={`${errorId}-timeout`} role="alert">{errors.timeout_ms}</span> : null}
      </label>
      <label className="block text-xs text-[var(--fs-text-secondary)]">
        <span>{t("react.workflows.wait_event_payload_target", "Save the event payload to")}</span>
        <input
          aria-label={t("react.workflows.wait_event_payload_target", "Save the event payload to")}
          className={inputClass}
          onChange={(change) => {
            const target = change.target.value;
            const next = { ...mapping, payload: target };
            if (!target) delete next.payload;
            onChange({ output_mapping: Object.keys(next).length ? next : undefined });
          }}
          placeholder="state.event_payload"
          value={text(mapping.payload)}
        />
      </label>
      <p className="text-[11px] text-[var(--fs-text-tertiary)]">
        {t("react.workflows.wait_event_hint", "The run waits until the extension reports one of these events for this key. At the timeout the step fails; connect its error edge to handle it.")}
      </p>
    </div>
  );
}
