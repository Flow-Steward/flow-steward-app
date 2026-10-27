import { useI18n } from "@/lib/i18n/useI18n";
import { useHasCapability } from "@/lib/auth/capabilities";
import { missingExtensionHeadline, missingExtensionNotice } from "@/pages/workflows/shared/missing-extension";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useMemo } from "react";

import { useQuery } from "@tanstack/react-query";

import { queryKeys } from "@/lib/query/keys";
import {
  fetchWorkflowCanvasGraph,
  fetchWorkflowContract,
  fetchWorkflowDag,
  fetchWorkflowDetail,
  fetchWorkflowExportPolicy,
  fetchWorkflowValidation,
} from "@/pages/workflows/shared/api";
import {
  fetchAllProjectsForWorkflowContext,
  findWorkflowLinkedProjectId,
} from "@/pages/workflows/shared/project-context";
import {
  buildWorkflowValidationIssueGroups,
  listFromUnknown,
  normalizeWorkflowDefinition,
  objectOrEmpty,
  textOrEmpty,
  workflowIsScaffoldOnly,
} from "@/pages/workflows/detail/detail-helpers";
import {
  definitionFromWorkflow,
  locallyCreatedForEachIds,
  locallyCreatedGroupIds,
  locallyCreatedStepIds,
  type WorkflowDefinitionModel,
} from "@/pages/workflows/builder/change-set";
import type { WorkflowChangeOperation } from "@/pages/workflows/builder/api";

export function useWorkflowDetailDataController(ctx: Record<string, any>) {
  const { t } = useI18n();
  const {
    builderPath,
    drawerMode,
    workflowTestRunLaunchOpen,
    searchParams,
    surface,
    workflowId,
  } = ctx;

  const detailQuery = useQuery({
    queryKey: queryKeys.workflowDetail(workflowId),
    queryFn: () => fetchWorkflowDetail(workflowId),
    enabled: Boolean(workflowId),
    refetchOnMount: "always",
  });
  const validationQuery = useQuery({
    queryKey: queryKeys.workflowValidation(workflowId),
    queryFn: () => fetchWorkflowValidation(workflowId),
    enabled: Boolean(workflowId),
  });
  const dagQuery = useQuery({
    queryKey: queryKeys.workflowDag(workflowId),
    queryFn: () => fetchWorkflowDag(workflowId),
    enabled: Boolean(workflowId),
    refetchOnMount: "always",
  });
  const canvasGraphQuery = useQuery({
    queryKey: queryKeys.workflowCanvasGraph(workflowId),
    queryFn: () => fetchWorkflowCanvasGraph(workflowId),
    enabled: surface === "builder" && Boolean(workflowId),
    refetchOnMount: "always",
  });
  const exportPolicyQuery = useQuery({
    queryKey: ["workflows", "export-policy", workflowId],
    queryFn: () => fetchWorkflowExportPolicy(workflowId),
    enabled: Boolean(workflowId),
    retry: false,
    refetchOnMount: "always",
  });
  const workflowContractQuery = useQuery({
    queryKey: ["workflows", "contract", workflowId],
    queryFn: () => fetchWorkflowContract(workflowId),
    enabled: Boolean(workflowId),
    retry: false,
    refetchOnMount: "always",
  });

  const workflow = detailQuery.data;
  const workflowRecord = objectOrEmpty(workflow);

  const canAdministerPlatform = useHasCapability("administer_platform");
  /* The structured reason a missing extension leaves, from whichever payload
     carries it. The builder's one notice words it for a person and offers the
     fix; the plain sentence below stays for drawers and older payloads. */
  const workflowMissingExtensions = useMemo(
    () => missingExtensionNotice([
      canvasGraphQuery.data,
      objectOrEmpty(workflowRecord.portability),
      exportPolicyQuery.data,
      workflowRecord,
    ], canAdministerPlatform),
    [canAdministerPlatform, canvasGraphQuery.data, exportPolicyQuery.data, workflowRecord],
  );
  const workflowAvailabilityNotice = useMemo(() => {
    if (workflowMissingExtensions) return missingExtensionHeadline(workflowMissingExtensions, t);
    const canvasGraphRecord = objectOrEmpty(canvasGraphQuery.data);
    const workflowPortability = objectOrEmpty(workflowRecord.portability);
    const sourceInstallId = textOrEmpty(
      canvasGraphRecord.source_install_id ||
        workflowPortability.source_install_id ||
        exportPolicyQuery.data?.source_install_id ||
        workflowRecord.source_install_id,
    );
    const workflowEnabled = canvasGraphRecord.workflow
      ? Boolean(objectOrEmpty(canvasGraphRecord.workflow).enabled)
      : Boolean(workflowRecord.enabled);
    const availabilityStatus = textOrEmpty(
      canvasGraphRecord.availability_status ||
        workflowPortability.availability_status ||
        exportPolicyQuery.data?.availability_status ||
        workflowRecord.availability_status,
    ).toLowerCase();
    const availabilityReason = textOrEmpty(
      canvasGraphRecord.availability_reason_text ||
        workflowPortability.availability_reason_text ||
        workflowPortability.reason ||
        exportPolicyQuery.data?.availability_reason_text ||
        exportPolicyQuery.data?.reason ||
        workflowRecord.availability_reason_text,
    );
    if (availabilityStatus && availabilityStatus !== "available") {
      return availabilityReason || t("react.workflows.this_workflow_is_currently_unavailable", "This workflow is currently unavailable.");
    }
    if (sourceInstallId && workflowEnabled === false) {
      return t("react.workflows.install_is_disabled", "Install is disabled.");
    }
    return "";
  }, [
    workflowMissingExtensions,
    canvasGraphQuery.data,
    exportPolicyQuery.data,
    workflowRecord.availability_reason_text,
    workflowRecord.availability_status,
    workflowRecord.portability,
    workflowRecord.source_install_id,
    workflowRecord.enabled, t
  ]);

  const workflowDisabledForEditingOnly = useMemo(() => {
    const canvasGraphRecord = objectOrEmpty(canvasGraphQuery.data);
    const workflowPortability = objectOrEmpty(workflowRecord.portability);
    const sourceInstallId = textOrEmpty(
      canvasGraphRecord.source_install_id ||
        workflowPortability.source_install_id ||
        exportPolicyQuery.data?.source_install_id ||
        workflowRecord.source_install_id,
    );
    const workflowEnabled = canvasGraphRecord.workflow
      ? Boolean(objectOrEmpty(canvasGraphRecord.workflow).enabled)
      : Boolean(workflowRecord.enabled);
    const availabilityStatus = textOrEmpty(
      canvasGraphRecord.availability_status ||
        workflowPortability.availability_status ||
        exportPolicyQuery.data?.availability_status ||
        workflowRecord.availability_status,
    ).toLowerCase();
    const availabilityReasonCode = textOrEmpty(
      canvasGraphRecord.availability_reason_code ||
        workflowPortability.availability_reason_code ||
        exportPolicyQuery.data?.availability_reason_code ||
        workflowRecord.availability_reason_code,
    ).toLowerCase();
    if (sourceInstallId || workflowEnabled !== false) {
      return false;
    }
    return (
      (!availabilityStatus ||
        availabilityStatus === "unavailable" ||
        availabilityStatus === "disabled") &&
      (!availabilityReasonCode ||
        availabilityReasonCode === "workflow_disabled")
    );
  }, [canvasGraphQuery.data, exportPolicyQuery.data, workflowRecord]);

  const workflowProtectionNotice = useMemo(() => {
    const canvasGraphRecord = objectOrEmpty(canvasGraphQuery.data);
    const workflowPortability = objectOrEmpty(workflowRecord.portability);
    const sourceInstallId = textOrEmpty(
      canvasGraphRecord.source_install_id ||
        workflowPortability.source_install_id ||
        exportPolicyQuery.data?.source_install_id ||
        workflowRecord.source_install_id,
    );
    const groups = Array.isArray(canvasGraphRecord.groups)
      ? canvasGraphRecord.groups
      : [];
    const steps = Array.isArray(canvasGraphRecord.steps)
      ? canvasGraphRecord.steps
      : [];
    const protectedPlaceholderGraph =
      surface === "builder" &&
      (Boolean(sourceInstallId) ||
        canvasGraphRecord.protected_workflow === true) &&
      groups.length === 0 &&
      steps.length === 0;
    const workflowEnabled = canvasGraphRecord.workflow
      ? Boolean(objectOrEmpty(canvasGraphRecord.workflow).enabled)
      : Boolean(workflowRecord.enabled);
    if (!workflowEnabled && sourceInstallId) {
      return "";
    }
    if (protectedPlaceholderGraph) {
      return (
        textOrEmpty(
          workflowPortability.reason || exportPolicyQuery.data?.reason,
        ) ||
        t("react.workflows.this_workflow_is_provided_by_an_extension_and_cannot_be_edited_here", "This workflow is provided by an extension and cannot be edited here.")
      );
    }
    if (
      surface === "builder" &&
      (exportPolicyQuery.data?.extension_owned === true ||
        workflowPortability.extension_owned === true) &&
      (exportPolicyQuery.data?.structure_change_allowed === false ||
        workflowPortability.structure_change_allowed === false)
    ) {
      return (
        textOrEmpty(
          exportPolicyQuery.data?.reason || workflowPortability.reason,
        ) ||
        t("react.workflows.this_workflow_is_provided_by_an_extension_and_cannot_be_edited_here", "This workflow is provided by an extension and cannot be edited here.")
      );
    }
    return "";
  }, [
    canvasGraphQuery.data,
    exportPolicyQuery.data,
    surface,
    workflowRecord.enabled,
    workflowRecord.portability,
    workflowRecord.source_install_id, t
  ]);

  const workflowRunsBlocked = useMemo(() => {
    const canvasGraphRecord = objectOrEmpty(canvasGraphQuery.data);
    const workflowPortability = objectOrEmpty(workflowRecord.portability);
    const sourceInstallId = textOrEmpty(
      canvasGraphRecord.source_install_id ||
        workflowPortability.source_install_id ||
        exportPolicyQuery.data?.source_install_id ||
        workflowRecord.source_install_id,
    );
    const workflowEnabled = canvasGraphRecord.workflow
      ? Boolean(objectOrEmpty(canvasGraphRecord.workflow).enabled)
      : Boolean(workflowRecord.enabled);
    const availabilityStatus = textOrEmpty(
      canvasGraphRecord.availability_status ||
        workflowPortability.availability_status ||
        exportPolicyQuery.data?.availability_status ||
        workflowRecord.availability_status,
    ).toLowerCase();
    const availabilityReasonCode = textOrEmpty(
      canvasGraphRecord.availability_reason_code ||
        workflowPortability.availability_reason_code ||
        exportPolicyQuery.data?.availability_reason_code ||
        workflowRecord.availability_reason_code,
    ).toLowerCase();
    const disabledOnlyAvailability =
      !sourceInstallId &&
      workflowEnabled === false &&
      (!availabilityStatus ||
        availabilityStatus === "unavailable" ||
        availabilityStatus === "disabled") &&
      (!availabilityReasonCode ||
        availabilityReasonCode === "workflow_disabled");
    return (
      Boolean(exportPolicyQuery.data?.new_runs_blocked) ||
      Boolean(workflowPortability.new_runs_blocked) ||
      (Boolean(sourceInstallId) && workflowEnabled === false) ||
      (Boolean(availabilityStatus) &&
        availabilityStatus !== "available" &&
        !disabledOnlyAvailability)
    );
  }, [canvasGraphQuery.data, exportPolicyQuery.data, workflowRecord]);

  const workflowDrawerNotice = workflowDisabledForEditingOnly
    ? workflowProtectionNotice
    : workflowAvailabilityNotice || workflowProtectionNotice;
  const workflowDrawerNoticeTitle = workflowRunsBlocked
    ? t("react.workflows.workflow_unavailable", "Workflow unavailable")
    : t("react.workflows.display_protected_workflow", "Protected workflow");

  const explicitProjectId =
    textOrEmpty(searchParams.get("project_id")) ||
    textOrEmpty(workflowRecord.project_id) ||
    textOrEmpty(objectOrEmpty(workflowRecord.project).id);
  const linkedProjectQuery = useQuery({
    queryKey: ["workflows", workflowId, "linked-project-id"],
    queryFn: () => findWorkflowLinkedProjectId(workflowId),
    enabled: Boolean(workflowId) && detailQuery.isSuccess && !explicitProjectId,
    staleTime: 30_000,
  });
  const selectedProjectId = explicitProjectId || textOrEmpty(linkedProjectQuery.data);
  const availableProjectsQuery = useQuery({
    queryKey: ["workflows", workflowId, "project-context-options"],
    queryFn: () => fetchAllProjectsForWorkflowContext(),
    enabled:
      Boolean(workflowId) &&
      (!selectedProjectId ||
        workflowTestRunLaunchOpen ||
        drawerMode === "workflow-settings" ||
        drawerMode === "workflow-test-run" ||
        drawerMode === "step"),
    staleTime: 60_000,
  });

  const workflowPolicy =
    exportPolicyQuery.data || objectOrEmpty(workflowRecord.portability);
  const fullExportAllowed = Boolean(workflowPolicy.full_export_allowed);
  // A protected extension workflow offers no export: its definition belongs to the extension.
  const exportYamlHref = fullExportAllowed
    ? `/api/workflows/${encodeURIComponent(workflowId)}/export.yaml`
    : "";
  const exportYamlLabel = t("react.workflows.export_yaml", "Export YAML");
  const workflowDetailPathWithProject = selectedProjectId
    ? `/registry/workflows/${encodeURIComponent(workflowId)}?project_id=${encodeURIComponent(selectedProjectId)}`
    : `/registry/workflows/${encodeURIComponent(workflowId)}`;
  const builderPathWithProject = selectedProjectId
    ? `${builderPath}?project_id=${encodeURIComponent(selectedProjectId)}`
    : builderPath;
  const workflowTitle = textOrEmpty(workflow?.display_name) || workflowId;
  const workflowDescription =
    textOrEmpty(workflow?.description) || t("react.workflows.display_no_workflow_description", "No workflow description configured.");

  const serverDefinition = useMemo(() => definitionFromWorkflow(workflow), [workflow]);
  const serverRevision = Number(workflowRecord.revision || 0) || null;
  const workflowScaffoldOnly = useMemo(
    () => workflowIsScaffoldOnly(serverDefinition.steps),
    [serverDefinition],
  );

  const validation = validationQuery.data?.validation;
  const isValid = Boolean(validation?.valid);
  const errorCount = Array.isArray(validation?.errors)
    ? validation.errors.length
    : 0;
  const warningCount = Array.isArray(validation?.warnings)
    ? validation.warnings.length
    : 0;
  const validationErrors = listFromUnknown(validation?.errors);
  const validationWarnings = listFromUnknown(validation?.warnings);
  const dagNodeCount = Number(dagQuery.data?.nodes?.length || 0);
  const dagEdgeCount = Number(dagQuery.data?.edges?.length || 0);
  const workflowPortability = objectOrEmpty(workflowRecord.portability);
  const installedSourceStructureLocked =
    (exportPolicyQuery.data?.extension_owned === true ||
      workflowPortability.extension_owned === true) &&
    (exportPolicyQuery.data?.structure_change_allowed === false ||
      workflowPortability.structure_change_allowed === false);
  const protectedWorkflowStructureRedacted =
    (exportPolicyQuery.data?.extension_owned === true ||
      workflowPortability.extension_owned === true) &&
    (exportPolicyQuery.data?.protected === true || workflowPortability.protected === true);
  // A workflow whose structure cannot change (a protected author graph) opens
  // on a read-only canvas: its real structure, with hooks still connectable.
  const canvasStructureLocked =
    exportPolicyQuery.data?.structure_change_allowed === false ||
    workflowPortability.structure_change_allowed === false;

  return {
    availableProjectsQuery,
    builderPathWithProject,
    canvasStructureLocked,
    canvasGraphQuery,
    dagEdgeCount,
    dagNodeCount,
    dagQuery,
    detailQuery,
    errorCount,
    exportPolicyQuery,
    exportYamlHref,
    exportYamlLabel,
    isValid,
    installedSourceStructureLocked,
    linkedProjectQuery,
    protectedWorkflowStructureRedacted,
    selectedProjectId,
    serverDefinition,
    serverRevision,
    validationErrors,
    validationQuery,
    validationWarnings,
    warningCount,
    workflow,
    workflowAvailabilityNotice,
    workflowMissingExtensions,
    workflowContractQuery,
    workflowProtectionNotice,
    workflowDescription,
    workflowDetailPathWithProject,
    workflowDrawerNotice,
    workflowDrawerNoticeTitle,
    workflowRecord,
    workflowRunsBlocked,
    workflowScaffoldOnly,
    workflowTitle,
  };
}

/**
 * Drawer and list rows of the definition the builder shows: the server's,
 * with the unapplied change set applied on top.
 */
export function useWorkflowDefinitionRows({
  operations,
  preview,
  selectedGroupId,
  selectedStepId,
  validationErrors,
  validationWarnings,
}: {
  operations: WorkflowChangeOperation[];
  preview: WorkflowDefinitionModel;
  selectedGroupId: string;
  selectedStepId: string;
  validationErrors: Array<Record<string, unknown>>;
  validationWarnings: Array<Record<string, unknown>>;
}) {
  const { t } = useI18n();
  const definitionRows = useMemo(
    () =>
      normalizeWorkflowDefinition(
        preview.steps,
        preview.groups,
        preview.forEach,
        locallyCreatedStepIds(operations),
        new Set([...locallyCreatedGroupIds(operations), ...locallyCreatedForEachIds(operations)]),
      ),
    [operations, preview],
  );
  const selectedStep = definitionRows.stepRowsById.get(selectedStepId) || null;
  const selectedGroup =
    definitionRows.groupRows.find((group) => group.groupId === selectedGroupId) || null;
  const validationErrorGroups = useMemo(
    () => buildWorkflowValidationIssueGroups(validationErrors, definitionRows, "error", t),
    [definitionRows, validationErrors, t],
  );
  const validationWarningGroups = useMemo(
    () => buildWorkflowValidationIssueGroups(validationWarnings, definitionRows, "warning", t),
    [definitionRows, validationWarnings, t],
  );
  const publishedBlocks = useMemo(
    () =>
      definitionRows.stepRows
        .filter((step) => Boolean(textOrEmpty(step.blockKey)))
        .map((step) => ({
          block_key: step.blockKey,
          display_name: textOrEmpty(step.primaryLabel) || step.blockKey,
          mapping_resolved: true,
        })),
    [definitionRows],
  );
  return {
    definitionRows,
    publishedBlocks,
    selectedGroup,
    selectedStep,
    validationErrorGroups,
    validationWarningGroups,
  };
}
