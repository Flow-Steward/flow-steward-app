import { useEffect, useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router";

import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { DsAlert } from "@/components/shared/design-system";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { useI18n } from "@/lib/i18n/useI18n";
import { queryKeys } from "@/lib/query/keys";
import { fetchWorkflowContract, fetchWorkflowDetail } from "@/pages/workflows/shared/api";
import { findWorkflowLinkedProjectId } from "@/pages/workflows/shared/project-context";
import { isWorkflowSettingsTab, type WorkflowChannelState } from "@/pages/workflows/shared/workflow-page-tabs";
import { AutomationRuleEditor } from "./detail-automation-editor";
import { createWorkflowInterfaceRenderers } from "./detail-interface-renderers";
import { objectOrEmpty } from "./detail-helpers";
import { useWorkflowLeaveGuard } from "./detail-leave-guard";
import { WorkflowScheduleProjectContext } from "./detail-schedule-project-context";
import { useWorkflowTriggersController } from "./detail-triggers-controller";
import { WorkflowTriggersSurface } from "./detail-triggers-surface";
import { useWorkflowSettingsController, useWorkflowSettingsHydration } from "./detail-workflow-settings-controller";
import { WorkflowHistorySurface } from "./detail-workflow-history-surface";
import { WorkflowSettingsSmartSetup } from "./detail-workflow-settings-smart-setup";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

import "./detail-builder-inspectors.css";

type Props = {
  workflowId: string;
  projectId: string;
  /** The page tab on screen: a settings tab shows this surface, any other hides it with its drafts kept. */
  tab: string;
  onSelectTab: (tab: string) => void;
  onSaved: () => Promise<void>;
  /** The unsaved channel draft, so the Webhook and Schedules tabs follow it at once; null when nothing is unsaved. */
  onChannelDraft: (draft: WorkflowChannelState | null) => void;
};

function TriggersTab({ ctx }: Readonly<{ ctx: Parameters<typeof useWorkflowTriggersController>[0] }>) {
  const triggers = useWorkflowTriggersController(ctx);
  return <WorkflowTriggersSurface ctx={{ ...ctx, ...triggers }} />;
}

/**
 * The workflow page's settings tabs: Smart setup, Webhook, Schedules, Automation
 * and History, over one settings form whose drafts and unsaved-changes guard
 * outlive a change of tab. Graph and step authoring stay in Builder.
 */
export default function WorkflowSettingsSurface({ workflowId, projectId, tab, onSelectTab, onSaved, onChannelDraft }: Readonly<Props>) {
  const { t } = useI18n();
  const canAuthor = useIsAdmin();
  const [searchParams, setSearchParams] = useSearchParams();
  const [, setSaveNotice] = useState("");
  const [saveError, setSaveError] = useState("");
  const [savingWorkflowSurface, setSavingWorkflowSurface] = useState("");
  const [triggersWasOpened, setTriggersWasOpened] = useState(tab === "webhook");
  const workflowInterfaceControlIdPrefix = useId();
  const active = canAuthor && isWorkflowSettingsTab(tab);
  // The triggers controller and surface know the Webhook tab by its drawer key.
  const drawerTab = tab === "webhook" ? "triggers" : tab;
  const detail = useQuery({ queryKey: queryKeys.workflowDetail(workflowId), queryFn: () => fetchWorkflowDetail(workflowId), enabled: canAuthor, retry: false });
  const contract = useQuery({ queryKey: ["workflows", "contract", workflowId], queryFn: () => fetchWorkflowContract(workflowId), enabled: canAuthor, retry: false });
  const linkedProjectQuery = useQuery({ queryKey: ["workflows", workflowId, "linked-project-id"], queryFn: () => findWorkflowLinkedProjectId(workflowId), enabled: canAuthor && !projectId && !detail.isPending && !detail.data?.project_id && !objectOrEmpty(detail.data?.project).id, retry: false, staleTime: 30_000 });
  const workflowRecord = objectOrEmpty(detail.data);
  const sourcePolicy = objectOrEmpty(workflowRecord.portability);
  const structureLocked = sourcePolicy.structure_change_allowed === false
    || Boolean(workflowRecord.source_install_id || sourcePolicy.source_install_id);
  const selectedProjectId = projectId || firstStringFromUnknownValue([workflowRecord.project_id, objectOrEmpty(workflowRecord.project).id, linkedProjectQuery.data]);
  const refetchWorkflowSurfaces = async () => {
    await Promise.all([detail.refetch({ throwOnError: true }), contract.refetch({ throwOnError: true })]);
    await onSaved();
  };
  function changeDrawerTab(next: string) {
    onSelectTab(next === "triggers" ? "webhook" : next);
  }
  const settings = useWorkflowSettingsController({
    workflowId, workflowRecord, workflowInterfaceControlIdPrefix, setDrawerTab: changeDrawerTab, setSaveNotice, setSaveError, drawerMode: "workflow-settings",
    setSavingWorkflowSurface: (value: string) => {
      if (value === "workflow") {
        setSaveError("");
      }
      setSavingWorkflowSurface(value);
    },
    refetchWorkflowSurfaces,
  });
  useWorkflowSettingsHydration({ ...settings, workflow: detail.data, workflowContractQuery: contract, workflowId, savingWorkflowSurface });
  const leave = useWorkflowLeaveGuard(settings.workflowFormDirty || settings.workflowFormSaveFailed || savingWorkflowSurface === "workflow");

  const unsaved = settings.workflowFormDirty;
  const channelsKey = `${unsaved}|${settings.workflowInterfaceEnabledDraft}|${settings.workflowInterfaceTriggerChannelsDraft.join(",")}`;
  useEffect(() => {
    onChannelDraft(unsaved
      ? { enabled: settings.workflowInterfaceEnabledDraft, channels: settings.workflowInterfaceTriggerChannelsDraft }
      : null);
    // The key carries the draft; the callback only reports it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channelsKey]);
  if (drawerTab === "triggers" && !triggersWasOpened) setTriggersWasOpened(true);

  const ctx = {
    ...settings, ...createWorkflowInterfaceRenderers({ ...settings, workflowInterfaceControlIdPrefix }, t),
    workflowInterfaceControlIdPrefix, workflowId, workflowRecord, workflowContractQuery: contract,
    linkedProjectQuery, selectedProjectId, searchParams, savingWorkflowSurface, advancedMode: false,
    drawerOpen: active, drawerMode: "workflow-settings", drawerTab, structureLocked,
    changeDrawerTab, setDrawerTab: changeDrawerTab,
    refetchWorkflowSurfaces, serverRevision: Number(workflowRecord.revision) || undefined,
  };
  const loadError = detail.error || contract.error;
  const ready = detail.isSuccess && contract.isSuccess;
  return <>
    <div hidden={!active} inert={!active}>
      {loadError ? <DsAlert title={t("react.workflows.workflow_unavailable", "Workflow unavailable")} tone="danger">{loadError.message}</DsAlert> : !ready ? <p>{t("registry.workflow_detail.loading", "Loading...")}</p> : <div className="flex flex-col gap-3">
        {/* A save that landed says so inside its button; only a failure stays on screen. */}
        {saveError ? <DsAlert title={t("react.workflows.not_saved", "Not saved")} tone="danger"><p>{saveError}</p></DsAlert> : null}
        <div id="workflow-settings-surface"><WorkflowSettingsSmartSetup ctx={ctx} /></div>
        {triggersWasOpened ? <TriggersTab ctx={ctx} /> : null}
        {active && drawerTab === "schedules" ? <WorkflowScheduleProjectContext workflowId={workflowId} projectHint={projectId} onProjectChange={(id) => { const next = new URLSearchParams(searchParams); next.set("project_id", id); setSearchParams(next, { replace: true }); }} onTurnOnScheduleChannel={() => changeDrawerTab("smart-setup")} /> : null}
        {active && drawerTab === "automation" ? <AutomationRuleEditor projectId={selectedProjectId} scopeKind="workflow" workflowId={workflowId} /> : null}
        {active && drawerTab === "history" ? <WorkflowHistorySurface ctx={ctx} /> : null}
      </div>}
    </div>
    <ConfirmDialog {...leave} title={t("project_edit.dirty.unsaved_changes", "Unsaved changes")} />
  </>;
}
