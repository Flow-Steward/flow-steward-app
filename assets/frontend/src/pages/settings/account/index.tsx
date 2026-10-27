import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { createElement, useEffect, useMemo, useRef, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router";

import {
  registeredAccountProfileTabs,
  resolveRegisteredPageComponent,
} from "@/app/extensions";
import type { AccountProfileTabExtension } from "@/app/extensions";
import type { RegisteredPageRecord } from "@/lib/auth/AuthProvider";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { useAuth } from "@/lib/auth/useAuth";
import { useI18n } from "@/lib/i18n/useI18n";
import { addDays, formatCalendarDay, todayIn, wholeDaysBetween } from "@/lib/time/calendar-days";
import { useUserTimezone } from "@/lib/time/user-timezone";
import { ignoreAsyncResult } from "@/lib/utils/async";

function profileSaveStatus(saving: boolean, dirtyCount: number, t: I18nContextValue["t"] = fallbackContext.t): { className: string; label: string } {
  if (saving) return { className: "is-saving", label: t("react.settings.saving_changes", "Saving changes…") };
  if (dirtyCount) {
    return {
      className: "is-dirty",
      label: dirtyCount === 1
        ? t("react.settings.one_unsaved_change", "1 unsaved change")
        : t("react.settings.unsaved_changes_count", "Unsaved changes: {count}", { count: dirtyCount }),
    };
  }
  return { className: "is-saved", label: t("react.settings.no_unsaved_changes", "No unsaved changes") };
}

function pastExpiryError(date: string, today: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (wholeDaysBetween(today, date) >= 1) return "";
  return t("react.settings.value1_is_not_in_the_future_choose_value2_or_later", "{value1} is not in the future. Choose {value2} or later.", { value1: String(formatCalendarDay(date)), value2: String(formatCalendarDay(addDays(today, 1))) });
}

function expiryLabel(value: ExpiryChoice, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (value === "none") return t("account.tokens.no_expiry", "No expiry");
  if (value === "custom") return t("project_detail.schedule.custom", "Custom");
  return t("react.settings.value1_days", "{value1} days", { value1: String(value) });
}
import { fetchProjectsList } from "@/pages/projects/list/api";
import {
  changeAccountPassword,
  createPersonalToken,
  fetchAccountProfileRecord,
  fetchPersonalTokensCollection,
  requestAccountEmailChange,
  revokePersonalToken,
  saveAccountProfileRecord,
} from "@/pages/settings/shared/api";
import {
  DsAlert,
  DsCard,
  DsCheckbox,
  DsDialog,
  DsRadio,
  DsSelect,
  DsToast,
  MultiCombobox,
  Pagination,
  RequiredLabel,
  RowActionMenu,
  SensitiveInput,
  SingleCombobox,
  DsPageHeader,
} from "@/components/shared/design-system";
import type { DsOption } from "@/components/shared/design-system";
import {
  TOKEN_SCOPES,
  type AccountProfile,
  type CreatePersonalTokenInput,
  type PersonalToken,
  type ProfileField,
  type TokenScope,
} from "@/pages/settings/shared/types";
import {
  extractOneTimeTokenLabel,
  extractOneTimeTokenSecret,
} from "@/pages/settings/shared/one-time-token-secret-utils";
import { fetchWorkflows } from "@/pages/workflows/shared/api";

type AccountTabRecord = { key: string; label: string };
type ExpiryChoice = "none" | "30" | "90" | "custom";



function getBuiltInAccountTabs(t: I18nContextValue["t"] = fallbackContext.t): AccountTabRecord[] {
  return [
  { key: "profile", label: t("account.profile.title", "Profile") },
  { key: "signin", label: t("react.settings.sign_in", "Sign-in") },
  { key: "tokens", label: t("react.settings.personal_tokens", "Personal tokens") },
];
}
const EXTENSION_ACCOUNT_TABS = registeredAccountProfileTabs.filter((tab) => Boolean(resolveRegisteredPageComponent(tab.componentKey)));


function getAccountTabs(t: I18nContextValue["t"] = fallbackContext.t) {
  return [...getBuiltInAccountTabs(t), ...EXTENSION_ACCOUNT_TABS.map((tab) => ({ key: tab.key, label: tab.label }))];
}
const FALLBACK_TIMEZONES = ["UTC", "Europe/London", "Europe/Paris", "Europe/Vilnius", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "Asia/Tokyo", "Asia/Singapore", "Australia/Sydney"];
const EMPTY_PROFILE: AccountProfile = { id: "", email: "", username: "", display_name: "", roles: [], account_id: "", account_ids: [], timezone: "", locale: "", auth_source: "" };

function tabPageRecord(tab: AccountProfileTabExtension): RegisteredPageRecord {
  return { key: `account_${tab.key}`, page_key: `account_${tab.key}`, label: tab.label, title: tab.title || tab.label, path: `/settings/account?tab=${encodeURIComponent(tab.key)}`, component_key: tab.componentKey };
}

function initials(value: string) {
  return value.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "U";
}

function formatDate(value?: string, t: I18nContextValue["t"] = fallbackContext.t) {
  if (!value) return t("account.tokens.never", "Never");
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString();
}

function timezoneOptions(current: string): DsOption[] {
  const withSupportedValues = Intl as typeof Intl & { supportedValuesOf?: (key: "timeZone") => string[] };
  const values = typeof withSupportedValues.supportedValuesOf === "function" ? withSupportedValues.supportedValuesOf("timeZone") : FALLBACK_TIMEZONES;
  return Array.from(new Set([current, ...values, ...FALLBACK_TIMEZONES])).filter(Boolean).sort((left, right) => left.localeCompare(right)).map((value) => ({ value, label: value }));
}

function localeOptions(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): DsOption[] {
  if (!Array.isArray(value)) return [{ value: "en", label: t("react.settings.english", "English") }];
  return value.flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const row = entry as { code?: string; label?: string; native_label?: string };
    const code = String(row.code || "").split("-")[0]?.toLowerCase();
    if (!code) return [];
    const label = row.native_label && row.label && row.native_label !== row.label ? `${row.native_label} (${row.label})` : row.native_label || row.label || code;
    return [{ value: code, label }];
  });
}

function tokenStatus(token: PersonalToken, t: I18nContextValue["t"] = fallbackContext.t) {
  if (token.revoked_at || token.is_active === false) return t("react.settings.revoked", "Revoked");
  if (token.expires_at && new Date(token.expires_at).getTime() < Date.now()) return t("react.settings.expired", "Expired");
  return t("account_settings.status.active", "Active");
}

function nextAccountTabIndex(key: string, index: number, t: I18nContextValue["t"] = fallbackContext.t): number {
  if (key === "Home") {
    return 0;
  }
  if (key === "End") {
    return getAccountTabs(t).length - 1;
  }
  const direction = key === "ArrowRight" ? 1 : -1;
  return (index + direction + getAccountTabs(t).length) % getAccountTabs(t).length;
}

export function SettingsAccountPage() {
  const { t } = useI18n();
  const auth = useAuth();
  const isAdmin = useIsAdmin();
  const { locale: currentLocale, supportedLocales } = useI18n();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") || "profile";
  const activeTab = getAccountTabs(t).some((tab) => tab.key === requestedTab) ? requestedTab : "profile";

  const profileQuery = useQuery({ queryKey: ["settings", "account", "profile"], queryFn: fetchAccountProfileRecord });
  const profile = profileQuery.data?.profile || EMPTY_PROFILE;
  const localeChoices = useMemo(() => localeOptions(supportedLocales, t), [supportedLocales, t]);
  const emailChangeStatus = profileQuery.data?.email_change_status;
  const pendingEmailExpiry = emailChangeStatus?.expires_at
    ? t("react.settings.expires_value1", " · expires {value1}", { value1: String(formatDate(emailChangeStatus.expires_at, t)) })
    : "";
  const pendingEmailSummary = emailChangeStatus?.new_email
    ? t("react.settings.pending_value1_value2", "Pending · {value1}{value2}", { value1: String(emailChangeStatus.new_email), value2: String(pendingEmailExpiry) })
    : t("react.settings.no_pending_email_change", "No pending email change");

  const [saved, setSaved] = useState(EMPTY_PROFILE);
  const [draft, setDraft] = useState(EMPTY_PROFILE);
  const [profileMessage, setProfileMessage] = useState<string | { savedFieldCount: number; failureSummary: string }>("");
  const [profileFeedbackKind, setProfileFeedbackKind] = useState<"success" | "partial" | "error">("success");
  const profileFeedbackMessage = typeof profileMessage === "string"
    ? profileMessage
    : profileMessage.failureSummary
      ? (profileMessage.savedFieldCount === 1
        ? t("react.settings.profile_partial_one_field", "Saved 1 field. {failures}", { failures: profileMessage.failureSummary })
        : t("react.settings.profile_partial_fields", "Saved {count} fields. {failures}", { count: profileMessage.savedFieldCount, failures: profileMessage.failureSummary }))
      : t("react.settings.profile_changes_saved", "Profile changes saved.");
  const [savingProfile, setSavingProfile] = useState(false);
  const [pendingTab, setPendingTab] = useState("");
  const [discardOpen, setDiscardOpen] = useState(false);
  const dirtyFields = useMemo(() => (["display_name", "timezone", "locale"] as ProfileField[]).filter((field) => draft[field] !== saved[field]), [draft, saved]);

  // The fallback language only fills a profile that has none; a change of the
  // interface language (which saving the language itself causes) must not
  // re-hydrate the form from the profile it was loaded with.
  const localeFallbackRef = useRef("en");
  localeFallbackRef.current = currentLocale || localeChoices[0]?.value || "en";
  const savedRef = useRef(saved);
  savedRef.current = saved;

  useEffect(() => {
    if (!profileQuery.data) return;
    const normalized = {
      ...profileQuery.data.profile,
      timezone: profileQuery.data.profile.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      locale: profileQuery.data.profile.locale || localeFallbackRef.current,
    };
    const previousSaved = savedRef.current;
    setSaved(normalized);
    // A field the user is still editing stays as typed.
    setDraft((previous) => {
      const next = { ...normalized };
      for (const field of ["display_name", "timezone", "locale"] as ProfileField[]) {
        if (previous[field] !== previousSaved[field]) next[field] = previous[field];
      }
      return next;
    });
  }, [profileQuery.data]);

  useEffect(() => {
    const confirmNavigation = (event: BeforeUnloadEvent) => {
      if (!dirtyFields.length) return;
      event.preventDefault();
    };
    window.addEventListener("beforeunload", confirmNavigation);
    return () => window.removeEventListener("beforeunload", confirmNavigation);
  }, [dirtyFields.length]);

  const [newEmail, setNewEmail] = useState("");
  const [emailMessage, setEmailMessage] = useState("");
  const [emailRequestSucceeded, setEmailRequestSucceeded] = useState(false);
  const [emailSaving, setEmailSaving] = useState(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [revokeOtherSessions, setRevokeOtherSessions] = useState(true);
  const [passwordMessage, setPasswordMessage] = useState("");
  const [passwordChangeSucceeded, setPasswordChangeSucceeded] = useState(false);
  const [passwordSaving, setPasswordSaving] = useState(false);
  const canEditSignin = (profile.auth_source || auth.session?.auth_source) === "local";

  const [tokenPage, setTokenPage] = useState(1);
  const [tokenPerPage, setTokenPerPage] = useState(5);
  const tokensQuery = useQuery({ queryKey: ["settings", "account", "tokens", tokenPage, tokenPerPage], queryFn: () => fetchPersonalTokensCollection(tokenPage, tokenPerPage), placeholderData: (previous) => previous });
  const [createTokenOpen, setCreateTokenOpen] = useState(false);
  const [tokenLabel, setTokenLabel] = useState("");
  const [expiry, setExpiry] = useState<ExpiryChoice>("30");
  const [customExpiry, setCustomExpiry] = useState("");
  const [customExpiryError, setCustomExpiryError] = useState("");
  const userTimezone = useUserTimezone();
  const customExpiryRef = useRef<HTMLInputElement>(null);
  const [scopes, setScopes] = useState<TokenScope[]>(["jobs.read", "projects.read", "workflows.read"]);
  const [administratorAccess, setAdministratorAccess] = useState(false);
  useEffect(() => {
    if (!createTokenOpen || !isAdmin) setAdministratorAccess(false);
  }, [createTokenOpen, isAdmin]);
  const [projectIds, setProjectIds] = useState<string[]>([]);
  const [workflowIds, setWorkflowIds] = useState<string[]>([]);
  const [tokenError, setTokenError] = useState("");
  const [tokenSubmitting, setTokenSubmitting] = useState(false);
  const [secret, setSecret] = useState("");
  const [secretLabel, setSecretLabel] = useState("");
  const [copyFeedback, setCopyFeedback] = useState<{ tone: "success" | "danger"; message: string } | null>(null);
  const [menuToken, setMenuToken] = useState<PersonalToken | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<PersonalToken | null>(null);
  const [revoking, setRevoking] = useState(false);

  const projectsQuery = useQuery({ queryKey: ["settings", "account", "token-projects"], queryFn: () => fetchProjectsList({ page: 1, perPage: 200, search: "" }), enabled: createTokenOpen });
  const workflowsQuery = useQuery({ queryKey: ["settings", "account", "token-workflows"], queryFn: () => fetchWorkflows({ enabledOnly: false, search: "" }), enabled: createTokenOpen });
  const projectOptions = (projectsQuery.data?.projects || []).map((project) => ({ value: String(project.id), label: project.name?.trim() || t("react.settings.unnamed_project", "Unnamed project") }));
  const workflowOptions = (workflowsQuery.data || []).map((workflow) => ({ value: workflow.workflow_id, label: workflow.display_name?.trim() || t("react.settings.unnamed_workflow", "Unnamed workflow") }));

  useEffect(() => {
    if (!copyFeedback) return;
    const timer = window.setTimeout(() => setCopyFeedback(null), 2400);
    return () => window.clearTimeout(timer);
  }, [copyFeedback]);

  function goToTab(tab: string, force = false) {
    if (!force && dirtyFields.length && tab !== activeTab) {
      setPendingTab(tab);
      setDiscardOpen(true);
      return;
    }
    const next = new URLSearchParams(searchParams);
    if (tab === "profile") next.delete("tab"); else next.set("tab", tab);
    setSearchParams(next, { replace: true });
  }

  async function saveProfile(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setProfileFeedbackKind("error");
    if (!draft.display_name.trim()) { setProfileMessage(t("react.settings.display_name_is_required", "Display name is required.")); return; }
    const changes = Object.fromEntries(dirtyFields.map((field) => [field, draft[field]]));
    setSavingProfile(true);
    setProfileMessage("");
    try {
      const result = await saveAccountProfileRecord(changes, t);
      const savedValues = Object.fromEntries(result.succeeded.map((field) => [field, draft[field]]));
      setSaved((previous) => ({ ...previous, ...savedValues }));
      savedRef.current = { ...savedRef.current, ...savedValues };
      queryClient.setQueryData<typeof profileQuery.data>(
        ["settings", "account", "profile"],
        (previous) => (previous ? { ...previous, profile: { ...previous.profile, ...savedValues } } : previous),
      );
      await auth.refresh();
      const failed = Object.entries(result.failed);
      const failureSummary = failed.map(([field, message]) => `${field}: ${message}`).join(" ");
      setProfileFeedbackKind(failed.length ? "partial" : "success");
      setProfileMessage({ savedFieldCount: result.succeeded.length, failureSummary });
    } catch (error) {
      setProfileFeedbackKind("error");
      setProfileMessage(error instanceof Error ? error.message : t("react.settings.profile_could_not_be_saved", "Profile could not be saved."));
    } finally { setSavingProfile(false); }
  }

  async function requestEmail(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setEmailRequestSucceeded(false);
    if (!newEmail.trim() || !newEmail.includes("@")) { setEmailMessage(t("react.settings.enter_a_valid_new_email_address", "Enter a valid new email address.")); return; }
    setEmailSaving(true); setEmailMessage("");
    try { await requestAccountEmailChange(newEmail.trim()); setNewEmail(""); setEmailMessage(t("react.settings.verification_email_sent_confirm_the_link_before_it_expires", "Verification email sent. Confirm the link before it expires.")); setEmailRequestSucceeded(true); await profileQuery.refetch(); }
    catch (error) { setEmailRequestSucceeded(false); setEmailMessage(error instanceof Error ? error.message : t("react.settings.email_change_could_not_be_requested", "Email change could not be requested.")); }
    finally { setEmailSaving(false); }
  }

  async function changePassword(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setPasswordChangeSucceeded(false);
    if (!currentPassword || newPassword.length < 10 || newPassword !== confirmPassword) { setPasswordMessage(t("react.settings.enter_the_current_password_and_matching_new_passwords_of_at_least_10_characters", "Enter the current password and matching new passwords of at least 10 characters.")); return; }
    setPasswordSaving(true); setPasswordMessage("");
    try { await changeAccountPassword({ current_password: currentPassword, new_password: newPassword, revoke_other_sessions: revokeOtherSessions }); setCurrentPassword(""); setNewPassword(""); setConfirmPassword(""); setPasswordMessage(t("react.auth.password_updated", "Password updated.")); setPasswordChangeSucceeded(true); }
    catch (error) { setPasswordMessage(error instanceof Error ? error.message : t("react.settings.password_could_not_be_changed", "Password could not be changed.")); }
    finally { setPasswordSaving(false); }
  }

  // "Today" is the user's configured zone, never the browser clock, and a custom
  // date converts at submit so a form left open past midnight stays exact.
  function expiryDays() {
    if (expiry === "none") return undefined;
    if (expiry !== "custom") return Number(expiry);
    return wholeDaysBetween(todayIn(userTimezone), customExpiry);
  }

  async function createToken(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!tokenLabel.trim()) { setTokenError(t("react.settings.label_is_required", "Label is required.")); return; }
    const requestedScopes: CreatePersonalTokenInput["scopes"] = isAdmin && administratorAccess
      ? [...scopes, "admin:all"]
      : scopes;
    if (!requestedScopes.length) { setTokenError(t("react.settings.select_at_least_one_scope", "Select at least one scope.")); return; }
    if (expiry === "custom") {
      const error = customExpiry ? pastExpiryError(customExpiry, todayIn(userTimezone), t) : t("react.settings.choose_the_date_the_token_expires", "Choose the date the token expires.");
      if (error) { setCustomExpiryError(error); customExpiryRef.current?.focus(); return; }
    }
    const payload: CreatePersonalTokenInput = { label: tokenLabel.trim(), expires_in_days: expiryDays(), scopes: requestedScopes, allowed_project_ids: projectIds, allowed_workflow_ids: workflowIds };
    setTokenSubmitting(true); setTokenError("");
    try {
      const response = await createPersonalToken(payload) as Record<string, unknown>;
      const oneTimeSecret = extractOneTimeTokenSecret(response);
      setSecret(oneTimeSecret); setSecretLabel(extractOneTimeTokenLabel(response, tokenLabel.trim(), t));
      setCreateTokenOpen(false); setTokenLabel(""); setProjectIds([]); setWorkflowIds([]);
      await queryClient.invalidateQueries({ queryKey: ["settings", "account", "tokens"] });
    } catch (error) { setTokenError(error instanceof Error ? error.message : t("react.settings.token_could_not_be_created", "Token could not be created.")); }
    finally { setTokenSubmitting(false); }
  }

  async function confirmRevoke() {
    if (!revokeTarget) return;
    setRevoking(true);
    try { await revokePersonalToken(revokeTarget.id); setRevokeTarget(null); await queryClient.invalidateQueries({ queryKey: ["settings", "account", "tokens"] }); }
    finally { setRevoking(false); }
  }

  function dismissSecret() {
    setSecret("");
    setSecretLabel("");
    setCopyFeedback(null);
  }

  async function copySecret() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
      await navigator.clipboard.writeText(secret);
      setCopyFeedback({ tone: "success", message: t("react.settings.token_copied_to_clipboard", "Token copied to clipboard.") });
    } catch {
      setCopyFeedback({ tone: "danger", message: t("react.settings.token_could_not_be_copied_select_the_value_and_copy_it_manually", "Token could not be copied. Select the value and copy it manually.") });
    }
  }

  function renderProfilePanel() {
    const saveStatus = profileSaveStatus(savingProfile, dirtyFields.length, t);
    return (
    <form className="fs-panel-stack" noValidate onSubmit={(event) => ignoreAsyncResult(saveProfile(event))}>
      <DsCard title={t("account.profile.title", "Profile")} description={t("react.settings.your_identity_and_preferences_across_flow_steward", "Your identity and preferences across Flow Steward.")} footer={<><span className={`fs-footer-status ${saveStatus.className}`}>{saveStatus.label}</span><button className="fs-button primary" disabled={!dirtyFields.length || savingProfile} type="submit">{savingProfile ? t("react.settings.saving", "Saving…") : t("react.settings.save_changes", "Save changes")}</button></>}>
        <div className="fs-identity"><span className="fs-avatar" aria-hidden="true">{initials(draft.display_name || draft.email)}</span><div className="fs-identity-copy"><div className="fs-identity-name">{draft.display_name || t("react.settings.your_profile", "Your profile")}</div><div className="fs-identity-email">{draft.email || "—"}</div></div><div className="fs-identity-context"><div className="fs-badge-row">{draft.roles.map((role, index) => <span className={`fs-badge${index === 0 ? " green" : ""}`} key={role}>{role}</span>)}</div></div></div>
        <div className="fs-form-grid">
          <label className="fs-field full"><RequiredLabel>{t("react.settings.display_name", "Display name")}</RequiredLabel><input autoComplete="name" className="fs-input" id="account-display-name" name="display-name" onChange={(event) => setDraft((value) => ({ ...value, display_name: event.target.value }))} required value={draft.display_name} /></label>
          <div className="fs-field"><span className="fs-field-label">{t("account.timezone.title", "Timezone")}</span><SingleCombobox ariaLabel={t("account.timezone.title", "Timezone")} onChange={(timezone) => setDraft((value) => ({ ...value, timezone }))} options={timezoneOptions(draft.timezone)} value={draft.timezone} /><span className="fs-field-helper">{t("react.settings.used_for_dates_schedules_and_activity_timestamps", "Used for dates, schedules and activity timestamps.")}</span></div>
          <div className="fs-field"><span className="fs-field-label">{t("react.settings.interface_language", "Interface language")}</span><DsSelect ariaLabel={t("react.settings.interface_language", "Interface language")} onChange={(locale) => setDraft((value) => ({ ...value, locale }))} options={localeChoices} value={draft.locale} /><span className="fs-field-helper">{t("react.settings.applies_after_the_profile_refresh_completes", "Applies after the profile refresh completes.")}</span></div>
        </div>
        {profileFeedbackMessage ? <DsAlert tone={profileFeedbackKind === "success" ? "success" : "warning"} title={profileFeedbackKind === "error" ? t("react.settings.profile_was_not_saved", "Profile was not saved") : t("react.settings.profile_update", "Profile update")}>{profileFeedbackMessage}</DsAlert> : null}
      </DsCard>
    </form>
    );
  }

  function renderSigninPanel() {
    if (!canEditSignin) {
      return (
        <DsCard title={t("react.settings.sign_in_is_managed_by_your_provider", "Sign-in is managed by your provider")} description={t("react.settings.local_email_and_password_changes_are_not_available_for_this_account", "Local email and password changes are not available for this account.")}>
          <DsAlert title={t("react.settings.provider_managed_account", "Provider-managed account")}>
            {t("react.settings.use_your_organization_s_identity_provider_to_change_your_email_password_or_sessions", "Use your organization’s identity provider to change your email, password or sessions.")} </DsAlert>
        </DsCard>
      );
    }
    const emailChangeAvailable = Boolean(auth.session?.system_email_available);
    return (
      <div className="fs-panel-stack">
      {emailChangeAvailable ? (
      <DsCard title={t("react.settings.email_address", "Email address")} description={t("react.settings.update_the_address_used_to_sign_in_after_verification", "Update the address used to sign in after verification.")} footer={<><span className="fs-footer-status">{pendingEmailSummary}</span><button className="fs-button primary" disabled={emailSaving} form="email-change-form" type="submit">{emailSaving ? t("react.settings.sending", "Sending…") : t("react.settings.send_verification", "Send verification")}</button></>}>
        <div className="fs-current-value"><span><span className="fs-current-value-label">{t("react.settings.current_email", "Current email")}</span><br /><strong>{profile.email}</strong></span></div>
        <form className="fs-field" id="email-change-form" noValidate onSubmit={(event) => ignoreAsyncResult(requestEmail(event))}><RequiredLabel>{t("react.settings.new_email_address", "New email address")}</RequiredLabel><input autoComplete="email" className="fs-input" id="account-new-email" name="email" onChange={(event) => setNewEmail(event.target.value)} placeholder={t("account.email.placeholder", "name@company.com")} required type="email" value={newEmail} /><span className="fs-field-helper">{t("react.settings.we_send_a_verification_link_before_the_address_changes", "We send a verification link before the address changes.")}</span></form>
        {emailMessage ? <DsAlert tone={emailRequestSucceeded ? "success" : "danger"} title={t("react.settings.email_change", "Email change")}>{emailMessage}</DsAlert> : null}
      </DsCard>
      ) : (
      <DsCard title={t("react.settings.email_address", "Email address")} description={t("react.settings.the_address_used_to_sign_in", "The address used to sign in.")}>
        <div className="fs-current-value"><span><span className="fs-current-value-label">{t("react.settings.current_email", "Current email")}</span><br /><strong>{profile.email}</strong></span></div>
        <DsAlert title={t("react.settings.email_is_not_set_up", "Email is not set up")}>
          {t("react.settings.a_new_sign_in_email_has_to_be_verified_by_email_once_an_administrator_connects_an_email_extension_you_can_chan", "A new sign-in email has to be verified by email. Once an administrator connects an email extension, you can change it here.")} </DsAlert>
      </DsCard>
      )}
      <DsCard title={t("auth.common.password", "Password")} description={t("react.settings.change_your_local_password_and_optionally_revoke_other_sessions", "Change your local password and optionally revoke other sessions.")} footer={<><span className="fs-footer-status">{t("react.settings.password_has_not_been_changed", "Password has not been changed")}</span><button className="fs-button primary" disabled={passwordSaving} form="password-change-form" type="submit">{passwordSaving ? t("react.settings.saving", "Saving…") : t("react.settings.change_password", "Change password")}</button></>}>
        <form className="fs-form-grid" id="password-change-form" noValidate onSubmit={(event) => ignoreAsyncResult(changePassword(event))}>
          <div className="fs-field full"><RequiredLabel>{t("react.settings.current_password", "Current password")}</RequiredLabel><SensitiveInput ariaLabel={t("react.settings.current_password", "Current password")} autoComplete="current-password" id="account-current-password" name="current-password" onChange={(event) => setCurrentPassword(event.target.value)} required value={currentPassword} /></div>
          <div className="fs-field"><RequiredLabel>{t("react.settings.new_password", "New password")}</RequiredLabel><SensitiveInput ariaLabel={t("react.settings.new_password", "New password")} autoComplete="new-password" id="account-new-password" minLength={10} name="new-password" onChange={(event) => setNewPassword(event.target.value)} required value={newPassword} /></div>
          <div className="fs-field"><RequiredLabel>{t("react.settings.confirm_new_password", "Confirm new password")}</RequiredLabel><SensitiveInput ariaLabel={t("react.settings.confirm_new_password", "Confirm new password")} autoComplete="new-password" id="account-confirm-password" minLength={10} name="confirm-password" onChange={(event) => setConfirmPassword(event.target.value)} required value={confirmPassword} /></div>
          <ul className="fs-requirements full" aria-label={t("react.settings.password_requirements", "Password requirements")}><li>{t("react.settings.10_or_more_characters", "10 or more characters")}</li><li>{t("react.settings.passwords_must_match", "Passwords must match")}</li><li>{t("react.settings.backend_policy_may_require_more", "Backend policy may require more")}</li></ul>
          <div className="fs-field full"><DsCheckbox checked={revokeOtherSessions} onChange={setRevokeOtherSessions}>{t("react.settings.revoke_other_sessions_after_changing_the_password", "Revoke other sessions after changing the password")}</DsCheckbox></div>
        </form>
        {passwordMessage ? <DsAlert tone={passwordChangeSucceeded ? "success" : "danger"} title={t("react.settings.password_update", "Password update")}>{passwordMessage}</DsAlert> : null}
      </DsCard>
      </div>
    );
  }

  const tokenRows = tokensQuery.data?.tokens || [];
  function renderTokensPanel() {
    return (
      <div className="fs-panel-stack">
      <div className="fs-panel-heading"><div><h2 className="fs-panel-title">{t("react.settings.personal_access_tokens", "Personal access tokens")}</h2><p>{t("react.settings.create_scoped_credentials_for_api_and_automation_access", "Create scoped credentials for API and automation access.")}</p></div><button className="fs-button primary" onClick={() => setCreateTokenOpen(true)} type="button">{t("react.settings.create_token", "Create token")}</button></div>
      <article className="fs-card fs-token-table-card">
        {tokensQuery.isError ? <div className="fs-card-body"><DsAlert tone="danger" title={t("react.settings.tokens_could_not_be_loaded", "Tokens could not be loaded")}>{t("react.settings.check_the_connection_and_try_again", "Check the connection and try again.")}</DsAlert><button className="fs-button" onClick={() => ignoreAsyncResult(tokensQuery.refetch())} type="button">{t("job_security.retry", "Retry")}</button></div> : null}
        <div className="fs-table-scroll"><table className="fs-table fs-token-table" aria-label={t("react.settings.personal_access_tokens", "Personal access tokens")} aria-busy={tokensQuery.isFetching}><thead><tr><th>{t("account.tokens.label", "Label")}</th><th>{t("account_settings.card.status", "Status")}</th><th>{t("react.settings.scopes", "Scopes")}</th><th>{t("react.settings.restrictions", "Restrictions")}</th><th>{t("account.tokens.created", "Created")}</th><th>{t("account.tokens.expires", "Expires")}</th><th>{t("react.settings.last_used", "Last used")}</th><th aria-label={t("monitoring.table.actions", "Actions")} /></tr></thead><tbody>{tokenRows.map((token) => <tr key={token.id}><td><strong>{token.label}</strong></td><td><span className={`fs-badge ${tokenStatus(token) === "Active" ? "green" : ""}`}>{tokenStatus(token, t)}</span></td><td title={token.scopes.join(", ")}>{token.scopes[0] || "—"}{token.scopes.length > 1 ? ` +${token.scopes.length - 1}` : ""}</td><td>{token.allowed_project_ids.length || token.allowed_workflow_ids.length ? t("react.settings.value1_projects_value2_workflows", "{value1} projects · {value2} workflows", { value1: String(token.allowed_project_ids.length), value2: String(token.allowed_workflow_ids.length) }) : t("react.settings.unrestricted_within_authority", "Unrestricted within authority")}</td><td>{formatDate(token.created_at, t)}</td><td>{token.expires_at ? formatDate(token.expires_at, t) : t("account.tokens.no_expiry", "No expiry")}</td><td>{formatDate(token.last_used_at, t)}</td><td className="fs-table-action-cell"><RowActionMenu label={t("react.settings.actions_for_value1", "Actions for {value1}", { value1: String(token.label) })} onOpenChange={(open) => setMenuToken(open ? token : null)} open={menuToken?.id === token.id}><button onClick={() => { setRevokeTarget(token); setMenuToken(null); }} role="menuitem" type="button">{t("react.settings.revoke_token", "Revoke token")}</button></RowActionMenu></td></tr>)}</tbody></table>{!tokensQuery.isLoading && !tokenRows.length ? <div className="fs-table-state"><strong>{t("react.settings.no_personal_tokens_yet", "No personal tokens yet")}</strong><span>{t("react.settings.create_a_scoped_token_when_an_api_or_automation_client_needs_access", "Create a scoped token when an API or automation client needs access.")}</span><button className="fs-button primary" onClick={() => setCreateTokenOpen(true)} type="button">{t("react.settings.create_token", "Create token")}</button></div> : null}{tokensQuery.isLoading ? <div className="fs-table-state"><strong>{t("react.settings.loading_personal_tokens", "Loading personal tokens")}</strong><span>{t("react.settings.table_geometry_remains_stable_while_data_is_fetched", "Table geometry remains stable while data is fetched.")}</span></div> : null}</div>
        <Pagination onPageChange={setTokenPage} onPerPageChange={(value) => { setTokenPerPage(value); setTokenPage(1); }} page={tokenPage} perPage={tokenPerPage} total={tokensQuery.data?.total || 0} />
      </article>
      </div>
    );
  }

  function renderAccountSettingsPanel() {
    if (profileQuery.isLoading) {
      return (
        <article className="fs-card">
          <div className="fs-card-body">
            <strong>{t("react.settings.loading_account_settings", "Loading account settings")}</strong>
            <span className="fs-field-helper">{t("react.settings.profile_and_sign_in_data_will_appear_in_this_settings_layout", "Profile and sign-in data will appear in this settings layout.")}</span>
          </div>
        </article>
      );
    }
    if (profileQuery.isError) {
      return (
        <article className="fs-card">
          <div className="fs-card-body">
            <DsAlert tone="danger" title={t("react.settings.account_settings_could_not_be_loaded", "Account settings could not be loaded")}>{t("react.settings.check_the_connection_and_try_again", "Check the connection and try again.")}</DsAlert>
            <button className="fs-button" onClick={() => ignoreAsyncResult(profileQuery.refetch())} type="button">{t("job_security.retry", "Retry")}</button>
          </div>
        </article>
      );
    }
    return (
      <section aria-label={t("react.settings.account_settings", "Account settings")} className="fs-settings-tabs">
      <div className="fs-tabs-scroll">
        <div aria-label={t("react.settings.account_sections", "Account sections")} className="fs-tabs-list" role="tablist">
          {getAccountTabs(t).map((tab, index) => (
            <button
              aria-controls={`account-panel-${tab.key}`}
              aria-selected={activeTab === tab.key}
              className="fs-tab"
              id={`account-tab-${tab.key}`}
              key={tab.key}
              onClick={() => goToTab(tab.key)}
              onKeyDown={(event) => {
                if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                event.preventDefault();
                const nextIndex = nextAccountTabIndex(event.key, index, t);
                document.getElementById(`account-tab-${getAccountTabs(t)[nextIndex]?.key}`)?.focus();
              }}
              role="tab"
              tabIndex={activeTab === tab.key ? 0 : -1}
              type="button"
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>
      <div
        aria-labelledby={`account-tab-${activeTab}`}
        className="fs-tabs-panel"
        id={`account-panel-${activeTab}`}
        role="tabpanel"
      >
        {activeTab === "profile" ? renderProfilePanel() : null}
        {activeTab === "signin" ? renderSigninPanel() : null}
        {activeTab === "tokens" ? renderTokensPanel() : null}
        {EXTENSION_ACCOUNT_TABS.map((tab) => {
          if (activeTab !== tab.key) return null;
          const Component = resolveRegisteredPageComponent(tab.componentKey);
          return Component ? createElement(Component, { page: tabPageRecord(tab) }) : null;
        })}
      </div>
      </section>
    );
  }

  function renderCustomExpiry() {
    const today = todayIn(userTimezone);
    const error = customExpiryError || (customExpiry ? pastExpiryError(customExpiry, today, t) : "");
    const helper = customExpiry && !error
      ? t("react.settings.custom_expiry_description", "Expires on {date}, {days} days from today.", { date: formatCalendarDay(customExpiry), days: wholeDaysBetween(today, customExpiry) })
      : t("react.settings.custom_expiry_future_help", "Must be a future date. The earliest allowed day is tomorrow.");
    return (
      <div className="fs-field">
        <label className="fs-field-label" htmlFor="personal-token-expires-on">{t("react.settings.expires_on", "Expires on")}</label>
        <input aria-describedby="personal-token-expires-helper" aria-invalid={Boolean(error)} className="fs-input" id="personal-token-expires-on" min={addDays(today, 1)} onChange={(event) => { setCustomExpiry(event.target.value); setCustomExpiryError(""); }} ref={customExpiryRef} type="date" value={customExpiry} />
        <span className={error ? "fs-field-helper error" : "fs-field-helper"} id="personal-token-expires-helper" role={error ? "alert" : undefined}>{error || helper}</span>
      </div>
    );
  }

  function renderAccountDialogs() {
    return (
      <>
        <DsDialog busy={tokenSubmitting} closeLabel={t("react.settings.close_create_token_dialog", "Close create token dialog")} description={t("react.settings.choose_the_minimum_scopes_and_restrictions_this_client_needs", "Choose the minimum scopes and restrictions this client needs.")} eyebrow={t("react.settings.personal_tokens", "Personal tokens")} footer={<><button className="fs-button" disabled={tokenSubmitting} onClick={() => setCreateTokenOpen(false)} type="button">{t("common.cancel", "Cancel")}</button><button className="fs-button primary" disabled={tokenSubmitting} form="create-personal-token" type="submit">{tokenSubmitting ? t("react.settings.creating", "Creating…") : t("react.settings.create_token", "Create token")}</button></>} onClose={() => setCreateTokenOpen(false)} open={createTokenOpen} title={t("react.settings.create_personal_token", "Create personal token")}>
          <form className="fs-dialog-form" id="create-personal-token" noValidate onSubmit={(event) => ignoreAsyncResult(createToken(event))}>
            <label className="fs-field"><RequiredLabel>{t("account.tokens.label", "Label")}</RequiredLabel><input className="fs-input" id="personal-token-label" name="label" onChange={(event) => setTokenLabel(event.target.value)} placeholder={t("react.settings.e_g_local_cli", "e.g. Local CLI")} required value={tokenLabel} /></label>
            <fieldset className="fs-field"><legend className="fs-field-label">{t("react.settings.expiration", "Expiration")}</legend><div className="fs-radio-grid">{(["none", "30", "90", "custom"] as ExpiryChoice[]).map((value) => <DsRadio checked={expiry === value} key={value} name="token-expiry" onChange={() => setExpiry(value)} value={value}>{expiryLabel(value, t)}</DsRadio>)}</div>{expiry === "custom" ? renderCustomExpiry() : null}</fieldset>
            <fieldset aria-required="true" className="fs-field"><legend className="fs-field-label fs-required-label">{t("react.settings.scopes", "Scopes")}<span aria-hidden="true" className="fs-required">*</span></legend><div className="fs-scope-grid">{TOKEN_SCOPES.map((scope) => <DsCheckbox checked={scopes.includes(scope)} key={scope} onChange={(checked) => setScopes(checked ? [...scopes, scope] : scopes.filter((value) => value !== scope))}>{scope}</DsCheckbox>)}</div><span className="fs-field-helper">{t("react.settings.personal_token_scope_help", "Select at least one scope. Granular scopes do not grant workflow authoring or full administrative access. The server still enforces your role and restrictions.")}</span></fieldset>
            {isAdmin ? <div className="fs-field"><DsCheckbox checked={administratorAccess} onChange={setAdministratorAccess}>{t("react.settings.personal_token_admin_access", "Full administrator access (admin:all)")}</DsCheckbox><span className="fs-field-helper">{t("react.settings.personal_token_admin_access_help", "Allows workflow creation and all other administrator operations permitted by your account, project and workflow restrictions. Select only for trusted clients.")}</span></div> : null}
            <div className="fs-field"><span className="fs-field-label">{t("react.settings.project_restrictions", "Project restrictions")}</span><MultiCombobox ariaLabel={t("react.settings.project_restrictions", "Project restrictions")} disabled={projectsQuery.isLoading} onChange={setProjectIds} options={projectOptions} placeholder={projectsQuery.isLoading ? t("react.settings.loading_projects", "Loading projects…") : t("react.settings.search_projects", "Search projects")} selected={projectIds} /><span className="fs-field-helper">{t("react.settings.empty_means_unrestricted_within_your_existing_authority", "Empty means unrestricted within your existing authority.")}</span></div>
            <div className="fs-field"><span className="fs-field-label">{t("react.settings.workflow_restrictions", "Workflow restrictions")}</span><MultiCombobox ariaLabel={t("react.settings.workflow_restrictions", "Workflow restrictions")} disabled={workflowsQuery.isLoading} onChange={setWorkflowIds} options={workflowOptions} placeholder={workflowsQuery.isLoading ? t("react.settings.loading_workflows", "Loading workflows…") : t("react.settings.search_workflows", "Search workflows")} selected={workflowIds} /><span className="fs-field-helper">{t("react.settings.options_load_only_while_this_dialog_is_needed", "Options load only while this dialog is needed.")}</span></div>
            {tokenError ? <DsAlert tone="danger" title={t("react.settings.token_could_not_be_created", "Token could not be created")}>{tokenError}</DsAlert> : null}
          </form>
        </DsDialog>

        <DsDialog closeLabel={t("react.settings.close_one_time_token_secret", "Close one-time token secret")} description={t("react.settings.this_secret_is_shown_once_and_disappears_when_you_close_this_dialog", "This secret is shown once and disappears when you close this dialog.")} eyebrow={t("react.settings.created_successfully", "Created successfully")} footer={<button className="fs-button primary" onClick={dismissSecret} type="button">{t("react.settings.i_ve_saved_it", "I've saved it")}</button>} onClose={dismissSecret} open={Boolean(secret)} title={t("react.settings.save_your_personal_token", "Save your personal token")} variant="secret"><div className="fs-secret-card"><div><h3 className="fs-card-title">{secretLabel}</h3><p className="fs-secret-description">{t("react.settings.store_this_value_in_a_secure_credential_manager", "Store this value in a secure credential manager.")}</p></div><DsAlert tone="warning" title={t("react.settings.you_cannot_view_this_token_again", "You cannot view this token again")}>{t("react.settings.copy_it_before_dismissing_this_surface", "Copy it before dismissing this surface.")}</DsAlert><div className="fs-secret-row"><SensitiveInput ariaLabel={t("react.settings.one_time_token_secret", "One-time token secret")} className="fs-code-value" readOnly value={secret} /><button className="fs-button" onClick={() => ignoreAsyncResult(copySecret())} type="button">{t("react.settings.copy_token", "Copy token")}</button></div></div></DsDialog>
        <DsDialog busy={revoking} closeLabel={t("react.settings.close_revoke_confirmation", "Close revoke confirmation")} description={t("react.settings.clients_using_this_token_lose_access_immediately_this_action_cannot_be_undone", "Clients using this token lose access immediately. This action cannot be undone.")} eyebrow={t("react.settings.personal_tokens", "Personal tokens")} footer={<><button className="fs-button" disabled={revoking} onClick={() => setRevokeTarget(null)} type="button">{t("common.cancel", "Cancel")}</button><button className="fs-button danger" disabled={revoking} onClick={() => ignoreAsyncResult(confirmRevoke())} type="button">{revoking ? t("react.settings.revoking", "Revoking…") : t("react.settings.revoke_token", "Revoke token")}</button></>} onClose={() => setRevokeTarget(null)} open={Boolean(revokeTarget)} title={t("react.settings.revoke_value1", "Revoke “{value1}”?", { value1: String(revokeTarget?.label || t("react.settings.token_lowercase", "token")) })} variant="confirm"><DsAlert tone="danger" title={t("react.settings.immediate_access_loss", "Immediate access loss")}>{t("react.settings.confirm_only_after_the_client_has_another_credential", "Confirm only after the client has another credential.")}</DsAlert></DsDialog>
        <DsDialog closeLabel={t("react.settings.close_discard_confirmation", "Close discard confirmation")} description={t("react.settings.display_name_timezone_and_language_return_to_their_last_saved_values", "Display name, timezone and language return to their last saved values.")} eyebrow={t("react.settings.unsaved_profile", "Unsaved profile")} footer={<><button className="fs-button" onClick={() => setDiscardOpen(false)} type="button">{t("react.settings.keep_editing", "Keep editing")}</button><button className="fs-button danger" onClick={() => { setDraft(saved); setDiscardOpen(false); if (pendingTab) goToTab(pendingTab, true); }} type="button">{t("react.settings.discard_changes", "Discard changes")}</button></>} onClose={() => setDiscardOpen(false)} open={discardOpen} title={t("react.settings.discard_your_profile_changes", "Discard your profile changes?")} variant="confirm" />
        {copyFeedback ? <DsToast tone={copyFeedback.tone}>{copyFeedback.message}</DsToast> : null}
      </>
    );
  }

  return (
    <section className="fs-settings fs-settings-detail">
      <DsPageHeader description={t("react.settings.manage_your_profile_sign_in_details_and_personal_access_tokens", "Manage your profile, sign-in details and personal access tokens.")} title={t("auth.login.account_label", "Account")} />
      {renderAccountSettingsPanel()}
      {renderAccountDialogs()}
    </section>
  );
}
