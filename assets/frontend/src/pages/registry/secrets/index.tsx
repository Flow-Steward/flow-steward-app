import { DsSearchInput } from "@/components/ui/search-input";
import { useHasCapability } from "@/lib/auth/capabilities";
import { useI18n } from "@/lib/i18n/useI18n";
import { useFilterState } from "@/lib/filters/use-filter-state";
import { z } from "@/lib/validation/zod";
import { formatUserDateTime } from "@/lib/time/user-timezone";
import { useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";

import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";

import {
  AdvancedTableFilters,
  DsAlert,
  DsPageHeader,
  PageContainer,
  Pagination,
  RowActionMenu,
} from "@/components/shared/design-system";
import {
  type CatalogSecret,
  createCatalogSecret,
  deleteCatalogSecret,
  CATALOG_SECRETS_QUERY_KEY,
  fetchCatalogSecrets,
  replaceCatalogSecretValue,
  updateCatalogSecret,
} from "@/pages/registry/secrets/api";
import {
  DeleteSecretDialog,
  ReplaceSecretValueDialog,
  SecretEditorDialog,
} from "@/pages/registry/secrets/secret-dialogs";
import {
  EMPTY_SECRET_EDITOR,
  type SecretEditorState,
} from "@/pages/registry/secrets/secret-form-model";
import {
  AdoptStoredSecretDialog,
  MoveSecretToLocalDialog,
  MoveSecretToStorageDialog,
  type SecretStorageAction,
} from "@/pages/registry/secrets/secret-storage-dialogs";
import {
  type SecretStorageConfig,
  SECRET_STORAGE_QUERY_KEY,
  adoptStoredSecret,
  fetchSecretStorage,
  moveSecretToLocal,
  moveSecretToStorage,
  usableStorages,
  writableStorages,
} from "@/pages/settings/secret-storage/api";

type SecretFilters = { search: string; usage: string };

const EMPTY_FILTERS: SecretFilters = { search: "", usage: "" };
const secretFiltersSchema = z.object({ search: z.string(), usage: z.enum(["", "used", "unused"]) });

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

function matches(secret: CatalogSecret, filters: SecretFilters): boolean {
  const query = filters.search.trim().toLowerCase();
  if (query && !`${secret.name} ${secret.description}`.toLowerCase().includes(query)) return false;
  const used = (secret.usage_count ?? 0) > 0;
  if (filters.usage === "used" && !used) return false;
  return !(filters.usage === "unused" && used);
}

function SecretRow({
  secret,
  menuOpen,
  setMenuOpen,
  onEdit,
  onReplace,
  onDelete,
  canDelete,
  keptIn,
  moveTargets,
  onMove,
  onMoveLocal,
}: Readonly<{
  secret: CatalogSecret;
  menuOpen: boolean;
  setMenuOpen: (open: boolean) => void;
  onEdit: () => void;
  onReplace: () => void;
  onDelete: () => void;
  /** ADR 0066 amendment: deleting unbinds every consumer, so it is the admin's. */
  canDelete: boolean;
  /** The external storage that keeps the value; `null` for one whose storage is gone; undefined when local. */
  keptIn?: SecretStorageConfig | null;
  /** Storages a local value may move to (account admin only). */
  moveTargets: SecretStorageConfig[];
  onMove: (storage: SecretStorageConfig) => void;
  onMoveLocal: (() => void) | null;
}>) {
  const { t } = useI18n();
  const uses = secret.usage_count ?? 0;
  const remote = keptIn !== undefined;
  const storageActions = remote
    ? onMoveLocal
      ? [{ key: "local", label: t("react.registry.secrets.storage.move_local", "Move back to this installation"), run: onMoveLocal }]
      : []
    : moveTargets.map((storage) => ({
        key: storage.id,
        label: t("react.registry.secrets.storage.move_to", "Move to {value0}", { value0: storage.name }),
        run: () => onMove(storage),
      }));
  return (
    <tr>
      <td>
        <span className="fs-ink-primary font-medium">{secret.name}</span>
        {secret.description ? <span className="block text-xs fs-ink-tertiary">{secret.description}</span> : null}
        {remote ? (
          <span className="block text-xs fs-ink-tertiary">
            {keptIn
              ? t("react.registry.secrets.storage.kept_in", "Kept in {value0}", { value0: keptIn.name })
              : t("react.registry.secrets.storage.kept_external", "Kept in an external storage")}
          </span>
        ) : null}
      </td>
      <td>
        {uses > 0
          ? t("react.registry.secrets.used_count", "{value0} place(s)", { value0: String(uses) })
          : <span className="fs-ink-tertiary">{t("react.registry.secrets.unused", "nothing yet")}</span>}
      </td>
      <td>
        {secret.value_updated_at ? formatUserDateTime(secret.value_updated_at) : ""}
        {secret.value_updated_by ? <span className="block text-xs fs-ink-tertiary">{secret.value_updated_by}</span> : null}
      </td>
      <td className="fs-table-action-cell">
        <RowActionMenu
          label={t("react.registry.secrets.actions_for", "Actions for {value0}", { value0: secret.name })}
          onOpenChange={setMenuOpen}
          open={menuOpen}
        >
          <div className="fs-row-menu-group">
            <button onClick={onEdit} role="menuitem" type="button">
              {t("react.registry.secrets.edit", "Edit details")}
            </button>
            <button onClick={onReplace} role="menuitem" type="button">
              {t("react.registry.secrets.replace", "Replace value")}
            </button>
          </div>
          {storageActions.length ? (
            <>
              <div className="fs-row-menu-separator" />
              <div className="fs-row-menu-group">
                {storageActions.map((action) => (
                  <button key={action.key} onClick={action.run} role="menuitem" type="button">
                    {action.label}
                  </button>
                ))}
              </div>
            </>
          ) : null}
          {canDelete ? (
            <>
              <div className="fs-row-menu-separator" />
              <div className="fs-row-menu-group">
                <button className="danger" onClick={onDelete} role="menuitem" type="button">
                  {t("react.registry.secrets.delete", "Delete")}
                </button>
              </div>
            </>
          ) : null}
        </RowActionMenu>
      </td>
    </tr>
  );
}

/**
 * Registry → Secrets (ADR 0057). The same inventory as Users: a filtered table, a row
 * menu and dialogs. A secret's value is write-only: this page never receives one.
 */
export function RegistrySecretsPage() {
  const { t } = useI18n();
  const canDelete = useHasCapability("administer_platform");
  const queryClient = useQueryClient();
  const [filters, setFilters] = useFilterState<SecretFilters>("secrets/filters", secretFiltersSchema, EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(10);
  const [menuFor, setMenuFor] = useState("");
  const [editorMode, setEditorMode] = useState<"closed" | "add" | "edit">("closed");
  const [editing, setEditing] = useState<CatalogSecret | null>(null);
  const [editor, setEditor] = useState<SecretEditorState>(EMPTY_SECRET_EDITOR);
  const [editorError, setEditorError] = useState("");
  const [replacing, setReplacing] = useState<CatalogSecret | null>(null);
  const [deleting, setDeleting] = useState<CatalogSecret | null>(null);
  const [feedback, setFeedback] = useState("");

  const secretsQuery = useQuery({ queryKey: CATALOG_SECRETS_QUERY_KEY, queryFn: fetchCatalogSecrets });
  // Where values are kept is the account admin's; without access or an add-on the page is unchanged.
  const storageQuery = useQuery({
    queryKey: SECRET_STORAGE_QUERY_KEY,
    queryFn: fetchSecretStorage,
    enabled: canDelete,
    retry: false,
  });
  const storage = storageQuery.isError ? undefined : storageQuery.data;
  const storagesById = new Map((storage?.configs ?? []).map((config) => [config.id, config]));
  const remoteBySecret = new Map((storage?.secrets ?? []).map((row) => [row.secret_id, storagesById.get(row.config_id) ?? null]));
  const moveTargets = writableStorages(storage);
  const adoptFrom = usableStorages(storage);
  const [storageAction, setStorageAction] = useState<SecretStorageAction | null>(null);
  const secretsLoaderShown = useLoaderDelay(secretsQuery.isLoading);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["secrets"] });

  const rows = (secretsQuery.data ?? []).filter((secret) => matches(secret, filters));
  const visible = rows.slice((page - 1) * perPage, page * perPage);
  const updateFilters = (next: Partial<SecretFilters>) => {
    setFilters((current) => ({ ...current, ...next }));
    setPage(1);
  };

  const save = useMutation({
    mutationFn: async () => {
      if (editorMode === "edit" && editing) {
        return updateCatalogSecret(editing.id, { name: editor.name.trim(), description: editor.description });
      }
      return createCatalogSecret({ name: editor.name.trim(), description: editor.description, value: editor.value });
    },
    onSuccess: async () => {
      setFeedback(
        editorMode === "add"
          ? t("react.registry.secrets.created_message", "Secret {value0} created.", { value0: editor.name.trim() })
          : t("react.registry.secrets.updated", "Secret {value0} updated.", { value0: editor.name.trim() }),
      );
      setEditorMode("closed");
      await refresh();
    },
    onError: (error) => setEditorError(errorText(error)),
  });
  const replace = useMutation({
    mutationFn: (value: string) => replaceCatalogSecretValue(replacing?.id ?? "", value),
    onSuccess: async () => {
      setFeedback(t("react.registry.secrets.value_replaced", "The value of {value0} was replaced.", { value0: replacing?.name ?? "" }));
      setReplacing(null);
      await refresh();
    },
  });
  const remove = useMutation({
    mutationFn: (planRevision: string) => deleteCatalogSecret(deleting?.id ?? "", planRevision),
    onSuccess: async () => {
      setFeedback(t("react.registry.secrets.deleted", "Secret {value0} deleted.", { value0: deleting?.name ?? "" }));
      setDeleting(null);
      await refresh();
    },
  });

  const storageMutation = useMutation({
    mutationFn: async (input: { nameInStorage?: string; replace?: boolean; adopt?: Parameters<typeof adoptStoredSecret>[0] }) => {
      if (storageAction?.kind === "move") {
        await moveSecretToStorage(storageAction.secret.id, storageAction.storage.id, input.nameInStorage ?? "", input.replace === true);
        return t("react.registry.secrets.storage.moved", "{value0} is now kept in {value1}.", {
          value0: storageAction.secret.name,
          value1: storageAction.storage.name,
        });
      }
      if (storageAction?.kind === "move-local") {
        await moveSecretToLocal(storageAction.secret.id);
        return t("react.registry.secrets.storage.moved_local", "{value0} is kept in this installation again.", {
          value0: storageAction.secret.name,
        });
      }
      if (input.adopt) {
        const adopted = await adoptStoredSecret(input.adopt);
        return t("react.registry.secrets.created_message", "Secret {value0} created.", { value0: adopted.name });
      }
      return "";
    },
    onSuccess: async (message) => {
      setFeedback(message);
      setStorageAction(null);
      await refresh();
    },
  });
  const openStorageAction = (action: SecretStorageAction) => {
    setMenuFor("");
    storageMutation.reset();
    setStorageAction(action);
  };

  const validationError = (): string => {
    if (!editor.name.trim()) return t("react.registry.secrets.name_required", "Enter a name.");
    if (editorMode === "add" && !editor.value) return t("react.registry.secrets.value_required", "Enter the value.");
    return "";
  };
  const submitEditor = () => {
    const problem = validationError();
    setEditorError(problem);
    if (!problem) save.mutate();
  };
  const openAdd = () => {
    setEditing(null);
    setEditor(EMPTY_SECRET_EDITOR);
    setEditorError("");
    setEditorMode("add");
  };
  const openEdit = (secret: CatalogSecret) => {
    setMenuFor("");
    setEditing(secret);
    setEditor({ ...EMPTY_SECRET_EDITOR, name: secret.name, description: secret.description });
    setEditorError("");
    setEditorMode("edit");
  };

  return (
    <PageContainer width="wide">
      <section className="fs-panel-stack">
      <DsPageHeader
        action={
          <span className="fs-footer-actions">
            {adoptFrom.length ? (
              <button className="fs-button" onClick={() => openStorageAction({ kind: "adopt", storages: adoptFrom })} type="button">
                {adoptFrom.length === 1
                  ? t("react.registry.secrets.storage.adopt_from", "Use an existing secret from {value0}", { value0: adoptFrom[0]?.name ?? "" })
                  : t("react.registry.secrets.storage.adopt_title", "Use an existing secret")}
              </button>
            ) : null}
            <button className="fs-button primary" onClick={openAdd} type="button">
              <Plus aria-hidden="true" />
              {t("react.registry.secrets.create_title", "New secret")}
            </button>
          </span>
        }
        description={t("react.registry.secrets.page_description_shared", "Create a secret once and choose it wherever a key or password is asked for, in any project. Replace it here and everything that uses it gets the new value. Stored values are never shown again.")}
        eyebrow={t("shell.nav.registry", "Registry")}
        title={t("react.registry.secrets.title", "Secrets")}
      />
      {feedback ? <DsAlert tone="success" title={t("react.registry.secrets.done", "Done")}>{feedback}</DsAlert> : null}
      <article className="fs-card fs-table-card">
        <div className="fs-table-toolbar">
          <AdvancedTableFilters
            disabled={secretsQuery.isLoading}
            fields={[
              {
                key: "usage",
                label: t("react.registry.secrets.usage", "Use"),
                options: [
                  { value: "used", label: t("react.registry.secrets.filter_used", "In use") },
                  { value: "unused", label: t("react.registry.secrets.filter_unused", "Not used") },
                ],
              },
            ]}
            onChange={(key, value) => updateFilters({ [key]: value })}
            onClear={() => updateFilters({ usage: "" })}
            values={{ usage: filters.usage }}
          />
          <DsSearchInput
            aria-label={t("react.registry.secrets.search", "Search secrets")}
            onValueChange={(value) => updateFilters({ search: value })}
            placeholder={t("react.registry.secrets.search_placeholder", "Search secrets…")}
            value={filters.search}
          />
          <span aria-live="polite" className="fs-management-result-announcement">
            {t("react.registry.secrets.count", "Secrets: {value0}", { value0: String(rows.length) })}
          </span>
        </div>
        <div className="fs-table-scroll">
          <table
            aria-busy={secretsQuery.isFetching}
            aria-label={t("react.registry.secrets.title", "Secrets")}
            className="fs-table fs-management-table"
          >
            <thead>
              <tr>
                <th>{t("react.registry.secrets.name", "Name")}</th>
                <th>{t("react.registry.secrets.used_by", "Used by")}</th>
                <th>{t("react.registry.secrets.value_changed_column", "Value changed")}</th>
                <th aria-label={t("monitoring.table.actions", "Actions")} />
              </tr>
            </thead>
            <tbody>
              {visible.map((secret) => (
                <SecretRow
                  canDelete={canDelete}
                  key={secret.id}
                  menuOpen={menuFor === secret.id}
                  onDelete={() => {
                    setMenuFor("");
                    remove.reset();
                    setDeleting(secret);
                  }}
                  onEdit={() => openEdit(secret)}
                  keptIn={remoteBySecret.has(secret.id) ? remoteBySecret.get(secret.id) : undefined}
                  moveTargets={moveTargets}
                  onMove={(target) => openStorageAction({ kind: "move", secret, storage: target })}
                  onMoveLocal={
                    canDelete && remoteBySecret.has(secret.id)
                      ? () => openStorageAction({ kind: "move-local", secret, storage: remoteBySecret.get(secret.id) ?? null })
                      : null
                  }
                  onReplace={() => {
                    setMenuFor("");
                    replace.reset();
                    setReplacing(secret);
                  }}
                  secret={secret}
                  setMenuOpen={(open) => setMenuFor(open ? secret.id : "")}
                />
              ))}
            </tbody>
          </table>
          {secretsLoaderShown ? (
            <div className="fs-table-state"><DsLoader label={t("react.registry.secrets.loading", "Loading secrets…")} variant="wide-block" /></div>
          ) : null}
          {secretsQuery.isError ? (
            <div className="fs-table-state">
              <DsAlert tone="danger" title={t("react.registry.secrets.load_failed", "Could not load secrets")}>
                {errorText(secretsQuery.error)}
              </DsAlert>
            </div>
          ) : null}
          {!secretsQuery.isLoading && !secretsLoaderShown && !secretsQuery.isError && rows.length === 0 ? (
            <div className="fs-table-state">
              {secretsQuery.data?.length
                ? t("react.registry.secrets.no_match", "No secret matches these filters.")
                : t("react.registry.secrets.empty", "No secrets yet. Create one with New secret.")}
            </div>
          ) : null}
        </div>
        <Pagination
          onPageChange={setPage}
          onPerPageChange={(next) => {
            setPerPage(next);
            setPage(1);
          }}
          page={page}
          perPage={perPage}
          total={rows.length}
        />
      </article>
      <SecretEditorDialog
        editing={editing}
        editor={editor}
        error={editorError}
        mode={editorMode}
        onClose={() => setEditorMode("closed")}
        onSubmit={submitEditor}
        saving={save.isPending}
        setEditor={setEditor}
      />
      <ReplaceSecretValueDialog
        key={replacing?.id ?? "none"}
        error={errorText(replace.error)}
        onClose={() => setReplacing(null)}
        onSubmit={(value) => replace.mutate(value)}
        saving={replace.isPending}
        secret={replacing}
      />
      <MoveSecretToStorageDialog
        action={storageAction?.kind === "move" ? storageAction : null}
        error={storageMutation.error}
        key={storageAction?.kind === "move" ? `${storageAction.secret.id}:${storageAction.storage.id}` : "move-none"}
        onClose={() => setStorageAction(null)}
        onSubmit={(nameInStorage, replace) => storageMutation.mutate({ nameInStorage, replace })}
        saving={storageMutation.isPending}
      />
      <MoveSecretToLocalDialog
        action={storageAction?.kind === "move-local" ? storageAction : null}
        error={errorText(storageMutation.error)}
        onClose={() => setStorageAction(null)}
        onConfirm={() => storageMutation.mutate({})}
        saving={storageMutation.isPending}
      />
      <AdoptStoredSecretDialog
        action={storageAction?.kind === "adopt" ? storageAction : null}
        error={errorText(storageMutation.error)}
        key={storageAction?.kind === "adopt" ? "adopt-open" : "adopt-none"}
        onClose={() => setStorageAction(null)}
        onSubmit={(adopt) => storageMutation.mutate({ adopt })}
        saving={storageMutation.isPending}
      />
      <DeleteSecretDialog
        deleting={remove.isPending}
        error={remove.error}
        onClose={() => setDeleting(null)}
        onConfirm={(planRevision) => remove.mutate(planRevision)}
        secret={deleting}
      />
      </section>
    </PageContainer>
  );
}
