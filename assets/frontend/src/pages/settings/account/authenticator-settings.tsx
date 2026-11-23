import { useState, type SubmitEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { AuthenticatorQr } from "@/components/shared/authenticator-qr";
import { DsAlert, DsBadge, DsCard, RequiredLabel, SensitiveInput } from "@/components/shared/design-system";
import { DsLoader } from "@/components/shared/ds-loader";
import { Button } from "@/components/ui/button";
import { fetchJson } from "@/lib/http/client";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";

const AUTHENTICATOR_API = "/api/auth/multi-factor";

type FactorStatus = { enabled: boolean; required: boolean };
type PendingEnrollment = { secret: string; uri: string };
type VerificationResult = { recovery_codes: string[] };
type ProofAction = "enroll" | "disable";

export function AuthenticatorSettings({ userId }: Readonly<{ userId: string }>) {
  const { t } = useI18n();
  const cache = useQueryClient();
  const statusQueryKey = ["settings", "account", "authenticator", userId];
  const statusQuery = useQuery({
    queryKey: statusQueryKey,
    queryFn: ({ signal }) => fetchJson<FactorStatus>(`${AUTHENTICATOR_API}/status`, { signal }),
  });
  const status = statusQuery.data;
  const [action, setAction] = useState<ProofAction | null>(null);
  const [enrollment, setEnrollment] = useState<PendingEnrollment | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [saving, setSaving] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const enrollmentUri = enrollment?.uri.startsWith("otpauth://") ? enrollment.uri : undefined;

  function clearStep() {
    setAction(null);
    setEnrollment(null);
    setRecoveryCodes([]);
    setPassword("");
    setCode("");
    setErrorMessage("");
  }

  async function submitProof(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!action || !status || saving) return;

    setErrorMessage("");
    setSaving(true);
    try {
      const body = JSON.stringify({
        current_password: password,
        current_code: status.enabled ? code.trim() : undefined,
      });
      if (action === "disable") {
        await fetchJson<FactorStatus>(`${AUTHENTICATOR_API}/disable`, { method: "POST", body });
        cache.setQueryData<FactorStatus>(statusQueryKey, { ...status, enabled: false });
        clearStep();
      } else {
        const result = await fetchJson<PendingEnrollment>(`${AUTHENTICATOR_API}/enroll`, { method: "POST", body });
        clearStep();
        setEnrollment(result);
      }
    } catch (error) {
      setCode("");
      setErrorMessage(error instanceof Error
        ? error.message
        : t("react.settings.authenticator_request_failed", "The authenticator could not be updated."));
    } finally {
      setSaving(false);
    }
  }

  async function finishEnrollment(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!status || saving) return;

    setErrorMessage("");
    setSaving(true);
    try {
      const result = await fetchJson<VerificationResult>(`${AUTHENTICATOR_API}/verify`, {
        method: "POST",
        body: JSON.stringify({ code: code.trim() }),
      });
      clearStep();
      cache.setQueryData<FactorStatus>(statusQueryKey, { ...status, enabled: true });
      setRecoveryCodes(result.recovery_codes);
    } catch (error) {
      setCode("");
      setErrorMessage(error instanceof Error
        ? error.message
        : t("react.settings.authenticator_request_failed", "The authenticator could not be updated."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DsCard
      description={t("react.settings.authenticator_description", "Add a second sign-in step using an authenticator app.")}
      title={t("react.settings.authenticator_title", "Authenticator app")}
    >
      {statusQuery.isPending ? <DsLoader label={t("react.settings.authenticator_loading", "Loading authenticator settings…")} variant="orbit-inline" /> : null}
      {statusQuery.isError ? (
        <>
          <DsAlert tone="danger" title={t("react.settings.authenticator_status_error", "Authenticator settings could not be loaded")} />
          <Button onClick={() => ignoreAsyncResult(statusQuery.refetch())} type="button" variant="secondary">
            {t("job_security.retry", "Retry")}
          </Button>
        </>
      ) : null}
      {status ? (
        <div className="flex flex-wrap items-center gap-2">
          <DsBadge tone={status.enabled ? "green" : "neutral"}>
            {status.enabled ? t("react.settings.enabled", "Enabled") : t("react.settings.disabled", "Disabled")}
          </DsBadge>
          {status.required ? <span>{t("react.settings.authenticator_policy_required", "An authenticator is required for your account.")}</span> : null}
        </div>
      ) : null}

      {status && !action && !enrollment && !recoveryCodes.length ? (
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => setAction("enroll")} type="button" variant="secondary">
            {status.enabled
              ? t("react.settings.authenticator_replace", "Replace authenticator")
              : t("react.settings.authenticator_setup", "Set up authenticator")}
          </Button>
          {status.enabled && !status.required ? (
            <Button onClick={() => setAction("disable")} type="button" variant="ghost">
              {t("react.settings.authenticator_disable", "Disable authenticator")}
            </Button>
          ) : null}
        </div>
      ) : null}

      {action && status ? (
        <form className="fs-form-grid" onSubmit={(event) => ignoreAsyncResult(submitProof(event))}>
          <div className="fs-field">
            <RequiredLabel>{t("react.settings.current_password", "Current password")}</RequiredLabel>
            <SensitiveInput
              ariaLabel={t("react.settings.current_password", "Current password")}
              autoComplete="current-password"
              disabled={saving}
              id="authenticator-current-password"
              onChange={(event) => setPassword(event.target.value)}
              required
              value={password}
            />
          </div>
          {status.enabled ? (
            <div className="fs-field">
              <RequiredLabel>{t("react.auth.mfa_code", "Authenticator or recovery code")}</RequiredLabel>
              <SensitiveInput
                ariaLabel={t("react.auth.mfa_code", "Authenticator or recovery code")}
                autoComplete="one-time-code"
                disabled={saving}
                id="authenticator-current-code"
                onChange={(event) => setCode(event.target.value)}
                required
                value={code}
              />
            </div>
          ) : null}
          <div className="full flex flex-wrap gap-2 self-end">
            <Button busy={saving} type="submit" variant={action === "disable" ? "danger" : "primary"}>
              {action === "disable"
                ? t("react.settings.authenticator_disable", "Disable authenticator")
                : t("react.settings.authenticator_continue_setup", "Continue")}
            </Button>
            <Button disabled={saving} onClick={clearStep} type="button" variant="ghost">{t("common.cancel", "Cancel")}</Button>
          </div>
        </form>
      ) : null}

      {enrollment ? (
        <form className="fs-form-grid" onSubmit={(event) => ignoreAsyncResult(finishEnrollment(event))}>
          {enrollmentUri ? (
            <div className="fs-field full">
              <span>{t("react.settings.authenticator_scan_instructions", "Scan the QR code with your authenticator app or use the setup key below.")}</span>
              <AuthenticatorQr uri={enrollmentUri} />
            </div>
          ) : null}
          <div className="fs-field full">
            <span>{t("react.settings.authenticator_setup_instructions", "Add a time-based account in your authenticator app using this setup key, then enter the six-digit code.")}</span>
            <code className="break-all">{enrollment.secret}</code>
            {enrollmentUri ? (
              <a className="fs-link" href={enrollmentUri}>{t("react.settings.authenticator_open", "Open in an authenticator app")}</a>
            ) : null}
          </div>
          <label className="fs-field" htmlFor="authenticator-verification-code">
            <span>{t("react.settings.authenticator_verification_code", "Authenticator code")}</span>
            <input
              autoComplete="one-time-code"
              className="fs-input"
              disabled={saving}
              id="authenticator-verification-code"
              inputMode="numeric"
              onChange={(event) => setCode(event.target.value)}
              pattern="[0-9]{6}"
              required
              value={code}
            />
          </label>
          <div className="full flex flex-wrap gap-2 self-end">
            <Button busy={saving} type="submit" variant="primary">{t("react.settings.authenticator_verify", "Verify and enable")}</Button>
            <Button disabled={saving} onClick={clearStep} type="button" variant="ghost">{t("common.cancel", "Cancel")}</Button>
          </div>
        </form>
      ) : null}

      {recoveryCodes.length ? (
        <>
          <DsAlert tone="success" title={t("react.settings.authenticator_recovery_codes", "Save your recovery codes")}>
            {t("react.settings.authenticator_recovery_instructions", "Keep these codes somewhere safe. Each code works once, and you can only see them now.")}
            <ul className="mt-2 grid gap-1 sm:grid-cols-2">
              {recoveryCodes.map((recoveryCode) => <li key={recoveryCode}><code>{recoveryCode}</code></li>)}
            </ul>
          </DsAlert>
          <Button onClick={clearStep} type="button" variant="secondary">{t("common.close", "Close")}</Button>
        </>
      ) : null}

      {errorMessage ? <DsAlert tone="danger" title={t("react.settings.authenticator_error", "Authenticator update failed")}>{errorMessage}</DsAlert> : null}
    </DsCard>
  );
}
