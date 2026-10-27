import { useI18n } from "@/lib/i18n/useI18n";
import type { I18nContextValue } from "@/lib/i18n/context";
import { useEffect, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ignoreAsyncResult } from "@/lib/utils/async";

type JsonRecord = Record<string, unknown>;

type JsonRecordWorkbenchProps = {
  eyebrow: string;
  title: string;
  description: string;
  queryKey: readonly unknown[];
  fetchRecord: () => Promise<JsonRecord>;
  saveRecord?: (payload: JsonRecord) => Promise<unknown>;
};

function toPrettyJson(value: unknown) {
  return JSON.stringify(value ?? {}, null, 2);
}

function parseRecord(value: string, t: I18nContextValue["t"]) {
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error(t("react.shared.json_payload_object_required", "JSON payload must be an object."));
  }
  return parsed as JsonRecord;
}

export function JsonRecordWorkbench({
  eyebrow,
  title,
  description,
  queryKey,
  fetchRecord,
  saveRecord,
}: Readonly<JsonRecordWorkbenchProps>) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [editorValue, setEditorValue] = useState("{\n\n}");
  const [feedback, setFeedback] = useState("");
  const [isSaving, setIsSaving] = useState(false);

  const recordQuery = useQuery({
    queryKey,
    queryFn: fetchRecord,
    refetchInterval: 30_000,
  });

  useEffect(() => {
    if (recordQuery.data) {
      setEditorValue(toPrettyJson(recordQuery.data));
    }
  }, [recordQuery.data]);

  async function handleRefresh() {
    setFeedback("");
    await queryClient.invalidateQueries({ queryKey });
  }

  async function handleSave() {
    if (!saveRecord) {
      return;
    }
    setFeedback("");
    setIsSaving(true);
    try {
      const payload = parseRecord(editorValue, t);
      await saveRecord(payload);
      setFeedback(t("react.shared.saved_successfully", "Saved successfully."));
      await handleRefresh();
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : t("react.shared.save_failed", "Save failed."));
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
            <CardTitle className="text-lg">{t("react.shared.json_payload", "JSON payload")}</CardTitle>
            <CardDescription>{t("react.shared.direct_contract_view_for_migration_safe_parity", "Direct contract view for migration-safe parity.")}</CardDescription>
          </div>
          <button
            className="rounded-xl border border-white/10 px-3 py-1.5 text-sm text-slate-200 hover:border-cyan-300/40 disabled:opacity-50"
            disabled={recordQuery.isFetching}
            onClick={() => ignoreAsyncResult(handleRefresh())}
            type="button"
          >
            {recordQuery.isFetching ? t("react.shared.refreshing", "Refreshing...") : t("account.tokens.refresh", "Refresh")}
          </button>
        </CardHeader>
        <CardContent className="space-y-3">
          {recordQuery.isError ? (
            <div className="rounded-2xl border border-rose-300/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-100">
              {recordQuery.error instanceof Error ? recordQuery.error.message : t("react.shared.failed_to_load_record", "Failed to load record.")}
            </div>
          ) : null}

          <textarea
            className="min-h-[360px] w-full rounded-2xl border border-white/10 bg-slate-950/70 px-4 py-3 font-mono text-xs text-slate-100 outline-none focus:border-cyan-300/40"
            onChange={(event) => setEditorValue(event.target.value)}
            value={editorValue}
          />

          {feedback ? (
            <div className="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-sm text-slate-200">
              {feedback}
            </div>
          ) : null}

          {saveRecord ? (
            <button
              className="rounded-xl border border-cyan-300/30 bg-cyan-400/10 px-3 py-1.5 text-sm text-cyan-100 hover:border-cyan-300/50 disabled:opacity-50"
              disabled={isSaving}
              onClick={() => ignoreAsyncResult(handleSave())}
              type="button"
            >
              {t("registry.common.save", "Save")} </button>
          ) : null}
        </CardContent>
      </Card>
    </section>
  );
}
