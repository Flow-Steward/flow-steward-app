import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import {
  SIGNED_WEBHOOK_PAYLOAD_RECIPE_LABELS,
  signedWebhookPayloadRecipeLabel,
  SIGNED_WEBHOOK_SECRET_GENERATED,
  SIGNED_WEBHOOK_MAX_TOLERANCE_SECONDS,
  SIGNED_WEBHOOK_SECRET_IMPORTED,
  signedWebhookAuthConfigFromDraft,
  signedWebhookPlaceholderHeaders,
  signedWebhookSummary,
  signedWebhookToleranceError,
  type SignedWebhookBuilderDraft,
  type SignedWebhookDigestFormat,
  type SignedWebhookPayloadRecipe,
  type SignedWebhookSecretDirection,
  type SignedWebhookTimestampMode,
  type SignedWebhookTimestampUnit,
  type SignedWebhookValueMode,
 localizedSIGNED_WEBHOOK_EXAMPLES} from "@/pages/workflows/detail/detail-webhook-signature-builder";

const FIELD_CLASS =
  "fs-input mt-1 w-full";
const LABEL_CLASS = "block text-xs fs-ink-secondary";
const HINT_CLASS = "mt-1 text-[11px] fs-ink-tertiary";

/**
 * Guided builder for the generic `webhook_signature` verifier.
 *
 * Every control writes into the same provider-agnostic config; the example
 * buttons only prefill these controls and never send a provider identity to the
 * backend.
 */
export function SignedWebhookAuthFields({
  authConfig,
  draft,
  importedSecret,
  onDraftChange,
  onImportedSecretChange,
  onResetToBuilderConfig,
  onSecretDirectionChange,
  secretDirection,
  showSecretDirection = true,
  supported,
}: Readonly<{
  authConfig: Record<string, unknown>;
  draft: SignedWebhookBuilderDraft;
  importedSecret: string;
  onDraftChange: (draft: SignedWebhookBuilderDraft) => void;
  onImportedSecretChange: (value: string) => void;
  onResetToBuilderConfig: () => void;
  onSecretDirectionChange: (direction: SignedWebhookSecretDirection) => void;
  secretDirection: SignedWebhookSecretDirection;
  /** A provider preset asks for its secret above, by name; Custom chooses here. */
  showSecretDirection?: boolean;
  supported: boolean;
}>) {
  const { t } = useI18n();
  const advancedJson = JSON.stringify(
    supported ? signedWebhookAuthConfigFromDraft(draft) : authConfig,
    null,
    2,
  );

  if (!supported) {
    return (
      <div className="space-y-2 rounded-lg border border-attention fs-surface-attention p-3">
        <p className="text-[11px] fs-ink-attention">
          {t("react.workflows.this_saved_signature_configuration_uses_a_shape_the_simple_editor_cannot_represent_so_editing_it_here_is_not_a", "This saved signature configuration uses a shape the simple editor cannot represent, so editing it here is not available. Reset it to start again from the guided controls.")}</p>
        <pre className="max-h-56 overflow-auto rounded fs-surface-inset p-2 text-[11px] fs-ink-primary">
          {advancedJson}
        </pre>
        <Button
          variant="secondary"
          onClick={onResetToBuilderConfig}
          type="button"
        >
          {t("react.workflows.reset_to_supported_builder_config", "Reset to supported builder config")}</Button>
      </div>
    );
  }

  const toleranceError = signedWebhookToleranceError(draft, t);

  const handleTimestampModeChange = (mode: SignedWebhookTimestampMode) => {
    let payloadRecipe: SignedWebhookPayloadRecipe = draft.payloadRecipe;
    if (mode === "none") {
      payloadRecipe = "raw_body";
    } else if (payloadRecipe === "raw_body") {
      payloadRecipe = mode === "header" ? "v0_timestamp_body" : "timestamp_dot_body";
    }
    onDraftChange({ ...draft, timestampMode: mode, payloadRecipe });
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs fs-ink-secondary">
          {t("react.workflows.start_from_example", "Start from example")}</span>
        {localizedSIGNED_WEBHOOK_EXAMPLES(t).map((example) => (
          <Button
            variant="secondary"
            size="sm"
            key={example.id}
            onClick={() => onDraftChange({ ...example.draft })}
            type="button"
          >
            {example.label}
          </Button>
        ))}
      </div>

      <div>
        <label className={LABEL_CLASS}>
          <span>{t("react.workflows.signature_header", "Signature header")}</span>
          <input
            className={FIELD_CLASS}
            onChange={(event) =>
              onDraftChange({ ...draft, signatureHeader: event.target.value })
            }
            placeholder="X-Hub-Signature-256"
            value={draft.signatureHeader}
          />
        </label>
        <p className={HINT_CLASS}>
          {t("react.workflows.the_exact_incoming_header_name_sent_by_the_source_service", "The exact incoming header name sent by the source service.")}</p>
      </div>

      <div>
        <label className={LABEL_CLASS}>
          <span>{t("react.workflows.signature_value", "Signature value")}</span>
          <select
            className={FIELD_CLASS}
            onChange={(event) =>
              onDraftChange({
                ...draft,
                valueMode: event.target.value as SignedWebhookValueMode,
              })
            }
            value={draft.valueMode}
          >
            <option value="raw">{t("react.workflows.full_header_value", "Full header value")}</option>
            <option value="prefixed">{t("react.workflows.header_value_after_prefix", "Header value after prefix")}</option>
            <option value="comma_kv">{t("react.workflows.comma_separated_key_value_field", "Comma-separated key/value field")}</option>
          </select>
        </label>
        {draft.valueMode === "comma_kv" ? (
          <p className={HINT_CLASS}>
            {t("react.workflows.use_this_when_one_header_contains_fields_such_as_t_and_v1", "Use this when one header contains fields such as `t=...` and `v1=...`.")}</p>
        ) : null}
      </div>

      {draft.valueMode === "prefixed" ? (
        <div>
          <label className={LABEL_CLASS}>
            <span>{t("react.workflows.prefix", "Prefix")}</span>
            <input
              className={FIELD_CLASS}
              onChange={(event) => onDraftChange({ ...draft, prefix: event.target.value })}
              placeholder="sha256="
              value={draft.prefix}
            />
          </label>
          <p className={HINT_CLASS}>
            {t("react.workflows.use_this_when_the_header_value_starts_with_a_label_such_as_sha256_or_v0", "Use this when the header value starts with a label such as `sha256=` or `v0=`.")}</p>
        </div>
      ) : null}

      {draft.valueMode === "comma_kv" ? (
        <>
          <label className={LABEL_CLASS}>
            <span>{t("react.workflows.signature_key", "Signature key")}</span>
            <input
              className={FIELD_CLASS}
              onChange={(event) =>
                onDraftChange({ ...draft, signatureKey: event.target.value })
              }
              placeholder="v1"
              value={draft.signatureKey}
            />
          </label>
          <label className="flex items-center gap-2 text-xs fs-ink-primary">
            <input
              checked={draft.allowMultiple}
              className="h-4 w-4 accent-[var(--fs-checkbox-bg-selected)]"
              onChange={(event) =>
                onDraftChange({ ...draft, allowMultiple: event.target.checked })
              }
              type="checkbox"
            />
            <span>{t("react.workflows.accept_any_matching_repeated_signature_key", "Accept any matching repeated signature key")}</span>
          </label>
        </>
      ) : null}

      <div>
        <label className={LABEL_CLASS}>
          <span>{t("react.workflows.digest_format", "Digest format")}</span>
          <select
            className={FIELD_CLASS}
            onChange={(event) =>
              onDraftChange({
                ...draft,
                digestFormat: event.target.value as SignedWebhookDigestFormat,
              })
            }
            value={draft.digestFormat}
          >
            <option value="hex">hex</option>
            <option value="base64">base64</option>
          </select>
        </label>
        <p className={HINT_CLASS}>
          {t("react.workflows.choose_the_format_of_the_signature_value_in_the_incoming_header_not_the_format_of_the_request_body", "Choose the format of the signature value in the incoming header, not the format of the request body.")}</p>
      </div>

      <div>
        <label className={LABEL_CLASS}>
          <span>{t("react.workflows.signed_payload", "Signed payload")}</span>
          <select
            className={FIELD_CLASS}
            onChange={(event) =>
              onDraftChange({
                ...draft,
                payloadRecipe: event.target.value as SignedWebhookPayloadRecipe,
              })
            }
            value={draft.payloadRecipe}
          >
            {(
              Object.keys(
                SIGNED_WEBHOOK_PAYLOAD_RECIPE_LABELS,
              ) as SignedWebhookPayloadRecipe[]
            ).map((recipe) => (
              <option key={recipe} value={recipe}>
                {signedWebhookPayloadRecipeLabel(recipe, t)}
              </option>
            ))}
          </select>
        </label>
        <p className={HINT_CLASS}>
          {t("react.workflows.this_is_the_exact_byte_string_flow_steward_signs_before_comparing_the_digest_raw_body_means_the_unmodified_req", "This is the exact byte string Flow Steward signs before comparing the digest. Raw body means the unmodified request body bytes.")}</p>
      </div>

      <div>
        <label className={LABEL_CLASS}>
          <span>{t("react.workflows.timestamp", "Timestamp")}</span>
          <select
            className={FIELD_CLASS}
            onChange={(event) =>
              handleTimestampModeChange(event.target.value as SignedWebhookTimestampMode)
            }
            value={draft.timestampMode}
          >
            <option value="none">{t("react.workflows.none", "None")}</option>
            <option value="header">{t("react.workflows.separate_timestamp_header", "Separate timestamp header")}</option>
            <option value="signature_param">{t("react.workflows.timestamp_field_inside_signature_header", "Timestamp field inside signature header")}</option>
          </select>
        </label>
        <p className={HINT_CLASS}>
          {t("react.workflows.use_the_same_timestamp_value_that_the_source_service_includes_in_the_signed_payload", "Use the same timestamp value that the source service includes in the signed payload.")}</p>
      </div>

      {draft.timestampMode === "header" ? (
        <label className={LABEL_CLASS}>
          <span>{t("react.workflows.timestamp_header", "Timestamp header")}</span>
          <input
            className={FIELD_CLASS}
            onChange={(event) =>
              onDraftChange({ ...draft, timestampHeader: event.target.value })
            }
            placeholder="X-Slack-Request-Timestamp"
            value={draft.timestampHeader}
          />
        </label>
      ) : null}

      {draft.timestampMode === "signature_param" ? (
        <label className={LABEL_CLASS}>
          <span>{t("react.workflows.timestamp_key", "Timestamp key")}</span>
          <input
            className={FIELD_CLASS}
            onChange={(event) =>
              onDraftChange({ ...draft, timestampKey: event.target.value })
            }
            placeholder="t"
            value={draft.timestampKey}
          />
        </label>
      ) : null}

      {draft.timestampMode === "none" ? null : (
        <div className="grid gap-2 sm:grid-cols-2">
          <label className={LABEL_CLASS}>
            <span>{t("react.workflows.timestamp_unit", "Timestamp unit")}</span>
            <select
              className={FIELD_CLASS}
              onChange={(event) =>
                onDraftChange({
                  ...draft,
                  timestampUnit: event.target.value as SignedWebhookTimestampUnit,
                })
              }
              value={draft.timestampUnit}
            >
              <option value="seconds">{t("react.workflows.seconds", "seconds")}</option>
              <option value="milliseconds">{t("react.workflows.milliseconds", "milliseconds")}</option>
            </select>
          </label>
          <div>
            <label className={LABEL_CLASS}>
              <span>{t("react.workflows.tolerance", "Tolerance")}</span>
              <input
                className={FIELD_CLASS}
                max={SIGNED_WEBHOOK_MAX_TOLERANCE_SECONDS}
                min={1}
                onChange={(event) =>
                  onDraftChange({ ...draft, toleranceSeconds: event.target.value })
                }
                step={1}
                type="number"
                value={draft.toleranceSeconds}
              />
            </label>
            {toleranceError ? (
              <p className="mt-1 text-[11px] normal-case tracking-normal fs-ink-attention">
                {toleranceError}
              </p>
            ) : null}
          </div>
        </div>
      )}

      <div className="space-y-1 rounded border border-default fs-surface-inset p-2">
        <p className="text-xs fs-ink-secondary">
          {t("react.workflows.signature_rule", "Signature rule")}</p>
        <p className="text-[11px] fs-ink-primary">{signedWebhookSummary(draft, t)}</p>
        <p className="text-[11px] fs-ink-tertiary">
          {t("react.workflows.request_headers_the_source_must_send", "Request headers the source must send")}</p>
        <pre className="overflow-x-auto rounded fs-surface-inset p-2 text-[11px] fs-ink-primary">
          {signedWebhookPlaceholderHeaders(draft)
            .map(([name, value]) => `${name}: ${value}`)
            .join("\n")}
        </pre>
      </div>

      {showSecretDirection ? <fieldset className="space-y-2 rounded border border-default fs-surface-inset p-2">
        <legend className="px-1 text-xs fs-ink-secondary">
          {t("react.workflows.signing_secret", "Signing secret")}</legend>
        <label className="flex items-start gap-2 text-xs fs-ink-primary">
          <input
            checked={secretDirection === SIGNED_WEBHOOK_SECRET_GENERATED}
            className="mt-0.5 h-4 w-4 accent-[var(--fs-checkbox-bg-selected)]"
            name="signed_webhook_secret_direction"
            onChange={() => onSecretDirectionChange(SIGNED_WEBHOOK_SECRET_GENERATED)}
            type="radio"
          />
          <span>
            {t("react.workflows.generate_a_new_signing_secret", "Generate a new signing secret")}
            <span className="block text-[11px] fs-ink-secondary">
              {t("react.workflows.flow_steward_shows_it_once_after_saving_copy_it_into_the_source_service", "Flow Steward shows it once after saving; copy it into the source service.")}</span>
          </span>
        </label>
        <label className="flex items-start gap-2 text-xs fs-ink-primary">
          <input
            checked={secretDirection === SIGNED_WEBHOOK_SECRET_IMPORTED}
            className="mt-0.5 h-4 w-4 accent-[var(--fs-checkbox-bg-selected)]"
            name="signed_webhook_secret_direction"
            onChange={() => onSecretDirectionChange(SIGNED_WEBHOOK_SECRET_IMPORTED)}
            type="radio"
          />
          <span>
            {t("react.workflows.use_an_existing_provider_signing_secret", "Use an existing provider signing secret")}
            <span className="block text-[11px] fs-ink-secondary">
              {t("react.workflows.create_the_endpoint_in_the_source_service_first_then_paste_the_signing_secret_it_shows_you", "Create the endpoint in the source service first, then paste the signing secret it shows you.")}</span>
          </span>
        </label>
        {secretDirection === SIGNED_WEBHOOK_SECRET_IMPORTED ? (
          <label className={LABEL_CLASS}>
            <span>{t("react.workflows.provider_signing_secret", "Provider signing secret")}</span>
            <input
              autoComplete="off"
              className={FIELD_CLASS}
              onChange={(event) => onImportedSecretChange(event.target.value)}
              placeholder={t("react.workflows.paste_the_signing_secret_from_the_source_service", "Paste the signing secret from the source service")}
              type="password"
              value={importedSecret}
            />
          </label>
        ) : null}
      </fieldset> : null}

      <details className="rounded border border-default fs-surface-inset p-2">
        <summary className="cursor-pointer text-xs fs-ink-secondary">
          {t("react.workflows.advanced_json_read_only", "Advanced JSON (read-only)")}</summary>
        <pre className="mt-2 max-h-56 overflow-auto rounded fs-surface-inset p-2 text-[11px] fs-ink-primary">
          {advancedJson}
        </pre>
      </details>
    </div>
  );
}
