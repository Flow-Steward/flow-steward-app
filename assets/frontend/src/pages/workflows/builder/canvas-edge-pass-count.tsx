import { EdgeLabelRenderer } from "@xyflow/react";

import { useI18n } from "@/lib/i18n/useI18n";
import "./canvas-test-run.css";

/** A connection the run took more than once (inside a loop) says how often: "2×". */
export function CanvasEdgePassCount({ passes, x, y }: Readonly<{ passes?: number; x: number; y: number }>) {
  const { t } = useI18n();
  if (!passes || passes < 2) return null;
  const label = t("react.workflows.run_mark_passes", "{count}×", { count: String(passes) });
  return <EdgeLabelRenderer>
    <span className="workflow-edge-passes nodrag nopan" data-testid="workflow-edge-passes"
      title={t("react.workflows.edge_passes_title", "Taken {count} times in this run", { count: String(passes) })}
      style={{ transform: `translate(${x}px, ${y}px) translate(-50%, calc(-50% - 16px))` }}>{label}</span>
  </EdgeLabelRenderer>;
}
