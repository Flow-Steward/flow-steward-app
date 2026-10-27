import { scopeExitTargetLabel, scopeTargetOptions, type TargetOption } from "@/pages/workflows/builder/canvas-connection-policy";
import { workflowStepDeclaresHook } from "@/pages/workflows/shared/workflow-hook-placement";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useRef, useState } from "react";
import { DsCheckbox, DsSelect } from "@/components/shared/design-system";
import { InlineConfirm } from "@/components/shared/inline-confirm";

import {
  nodeRef,
  outcomeHandleId,
  targetKey,
} from "@/pages/workflows/builder/api";
import {
  edgesTouchingStep,
  groupBodyEntryStepId,
  groupBodyKind,
  groupLanes,
  groupPorts,
  placementContainer,
  portEdge,
  ROOT_PLACEMENT,
  stepPlacement,
  stepPorts,
  type WorkflowDefinitionModel,
  type WorkflowPlacement,
} from "@/pages/workflows/builder/change-set";
import type {
  WorkflowCanvasPort,
  WorkflowForEach,
  WorkflowGroup,
  WorkflowNodeRef,
  WorkflowOutcome,
  WorkflowTargetRef,
} from "@/pages/workflows/shared/api";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { textOrEmpty } from "@/pages/workflows/detail/detail-helpers";
import { edgeNameLabel } from "@/pages/workflows/detail/detail-canvas-handlers";

function placementKey(placement: WorkflowPlacement) {
  return `${placement.groupId}::${placement.forEachId}::${placement.laneId}`;
}

function placementFromKey(key: string): WorkflowPlacement {
  const [groupId = "", forEachId = "", laneId = ""] = key.split("::");
  return { groupId, forEachId, laneId };
}

/** Where a step can live: the root, or a sequence body or one lane of a Group or a For each. */
function placementOptions(definition: WorkflowDefinitionModel, rootLabel: string) {
  const options: Array<{ key: string; label: string }> = [{ key: placementKey(ROOT_PLACEMENT), label: rootLabel }];
  const groupLabel = (groupId: string) => {
    const group = definition.groups.find((item) => textOrEmpty(item.group_id) === groupId);
    return textOrEmpty(group?.display_name) || groupId;
  };
  const addContainer = (owner: WorkflowGroup | WorkflowForEach, label: string, ids: { groupId: string; forEachId: string }) => {
    if (groupBodyKind(owner) === "sequence") {
      options.push({ key: placementKey({ ...ids, laneId: "" }), label });
    }
    groupLanes(owner).forEach((lane) => {
      const laneId = textOrEmpty(lane.lane_id);
      options.push({ key: placementKey({ ...ids, laneId }), label: `${label} · ${laneId}` });
    });
  };
  definition.groups.forEach((group) => {
    const groupId = textOrEmpty(group.group_id);
    addContainer(group, groupLabel(groupId), { groupId, forEachId: "" });
  });
  definition.forEach.forEach((item) => {
    const forEachId = textOrEmpty(item.for_each_id);
    const ownLabel = textOrEmpty(item.display_name) || forEachId;
    const parent = textOrEmpty(item.group_id);
    addContainer(item, parent ? `${groupLabel(parent)} › ${ownLabel}` : ownLabel, { groupId: "", forEachId });
  });
  return options;
}

/** One select per outcome of a node: where that outcome goes, or not connected. */
function PortConnections({
  connectPort,
  disconnectPort,
  ports,
  preview,
  source,
  targets,
}: Readonly<{
  connectPort: (source: WorkflowNodeRef, outcome: WorkflowOutcome, target: WorkflowTargetRef) => unknown;
  disconnectPort: (source: WorkflowNodeRef, outcome: WorkflowOutcome) => unknown;
  ports: WorkflowCanvasPort[];
  preview: WorkflowDefinitionModel;
  source: WorkflowNodeRef;
  targets: TargetOption[];
}>) {
  const { t } = useI18n();
  return (
    <div className="mt-2 space-y-2">
      {ports.map((port) => {
        const edge = portEdge(preview, source, port.outcome);
        const value = edge ? targetKey(edge.target) : "";
        const known = targets.some((option) => option.key === value);
        return (
          <label className="block text-xs fs-ink-secondary" key={outcomeHandleId(port.outcome)}>
            <span>{t("react.workflows.connection_for_outcome", "Next step for {outcome}", { outcome: port.label })}</span>
            <DsSelect
              ariaLabel={t("react.workflows.connection_for_outcome", "Next step for {outcome}", { outcome: port.label })}
              className="mt-1"
              onChange={(nextValue) => {
                const next = targets.find((option) => option.key === nextValue);
                if (!next) {
                  ignoreAsyncResult(Promise.resolve().then(() => disconnectPort(source, port.outcome)));
                  return;
                }
                ignoreAsyncResult(Promise.resolve(connectPort(source, port.outcome, next.target)));
              }}
              options={[
                { value: "", label: t("react.workflows.not_connected", "Not connected") },
                ...(!known && value ? [{ value, label: value }] : []),
                ...targets.map((option) => ({ value: option.key, label: option.label })),
              ]}
              value={value}
            />
          </label>
        );
      })}
    </div>
  );
}

/**
 * Where a Group or a For each goes when it finishes (or fails): the same
 * connection choices a step has, so a container is wired without dragging.
 */
export function ContainerConnections({
  connectPort,
  containerId,
  containerKind,
  disconnectPort,
  preview,
}: Readonly<{
  connectPort: (source: WorkflowNodeRef, outcome: WorkflowOutcome, target: WorkflowTargetRef) => unknown;
  containerId: string;
  containerKind: "group" | "for_each";
  disconnectPort: (source: WorkflowNodeRef, outcome: WorkflowOutcome) => unknown;
  preview: WorkflowDefinitionModel;
}>) {
  const { t } = useI18n();
  const container = containerKind === "for_each"
    ? preview.forEach.find((item) => textOrEmpty(item.for_each_id) === containerId)
    : preview.groups.find((group) => textOrEmpty(group.group_id) === containerId);
  if (!container) return null;
  // A Group sits at the root; a For each at the root or in its sequence Group's body.
  const parentGroupId = containerKind === "for_each" ? textOrEmpty((container as WorkflowForEach).group_id) : "";
  const source = nodeRef(containerKind, containerId);
  const parentPlacement = { ...ROOT_PLACEMENT, groupId: parentGroupId };
  const targets = scopeTargetOptions(
    preview,
    parentPlacement,
    source,
    scopeExitTargetLabel(parentPlacement, t),
  );
  return (
    <div data-testid="container-connections">
      <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-primary">
        {t("react.workflows.connections", "Connections")}</p>
      <PortConnections
        connectPort={connectPort}
        disconnectPort={disconnectPort}
        ports={groupPorts(container, t)}
        preview={preview}
        source={source}
        targets={targets}
      />
    </div>
  );
}

export function WorkflowStepStructureSetup({ ctx }: Readonly<{ ctx: Record<string, any> }>) {
  const { t } = useI18n();
  const {
    connectPort,
    createAsEntry,
    disconnectPort,
    moveStep,
    placementFocusNonce,
    hookName,
    preview,
    selectedStep,
    setCreateAsEntry,
    setStepCreateContext,
    stepCreateContext,
  } = ctx as Record<string, any> & { preview: WorkflowDefinitionModel };
  const [moveTargetKey, setMoveTargetKey] = useState("");
  const [moving, setMoving] = useState(false);
  const placementRef = useRef<HTMLDivElement>(null);
  // "Move to…" from a step's menu opens this editor here, with the choice ready.
  useEffect(() => {
    if (!placementFocusNonce) return;
    const node = placementRef.current;
    node?.scrollIntoView?.({ block: "nearest" });
    node?.querySelector<HTMLElement>('button, [role="combobox"], select')?.focus();
  }, [placementFocusNonce]);
  const hookLabel = hookName ? `${t("react.workflows.hook", "Hook")} ${hookName}` : "";
  const rootLabel = hookLabel || t("react.workflows.placement_root", "Workflow (root)");
  const rootOnlyHook = workflowStepDeclaresHook(selectedStep?.stepKind, selectedStep?.raw?.override);
  const options = placementOptions(preview, rootLabel)
    .filter((option) => !rootOnlyHook || option.key === placementKey(ROOT_PLACEMENT))
    .map((option) => hookLabel && option.key !== placementKey(ROOT_PLACEMENT)
      ? { ...option, label: `${hookLabel} › ${option.label}` } : option);

  if (!selectedStep) {
    const insert = stepCreateContext?.insert;
    if (insert?.append) {
      return (
        <div className="fs-surface-radius border border-subtle fs-surface-inset p-3 text-xs fs-ink-secondary">
          <p className="font-medium uppercase tracking-[0.12em] fs-ink-primary">
            {t("react.workflows.append_after_title", "Add after")}</p>
          <p className="mt-1 font-mono">{insert.label}</p>
          <p className="mt-1">
            {t("react.workflows.append_after_hint", "The new step is connected after this outcome, in the same place as the step before it.")}
          </p>
        </div>
      );
    }
    if (insert) {
      return (
        <div className="fs-surface-radius border border-subtle fs-surface-inset p-3 text-xs fs-ink-secondary">
          <p className="font-medium uppercase tracking-[0.12em] fs-ink-primary">
            {t("react.workflows.insert_between_title", "Insert between")}</p>
          <p className="mt-1 font-mono">{insert.label}</p>
          <p className="mt-1">
            {t("react.workflows.insert_between_hint", "The new step takes this connection's place and continues to its old target. Only a step with a single outcome can be inserted this way.")}
          </p>
        </div>
      );
    }
    const current = placementKey({
      groupId: textOrEmpty(stepCreateContext?.groupId),
      forEachId: textOrEmpty(stepCreateContext?.forEachId),
      laneId: textOrEmpty(stepCreateContext?.laneId),
    });
    const placement = placementFromKey(current);
    const group = placementContainer(preview, placement);
    return (
      <div className="space-y-2 fs-surface-radius border border-subtle fs-surface-inset p-3">
        <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-primary">
          {t("react.workflows.placement", "Placement")}</p>
        <label className="block text-xs fs-ink-secondary">
          <span>{t("react.workflows.placement_new_step", "Add the new step to")}</span>
          <DsSelect
            ariaLabel={t("react.workflows.placement_new_step", "Add the new step to")}
            className="mt-1"
            onChange={(value) => setStepCreateContext({ ...placementFromKey(value), insert: null })}
            options={options.map((option) => ({ value: option.key, label: option.label }))}
            value={current}
          />
        </label>
        {group ? (
          <DsCheckbox checked={Boolean(createAsEntry)} onChange={setCreateAsEntry}>
            <span>
              {placement.laneId
                ? t("react.workflows.make_lane_entry", "Make it the entry of lane {lane}", { lane: placement.laneId })
                : t("react.workflows.make_body_entry", "Make it the entry of {group}", { group: textOrEmpty(group.display_name) || placement.forEachId || placement.groupId })}
              {groupBodyEntryStepId(group, placement.laneId) ? (
                <span className="mt-0.5 block text-[11px] fs-ink-tertiary">
                  {t("react.workflows.entry_replaces_current", "Replaces the current entry {step}.", {
                    step: groupBodyEntryStepId(group, placement.laneId),
                  })}
                </span>
              ) : null}
            </span>
          </DsCheckbox>
        ) : (
          <p className="text-[11px] fs-ink-tertiary">
            {preview.graph.entry
              ? t("react.workflows.new_root_step_not_entry", "The workflow keeps its entry. Connect the new step from another step's outcome.")
              : t("react.workflows.new_root_step_becomes_entry", "The workflow has no entry yet, so this step becomes it.")}
          </p>
        )}
      </div>
    );
  }

  const stepId = textOrEmpty(selectedStep.runtimeId);
  const step = preview.steps.find((item) => textOrEmpty(item.step_id) === stepId);
  if (!step) {
    return null;
  }
  const placement = stepPlacement(step);
  const currentKey = placementKey(placement);
  const currentLabel = options.find((option) => option.key === currentKey)?.label || rootLabel;
  const ref = nodeRef("step", stepId);
  const ports = stepPorts(step, t);
  const targets = scopeTargetOptions(preview, placement, ref, scopeExitTargetLabel(placement, t), t);
  const selectedMoveKey = moveTargetKey && moveTargetKey !== currentKey ? moveTargetKey : "";
  const moveTargetLabel = options.find((option) => option.key === selectedMoveKey)?.label || "";
  const droppedConnections = selectedMoveKey
    ? edgesTouchingStep(preview, stepId).map((edge) => edgeNameLabel(preview, edge, t))
    : [];
  const currentPlaceText = !hookLabel && currentKey === placementKey(ROOT_PLACEMENT)
    ? t("react.workflows.placement_now_root", "Now in the workflow root.")
    : t("react.workflows.placement_now_in", "Now in {place}.", { place: currentLabel });

  return (
    <div className="space-y-3 fs-surface-radius border border-subtle fs-surface-inset p-3" data-testid="step-structure-setup">
      <div ref={placementRef} data-step-placement="">
        <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-primary">
          {t("react.workflows.placement", "Placement")}</p>
        <div className="mt-2">
          <DsSelect
            ariaLabel={t("react.workflows.move_step_to", "Move step to")}
            disabled={moving}
            onChange={setMoveTargetKey}
            options={[
              { value: "", label: t("react.workflows.move_step_choose", "Move to…") },
              ...options.filter((option) => option.key !== currentKey).map((option) => ({ value: option.key, label: option.label })),
            ]}
            value={selectedMoveKey}
          />
        </div>
        <p className="mt-1 text-[11px] fs-ink-tertiary">{currentPlaceText}</p>
        {rootOnlyHook ? <p className="mt-1 text-xs fs-ink-secondary">{t("react.workflows.hook_root_only", "Hooks belong in the workflow root, before or after a Group or For each.")}</p> : null}
        {selectedMoveKey ? (
          <div className="mt-2" data-testid="move-step-confirm">
            <InlineConfirm
              cancelLabel={t("common.cancel", "Cancel")}
              confirmLabel={t("react.workflows.move_step_confirm", "Move step")}
              onCancel={() => setMoveTargetKey("")}
              onConfirm={() => {
                setMoving(true);
                ignoreAsyncResult(
                  Promise.resolve(moveStep(stepId, placementFromKey(selectedMoveKey), { confirmed: true, placeLabel: moveTargetLabel }))
                    .finally(() => {
                      setMoving(false);
                      setMoveTargetKey("");
                    }),
                );
              }}
              pending={moving}
              pendingLabel={t("react.workflows.move_step_confirm", "Move step")}
              question={t("react.workflows.move_step_question", "Move it to {place}?", { place: moveTargetLabel })}
              tone="primary"
            >
              <p>{t(
                "react.workflows.move_step_body",
                "Its connections are removed by the move. Connect it again in its new place before applying.",
              )}</p>
              {droppedConnections.length ? (
                <ul className="mt-1 space-y-0.5">
                  {droppedConnections.map((label) => <li key={label}>{label}</li>)}
                </ul>
              ) : null}
            </InlineConfirm>
          </div>
        ) : null}
      </div>
      <div>
        <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-primary">
          {t("react.workflows.connections", "Connections")}</p>
        <p className="mt-1 text-[11px] fs-ink-tertiary">
          {t("react.workflows.connections_structure_help", "A change here joins the unapplied structure at once. A connection stays inside one group or lane, or in the root.")}
        </p>
        {ports.length ? (
          <PortConnections
            connectPort={connectPort}
            disconnectPort={disconnectPort}
            ports={ports}
            preview={preview}
            source={ref}
            targets={targets}
          />
        ) : (
          <p className="mt-1 text-xs fs-ink-tertiary">
            {t("react.workflows.stop_has_no_connections", "A Stop step ends the run and has no outcomes to connect.")}
          </p>
        )}
      </div>
    </div>
  );
}
