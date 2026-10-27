import { parseGroupConcurrency } from "./detail-group-concurrency";
import { announceCanvasToast, watchGraphIntent } from "@/pages/workflows/builder/canvas-toast-feed";
import { planGroupBodyChange } from "@/pages/workflows/builder/canvas-group-selection";
import { useI18n } from "@/lib/i18n/useI18n";
import { useState } from "react";
/* eslint-disable @typescript-eslint/no-explicit-any */
import {
  applyWorkflowChanges,
  isWorkflowRevisionConflict,
  workflowChangeErrorIssues,
  type WorkflowChangeOperation,
} from "@/pages/workflows/builder/api";
import {
  groupLanes,
  uniqueLocalId,
  type WorkflowDefinitionModel,
} from "@/pages/workflows/builder/change-set";
import { commaList, objectOrEmpty, textOrEmpty } from "@/pages/workflows/detail/detail-helpers";
import type { GroupDraft } from "@/pages/workflows/detail/detail-drawer-controller";
import type { WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";
import type { WorkflowForEach, WorkflowGroup } from "@/pages/workflows/shared/api";

function lanesFromDraft(draft: GroupDraft): string[] {
  return [...new Set(commaList(draft.lanes))];
}

export function groupBodyFromDraft(draft: GroupDraft, existing: Record<string, unknown> = {}): Record<string, unknown> {
  if (draft.bodyKind === "sequence") {
    return existing.kind === "sequence" ? { ...existing } : { kind: "sequence" };
  }
  const existingLanes = groupLanes({ body: existing });
  const lanes = lanesFromDraft(draft).map((laneId) => {
    const kept = existingLanes.find((lane) => textOrEmpty(lane.lane_id) === laneId);
    return kept ? { ...kept } : { lane_id: laneId };
  });
  const body: Record<string, unknown> = { kind: "lanes", lanes };
  const limit = parseGroupConcurrency(draft.concurrencyLimit);
  if (limit.valid && limit.value !== undefined) {
    body.concurrency_limit = limit.value;
  }
  return body;
}

/** The For each's own settings: its source and the names its body reads. */
export function forEachSettingsFromDraft(draft: GroupDraft): Record<string, unknown> {
  return {
    over: draft.over.trim(),
    item_name: draft.itemName.trim() || "item",
    index_name: draft.indexName.trim() || null,
  };
}

export function useWorkflowGroupActions(ctx: Record<string, any>) {
  const { t } = useI18n();
  // Every group save that lands bumps this: ✓ Saved, or ✓ Updated when it joined the unapplied structure.
  const [groupSaveSerial, setGroupSaveSerial] = useState({ count: 0, staged: false });
  const markGroupSaved = (staged: boolean) => setGroupSaveSerial((current) => ({ count: current.count + 1, staged }));
  const {
    changeSet,
    groupCreateMode,
    groupDraft,
    refetchWorkflowSurfaces,
    requestConfirmation,
    deletionGuard,
    selectedGroup,
    serverRevision,
    setGroupCreateMode,
    setGroupDraft,
    setSaveError,
    setSaveNotice,
    setSavingWorkflowSurface,
    setSelectedGroupId,
    setDrawerMode,
    workflowId,
  } = ctx as Record<string, any> & { changeSet: WorkflowChangeSetController; groupDraft: GroupDraft };

  const isForEach = groupDraft.containerKind === "for_each";
  const updateOp = (id: string, patch: Record<string, unknown>): WorkflowChangeOperation =>
    isForEach
      ? { op: "update_for_each_config", for_each_id: id, patch }
      : { op: "update_group_config", group_id: id, patch };

  function draftValidation(): string {
    if (isForEach && !groupDraft.over.trim()) {
      return t("react.workflows.for_each_source_required", "Enter the list the For each goes through.");
    }
    if (groupDraft.bodyKind === "lanes" && !lanesFromDraft(groupDraft).length) {
      return t("react.workflows.group_lanes_required", "Add at least one lane.");
    }
    if (groupDraft.bodyKind === "lanes" && !parseGroupConcurrency(groupDraft.concurrencyLimit).valid) {
      return t("react.workflows.concurrency_positive_integer", "Enter a whole number greater than zero, or leave this blank.");
    }
    return "";
  }

  function stageNewGroup() {
    const preview = changeSet.preview as WorkflowDefinitionModel;
    // Ids are unique across steps, Groups and For each containers.
    const groupId = uniqueLocalId(
      textOrEmpty(groupDraft.groupId) || textOrEmpty(groupDraft.displayName),
      [
        ...preview.steps.map((step) => textOrEmpty(step.step_id)),
        ...preview.groups.map((group) => textOrEmpty(group.group_id)),
        ...preview.forEach.map((item) => textOrEmpty(item.for_each_id)),
      ],
      isForEach ? "for_each" : "group",
    );
    const description = groupDraft.description.trim();
    const shared = {
      display_name: groupDraft.displayName.trim() || groupId,
      body: groupBodyFromDraft(groupDraft),
      ...(description ? { description } : {}),
    };
    const notice = isForEach
      ? t("react.workflows.for_each_added_to_changes", "For each added to the unapplied structure. Add its steps, then apply.")
      : t("react.workflows.group_added_to_changes", "Group added to the unapplied structure. Add its steps, then apply.");
    if (isForEach) {
      const item: WorkflowForEach = {
        for_each_id: groupId,
        group_id: groupDraft.parentGroupId || null,
        ...shared,
        ...(forEachSettingsFromDraft(groupDraft) as Pick<WorkflowForEach, "over" | "item_name">),
      };
      changeSet.stage({ op: "create_for_each", for_each: item }, notice);
    } else {
      const group: WorkflowGroup = { group_id: groupId, ...shared };
      changeSet.stage({ op: "create_group", group }, notice);
    }
    setGroupCreateMode(false);
    setSelectedGroupId(groupId);
    setSaveNotice(notice);
  }

  async function handleSaveGroup() {
    const validation = draftValidation();
    if (validation) {
      setSaveError(validation);
      return;
    }
    if (groupCreateMode) {
      stageNewGroup();
      return;
    }
    if (!selectedGroup) {
      return;
    }
    const groupId = textOrEmpty(selectedGroup.groupId);
    const raw = selectedGroup.raw;
    const configPatch: Record<string, unknown> = {
      display_name: groupDraft.displayName.trim() || groupId,
      description: groupDraft.description.trim(),
      ...(isForEach ? forEachSettingsFromDraft(groupDraft) : {}),
    };
    if (!isForEach && selectedGroup.bodyKind && groupDraft.bodyKind !== selectedGroup.bodyKind) {
      // One after another ↔ Parallel lanes keeps the steps and their connections: one structural intent.
      const limit = parseGroupConcurrency(groupDraft.concurrencyLimit);
      const plan = planGroupBodyChange(changeSet.preview as WorkflowDefinitionModel, groupId, {
        kind: groupDraft.bodyKind,
        laneIds: lanesFromDraft(groupDraft),
        ...(limit.valid && limit.value !== undefined ? { concurrencyLimit: limit.value } : {}),
      }, t);
      if (!plan.ok) {
        setSaveError(plan.reason);
        return;
      }
      const message = t("react.workflows.group_body_kind_staged", "How the group's steps run changed, so this save is part of the unapplied structure.");
      changeSet.stage([updateOp(groupId, configPatch), ...plan.operations], message);
      setSaveNotice(message);
      markGroupSaved(true);
      return;
    }
    const nextBody = groupBodyFromDraft(groupDraft, objectOrEmpty(raw.body));
    const bodyChanged = JSON.stringify(nextBody) !== JSON.stringify(objectOrEmpty(raw.body));
    if (selectedGroup.local) {
      changeSet.stage(updateOp(groupId, { ...configPatch, body: nextBody }));
      setSaveNotice(t("react.workflows.group_changes_staged", "Group changes added to the unapplied structure."));
      markGroupSaved(true);
      return;
    }
    if (bodyChanged) {
      // Lanes are the group's structure: they go with the other unapplied structure changes.
      changeSet.stage(
        updateOp(groupId, { ...configPatch, body: nextBody }),
        t("react.workflows.group_lanes_staged", "The group's lanes changed, so this save is part of the unapplied structure."),
      );
      setSaveNotice(t("react.workflows.group_lanes_staged", "The group's lanes changed, so this save is part of the unapplied structure."));
      markGroupSaved(true);
      return;
    }
    if (changeSet.touchesGroup(groupId)) {
      setSaveError(
        t(
          "react.workflows.group_save_blocked_by_structure",
          "This group is part of the unapplied structure changes. Apply or discard them before saving its settings.",
        ),
      );
      return;
    }
    if (!serverRevision) {
      setSaveError(t("react.workflows.change_set_revision_unknown", "The workflow revision is unknown. Reload the workflow."));
      return;
    }
    setSaveNotice("");
    setSavingWorkflowSurface("group");
    try {
      const result = await applyWorkflowChanges(workflowId, {
        expected_revision: serverRevision,
        operations: [updateOp(groupId, configPatch)],
      });
      if (result?.revision) {
        changeSet.rebase(result.revision, serverRevision);
        // The saved settings are one action of the history: Undo takes them back locally.
        changeSet.recordSavedChange?.([updateOp(groupId, configPatch)], result.revision);
      }
      await refetchWorkflowSurfaces();
      setSaveNotice(
        isForEach
          ? t("react.workflows.for_each_settings_saved", "For each settings saved.")
          : t("react.workflows.group_settings_saved", "Group settings saved."),
      );
      markGroupSaved(false);
    } catch (error) {
      if (isWorkflowRevisionConflict(error)) {
        changeSet.reportConflict();
        setSaveError(
          t(
            "react.workflows.step_save_conflict",
            "The workflow changed after it was loaded. Nothing was saved; reload the workflow and save again.",
          ),
        );
        return;
      }
      const issueText = workflowChangeErrorIssues(error)
        .map((issue) => textOrEmpty(issue.message))
        .filter(Boolean)
        .join(" ");
      setSaveError(
        issueText ||
          (error instanceof Error ? error.message : t("react.workflows.failed_to_save_group", "Failed to save group.")),
      );
    } finally {
      setSavingWorkflowSurface("");
    }
  }

  async function handleDeleteGroup() {
    if (!selectedGroup) {
      return;
    }
    const groupId = textOrEmpty(selectedGroup.groupId);
    // Delete acts at once; the canvas toast names what went and offers Undo.
    const name = String(selectedGroup.displayName || groupId);
    const notice = isForEach
      ? t("react.workflows.for_each_delete_staged", "For each deletion added to the unapplied structure.")
      : t("react.workflows.group_delete_staged", "Group deletion added to the unapplied structure.");
    await deletionGuard.requestDelete({ kind: isForEach ? "for_each" : "group", id: groupId }, () => {
      const undo = watchGraphIntent(changeSet);
      changeSet.stageGraphIntent(
        isForEach
          ? [{ op: "delete_for_each", for_each_id: groupId, with_contents: true }]
          : [{ op: "delete_group", group_id: groupId, with_contents: true }],
        notice,
      );
      setSelectedGroupId("");
      setDrawerMode("");
      setSaveNotice(notice);
      announceCanvasToast({ label: t("react.workflows.node_deleted", "{name} deleted.", { name }), tone: "success", action: undo(t("react.workflows.undo", "Undo")) });
    });
  }

  /** Move the selected For each to the root or into a sequence Group, with its body. */
  async function handleMoveForEach(parentGroupId: string) {
    if (selectedGroup?.containerKind !== "for_each") {
      return false;
    }
    const forEachId = textOrEmpty(selectedGroup.groupId);
    if (parentGroupId === selectedGroup.parentGroupId) {
      return false;
    }
    const confirmed = await requestConfirmation({
      title: t("react.workflows.move_for_each_title", "Move this For each?"),
      body: t(
        "react.workflows.move_for_each_body",
        "It moves with every step inside it. Its own connections are removed by the move; connect it again in its new place before applying.",
      ),
      confirmLabel: t("react.workflows.move_for_each_confirm", "Move For each"),
    });
    if (!confirmed) {
      return false;
    }
    const notice = t("react.workflows.move_for_each_staged", "Move added to the unapplied structure. Reconnect the For each, then apply.");
    changeSet.stage({ op: "move_for_each", for_each_id: forEachId, group_id: parentGroupId || null }, notice);
    setGroupDraft((current: GroupDraft) => ({ ...current, parentGroupId }));
    setSaveNotice(notice);
    return true;
  }

  return {
    groupSaveSerial,
    handleDeleteGroup,
    handleMoveForEach,
    handleSaveGroup,
  };
}
