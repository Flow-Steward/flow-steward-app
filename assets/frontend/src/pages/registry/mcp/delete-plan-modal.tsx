import { useI18n } from "@/lib/i18n/useI18n";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { AccessibleDialog } from "@/components/shared/accessible-dialog";
import type { McpConnectorDeletePlan } from "@/pages/registry/mcp/api";

type Props = {
  busy: boolean;
  error: string;
  onClose: () => void;
  onConfirm: () => void;
  plan: McpConnectorDeletePlan;
};

export function McpConnectorDeletePlanModal({
  busy,
  error,
  onClose,
  onConfirm,
  plan,
}: Readonly<Props>) {
  const { t } = useI18n();
  const [acknowledged, setAcknowledged] = useState(false);
  const acknowledgementRef = useRef<HTMLInputElement>(null);

  return (
    <AccessibleDialog
      ariaLabel={t("react.registry.delete_mcp_connector", "Delete MCP connector")}
      initialFocusRef={acknowledgementRef}
      onClose={onClose}
      panelClassName="max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-2xl border border-white/15 bg-slate-950 p-5 shadow-2xl"
    >
      <h2 className="text-xl font-semibold text-white">{t("react.registry.delete_mcp_connector", "Delete MCP connector")}</h2>
      <p className="mt-1 text-sm text-slate-300">
        {plan.connector.display_name} ({plan.connector_id})
      </p>

      <section className="mt-5">
        <h3 className="text-sm font-semibold text-rose-100">{t("react.registry.tools_moved_to_trash", "Tools moved to Trash")}</h3>
        {plan.remove.tools.length ? (
          <ul className="mt-2 divide-y divide-white/10 border-y border-white/10 text-sm text-slate-200">
            {plan.remove.tools.map((tool) => (
              <li className="py-2" key={tool.tool_id}>
                <p>{tool.display_name || tool.tool_id}</p>
                <p className="font-mono text-xs text-slate-400">{tool.tool_id}</p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-slate-400">{t("react.registry.no_projected_tools", "No projected tools.")}</p>
        )}
      </section>

      <section className="mt-5">
        <h3 className="text-sm font-semibold text-amber-100">
          {t("react.registry.workflows_that_will_stop_working", "Workflows that will stop working")}</h3>
        {plan.affected.workflows.length ? (
          <ul className="mt-2 divide-y divide-white/10 border-y border-white/10 text-sm text-slate-200">
            {plan.affected.workflows.map((workflow) => (
              <li className="py-2" key={workflow.workflow_id}>
                <p>{workflow.display_name || workflow.workflow_id}</p>
                <p className="font-mono text-xs text-slate-400">
                  {workflow.workflow_id} ·{" "}
                  {workflow.tool_ids.length > 0 ? workflow.tool_ids.join(", ") : null}
                  {!workflow.tool_ids.length && workflow.via_connector
                    ? t("react.registry.direct_connector_dependency", "direct connector dependency")
                    : null}
                  {!workflow.tool_ids.length && !workflow.via_connector
                    ? t("react.registry.projected_tool_dependency", "projected tool dependency")
                    : null}
                </p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-slate-400">{t("react.registry.no_affected_workflows", "No affected workflows.")}</p>
        )}
      </section>

      <ul className="mt-5 list-disc space-y-1 pl-5 text-sm text-amber-100">
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
        <span>
          {t("react.registry.i_understand_the_connector_and_all_listed_tools_will_be_moved_to_trash", "I understand the connector and all listed tools will be moved to Trash.")}</span>
      </label>
      {error ? <p className="mt-3 text-sm text-rose-200" role="alert">{error}</p> : null}
      <div className="mt-5 flex justify-end gap-2">
        <Button disabled={busy} onClick={onClose} type="button" variant="secondary">
          {t("extensions.repeater.cancel", "Cancel")}</Button>
        <Button
          className="border-rose-300/30 bg-rose-500/15 text-rose-100 hover:bg-rose-500/25"
          disabled={busy || !acknowledged}
          onClick={onConfirm}
          type="button"
          variant="secondary"
        >
          {t("react.registry.delete_connector", "Delete connector")}</Button>
      </div>
    </AccessibleDialog>
  );
}
