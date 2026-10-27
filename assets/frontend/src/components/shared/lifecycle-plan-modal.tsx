import { useI18n } from "@/lib/i18n/useI18n";
import { useMemo, useState } from "react";

import { DsAlert, DsBadge, DsCheckbox, DsDialog } from "@/components/shared/design-system";
import { LocalizedStatusBadge } from "@/components/shared/localized-status-badge";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export type LifecycleTreeNode = {
  node_id: string;
  entity_type: string;
  entity_id: string;
  label: string;
  selectable?: boolean;
  selected?: boolean;
  mandatory?: boolean;
  status?: string;
  children?: LifecycleTreeNode[];
};

export type LifecyclePlan = {
  action_type: string;
  plan_version: string;
  root_entity: { entity_type: string; entity_id: string; label: string };
  tree: LifecycleTreeNode;
  impact_summary?: Record<string, unknown>;
  auto_managed_cleanup_groups?: Array<{ group_key: string; label: string }>;
  direct_dependency_conflicts?: Array<Record<string, unknown>>;
  warnings?: string[];
  validation_errors?: string[];
};

type Props = {
  busy?: boolean;
  error?: string;
  onClose: () => void;
  onConfirm: (selectedNodeIds: string[]) => void;
  open: boolean;
  plan: LifecyclePlan | null;
};

function collectDefaults(node: LifecycleTreeNode, selected: Set<string>) {
  if (node.selectable && node.selected !== false && node.node_id) {
    selected.add(node.node_id);
  }
  (node.children || []).forEach((child) => collectDefaults(child, selected));
}

type LifecyclePlanModalContentProps = {
  activePlan: LifecyclePlan;
  busy: boolean;
  error: string;
  initialSelectedNodeIds: Set<string>;
  onClose: () => void;
  onConfirm: (selectedNodeIds: string[]) => void;
};

function LifecyclePlanModalContent({
  activePlan,
  busy,
  error,
  initialSelectedNodeIds,
  onClose,
  onConfirm,
}: Readonly<LifecyclePlanModalContentProps>) {
  const { t } = useI18n();
  const [selectedNodeIds, setSelectedNodeIds] = useState<Set<string>>(
    () => new Set(initialSelectedNodeIds),
  );
  const selectedCount = useMemo(() => selectedNodeIds.size, [selectedNodeIds]);
  const restoring = activePlan.action_type === "restore";

  function toggleSubtree(node: LifecycleTreeNode, nextChecked: boolean, next: Set<string>) {
    if (node.selectable && node.node_id) {
      if (nextChecked) {
        next.add(node.node_id);
      } else {
        next.delete(node.node_id);
      }
    }
    (node.children || []).forEach((child) => toggleSubtree(child, nextChecked, next));
  }

  function handleToggle(nodeId: string, nextChecked: boolean) {
    const next = new Set(selectedNodeIds);
    const walk = (node: LifecycleTreeNode | null): boolean => {
      if (!node) return false;
      if (node.node_id === nodeId) {
        toggleSubtree(node, nextChecked, next);
        return true;
      }
      return (node.children || []).some((child) => walk(child));
    };
    walk(activePlan.tree);
    setSelectedNodeIds(next);
  }

  // The depth used to be an inline `marginLeft: level * 18`. Nesting carries the
  // same hierarchy without a pixel literal in the markup, and each level indents
  // by one published spacing step.
  function renderTree(node: LifecycleTreeNode) {
    const children = node.children || [];
    return (
      <div key={node.node_id}>
        <DsCheckbox
          checked={selectedNodeIds.has(node.node_id)}
          disabled={!node.selectable || node.mandatory}
          onChange={(checked) => handleToggle(node.node_id, checked)}
        >
          {node.label} <DsBadge>{node.entity_type.replaceAll("_", " ")}</DsBadge>
          {node.status ? (
            <>
              {" "}
              <LocalizedStatusBadge tone={node.status === "active" ? "green" : "neutral"} value={node.status} />
            </>
          ) : null}
        </DsCheckbox>
        {children.length ? (
          <div className="pl-4">{children.map((child) => renderTree(child))}</div>
        ) : null}
      </div>
    );
  }

  return (
    <DsDialog
      busy={busy}
      description={activePlan.root_entity.label}
      eyebrow={activePlan.root_entity.entity_type.replaceAll("_", " ")}
      footer={
        <>
          <button className="fs-button" onClick={onClose} type="button">
            {t("common.cancel", "Cancel")} </button>
          <button
            className={restoring ? "fs-button primary" : "fs-button danger"}
            disabled={busy || Boolean(activePlan.validation_errors?.length)}
            onClick={() => onConfirm(Array.from(selectedNodeIds))}
            type="button"
          >
            {restoring ? t("react.shared.restore_selected", "Restore selected") : t("react.shared.hard_delete_selected", "Hard delete selected")}
          </button>
        </>
      }
      onClose={onClose}
      open
      title={restoring ? t("react.shared.restore_plan", "Restore plan") : t("react.shared.hard_delete_plan", "Hard delete plan")}
      variant="form"
    >
      <div className="fs-dialog-form">
        <p>{t("react.shared.selected_nodes", "Selected nodes:")} {selectedCount}</p>

        {activePlan.auto_managed_cleanup_groups?.length ? (
          <DsAlert title={t("react.shared.auto_managed_cleanup", "Auto-managed cleanup")}>
            {activePlan.auto_managed_cleanup_groups.map((group) => group.label).join(" · ")}
          </DsAlert>
        ) : null}
        {activePlan.direct_dependency_conflicts?.length ? (
          <DsAlert title={t("react.shared.dependency_conflicts", "Dependency conflicts")} tone="warning">
            {activePlan.direct_dependency_conflicts
              .map((conflict) =>
                firstStringFromUnknownValue([
                  conflict.message,
                  conflict.reason,
                  conflict.entity_id,
                  t("react.shared.selection_conflict", "Selection conflict"),
                ]),
              )
              .join(" · ")}
          </DsAlert>
        ) : null}
        {activePlan.warnings?.length ? (
          <DsAlert title={t("react.shared.review_before_continuing", "Review before continuing")} tone="warning">
            {activePlan.warnings.join(" ")}
          </DsAlert>
        ) : null}
        {activePlan.validation_errors?.length ? (
          <DsAlert title={t("react.shared.this_plan_cannot_run", "This plan cannot run")} tone="danger">
            {activePlan.validation_errors.join(" ")}
          </DsAlert>
        ) : null}
        {error ? (
          <DsAlert title={t("react.shared.lifecycle_action_failed", "Lifecycle action failed")} tone="danger">
            {error}
          </DsAlert>
        ) : null}

        <div>{renderTree(activePlan.tree)}</div>
      </div>
    </DsDialog>
  );
}

export function LifecyclePlanModal({ busy = false, error = "", onClose, onConfirm, open, plan }: Readonly<Props>) {
  const planSelectionKey = useMemo(() => JSON.stringify(plan?.tree || null), [plan]);
  const initialSelectedNodeIds = useMemo(() => {
    const defaults = new Set<string>();
    if (plan?.tree) {
      collectDefaults(plan.tree, defaults);
    }
    return defaults;
  }, [plan]);

  if (!open || !plan) {
    return null;
  }

  return (
    <LifecyclePlanModalContent
      key={planSelectionKey}
      activePlan={plan}
      busy={busy}
      error={error}
      initialSelectedNodeIds={initialSelectedNodeIds}
      onClose={onClose}
      onConfirm={onConfirm}
    />
  );
}
