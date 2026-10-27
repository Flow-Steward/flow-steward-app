import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

import { builderMetadataDraftFromUnknown, builderMetadataPayload } from "@/pages/registry/shared/builder-metadata";
import {
  jsonObjectFromDraft,
  jsonText,
  stringList,
  textOrEmpty,
  contractRows,
  workflowFieldDraftsFromUnknown,
  objectOrEmpty,
  saveJson,
  workflowFieldPayload,
  workflowOutputFieldPayload,
  type WorkflowInterfaceFieldDraft,
} from "@/pages/workflows/detail/detail-helpers";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import {
  clearWorkflowInputSampleFile,
  clearWorkflowInputSecret,
  setWorkflowInputSampleFile,
  setWorkflowInputSecret,
} from "@/pages/workflows/shared/api";

type SecretActionFailure = { name: string; message: string };
type InterfaceNameFocusRequest = { workflowId: string; tab: string; id: string; serial: number };

function responseRevision(value: unknown, fallback: number): number {
  const revision = Number((value as { revision?: unknown } | null)?.revision);
  return Number.isInteger(revision) && revision > 0 ? revision : fallback;
}

export async function applyWorkflowInputSecretActions({
  workflowId,
  initialRevision,
  rows,
}: {
  workflowId: string;
  initialRevision: number;
  rows: WorkflowInterfaceFieldDraft[];
}, t: I18nContextValue["t"] = fallbackContext.t): Promise<{
  revision: number;
  applied: string[];
  failures: SecretActionFailure[];
}> {
  let revision = initialRevision;
  const applied: string[] = [];
  const failures: SecretActionFailure[] = [];
  for (const row of rows) {
    const action = row.secretAction || "";
    if (!action || String(row.type || "").trim() !== "secret" || !row.name.trim()) {
      continue;
    }
    try {
      const response =
        action === "set"
          ? await setWorkflowInputSecret({
              workflowId,
              name: row.name,
              value: String(row.secretDraft ?? ""),
              expectedRevision: revision,
            })
          : await clearWorkflowInputSecret({
              workflowId,
              name: row.name,
              expectedRevision: revision,
            });
      revision = responseRevision(response, revision);
      applied.push(row.name);
    } catch (error) {
      failures.push({
        name: row.name,
        message:
          error instanceof Error && error.message.trim()
            ? error.message.trim()
            : t("react.workflows.secret_update_failed", "Secret update failed."),
      });
    }
  }
  return { revision, applied, failures };
}

/** Upload or remove the test files picked in the editor, one request each. */
export async function applyWorkflowInputSampleFileActions({
  workflowId,
  initialRevision,
  rows,
}: {
  workflowId: string;
  initialRevision: number;
  rows: WorkflowInterfaceFieldDraft[];
}, t: I18nContextValue["t"] = fallbackContext.t): Promise<{
  revision: number;
  applied: string[];
  failures: SecretActionFailure[];
}> {
  let revision = initialRevision;
  const applied: string[] = [];
  const failures: SecretActionFailure[] = [];
  for (const row of rows) {
    const action = row.sampleFileAction || "";
    const file = row.sampleFileDraft ?? null;
    if (!action || String(row.type || "").trim() !== "file" || !row.name.trim()) {
      continue;
    }
    if (action === "set" && !file) {
      continue;
    }
    try {
      const response =
        action === "set" && file
          ? await setWorkflowInputSampleFile({
              workflowId,
              name: row.name,
              file,
              expectedRevision: revision,
            })
          : await clearWorkflowInputSampleFile({
              workflowId,
              name: row.name,
              expectedRevision: revision,
            });
      revision = responseRevision(response, revision);
      applied.push(row.name);
    } catch (error) {
      failures.push({
        name: row.name,
        message:
          error instanceof Error && error.message.trim()
            ? error.message.trim()
            : t("react.workflows.test_file_update_failed", "Test file update failed."),
      });
    }
  }
  return { revision, applied, failures };
}

export function useWorkflowSettingsController(ctx: Record<string, any>) {
  const { t } = useI18n();
  const {
    changeSet,
    drawerMode,
    refetchWorkflowSurfaces,
    setDrawerTab,
    setSaveError,
    setSaveNotice,
    setSavingWorkflowSurface,
    workflowId,
    workflowInterfaceControlIdPrefix,
    workflowRecord,
  } = ctx;

  const [workflowFormDirty, setWorkflowFormDirty] = useState(false);
  const [workflowFormSaveFailed, setWorkflowFormSaveFailed] = useState(false);
  const [workflowInterfaceSchemaVisualIssueDraft, setWorkflowInterfaceSchemaVisualIssueDraft] = useState(false);
  const [workflowInterfaceNameValidationAttempted, setWorkflowInterfaceNameValidationAttempted] = useState(false);
  const [workflowInterfaceNameFocusRequest, setWorkflowInterfaceNameFocusRequest] = useState<InterfaceNameFocusRequest | null>(null);
  const workflowInterfaceNameFocusSerial = useRef(0);
  function trackChange<T>(setter: Dispatch<SetStateAction<T>>) {
    return (value: SetStateAction<T>) => {
      setWorkflowFormDirty(true);
      setter(value);
    };
  }
  const [workflowDisplayNameDraft, setWorkflowDisplayNameDraft] = useState("");
  const [workflowDescriptionDraft, setWorkflowDescriptionDraft] = useState("");
  const [workflowEnabledDraft, setWorkflowEnabledDraft] = useState(true);
  const [workflowExpectedFailureDraft, setWorkflowExpectedFailureValue] = useState(false);
  const [workflowExpectedFailureEdited, setWorkflowExpectedFailureEdited] = useState(false);
  const setWorkflowExpectedFailureDraft = (value: boolean) => {
    setWorkflowFormDirty(true);
    setWorkflowExpectedFailureEdited(true);
    setWorkflowExpectedFailureValue(value);
  };
  const rawTags = workflowRecord.tags;
  let intentTags: unknown = rawTags;
  if (typeof rawTags === "string") {
    try { intentTags = JSON.parse(rawTags); } catch { intentTags = []; }
  }
  const workflowExpectedFailureTag = Array.isArray(intentTags) && intentTags.some((tag) =>
    typeof tag === "string" && ["expected-failure", "expected_failure"].includes(tag.trim().toLowerCase()));
  const [workflowInterfaceEnabledDraft, setWorkflowInterfaceEnabledDraft] =
    useState(false);
  const [
    workflowInterfaceTriggerChannelsDraft,
    setWorkflowInterfaceTriggerChannelsDraft,
  ] = useState<string[]>([]);
  const [workflowInterfaceInputModeDraft, setWorkflowInterfaceInputModeDraft] =
    useState("fields");
  const [
    workflowInterfaceStrictInputsDraft,
    setWorkflowInterfaceStrictInputsDraft,
  ] = useState(true);
  const [workflowInterfaceInputsDraft, setWorkflowInterfaceInputsDraft] =
    useState<WorkflowInterfaceFieldDraft[]>([]);
  const [
    workflowInterfaceInputSchemaDraft,
    setWorkflowInterfaceInputSchemaDraft,
  ] = useState("");
  const [workflowInterfaceOutputsDraft, setWorkflowInterfaceOutputsDraft] =
    useState<WorkflowInterfaceFieldDraft[]>([]);
  // Read only: each returned file is a step result ticked "Return from workflow".
  const [workflowInterfaceReturnedFiles, setWorkflowInterfaceReturnedFiles] =
    useState<Array<Record<string, unknown>>>([]);
  const [workflowBuilderPurposeDraft, setWorkflowBuilderPurposeDraft] =
    useState("");
  const [workflowBuilderUseCasesDraft, setWorkflowBuilderUseCasesDraft] =
    useState("");
  const [workflowBuilderAntiUseCasesDraft, setWorkflowBuilderAntiUseCasesDraft] =
    useState("");
  const [
    workflowBuilderCompositionRoleDraft,
    setWorkflowBuilderCompositionRoleDraft,
  ] = useState("");
  const [
    workflowBuilderDeterminismLevelDraft,
    setWorkflowBuilderDeterminismLevelDraft,
  ] = useState("hybrid");
  const [
    workflowBuilderSafeForAutoUseDraft,
    setWorkflowBuilderSafeForAutoUseDraft,
  ] = useState(false);
  async function handleSaveWorkflowSettings() {
    const normalizedWorkflowId = firstStringFromUnknownValue([workflowId, ""]).trim();
    if (!normalizedWorkflowId) {
      return;
    }
    setSaveNotice("");
    const firstBlankInput = workflowInterfaceInputModeDraft === "fields"
      ? workflowInterfaceInputsDraft.find((row) => !row.name.trim())
      : undefined;
    const firstBlankOutput = workflowInterfaceOutputsDraft.find((row) => !row.name.trim());
    const firstBlankNameTab = firstBlankInput ? "inputs" : firstBlankOutput ? "outputs" : "";
    if (firstBlankNameTab) {
      setWorkflowInterfaceNameValidationAttempted(true);
      setWorkflowFormSaveFailed(true);
      setSaveError(t("react.registry.name_is_required", "Name is required."));
      // Smart setup holds the whole contract: it stays, and the unnamed row opens there.
      const focusTab = drawerMode === "workflow-settings" ? "smart-setup" : firstBlankNameTab;
      setDrawerTab(focusTab);
      const invalidRow = firstBlankInput || firstBlankOutput;
      const direction = firstBlankInput ? "input" : "output";
      if (invalidRow) {
        const nameId = `${workflowInterfaceControlIdPrefix}-${direction}-${invalidRow.draftId}-name`;
        workflowInterfaceNameFocusSerial.current += 1;
        setWorkflowInterfaceNameFocusRequest({
          workflowId: normalizedWorkflowId,
          tab: focusTab,
          id: nameId,
          serial: workflowInterfaceNameFocusSerial.current,
        });
      }
      return;
    }
    setWorkflowInterfaceNameValidationAttempted(false);
    setWorkflowInterfaceNameFocusRequest(null);
    setWorkflowFormSaveFailed(false);
    setSavingWorkflowSurface("workflow");
    try {
      if (workflowInterfaceInputModeDraft === "json_schema" && workflowInterfaceSchemaVisualIssueDraft) {
        throw new Error(t("react.workflows.structured_input_names_required", "Give each structured input and member a unique name before saving."));
      }
      const existingSettings = objectOrEmpty(workflowRecord.settings);
      const existingCallableSettings = objectOrEmpty(existingSettings.callable);
      const existingTraits = objectOrEmpty(existingSettings.traits);
      const callableContract: Record<string, unknown> = {
        ...existingCallableSettings,
        enabled: workflowInterfaceEnabledDraft,
        trigger_channels: workflowInterfaceTriggerChannelsDraft,
        input_mode: workflowInterfaceInputModeDraft,
        strict_inputs: workflowInterfaceStrictInputsDraft,
      };
      if (workflowInterfaceInputModeDraft === "json_schema") {
        callableContract.input_schema = jsonObjectFromDraft(
          workflowInterfaceInputSchemaDraft, t
        );
        callableContract.expected_inputs = Array.isArray(existingCallableSettings.expected_inputs)
          ? existingCallableSettings.expected_inputs : [];
      } else {
        callableContract.expected_inputs = workflowFieldPayload(
          workflowInterfaceInputsDraft,
          t,
        );
      }
      callableContract.expected_outputs = workflowOutputFieldPayload(
        workflowInterfaceOutputsDraft,
        t,
      );
      const savedWorkflow = await saveJson(
        `/api/workflows/${encodeURIComponent(normalizedWorkflowId)}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            expected_revision:
              Number.isInteger(Number(workflowRecord.revision)) &&
              Number(workflowRecord.revision) > 0
                ? Number(workflowRecord.revision)
                : undefined,
            builder_metadata_jsonb: builderMetadataPayload({
              antiUseCases: workflowBuilderAntiUseCasesDraft,
              compositionRole: workflowBuilderCompositionRoleDraft,
              determinismLevel: workflowBuilderDeterminismLevelDraft,
              purpose: workflowBuilderPurposeDraft,
              safeForAutoUse: workflowBuilderSafeForAutoUseDraft,
              useCases: workflowBuilderUseCasesDraft,
            }),
            display_name:
              workflowDisplayNameDraft.trim() || normalizedWorkflowId,
            description: workflowDescriptionDraft,
            enabled: workflowEnabledDraft,
            settings: {
              ...existingSettings,
              ...(workflowExpectedFailureEdited ? { traits: { ...existingTraits,
                expected_failure: workflowExpectedFailureDraft } } : {}),
              callable: callableContract,
            },
          }),
        },
      );
      // Secrets travel on their own write-only endpoints, never in the
      // settings body. Applied after the save so they act on the revision that
      // now exists, and each one reports its own failure rather than being
      // rolled into a generic "settings could not be saved".
      const secretResult = await applyWorkflowInputSecretActions({
        workflowId: normalizedWorkflowId,
        initialRevision: responseRevision(savedWorkflow, Number(workflowRecord.revision) + 1),
        rows: workflowInterfaceInputsDraft,
      }, t);
      const sampleFileResult = await applyWorkflowInputSampleFileActions({
        workflowId: normalizedWorkflowId,
        initialRevision: secretResult.revision,
        rows: workflowInterfaceInputsDraft,
      }, t);
      const appliedSecrets = new Set(secretResult.applied);
      const appliedSampleFiles = new Set(sampleFileResult.applied);
      setWorkflowInterfaceInputsDraft((current) =>
        current.map((row) => {
          let next = row;
          if (appliedSecrets.has(row.name)) {
            next = { ...next, secretAction: "", secretDraft: "" };
          }
          if (appliedSampleFiles.has(row.name)) {
            next = { ...next, sampleFileAction: "", sampleFileDraft: null };
          }
          return next;
        }),
      );
      changeSet?.rebase(sampleFileResult.revision, Number(workflowRecord.revision));
      await refetchWorkflowSurfaces();
      const notApplied = [
        ...secretResult.failures.map((failure) => `secret ${failure.name}: ${failure.message}`),
        ...sampleFileResult.failures.map(
          (failure) => t("react.workflows.display_test_file_failure", "test file {name}: {message}", { name: failure.name, message: failure.message }),
        ),
      ];
      setWorkflowFormDirty(notApplied.length > 0);
      setWorkflowExpectedFailureEdited(false);
      setWorkflowFormSaveFailed(notApplied.length > 0);
      // A partial save is a failure the author must see; a full one is said by the Save button.
      if (notApplied.length) {
        setSaveError(t("react.workflows.workflow_settings_saved_not_applied_value0", "Workflow settings saved. Not applied: {value0}.", { value0: notApplied.join("; ") }));
        return;
      }
      setSaveNotice(
        workflowInterfaceEnabledDraft && !workflowInterfaceTriggerChannelsDraft.length
          ? t("react.workflows.workflow_settings_saved_channels_not_ready", "Workflow settings saved. Not ready for Test Run: choose at least one trigger channel.")
          : t("react.workflows.workflow_settings_saved", "Workflow settings saved."),
      );
    } catch (error) {
      setWorkflowFormSaveFailed(true);
      const rawMessage = error instanceof Error ? error.message : "";
      setSaveError(
        rawMessage.includes("platform_workflow_requires_clone")
          ? t("react.workflows.this_platform_workflow_is_read_only_use_clone_workflow_to_create_an_editable_copy_for_this_account", "This platform workflow is read-only. Use “Clone Workflow” to create an editable copy for this account.")
          : rawMessage || t("react.workflows.display_failed_workflow_settings", "Failed to save workflow settings."),
      );
    } finally {
      setSavingWorkflowSurface("");
    }
  }


  return {
    handleSaveWorkflowSettings,
    setWorkflowFormDirty,
    workflowFormDirty,
    workflowFormSaveFailed,
    setWorkflowBuilderAntiUseCasesDraft: trackChange(setWorkflowBuilderAntiUseCasesDraft),
    setWorkflowBuilderCompositionRoleDraft: trackChange(setWorkflowBuilderCompositionRoleDraft),
    setWorkflowBuilderDeterminismLevelDraft: trackChange(setWorkflowBuilderDeterminismLevelDraft),
    setWorkflowBuilderPurposeDraft: trackChange(setWorkflowBuilderPurposeDraft),
    setWorkflowBuilderSafeForAutoUseDraft: trackChange(setWorkflowBuilderSafeForAutoUseDraft),
    setWorkflowBuilderUseCasesDraft: trackChange(setWorkflowBuilderUseCasesDraft),
    setWorkflowDescriptionDraft: trackChange(setWorkflowDescriptionDraft),
    setWorkflowDisplayNameDraft: trackChange(setWorkflowDisplayNameDraft),
    setWorkflowEnabledDraft: trackChange(setWorkflowEnabledDraft),
    setWorkflowExpectedFailureDraft,
    setWorkflowExpectedFailureValue,
    setWorkflowExpectedFailureEdited,
    setWorkflowInterfaceEnabledDraft: trackChange(setWorkflowInterfaceEnabledDraft),
    setWorkflowInterfaceInputModeDraft: trackChange(setWorkflowInterfaceInputModeDraft),
    setWorkflowInterfaceInputSchemaDraft: trackChange(setWorkflowInterfaceInputSchemaDraft),
    setWorkflowInterfaceSchemaVisualIssueDraft,
    setWorkflowInterfaceInputsDraft: trackChange(setWorkflowInterfaceInputsDraft),
    setWorkflowInterfaceOutputsDraft: trackChange(setWorkflowInterfaceOutputsDraft),
    setWorkflowInterfaceReturnedFiles,
    setWorkflowInterfaceStrictInputsDraft: trackChange(setWorkflowInterfaceStrictInputsDraft),
    setWorkflowInterfaceTriggerChannelsDraft: trackChange(setWorkflowInterfaceTriggerChannelsDraft),
    workflowBuilderAntiUseCasesDraft,
    workflowBuilderCompositionRoleDraft,
    workflowBuilderDeterminismLevelDraft,
    workflowBuilderPurposeDraft,
    workflowBuilderSafeForAutoUseDraft,
    workflowBuilderUseCasesDraft,
    workflowDescriptionDraft,
    workflowDisplayNameDraft,
    workflowEnabledDraft,
    workflowExpectedFailureDraft,
    workflowExpectedFailureTag,
    workflowInterfaceEnabledDraft,
    workflowInterfaceInputModeDraft,
    workflowInterfaceInputSchemaDraft,
    workflowInterfaceSchemaVisualIssueDraft,
    workflowInterfaceInputsDraft,
    workflowInterfaceNameFocusRequest,
    workflowInterfaceNameValidationAttempted,
    workflowInterfaceOutputsDraft,
    workflowInterfaceReturnedFiles,
    workflowInterfaceStrictInputsDraft,
    workflowInterfaceTriggerChannelsDraft,
  };
}

export function useWorkflowSettingsHydration(ctx: Record<string, any>) {
  const {
    setWorkflowBuilderAntiUseCasesDraft,
    setWorkflowBuilderCompositionRoleDraft,
    setWorkflowBuilderDeterminismLevelDraft,
    setWorkflowBuilderPurposeDraft,
    setWorkflowBuilderSafeForAutoUseDraft,
    setWorkflowBuilderUseCasesDraft,
    setWorkflowDescriptionDraft,
    setWorkflowDisplayNameDraft,
    setWorkflowEnabledDraft,
    setWorkflowExpectedFailureValue,
    setWorkflowExpectedFailureEdited,
    setWorkflowInterfaceEnabledDraft,
    setWorkflowInterfaceInputModeDraft,
    setWorkflowInterfaceInputSchemaDraft,
    setWorkflowInterfaceInputsDraft,
    setWorkflowInterfaceOutputsDraft,
    setWorkflowInterfaceReturnedFiles,
    setWorkflowInterfaceStrictInputsDraft,
    setWorkflowInterfaceTriggerChannelsDraft,
    workflow,
    workflowContractQuery,
    workflowId,
    workflowFormDirty,
    setWorkflowFormDirty,
    savingWorkflowSurface,
  } = ctx;
  useEffect(() => {
    if (!workflow || (workflowFormDirty && savingWorkflowSurface !== "workflow")) {
      return;
    }
    const workflowRecord = objectOrEmpty(workflow);
    setWorkflowDisplayNameDraft(
      textOrEmpty(workflowRecord.display_name) || workflowId,
    );
    setWorkflowDescriptionDraft(textOrEmpty(workflowRecord.description));
    setWorkflowEnabledDraft(Boolean(workflowRecord.enabled));
    setWorkflowExpectedFailureValue(objectOrEmpty(objectOrEmpty(workflowRecord.settings).traits).expected_failure === true);
    setWorkflowExpectedFailureEdited(false);
    const normalizedInterface = objectOrEmpty(
      workflowContractQuery.data?.interface,
    );
    const storedCallable = objectOrEmpty(objectOrEmpty(workflowRecord.settings).callable);
    const callable = Object.keys(normalizedInterface).length
      ? normalizedInterface
      : storedCallable;
    setWorkflowInterfaceEnabledDraft(Boolean(callable.enabled));
    setWorkflowInterfaceTriggerChannelsDraft(
      stringList(callable.trigger_channels),
    );
    setWorkflowInterfaceInputModeDraft(
      textOrEmpty(callable.input_mode) || "fields",
    );
    setWorkflowInterfaceStrictInputsDraft(callable.strict_inputs !== false);
    setWorkflowInterfaceInputsDraft(
      workflowFieldDraftsFromUnknown(callable.expected_inputs, storedCallable.expected_inputs),
    );
    // A public empty-schema fallback is for callers, not an authored draft.
    const storedSchemaIsEmpty = [storedCallable.input_schema, storedCallable.json_schema, storedCallable.schema].every((value) => {
      try {
        const schema = typeof value === "string" ? JSON.parse(value || "{}") : value;
        return Object.keys(objectOrEmpty(schema)).length === 0;
      } catch {
        return false;
      }
    });
    const keepEmptySchema = callable.input_mode === "json_schema" &&
      textOrEmpty(storedCallable.input_mode).trim().toLowerCase() === "json_schema" && storedSchemaIsEmpty;
    const authoredSchema = Object.hasOwn(storedCallable, "input_schema")
      ? storedCallable.input_schema : callable.input_schema;
    setWorkflowInterfaceInputSchemaDraft(jsonText(keepEmptySchema ? {} : authoredSchema));
    setWorkflowInterfaceOutputsDraft(
      workflowFieldDraftsFromUnknown(callable.expected_outputs, storedCallable.expected_outputs),
    );
    setWorkflowInterfaceReturnedFiles(contractRows(callable.returned_files));
    const builderMetadata = builderMetadataDraftFromUnknown(
      workflowRecord.builder_metadata_jsonb || workflowRecord.builder_metadata,
    );
    setWorkflowBuilderPurposeDraft(builderMetadata.purpose);
    setWorkflowBuilderUseCasesDraft(builderMetadata.useCases);
    setWorkflowBuilderAntiUseCasesDraft(builderMetadata.antiUseCases);
    setWorkflowBuilderCompositionRoleDraft(builderMetadata.compositionRole);
    setWorkflowBuilderDeterminismLevelDraft(builderMetadata.determinismLevel);
    setWorkflowBuilderSafeForAutoUseDraft(builderMetadata.safeForAutoUse);
    setWorkflowFormDirty(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflow, workflowContractQuery.data, workflowId]);
}
