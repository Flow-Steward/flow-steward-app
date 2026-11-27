import { useI18n } from "@/lib/i18n/useI18n";
import { useState, type SubmitEvent } from "react";

import { DsAlert, DsCheckbox, DsDialog, DsSelect, OptionalLabel, RequiredLabel } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { isHttpError } from "@/lib/http/errors";
import type { CatalogSecret } from "@/pages/registry/secrets/api";
import type { SecretStorageConfig } from "@/pages/settings/secret-storage/api";

/** What a storage action on the Secrets page is about to do. */
export type SecretStorageAction =
  | { kind: "move"; secret: CatalogSecret; storage: SecretStorageConfig }
  | { kind: "move-local"; secret: CatalogSecret; storage: SecretStorageConfig | null }
  | { kind: "adopt"; storages: SecretStorageConfig[] };

/** The server's 409 when a secret already exists under that name in the storage. */
function isTargetExists(error: unknown): boolean {
  return isHttpError(error) && error.status === 409 && /already exists/i.test(error.message);
}

/**
 * Move a secret's value to an external storage. The one thing to decide is the name
 * it gets there; it starts as the secret's own name. A secret already kept under
 * that name in the storage is overwritten only when the admin ticks "Replace".
 */
export function MoveSecretToStorageDialog({
  action,
  saving,
  error,
  onClose,
  onSubmit,
}: Readonly<{
  action: Extract<SecretStorageAction, { kind: "move" }> | null;
  saving: boolean;
  error: unknown;
  onClose: () => void;
  onSubmit: (nameInStorage: string, replace: boolean) => void;
}>) {
  const { t } = useI18n();
  // The page keys this dialog by secret and storage, so each starts from the secret's name.
  const [nameInStorage, setNameInStorage] = useState(action?.secret.name ?? "");
  const [replace, setReplace] = useState(false);
  const storageName = action?.storage.name ?? "";
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (nameInStorage.trim()) onSubmit(nameInStorage.trim(), replace);
  };
  let errorMessage = "";
  if (isTargetExists(error)) {
    errorMessage = t(
      "react.registry.secrets.storage.target_exists",
      "A secret with this name already exists in {value0}. Choose another name or replace it.",
      { value0: storageName },
    );
  } else if (error instanceof Error) {
    errorMessage = error.message;
  }
  return (
    <DsDialog
      busy={saving}
      closeLabel={t("react.registry.secrets.close_dialog", "Close")}
      description={t(
        "react.registry.secrets.storage.move_description",
        "The value is written to {value0} and removed from this installation. Everything that uses the secret keeps working.",
        { value0: storageName },
      )}
      eyebrow={t("react.registry.secrets.title", "Secrets")}
      footer={
        <>
          <button className="fs-button" disabled={saving} onClick={onClose} type="button">
            {t("common.cancel", "Cancel")}
          </button>
          <Button busy={saving} busyLabel={t("react.registry.secrets.storage.moving", "Moving…")} disabled={!nameInStorage.trim()} form="secret-move" type="submit" variant="primary">
            {t("react.registry.secrets.storage.move", "Move")}
          </Button>
        </>
      }
      onClose={onClose}
      open={Boolean(action)}
      title={t("react.registry.secrets.storage.move_title", "Move {value0} to {value1}", {
        value0: action?.secret.name ?? "",
        value1: storageName,
      })}
    >
      <form autoComplete="off" className="fs-dialog-form" id="secret-move" noValidate onSubmit={submit}>
        {errorMessage ? <DsAlert tone="danger" title={t("react.registry.secrets.storage.not_moved", "Not moved")}>{errorMessage}</DsAlert> : null}
        <label className="fs-field">
          <RequiredLabel>{t("react.registry.secrets.storage.name_in", "Name in {value0}", { value0: storageName })}</RequiredLabel>
          <input
            className="fs-input"
            maxLength={500}
            onChange={(event) => setNameInStorage(event.target.value)}
            required
            value={nameInStorage}
          />
        </label>
        <DsCheckbox checked={replace} onChange={setReplace}>
          {t("react.registry.secrets.storage.replace_existing", "Replace the existing secret in {value0}", { value0: storageName })}
        </DsCheckbox>
        {replace ? (
          <DsAlert tone="warning" title={t("react.registry.secrets.storage.replace_warning_title", "The existing secret is overwritten")}>
            {t(
              "react.registry.secrets.storage.replace_warning",
              "If {value0} already keeps a secret under this name, its value is replaced and cannot be restored from here. Anything else that reads it gets this value.",
              { value0: storageName },
            )}
          </DsAlert>
        ) : null}
      </form>
    </DsDialog>
  );
}

/** Copy a value back into this installation. The copy in the storage is not deleted. */
export function MoveSecretToLocalDialog({
  action,
  saving,
  error,
  onClose,
  onConfirm,
}: Readonly<{
  action: Extract<SecretStorageAction, { kind: "move-local" }> | null;
  saving: boolean;
  error: string;
  onClose: () => void;
  onConfirm: () => void;
}>) {
  const { t } = useI18n();
  const storageName = action?.storage?.name ?? t("react.registry.secrets.storage.external", "the external storage");
  return (
    <DsDialog
      busy={saving}
      closeLabel={t("react.registry.secrets.close_dialog", "Close")}
      description={t(
        "react.registry.secrets.storage.move_local_description",
        "The value is read from {value0} and kept encrypted in this installation again. The copy in {value0} is not deleted.",
        { value0: storageName },
      )}
      eyebrow={t("react.registry.secrets.title", "Secrets")}
      footer={
        <>
          <button className="fs-button" disabled={saving} onClick={onClose} type="button">
            {t("common.cancel", "Cancel")}
          </button>
          <Button busy={saving} busyLabel={t("react.registry.secrets.storage.moving", "Moving…")} onClick={onConfirm} type="button" variant="primary">
            {t("react.registry.secrets.storage.move_back", "Move back")}
          </Button>
        </>
      }
      onClose={onClose}
      open={Boolean(action)}
      title={t("react.registry.secrets.storage.move_local_title", "Move {value0} back to this installation?", {
        value0: action?.secret.name ?? "",
      })}
      variant="confirm"
    >
      {error ? <DsAlert tone="danger" title={t("react.registry.secrets.storage.not_moved", "Not moved")}>{error}</DsAlert> : null}
    </DsDialog>
  );
}

/**
 * Use a secret that already exists in an external storage: its value is checked
 * there and never copied here. The storage is asked only when there is a choice.
 */
export function AdoptStoredSecretDialog({
  action,
  saving,
  error,
  onClose,
  onSubmit,
}: Readonly<{
  action: Extract<SecretStorageAction, { kind: "adopt" }> | null;
  saving: boolean;
  error: string;
  onClose: () => void;
  onSubmit: (payload: { configId: string; nameInStorage: string; name: string; description: string }) => void;
}>) {
  const { t } = useI18n();
  const storages = action?.storages ?? [];
  const [configId, setConfigId] = useState(storages[0]?.id ?? "");
  const [nameInStorage, setNameInStorage] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const storage = storages.find((candidate) => candidate.id === configId) ?? storages[0];
  const storageName = storage?.name ?? "";
  const ready = Boolean(storage) && Boolean(nameInStorage.trim());
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!ready || !storage) return;
    onSubmit({
      configId: storage.id,
      nameInStorage: nameInStorage.trim(),
      // Without a name of its own the secret is called as it is there.
      name: name.trim() || nameInStorage.trim(),
      description,
    });
  };
  return (
    <DsDialog
      busy={saving}
      closeLabel={t("react.registry.secrets.close_dialog", "Close")}
      description={t(
        "react.registry.secrets.storage.adopt_description",
        "The secret stays where it is. Its value is checked once and never copied here; only where it is kept is remembered.",
      )}
      eyebrow={t("react.registry.secrets.title", "Secrets")}
      footer={
        <>
          <button className="fs-button" disabled={saving} onClick={onClose} type="button">
            {t("common.cancel", "Cancel")}
          </button>
          <Button busy={saving} busyLabel={t("react.settings.saving", "Saving…")} disabled={!ready} form="secret-adopt" type="submit" variant="primary">
            {t("react.registry.secrets.storage.adopt", "Use this secret")}
          </Button>
        </>
      }
      onClose={onClose}
      open={Boolean(action)}
      title={
        storages.length === 1
          ? t("react.registry.secrets.storage.adopt_from", "Use an existing secret from {value0}", { value0: storageName })
          : t("react.registry.secrets.storage.adopt_title", "Use an existing secret")
      }
    >
      <form autoComplete="off" className="fs-dialog-form" id="secret-adopt" noValidate onSubmit={submit}>
        {error ? <DsAlert tone="danger" title={t("react.registry.secrets.failed", "Not saved")}>{error}</DsAlert> : null}
        {storages.length > 1 ? (
          <div className="fs-field">
            <RequiredLabel>{t("react.registry.secrets.storage.storage", "Storage")}</RequiredLabel>
            <DsSelect
              ariaLabel={t("react.registry.secrets.storage.storage", "Storage")}
              onChange={setConfigId}
              options={storages.map((candidate) => ({ value: candidate.id, label: candidate.name }))}
              required
              value={configId}
            />
          </div>
        ) : null}
        <label className="fs-field">
          <RequiredLabel>{t("react.registry.secrets.storage.name_in", "Name in {value0}", { value0: storageName })}</RequiredLabel>
          <input
            className="fs-input"
            maxLength={500}
            onChange={(event) => setNameInStorage(event.target.value)}
            required
            value={nameInStorage}
          />
        </label>
        <label className="fs-field">
          <OptionalLabel>{t("react.registry.secrets.name", "Name")}</OptionalLabel>
          <input
            className="fs-input"
            maxLength={200}
            onChange={(event) => setName(event.target.value)}
            placeholder={nameInStorage}
            value={name}
          />
          <span className="fs-field-helper">
            {t("react.registry.secrets.storage.name_helper", "How it is called here. Leave blank to use the name in the storage.")}
          </span>
        </label>
        <label className="fs-field">
          <OptionalLabel>{t("react.registry.secrets.description_label", "Description")}</OptionalLabel>
          <input className="fs-input" maxLength={2000} onChange={(event) => setDescription(event.target.value)} value={description} />
        </label>
      </form>
    </DsDialog>
  );
}
