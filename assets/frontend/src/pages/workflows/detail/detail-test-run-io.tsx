import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { isHiddenValue, valuePreview, valueText } from "./detail-test-run-results-model";
import "@/pages/workflows/builder/canvas-test-run.css";

/** One value of a pass: a long one shows its start and "Show all · N lines" until "Show less". */
export function RunValue({ name, value }: Readonly<{ name?: string; value: unknown }>) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const hidden = isHiddenValue(value);
  const full = hidden ? "" : valueText(value);
  const preview = valuePreview(full);
  return <div className="workflow-run-value">
    {name ? <span className="workflow-run-value-name">{name}</span> : null}
    {hidden ? <span className="workflow-run-value-hidden">{t("react.workflows.test_run_value_hidden", "hidden")}</span>
      : <pre className="workflow-run-value-text">{open || !preview.long ? full : preview.preview}</pre>}
    {!hidden && preview.long ? <Button className="workflow-run-value-toggle" size="sm" type="button" variant="ghost" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
      {open ? t("react.workflows.test_run_show_less", "Show less")
        : t("react.workflows.test_run_show_all_lines", "Show all · {count} lines", { count: String(preview.lines) })}
    </Button> : null}
  </div>;
}

function valueRows(values: Record<string, unknown>) {
  return Object.entries(values).filter(([key]) => key !== "_flow_steward_projection");
}

/** What a pass read and wrote: its declared inputs before it ran, its outputs after. */
export function RunPassValues({ inputs, outputs, error }: Readonly<{ inputs: Record<string, unknown>; outputs: Record<string, unknown>; error?: string }>) {
  const { t } = useI18n();
  const inputRows = valueRows(inputs);
  const outputRows = valueRows(outputs);
  return <dl className="workflow-run-io">
    <dt>{t("registry.tool_detail.schemas.input", "Input")}</dt>
    <dd>{inputRows.length ? inputRows.map(([name, value]) => <RunValue key={name} name={name} value={value} />)
      : <span className="workflow-run-value-hidden">{t("react.workflows.test_run_no_inputs", "No inputs were recorded.")}</span>}</dd>
    <dt>{t("registry.tool_detail.schemas.output", "Output")}</dt>
    <dd>{error ? <RunValue name="error" value={error} /> : null}
      {outputRows.length ? outputRows.map(([name, value]) => <RunValue key={name} name={name} value={value} />)
        : !error ? <span className="workflow-run-value-hidden">{t("react.workflows.test_run_no_outputs", "No outputs were recorded.")}</span> : null}</dd>
  </dl>;
}
