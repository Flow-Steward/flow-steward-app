import { useI18n } from "@/lib/i18n/useI18n";

import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { useQueryClient, useQuery } from "@tanstack/react-query";
import { useUserTimezone } from "@/lib/time/user-timezone";
import { useState, useRef, useMemo } from "react";
import {
  fetchServiceAccountsCollection,
  fetchServiceAccountTokensCollection,
  createServiceAccount,
  updateServiceAccount,
  deleteServiceAccount,
  deleteServiceAccountToken,
  revokeServiceAccountToken,
  createServiceAccountToken,
  updateServiceAccountToken,
} from "@/pages/settings/shared/api";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { fetchWorkflows } from "@/pages/workflows/shared/api";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { DsCard, DsAlert } from "@/components/shared/design-system";
import { todayIn, wholeDaysBetween } from "@/lib/time/calendar-days";
import {
  extractOneTimeTokenSecret,
  extractOneTimeTokenLabel,
} from "@/pages/settings/shared/one-time-token-secret-utils";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { ConfirmActionDialog } from "@/components/shared/confirm-action-dialog";
import {
  type AccountDrawerMode,
  type TokenDrawerMode,
  type TokenExpiryMode,
  tokenId,
  serviceAccountKey,
  toStringList,
  focusFirstAvailable,
  neighbourIds,
  customExpiryError,
} from "./service-account-support";
import { ServiceAccountForm } from "./service-account-form";
import { MachineTokenForm } from "./machine-token-form";
import {
  ServiceAccountsListCard,
  ServiceAccountDrawer,
  ServiceAccountCredentials,
  MachineTokenDrawer,
} from "./service-account-drawers";

import "@/pages/settings/shared/settings.css";

export function SettingsServiceAccountsPage() {
  const { t } = useI18n();
  const isAdmin = useIsAdmin();
  const queryClient = useQueryClient();
  const userTimezone = useUserTimezone();
  const [selectedServiceAccountKey, setSelectedServiceAccountKey] =
    useState("");
  const [selectedTokenId, setSelectedTokenId] = useState("");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [accountDrawerMode, setAccountDrawerMode] =
    useState<AccountDrawerMode>("closed");
  const [tokenDrawerMode, setTokenDrawerMode] =
    useState<TokenDrawerMode>("closed");
  // One confirmation at a time. Each is irreversible, so each is a blocking
  // Modal / Dialog under canon's routing, and its failure stays inside it.
  const [confirmation, setConfirmation] = useState<
    | { kind: "delete-account"; key: string; label: string }
    | { kind: "delete-token"; id: string; label: string }
    | { kind: "revoke-token"; id: string; label: string }
    | null
  >(null);
  const [confirmationError, setConfirmationError] = useState("");

  // A new principal starts with no authority and inactive. Prefilling a key,
  // a scope and an active flag made a broad credential the default path, and
  // empty restriction lists mean unrestricted in the principal contract.
  const [accountKeyInput, setAccountKeyInput] = useState("");
  const [accountDisplayNameInput, setAccountDisplayNameInput] = useState("");
  const [accountDescriptionInput, setAccountDescriptionInput] = useState("");
  const [accountScopesInput, setAccountScopesInput] = useState<string[]>([]);
  const [accountProjectIdsInput, setAccountProjectIdsInput] = useState<
    string[]
  >([]);
  const [accountWorkflowIdsInput, setAccountWorkflowIdsInput] = useState<
    string[]
  >([]);
  const [accountActiveInput, setAccountActiveInput] = useState(false);

  const [tokenLabelInput, setTokenLabelInput] = useState("service-token");
  const [tokenExpiryMode, setTokenExpiryMode] = useState<TokenExpiryMode>("30");
  const [tokenExpiryDate, setTokenExpiryDate] = useState("");
  const [tokenScopesInput, setTokenScopesInput] = useState<string[]>([]);
  const [accountProjectSearch, setAccountProjectSearch] = useState("");
  const [accountWorkflowSearch, setAccountWorkflowSearch] = useState("");
  const [newTokenSecret, setNewTokenSecret] = useState("");
  const [newTokenLabel, setNewTokenLabel] = useState("");
  const [accountFieldErrors, setAccountFieldErrors] = useState({
    key: "",
    scopes: "",
  });
  const [tokenExpiresError, setTokenExpiresError] = useState("");
  const accountKeyRef = useRef<HTMLInputElement>(null);
  const firstAccountScopeRef = useRef<HTMLInputElement>(null);
  const tokenExpiresRef = useRef<HTMLInputElement>(null);

  const serviceAccountsQuery = useQuery({
    queryKey: ["settings", "service-accounts"],
    queryFn: fetchServiceAccountsCollection,
    enabled: isAdmin,
    refetchInterval: 30_000,
  });

  const tokensQuery = useQuery({
    queryKey: [
      "settings",
      "service-accounts",
      "tokens",
      selectedServiceAccountKey,
    ],
    queryFn: () =>
      fetchServiceAccountTokensCollection(selectedServiceAccountKey),
    enabled: isAdmin && Boolean(selectedServiceAccountKey),
    refetchInterval: 30_000,
  });

  const projectsQuery = useQuery({
    queryKey: ["settings", "service-accounts", "project-picker"],
    queryFn: () => fetchProjectsList({ page: 1, perPage: 200, search: "" }),
    enabled: isAdmin,
    refetchInterval: 60_000,
  });

  const workflowsQuery = useQuery({
    queryKey: ["settings", "service-accounts", "workflow-picker"],
    queryFn: () => fetchWorkflows({ enabledOnly: false, search: "" }),
    enabled: isAdmin,
    refetchInterval: 60_000,
  });

  const projectOptions = useMemo(
    () => projectsQuery.data?.projects || [],
    [projectsQuery.data?.projects],
  );
  const workflowOptions = useMemo(
    () => (Array.isArray(workflowsQuery.data) ? workflowsQuery.data : []),
    [workflowsQuery.data],
  );

  const accountProjectOptions = useMemo(() => {
    const needle = accountProjectSearch.trim().toLowerCase();
    return projectOptions
      .filter((project) => {
        const label = `${project.id} ${project.name || ""}`.toLowerCase();
        return !needle || label.includes(needle);
      })
      .map((project) => ({
        id: project.id,
        label: project.name?.trim() || t("react.settings.unnamed_project", "Unnamed project"),
      }));
  }, [accountProjectSearch, projectOptions, t]);

  const accountWorkflowOptions = useMemo(() => {
    const needle = accountWorkflowSearch.trim().toLowerCase();
    return workflowOptions.flatMap((workflow) => {
      const workflowId = firstStringFromUnknownValue([
        workflow.workflow_id,
        "",
      ]).trim();
      const label = firstStringFromUnknownValue([workflow.display_name]).trim() || t("react.settings.unnamed_workflow", "Unnamed workflow");
      const searchable = `${workflowId} ${label}`.toLowerCase();
      return workflowId && (!needle || searchable.includes(needle))
        ? [{ id: workflowId, label, description: firstStringFromUnknownValue([workflow.description]).trim() }]
        : [];
    });
  }, [accountWorkflowSearch, workflowOptions, t]);

  const selectedToken = useMemo(
    () =>
      (tokensQuery.data || []).find(
        (token) => tokenId(token.id ?? token.token_id) === selectedTokenId,
      ),
    [selectedTokenId, tokensQuery.data],
  );
  const selectedServiceAccount = useMemo(
    () =>
      (serviceAccountsQuery.data || []).find(
        (account) =>
          serviceAccountKey(account.service_account_key) ===
          selectedServiceAccountKey,
      ),
    [selectedServiceAccountKey, serviceAccountsQuery.data],
  );

  const accountDrawerOpen = accountDrawerMode !== "closed";
  const tokenDrawerOpen = tokenDrawerMode !== "closed";
  const selectedTokenRevoked = Boolean(selectedToken?.revoked);

  if (!isAdmin) {
    return (
      <DsCard
        description={t("react.settings.you_do_not_have_permission_to_view_service_account_identities_or_their_authority", "You do not have permission to view service-account identities or their authority.")}
        title={t("react.settings.administrator_access_required", "Administrator access required")}
      />
    );
  }

  async function refresh(serviceAccountKey = selectedServiceAccountKey) {
    await queryClient.invalidateQueries({
      queryKey: ["settings", "service-accounts"],
    });
    if (serviceAccountKey) {
      await queryClient.invalidateQueries({
        queryKey: ["settings", "service-accounts", "tokens", serviceAccountKey],
      });
    }
  }

  function closeTokenDrawer() {
    setTokenDrawerMode("closed");
    setSelectedTokenId("");
    setNewTokenSecret("");
    setNewTokenLabel("");
    setTokenExpiresError("");
  }

  function closeAccountDrawer() {
    setAccountDrawerMode("closed");
    setAccountFieldErrors({ key: "", scopes: "" });
  }

  function openCreateAccountDrawer() {
    setMessage("");
    closeTokenDrawer();
    // Every field, not just the mode. Without this, opening New after viewing
    // an account carried that account's key, scopes, restrictions, description
    // and active state into the create surface.
    setSelectedServiceAccountKey("");
    setAccountKeyInput("");
    setAccountDisplayNameInput("");
    setAccountDescriptionInput("");
    setAccountScopesInput([]);
    setAccountProjectIdsInput([]);
    setAccountWorkflowIdsInput([]);
    setAccountActiveInput(false);
    setAccountProjectSearch("");
    setAccountWorkflowSearch("");
    setAccountFieldErrors({ key: "", scopes: "" });
    setAccountDrawerMode("create");
  }

  function openAccountDetailDrawer(account: Record<string, unknown>) {
    const key = serviceAccountKey(account.service_account_key);
    setMessage("");
    closeTokenDrawer();
    setSelectedServiceAccountKey(key);
    setAccountKeyInput(key);
    setAccountDisplayNameInput(
      firstStringFromUnknownValue([account.display_name, key]),
    );
    setAccountDescriptionInput(
      firstStringFromUnknownValue([account.description, ""]),
    );
    setAccountScopesInput(toStringList(account.scopes));
    setAccountProjectIdsInput(toStringList(account.allowed_project_ids));
    setAccountWorkflowIdsInput(toStringList(account.allowed_workflow_ids));
    setAccountActiveInput(Boolean(account.is_active));
    setAccountProjectSearch("");
    setAccountWorkflowSearch("");
    setAccountFieldErrors({ key: "", scopes: "" });
    setAccountDrawerMode("detail");
  }

  function resetTokenCreateForm() {
    setTokenLabelInput("service-token");
    setTokenExpiryMode("30");
    setTokenExpiryDate("");
    setTokenScopesInput([]);
    setNewTokenSecret("");
    setNewTokenLabel("");
    setTokenExpiresError("");
  }

  function openCreateTokenDrawer() {
    if (!selectedServiceAccountKey) {
      setMessage(t("react.settings.select_a_service_account_first", "Select a service account first."));
      return;
    }
    setMessage("");
    setSelectedTokenId("");
    resetTokenCreateForm();
    setTokenDrawerMode("create");
  }

  function openEditTokenDrawer(token: Record<string, unknown>) {
    const id = tokenId(token.id ?? token.token_id);
    setMessage("");
    setSelectedTokenId(id);
    setTokenLabelInput(
      firstStringFromUnknownValue([token.label, token.name, t("react.settings.token_value1", "Token {value1}", { value1: String(id) })]),
    );
    setTokenScopesInput(toStringList(token.scopes));
    setNewTokenSecret("");
    setNewTokenLabel("");
    setTokenDrawerMode("edit");
  }

  async function handleCreateServiceAccount() {
    setMessage("");
    setNewTokenSecret("");
    setNewTokenLabel("");
    const payload = {
      service_account_key: accountKeyInput.trim(),
      display_name: accountDisplayNameInput.trim(),
      description: accountDescriptionInput.trim(),
      scopes: accountScopesInput,
      allowed_project_ids: accountProjectIdsInput,
      allowed_workflow_ids: accountWorkflowIdsInput,
      is_active: accountActiveInput,
    };
    if (!payload.service_account_key) {
      setAccountFieldErrors({ key: t("react.settings.account_key_required", "Account key is required."), scopes: "" });
      queueMicrotask(() => accountKeyRef.current?.focus());
      return;
    }
    if (!payload.scopes.length) {
      setAccountFieldErrors({ key: "", scopes: t("react.settings.select_at_least_one_scope", "Select at least one scope.") });
      queueMicrotask(() => firstAccountScopeRef.current?.focus());
      return;
    }
    setAccountFieldErrors({ key: "", scopes: "" });
    setSaving(true);
    try {
      const duplicate = (serviceAccountsQuery.data || []).some(
        (account) =>
          serviceAccountKey(account.service_account_key) ===
          payload.service_account_key,
      );
      if (duplicate) {
        throw new Error(
          t("react.settings.service_account_key_already_exists", "Service account '{key}' already exists. Open it to issue another token.", { key: payload.service_account_key }),
        );
      }
      await createServiceAccount(payload);
      setMessage(t("react.settings.service_account_created", "Service account created."));
      setSelectedServiceAccountKey(payload.service_account_key);
      setAccountDrawerMode("detail");
      await refresh(payload.service_account_key);
    } catch (error) {
      setMessage(
        error instanceof Error
          ? error.message
          : t("react.settings.failed_to_create_service_account", "Failed to create service account."),
      );
    } finally {
      setSaving(false);
    }
  }

  async function handleUpdateServiceAccount(
    nextActive = accountActiveInput,
    validateAuthority = true,
  ) {
    if (!selectedServiceAccountKey) {
      return;
    }
    if (validateAuthority && !accountScopesInput.length) {
      setAccountFieldErrors({ key: "", scopes: t("react.settings.select_at_least_one_scope", "Select at least one scope.") });
      queueMicrotask(() => firstAccountScopeRef.current?.focus());
      return;
    }
    setAccountFieldErrors({ key: "", scopes: "" });
    setMessage("");
    setSaving(true);
    try {
      setAccountActiveInput(nextActive);
      await updateServiceAccount(selectedServiceAccountKey, {
        display_name: accountDisplayNameInput.trim(),
        description: accountDescriptionInput.trim(),
        scopes: accountScopesInput,
        allowed_project_ids: accountProjectIdsInput,
        allowed_workflow_ids: accountWorkflowIdsInput,
        is_active: nextActive,
      });
      setMessage(t("react.settings.service_account_value1_updated", "Service account {value1} updated.", { value1: String(selectedServiceAccountKey) }));
      await refresh();
    } catch (error) {
      setMessage(
        error instanceof Error
          ? error.message
          : t("react.settings.failed_to_update_service_account", "Failed to update service account."),
      );
    } finally {
      setSaving(false);
    }
  }

  function requestConfirmation(next: NonNullable<typeof confirmation>) {
    setConfirmationError("");
    setConfirmation(next);
  }

  function cancelConfirmation() {
    if (saving) return;
    setConfirmation(null);
    setConfirmationError("");
  }

  async function confirmPendingAction() {
    if (!confirmation || !selectedServiceAccountKey) {
      return;
    }
    setConfirmationError("");
    setMessage("");
    setSaving(true);
    try {
      if (confirmation.kind === "delete-account") {
        const accountKeys = (serviceAccountsQuery.data || []).map((account) =>
          serviceAccountKey(account.service_account_key),
        );
        await deleteServiceAccount(confirmation.key);
        setConfirmation(null);
        setMessage(t("react.settings.service_account_value1_deleted", "Service account {value1} deleted.", { value1: String(confirmation.key) }));
        setSelectedServiceAccountKey("");
        closeAccountDrawer();
        closeTokenDrawer();
        // Focus moves now, to a row that survives the refresh, so it never
        // waits on body while the list reloads.
        focusFirstAvailable([
          ...neighbourIds(accountKeys, confirmation.key, "service-account-row-"),
          "service-accounts-heading",
        ]);
        await refresh("");
      } else if (confirmation.kind === "delete-token") {
        const tokenIds = (tokensQuery.data || []).map((token) => tokenId(token.id ?? token.token_id));
        await deleteServiceAccountToken(selectedServiceAccountKey, confirmation.id);
        setConfirmation(null);
        setMessage(t("react.settings.token_value1_deleted", "Token “{value1}” deleted.", { value1: String(confirmation.label) }));
        closeTokenDrawer();
        focusFirstAvailable([
          ...neighbourIds(tokenIds, confirmation.id, "machine-token-row-"),
          "service-account-credentials-heading",
        ]);
        await refresh();
      } else {
        await revokeServiceAccountToken(selectedServiceAccountKey, confirmation.id);
        setConfirmation(null);
        setMessage(t("react.settings.token_value1_revoked", "Token “{value1}” revoked.", { value1: String(confirmation.label) }));
        // Revoke survives on screen but is about to disable, so it cannot hold focus.
        focusFirstAvailable(["machine-token-delete-action", "service-account-credentials-heading"]);
        await refresh();
      }
    } catch (error) {
      setConfirmationError(error instanceof Error ? error.message : t("react.settings.the_action_did_not_complete_try_again", "The action did not complete. Try again."));
    } finally {
      setSaving(false);
    }
  }

  async function handleCreateToken() {
    if (!selectedServiceAccountKey) {
      setMessage(t("react.settings.select_a_service_account_first", "Select a service account first."));
      return;
    }
    // Every branch resolves to a day count of at least 1. Omitting the field
    // made the API read it as "no expiry supplied" and mint a token that never
    // expires. A custom date converts here, at submit, so a form opened before
    // midnight and sent after it does not send a count one day too large.
    let expires = Number(tokenExpiryMode);
    if (tokenExpiryMode === "custom") {
      const today = todayIn(userTimezone);
      const error = tokenExpiryDate
        ? customExpiryError(tokenExpiryDate, today, t)
        : t("react.settings.choose_the_date_the_token_expires", "Choose the date the token expires.");
      if (error) {
        setTokenExpiresError(error);
        queueMicrotask(() => tokenExpiresRef.current?.focus());
        return;
      }
      expires = wholeDaysBetween(today, tokenExpiryDate);
    }
    setTokenExpiresError("");
    setMessage("");
    setSaving(true);
    try {
      const payload: Record<string, unknown> = {
        label: tokenLabelInput.trim(),
        expires_in_days: expires,
        scopes: tokenScopesInput,
      };
      const response = await createServiceAccountToken(
        selectedServiceAccountKey,
        payload,
      );
      const rawToken = extractOneTimeTokenSecret(response);
      setNewTokenSecret(rawToken);
      setNewTokenLabel(
        extractOneTimeTokenLabel(
          response,
          tokenLabelInput.trim() || "Machine token", t,
        ),
      );
      setMessage(
        rawToken
          ? t("react.settings.machine_token_created_save_it_now_it_is_shown_only_once", "Machine token created. Save it now; it is shown only once.")
          : t("react.settings.machine_token_created", "Machine token created."),
      );
      await refresh();
    } catch (error) {
      setMessage(
        error instanceof Error
          ? error.message
          : t("react.settings.failed_to_create_machine_token", "Failed to create machine token."),
      );
    } finally {
      setSaving(false);
    }
  }

  async function handleUpdateToken() {
    if (!selectedServiceAccountKey || !selectedTokenId) {
      return;
    }
    setMessage("");
    setSaving(true);
    try {
      await updateServiceAccountToken(
        selectedServiceAccountKey,
        selectedTokenId,
        {
          label: tokenLabelInput.trim(),
          scopes: tokenScopesInput,
        },
      );
      setMessage(t("react.settings.token_value1_updated", "Token {value1} updated.", { value1: String(selectedTokenId) }));
      await refresh();
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : t("react.settings.failed_to_update_token", "Failed to update token."),
      );
    } finally {
      setSaving(false);
    }
  }

  const accountCreateForm = (
    <ServiceAccountForm
      accountActiveInput={accountActiveInput}
      accountDescriptionInput={accountDescriptionInput}
      accountDisplayNameInput={accountDisplayNameInput}
      accountDrawerMode={accountDrawerMode}
      accountFieldErrors={accountFieldErrors}
      accountKeyInput={accountKeyInput}
      accountKeyRef={accountKeyRef}
      accountProjectIdsInput={accountProjectIdsInput}
      accountProjectOptions={accountProjectOptions}
      accountProjectSearch={accountProjectSearch}
      accountScopesInput={accountScopesInput}
      accountWorkflowIdsInput={accountWorkflowIdsInput}
      accountWorkflowOptions={accountWorkflowOptions}
      accountWorkflowSearch={accountWorkflowSearch}
      firstAccountScopeRef={firstAccountScopeRef}
      onCreate={() => ignoreAsyncResult(handleCreateServiceAccount())}
      onUpdate={() => ignoreAsyncResult(handleUpdateServiceAccount())}
      setAccountActiveInput={setAccountActiveInput}
      setAccountDescriptionInput={setAccountDescriptionInput}
      setAccountDisplayNameInput={setAccountDisplayNameInput}
      setAccountFieldErrors={setAccountFieldErrors}
      setAccountKeyInput={setAccountKeyInput}
      setAccountProjectIdsInput={setAccountProjectIdsInput}
      setAccountProjectSearch={setAccountProjectSearch}
      setAccountScopesInput={setAccountScopesInput}
      setAccountWorkflowIdsInput={setAccountWorkflowIdsInput}
      setAccountWorkflowSearch={setAccountWorkflowSearch}
    />
  );

  const tokenForm = (
    <MachineTokenForm
      accountScopes={toStringList(selectedServiceAccount?.scopes)}
      message={message}
      onCreate={() => ignoreAsyncResult(handleCreateToken())}
      onUpdate={() => ignoreAsyncResult(handleUpdateToken())}
      setTokenExpiryDate={setTokenExpiryDate}
      setTokenExpiryMode={setTokenExpiryMode}
      setTokenExpiresError={setTokenExpiresError}
      setTokenLabelInput={setTokenLabelInput}
      setTokenScopesInput={setTokenScopesInput}
      timezone={userTimezone}
      tokenDrawerMode={tokenDrawerMode}
      tokenExpiryDate={tokenExpiryDate}
      tokenExpiryMode={tokenExpiryMode}
      tokenExpiresError={tokenExpiresError}
      tokenExpiresRef={tokenExpiresRef}
      tokenLabelInput={tokenLabelInput}
      tokenScopesInput={tokenScopesInput}
    />
  );
  const confirmationCopy = confirmation
    ? {
        "delete-account": {
          eyebrow: t("react.settings.service_accounts_confirmation_group", "Service accounts"),
          title: t("react.settings.delete_service_account_value1", "Delete service account “{value1}”?", { value1: String(confirmation.label) }),
          consequence: t("react.settings.every_machine_token_issued_to_this_identity_stops_working_immediately", "Every machine token issued to this identity stops working immediately."),
          confirmLabel: t("react.settings.delete_service_account", "Delete service account"),
          pendingLabel: t("react.settings.deleting", "Deleting…"),
        },
        "delete-token": {
          eyebrow: t("react.settings.machine_tokens", "Machine tokens"),
          title: t("react.settings.delete_token_value1", "Delete token “{value1}”?", { value1: String(confirmation.label) }),
          consequence: t("react.settings.clients_using_this_token_lose_access_immediately_and_its_record_is_removed", "Clients using this token lose access immediately, and its record is removed."),
          confirmLabel: t("react.settings.delete_token", "Delete token"),
          pendingLabel: t("react.settings.deleting", "Deleting…"),
        },
        "revoke-token": {
          eyebrow: t("react.settings.machine_tokens", "Machine tokens"),
          title: t("react.settings.revoke_value1", "Revoke “{value1}”?", { value1: String(confirmation.label) }),
          consequence: t("react.settings.clients_using_this_token_lose_access_immediately", "Clients using this token lose access immediately."),
          confirmLabel: t("react.settings.revoke_token", "Revoke token"),
          pendingLabel: t("react.settings.revoking", "Revoking…"),
        },
      }[confirmation.kind]
    : null;

  return (
    <>
      <ServiceAccountsListCard
        accounts={serviceAccountsQuery.data || []}
        isLoading={serviceAccountsQuery.isLoading}
        onCreate={openCreateAccountDrawer}
        onSelect={openAccountDetailDrawer}
        saving={saving}
        selectedKey={selectedServiceAccountKey}
      />

      {message && !accountDrawerOpen && !tokenDrawerOpen ? <DsAlert title={message} /> : null}

      <ServiceAccountDrawer
        accountActive={accountActiveInput}
        accountForm={accountCreateForm}
        accountKey={selectedServiceAccountKey}
        accountName={firstStringFromUnknownValue([
          selectedServiceAccount?.display_name,
          selectedServiceAccountKey,
          t("react.settings.service_account", "Service account"),
        ])}
        credentials={
          <ServiceAccountCredentials
            accountActive={accountActiveInput}
            accountKey={selectedServiceAccountKey}
            isLoading={tokensQuery.isLoading}
            onCreate={openCreateTokenDrawer}
            onSelect={openEditTokenDrawer}
            saving={saving}
            timezone={userTimezone}
            tokens={tokensQuery.data || []}
          />
        }
        message={message}
        mode={accountDrawerMode}
        nestedDrawerOpen={tokenDrawerOpen}
        onClose={closeAccountDrawer}
        onDelete={() =>
          requestConfirmation({
            kind: "delete-account",
            key: selectedServiceAccountKey,
            label: firstStringFromUnknownValue([
              selectedServiceAccount?.display_name,
              selectedServiceAccountKey,
            ]),
          })
        }
        onToggleActive={() =>
          ignoreAsyncResult(
            handleUpdateServiceAccount(!accountActiveInput, false),
          )
        }
        saving={saving}
        suspended={Boolean(confirmation)}
      />

      <MachineTokenDrawer
        accountKey={selectedServiceAccountKey}
        form={tokenForm}
        mode={tokenDrawerMode}
        newTokenLabel={newTokenLabel}
        newTokenSecret={newTokenSecret}
        onClose={closeTokenDrawer}
        onDelete={() =>
          requestConfirmation({
            kind: "delete-token",
            id: selectedTokenId,
            label: firstStringFromUnknownValue([selectedToken?.label, selectedTokenId]),
          })
        }
        onHideSecret={() => {
          setNewTokenSecret("");
          setNewTokenLabel("");
        }}
        onRevoke={() =>
          requestConfirmation({
            kind: "revoke-token",
            id: selectedTokenId,
            label: firstStringFromUnknownValue([selectedToken?.label, selectedTokenId]),
          })
        }
        revoked={selectedTokenRevoked}
        saving={saving}
        selectedToken={selectedToken}
        selectedTokenId={selectedTokenId}
        suspended={Boolean(confirmation)}
        timezone={userTimezone}
        tokenLabel={tokenLabelInput}
      />

      <ConfirmActionDialog
        closeLabel={t("react.settings.close_confirmation", "Close confirmation")}
        confirmLabel={confirmationCopy?.confirmLabel || ""}
        consequence={confirmationCopy?.consequence || ""}
        error={confirmationError}
        eyebrow={confirmationCopy?.eyebrow || ""}
        onCancel={cancelConfirmation}
        onConfirm={() => ignoreAsyncResult(confirmPendingAction())}
        open={Boolean(confirmation)}
        pending={saving && Boolean(confirmation)}
        pendingLabel={confirmationCopy?.pendingLabel || ""}
        title={confirmationCopy?.title || ""}
      />
    </>
  );
}
