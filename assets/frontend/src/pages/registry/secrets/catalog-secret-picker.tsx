import { useI18n } from "@/lib/i18n/useI18n";
import { useId, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, KeyRound } from "lucide-react";

import { DsAlert, DsBadge, DsValueChip, SensitiveInput } from "@/components/shared/design-system-primitives";
import { DsSelect } from "@/components/shared/design-system-selects";
import { Button } from "@/components/ui/button";
import { catalogSecretsQueryKey, createCatalogSecret, fetchCatalogSecrets } from "@/pages/registry/secrets/api";

const REGISTRY_SECRETS_PATH = "/registry/secrets";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

type CatalogSecretPickerProps = Readonly<{
  projectId: string;
  /** The field the secret is for, so every control is named after it. */
  fieldLabel: string;
  /** The secret bound now, or "" when none is. */
  boundSecretId: string;
  busy?: boolean;
  /** The last bind or unbind failure, shown above the controls. */
  error?: unknown;
  /** Offer a name and a value to create a project secret and use it at once. */
  allowCreate?: boolean;
  onChoose: (secretId: string) => Promise<unknown>;
  onRemove: () => void;
}>;

/**
 * Choose a stored secret from the catalog for one field, or create one in place.
 *
 * Values are write-only: the list carries names, and a value typed here goes to
 * the catalog once and is never read back. Choosing only names the secret; the
 * binding itself is the caller's own request.
 */
export function CatalogSecretPicker({
  projectId,
  fieldLabel,
  boundSecretId,
  busy = false,
  error,
  allowCreate = false,
  onChoose,
  onRemove,
}: CatalogSecretPickerProps) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const nameId = useId();
  const [chosen, setChosen] = useState("");
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const secretsQuery = useQuery({
    queryKey: catalogSecretsQueryKey(projectId),
    queryFn: () => fetchCatalogSecrets(projectId),
    enabled: Boolean(projectId),
  });
  const secrets = secretsQuery.data ?? [];
  const bound = secrets.find((secret) => secret.id === boundSecretId);
  const create = useMutation({
    mutationFn: async () => {
      const created = await createCatalogSecret({ name: name.trim(), description: "", value, project_id: projectId });
      await onChoose(created.id);
      return created;
    },
    onSuccess: () => {
      setName("");
      setValue("");
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["secrets"] }),
  });
  const failure = errorText(error) || errorText(create.error) || errorText(secretsQuery.error);
  const ownerLabel = (owner: string) =>
    owner === "workspace"
      ? t("react.secret_catalog.owner_workspace", "workspace")
      : t("react.secret_catalog.owner_project", "project");
  const options = secrets.map((secret) => ({
    value: secret.id,
    label: secret.name,
    description: ownerLabel(secret.owner),
  }));
  const pending = busy || create.isPending;

  return (
    <div className="mt-2 space-y-2 text-xs fs-ink-secondary" data-testid="catalog-secret-picker">
      {failure ? (
        <DsAlert tone="danger" title={t("react.secret_catalog.failed", "The stored secret was not changed")}>
          {failure}
        </DsAlert>
      ) : null}
      {boundSecretId ? (
        <div className="flex flex-wrap items-center gap-2">
          <KeyRound aria-hidden="true" className="h-3.5 w-3.5" />
          <span>{t("react.secret_catalog.bound", "Uses stored secret")}</span>
          <DsValueChip>{bound?.name || boundSecretId}</DsValueChip>
          {bound ? <DsBadge>{ownerLabel(bound.owner)}</DsBadge> : null}
          <Button
            aria-label={t("react.secret_catalog.remove_for", "Stop using the stored secret for {field}", { field: fieldLabel })}
            disabled={pending}
            onClick={onRemove}
            size="sm"
            type="button"
            variant="ghost-danger"
          >
            {t("react.secret_catalog.remove", "Stop using")}
          </Button>
        </div>
      ) : null}
      <div className="flex flex-wrap items-end gap-2">
        <DsSelect
          ariaLabel={t("react.secret_catalog.choose_for", "Stored secret for {field}", { field: fieldLabel })}
          disabled={pending || secretsQuery.isLoading}
          emptyLabel={t("react.secret_catalog.none_available", "No stored secrets are available to this project")}
          onChange={setChosen}
          options={options.filter((option) => option.value !== boundSecretId)}
          placeholder={t("react.secret_catalog.choose", "Choose a stored secret")}
          value={chosen}
        />
        <Button
          disabled={pending || !chosen || chosen === boundSecretId}
          onClick={() => {
            void onChoose(chosen).then(() => setChosen(""), () => undefined);
          }}
          size="sm"
          type="button"
          variant="secondary"
        >
          {boundSecretId
            ? t("react.secret_catalog.use_instead", "Use instead")
            : t("react.secret_catalog.use", "Use this secret")}
        </Button>
      </div>
      {allowCreate ? (
        <fieldset className="space-y-1">
          <legend className="fs-ink-tertiary">{t("react.secret_catalog.create_legend", "Or create a project secret")}</legend>
          <div className="flex flex-wrap items-end gap-2">
            <label className="block" htmlFor={nameId}>
              <span className="fs-ink-tertiary">{t("react.secret_catalog.create_name", "Secret name")}</span>
              <input className="fs-input" id={nameId} onChange={(event) => setName(event.target.value)} value={name} />
            </label>
            <label className="block">
              <span className="fs-ink-tertiary">{t("react.secret_catalog.create_value", "Value")}</span>
              <SensitiveInput
                ariaLabel={t("react.secret_catalog.create_value_for", "Value of the new secret for {field}", { field: fieldLabel })}
                autoComplete="off"
                onChange={(event) => setValue(event.target.value)}
                value={value}
              />
            </label>
            <Button
              disabled={pending || !name.trim() || !value}
              onClick={() => create.mutate()}
              size="sm"
              type="button"
              variant="secondary"
            >
              {t("react.secret_catalog.create_and_use", "Create and use")}
            </Button>
          </div>
        </fieldset>
      ) : null}
      <a className="inline-flex items-center gap-1 fs-ink-brand underline" href={REGISTRY_SECRETS_PATH} rel="noreferrer" target="_blank">
        {t("react.secret_catalog.manage", "Manage stored secrets")}
        <ExternalLink aria-hidden="true" className="h-3 w-3" />
      </a>
    </div>
  );
}
