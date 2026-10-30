import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { BuilderMetadataEditor } from "@/pages/registry/shared/builder-metadata-editor";
import {
  localizedWORKFLOW_INTERFACE_TRIGGER_CHANNELS,
  objectOrEmpty,
  workflowInputModeHelp,
  workflowInputModeOptions,
} from "@/pages/workflows/detail/detail-helpers";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { DsAlert } from "@/components/shared/design-system-primitives";
import { Button } from "@/components/ui/button";
import { DsSwitch } from "@/components/shared/ds-switch";
import { ContractReadOnly, ContractRequiredLegend } from "./detail-contract-block";
import { DrawerFooter, DrawerFooterSpacer } from "./detail-drawer-footer";
import { SaveStateButton } from "./detail-save-button";

type WorkflowSettingsSmartSetupContext = Record<string, any>;

function updatedTriggerChannels(
  current: string[],
  channel: string,
  checked: boolean,
): string[] {
  if (checked) return [...current, channel];
  return current.filter((value) => value !== channel);
}

function shouldExplainHiddenInputModes(advancedMode: boolean, inputMode: string): boolean {
  return !advancedMode && inputMode === "fields";
}

/** Hydration keeps the stored empty schema; a nonempty draft is a declaration. */
function hasWorkflowSchemaDeclaration(ctx: WorkflowSettingsSmartSetupContext): boolean {
  if (ctx.workflowInterfaceInputModeDraft !== "json_schema") return false;
  try {
    const schema = objectOrEmpty(JSON.parse(ctx.workflowInterfaceInputSchemaDraft || "{}"));
    return Object.keys(schema).length > 0;
  } catch {
    return false;
  }
}

type StructuredScalarType = "string" | "number" | "boolean";
type StructuredInputType = StructuredScalarType | "object";
type StructuredMemberRow = {
  id: string;
  originalName?: string;
  original: Record<string, unknown>;
  name: string;
  label: string;
  description: string;
  type: StructuredScalarType;
  required: boolean;
};
type StructuredInputRow = Omit<StructuredMemberRow, "type"> & {
  type: StructuredInputType;
  members: StructuredMemberRow[];
};
type StructuredEditorModel =
  | { kind: "simple"; original: Record<string, unknown>; rows: StructuredInputRow[] }
  | { kind: "retained" };

let structuredRowSequence = 0;
function structuredRowId(): string {
  structuredRowSequence += 1;
  return `structured-input-${structuredRowSequence}`;
}

function plainSchema(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function knownSchemaKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  return Object.keys(value).every((key) => allowed.includes(key));
}

function requiredSchemaNames(value: unknown): string[] | null {
  if (value === undefined) return [];
  return Array.isArray(value) && value.every((name) => typeof name === "string")
    ? value : null;
}

function hasSupportedAnnotation(value: Record<string, unknown>): boolean {
  return ["title", "description"].every((key) => value[key] === undefined || typeof value[key] === "string");
}

function ordinarySchemaMemberName(name: string): boolean {
  return name.length > 0 && name === name.trim() && ![".", "[", "]"].some((part) => name.includes(part));
}

function hasRepresentedRequired(required: string[], properties: Record<string, unknown>): boolean {
  return new Set(required).size === required.length && required.every((name) => Object.hasOwn(properties, name));
}

function inspectStructuredInputs(draft: string): StructuredEditorModel {
  let parsed: unknown;
  try { parsed = JSON.parse(draft || "{}"); } catch { return { kind: "retained" }; }
  if (!plainSchema(parsed)) return { kind: "retained" };
  if (!Object.keys(parsed).length) return { kind: "simple", original: parsed, rows: [] };
  if (parsed.type !== "object" || !knownSchemaKeys(parsed, ["type", "properties", "required", "title", "description", "additionalProperties"]) || !hasSupportedAnnotation(parsed)) return { kind: "retained" };
  if (parsed.additionalProperties !== undefined && typeof parsed.additionalProperties !== "boolean") return { kind: "retained" };
  if (parsed.properties !== undefined && !plainSchema(parsed.properties)) return { kind: "retained" };
  const required = requiredSchemaNames(parsed.required);
  if (!required || !hasRepresentedRequired(required, objectOrEmpty(parsed.properties))) return { kind: "retained" };
  const rows: StructuredInputRow[] = [];
  for (const [name, raw] of Object.entries(objectOrEmpty(parsed.properties))) {
    if (!ordinarySchemaMemberName(name)) return { kind: "retained" };
    if (!plainSchema(raw) || !["string", "number", "boolean", "object"].includes(String(raw.type))) return { kind: "retained" };
    if (!knownSchemaKeys(raw, ["type", "title", "description", "properties", "required", "additionalProperties"]) || !hasSupportedAnnotation(raw)) return { kind: "retained" };
    if (raw.additionalProperties !== undefined && typeof raw.additionalProperties !== "boolean") return { kind: "retained" };
    if (raw.type !== "object" && (raw.properties !== undefined || raw.required !== undefined || raw.additionalProperties !== undefined)) return { kind: "retained" };
    const memberRequired = requiredSchemaNames(raw.required);
    if (!memberRequired || (raw.properties !== undefined && !plainSchema(raw.properties)) ||
      (raw.type === "object" && !hasRepresentedRequired(memberRequired, objectOrEmpty(raw.properties)))) return { kind: "retained" };
    const members: StructuredMemberRow[] = [];
    if (raw.type === "object") {
      for (const [memberName, memberRaw] of Object.entries(objectOrEmpty(raw.properties))) {
        if (!ordinarySchemaMemberName(memberName)) return { kind: "retained" };
        if (!plainSchema(memberRaw) || !["string", "number", "boolean"].includes(String(memberRaw.type)) ||
          (!knownSchemaKeys(memberRaw, ["type", "title", "description"]) || !hasSupportedAnnotation(memberRaw))) return { kind: "retained" };
        members.push({ id: structuredRowId(), originalName: memberName, original: memberRaw,
          name: memberName, label: memberRaw.title as string || "", description: memberRaw.description as string || "",
          type: memberRaw.type as StructuredScalarType, required: memberRequired.includes(memberName) });
      }
    }
    rows.push({ id: structuredRowId(), originalName: name, original: raw, name,
      label: raw.title as string || "", description: raw.description as string || "",
      type: raw.type as StructuredInputType, required: required.includes(name), members });
  }
  return { kind: "simple", original: parsed, rows };
}

function structuredRowIssue(rows: StructuredInputRow[], t: I18nContextValue["t"]): string {
  const inputNames = new Set<string>();
  for (const row of rows) {
    if (row.name === "" || row.name !== row.name.trim() || inputNames.has(row.name)) return t("react.workflows.structured_input_name_issue", "Give each structured input a unique name.");
    inputNames.add(row.name);
    const memberNames = new Set<string>();
    for (const member of row.members) {
      if (member.name === "" || member.name !== member.name.trim() || memberNames.has(member.name)) return t("react.workflows.structured_member_name_issue", "Give each member a unique name.");
      memberNames.add(member.name);
    }
  }
  return "";
}

function patchedRequiredNames(original: unknown, rows: Array<{ originalName?: string; name: string; required: boolean }>): string[] {
  const next: string[] = [];
  for (const oldName of requiredSchemaNames(original) || []) {
    const row = rows.find((item) => item.originalName === oldName);
    if (row?.required && !next.includes(row.name)) next.push(row.name);
  }
  for (const row of rows) if (row.required && !next.includes(row.name)) next.push(row.name);
  return next;
}

function patchStructuredAnnotation(
  field: Record<string, unknown>, original: Record<string, unknown>, key: "title" | "description", value: string,
) {
  if (value === (typeof original[key] === "string" ? original[key] : "")) return;
  if (value) field[key] = value; else delete field[key];
}

function applyStructuredInputRows(original: Record<string, unknown>, rows: StructuredInputRow[]): Record<string, unknown> {
  const properties: Record<string, unknown> = Object.create(null);
  for (const row of rows) {
    const field: Record<string, unknown> = { ...row.original, type: row.type };
    patchStructuredAnnotation(field, row.original, "title", row.label);
    patchStructuredAnnotation(field, row.original, "description", row.description);
    if (row.type === "object") {
      const members: Record<string, unknown> = Object.create(null);
      for (const member of row.members) {
        const fieldMember: Record<string, unknown> = { ...member.original, type: member.type };
        patchStructuredAnnotation(fieldMember, member.original, "title", member.label);
        patchStructuredAnnotation(fieldMember, member.original, "description", member.description);
        members[member.name] = fieldMember;
      }
      if (row.members.length || Object.hasOwn(row.original, "properties") || row.original.type !== "object")
        field.properties = members;
      else delete field.properties;
      const required = patchedRequiredNames(row.original.required, row.members);
      if (required.length || Object.hasOwn(row.original, "required")) field.required = required;
      else delete field.required;
    } else {
      delete field.properties;
      delete field.required;
      delete field.additionalProperties;
    }
    properties[row.name] = field;
  }
  const result: Record<string, unknown> = { ...original, type: "object", properties };
  const required = patchedRequiredNames(original.required, rows);
  if (required.length || Object.hasOwn(original, "required")) result.required = required;
  else delete result.required;
  return result;
}

function StructuredInputEditor({ ctx }: Readonly<{ ctx: WorkflowSettingsSmartSetupContext }>) {
  const { t } = useI18n();
  const draft = String(ctx.workflowInterfaceInputSchemaDraft || "");
  const [local, setLocal] = useState<{ draft: string; model: StructuredEditorModel; issue: string } | null>(null);
  const inspected = useMemo(() => inspectStructuredInputs(draft), [draft]);
  const model = local?.draft === draft ? local.model : inspected;
  const issue = local?.draft === draft ? local.issue : "";
  const setVisualIssue = ctx.setWorkflowInterfaceSchemaVisualIssueDraft as (value: boolean) => void;
  useEffect(() => {
    if (local?.draft !== draft) setVisualIssue(false);
  }, [draft, local?.draft, setVisualIssue]);
  useEffect(() => () => setVisualIssue(false), [setVisualIssue]);

  function commit(rows: StructuredInputRow[]) {
    if (model.kind !== "simple") return;
    const nextModel: StructuredEditorModel = { ...model, rows };
    const nextIssue = structuredRowIssue(rows, t);
    setVisualIssue(Boolean(nextIssue));
    ctx.setWorkflowFormDirty(true);
    if (nextIssue) {
      setLocal({ draft, model: nextModel, issue: nextIssue });
      return;
    }
    const nextDraft = JSON.stringify(applyStructuredInputRows(model.original, rows), null, 2);
    setLocal({ draft: nextDraft, model: nextModel, issue: "" });
    ctx.setWorkflowInterfaceInputSchemaDraft(nextDraft);
  }

  const advanced = <details className="text-xs">
    <summary className="cursor-pointer">{t("react.workflows.advanced_schema", "Advanced schema")}</summary>
    <label className="mt-2 block">
      <span>{t("react.workflows.input_json_schema", "Input JSON schema")}</span>
      <textarea className="fs-input mt-1 min-h-28 w-full font-mono text-xs" name="workflow_interface_input_schema"
        onChange={(event) => ctx.setWorkflowInterfaceInputSchemaDraft(event.target.value)} value={draft} />
    </label>
  </details>;
  if (model.kind === "retained") return <div>
    <p className="fs-field-helper">{t("react.workflows.advanced_schema_retained", "This schema uses advanced rules. Open the saved schema to inspect or edit it.")}</p>
    {advanced}
  </div>;

  return <div className="space-y-3" data-testid="structured-input-editor">
    {model.rows.map((row, index) => <div aria-label={t("react.workflows.structured_input_number", "Structured input {number}", { number: index + 1 })} className="space-y-2 rounded border p-3" key={row.id} role="group">
      <label className="block text-xs">{t("react.registry.name", "Name")}
        <input className="fs-input mt-1 w-full" onChange={(event) => commit(model.rows.map((item) => item.id === row.id ? { ...item, name: event.target.value } : item))} value={row.name} />
      </label>
      <label className="block text-xs">{t("react.registry.label", "Label")}
        <input className="fs-input mt-1 w-full" onChange={(event) => commit(model.rows.map((item) => item.id === row.id ? { ...item, label: event.target.value } : item))} value={row.label} />
      </label>
      <label className="block text-xs">{t("react.registry.type", "Type")}
        <select className="fs-input mt-1 w-full" onChange={(event) => commit(model.rows.map((item) => item.id === row.id ? { ...item, type: event.target.value as StructuredInputType, members: event.target.value === "object" ? item.members : [] } : item))} value={row.type}>
          <option value="string">{t("react.workflows.type_string", "String")}</option><option value="number">{t("react.workflows.type_number", "Number")}</option>
          <option value="boolean">{t("react.workflows.type_boolean", "Boolean")}</option><option value="object">{t("react.workflows.type_object", "Object")}</option>
        </select>
      </label>
      <label className="block text-xs">{t("react.registry.description", "Description")}
        <input className="fs-input mt-1 w-full" onChange={(event) => commit(model.rows.map((item) => item.id === row.id ? { ...item, description: event.target.value } : item))} value={row.description} />
      </label>
      <label className="flex gap-2 text-xs"><input checked={row.required} onChange={(event) => commit(model.rows.map((item) => item.id === row.id ? { ...item, required: event.target.checked } : item))} type="checkbox" />{t("react.registry.required", "Required")}</label>
      {row.type === "object" ? <div className="space-y-2 border-l pl-3">
        {row.members.map((member, memberIndex) => <div aria-label={t("react.workflows.member_number", "Member {number}", { number: memberIndex + 1 })} className="space-y-2 rounded border p-2" key={member.id} role="group">
          <label className="block text-xs">{t("react.registry.name", "Name")}<input className="fs-input mt-1 w-full" onChange={(event) => commit(model.rows.map((item) => item.id === row.id ? { ...item, members: item.members.map((part) => part.id === member.id ? { ...part, name: event.target.value } : part) } : item))} value={member.name} /></label>
          <label className="block text-xs">{t("react.registry.label", "Label")}<input className="fs-input mt-1 w-full" onChange={(event) => commit(model.rows.map((item) => item.id === row.id ? { ...item, members: item.members.map((part) => part.id === member.id ? { ...part, label: event.target.value } : part) } : item))} value={member.label} /></label>
          <label className="block text-xs">{t("react.registry.type", "Type")}<select className="fs-input mt-1 w-full" onChange={(event) => commit(model.rows.map((item) => item.id === row.id ? { ...item, members: item.members.map((part) => part.id === member.id ? { ...part, type: event.target.value as StructuredScalarType } : part) } : item))} value={member.type}><option value="string">{t("react.workflows.type_string", "String")}</option><option value="number">{t("react.workflows.type_number", "Number")}</option><option value="boolean">{t("react.workflows.type_boolean", "Boolean")}</option></select></label>
          <label className="block text-xs">{t("react.registry.description", "Description")}<input className="fs-input mt-1 w-full" onChange={(event) => commit(model.rows.map((item) => item.id === row.id ? { ...item, members: item.members.map((part) => part.id === member.id ? { ...part, description: event.target.value } : part) } : item))} value={member.description} /></label>
          <label className="flex gap-2 text-xs"><input checked={member.required} onChange={(event) => commit(model.rows.map((item) => item.id === row.id ? { ...item, members: item.members.map((part) => part.id === member.id ? { ...part, required: event.target.checked } : part) } : item))} type="checkbox" />{t("react.registry.required", "Required")}</label>
          <Button onClick={() => commit(model.rows.map((item) => item.id === row.id ? { ...item, members: item.members.filter((part) => part.id !== member.id) } : item))} size="sm" type="button" variant="secondary">{t("react.workflows.remove_member", "Remove member")}</Button>
        </div>)}
        <Button onClick={() => commit(model.rows.map((item) => item.id === row.id ? { ...item, members: [...item.members, { id: structuredRowId(), original: {}, name: "", label: "", description: "", type: "string", required: false }] } : item))} size="sm" type="button" variant="secondary">{t("react.workflows.add_member", "Add member")}</Button>
      </div> : null}
      <Button onClick={() => commit(model.rows.filter((item) => item.id !== row.id))} size="sm" type="button" variant="secondary">{t("react.workflows.remove_structured_input", "Remove structured input")}</Button>
    </div>)}
    {issue ? <p className="text-xs text-red-600" role="alert">{issue}</p> : null}
    <Button onClick={() => commit([...model.rows, { id: structuredRowId(), original: {}, name: "", label: "", description: "", type: "string", required: false, members: [] }])} size="sm" type="button" variant="secondary">{t("react.workflows.add_structured_input", "Add structured input")}</Button>
    {advanced}
  </div>;
}

function workflowInputContractEditor(ctx: WorkflowSettingsSmartSetupContext, t: I18nContextValue["t"] = fallbackContext.t) {
  if (ctx.workflowInterfaceInputModeDraft === "json_schema") {
    return <StructuredInputEditor ctx={ctx} />;
  }
  return ctx.renderWorkflowInterfaceFieldEditor(
    t("react.registry.expected_inputs", "Expected inputs"),
    ctx.workflowInterfaceInputsDraft,
    ctx.setWorkflowInterfaceInputsDraft,
    "input",
  );
}

export function WorkflowSettingsSmartSetup({
  ctx,
}: Readonly<{
  ctx: WorkflowSettingsSmartSetupContext;
}>) {
  const { t } = useI18n();
  const canEditSource = useIsAdmin() && !ctx.structureLocked;
  const handledNameFocusSerial = useRef(0);
  const {
    advancedMode,
    drawerTab,
    handleSaveWorkflowSettings,
    renderWorkflowInterfaceFieldEditor,
    renderWorkflowReturnedFiles,
    savingWorkflowSurface,
    setWorkflowBuilderAntiUseCasesDraft,
    setWorkflowBuilderCompositionRoleDraft,
    setWorkflowBuilderDeterminismLevelDraft,
    setWorkflowBuilderPurposeDraft,
    setWorkflowBuilderSafeForAutoUseDraft,
    setWorkflowBuilderUseCasesDraft,
    setWorkflowDescriptionDraft,
    setWorkflowDisplayNameDraft,
    setWorkflowEnabledDraft,
    setWorkflowExpectedFailureDraft,
    setWorkflowInterfaceEnabledDraft,
    setWorkflowInterfaceInputModeDraft,
    setWorkflowInterfaceOutputsDraft,
    setWorkflowInterfaceStrictInputsDraft,
    setWorkflowInterfaceTriggerChannelsDraft,
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
    workflowInterfaceControlIdPrefix,
    workflowInterfaceEnabledDraft,
    workflowInterfaceInputModeDraft,
    workflowInterfaceNameFocusRequest,
    workflowInterfaceOutputsDraft,
    workflowInterfaceStrictInputsDraft,
    workflowInterfaceTriggerChannelsDraft,
  } = ctx;
  const interfaceChannelsMissing = workflowInterfaceEnabledDraft && !workflowInterfaceTriggerChannelsDraft.length;
  const channelIssueId = `${workflowInterfaceControlIdPrefix}-workflow-interface-channels-issue`;
  const interfaceHelpId = `${workflowInterfaceControlIdPrefix}-workflow-interface-help`;

  useEffect(() => {
    const request = workflowInterfaceNameFocusRequest as { workflowId: string; tab: string; id: string; serial: number } | null;
    if (!request || handledNameFocusSerial.current === request.serial) return;
    if (["workflow-interface", "workflow-settings"].includes(ctx.drawerMode) && ctx.workflowId === request.workflowId && drawerTab === request.tab) {
      const surface = document.getElementById("workflow-settings-surface");
      const control = document.getElementById(request.id);
      if (control && surface?.contains(control)) control.focus();
    }
    handledNameFocusSerial.current = request.serial;
  }, [ctx.drawerMode, ctx.workflowId, drawerTab, workflowInterfaceNameFocusRequest]);

  function renderInactiveWarning(): ReactNode {
    const declaredRows = [
      ...(ctx.workflowInterfaceInputsDraft || []),
      ...(workflowInterfaceOutputsDraft || []),
      ...(ctx.workflowInterfaceReturnedFiles || []),
    ];
    const hasDeclarations = declaredRows.some((row) => String(row.name || "").trim()) || hasWorkflowSchemaDeclaration(ctx);
    if (workflowInterfaceEnabledDraft || !hasDeclarations) return null;
    return <DsAlert tone="warning" title={t("react.workflows.interface_inactive_title", "Workflow interface is off")}>
      <p>{t("react.workflows.interface_inactive_help", "Any declared outputs are not returned and required-output checks are inactive until it is enabled. Input forms may still validate declared inputs.")}</p>
      {ctx.drawerMode === "workflow-interface" && canEditSource ? <Button
        onClick={() => ignoreAsyncResult(ctx.openWorkflowSettingsDrawer("smart-setup"))}
        size="sm" type="button" variant="secondary"
      >{t("react.workflows.open_workflow_settings", "Open workflow settings")}</Button> : null}
    </DsAlert>;
  }

  function renderTriggerChannels(): ReactNode {
    return (
      <div>
        <p className="text-xs text-[var(--fs-text-secondary)]">
          {t("react.workflows.trigger_channels", "Trigger channels")}</p>
        <div className="mt-1 grid gap-2 sm:grid-cols-2">
          {localizedWORKFLOW_INTERFACE_TRIGGER_CHANNELS(t).map((channel) => (
            <label
              className="flex items-center gap-2 text-xs text-[var(--fs-text-primary)]"
              key={channel.value}
            >
              <input
                checked={workflowInterfaceTriggerChannelsDraft.includes(
                  channel.value,
                )}
                className="h-4 w-4 accent-[var(--fs-action-primary)]"
                id={`${workflowInterfaceControlIdPrefix}-workflow-interface-trigger-${channel.value}`}
                name={`workflow_interface_trigger_${channel.value}`}
                onChange={(event) => {
                  setWorkflowInterfaceTriggerChannelsDraft(
                    (current: string[]) =>
                      updatedTriggerChannels(
                        current,
                        channel.value,
                        event.target.checked,
                      ),
                  );
                }}
                type="checkbox"
              />
              {channel.label}
            </label>
          ))}
        </div>
        {interfaceChannelsMissing ? <p id={channelIssueId} className="fs-field-helper" role="status">{t("react.workflows.test_run_channels_required", "Choose at least one trigger channel before running a Test Run.")}</p> : null}
      </div>
    );
  }

  function renderModeSelects(): ReactNode {
    return (
      <div>
        <div>
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <span>{t("react.workflows.input_mode", "Input mode")}</span>
            <select
              className="fs-input mt-1 w-full"
              id={`${workflowInterfaceControlIdPrefix}-workflow-interface-input-mode`}
              name="workflow_interface_input_mode"
              onChange={(event) =>
                setWorkflowInterfaceInputModeDraft(event.target.value)
              }
              value={workflowInterfaceInputModeDraft}
            >
              {workflowInputModeOptions(
                workflowInterfaceInputModeDraft,
                advancedMode, t, ctx.workflowInterfaceInputsDraft.length === 0
              ).map((mode) => (
                <option key={mode.value} value={mode.value}>
                  {mode.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <p className="fs-field-helper mt-1">
          {workflowInputModeHelp(workflowInterfaceInputModeDraft, t)}
          {shouldExplainHiddenInputModes(advancedMode, workflowInterfaceInputModeDraft)
            ? ` ${t("react.workflows.advanced_json_modes_are_hidden_here_to_keep_normal_workflow_setup_field_based", "Advanced JSON modes are hidden here to keep normal workflow setup field-based.")}`
            : ""}
        </p>
      </div>
    );
  }

  function renderStrictInputs(): ReactNode {
    if (workflowInterfaceInputModeDraft !== "fields") {
      return (
        <p className="fs-field-helper">
          {t("react.workflows.extra_input_handling_is_controlled_by_this_advanced_input_mode_not_by_the_field_list_below", "Extra input handling is controlled by this advanced input mode, not by the field list below.")}</p>
      );
    }
    return (
      <div className="flex items-start gap-2 text-xs text-[var(--fs-text-primary)]">
        <input
          checked={workflowInterfaceStrictInputsDraft}
          className="mt-0.5 h-4 w-4 accent-[var(--fs-action-primary)]"
          id={`${workflowInterfaceControlIdPrefix}-workflow-interface-strict-inputs`}
          name="workflow_interface_strict_inputs"
          onChange={(event) =>
            setWorkflowInterfaceStrictInputsDraft(
              event.target.checked,
            )
          }
          type="checkbox"
        />
        <span>
          <label
            className="font-medium"
            htmlFor={`${workflowInterfaceControlIdPrefix}-workflow-interface-strict-inputs`}
          >
            {t("react.workflows.only_accept_listed_inputs", "Only accept listed inputs")}</label>
          <span className="fs-field-helper mt-1 block">
            {t("react.workflows.only_accept_listed_inputs_help", "Launches fail if a caller sends a field that is not in Expected inputs. When off, extra values are kept and passed into the workflow.")}</span>
        </span>
      </div>
    );
  }

  /** Save workflow settings in the sticky footer: Smart setup, the interface and the advanced metadata share it. */
  function renderSettingsFooter(): ReactNode {
    const settingsTab = ctx.drawerMode === "workflow-settings" && (drawerTab === "smart-setup" || (advancedMode && drawerTab === "advanced"));
    if (!canEditSource || !(settingsTab || ctx.drawerMode === "workflow-interface")) return null;
    return (
      <DrawerFooter>
        <DrawerFooterSpacer />
        <SaveStateButton
          dirty={Boolean(ctx.workflowFormDirty || ctx.workflowFormSaveFailed)}
          doneAnnouncement={t("react.workflows.workflow_settings_saved", "Workflow settings saved.")}
          doneLabel={t("react.workflows.save_done_saved", "Saved")}
          failed={Boolean(ctx.workflowFormSaveFailed)}
          disabled={Boolean(ctx.workflowInterfaceSchemaVisualIssueDraft)}
          label={t("react.workflows.save_workflow_settings", "Save workflow settings")}
          onClick={() => ignoreAsyncResult(handleSaveWorkflowSettings())}
          saving={savingWorkflowSurface === "workflow"}
          savingLabel={t("react.workflows.save_workflow_settings_saving", "Saving workflow settings…")}
        />
      </DrawerFooter>
    );
  }

  return (
    <>
          {drawerTab === "smart-setup" && ctx.drawerMode === "workflow-settings" ? (
            <fieldset className="workflow-settings-groups" disabled={!canEditSource}>
              <ContractReadOnly value={!canEditSource}>
              <div className="workflow-settings-group">
                <label className="block text-xs text-[var(--fs-text-secondary)]">
                  <span>{t("react.workflows.display_name", "Display name")}</span>
                  <input
                    className="fs-input mt-1 w-full"
                    name="workflow_display_name"
                    onChange={(event) =>
                      setWorkflowDisplayNameDraft(event.target.value)
                    }
                    value={workflowDisplayNameDraft}
                  />
                </label>
                <label className="block text-xs text-[var(--fs-text-secondary)]">
                  <span>{t("registry.workflows.simulation.description", "Description")}</span>
                  <textarea
                    className="fs-input mt-1 min-h-16 w-full"
                    name="workflow_description"
                    onChange={(event) =>
                      setWorkflowDescriptionDraft(event.target.value)
                    }
                    value={workflowDescriptionDraft}
                  />
                </label>
                <label className="block text-xs text-[var(--fs-text-secondary)]">
                  <span>{t("extensions.runtime.status", "Status")}</span>
                  <select
                    className="fs-input mt-1 w-full"
                    name="workflow_status"
                    onChange={(event) =>
                      setWorkflowEnabledDraft(event.target.value === "enabled")
                    }
                    value={workflowEnabledDraft ? "enabled" : "disabled"}
                  >
                    <option value="enabled">{t("settings.extensions.enabled", "enabled")}</option>
                    <option value="disabled">{t("settings.notifications.status.disabled", "disabled")}</option>
                  </select>
                </label>
              </div>
              <div className="workflow-settings-group">
                <label className="flex items-start gap-2 text-sm fs-ink-primary">
                  <input
                    checked={Boolean(workflowExpectedFailureDraft)}
                    className="mt-0.5 h-4 w-4"
                    onChange={(event) => setWorkflowExpectedFailureDraft(event.target.checked)}
                    type="checkbox"
                  />
                  <span>{t("react.workflows.declare_expected_failure", "Declare an expected failure")}</span>
                </label>
                <p className="fs-field-helper mt-1">
                  {t("react.workflows.expected_failure_runs_still_fail", "Runs that stop with failure still fail; this declares that failure is intended for this workflow.")}
                </p>
                {workflowExpectedFailureTag ? <p className="fs-field-helper mt-1">
                  {t("react.workflows.expected_failure_tag_note", "An existing expected-failure tag also declares this intent and stays in place when other settings are saved.")}
                </p> : null}
              </div>
              <div className="workflow-settings-group">
                <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
                  <span className="fs-drawer-label">{t("react.workflows.workflow_interface", "Workflow interface")}</span>
                  <div className="flex shrink-0 items-center gap-2 text-sm text-[var(--fs-text-primary)]">
                    <span>{t("react.workflows.enable_workflow_interface", "Enable workflow interface")}</span>
                    <DsSwitch
                      checked={Boolean(workflowInterfaceEnabledDraft)}
                      describedBy={[interfaceHelpId, interfaceChannelsMissing ? channelIssueId : undefined].filter(Boolean).join(" ")}
                      label={t("react.workflows.enable_workflow_interface", "Enable workflow interface")}
                      onCheckedChange={setWorkflowInterfaceEnabledDraft}
                    />
                  </div>
                </div>
                <p id={interfaceHelpId} className="fs-field-helper mt-1">
                  {t("react.workflows.workflow_interface_enabled_help", "Publishes the declared workflow interface on the selected trigger channels. The Status above separately controls whether the workflow can run.")}
                </p>
                {renderInactiveWarning()}
                {renderTriggerChannels()}
                {renderModeSelects()}
                {renderStrictInputs()}
              </div>
              <div className="workflow-settings-group" data-testid="workflow-settings-contract">
                <div>
                  <span className="fs-drawer-label">{t("react.workflows.contract", "Contract")}</span>
                  <p className="fs-field-helper mt-1">{t("react.workflows.contract_help", "What callers send and what the workflow promises back. Expected inputs also build the Test Run fields, schedule inputs and the webhook mapping.")}</p>
                </div>
                {workflowInputContractEditor(ctx, t)}
                {renderWorkflowInterfaceFieldEditor(t("react.registry.expected_outputs", "Expected outputs"), workflowInterfaceOutputsDraft, setWorkflowInterfaceOutputsDraft, "output")}
                {renderWorkflowReturnedFiles()}
                <ContractRequiredLegend />
              </div>
              </ContractReadOnly>
            </fieldset>
          ) : null}
          {ctx.drawerMode === "workflow-interface" ? (
            <>
            {renderInactiveWarning()}
            <fieldset disabled={!canEditSource} className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] p-3">
              <ContractReadOnly value={!canEditSource}>
              <p className="text-xs text-[var(--fs-text-tertiary)]">{t("react.workflows.shared_workflow_interface", "This contract belongs to the whole workflow. Every successful branch uses the same output definition.")}</p>
              {drawerTab === "inputs" ? <div className="space-y-3" role="tabpanel" aria-labelledby="workflow-interface-tab-inputs" id="workflow-contract-section-inputs">
                  <div>
                    <label className="block text-xs uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
                      <span>{t("react.workflows.input_mode", "Input mode")}</span>
                      <select
                        className="fs-input mt-1 w-full"
                        id={`${workflowInterfaceControlIdPrefix}-workflow-interface-input-mode`}
                        name="workflow_interface_input_mode"
                        onChange={(event) =>
                          setWorkflowInterfaceInputModeDraft(event.target.value)
                        }
                        value={workflowInterfaceInputModeDraft}
                      >
                        {workflowInputModeOptions(
                          workflowInterfaceInputModeDraft,
                          advancedMode, t, ctx.workflowInterfaceInputsDraft.length === 0
                        ).map((mode) => (
                          <option key={mode.value} value={mode.value}>
                            {mode.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <p className="mt-1 text-[11px] leading-4 text-[var(--fs-text-tertiary)]">
                      {workflowInputModeHelp(workflowInterfaceInputModeDraft, t)}
                    </p>
                    {shouldExplainHiddenInputModes(
                      advancedMode,
                      workflowInterfaceInputModeDraft,
                    ) ? (
                      <p className="mt-1 text-[11px] leading-4 text-[var(--fs-text-tertiary)]">
                        {t("react.workflows.advanced_json_modes_are_hidden_here_to_keep_normal_workflow_setup_field_based", "Advanced JSON modes are hidden here to keep normal workflow setup field-based.")}</p>
                    ) : null}
                  </div>
                {workflowInterfaceInputModeDraft === "fields" ? (
                  <div className="flex items-start gap-2 rounded-lg border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] px-3 py-2 text-xs text-[var(--fs-text-primary)]">
                    <input
                      checked={workflowInterfaceStrictInputsDraft}
                      className="mt-0.5 h-4 w-4 accent-cyan-400"
                      id="workflow-interface-strict-inputs"
                      name="workflow_interface_strict_inputs"
                      onChange={(event) =>
                        setWorkflowInterfaceStrictInputsDraft(
                          event.target.checked,
                        )
                      }
                      type="checkbox"
                    />
                    <span>
                      <label
                        className="font-medium"
                        htmlFor="workflow-interface-strict-inputs"
                      >
                        {t("react.workflows.only_accept_listed_inputs", "Only accept listed inputs")}</label>
                      <span className="mt-1 block text-[11px] leading-4 text-[var(--fs-text-tertiary)]">
                        {t("react.workflows.when_enabled_launches_fail_if_api_mcp_webhook_schedule_or_ui_data_includes_a_field_that_is_not_in_expected_inp", "When enabled, launches fail if API, MCP, webhook, schedule, or UI data includes a field that is not in Expected inputs. When disabled, extra values are kept and passed into the workflow.")}</span>
                    </span>
                  </div>
                ) : (
                  <p className="rounded-lg border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] px-3 py-2 text-xs leading-4 text-[var(--fs-text-tertiary)]">
                    {t("react.workflows.extra_input_handling_is_controlled_by_this_advanced_input_mode_not_by_the_field_list_below", "Extra input handling is controlled by this advanced input mode, not by the field list below.")}</p>
                )}
                {workflowInputContractEditor(ctx, t)}
              </div> : null}
              {drawerTab === "outputs" ? <div className="space-y-3" role="tabpanel" aria-labelledby="workflow-interface-tab-outputs" id="workflow-contract-section-outputs">
                {renderWorkflowInterfaceFieldEditor(t("react.registry.expected_outputs", "Expected outputs"), workflowInterfaceOutputsDraft, setWorkflowInterfaceOutputsDraft, "output")}
                {renderWorkflowReturnedFiles()}
              </div> : null}
              </ContractReadOnly>
            </fieldset>
            </>
          ) : null}
          {advancedMode && drawerTab === "advanced" ? (
            <fieldset disabled={!canEditSource} className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] p-3">
              <div>
                <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-primary)]">
                  {t("react.workflows.builder_metadata", "Builder metadata")}</p>
                <p className="mt-1 text-xs text-[var(--fs-text-tertiary)]">
                  {t("react.workflows.workflow_discovery_uses_this_metadata_to_identify_when_this_workflow_can_be_reused", "Workflow discovery uses this metadata to identify when this workflow can be reused.")}</p>
              </div>
              <BuilderMetadataEditor
                draft={{
                  antiUseCases: workflowBuilderAntiUseCasesDraft,
                  compositionRole: workflowBuilderCompositionRoleDraft,
                  determinismLevel: workflowBuilderDeterminismLevelDraft,
                  purpose: workflowBuilderPurposeDraft,
                  safeForAutoUse: workflowBuilderSafeForAutoUseDraft,
                  useCases: workflowBuilderUseCasesDraft,
                }}
                onChange={(next) => {
                  setWorkflowBuilderPurposeDraft(next.purpose);
                  setWorkflowBuilderUseCasesDraft(next.useCases);
                  setWorkflowBuilderAntiUseCasesDraft(next.antiUseCases);
                  setWorkflowBuilderCompositionRoleDraft(next.compositionRole);
                  setWorkflowBuilderDeterminismLevelDraft(next.determinismLevel);
                  setWorkflowBuilderSafeForAutoUseDraft(next.safeForAutoUse);
                }}
              />
            </fieldset>
          ) : null}
          {renderSettingsFooter()}
    </>
  );
}
