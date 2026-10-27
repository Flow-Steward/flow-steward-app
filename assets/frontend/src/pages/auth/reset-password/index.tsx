import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { type ReactNode, useEffect, useState } from "react";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, AlertTriangle, Check, Info, LockKeyhole } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import {
  type FieldValues,
  type Path,
  useForm,
  useWatch,
  type UseFormReturn,
} from "react-hook-form";
import { NavLink, useSearchParams } from "react-router";

import { useAuth } from "@/lib/auth/useAuth";
import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { z } from "@/lib/validation/zod";
import { PasswordVisibilityToggle } from "@/pages/auth/password-visibility-toggle";
import {
  AuthContentPresence,
  MotionButton,
} from "@/pages/auth/public-auth-motion";
import {
  AUTH_BUTTON_MOTION,
  AUTH_FEEDBACK_MOTION,
  AUTH_FEEDBACK_TRANSITION,
  AUTH_LAYOUT_TRANSITION,
} from "@/pages/auth/public-auth-motion-config";

import "@/pages/auth/login/public-auth-sign-in.css";

function getPasswordresetschema(t: I18nContextValue["t"] = fallbackContext.t) {
  return z.object({
  email: z.string().trim().min(1, t("react.auth.enter_your_email", "Enter your email.")).pipe(z.email(t("users.validation.invalid_email", "Enter a valid email address."))),
});
}

function getPasswordresetconfirmschema(t: I18nContextValue["t"] = fallbackContext.t) {
  return z
  .object({
    password: z.string().min(10, t("react.auth.use_at_least_10_characters", "Use at least 10 characters.")),
    confirmPassword: z.string().min(10, t("react.auth.use_at_least_10_characters", "Use at least 10 characters.")),
    revokeOtherSessions: z.boolean(),
  })
  .refine((values) => values.password === values.confirmPassword, {
    message: t("auth.common.passwords_do_not_match", "Passwords do not match."),
    path: ["confirmPassword"],
  });
}

type PasswordResetValues = z.infer<ReturnType<typeof getPasswordresetschema>>;
type PasswordResetConfirmValues = z.infer<ReturnType<typeof getPasswordresetconfirmschema>>;

type ResetNotice = {
  copy: string;
  title: string;
  tone: "danger" | "info" | "warning";
};

function passwordUpdatedNotice(revokeOtherSessions: boolean, revokedSessions: number, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (!revokeOtherSessions) {
    return t("react.auth.password_updated", "Password updated.");
  }
  return revokedSessions === 1
    ? t("react.auth.password_updated_one_session_revoked", "Password updated. 1 other session revoked.")
    : t("react.auth.password_updated_sessions_revoked", "Password updated. {count} other sessions revoked.", { count: revokedSessions });
}

function PasswordResetNotice({ notice, role = "alert" }: Readonly<{
  notice: ResetNotice;
  role?: "alert" | "status";
}>) {
  let Icon = Info;
  if (notice.tone === "danger") {
    Icon = AlertCircle;
  } else if (notice.tone === "warning") {
    Icon = AlertTriangle;
  }
  return (
    <motion.div
      {...AUTH_FEEDBACK_MOTION}
      className="fs-public-auth__alert"
      data-tone={notice.tone}
      layout
      role={role}
    >
      <span aria-hidden="true" className="fs-public-auth__alert-icon">
        <Icon data-icon size={16} strokeWidth={2} />
      </span>
      <div>
        <strong>{notice.title}</strong>
        <p>{notice.copy}</p>
      </div>
    </motion.div>
  );
}

function RecoveryIcon() {
  return (
    <motion.span
      animate={{ opacity: 1, transform: "scale(1) rotate(0deg)" }}
      aria-hidden="true"
      className="fs-public-auth__recovery-icon"
      initial={{ opacity: 0, transform: "scale(0.92) rotate(-5deg)" }}
      transition={{ bounce: 0.04, type: "spring", visualDuration: 0.24 }}
    >
      <LockKeyhole data-icon size={20} strokeWidth={2} />
    </motion.span>
  );
}

function resetErrorNotice(error: unknown, t: I18nContextValue["t"] = fallbackContext.t): ResetNotice {
  if (!(error instanceof HttpError)) {
    return {
      copy: t("react.auth.check_your_connection_and_try_again", "Check your connection and try again."),
      title: t("react.auth.we_couldn_t_reach_flow_steward", "We couldn’t reach Flow Steward."),
      tone: "danger",
    };
  }
  if (error.status === 429) {
    return {
      copy: error.retryAfterSeconds
        ? t("react.auth.try_again_in_value1_seconds", "Try again in {value1} seconds.", { value1: String(Math.ceil(error.retryAfterSeconds)) })
        : t("react.auth.wait_a_moment_before_trying_again", "Wait a moment before trying again."),
      title: t("react.auth.too_many_reset_requests", "Too many reset requests."),
      tone: "warning",
    };
  }
  if (error.status >= 500) {
    return {
      copy: t("react.auth.your_email_is_still_here_try_again_in_a_moment", "Your email is still here. Try again in a moment."),
      title: t("react.auth.password_recovery_is_temporarily_unavailable", "Password recovery is temporarily unavailable."),
      tone: "danger",
    };
  }
  return {
    copy: error.message || t("react.auth.try_again_in_a_moment", "Try again in a moment."),
    title: t("react.auth.we_couldn_t_send_the_reset_link", "We couldn’t send the reset link."),
    tone: "danger",
  };
}

function ResetPasswordShell({ children }: Readonly<{ children: ReactNode }>) {
  const { t } = useI18n();
  return (
    <>
      <a className="fs-public-auth__skip-link" href="#public-auth-reset">{t("react.auth.skip_to_account_recovery", "Skip to account recovery")}</a>
      <main className="fs-public-auth">
        <section aria-labelledby="public-auth-reset-title" className="fs-public-auth__sign-in-panel" id="public-auth-reset">
          <motion.div className="fs-public-auth__sign-in-column fs-public-auth__sign-in-column--compact" layout style={{ borderRadius: "var(--fs-radius-large)", boxShadow: "0 20px 48px -30px rgb(0 5 9 / 30%)" }} transition={{ layout: AUTH_LAYOUT_TRANSITION }}>
            {children}
          </motion.div>
        </section>
      </main>
    </>
  );
}

type VisibilitySetter = (updater: (visible: boolean) => boolean) => void;

/** Also used by the sign-in page when a temporary password must be replaced. */
export function NewPasswordField<T extends FieldValues>({
  form,
  helper,
  name,
  passwordVisible,
  setPasswordVisible,
}: Readonly<{
  form: UseFormReturn<T>;
  helper?: string;
  name: Path<T>;
  passwordVisible: boolean;
  setPasswordVisible: VisibilitySetter;
}>) {
  const { t } = useI18n();
  const error = form.getFieldState(name, form.formState).error;
  return (
    <motion.div className="fs-public-auth__field" layout transition={{ layout: AUTH_LAYOUT_TRANSITION }}>
      <label className="fs-public-auth__field-label" htmlFor="new-password">{t("react.auth.new_password", "New password")}</label>
      <span className="fs-public-auth__input-shell">
        <input {...form.register(name)} aria-describedby={error ? "new-password-error" : "new-password-helper"} aria-invalid={Boolean(error)} autoComplete="new-password" className="fs-public-auth__input" id="new-password" placeholder={t("react.auth.create_a_password", "Create a password")} type={passwordVisible ? "text" : "password"} />
        <PasswordVisibilityToggle hideLabel={t("react.auth.hide_new_password", "Hide new password")} onToggle={() => setPasswordVisible((visible) => !visible)} showLabel={t("react.auth.show_new_password", "Show new password")} visible={passwordVisible} />
      </span>
      <motion.span {...AUTH_FEEDBACK_MOTION} className={`fs-public-auth__field-helper${error ? " fs-public-auth__field-helper--error" : ""}`} id={error ? "new-password-error" : "new-password-helper"}>{error?.message || helper || t("react.auth.use_at_least_10_characters", "Use at least 10 characters.")}</motion.span>
    </motion.div>
  );
}

export function ConfirmPasswordField<T extends FieldValues>({
  confirmationVisible,
  form,
  name,
  setConfirmationVisible,
}: Readonly<{
  confirmationVisible: boolean;
  form: UseFormReturn<T>;
  name: Path<T>;
  setConfirmationVisible: VisibilitySetter;
}>) {
  const { t } = useI18n();
  const error = form.getFieldState(name, form.formState).error;
  return (
    <motion.div className="fs-public-auth__field" layout transition={{ layout: AUTH_LAYOUT_TRANSITION }}>
      <label className="fs-public-auth__field-label" htmlFor="confirm-password">{t("react.auth.confirm_password", "Confirm password")}</label>
      <span className="fs-public-auth__input-shell">
        <input {...form.register(name)} aria-describedby={error ? "confirm-password-error" : undefined} aria-invalid={Boolean(error)} autoComplete="new-password" className="fs-public-auth__input" id="confirm-password" placeholder={t("react.auth.repeat_your_password", "Repeat your password")} type={confirmationVisible ? "text" : "password"} />
        <PasswordVisibilityToggle hideLabel={t("react.auth.hide_confirmed_password", "Hide confirmed password")} onToggle={() => setConfirmationVisible((visible) => !visible)} showLabel={t("react.auth.show_confirmed_password", "Show confirmed password")} visible={confirmationVisible} />
      </span>
      <AnimatePresence initial={false}>{error ? <motion.span {...AUTH_FEEDBACK_MOTION} className="fs-public-auth__field-helper fs-public-auth__field-helper--error" id="confirm-password-error" key="confirm-password-error">{error.message}</motion.span> : null}</AnimatePresence>
    </motion.div>
  );
}

function RevokeSessionsField({
  form,
  revokeOtherSessions,
}: Readonly<{
  form: UseFormReturn<PasswordResetConfirmValues>;
  revokeOtherSessions: boolean;
}>) {
  const { t } = useI18n();
  return (
    <label className="fs-public-auth__checkbox-label">
      <input {...form.register("revokeOtherSessions")} type="checkbox" />
      <motion.span animate={{ transform: revokeOtherSessions ? "scale(1)" : "scale(0.94)" }} aria-hidden="true" className="fs-public-auth__checkbox-mark" transition={{ bounce: 0, type: "spring", visualDuration: 0.16 }}>
        <motion.span animate={{ opacity: revokeOtherSessions ? 1 : 0, transform: revokeOtherSessions ? "scale(1)" : "scale(0.8)" }} className="fs-public-auth__checkbox-glyph" transition={AUTH_FEEDBACK_TRANSITION}><Check data-icon size={12} strokeWidth={2} /></motion.span>
      </motion.span>
      <span>{t("react.auth.sign_out_other_sessions_after_reset", "Sign out other sessions after reset")}</span>
    </label>
  );
}

function ConfirmResetFeedback({
  confirmError,
  confirmNotice,
  submitting,
}: Readonly<{ confirmError: ResetNotice | null; confirmNotice: string; submitting: boolean }>) {
  const { t } = useI18n();
  return (
    <>
      <AnimatePresence initial={false}>
        {confirmError ? <PasswordResetNotice key="confirm-error" notice={confirmError} /> : null}
        {confirmNotice ? <PasswordResetNotice key="confirm-success" notice={{ copy: confirmNotice, title: t("react.auth.password_updated_2", "Password updated"), tone: "info" }} role="status" /> : null}
      </AnimatePresence>
      <MotionButton {...AUTH_BUTTON_MOTION} aria-label={submitting ? t("react.auth.updating_password", "Updating password…") : undefined} aria-busy={submitting} className="fs-public-auth__button fs-public-auth__button--primary" data-loading={submitting ? "true" : undefined} disabled={submitting} type="submit">
        <AnimatePresence initial={false}>{submitting ? <motion.span animate={{ opacity: 1 }} aria-hidden="true" className="fs-public-auth__button-spinner" data-testid="auth-submit-spinner" exit={{ opacity: 0 }} initial={{ opacity: 0 }} transition={AUTH_FEEDBACK_TRANSITION} /> : null}</AnimatePresence>
        <span className="fs-public-auth__button-label">{t("react.auth.update_password", "Update password")}</span>
        <span className="fs-public-auth__reduced-loading-label">{t("react.auth.updating_password", "Updating password…")}</span>
      </MotionButton>
    </>
  );
}

function ConfirmResetForm({
  confirmError,
  confirmationVisible,
  confirmForm,
  confirmNotice,
  onConfirm,
  passwordVisible,
  revokeOtherSessions,
  setConfirmationVisible,
  setPasswordVisible,
}: Readonly<{
  confirmError: ResetNotice | null;
  confirmationVisible: boolean;
  confirmForm: UseFormReturn<PasswordResetConfirmValues>;
  confirmNotice: string;
  onConfirm: (values: PasswordResetConfirmValues) => Promise<void>;
  passwordVisible: boolean;
  revokeOtherSessions: boolean;
  setConfirmationVisible: VisibilitySetter;
  setPasswordVisible: VisibilitySetter;
}>) {
  const { t } = useI18n();
  return (
    <form className="fs-public-auth__reset-form" noValidate onSubmit={(event) => { ignoreAsyncResult(confirmForm.handleSubmit(onConfirm)(event)); }}>
      <NewPasswordField form={confirmForm} name="password" passwordVisible={passwordVisible} setPasswordVisible={setPasswordVisible} />
      <ConfirmPasswordField confirmationVisible={confirmationVisible} form={confirmForm} name="confirmPassword" setConfirmationVisible={setConfirmationVisible} />
      <RevokeSessionsField form={confirmForm} revokeOtherSessions={revokeOtherSessions} />
      <ConfirmResetFeedback confirmError={confirmError} confirmNotice={confirmNotice} submitting={confirmForm.formState.isSubmitting} />
      <NavLink className="fs-public-auth__quiet-link" to="/login">{t("react.auth.back_to_sign_in", "Back to sign in")}</NavLink>
    </form>
  );
}

function TokenResetView({
  confirmForm,
  confirmError,
  confirmationVisible,
  confirmNotice,
  invalidToken,
  loading,
  onConfirm,
  passwordVisible,
  revokeOtherSessions,
  setConfirmationVisible,
  setPasswordVisible,
  verificationEmail,
}: Readonly<{
  confirmForm: UseFormReturn<PasswordResetConfirmValues>;
  confirmError: ResetNotice | null;
  confirmationVisible: boolean;
  confirmNotice: string;
  invalidToken: boolean;
  loading: boolean;
  onConfirm: (values: PasswordResetConfirmValues) => Promise<void>;
  passwordVisible: boolean;
  revokeOtherSessions: boolean;
  setConfirmationVisible: VisibilitySetter;
  setPasswordVisible: VisibilitySetter;
  verificationEmail?: string;
}>) {
  const { t } = useI18n();
  return (
    <ResetPasswordShell>
      <AnimatePresence initial={false} mode="wait">
        {loading ? <motion.div {...AUTH_FEEDBACK_MOTION} aria-live="polite" className="fs-public-auth__reset-loading" key="verifying" role="status">{t("react.auth.verifying_reset_link", "Verifying reset link…")}</motion.div> : null}
        {invalidToken ? <motion.div {...AUTH_FEEDBACK_MOTION} className="fs-public-auth__reset-content" key="invalid-token" layout><RecoveryIcon /><header className="fs-public-auth__reset-header"><h1 id="public-auth-reset-title">{t("react.auth.reset_link_unavailable", "Reset link unavailable")}</h1><p>{t("react.auth.this_password_reset_link_is_invalid_or_expired", "This password reset link is invalid or expired.")}</p></header><MotionButton {...AUTH_BUTTON_MOTION} asChild className="fs-public-auth__button fs-public-auth__button--primary" variant="primary"><NavLink to="/forgot-password">{t("react.auth.request_a_new_reset_link", "Request a new reset link")}</NavLink></MotionButton></motion.div> : null}
        {!loading && !invalidToken ? (
          <motion.div {...AUTH_FEEDBACK_MOTION} className="fs-public-auth__reset-content" key="reset-form" layout>
            <RecoveryIcon />
            <header className="fs-public-auth__reset-header"><h1 id="public-auth-reset-title">{t("react.auth.choose_a_new_password", "Choose a new password")}</h1><p>{verificationEmail ? t("react.auth.resetting_password_for_value1", "Resetting password for {value1}.", { value1: String(verificationEmail) }) : t("react.auth.enter_and_confirm_your_new_password_to_complete_account_recovery", "Enter and confirm your new password to complete account recovery.")}</p></header>
            <ConfirmResetForm confirmError={confirmError} confirmationVisible={confirmationVisible} confirmForm={confirmForm} confirmNotice={confirmNotice} onConfirm={onConfirm} passwordVisible={passwordVisible} revokeOtherSessions={revokeOtherSessions} setConfirmationVisible={setConfirmationVisible} setPasswordVisible={setPasswordVisible} />
          </motion.div>
        ) : null}
      </AnimatePresence>
    </ResetPasswordShell>
  );
}

function RequestResetFeedback({ requestNotice, submittedEmail }: Readonly<{ requestNotice: ResetNotice | null; submittedEmail: string }>) {
  const { t } = useI18n();
  return (
    <AnimatePresence initial={false}>
      {submittedEmail ? <PasswordResetNotice key="reset-request-success" notice={{ copy: t("react.auth.if_value1_exists_in_flow_steward_the_reset_instructions_are_on_the_way", "If {value1} exists in Flow Steward, the reset instructions are on the way.", { value1: String(submittedEmail) }), title: t("react.auth.check_your_email", "Check your email"), tone: "info" }} role="status" /> : null}
      {requestNotice ? <PasswordResetNotice key="reset-request-error" notice={requestNotice} /> : null}
    </AnimatePresence>
  );
}

function RequestResetView({
  form,
  onSubmit,
  requestNotice,
  submittedEmail,
}: Readonly<{
  form: UseFormReturn<PasswordResetValues>;
  onSubmit: (values: PasswordResetValues) => Promise<void>;
  requestNotice: ResetNotice | null;
  submittedEmail: string;
}>) {
  const { t } = useI18n();
  const error = form.formState.errors.email;
  const submitting = form.formState.isSubmitting;
  return (
    <ResetPasswordShell>
      <AuthContentPresence className="fs-public-auth__recovery-content" stateKey="request-reset">
        <RecoveryIcon />
        <header className="fs-public-auth__reset-header"><h1 id="public-auth-reset-title">{t("auth.login.reset_password_link", "Reset your password")}</h1><p>{t("react.auth.enter_the_email_tied_to_your_account_and_we_ll_send_a_secure_link_to_set_a_new_password", "Enter the email tied to your account and we’ll send a secure link to set a new password.")}</p></header>
        <form className="fs-public-auth__reset-form" noValidate onSubmit={(event) => { ignoreAsyncResult(form.handleSubmit(onSubmit)(event)); }}>
          <motion.div className="fs-public-auth__field" layout transition={{ layout: AUTH_LAYOUT_TRANSITION }}>
            <label className="fs-public-auth__field-label" htmlFor="reset-email">{t("react.auth.email_address", "Email address")}</label>
            <input {...form.register("email")} aria-describedby={error ? "reset-email-error" : undefined} aria-invalid={Boolean(error)} autoCapitalize="none" autoComplete="email" className="fs-public-auth__input" id="reset-email" inputMode="email" spellCheck={false} type="email" />
            <AnimatePresence initial={false}>{error ? <motion.span {...AUTH_FEEDBACK_MOTION} className="fs-public-auth__field-helper fs-public-auth__field-helper--error" id="reset-email-error" key="reset-email-error">{error.message}</motion.span> : null}</AnimatePresence>
          </motion.div>
          <RequestResetFeedback requestNotice={requestNotice} submittedEmail={submittedEmail} />
          <div className="fs-public-auth__recovery-actions">
            <MotionButton {...AUTH_BUTTON_MOTION} asChild className="fs-public-auth__button fs-public-auth__button--secondary" variant="secondary"><NavLink to="/login">{t("common.cancel", "Cancel")}</NavLink></MotionButton>
            <MotionButton {...AUTH_BUTTON_MOTION} aria-label={submitting ? t("react.auth.sending_reset_link", "Sending reset link…") : undefined} aria-busy={submitting} className="fs-public-auth__button fs-public-auth__button--primary" data-loading={submitting ? "true" : undefined} disabled={submitting} type="submit">
              <AnimatePresence initial={false}>{submitting ? <motion.span animate={{ opacity: 1 }} aria-hidden="true" className="fs-public-auth__button-spinner" data-testid="auth-submit-spinner" exit={{ opacity: 0 }} initial={{ opacity: 0 }} transition={AUTH_FEEDBACK_TRANSITION} /> : null}</AnimatePresence>
              <span className="fs-public-auth__button-label">{t("react.auth.send_reset_link", "Send reset link")}</span><span className="fs-public-auth__reduced-loading-label">{t("react.auth.sending_reset_link", "Sending reset link…")}</span>
            </MotionButton>
          </div>
        </form>
      </AuthContentPresence>
    </ResetPasswordShell>
  );
}

/** Without a connected email extension there is no reset link to send. */
function ResetUnavailableView() {
  const { t } = useI18n();
  return (
    <ResetPasswordShell>
      <AuthContentPresence className="fs-public-auth__recovery-content" stateKey="reset-unavailable">
        <RecoveryIcon />
        <header className="fs-public-auth__reset-header"><h1 id="public-auth-reset-title">{t("react.auth.ask_your_administrator", "Ask your administrator")}</h1><p>{t("react.auth.email_is_not_set_up_in_flow_steward_so_a_reset_link_cannot_be_sent_an_administrator_can_set_a_new_password_for", "Email is not set up in Flow Steward, so a reset link cannot be sent. An administrator can set a new password for you in Settings → Users.")}</p></header>
        <div className="fs-public-auth__recovery-actions">
          <MotionButton {...AUTH_BUTTON_MOTION} asChild className="fs-public-auth__button fs-public-auth__button--primary" variant="primary"><NavLink to="/login">{t("react.auth.back_to_sign_in", "Back to sign in")}</NavLink></MotionButton>
        </div>
      </AuthContentPresence>
    </ResetPasswordShell>
  );
}

export function ResetPasswordPage() {
  const { t } = useI18n();
  const auth = useAuth();
  const [searchParams] = useSearchParams();
  const token = firstStringFromUnknownValue([searchParams.get("token"), ""]).trim();
  const [submittedEmail, setSubmittedEmail] = useState("");
  const [confirmNotice, setConfirmNotice] = useState("");
  const [requestNotice, setRequestNotice] = useState<ResetNotice | null>(null);
  const [confirmError, setConfirmError] = useState<ResetNotice | null>(null);
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [confirmationVisible, setConfirmationVisible] = useState(false);
  const form = useForm<PasswordResetValues>({
    defaultValues: { email: "" },
    resolver: zodResolver(getPasswordresetschema(t)),
  });
  const confirmForm = useForm<PasswordResetConfirmValues>({
    defaultValues: {
      password: "",
      confirmPassword: "",
      revokeOtherSessions: true,
    },
    resolver: zodResolver(getPasswordresetconfirmschema(t)),
  });
  const revokeOtherSessions = useWatch({
    control: confirmForm.control,
    name: "revokeOtherSessions",
  });
  const tokenVerificationQuery = useQuery({
    queryKey: ["auth", "password-reset", "verify", token],
    queryFn: () =>
      fetchJson<{ valid?: boolean; email?: string; username?: string }>(
        "/api/auth/password-reset/verify",
        {
          method: "POST",
          body: JSON.stringify({ token }),
        },
      ),
    enabled: Boolean(token),
    retry: false,
  });

  useEffect(() => {
    if (!token) {
      form.setFocus("email");
    }
  }, [form, token]);

  async function onSubmit(values: PasswordResetValues) {
    setRequestNotice(null);
    setSubmittedEmail("");
    try {
      await fetchJson<{ status: string }>("/api/auth/password-reset/request", {
        method: "POST",
        body: JSON.stringify(values),
      });
      setSubmittedEmail(values.email);
      form.reset(values);
    } catch (error) {
      setRequestNotice(resetErrorNotice(error, t));
    }
  }

  async function onConfirm(values: PasswordResetConfirmValues) {
    setConfirmNotice("");
    setConfirmError(null);
    if (!tokenVerificationQuery.data?.valid) {
      return;
    }
    try {
      const result = await fetchJson<{ status: string; revoked_sessions?: number }>(
        "/api/auth/password-reset/confirm",
        {
          method: "POST",
          body: JSON.stringify({
            token,
            new_password: values.password,
            revoke_other_sessions: values.revokeOtherSessions,
          }),
        },
      );
      const revoked = Number(result.revoked_sessions || 0);
      setConfirmNotice(passwordUpdatedNotice(values.revokeOtherSessions, revoked, t));
      setPasswordVisible(false);
      setConfirmationVisible(false);
      confirmForm.reset({
        password: "",
        confirmPassword: "",
        revokeOtherSessions: values.revokeOtherSessions,
      });
    } catch (error) {
      setConfirmError(resetErrorNotice(error, t));
    }
  }

  if (token) {
    const invalidToken = tokenVerificationQuery.isError
      || (tokenVerificationQuery.isFetched && !tokenVerificationQuery.data?.valid);
    return (
      <TokenResetView
        confirmError={confirmError}
        confirmationVisible={confirmationVisible}
        confirmForm={confirmForm}
        confirmNotice={confirmNotice}
        invalidToken={invalidToken}
        loading={tokenVerificationQuery.isLoading}
        onConfirm={onConfirm}
        passwordVisible={passwordVisible}
        revokeOtherSessions={revokeOtherSessions}
        setConfirmationVisible={setConfirmationVisible}
        setPasswordVisible={setPasswordVisible}
        verificationEmail={tokenVerificationQuery.data?.email}
      />
    );
  }

  if (auth.session && !auth.session.system_email_available) {
    return <ResetUnavailableView />;
  }

  return (
    <RequestResetView
      form={form}
      onSubmit={onSubmit}
      requestNotice={requestNotice}
      submittedEmail={submittedEmail}
    />
  );
}
