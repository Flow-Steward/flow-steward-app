import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useState, type SubmitEvent } from "react";
import { Link } from "react-router";

import { useQuery } from "@tanstack/react-query";

import {
  DsAlert,
  DsCheckbox,
  DsDialog,
  OptionalLabel,
  RequiredLabel,
  SensitiveInput,
} from "@/components/shared/design-system";
import { HttpError } from "@/lib/http/errors";
import {
  type CatalogSecret,
  type CatalogSecretUsage,
  fetchCatalogSecretDeletePlan,
} from "@/pages/registry/secrets/api";
import { type SecretEditorState, usageLink } from "@/pages/registry/secrets/secret-form-model";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

/** Create a secret (name, description, value) or edit its details. Every project may use it. */
export function SecretEditorDialog({
  mode,
  editing,
  editor,
  setEditor,
  saving,
  error,
  onClose,
  onSubmit,
}: Readonly<{
  mode: "closed" | "add" | "edit";
  editing: CatalogSecret | null;
  editor: SecretEditorState;
  setEditor: (next: SecretEditorState) => void;
  saving: boolean;
  error: string;
  onClose: () => void;
  onSubmit: () => void;
}>) {
  const { t } = useI18n();
  const adding = mode === "add";
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSubmit();
  };
  return (
    <DsDialog
      busy={saving}
      closeLabel={t("react.registry.secrets.close_dialog", "Close")}
      description={
        adding
          ? t("react.registry.secrets.add_description_shared", "Store a value once and choose it wherever a key or password is asked for, in any project. It is never shown again.")
          : t("react.registry.secrets.edit_description", "Rename the secret or change its description. The value stays as it is.")
      }
      eyebrow={t("react.registry.secrets.title", "Secrets")}
      footer={
        <>
          <button className="fs-button" disabled={saving} onClick={onClose} type="button">
            {t("common.cancel", "Cancel")}
          </button>
          <button className="fs-button primary" disabled={saving} form="secret-editor" type="submit">
            {adding ? t("react.registry.secrets.create", "Create secret") : t("react.registry.secrets.save", "Save")}
          </button>
        </>
      }
      onClose={onClose}
      open={mode !== "closed"}
      title={
        adding
          ? t("react.registry.secrets.create_title", "New secret")
          : t("react.registry.secrets.edit_title", "Edit {value0}", { value0: editing?.name ?? "" })
      }
    >
      <form autoComplete="off" className="fs-dialog-form" id="secret-editor" noValidate onSubmit={submit}>
        {error ? <DsAlert tone="danger" title={t("react.registry.secrets.failed", "Not saved")}>{error}</DsAlert> : null}
        <label className="fs-field">
          <RequiredLabel>{t("react.registry.secrets.name", "Name")}</RequiredLabel>
          <input
            className="fs-input"
            maxLength={200}
            onChange={(event) => setEditor({ ...editor, name: event.target.value })}
            placeholder={t("react.registry.secrets.name_placeholder", "e.g. Shop API key")}
            required
            value={editor.name}
          />
        </label>
        <label className="fs-field">
          <OptionalLabel>{t("react.registry.secrets.description_label", "Description")}</OptionalLabel>
          <input
            className="fs-input"
            maxLength={2000}
            onChange={(event) => setEditor({ ...editor, description: event.target.value })}
            value={editor.description}
          />
        </label>
        {adding ? (
          <label className="fs-field">
            <RequiredLabel>{t("react.registry.secrets.value", "Value")}</RequiredLabel>
            <SensitiveInput
              ariaLabel={t("react.registry.secrets.value", "Value")}
              autoComplete="new-password"
              onChange={(event) => setEditor({ ...editor, value: event.target.value })}
              required
              value={editor.value}
            />
            <span className="fs-field-helper">{t("react.registry.secrets.value_helper", "Stored encrypted. Nobody can read it back, only replace it.")}</span>
          </label>
        ) : null}
      </form>
    </DsDialog>
  );
}

/** A new value for one secret; every connection and workflow input using it gets the new value. */
export function ReplaceSecretValueDialog({
  secret,
  saving,
  error,
  onClose,
  onSubmit,
}: Readonly<{
  secret: CatalogSecret | null;
  saving: boolean;
  error: string;
  onClose: () => void;
  onSubmit: (value: string) => void;
}>) {
  const { t } = useI18n();
  // The page keys this dialog by secret, so every secret starts from an empty value.
  const [value, setValue] = useState("");
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSubmit(value);
  };
  return (
    <DsDialog
      busy={saving}
      closeLabel={t("react.registry.secrets.close_dialog", "Close")}
      description={t("react.registry.secrets.replace_description", "Everything that uses this secret gets the new value. The old one cannot be shown.")}
      eyebrow={t("react.registry.secrets.title", "Secrets")}
      footer={
        <>
          <button className="fs-button" disabled={saving} onClick={onClose} type="button">
            {t("common.cancel", "Cancel")}
          </button>
          <button className="fs-button primary" disabled={saving || !value} form="secret-replace" type="submit">
            {t("react.registry.secrets.replace", "Replace value")}
          </button>
        </>
      }
      onClose={onClose}
      open={Boolean(secret)}
      title={t("react.registry.secrets.replace_title", "Replace the value of {value0}", { value0: secret?.name ?? "" })}
      variant="secret"
    >
      <form autoComplete="off" className="fs-dialog-form" id="secret-replace" noValidate onSubmit={submit}>
        {error ? <DsAlert tone="danger" title={t("react.registry.secrets.failed", "Not saved")}>{error}</DsAlert> : null}
        <label className="fs-field">
          <RequiredLabel>{t("react.registry.secrets.new_value", "New value")}</RequiredLabel>
          <SensitiveInput
            ariaLabel={t("react.registry.secrets.new_value", "New value")}
            autoComplete="new-password"
            onChange={(event) => setValue(event.target.value)}
            required
            value={value}
          />
        </label>
      </form>
    </DsDialog>
  );
}

function consumerLabel(usage: CatalogSecretUsage, t: (key: string, fallback: string) => string): string {
  const named = usage.consumer_name || usage.consumer_id || "";
  switch (usage.consumer_kind) {
    case "workflow_input":
      return `${t("react.registry.secrets.usage_workflow", "Workflow")} ${usage.workflow_name || usage.workflow_id || ""}`;
    case "data_connection":
      return `${t("react.registry.secrets.usage_data_connection", "Data connection")} ${named}`;
    case "mcp_connector":
      return `${t("react.registry.secrets.usage_mcp", "MCP connector")} ${named}`;
    case "llm_provider":
      return `${t("react.registry.secrets.usage_llm_provider", "LLM provider")} ${named}`;
    default:
      return `${t("react.registry.secrets.usage_connection", "Connection")} ${usage.connection_name || usage.connection_id || ""}`;
  }
}

function UsageLine({ usage }: Readonly<{ usage: CatalogSecretUsage }>) {
  const { t } = useI18n();
  const consumer = consumerLabel(usage, t);
  const label = `${consumer.trim()} · ${usage.field}`;
  const link = usageLink(usage);
  return (
    <li>
      {link ? <Link className="underline" to={link}>{label}</Link> : label}
      {usage.project_name ? <span className="fs-ink-tertiary"> — {usage.project_name}</span> : null}
    </li>
  );
}

/**
 * Delete one secret. Its uses are listed first; a used secret is deleted only after the
 * person confirms, and its bindings go with it.
 */
export function DeleteSecretDialog({
  secret,
  deleting,
  error,
  onClose,
  onConfirm,
}: Readonly<{
  secret: CatalogSecret | null;
  deleting: boolean;
  error: unknown;
  onClose: () => void;
  onConfirm: (planRevision: string) => void;
}>) {
  const { t } = useI18n();
  // An acknowledgement covers one plan revision: a changed list needs a new one.
  const [acknowledgedRevision, setAcknowledgedRevision] = useState("");
  const planQuery = useQuery({
    queryKey: ["secrets", "delete-plan", secret?.id ?? ""],
    queryFn: () => fetchCatalogSecretDeletePlan(secret?.id ?? ""),
    enabled: Boolean(secret),
  });
  const revision = planQuery.data?.revision ?? "";
  const acknowledged = Boolean(revision) && acknowledgedRevision === revision;
  const stale = error instanceof HttpError && error.status === 409;
  const { refetch } = planQuery;
  useEffect(() => {
    if (stale) void refetch();
  }, [stale, refetch]);
  const usages = planQuery.data?.usages ?? [];
  const used = usages.length > 0;
  const ready = Boolean(planQuery.data) && !planQuery.isFetching;
  return (
    <DsDialog
      busy={deleting}
      closeLabel={t("react.registry.secrets.close_dialog", "Close")}
      description={t("react.registry.secrets.delete_description", "The stored value is deleted for good.")}
      eyebrow={t("react.registry.secrets.title", "Secrets")}
      footer={
        <>
          <button className="fs-button" disabled={deleting} onClick={onClose} type="button">
            {t("common.cancel", "Cancel")}
          </button>
          <button
            className="fs-button danger"
            disabled={deleting || !ready || (used && !acknowledged)}
            onClick={() => onConfirm(revision)}
            type="button"
          >
            {used
              ? t("react.registry.secrets.delete_with_uses", "Delete and remove its uses")
              : t("react.registry.secrets.delete", "Delete")}
          </button>
        </>
      }
      onClose={onClose}
      open={Boolean(secret)}
      title={t("react.registry.secrets.delete_title", "Delete {value0}?", { value0: secret?.name ?? "" })}
      variant="confirm"
    >
      {planQuery.isError ? (
        <DsAlert tone="danger" title={t("react.registry.secrets.plan_failed", "Could not load where this secret is used")}>
          {errorText(planQuery.error)}
        </DsAlert>
      ) : null}
      {stale ? (
        <DsAlert tone="warning" title={t("react.registry.secrets.plan_changed", "Its uses changed")}>
          {t("react.registry.secrets.plan_changed_help", "Review the current list and confirm again.")}
        </DsAlert>
      ) : null}
      {error && !stale ? (
        <DsAlert tone="danger" title={t("react.registry.secrets.delete_failed", "Not deleted")}>{errorText(error)}</DsAlert>
      ) : null}
      {ready && used ? (
        <>
          <DsAlert tone="warning" title={t("react.registry.secrets.delete_used_title", "Used in {value0} place(s)", { value0: String(usages.length) })}>
            {t("react.registry.secrets.delete_used_help", "These connections and workflow inputs lose this secret and need another value before they run.")}
          </DsAlert>
          <ul className="list-disc space-y-1 pl-5 text-sm" data-testid="secret-delete-usages">
            {usages.map((usage) => (
              <UsageLine key={`${usage.consumer_kind}:${usage.connection_id ?? usage.workflow_id ?? usage.consumer_id}:${usage.field}`} usage={usage} />
            ))}
          </ul>
          <DsCheckbox checked={acknowledged} onChange={(checked) => setAcknowledgedRevision(checked ? revision : "")}>
            {t("react.registry.secrets.delete_acknowledge", "Remove the secret from these places and delete it")}
          </DsCheckbox>
        </>
      ) : null}
      {ready && !used ? (
        <p className="text-sm">{t("react.registry.secrets.delete_unused", "Nothing uses this secret.")}</p>
      ) : null}
    </DsDialog>
  );
}
