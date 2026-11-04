import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { SensitiveInput } from "@/components/shared/design-system-primitives";
import { createCatalogSecret } from "@/pages/registry/secrets/api";
import { StoredSecretSelect } from "@/pages/registry/secrets/stored-secret-select";
import {
  basicAuthRef,
  inputNameFromAuthPart,
  splitBasicAuthRef,
  storedSecretInputName,
  secretInputAuthRef,
} from "@/pages/workflows/detail/detail-http-auth-model";
import { createWorkflowInterfaceFieldDraft, type WorkflowInterfaceFieldDraft } from "@/pages/workflows/detail/detail-interface";
import {
  bindWorkflowInputSecret,
  fetchWorkflowSecretApproval,
  saveWorkflowInputSecretValue,
  workflowSecretApprovalQueryKey,
} from "@/pages/workflows/shared/workflow-secret-catalog-api";

type HttpAuthSecretProps = Readonly<{
  authMode: string;
  authRef: string;
  onAuthRefChange: (next: string) => void;
  /** The workflow's input rows on screen, saved and new. */
  inputs: WorkflowInterfaceFieldDraft[];
  /** Adds a secret input bound to a stored secret and saves the workflow settings. */
  addBoundInput?: (row: WorkflowInterfaceFieldDraft) => Promise<void>;
  /** The saved workflow in its project, where an input's stored secret is bound. */
  projectId?: string;
  /** Runs a task when the step is saved: a typed value is kept as a stored secret then. */
  registerSaveTask?: (key: string, task: (() => Promise<void>) | null) => void;
  workflowId?: string;
  inputClass: string;
  describedBy?: string;
  invalid?: boolean;
}>;

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

/**
 * Bearer or basic credentials of an HTTP step, chosen like a connection's credential
 * field: a stored secret from the catalog, or a value typed in, which is kept as a
 * stored secret. Behind the step each credential is a secret input of the workflow,
 * so the request carries it through the same checked boundary (ADR 0057). A legacy
 * project secret key typed earlier stays editable as text.
 */
export function HttpAuthSecretField(props: HttpAuthSecretProps) {
  const { authMode, authRef, onAuthRefChange } = props;
  const { t } = useI18n();
  const legacy = Boolean(authRef.trim()) && !authRef.includes("{{");
  if (legacy) {
    return (
      <>
        <input
          aria-describedby={props.describedBy}
          aria-invalid={props.invalid}
          aria-label={t("react.workflows.auth_reference", "Auth reference")}
          className={props.inputClass}
          onChange={(event) => onAuthRefChange(event.target.value)}
          value={authRef}
        />
        <span className="fs-field-helper">
          {t("react.workflows.http_auth_legacy", "A project secret key. Clear it to choose a stored secret instead.")}
        </span>
      </>
    );
  }
  if (authMode === "basic") {
    const [user, password] = splitBasicAuthRef(authRef);
    return (
      <div className="space-y-2">
        <AuthPart {...props} label={t("react.workflows.http_auth_username", "Username")} onChange={(next) => onAuthRefChange(basicAuthRef(next, password))} part={user} />
        <AuthPart {...props} label={t("react.workflows.http_auth_password", "Password")} onChange={(next) => onAuthRefChange(basicAuthRef(user, next))} part={password} />
      </div>
    );
  }
  return <AuthPart {...props} label={t("react.workflows.http_auth_token", "Token")} onChange={onAuthRefChange} part={authRef} />;
}

function AuthPart({
  label,
  part,
  onChange,
  inputs,
  addBoundInput,
  projectId = "",
  workflowId = "",
  registerSaveTask,
}: HttpAuthSecretProps & Readonly<{ label: string; part: string; onChange: (next: string) => void }>) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [typing, setTyping] = useState(false);
  const [typed, setTyped] = useState("");
  const inputName = inputNameFromAuthPart(part);
  const row = inputs.find((item) => item.name === inputName && item.type === "secret");
  // An input the saved workflow declares is bound and rebound by its own request.
  const saved = Boolean(row?.originalAuthoredRow) && Boolean(projectId && workflowId);
  const status = useQuery({
    queryKey: workflowSecretApprovalQueryKey(projectId, workflowId, null),
    queryFn: () => fetchWorkflowSecretApproval(projectId, workflowId),
    enabled: saved,
    retry: false,
  });
  const boundSecretId = (saved ? status.data?.secrets.find((item) => item.field === inputName)?.secret_id : "")
    || row?.catalogSecretId
    || "";
  const refresh = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: ["workflows", workflowId, "secret-approval"] }),
    queryClient.invalidateQueries({ queryKey: ["secrets"] }),
  ]);
  const choose = useMutation({
    mutationFn: async (secretId: string) => {
      if (saved && inputName) {
        await bindWorkflowInputSecret(projectId, workflowId, inputName, secretId);
        return;
      }
      if (!addBoundInput) return;
      const name = storedSecretInputName(label, inputs.map((item) => item.name));
      await addBoundInput(createWorkflowInterfaceFieldDraft({ name, label, type: "secret", catalogSecretId: secretId }));
      onChange(secretInputAuthRef(name));
    },
    onSuccess: async () => {
      setTyping(false);
      await refresh();
    },
  });
  // A typed value is kept when the step is saved, as a connection keeps one on save:
  // the input it fills is named now, so the saved step already refers to it.
  const plannedName = inputName || storedSecretInputName(label, inputs.map((item) => item.name));
  useEffect(() => {
    if (!registerSaveTask) return undefined;
    const key = `http-auth:${label}`;
    if (!typed) {
      registerSaveTask(key, null);
      return undefined;
    }
    registerSaveTask(key, async () => {
      if (saved && inputName) {
        // The server keeps the value as a catalog secret bound to the input.
        await saveWorkflowInputSecretValue(projectId, workflowId, inputName, typed);
      } else if (addBoundInput) {
        const secret = await createCatalogSecret({ name: `${workflowId || "workflow"} / ${plannedName}`, description: "", value: typed });
        await addBoundInput(createWorkflowInterfaceFieldDraft({ name: plannedName, label, type: "secret", catalogSecretId: secret.id }));
      }
      setTyped("");
      setTyping(false);
      await refresh();
    });
    return () => registerSaveTask(key, null);
  });
  const typeValue = (value: string) => {
    setTyped(value);
    if (value && !inputName) onChange(secretInputAuthRef(plannedName));
  };
  const busy = choose.isPending || status.isLoading;
  const chosen = typing ? "" : boundSecretId;
  const failure = errorText(choose.error || status.error);
  return (
    <div className="space-y-1">
      <span className="fs-ink-tertiary">{label}</span>
      {chosen ? null : (
        <SensitiveInput
          ariaLabel={t("react.workflows.http_auth_value_for", "Value of {field}", { field: label })}
          autoComplete="off"
          onChange={(event) => typeValue(event.target.value)}
          value={typed}
        />
      )}
      <StoredSecretSelect
        disabled={busy || (!saved && !addBoundInput)}
        fieldLabel={label}
        helper={
          failure
          || (chosen
            ? t("react.workflows.http_auth_uses_stored", "Uses this stored secret. Replace its value in Registry → Secrets.")
            : t("react.workflows.http_auth_value_on_save", "Saving the step keeps a typed value as a stored secret, so you can choose it elsewhere."))
        }
        onChange={(secretId) => (secretId ? choose.mutate(secretId) : setTyping(true))}
        value={chosen}
      />
    </div>
  );
}
