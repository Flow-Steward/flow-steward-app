import { useI18n } from "@/lib/i18n/useI18n";
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
  Search,
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

type SecretFilters = { search: string; usage: string };

const EMPTY_FILTERS: SecretFilters = { search: "", usage: "" };

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
}: Readonly<{
  secret: CatalogSecret;
  menuOpen: boolean;
  setMenuOpen: (open: boolean) => void;
  onEdit: () => void;
  onReplace: () => void;
  onDelete: () => void;
}>) {
  const { t } = useI18n();
  const uses = secret.usage_count ?? 0;
  return (
    <tr>
      <td>
        <span className="fs-ink-primary font-medium">{secret.name}</span>
        {secret.description ? <span className="block text-xs fs-ink-tertiary">{secret.description}</span> : null}
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
          <div className="fs-row-menu-separator" />
          <div className="fs-row-menu-group">
            <button className="danger" onClick={onDelete} role="menuitem" type="button">
              {t("react.registry.secrets.delete", "Delete")}
            </button>
          </div>
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
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState<SecretFilters>(EMPTY_FILTERS);
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
          <button className="fs-button primary" onClick={openAdd} type="button">
            <Plus aria-hidden="true" />
            {t("react.registry.secrets.create_title", "New secret")}
          </button>
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
          <label className="fs-search">
            <Search aria-hidden="true" />
            <input
              aria-label={t("react.registry.secrets.search", "Search secrets")}
              onChange={(event) => updateFilters({ search: event.target.value })}
              placeholder={t("react.registry.secrets.search_placeholder", "Search secrets…")}
              type="search"
              value={filters.search}
            />
          </label>
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
                  key={secret.id}
                  menuOpen={menuFor === secret.id}
                  onDelete={() => {
                    setMenuFor("");
                    remove.reset();
                    setDeleting(secret);
                  }}
                  onEdit={() => openEdit(secret)}
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
