import { useState } from "react";
import type { ExtensionExternalEffect } from "@/pages/extensions/api";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { useI18n } from "@/lib/i18n/useI18n";
import { ChevronRight } from "lucide-react";

import { DsBadge, RequiredLabel, type DsBadgeTone } from "@/components/shared/design-system";
import {
  manifestActionOutcome,
  objectOrEmpty,
} from "@/pages/extensions/extension-manifest-utils";
import {
  redactPayload,
  summaryEntriesFromResult,
} from "@/pages/extensions/extension-action-result-summary";
import { translatedExtensionActionErrorGuidance } from "@/pages/extensions/extension-action-error-guidance";

export type ExtensionActionResult = {
  label: string;
  ok: boolean;
  payload: unknown;
  scopeKey?: string;
};

export type ExtensionEffectRecovery = {
  effect: ExtensionExternalEffect;
  canManage: boolean;
  originalRequestAvailable: boolean;
  busy: boolean;
  error: string;
  onDecision: (action: "approve" | "reject", comment: string) => Promise<void>;
  onRetry: () => Promise<void>;
  onLater: () => void;
};

function EffectRecoveryControls({ recovery }: Readonly<{ recovery: ExtensionEffectRecovery }>) {
  const { t } = useI18n();
  const [comment, setComment] = useState("");
  const [commentError, setCommentError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const { effect } = recovery;
  const busy = recovery.busy || submitting;
  const canDecide = recovery.canManage && effect.decision_allowed;
  const inProgress = effect.retry_unavailable_reason === "retry_in_progress";
  const canRetry = recovery.canManage && effect.retry_supported
    && effect.resolution_ref?.effect_id === effect.effect_id
    && recovery.originalRequestAvailable && !inProgress;

  async function decide(action: "approve" | "reject") {
    if (!canDecide || busy) return;
    const trimmed = comment.trim();
    if (!trimmed) {
      setCommentError(t("react.extensions.effect_recovery_comment_required", "Add a comment before recording this decision."));
      return;
    }
    setCommentError("");
    setSubmitting(true);
    try { await recovery.onDecision(action, trimmed); }
    finally { setSubmitting(false); }
  }

  return <div className="space-y-3">
    {!recovery.canManage ? <p>{t("react.extensions.effect_recovery_read_only", "You can view this effect. An operator must record the decision.")}</p> : <>
      {!recovery.originalRequestAvailable ? <p>{t("react.extensions.effect_recovery_original_unavailable", "The original request for this effect is unavailable. Retry is unavailable.")}</p> : null}
      {inProgress ? <p>{t("react.extensions.effect_recovery_in_progress", "The authorized retry is already in progress.")}</p> : null}
      {canDecide ? <label className="fs-field min-w-0"><RequiredLabel>{t("jobs.detail.review.comment", "Comment")}</RequiredLabel><textarea aria-required="true" required className="fs-textarea" maxLength={4000} disabled={busy} value={comment} onChange={(event) => setComment(event.target.value)} /></label> : null}
      {commentError || recovery.error ? <p className="fs-extension-result-error" role="alert">{commentError || recovery.error}</p> : null}
      <div className="flex flex-wrap gap-2">
        {canDecide ? <>
          <button className="fs-button accent" type="button" disabled={busy} onClick={() => ignoreAsyncResult(decide("approve"))}>{t("react.extensions.effect_recovery_completed", "Operation completed — continue")}</button>
          <button className="fs-button" type="button" disabled={busy} onClick={() => ignoreAsyncResult(decide("reject"))}>{t("react.extensions.effect_recovery_not_completed", "Operation not completed — retry")}</button>
        </> : null}
        {!effect.decision_allowed || !recovery.originalRequestAvailable ? <button className="fs-button" type="button" disabled={busy || !canRetry} onClick={() => ignoreAsyncResult(recovery.onRetry())}>{t("react.extensions.effect_recovery_retry", "Retry original action")}</button> : null}
      </div>
    </>}
    <button className="fs-button" type="button" disabled={busy} onClick={recovery.onLater}>{t("react.extensions.effect_recovery_later", "Decide later")}</button>
  </div>;
}

export function ExtensionActionResultPanel({
  guidance = "",
  result,
  recovery,
}: Readonly<{ guidance?: string; result: ExtensionActionResult; recovery?: ExtensionEffectRecovery }>) {
  const { t } = useI18n();
  const outcome = manifestActionOutcome(result.payload, t);
  const summary = summaryEntriesFromResult(objectOrEmpty(outcome.result), t);
  const outcomeMessage = translatedExtensionActionErrorGuidance(t, outcome.message)
    || (
      !outcome.hasProvidedMessage
        ? translatedExtensionActionErrorGuidance(
          t,
          outcome.result.error_code,
          outcome.response.error_code,
        )
        : ""
    )
    || outcome.message;
  const failureMessage = guidance || (recovery || result.ok ? "" : outcomeMessage);
  let displayStatus = result.ok ? "succeeded" : "failed";
  let badgeTone: DsBadgeTone = result.ok ? "green" : "danger";
  let badgeLabel = result.ok ? t("react.extensions.succeeded", "succeeded") : t("react.extensions.failed", "failed");
  if (recovery) {
    const inProgress = ["claimed", "running"].includes(recovery.effect.status)
      || recovery.effect.retry_unavailable_reason === "retry_in_progress";
    displayStatus = inProgress ? "running" : recovery.effect.resolution_ref ? "pending" : "unknown";
    badgeTone = inProgress ? "neutral" : "orange";
    badgeLabel = inProgress ? t("processes.status.running", "Running")
      : recovery.effect.resolution_ref ? t("react.shared.badge_pending", "pending")
        : t("react.shared.badge_unknown", "unknown");
  }
  return (
    <section
      aria-label={t("react.extensions.value0_result", "{value0} result", { value0: result.label })}
      className="fs-extension-result"
      data-outcome={displayStatus}
    >
      <div className="fs-extension-result-head">
        <strong>{result.label} {" "}{t("react.extensions.result", "result")}</strong>
        <DsBadge tone={badgeTone}>{badgeLabel}</DsBadge>
      </div>
      {!result.ok && failureMessage ? (
        <p className="fs-extension-result-error" role="alert">
          {failureMessage}
        </p>
      ) : null}
      {result.ok && summary.length ? (
        <dl className="fs-kv is-bare">
          {summary.map(([label, value]) => (
            <div className="fs-kv-row" key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {recovery ? <EffectRecoveryControls key={`${recovery.effect.effect_id}:${recovery.effect.observed_version}`} recovery={recovery} /> : null}
      <details className="fs-settings-section">
        <summary>
          <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
          {t("registry.skill_detail.technical_details", "Technical details")}</summary>
        <div className="fs-settings-section-body">
          <pre className="fs-extension-detail-manifest">
            {JSON.stringify(redactPayload(result.payload), null, 2)}
          </pre>
        </div>
      </details>
    </section>
  );
}
