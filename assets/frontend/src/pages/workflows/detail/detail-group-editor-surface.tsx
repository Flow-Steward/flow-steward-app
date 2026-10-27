import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { OptionalLabel, RequiredLabel } from "@/components/shared/design-system";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useId, useRef, useState, type ReactNode } from "react";
import { parseGroupConcurrency } from "./detail-group-concurrency";
import { DrawerFooter, DrawerFooterSpacer } from "./detail-drawer-footer";
import { SaveStateButton } from "./detail-save-button";

import {
  AutomationRuleEditor,
  commaList,
  workflowVariableSuggestionPanelId,
  type WorkflowGroupRow,
} from "@/pages/workflows/detail/detail-helpers";
import type { GroupDraft } from "@/pages/workflows/detail/detail-drawer-controller";
import { nodeRef } from "@/pages/workflows/builder/api";
import { groupBodyEntryStepId, groupBodyKind } from "@/pages/workflows/builder/change-set";
import { groupChainLaneIds } from "@/pages/workflows/builder/canvas-group-selection";
import type { WorkflowGroup } from "@/pages/workflows/shared/api";
import { ContainerConnections } from "@/pages/workflows/detail/detail-step-structure-setup";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

const INPUT_CLASS = "fs-input mt-1 w-full";

/**
 * The editor of one container: a Group (its body runs once, as a sequence or
 * parallel lanes) or a For each (its body runs once per item). Name,
 * description and the For each source are the container's own settings and
 * are saved directly; lanes, entries, placement and membership are structure
 * and go to the unapplied change set.
 */
export function GroupEditorSurface({ ctx }: Readonly<{ ctx: Record<string, any> }>) {
  const { t } = useI18n();
  const sourceLabel = useRef<HTMLLabelElement>(null);
  const sourceErrorId = useId();
  const concurrencyInput = useRef<HTMLInputElement>(null);
  const concurrencyErrorId = useId();
  const [concurrencyError, setConcurrencyError] = useState(false);
  const {
    drawerMode,
    drawerTab,
    groupCreateMode,
    groupDraft,
    canConvertGroup,
    connectPort,
    disconnectPort,
    groupExtractionBlockedReason,
    onConvertGroup,
    handleDeleteGroup,
    handleMoveForEach,
    handleSaveGroup,
    openNewStepDrawer,
    openStepDrawer,
    preview,
    renderWorkflowVariableInput,
    savingWorkflowSurface,
    selectedGroup,
    selectedProjectId,
    setEntry,
    setGroupDraft,
    structureLocked,
    workflowId,
    workflowRootEntryId,
  } = ctx as Record<string, any> & { groupDraft: GroupDraft; selectedGroup: WorkflowGroupRow | null };
  const updateDraft = (patch: Partial<GroupDraft>) =>
    setGroupDraft((current: GroupDraft) => ({ ...current, ...patch }));
  const isForEach = groupDraft.containerKind === "for_each";
  const usesLanes = groupDraft.bodyKind === "lanes";
  const sourceError = isForEach && !groupDraft.over.trim()
    ? t("react.workflows.for_each_source_required", "Enter the list the For each goes through.")
    : "";
  const invalidConcurrency = usesLanes && !parseGroupConcurrency(groupDraft.concurrencyLimit).valid;
  const showConcurrencyError = concurrencyError && invalidConcurrency;
  function saveGroup() {
    // Preserve the action's existing source/lane validation order.
    const earlierInvalid = Boolean(sourceError) || (usesLanes && !commaList(groupDraft.lanes).length);
    if (invalidConcurrency && !earlierInvalid) {
      setConcurrencyError(true);
      concurrencyInput.current?.focus();
      return;
    }
    setConcurrencyError(false);
    ignoreAsyncResult(handleSaveGroup());
    if (sourceError) sourceLabel.current?.querySelector<HTMLInputElement>('[name="for_each_over"]')?.focus();
  }
  const sequenceGroups = ((preview?.groups || []) as WorkflowGroup[]).filter(
    (group) => groupBodyKind(group) === "sequence",
  );
  const groupId = selectedGroup?.groupId || "";
  const owner = isForEach ? { forEachId: groupId } : { groupId };
  // A Group's body may start with a For each placed in it, not only with a step.
  const FOR_EACH_PREFIX = "for_each:";
  const childForEachEntries = isForEach
    ? []
    : ((preview?.forEach || []) as Array<Record<string, unknown>>)
        .filter((item) => stringFromUnknownValue(item.group_id) === groupId && !item.lane_id)
        .map((item) => ({
          primaryLabel: String(item.display_name || item.for_each_id),
          runtimeId: `${FOR_EACH_PREFIX}${String(item.for_each_id)}`,
        }));
  const bodyEntry = ((selectedGroup?.raw as { body?: { entry?: { kind?: string } } } | undefined)?.body?.entry) || {};
  const bodyEntryValue = (entryId: string) =>
    bodyEntry.kind === "for_each" && entryId ? `${FOR_EACH_PREFIX}${entryId}` : entryId;
  const setBodyEntry = (value: string) =>
    value.startsWith(FOR_EACH_PREFIX)
      ? setEntry(nodeRef("for_each", value.slice(FOR_EACH_PREFIX.length)), owner)
      : setEntry(nodeRef("step", value), owner);
  const lanes = selectedGroup?.lanes || [];
  const renderEntrySelect = (
    key: string,
    label: string,
    entryStepId: string,
    steps: Array<{ primaryLabel: string; runtimeId: string }>,
    onSelect: (stepId: string) => void,
  ) => (
    <label className="block text-xs fs-ink-brand" key={key}>
      <span>{label}</span>
      <select
        aria-label={label}
        className="fs-input mt-1 w-full"
        onChange={(event) => {
          if (event.target.value) {
            onSelect(event.target.value);
          }
        }}
        value={entryStepId}
      >
        <option value="">{t("react.workflows.no_entry_selected", "No entry selected")}</option>
        {steps.map((step) => (
          <option key={step.runtimeId} value={step.runtimeId}>{step.primaryLabel}</option>
        ))}
      </select>
    </label>
  );

  if (structureLocked) {
    return (
      <div className={drawerMode === "group" ? "mt-3 block" : "mt-3 hidden"} data-testid="protected-container-view" id="group-editor-surface">
        <div className="space-y-2 rounded-xl border border-default fs-surface-inset p-3 text-xs fs-ink-secondary">
          <p className="text-sm fs-ink-primary">{groupDraft.displayName || groupId}</p>
          {groupDraft.description ? <p>{groupDraft.description}</p> : null}
          <p>
            {isForEach
              ? t("react.workflows.for_each_source_item", "{item} in {over}", { item: groupDraft.itemName, over: groupDraft.over })
              : usesLanes
                ? t("react.workflows.group_kind_parallel", "Parallel lanes")
                : t("react.workflows.group_body_one_after_another", "One after another")}
          </p>
          <ul className="space-y-1">
            {(selectedGroup?.steps || []).map((step) => (
              <li key={step.runtimeId}>
                <button
                  className="w-full rounded-lg border border-default fs-surface-inset px-2 py-1 text-left text-xs fs-ink-primary hover:border-[var(--fs-border-selected)]"
                  onClick={() => openStepDrawer(step.runtimeId)}
                  type="button"
                >
                  {step.primaryLabel}
                </button>
              </li>
            ))}
          </ul>
          <p className="text-[11px] fs-ink-tertiary">
            {t("react.workflows.protected_container_hint", "This part belongs to a protected workflow and is read-only.")}
          </p>
        </div>
      </div>
    );
  }

  /** One after another or Parallel lanes; on a saved Group the switch keeps its steps and their connections. */
  function changeBodyKind(bodyKind: GroupDraft["bodyKind"]) {
    setConcurrencyError(false);
    let lanes = groupDraft.lanes;
    if (bodyKind === "sequence") lanes = groupCreateMode ? groupDraft.lanes : "";
    else if (!groupCreateMode && selectedGroup?.bodyKind === "lanes") lanes = selectedGroup.lanes.join(", ");
    else if (!lanes.trim()) lanes = !groupCreateMode && preview ? groupChainLaneIds(preview, groupId).join(", ") : "lane_a, lane_b";
    updateDraft({ bodyKind, lanes });
  }
  const bodyKindSelect = (
    <label className="block text-xs fs-ink-secondary">
      <span>{isForEach ? t("react.workflows.group_kind", "Group kind") : t("react.workflows.group_steps_run", "Steps run")}</span>
      <select
        className={INPUT_CLASS}
        name="group_body_kind"
        onChange={(event) => changeBodyKind(event.target.value === "lanes" ? "lanes" : "sequence")}
        value={groupDraft.bodyKind}
      >
        <option value="sequence">
          {isForEach
            ? t("react.workflows.for_each_body_sequence", "Steps in sequence for each item")
            : t("react.workflows.group_body_one_after_another", "One after another")}
        </option>
        <option value="lanes">
          {isForEach
            ? t("react.workflows.loop_body_lanes", "Parallel lanes per item")
            : t("react.workflows.group_kind_parallel", "Parallel lanes")}
        </option>
      </select>
    </label>
  );

  const createLabel = isForEach
    ? t("react.workflows.add_for_each_to_changes", "Add For each to changes")
    : t("react.workflows.add_group_to_changes", "Add group to changes");
  const saveStaged = Boolean(ctx.groupSaveSerial?.staged);
  const saveLabels = {
    label: isForEach ? t("react.workflows.save_for_each", "Save For each") : t("react.workflows.save_group", "Save group"),
    savingLabel: isForEach ? t("react.workflows.save_for_each_saving", "Saving For each…") : t("react.workflows.save_group_saving", "Saving group…"),
    doneLabel: saveStaged ? t("react.workflows.save_done_updated", "Updated") : t("react.workflows.save_done_saved", "Saved"),
    doneAnnouncement: saveStaged
      ? t("react.workflows.group_changes_staged", "Group changes added to the unapplied structure.")
      : isForEach
        ? t("react.workflows.for_each_settings_saved", "For each settings saved.")
        : t("react.workflows.group_settings_saved", "Group settings saved."),
  };

  /** The workflow (or the parent Group body) can start at this container. */
  function renderContainerEntry(): ReactNode {
    const ref = nodeRef(isForEach ? "for_each" : "group", groupId);
    const parentGroupId = isForEach ? groupDraft.parentGroupId : "";
    const parent = sequenceGroups.find((group) => group.group_id === parentGroupId);
    const isEntry = parent
      ? groupBodyEntryStepId(parent) === groupId
      : workflowRootEntryId === `${ref.kind}:${groupId}`;
    if (isEntry) {
      return (
        <p className="text-xs fs-ink-success">
          {parent
            ? t("react.workflows.for_each_is_group_entry", "The group starts with this For each.")
            : t("react.workflows.group_is_workflow_entry", "The workflow starts with this group.")}
        </p>
      );
    }
    return (
      <Button
        variant="secondary"
        onClick={() => setEntry(ref, parent ? { groupId: parentGroupId } : {})}
        type="button"
      >
        {parent
          ? t("react.workflows.make_body_entry", "Make it the entry of {group}", { group: parent.display_name || parentGroupId })
          : t("react.workflows.make_workflow_entry", "Make it the workflow entry")}
      </Button>
    );
  }

  return (
    <div className={drawerMode === "group" ? "mt-3 block" : "mt-3 hidden"} id="group-editor-surface">
      {renderWhen(drawerTab === "settings", (
        <div className="space-y-3 rounded-xl border border-default fs-surface-inset p-3">
          <p className="text-xs uppercase tracking-[0.12em] fs-ink-secondary">
            {isForEach
              ? groupCreateMode
                ? t("react.workflows.new_for_each", "New For each")
                : t("react.workflows.for_each_settings", "For each settings")
              : groupCreateMode
                ? t("react.workflows.new_group", "New group")
                : t("react.workflows.group_settings", "Group settings")}
          </p>
          {renderWhen(Boolean(groupCreateMode), (
            <div className="grid gap-2 sm:grid-cols-2">
              {bodyKindSelect}
              <label className="block text-xs fs-ink-secondary">
                <span>{isForEach ? t("react.workflows.for_each_id", "For each ID") : t("react.workflows.group_id", "Group ID")}</span>
                <input
                  className={INPUT_CLASS}
                  name="group_id"
                  onChange={(event) => updateDraft({ groupId: event.target.value })}
                  placeholder={isForEach ? "each_order" : "prepare_orders"}
                  value={groupDraft.groupId}
                />
              </label>
            </div>
          ))}
          {renderWhen(Boolean(!groupCreateMode && !isForEach && selectedGroup), bodyKindSelect)}
          <label className="block text-xs fs-ink-secondary">
            <span>{t("react.workflows.display_name", "Display name")}</span>
            <input
              className={INPUT_CLASS}
              name="group_display_name"
              onChange={(event) => updateDraft({ displayName: event.target.value })}
              value={groupDraft.displayName}
            />
          </label>
          <label className="block text-xs fs-ink-secondary">
            <span>{t("registry.workflows.simulation.description", "Description")}</span>
            <textarea
              className={`${INPUT_CLASS} min-h-16`}
              name="group_description"
              onChange={(event) => updateDraft({ description: event.target.value })}
              value={groupDraft.description}
            />
          </label>
          {renderWhen(isForEach, (
            <div className="space-y-2 rounded-xl border border-default fs-surface-inset p-3" data-testid="for-each-settings">
              <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-primary">
                {t("react.workflows.for_each_loop_policy", "Loop policy")}</p>
              <p className="text-xs leading-5 fs-ink-secondary">
                {groupDraft.bodyKind === "lanes"
                  ? t("react.workflows.for_each_lanes_hint", "For each item, the lanes run side by side; the next item starts when every lane is done.")
                  : t("react.workflows.for_each_sequence_hint", "The steps inside run once per item, one item at a time. Collect, Break and Continue work here.")}
              </p>
              <label className="block text-xs fs-ink-secondary" ref={sourceLabel}>
                <RequiredLabel>{t("react.workflows.for_each_loop_source", "Loop source")}</RequiredLabel>
                {renderWorkflowVariableInput({
                  ariaLabel: t("react.workflows.for_each_loop_source", "Loop source"),
                  ariaRequired: true,
                  ariaInvalid: Boolean(sourceError),
                  ariaDescribedBy: sourceError ? sourceErrorId : undefined,
                  className: "fs-input mt-1 w-full font-mono",
                  name: "for_each_over",
                  onChange: (nextValue: string) => updateDraft({ over: nextValue }),
                  panelId: workflowVariableSuggestionPanelId("for-each-source"),
                  placeholder: "state.items",
                  value: groupDraft.over,
                })}
                {sourceError ? <span className="fs-field-helper error block" id={sourceErrorId}>{sourceError}</span> : null}
              </label>
              <div className="grid gap-2 sm:grid-cols-2">
                <label className="block text-xs fs-ink-secondary">
                  <span>{t("react.workflows.loop_item_name", "Item name")}</span>
                  <input
                    className="fs-input mt-1 w-full font-mono"
                    name="for_each_item_name"
                    onChange={(event) => updateDraft({ itemName: event.target.value })}
                    value={groupDraft.itemName}
                  />
                </label>
                <label className="block text-xs fs-ink-secondary">
                  <OptionalLabel>{t("react.workflows.index_name", "Index name")}</OptionalLabel>
                  <input
                    className="fs-input mt-1 w-full font-mono"
                    name="for_each_index_name"
                    onChange={(event) => updateDraft({ indexName: event.target.value })}
                    placeholder="index"
                    value={groupDraft.indexName}
                  />
                </label>
              </div>
              <label className="block text-xs fs-ink-secondary">
                <span>{t("react.workflows.placement", "Placement")}</span>
                <select
                  className="fs-input mt-1 w-full"
                  name="for_each_parent_group"
                  onChange={(event) => {
                    if (groupCreateMode) {
                      updateDraft({ parentGroupId: event.target.value });
                    } else {
                      ignoreAsyncResult(Promise.resolve(handleMoveForEach(event.target.value)));
                    }
                  }}
                  value={groupDraft.parentGroupId}
                >
                  <option value="">{t("react.workflows.placement_root", "Workflow (root)")}</option>
                  {sequenceGroups.map((group) => (
                    <option key={group.group_id} value={group.group_id}>
                      {group.display_name || group.group_id}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          ))}
          {renderWhen(usesLanes, (
            <div className="space-y-2 rounded-xl border border-default fs-surface-inset p-3">
              <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-primary">
                {t("react.workflows.parallel_lanes", "Parallel lanes")}</p>
              <label className="block text-xs fs-ink-secondary">
                <span>{t("react.workflows.lane_ids", "Lane IDs (comma separated)")}</span>
                <input
                  className="fs-input mt-1 w-full"
                  name="group_lanes"
                  onChange={(event) => updateDraft({ lanes: event.target.value })}
                  value={groupDraft.lanes}
                />
              </label>
              <label className="block text-xs fs-ink-secondary">
                <OptionalLabel>{t("react.workflows.lane_concurrency_limit", "Lanes running at once")}</OptionalLabel>
                <input
                  className="fs-input mt-1 w-full"
                  aria-describedby={showConcurrencyError ? concurrencyErrorId : undefined}
                  aria-invalid={showConcurrencyError || undefined}
                  inputMode="numeric"
                  name="group_concurrency_limit"
                  onChange={(event) => {
                    setConcurrencyError(false);
                    updateDraft({ concurrencyLimit: event.target.value });
                  }}
                  ref={concurrencyInput}
                  value={groupDraft.concurrencyLimit}
                />
              </label>
              {showConcurrencyError ? (
                <p className="text-xs text-[var(--fs-text-danger)]" id={concurrencyErrorId} role="alert">
                  {t("react.workflows.concurrency_positive_integer", "Enter a whole number greater than zero, or leave this blank.")}
                </p>
              ) : null}
              {renderWhen(!groupCreateMode, (
                <p className="text-[11px] fs-ink-secondary">
                  {t("react.workflows.group_lanes_structure_hint", "Adding or removing a lane changes the structure; it is applied together with the other structure changes.")}
                </p>
              ))}
            </div>
          ))}
          {renderWhen(Boolean(!groupCreateMode && selectedGroup), (
            <div className="space-y-2 rounded-xl border border-selected fs-surface-selected p-3">
              <p className="text-xs font-medium uppercase tracking-[0.12em] fs-ink-brand">
                {isForEach
                  ? t("react.workflows.for_each_steps", "Steps for each item")
                  : t("react.workflows.group_steps", "Steps in this group")}</p>
              {selectedGroup?.bodyKind === "sequence"
                ? renderEntrySelect(
                    "body",
                    t("react.workflows.group_body_entry", "Body entry"),
                    bodyEntryValue(groupBodyEntryStepId(selectedGroup.raw)),
                    [...selectedGroup.steps.filter((step) => !step.laneId), ...childForEachEntries],
                    setBodyEntry,
                  )
                : null}
              {lanes.map((laneId) =>
                renderEntrySelect(
                  laneId,
                  t("react.workflows.lane_entry_value0", "Entry of lane {value0}", { value0: laneId }),
                  selectedGroup ? groupBodyEntryStepId(selectedGroup.raw, laneId) : "",
                  (selectedGroup?.steps || []).filter((step) => step.laneId === laneId),
                  (stepId) => setEntry(nodeRef("step", stepId), { ...owner, laneId }),
                ),
              )}
              <ul className="space-y-1">
                {(selectedGroup?.steps || []).map((step) => (
                  <li key={step.runtimeId}>
                    <button
                      className="w-full rounded-lg border border-default fs-surface-inset px-2 py-1 text-left text-xs fs-ink-primary hover:border-[var(--fs-border-selected)]"
                      onClick={() => openStepDrawer(step.runtimeId)}
                      type="button"
                    >
                      {step.primaryLabel}
                      {step.laneId ? <span className="ml-2 fs-ink-tertiary">{step.laneId}</span> : null}
                    </button>
                  </li>
                ))}
              </ul>
              <div className="flex flex-wrap gap-2">
                {selectedGroup?.bodyKind === "sequence" ? (
                  <Button
                    variant="secondary"
                    onClick={() => openNewStepDrawer({ ...owner, laneId: "" })}
                    type="button"
                  >
                    {isForEach
                      ? t("react.workflows.add_step_to_for_each", "Add step for each item")
                      : t("react.workflows.add_step_to_group", "Add step to this group")}
                  </Button>
                ) : null}
                {lanes.map((laneId) => (
                  <Button
                    variant="secondary"
                    key={laneId}
                    onClick={() => openNewStepDrawer({ ...owner, laneId })}
                    type="button"
                  >
                    {t("react.workflows.add_step_to_lane", "Add step to lane {lane}", { lane: laneId })}
                  </Button>
                ))}
              </div>
              {renderContainerEntry()}
            </div>
          ))}
          {renderWhen(Boolean(!groupCreateMode && selectedGroup && preview), (
            <div className="rounded-xl border border-default fs-surface-inset p-3">
              <ContainerConnections
                connectPort={connectPort}
                containerId={groupId}
                containerKind={isForEach ? "for_each" : "group"}
                disconnectPort={disconnectPort}
                preview={preview}
              />
            </div>
          ))}
          {drawerMode === "group" ? <DrawerFooter>
            {groupCreateMode ? <>
              <DrawerFooterSpacer />
              <Button disabled={savingWorkflowSurface === "group"} onClick={() => ctx.cancelNewDrawerItem?.()} type="button" variant="secondary">
                {t("common.cancel", "Cancel")}
              </Button>
              <Button disabled={savingWorkflowSurface === "group"} onClick={saveGroup} type="button" variant="primary">
                {createLabel}
              </Button>
            </> : <>
              {selectedGroup ? (
                <Button
                  disabled={savingWorkflowSurface === "group"}
                  onClick={() => ignoreAsyncResult(handleDeleteGroup())}
                  type="button"
                  variant="ghost-danger"
                >
                  {isForEach
                    ? t("react.workflows.delete_for_each", "Delete For each")
                    : t("react.workflows.delete_group", "Delete group")}
                </Button>
              ) : null}
              <DrawerFooterSpacer />
              <SaveStateButton
                {...saveLabels}
                dirty={Boolean(ctx.groupFormDirty || ctx.groupFormSaveFailed)}
                doneSignal={ctx.groupSaveSerial?.count}
                failed={Boolean(ctx.groupFormSaveFailed)}
                onClick={saveGroup}
                saving={savingWorkflowSurface === "group"}
              />
            </>}
          </DrawerFooter> : null}
          {canConvertGroup && !isForEach && !groupCreateMode && selectedGroup ? (
            <div className="space-y-2">
              <Button
                variant="secondary"
                disabled={Boolean(groupExtractionBlockedReason || savingWorkflowSurface)}
                onClick={onConvertGroup}
                type="button"
              >
                {t("react.workflows.convert_group_to_child", "Convert group to child workflow")}
              </Button>
              {groupExtractionBlockedReason ? <p className="text-xs fs-ink-attention">{groupExtractionBlockedReason}</p> : null}
            </div>
          ) : null}
        </div>
      ))}
      {renderWhen(drawerMode === "group" && drawerTab === "automation" && Boolean(groupId) && !isForEach, (
        <div
          className="space-y-2 rounded-xl border border-default fs-surface-inset p-3"
          id="group-automation-surface"
        >
          <p className="text-xs uppercase tracking-[0.12em] fs-ink-secondary">
            {t("react.workflows.group_automation", "Group automation")}</p>
          <p className="text-xs fs-ink-secondary">
            {t("react.workflows.group_automation_hint", "React when this group starts, completes or fails, and to the steps and waits inside it.")}</p>
          <AutomationRuleEditor
            onDirtyChange={ctx.onAutomationDirtyChange}
            projectId={selectedProjectId}
            scopeId={groupId}
            scopeKind="group"
            workflowId={workflowId}
          />
        </div>
      ))}
    </div>
  );
}
