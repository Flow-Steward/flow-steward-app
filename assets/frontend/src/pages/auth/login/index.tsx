import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import {
  type FocusEvent,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";

import { zodResolver } from "@hookform/resolvers/zod";
import {
  AlertCircle,
  AlertTriangle,
  Check,
  ChevronDown,
  Info,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import {
  useForm,
  useWatch,
  type UseFormRegisterReturn,
  type UseFormReturn,
} from "react-hook-form";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router";

import { useAuth } from "@/lib/auth/useAuth";
import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { z } from "@/lib/validation/zod";
import { normalizeFlashMessage } from "@/pages/auth/login/flash";
import { PasswordVisibilityToggle } from "@/pages/auth/password-visibility-toggle";
import { ConfirmPasswordField, NewPasswordField } from "@/pages/auth/reset-password";
import {
  AuthContentPresence,
  MotionButton,
} from "@/pages/auth/public-auth-motion";
import {
  AUTH_BUTTON_MOTION,
  AUTH_FEEDBACK_MOTION,
  AUTH_FEEDBACK_TRANSITION,
  AUTH_LAYOUT_TRANSITION,
  AUTH_NOTICE_MOTION,
} from "@/pages/auth/public-auth-motion-config";

import "@/pages/auth/login/public-auth-sign-in.css";

function getLoginschema(t: I18nContextValue["t"] = fallbackContext.t) {
  return z.object({
  email: z.string().trim().min(1, t("react.auth.enter_your_email", "Enter your email.")).pipe(z.email(t("users.validation.invalid_email", "Enter a valid email address."))),
  password: z.string().min(1, t("react.auth.enter_your_password", "Enter your password.")),
  rememberMe: z.boolean(),
  accountId: z.string(),
  newPassword: z.string(),
  confirmPassword: z.string(),
});
}

type LoginFormValues = z.infer<ReturnType<typeof getLoginschema>>;

type AccountOption = {
  account_id: string;
  account_name?: string;
  display_name?: string;
};

type LoginResponse = {
  user: {
    username: string;
  };
};

type ExternalSignInProvider = {
  provider_key: string;
  display_name?: string;
  login_path?: string;
};

type ExternalSignInState = {
  providers: ExternalSignInProvider[];
  requestedPath: string;
  status: "idle" | "loading" | "ready" | "unavailable";
};

/** A temporary password was accepted: it may only be replaced, never used to sign in. */
type PasswordChange = { minLength: number };

type AuthNotice = {
  copy: string;
  title: string;
  tone: "danger" | "info" | "neutral" | "warning";
};

function isSafeLocalPath(path: string) {
  return path.startsWith("/") && !path.startsWith("//") && !path.includes("\\");
}

function appendNextParam(path: string, nextPath: string) {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}next=${encodeURIComponent(nextPath)}`;
}

function normalizeNextPath(raw: string | null) {
  return raw && isSafeLocalPath(raw) ? raw : "/dashboard";
}

function payloadRecord(error: HttpError) {
  if (!error.payload || typeof error.payload !== "object" || Array.isArray(error.payload)) {
    return {} as Record<string, unknown>;
  }
  return error.payload as Record<string, unknown>;
}

function loginErrorNotice(error: unknown, t: I18nContextValue["t"] = fallbackContext.t): AuthNotice {
  if (!(error instanceof HttpError)) {
    return {
      copy: t("react.auth.check_your_connection_and_try_again", "Check your connection and try again."),
      title: t("react.auth.we_couldn_t_reach_flow_steward", "We couldn’t reach Flow Steward."),
      tone: "danger",
    };
  }

  const payload = payloadRecord(error);
  const message = error.message.toLowerCase();

  if (error.status === 401) {
    return {
      copy: t("react.auth.check_your_email_and_password_then_try_again", "Check your email and password, then try again."),
      title: t("react.auth.we_couldn_t_sign_you_in", "We couldn’t sign you in."),
      tone: "danger",
    };
  }

  if (error.status === 429) {
    const retryAfterSeconds = Number(payload.retry_after_seconds || error.retryAfterSeconds);
    const retryCopy = Number.isFinite(retryAfterSeconds) && retryAfterSeconds > 0
      ? t("react.auth.try_again_in_value1_seconds", "Try again in {value1} seconds.", { value1: String(Math.ceil(retryAfterSeconds)) })
      : t("react.auth.wait_a_moment_before_trying_again", "Wait a moment before trying again.");
    return {
      copy: retryCopy,
      title: t("react.auth.too_many_sign_in_attempts", "Too many sign-in attempts."),
      tone: "warning",
    };
  }

  if (error.status === 403 && message.includes("selected account is disabled")) {
    return {
      copy: t("react.auth.choose_another_account_or_contact_your_workspace_administrator", "Choose another account or contact your workspace administrator."),
      title: t("react.auth.this_account_is_disabled", "This account is disabled."),
      tone: "danger",
    };
  }

  if (error.status === 403 && message.includes("selected account is not available")) {
    return {
      copy: t("react.auth.choose_an_account_linked_to_this_sign_in_and_try_again", "Choose an account linked to this sign-in and try again."),
      title: t("react.auth.that_account_is_unavailable", "That account is unavailable."),
      tone: "danger",
    };
  }

  if (error.status === 400 && message.includes("local auth is disabled")) {
    return {
      copy: t("react.auth.use_a_configured_external_provider_to_continue", "Use a configured external provider to continue."),
      title: t("react.auth.password_sign_in_is_unavailable", "Password sign-in is unavailable."),
      tone: "neutral",
    };
  }

  if (error.status >= 500) {
    return {
      copy: t("react.auth.your_details_are_still_here_try_again_in_a_moment", "Your details are still here. Try again in a moment."),
      title: t("react.auth.sign_in_is_temporarily_unavailable", "Sign-in is temporarily unavailable."),
      tone: "danger",
    };
  }

  return {
    copy: error.message || t("react.auth.try_again_in_a_moment", "Try again in a moment."),
    title: t("react.auth.we_couldn_t_complete_sign_in", "We couldn’t complete sign-in."),
    tone: "danger",
  };
}

function accountSelectionFromLoginError(error: unknown): {
  required: boolean;
  options: AccountOption[];
} {
  if (!(error instanceof HttpError) || error.status !== 409) {
    return { required: false, options: [] };
  }
  const payload = payloadRecord(error);
  if (!payload.requires_account_selection) return { required: false, options: [] };
  const candidates = Array.isArray(payload.account_options) ? payload.account_options : [];
  const options = candidates.filter((option): option is AccountOption => (
    Boolean(option)
    && typeof option === "object"
    && typeof (option as AccountOption).account_id === "string"
    && Boolean((option as AccountOption).account_id)
  ));
  return { required: true, options };
}

function passwordChangeFromLoginError(error: unknown): PasswordChange | null {
  if (!(error instanceof HttpError) || error.status !== 403) return null;
  const payload = payloadRecord(error);
  if (!payload.password_change_required) return null;
  const minLength = Number(payload.password_min_length);
  return { minLength: Number.isFinite(minLength) && minLength > 0 ? minLength : 10 };
}

function newPasswordErrorFromLoginError(error: unknown): string {
  if (!(error instanceof HttpError) || error.status !== 400) return "";
  return payloadRecord(error).field === "new_password" ? error.message : "";
}

function WaveLoader() {
  return (
    <span aria-hidden="true" className="fs-public-auth__wave-loader">
      <span />
      <span />
      <span />
      <span />
    </span>
  );
}

function Notice({ notice, role = "alert" }: Readonly<{ notice: AuthNotice; role?: "alert" | "status" }>) {
  let Icon = Info;
  if (notice.tone === "danger") {
    Icon = AlertCircle;
  } else if (notice.tone === "warning") {
    Icon = AlertTriangle;
  }

  return (
    <motion.div
      {...AUTH_NOTICE_MOTION}
      className="fs-public-auth__alert"
      data-tone={notice.tone}
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

function placeCaretAfterAutofilledPassword(event: FocusEvent<HTMLInputElement>) {
  const input = event.currentTarget;
  window.requestAnimationFrame(() => {
    if (document.activeElement !== input) return;
    if (input.selectionStart === input.selectionEnd) return;
    const end = input.value.length;
    input.setSelectionRange(end, end);
  });
}

function ExternalProviderSection({
  localAuthEnabled,
  nextPath,
  providers,
  status,
  visible,
}: Readonly<{
  localAuthEnabled: boolean;
  nextPath: string;
  providers: ExternalSignInProvider[];
  status: ExternalSignInState["status"];
  visible: boolean;
}>) {
  const { t } = useI18n();
  if (!visible) return null;
  return (
    <section aria-labelledby="public-auth-provider-title" className="fs-public-auth__provider-section">
      <h3 id="public-auth-provider-title">{t("react.auth.continue_with_external_sign_in", "Continue with external sign-in")}</h3>
      {status === "loading" ? <div aria-live="polite" className="fs-public-auth__provider-loading"><WaveLoader /><span>{t("react.auth.loading_sign_in_providers", "Loading sign-in providers…")}</span></div> : null}
      {status === "ready" ? (
        <div className="fs-public-auth__provider-list">
          {providers.map((provider) => (
            <MotionButton {...AUTH_BUTTON_MOTION} asChild className="fs-public-auth__button fs-public-auth__provider-button" key={provider.provider_key} size="lg" variant="secondary">
              <a href={appendNextParam(provider.login_path || "", nextPath)}>{t("react.auth.continue_with", "Continue with")} {provider.display_name}</a>
            </MotionButton>
          ))}
        </div>
      ) : null}
      {status === "unavailable" ? (
        <Notice notice={{ copy: localAuthEnabled ? t("react.auth.you_can_still_use_your_email_and_password", "You can still use your email and password.") : t("react.auth.contact_your_workspace_administrator_or_try_again_later", "Contact your workspace administrator or try again later."), title: t("react.auth.no_external_providers_are_available", "No external providers are available."), tone: "neutral" }} role="status" />
      ) : null}
    </section>
  );
}

function LoginEmailField({ form }: Readonly<{ form: UseFormReturn<LoginFormValues> }>) {
  const { t } = useI18n();
  const error = form.formState.errors.email;
  return (
    <motion.div className="fs-public-auth__field" layout transition={{ layout: AUTH_LAYOUT_TRANSITION }}>
      <label className="fs-public-auth__field-label" htmlFor="email">{t("react.auth.email_address", "Email address")}</label>
      <input {...form.register("email")} aria-describedby={error ? "email-error" : undefined} aria-invalid={Boolean(error)} autoCapitalize="none" autoComplete="username" className="fs-public-auth__input" id="email" inputMode="email" placeholder={t("react.auth.you_company_com", "you@company.com")} spellCheck={false} type="email" />
      <AnimatePresence initial={false}>{error ? <motion.span {...AUTH_FEEDBACK_MOTION} className="fs-public-auth__field-helper fs-public-auth__field-helper--error" id="email-error" key="email-error">{error.message}</motion.span> : null}</AnimatePresence>
    </motion.div>
  );
}

function LoginPasswordField({
  forgotPasswordAvailable,
  form,
  label,
  passwordVisible,
  setPasswordVisible,
}: Readonly<{
  forgotPasswordAvailable: boolean;
  form: UseFormReturn<LoginFormValues>;
  label?: string;
  passwordVisible: boolean;
  setPasswordVisible: (updater: (visible: boolean) => boolean) => void;
}>) {
  const { t } = useI18n();
  const error = form.formState.errors.password;
  return (
    <motion.div className="fs-public-auth__field" layout transition={{ layout: AUTH_LAYOUT_TRANSITION }}>
      <div className="fs-public-auth__field-label-row"><label className="fs-public-auth__field-label" htmlFor="password">{label || t("auth.common.password", "Password")}</label>{forgotPasswordAvailable ? <Link className="fs-public-auth__quiet-link" to="/forgot-password">{t("auth.login.forgot_password", "Forgot password?")}</Link> : null}</div>
      <span className="fs-public-auth__input-shell" data-password-state={passwordVisible ? "visible" : "masked"}>
        <input {...form.register("password")} aria-describedby={error ? "password-error" : undefined} aria-invalid={Boolean(error)} autoComplete="current-password" className="fs-public-auth__input" id="password" onFocus={placeCaretAfterAutofilledPassword} placeholder={t("react.auth.enter_password", "Enter password")} type={passwordVisible ? "text" : "password"} />
        <span aria-hidden="true" className="fs-public-auth__fixed-password-mask">••••••••••</span>
        <PasswordVisibilityToggle hideLabel={t("react.auth.hide_password", "Hide password")} onToggle={() => setPasswordVisible((visible) => !visible)} showLabel={t("react.auth.show_password", "Show password")} visible={passwordVisible} />
      </span>
      <AnimatePresence initial={false}>{error ? <motion.span {...AUTH_FEEDBACK_MOTION} className="fs-public-auth__field-helper fs-public-auth__field-helper--error" id="password-error" key="password-error">{error.message}</motion.span> : null}</AnimatePresence>
    </motion.div>
  );
}

function LoginAccountField({
  accountOptions,
  accountRegistration,
  accountSelectRef,
  form,
}: Readonly<{
  accountOptions: AccountOption[];
  accountRegistration: UseFormRegisterReturn<"accountId">;
  accountSelectRef: RefObject<HTMLSelectElement | null>;
  form: UseFormReturn<LoginFormValues>;
}>) {
  const { t } = useI18n();
  if (!accountOptions.length) return null;
  const error = form.formState.errors.accountId;
  return (
    <AnimatePresence initial={false}>
      <motion.div {...AUTH_FEEDBACK_MOTION} className="fs-public-auth__field" key="account-selection" layout>
        <label className="fs-public-auth__field-label" htmlFor="account-id">{t("auth.login.account_label", "Account")}</label>
        <span className="fs-public-auth__select-shell">
          <select {...accountRegistration} aria-describedby={error ? "account-error" : "account-helper"} aria-invalid={Boolean(error)} className="fs-public-auth__select" id="account-id" ref={(element) => { accountRegistration.ref(element); accountSelectRef.current = element; }}>
            <option value="">{t("react.auth.choose_an_account", "Choose an account")}</option>
            {accountOptions.map((option) => <option key={option.account_id} value={option.account_id}>{option.account_name || option.display_name || option.account_id}</option>)}
          </select>
          <ChevronDown aria-hidden="true" data-icon size={14} strokeWidth={2} />
        </span>
        {error ? <span className="fs-public-auth__field-helper fs-public-auth__field-helper--error" id="account-error">{error.message}</span> : <span className="fs-public-auth__field-helper" id="account-helper">{t("react.auth.choose_the_workspace_account_you_want_to_enter", "Choose the workspace account you want to enter.")}</span>}
      </motion.div>
    </AnimatePresence>
  );
}

function LoginSubmitArea({ activeNotice, label, submitting }: Readonly<{ activeNotice: AuthNotice | null; label: string; submitting: boolean }>) {
  const { t } = useI18n();
  return (
    <>
      <AnimatePresence initial={false}>{activeNotice ? <Notice key={`${activeNotice.tone}-${activeNotice.title}`} notice={activeNotice} /> : null}</AnimatePresence>
      <MotionButton {...AUTH_BUTTON_MOTION} aria-label={submitting ? t("react.auth.signing_in", "Signing in…") : undefined} aria-busy={submitting} className="fs-public-auth__button fs-public-auth__sign-in-button" data-loading={submitting ? "true" : undefined} disabled={submitting} id="login-btn" size="lg" type="submit">
        <AnimatePresence initial={false}>{submitting ? <motion.span animate={{ opacity: 1 }} aria-hidden="true" className="fs-public-auth__button-spinner" data-testid="auth-submit-spinner" exit={{ opacity: 0 }} initial={{ opacity: 0 }} transition={AUTH_FEEDBACK_TRANSITION} /> : null}</AnimatePresence>
        <span className="fs-public-auth__button-label">{label}</span>
        <span className="fs-public-auth__reduced-loading-label">{t("react.auth.signing_in", "Signing in…")}</span>
      </MotionButton>
    </>
  );
}

function LocalLoginForm({
  accountOptions,
  accountRegistration,
  accountSelectRef,
  activeNotice,
  enabled,
  forgotPasswordAvailable,
  form,
  onSubmit,
  passwordChange,
  passwordVisible,
  rememberMe,
  setPasswordVisible,
}: Readonly<{
  accountOptions: AccountOption[];
  accountRegistration: UseFormRegisterReturn<"accountId">;
  accountSelectRef: RefObject<HTMLSelectElement | null>;
  activeNotice: AuthNotice | null;
  enabled: boolean;
  forgotPasswordAvailable: boolean;
  form: UseFormReturn<LoginFormValues>;
  onSubmit: (values: LoginFormValues) => Promise<void>;
  passwordChange: PasswordChange | null;
  passwordVisible: boolean;
  rememberMe: boolean;
  setPasswordVisible: (updater: (visible: boolean) => boolean) => void;
}>) {
  const { t } = useI18n();
  const [newPasswordVisible, setNewPasswordVisible] = useState(false);
  const [confirmationVisible, setConfirmationVisible] = useState(false);
  if (!enabled) return null;
  return (
    <form className="fs-public-auth__form" noValidate onSubmit={(event) => { ignoreAsyncResult(form.handleSubmit(onSubmit)(event)); }}>
      <LoginEmailField form={form} />
      <LoginPasswordField forgotPasswordAvailable={forgotPasswordAvailable} form={form} label={passwordChange ? t("react.auth.temporary_password", "Temporary password") : undefined} passwordVisible={passwordVisible} setPasswordVisible={setPasswordVisible} />
      {passwordChange ? (
        <>
          <NewPasswordField form={form} helper={t("react.auth.use_at_least_count_characters", "Use at least {count} characters.", { count: passwordChange.minLength })} name="newPassword" passwordVisible={newPasswordVisible} setPasswordVisible={setNewPasswordVisible} />
          <ConfirmPasswordField confirmationVisible={confirmationVisible} form={form} name="confirmPassword" setConfirmationVisible={setConfirmationVisible} />
        </>
      ) : null}
      <LoginAccountField accountOptions={accountOptions} accountRegistration={accountRegistration} accountSelectRef={accountSelectRef} form={form} />
      <label className="fs-public-auth__checkbox-label">
        <input {...form.register("rememberMe")} type="checkbox" />
        <motion.span animate={{ transform: rememberMe ? "scale(1)" : "scale(0.94)" }} aria-hidden="true" className="fs-public-auth__checkbox-mark" transition={{ bounce: 0, type: "spring", visualDuration: 0.16 }}>
          <motion.span animate={{ opacity: rememberMe ? 1 : 0, transform: rememberMe ? "scale(1)" : "scale(0.8)" }} className="fs-public-auth__checkbox-glyph" transition={AUTH_FEEDBACK_TRANSITION}><Check data-icon size={12} strokeWidth={2} /></motion.span>
        </motion.span>
        <span>{t("react.auth.keep_me_signed_in", "Keep me signed in")}</span>
      </label>
      <LoginSubmitArea activeNotice={activeNotice} label={passwordChange ? t("react.auth.set_password_and_sign_in", "Set password and sign in") : t("auth.login.heading", "Sign in")} submitting={form.formState.isSubmitting} />
    </form>
  );
}

function PasswordUnavailableNotice({
  externalUnavailable,
  localAuthEnabled,
  providersVisible,
}: Readonly<{ externalUnavailable: boolean; localAuthEnabled: boolean; providersVisible: boolean }>) {
  const { t } = useI18n();
  if (localAuthEnabled || (externalUnavailable && providersVisible)) return null;
  return <Notice notice={{ copy: providersVisible ? t("react.auth.use_a_configured_external_provider_to_continue", "Use a configured external provider to continue.") : t("react.auth.no_external_sign_in_flow_is_configured_contact_your_workspace_administrator", "No external sign-in flow is configured. Contact your workspace administrator."), title: t("react.auth.password_sign_in_is_unavailable", "Password sign-in is unavailable."), tone: "neutral" }} role="status" />;
}

export function LoginPage() {
  const { t } = useI18n();
  const auth = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const rawNextPath = searchParams.get("next");
  const nextPath = normalizeNextPath(rawNextPath);
  const [accountOptions, setAccountOptions] = useState<AccountOption[]>([]);
  const [notice, setNotice] = useState<AuthNotice | null>(null);
  const [passwordChange, setPasswordChange] = useState<PasswordChange | null>(null);
  const [flashDismissed, setFlashDismissed] = useState(false);
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [externalSignIn, setExternalSignIn] = useState<ExternalSignInState>({
    providers: [],
    requestedPath: "",
    status: "idle",
  });
  const accountSelectRef = useRef<HTMLSelectElement | null>(null);
  const flashMessage = flashDismissed ? "" : normalizeFlashMessage(searchParams.get("flash"));
  const localAuthEnabled = auth.session ? auth.session.local_auth_enabled : true;
  const externalSignInProvidersPath = firstStringFromUnknownValue([
    auth.session?.external_sign_in?.providers_path,
    "",
  ]).trim();
  const shouldLoadExternalSignIn = Boolean(externalSignInProvidersPath)
    && !auth.isAuthenticated
    && !auth.session?.bootstrap_required;

  const form = useForm<LoginFormValues>({
    defaultValues: {
      email: "",
      password: "",
      rememberMe: true,
      accountId: "",
      newPassword: "",
      confirmPassword: "",
    },
    resolver: zodResolver(getLoginschema(t)),
  });
  const accountRegistration = form.register("accountId");
  const rememberMe = useWatch({ control: form.control, name: "rememberMe" });

  useEffect(() => {
    let active = true;
    if (!shouldLoadExternalSignIn) {
      return () => {
        active = false;
      };
    }

    fetchJson<ExternalSignInProvider[]>(externalSignInProvidersPath)
      .then((providers) => {
        if (!active) return;
        const validProviders = (Array.isArray(providers) ? providers : [])
          .map((provider) => ({
            provider_key: firstStringFromUnknownValue([provider?.provider_key, ""]).trim(),
            display_name: firstStringFromUnknownValue([
              provider?.display_name,
              provider?.provider_key,
              "",
            ]).trim(),
            login_path: firstStringFromUnknownValue([provider?.login_path, ""]).trim(),
          }))
          .filter((provider) => (
            provider.provider_key
            && provider.display_name
            && provider.login_path
            && isSafeLocalPath(provider.login_path)
          ));
        setExternalSignIn({
          providers: validProviders,
          requestedPath: externalSignInProvidersPath,
          status: validProviders.length > 0 ? "ready" : "unavailable",
        });
      })
      .catch(() => {
        if (active) {
          setExternalSignIn({
            providers: [],
            requestedPath: externalSignInProvidersPath,
            status: "unavailable",
          });
        }
      });

    return () => {
      active = false;
    };
  }, [externalSignInProvidersPath, shouldLoadExternalSignIn]);

  if (auth.isAuthenticated) {
    return <Navigate replace to={nextPath} />;
  }

  if (auth.session?.bootstrap_required) {
    return <Navigate replace to="/bootstrap" />;
  }

  function newPasswordReady(values: LoginFormValues, change: PasswordChange): boolean {
    if (values.newPassword.length < change.minLength) {
      form.setError("newPassword", {
        message: t("react.auth.use_at_least_count_characters", "Use at least {count} characters.", { count: change.minLength }),
        type: "minLength",
      });
      return false;
    }
    if (values.newPassword === values.password) {
      form.setError("newPassword", {
        message: t("react.auth.choose_a_password_different_from_the_temporary_one", "Choose a password different from the temporary one."),
        type: "validate",
      });
      return false;
    }
    if (values.confirmPassword !== values.newPassword) {
      form.setError("confirmPassword", {
        message: t("auth.common.passwords_do_not_match", "Passwords do not match."),
        type: "validate",
      });
      return false;
    }
    return true;
  }

  async function onSubmit(values: LoginFormValues) {
    setFlashDismissed(true);
    setNotice(null);

    if (accountOptions.length > 0 && !values.accountId) {
      form.setError("accountId", {
        message: t("react.auth.choose_an_account_to_continue", "Choose an account to continue."),
        type: "required",
      });
      setNotice({
        copy: t("react.auth.select_the_workspace_account_you_want_to_enter", "Select the workspace account you want to enter."),
        title: t("react.auth.choose_an_account_2", "Choose an account."),
        tone: "warning",
      });
      accountSelectRef.current?.focus();
      return;
    }
    if (passwordChange && !newPasswordReady(values, passwordChange)) return;

    try {
      await fetchJson<LoginResponse>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({
          email: values.email,
          password: values.password,
          remember_me: values.rememberMe,
          account_id: values.accountId || undefined,
          new_password: passwordChange ? values.newPassword : undefined,
        }),
      });
      await auth.refresh();
      ignoreAsyncResult(navigate(nextPath, { replace: true }));
    } catch (error) {
      const accountSelection = accountSelectionFromLoginError(error);
      if (accountSelection.required) {
        if (!accountSelection.options.length) {
          setNotice({
            copy: t("react.auth.try_again_or_contact_your_workspace_administrator", "Try again or contact your workspace administrator."),
            title: t("react.auth.account_selection_is_temporarily_unavailable", "Account selection is temporarily unavailable."),
            tone: "danger",
          });
          return;
        }
        setAccountOptions(accountSelection.options);
        setNotice({
          copy: t("react.auth.select_the_workspace_account_you_want_to_enter", "Select the workspace account you want to enter."),
          title: t("react.auth.choose_an_account_2", "Choose an account."),
          tone: "info",
        });
        requestAnimationFrame(() => accountSelectRef.current?.focus());
        return;
      }

      const passwordChangeRequired = passwordChangeFromLoginError(error);
      if (passwordChangeRequired) {
        setPasswordChange(passwordChangeRequired);
        setNotice({
          copy: t("react.auth.your_temporary_password_worked_choose_a_new_password_to_finish_signing_in", "Your temporary password worked. Choose a new password to finish signing in."),
          title: t("react.auth.set_a_new_password", "Set a new password"),
          tone: "info",
        });
        requestAnimationFrame(() => form.setFocus("newPassword"));
        return;
      }
      const newPasswordError = newPasswordErrorFromLoginError(error);
      if (newPasswordError) {
        form.setError("newPassword", { message: newPasswordError, type: "server" });
        return;
      }

      setNotice(loginErrorNotice(error, t));
    }
  }

  const activeNotice = notice ?? (flashMessage
    ? {
        copy: flashMessage,
        title: t("react.auth.sign_in_update", "Sign-in update"),
        tone: "info" as const,
      }
    : null);
  const providersVisible = shouldLoadExternalSignIn;
  const externalSignInStatus = providersVisible
    && externalSignIn.requestedPath !== externalSignInProvidersPath
    ? "loading"
    : externalSignIn.status;
  const showPasswordDivider = providersVisible && localAuthEnabled;
  return (
    <>
      <a className="fs-public-auth__skip-link" href="#public-auth-sign-in">
        {t("react.auth.skip_to_account_access", "Skip to account access")} </a>
      <main className="fs-public-auth" id="public-auth-main">
        <section
          aria-labelledby="public-auth-sign-in-title"
          className="fs-public-auth__sign-in-panel"
          id="public-auth-sign-in"
        >
          <motion.div
            className="fs-public-auth__sign-in-column"
            layout
            style={{
              borderRadius: "var(--fs-radius-large)",
              boxShadow: "0 20px 48px -30px rgb(0 5 9 / 30%)",
            }}
            transition={{ layout: AUTH_LAYOUT_TRANSITION }}
          >
            <AuthContentPresence
              className="fs-public-auth__sign-in-content"
              stateKey="form"
            >
              <>
                <header className="fs-public-auth__sign-in-header">
                  <h2 id="public-auth-sign-in-title">{passwordChange ? t("react.auth.set_a_new_password", "Set a new password") : t("auth.login.heading", "Sign in")}</h2>
                  <p>{passwordChange ? t("react.auth.your_administrator_set_a_temporary_password_replace_it_with_your_own_to_continue", "Your administrator set a temporary password. Replace it with your own to continue.") : t("react.auth.welcome_back_enter_your_details_to_continue", "Welcome back — enter your details to continue.")}</p>
                </header>

                <ExternalProviderSection
                  localAuthEnabled={localAuthEnabled}
                  nextPath={nextPath}
                  providers={externalSignIn.providers}
                  status={externalSignInStatus}
                  visible={providersVisible}
                />

                {showPasswordDivider ? (
                  <div className="fs-public-auth__divider"><span>{t("react.auth.or_use_your_password", "or use your password")}</span></div>
                ) : null}

                <LocalLoginForm
                  accountOptions={accountOptions}
                  accountRegistration={accountRegistration}
                  accountSelectRef={accountSelectRef}
                  activeNotice={activeNotice}
                  enabled={localAuthEnabled}
                  forgotPasswordAvailable={Boolean(auth.session?.system_email_available)}
                  form={form}
                  onSubmit={onSubmit}
                  passwordChange={passwordChange}
                  passwordVisible={passwordVisible}
                  rememberMe={rememberMe}
                  setPasswordVisible={setPasswordVisible}
                />
                <PasswordUnavailableNotice
                  externalUnavailable={externalSignInStatus === "unavailable"}
                  localAuthEnabled={localAuthEnabled}
                  providersVisible={providersVisible}
                />
              </>
            </AuthContentPresence>
          </motion.div>
        </section>
      </main>
    </>
  );
}
