import { useI18n } from "@/lib/i18n/useI18n";
import type { Node, NodeProps } from "@xyflow/react";

import { ContainerCard } from "@/pages/workflows/builder/canvas-container-card";
import type { ForEachNodeData } from "@/pages/workflows/builder/layout";

const BADGE_CLASS =
  "workflow-fact";

/**
 * A For each on the canvas: its body runs once per item of a ready
 * collection, using its declared sequence or parallel lanes. It shows where the items come from and the
 * name each item is read under.
 */
export function ForEachNode({ data, id, selected }: Readonly<NodeProps<Node<ForEachNodeData>>>) {
  const { t } = useI18n();
  const { forEach } = data;
  const missingSource = !forEach.over || !forEach.item_name;
  return (
    <ContainerCard
      badges={
        <>
          <span className={BADGE_CLASS}>{t("react.workflows.for_each", "For each")}</span>
          {missingSource ? (
            <span className="workflow-fact workflow-container-source is-warning">
              {t("react.workflows.for_each_source_missing", "Source not set")}
            </span>
          ) : (
            <span className={`${BADGE_CLASS} workflow-container-source`} title={`${forEach.item_name} in ${forEach.over}`} data-for-each-source="true">
              {typeof forEach.body?.concurrency_limit === "number" && forEach.body.concurrency_limit > 1
                ? t("react.workflows.for_each_source_parallel", "per {item} of {over} · {count} at a time", { item: forEach.item_name, over: forEach.over, count: forEach.body.concurrency_limit })
                : t("react.workflows.for_each_source_serial", "per {item} of {over} · one at a time", { item: forEach.item_name, over: forEach.over })}
            </span>
          )}
          {data.lanes.length ? <span className={BADGE_CLASS}>{t("react.workflows.group_kind_parallel", "Parallel lanes")}</span> : null}
        </>
      }
      containerId={forEach.id}
      containerKind="for_each"
      data={data}
      displayName={forEach.display_name}
      onAddStep={
        data.onOpenNewStep
          ? (laneId) => data.onOpenNewStep?.({ groupId: "", laneId, forEachId: forEach.id })
          : undefined
      }
      onOpen={() => data.onOpenForEach(forEach.id)}
      nodeId={id}
      selected={selected}
      validationSummary={forEach.validation_summary}
    />
  );
}
