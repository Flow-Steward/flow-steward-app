import { useI18n } from "@/lib/i18n/useI18n";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { AccessibleDialog } from "@/components/shared/accessible-dialog";
import type { McpProjectedToolDeletePlan } from "@/pages/registry/shared/api";

type Props = {
  busy: boolean;
  error: string;
  onClose: () => void;
  onConfirm: () => void;
  plan: McpProjectedToolDeletePlan;
};

export function McpToolDeletePlanModal({ busy, error, onClose, onConfirm, plan }: Readonly<Props>) {
  const { t } = useI18n();
  const [acknowledged, setAcknowledged] = useState(false);
  const acknowledgementRef = useRef<HTMLInputElement>(null);
  return (
    <AccessibleDialog
      ariaLabel={t("react.registry.move_mcp_tool_to_trash", "Move MCP tool to Trash")}
      initialFocusRef={acknowledgementRef}
      onClose={onClose}
      panelClassName="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-white/15 bg-slate-950 p-5 shadow-2xl"
    >
      <h2 className="text-xl font-semibold text-white">{t("react.registry.move_mcp_tool_to_trash", "Move MCP tool to Trash")}</h2>
      <p className="mt-1 font-mono text-sm text-slate-300">{plan.tool_id}</p>
      <h3 className="mt-5 text-sm font-semibold text-amber-100">{t("react.registry.workflows_that_will_stop_working", "Workflows that will stop working")}</h3>
      {plan.affected.workflows.length ? (
        <ul className="mt-2 divide-y divide-white/10 border-y border-white/10 text-sm text-slate-200">
          {plan.affected.workflows.map((workflow) => (
            <li className="py-2" key={workflow.workflow_id}>
              {workflow.display_name || workflow.workflow_id}
              <span className="ml-2 font-mono text-xs text-slate-400">{workflow.workflow_id}</span>
            </li>
          ))}
        </ul>
      ) : <p className="mt-2 text-sm text-slate-400">{t("react.registry.no_affected_workflows", "No affected workflows.")}</p>}
      <ul className="mt-4 list-disc space-y-1 pl-5 text-sm text-amber-100">
        {plan.warnings.map((warning) => <li key={warning}>{warning}</li>)}
      </ul>
      <label className="mt-5 flex items-start gap-2 text-sm text-slate-200">
        <input
          checked={acknowledged}
          className="mt-1"
          onChange={(event) => setAcknowledged(event.target.checked)}
          ref={acknowledgementRef}
          type="checkbox"
        />
        <span>{t("react.registry.i_understand_this_tool_will_be_removed_from_the_connector_allowlist_and_moved_to_trash", "I understand this tool will be removed from the connector allowlist and moved to Trash.")}</span>
      </label>
      {error ? <p className="mt-3 text-sm text-rose-200" role="alert">{error}</p> : null}
      <div className="mt-5 flex justify-end gap-2">
        <Button disabled={busy} onClick={onClose} type="button" variant="secondary">{t("extensions.repeater.cancel", "Cancel")}</Button>
        <Button disabled={busy || !acknowledged} onClick={onConfirm} type="button" variant="secondary">
          {t("react.registry.move_to_trash_variant_9adcdf33", "Move to Trash")}</Button>
      </div>
    </AccessibleDialog>
  );
}
