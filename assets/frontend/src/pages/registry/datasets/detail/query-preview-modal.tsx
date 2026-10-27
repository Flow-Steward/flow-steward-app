import { useI18n } from "@/lib/i18n/useI18n";
import { Button } from "@/components/ui/button";

import type { QueryPreviewState } from "./dataset-detail-helpers";

export function QueryPreviewModal({
  previewState,
  onClose,
}: Readonly<{
  previewState: QueryPreviewState;
  onClose: () => void;
}>) {
  const { t } = useI18n();
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4">
      <div className="max-h-[calc(100vh-4rem)] w-full max-w-4xl overflow-y-auto rounded-3xl border border-white/10 bg-slate-950 p-5 shadow-2xl">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <p className="text-xs font-medium uppercase tracking-[0.3em] text-cyan-200">
              {t("react.registry.saved_query_preview", "Saved query preview")}</p>
            <h2 className="text-xl font-medium text-white">{previewState.queryName}</h2>
            <p className="mt-1 text-sm text-slate-400">
              {previewState.rowCount} {" "}{t("react.registry.row", "row")}{previewState.rowCount === 1 ? "" : "s"}
              {previewState.truncated ? t("react.registry.truncated", "(truncated)") : ""}
            </p>
          </div>
          <Button type="button" variant="secondary" onClick={onClose}>
            {t("registry.common.close", "Close")}</Button>
        </div>
        <div className="grid gap-4">
          <div
            className="max-w-full overflow-x-auto rounded-2xl border border-white/10"
            data-testid="query-preview-table-scroll"
          >
            <table className="min-w-max text-left text-sm">
              <thead className="bg-white/5 text-xs uppercase tracking-[0.15em] text-slate-400">
                <tr>
                  {(previewState.rows[0] ? Object.keys(previewState.rows[0]) : []).map((key) => (
                    <th key={key} className="whitespace-nowrap px-3 py-2">
                      {key}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {previewState.rows.length ? (
                  previewState.rows.map((row, index) => (
                    <tr key={`${previewState.queryName}-${index}`} className="border-t border-white/10">
                      {Object.entries(row).map(([key, value]) => (
                        <td key={key} className="whitespace-nowrap px-3 py-2 align-top text-slate-200">
                          {typeof value === "string" ? value : JSON.stringify(value)}
                        </td>
                      ))}
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td className="px-3 py-6 text-center text-slate-400" colSpan={1}>
                      {t("react.registry.preview_returned_no_rows", "Preview returned no rows.")}</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <details
            className="rounded-2xl border border-white/10 bg-slate-900/50 p-3"
            data-testid="query-preview-output-schema"
          >
            <summary className="cursor-pointer text-xs font-medium uppercase tracking-[0.15em] text-slate-400">
              {t("react.registry.output_schema", "Output schema")}</summary>
            <pre className="mt-3 overflow-x-auto text-xs text-slate-200">
              {JSON.stringify(previewState.outputSchema, null, 2)}
            </pre>
          </details>
        </div>
      </div>
    </div>
  );
}
