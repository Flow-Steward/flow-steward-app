import { Fragment } from "react";
import { useI18n } from "@/lib/i18n/useI18n";
import type { ContainerRuntimeMark } from "./runtime-container-marks";

/** What a loop or a parallel group says about this committed pass, in words, part by part. */
function containerRuntimeParts(mark: ContainerRuntimeMark, t: ReturnType<typeof useI18n>["t"]): string[] {
  const parts: string[] = [];
  if (mark.items) {
    const total = mark.items.selected;
    const item = String(Math.min(mark.items.processed + 1, Math.max(total, 1)));
    if (mark.status === "running" && total > 0) parts.push(t("react.workflows.container_item_of", "Item {item} of {total}", { item, total: String(total) }));
    else if (mark.status === "failed" && total > 0) parts.push(t("react.workflows.container_failed_at_item", "Failed at item {item} of {total}", { item, total: String(total) }));
    else parts.push(mark.items.processed === 1
      ? t("react.workflows.container_item_done", "1 item done")
      : t("react.workflows.container_items_done", "{count} items done", { count: String(mark.items.processed) }));
  }
  if (mark.lanes) {
    parts.push(mark.status === "completed" && mark.lanes.completed === mark.lanes.total
      ? t("react.workflows.container_all_lanes_done", "All lanes done")
      : t("react.workflows.container_lanes_done", "Lanes {completed} of {total} done", { completed: String(mark.lanes.completed), total: String(mark.lanes.total) }));
  }
  if (!parts.length) {
    parts.push(mark.status === "running" ? t("react.workflows.run_bar_running", "Running")
      : mark.status === "failed" ? t("react.workflows.test_run_failed", "Failed") : t("react.workflows.container_done", "Done"));
  }
  return parts;
}

/** Counts describe this committed container pass, never its child operation rows. */
export function CanvasContainerRuntimeMark({ mark, containerId }: Readonly<{ mark?: ContainerRuntimeMark; containerId: string }>) {
  const { t } = useI18n();
  if (!mark) return null;
  return <span data-testid={`workflow-container-runtime:${containerId}`} data-job-id={mark.jobId} data-execution-id={mark.executionId}
    data-run={mark.status === "completed" ? "done" : mark.status} className="workflow-container-run-text">
    {containerRuntimeParts(mark, t).map((part, index) => <Fragment key={part}>{index ? <span aria-hidden="true"> · </span> : null}<span>{part}</span></Fragment>)}
  </span>;
}
