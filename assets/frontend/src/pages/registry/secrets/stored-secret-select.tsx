import { useI18n } from "@/lib/i18n/useI18n";
import { useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus } from "lucide-react";

import { DsSelect } from "@/components/shared/design-system-selects";
import { Button } from "@/components/ui/button";
import {
  CATALOG_SECRETS_QUERY_KEY,
  createCatalogSecret,
  fetchCatalogSecrets,
} from "@/pages/registry/secrets/api";
import { SecretEditorDialog } from "@/pages/registry/secrets/secret-dialogs";
import { EMPTY_SECRET_EDITOR, type SecretEditorState } from "@/pages/registry/secrets/secret-form-model";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

type StoredSecretSelectProps = Readonly<{
  /** The field the secret is for, so every control is named after it. */
  fieldLabel: string;
  /** The chosen secret id, or "" when the field takes a typed value. */
  value: string;
  onChange: (secretId: string) => void;
  disabled?: boolean;
  /** What happens with the choice, under the list (for example: applied on save). */
  helper?: string;
}>;

/**
 * Choose one of the installation's stored secrets for a field, or create one in the
 * standard New secret dialog and choose it at once. Only names are listed: values are
 * write-only. The choice is the caller's state; binding it is the caller's request.
 */
export function StoredSecretSelect({ fieldLabel, value, onChange, disabled = false, helper }: StoredSecretSelectProps) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [editor, setEditor] = useState<SecretEditorState>(EMPTY_SECRET_EDITOR);
  const [editorError, setEditorError] = useState("");
  const secretsQuery = useQuery({ queryKey: CATALOG_SECRETS_QUERY_KEY, queryFn: fetchCatalogSecrets });
  const create = useMutation({
    mutationFn: () => createCatalogSecret({ name: editor.name.trim(), description: editor.description, value: editor.value }),
    onSuccess: async (created) => {
      setCreating(false);
      await queryClient.invalidateQueries({ queryKey: ["secrets"] });
      onChange(created.id);
    },
    onError: (error) => setEditorError(errorText(error)),
  });
  const secrets = secretsQuery.data ?? [];
  const submit = () => {
    const problem = !editor.name.trim()
      ? t("react.registry.secrets.name_required", "Enter a name.")
      : !editor.value
        ? t("react.registry.secrets.value_required", "Enter the value.")
        : "";
    setEditorError(problem);
    if (!problem) create.mutate();
  };
  return (
    <div className="mt-1 space-y-1 text-xs fs-ink-secondary" data-testid="stored-secret-select">
      <div className="flex flex-wrap items-center gap-2">
        <KeyRound aria-hidden="true" className="h-3.5 w-3.5" />
        <DsSelect
          ariaLabel={t("react.secret_catalog.choose_for", "Stored secret for {field}", { field: fieldLabel })}
          disabled={disabled || secretsQuery.isLoading}
          emptyLabel={t("react.secret_catalog.none_yet", "No stored secrets yet")}
          onChange={onChange}
          options={secrets.map((secret) => ({ value: secret.id, label: secret.name, description: secret.description || undefined }))}
          placeholder={t("react.secret_catalog.choose", "Choose a stored secret")}
          searchable
          value={value}
        />
        {value ? (
          <Button
            aria-label={t("react.secret_catalog.remove_for", "Stop using the stored secret for {field}", { field: fieldLabel })}
            disabled={disabled}
            onClick={() => onChange("")}
            size="sm"
            type="button"
            variant="ghost"
          >
            {t("react.secret_catalog.type_instead", "Type a value instead")}
          </Button>
        ) : (
          <Button
            aria-label={t("react.secret_catalog.new_for", "New secret for {field}", { field: fieldLabel })}
            disabled={disabled}
            onClick={() => {
              setEditor({ ...EMPTY_SECRET_EDITOR, name: fieldLabel });
              setEditorError("");
              setCreating(true);
            }}
            size="sm"
            type="button"
            variant="ghost"
          >
            <Plus className="h-3.5 w-3.5" />
            {t("react.registry.secrets.create_title", "New secret")}
          </Button>
        )}
      </div>
      {helper ? <p className="fs-field-helper">{helper}</p> : null}
      {secretsQuery.isError ? <p className="fs-field-helper error">{errorText(secretsQuery.error)}</p> : null}
      <SecretEditorDialog
        editing={null}
        editor={editor}
        error={editorError}
        mode={creating ? "add" : "closed"}
        onClose={() => setCreating(false)}
        onSubmit={submit}
        saving={create.isPending}
        setEditor={setEditor}
      />
    </div>
  );
}
