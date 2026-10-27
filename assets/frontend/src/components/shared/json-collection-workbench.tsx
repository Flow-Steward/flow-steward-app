import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

export type JsonEntity = Record<string, unknown>;

type JsonCollectionWorkbenchProps = {
  eyebrow: string;
  title: string;
  description: string;
  listQueryKey: readonly unknown[];
  fetchList: () => Promise<JsonEntity[]>;
  idKey: string;
  idAliases?: string[];
  fetchDetail?: (id: string) => Promise<JsonEntity>;
  createItem?: (payload: JsonEntity) => Promise<unknown>;
  updateItem?: (id: string, payload: JsonEntity) => Promise<unknown>;
  toggleItem?: (id: string) => Promise<unknown>;
  deleteItem?: (id: string) => Promise<unknown>;
  emptyMessage?: string;
  selectionQueryParam?: string;
};

function labelForRow(row: JsonEntity, idKey: string, t: I18nContextValue["t"] = fallbackContext.t) {
  const candidateKeys = ["display_name", "name", "title", idKey];
  for (const key of candidateKeys) {
    const value = row[key];
    if (value !== undefined && value !== null && stringFromUnknownValue(value).trim()) {
      return stringFromUnknownValue(value);
    }
  }
  return t("react.shared.unnamed", "Unnamed");
}

function readId(row: JsonEntity, idKey: string, idAliases: string[]) {
  const keys = [idKey, ...idAliases];
  for (const key of keys) {
    const value = row[key];
    if (value !== undefined && value !== null && stringFromUnknownValue(value).trim()) {
      return stringFromUnknownValue(value);
    }
  }
  return "";
}

function toPrettyJson(value: unknown) {
  return JSON.stringify(value ?? {}, null, 2);
}

function parseJsonEditor(value: string, t: I18nContextValue["t"]) {
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error(t("react.shared.json_payload_object_required", "JSON payload must be an object."));
  }
  return parsed as JsonEntity;
}

export function JsonCollectionWorkbench({
  eyebrow,
  title,
  description,
  listQueryKey,
  fetchList,
  idKey,
  idAliases = [],
  fetchDetail,
  createItem,
  updateItem,
  toggleItem,
  deleteItem,
  emptyMessage: suppliedEmptyMessage,
  selectionQueryParam = "selected",
}: Readonly<JsonCollectionWorkbenchProps>) {
  const { t } = useI18n();
  const emptyMessage = suppliedEmptyMessage ?? t("react.shared.no_rows_found", "No rows found.");
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [selectedId, setSelectedId] = useState("");
  const [editorValue, setEditorValue] = useState("{\n\n}");
  const [feedback, setFeedback] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const selectedFromQuery = firstStringFromUnknownValue([searchParams.get(selectionQueryParam), ""]).trim();

  const listQuery = useQuery({
    queryKey: listQueryKey,
    queryFn: fetchList,
    refetchInterval: 30_000,
  });

  const selectedListRow =
    listQuery.data?.find((row) => readId(row, idKey, idAliases) === selectedId) || null;

  const detailQuery = useQuery({
    queryKey: [...listQueryKey, "detail", selectedId] as const,
    queryFn: () => (fetchDetail ? fetchDetail(selectedId) : Promise.resolve({})),
    enabled: Boolean(fetchDetail && selectedId),
  });

  useEffect(() => {
    if (!selectedFromQuery) {
      return;
    }
    if (selectedFromQuery !== selectedId) {
      setSelectedId(selectedFromQuery);
    }
  }, [selectedFromQuery, selectedId]);

  useEffect(() => {
    if (!selectedId || !selectedListRow) {
      return;
    }
    setEditorValue(toPrettyJson(selectedListRow));
  }, [idKey, selectedId, selectedListRow]);

  useEffect(() => {
    if (detailQuery.data && selectedId) {
      setEditorValue(toPrettyJson(detailQuery.data));
    }
  }, [detailQuery.data, selectedId]);

  async function refreshList() {
    await queryClient.invalidateQueries({ queryKey: listQueryKey });
  }

  async function handleCreate() {
    if (!createItem) {
      return;
    }
    setFeedback("");
    setIsSaving(true);
    try {
      const payload = parseJsonEditor(editorValue, t);
      await createItem(payload);
      setFeedback(t("react.shared.created_successfully", "Created successfully."));
      await refreshList();
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : t("react.shared.create_failed", "Create failed."));
    } finally {
      setIsSaving(false);
    }
  }

  async function handleSave() {
    if (!updateItem || !selectedId) {
      return;
    }
    setFeedback("");
    setIsSaving(true);
    try {
      const payload = parseJsonEditor(editorValue, t);
      await updateItem(selectedId, payload);
      setFeedback(t("react.shared.saved_successfully", "Saved successfully."));
      await refreshList();
      await queryClient.invalidateQueries({
        queryKey: [...listQueryKey, "detail", selectedId] as const,
      });
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : t("react.shared.save_failed", "Save failed."));
    } finally {
      setIsSaving(false);
    }
  }

  async function handleToggle() {
    if (!toggleItem || !selectedId) {
      return;
    }
    setFeedback("");
    setIsSaving(true);
    try {
      await toggleItem(selectedId);
      setFeedback(t("react.shared.toggled_successfully", "Toggled successfully."));
      await refreshList();
      await queryClient.invalidateQueries({
        queryKey: [...listQueryKey, "detail", selectedId] as const,
      });
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : t("react.shared.toggle_failed", "Toggle failed."));
    } finally {
      setIsSaving(false);
    }
  }

  async function handleDelete() {
    if (!deleteItem || !selectedId) {
      return;
    }
    if (!globalThis.confirm(t("react.shared.confirm_delete_record", "Delete {id}?", { id: selectedId }))) {
      return;
    }
    setFeedback("");
    setIsSaving(true);
    try {
      await deleteItem(selectedId);
      setFeedback(t("react.shared.deleted_successfully", "Deleted successfully."));
      setSelectedId("");
      await refreshList();
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : t("react.shared.delete_failed", "Delete failed."));
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <section className="space-y-6">
      <Card className="border-white/10 bg-white/5">
        <CardHeader>
          <p className="text-xs font-medium uppercase tracking-[0.3em] text-cyan-200">{eyebrow}</p>
          <CardTitle>{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </CardHeader>
      </Card>

      <Card className="border-white/10 bg-white/5">
        <CardHeader className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <CardTitle className="text-lg">{t("react.shared.catalog", "Catalog")}</CardTitle>
            <CardDescription>{t("react.shared.select_a_row_to_inspect_and_edit_its_json_payload", "Select a row to inspect and edit its JSON payload.")}</CardDescription>
          </div>
          <button
            className="rounded-xl border border-white/10 px-3 py-1.5 text-sm text-slate-200 hover:border-cyan-300/40 disabled:opacity-50"
            disabled={listQuery.isFetching}
            onClick={() => ignoreAsyncResult(refreshList())}
            type="button"
          >
            {listQuery.isFetching ? t("react.shared.refreshing", "Refreshing...") : t("account.tokens.refresh", "Refresh")}
          </button>
        </CardHeader>
        <CardContent>
          {listQuery.isError ? (
            <div className="rounded-2xl border border-rose-300/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-100">
              {listQuery.error instanceof Error ? listQuery.error.message : t("react.shared.failed_to_load_collection", "Failed to load collection.")}
            </div>
          ) : null}

          {!listQuery.isError && !(listQuery.data || []).length ? (
            <div className="rounded-2xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-300">
              {emptyMessage}
            </div>
          ) : null}

          <div className="grid gap-2">
            {(listQuery.data || []).map((row) => {
              const rowId = readId(row, idKey, idAliases);
              const active = rowId === selectedId;
              return (
                <button
                  key={rowId || JSON.stringify(row)}
                  className={[
                    "w-full rounded-2xl border px-3 py-2 text-left text-sm transition",
                    active
                      ? "border-cyan-300/50 bg-cyan-400/10 text-white"
                      : "border-white/10 bg-slate-900/40 text-slate-200 hover:border-white/30",
                  ].map((value) => stringFromUnknownValue(value)).join(" ")}
                  onClick={() => {
                    setSelectedId(rowId);
                    if (rowId) {
                      const next = new URLSearchParams(searchParams);
                      next.set(selectionQueryParam, rowId);
                      setSearchParams(next, { replace: true });
                    }
                    setFeedback("");
                  }}
                  type="button"
                >
                  <div className="font-medium">{labelForRow(row, idKey, t)}</div>
                  <div className="mt-1 text-xs text-slate-400">{rowId || t("react.shared.no_identifier_found", "(no identifier found)")}</div>
                </button>
              );
            })}
          </div>
        </CardContent>
      </Card>

      <Card className="border-white/10 bg-white/5">
        <CardHeader>
          <CardTitle className="text-lg">{t("react.shared.json_editor", "JSON editor")}</CardTitle>
          <CardDescription>
            {t("react.shared.the_editor_is_json_first_to_preserve_backend_contract_fidelity_during_migration", "The editor is JSON-first to preserve backend contract fidelity during migration.")} </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <textarea
            className="min-h-[320px] w-full rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3 font-mono text-xs text-slate-100 outline-none focus:border-cyan-300/40"
            onChange={(event) => setEditorValue(event.target.value)}
            value={editorValue}
          />

          {feedback ? (
            <div className="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-sm text-slate-200">
              {feedback}
            </div>
          ) : null}

          <div className="flex flex-wrap gap-2">
            {createItem ? (
              <button
                className="rounded-xl border border-emerald-300/30 bg-emerald-400/10 px-3 py-1.5 text-sm text-emerald-100 hover:border-emerald-300/50 disabled:opacity-50"
                disabled={isSaving}
                onClick={() => ignoreAsyncResult(handleCreate())}
                type="button"
              >
                {t("projects.modal.create", "Create")} </button>
            ) : null}

            {updateItem ? (
              <button
                className="rounded-xl border border-cyan-300/30 bg-cyan-400/10 px-3 py-1.5 text-sm text-cyan-100 hover:border-cyan-300/50 disabled:opacity-50"
                disabled={isSaving || !selectedId}
                onClick={() => ignoreAsyncResult(handleSave())}
                type="button"
              >
                {t("react.shared.save_selected", "Save selected")} </button>
            ) : null}

            {toggleItem ? (
              <button
                className="rounded-xl border border-amber-300/30 bg-amber-400/10 px-3 py-1.5 text-sm text-amber-100 hover:border-amber-300/50 disabled:opacity-50"
                disabled={isSaving || !selectedId}
                onClick={() => ignoreAsyncResult(handleToggle())}
                type="button"
              >
                {t("react.shared.toggle_selected", "Toggle selected")} </button>
            ) : null}

            {deleteItem ? (
              <button
                className="rounded-xl border border-rose-300/30 bg-rose-500/10 px-3 py-1.5 text-sm text-rose-100 hover:border-rose-300/50 disabled:opacity-50"
                disabled={isSaving || !selectedId}
                onClick={() => ignoreAsyncResult(handleDelete())}
                type="button"
              >
                {t("react.shared.delete_selected", "Delete selected")} </button>
            ) : null}
          </div>
        </CardContent>
      </Card>
    </section>
  );
}
