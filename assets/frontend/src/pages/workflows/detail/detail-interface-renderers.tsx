import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { Button } from "@/components/ui/button";
import { ContractBlock } from "./detail-contract-block";
import { ContractFieldControls } from "./detail-contract-field-controls";
/* eslint-disable @typescript-eslint/no-explicit-any, react-refresh/only-export-components */
import type { Dispatch, SetStateAction } from "react";

import {
  localizedWORKFLOW_INTERFACE_FIELD_TYPES,
  contractItemDescription,
  contractItemName,
  contractItemType,
  contractRows,
  createWorkflowInterfaceFieldDraft,
  objectOrEmpty,
  textOrEmpty,
  type WorkflowInterfaceFieldDraft,
} from "@/pages/workflows/detail/detail-helpers";
import { isFileFieldType, isSecretFieldType } from "@/pages/workflows/detail/detail-interface";
import { WorkflowInputSecretBinding } from "@/pages/workflows/detail/detail-workflow-secret-catalog";
import { sizeLabel } from "@/pages/extensions/marketplace-model";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { trimRepeatedEdges } from "@/lib/utils/string-normalization";
import { FileDropzone } from "@/components/workflow-inputs/file-dropzone";

type WorkflowInterfaceRenderContext = Record<string, any>;

const DEFAULT_FIELD_CLASS =
  "fs-input mt-1 w-full";

/**
 * The control for a field's configured default, matched to its type.
 *
 * A default is what the workflow runs with, so it has to be editable in the
 * same shape the field is declared in: a checkbox for a boolean, the declared
 * options for a select, JSON for object and array. A secret has no value to
 * show at all — only whether one is set, and a way to replace or clear it.
 */
type DefaultControlProps = {
  configured: boolean;
  controlId: string;
  controlName: string;
  row: WorkflowInterfaceFieldDraft;
  updateRow: (patch: Partial<WorkflowInterfaceFieldDraft>) => void;
};

/** Where a secret input's catalog binding lives: the saved workflow in its project. */
export type SecretCatalogScope = {
  projectId: string;
  workflowId: string;
  revision?: number | null;
};

/** The row is a secret input of the saved workflow under the name on screen. */
function savedSecretInputName(row: WorkflowInterfaceFieldDraft): string {
  const original = row.originalAuthoredRow ?? {};
  const savedName = textOrEmpty(stringFromUnknownValue(original.name));
  const savedType = textOrEmpty(stringFromUnknownValue(original.type));
  return savedName && savedName === textOrEmpty(row.name) && isSecretFieldType(savedType) ? savedName : "";
}

function DefaultConfiguredControl({ configured, controlId, controlName, updateRow }: Readonly<DefaultControlProps>) {
  const { t } = useI18n();
  return (
    <>
      <label className="mt-1 flex items-center gap-2 text-[11px] text-[var(--fs-text-tertiary)]">
        <input
          aria-describedby={`${controlId}-default-help`}
          checked={configured}
          className="h-3.5 w-3.5 accent-[var(--fs-action-primary)]"
          id={`${controlId}-configured`}
          name={`${controlName}_configured`}
          onChange={(event) => updateRow({ defaultConfigured: event.target.checked })}
          type="checkbox"
        />
        <span>{t("react.workflows.fixed_default_callers_may_not_supply_this_input", "Fixed default (callers may not supply this input)")}</span>
      </label>
      <p className="fs-field-helper mt-1" id={`${controlId}-default-help`}>
        {t("react.workflows.fixed_default_help", "Enable Fixed default to edit and save this value. Callers cannot supply this input while a fixed default is configured, even if the input is optional.")}
      </p>
    </>
  );
}

function SecretDefaultControl(props: Readonly<DefaultControlProps & { catalog?: SecretCatalogScope | null }>) {
  const { t } = useI18n();
  const { catalog, configured, controlId, controlName, row, updateRow } = props;
  const savedName = savedSecretInputName(row);
  const draft = String(row.secretDraft ?? "");
  const typedControl = (
    <>
      <input
        autoComplete="off"
        className={DEFAULT_FIELD_CLASS}
        id={controlId}
        name={controlName}
        onChange={(event) => updateRow({ secretDraft: event.target.value })}
        placeholder={configured ? t("react.workflows.enter_a_new_value_to_replace", "Enter a new value to replace") : t("react.workflows.enter_a_value", "Enter a value")}
        type="password"
        value={draft}
      />
      <div className="mt-1 flex items-center gap-2">
        <button className="rounded border border-[var(--fs-border-default)] px-2 py-1 text-[11px] text-[var(--fs-text-primary)] disabled:opacity-40" disabled={!draft.trim()} onClick={() => updateRow({ secretAction: "set" })} type="button">
          {configured ? t("react.workflows.replace", "Replace") : t("react.workflows.set", "Set")}
        </button>
        <button className="rounded border border-[var(--fs-border-default)] px-2 py-1 text-[11px] text-[var(--fs-text-primary)] disabled:opacity-40" disabled={!configured} onClick={() => updateRow({ secretAction: "clear", secretDraft: "" })} type="button">
          {t("settings.common.clear", "Clear")}</button>
      </div>
      <p className="mt-1 text-[11px] text-[var(--fs-text-tertiary)]">
        {catalog?.projectId
          ? t("react.workflows.typed_secret_kept_in_catalog", "Saving the workflow keeps this value as a secret in Registry → Secrets, so you can choose it elsewhere. It is never shown again.")
          : t("react.workflows.stored_encrypted_and_never_shown_again_saving_the_workflow_applies_the_change", "Stored encrypted and never shown again. Saving the workflow applies the change.")}
      </p>
    </>
  );
  return (
    <div className="mt-2 rounded border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] p-2 text-xs text-[var(--fs-text-primary)]">
      <p className="flex items-center gap-2">
        <span>{t("react.workflows.default_value", "Default value")}</span>
        <span className={configured ? "text-[var(--fs-status-success)]" : "text-[var(--fs-text-tertiary)]"}>
          {configured ? t("react.workflows.configured", "configured") : t("react.workflows.not_configured", "not configured")}
        </span>
      </p>
      {catalog ? (
        <WorkflowInputSecretBinding
          field={savedName || textOrEmpty(row.name)}
          onPendingChange={(catalogSecretId) => updateRow({ catalogSecretId })}
          pendingSecretId={row.catalogSecretId ?? ""}
          projectId={catalog.projectId}
          revision={catalog.revision}
          saved={Boolean(savedName)}
          workflowId={catalog.workflowId}
        >
          {typedControl}
        </WorkflowInputSecretBinding>
      ) : typedControl}
    </div>
  );
}

/**
 * A file input has no default: a run passes its own file. What it can have is
 * a test file, which a test run uses when it passes none — so a workflow that
 * parses files can be tested without sending it an email or an upload.
 */
function SampleFileControl({ controlId, controlName, row, updateRow }: Readonly<DefaultControlProps>) {
  const { t } = useI18n();
  const picked = row.sampleFileDraft ?? null;
  const stored = row.sampleFile ?? null;
  const clearing = row.sampleFileAction === "clear";
  let state = t("react.workflows.display_no_sample_file", "none — test runs must pass a file");
  if (picked) state = t("react.workflows.display_sample_file_uploaded_on_save", "{name} ({size}) — uploaded on save", { name: picked.name, size: sizeLabel(picked.size, t) });
  else if (clearing) state = t("react.workflows.display_sample_file_removed_on_save", "removed on save");
  else if (stored?.filename) state = `${stored.filename}${stored.size_bytes ? ` (${sizeLabel(stored.size_bytes, t)})` : ""}`;
  return (
    <div className="mt-2 rounded border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] p-2 text-xs text-[var(--fs-text-primary)]">
      <p className="flex flex-wrap items-center gap-2">
        <span>{t("react.workflows.test_file", "Test file")}</span>
        <span className={picked || (stored && !clearing) ? "text-[var(--fs-status-success)]" : "text-[var(--fs-text-tertiary)]"}>{state}</span>
      </p>
      <div className="mt-1 grid gap-2">
        <div className="min-w-0">
          <FileDropzone
            ariaLabel={t("react.workflows.test_file", "Test file")}
            files={picked ? [picked] : []}
            inputId={controlId}
            multiple={false}
            name={controlName}
            onFiles={(next) => updateRow({ sampleFileDraft: next[0] ?? null, sampleFileAction: next[0] ? "set" : "" })}
            testId={`sample-file-${controlName}`}
          />
        </div>
        {/* The stored test file has no row in the list above: this clears it. */}
        {stored && !picked && !clearing ? (
          <button className="justify-self-start rounded border border-[var(--fs-border-default)] px-2 py-1 text-[11px] text-[var(--fs-text-primary)]" onClick={() => updateRow({ sampleFileAction: "clear", sampleFileDraft: null })} type="button">
            {t("extensions.repeater.remove", "Remove")}
          </button>
        ) : null}
      </div>
      <p className="mt-1 text-[11px] text-[var(--fs-text-tertiary)]">{t("react.workflows.test_runs_that_pass_no_file_use_this_one_real_runs_always_pass_their_own", "Test runs that pass no file use this one. Real runs always pass their own.")}</p>
    </div>
  );
}

function BooleanDefaultControl(props: Readonly<DefaultControlProps>) {
  const { t } = useI18n();
  const { configured, controlId, controlName, row, updateRow } = props;
  return (
    <div className="mt-2 text-xs text-[var(--fs-text-primary)]">
      <label htmlFor={controlId}>{t("react.workflows.default_value", "Default value")}</label>
      <label className="mt-1 flex items-center gap-2 text-[var(--fs-text-primary)]">
        <input aria-describedby={`${controlId}-default-help`} checked={Boolean(row.default)} className="h-4 w-4 accent-[var(--fs-action-primary)]" disabled={!configured} id={controlId} name={controlName} onChange={(event) => updateRow({ default: event.target.checked })} type="checkbox" />
        <span>{row.default ? "true" : "false"}</span>
      </label>
      <DefaultConfiguredControl {...props} />
    </div>
  );
}

function SelectDefaultControl(props: Readonly<DefaultControlProps>) {
  const { t } = useI18n();
  const { configured, controlId, controlName, row, updateRow } = props;
  const options = String(row.options || "").split(",").map((option) => option.trim()).filter(Boolean);
  return (
    <div className="mt-2 text-xs text-[var(--fs-text-primary)]">
      <label htmlFor={controlId}>{t("react.workflows.default_value", "Default value")}</label>
      <select aria-describedby={`${controlId}-default-help`} className={DEFAULT_FIELD_CLASS} disabled={!configured || !options.length} id={controlId} name={controlName} onChange={(event) => updateRow({ default: event.target.value })} value={stringFromUnknownValue(row.default)}>
        <option value="">{t("react.workflows.select_an_option", "Select an option")}</option>
        {options.map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
      <DefaultConfiguredControl {...props} />
    </div>
  );
}

function StructuredDefaultControl({ type, ...props }: Readonly<DefaultControlProps & { type: "array" | "object" }>) {
  const { t } = useI18n();
  const { configured, controlId, controlName, row, updateRow } = props;
  return (
    <div className="mt-2 text-xs text-[var(--fs-text-primary)]">
      <label htmlFor={controlId}>{t("react.workflows.default_value_json", "Default value (JSON)")}</label>
      <textarea aria-describedby={`${controlId}-default-help`} className={`${DEFAULT_FIELD_CLASS} font-mono`} disabled={!configured} id={controlId} name={controlName} onChange={(event) => updateRow({ default: event.target.value })} placeholder={type === "array" ? "[]" : "{}"} rows={3} value={stringFromUnknownValue(row.default)} />
      <DefaultConfiguredControl {...props} />
    </div>
  );
}

function ScalarDefaultControl({ type, ...props }: Readonly<DefaultControlProps & { type: string }>) {
  const { t } = useI18n();
  const { configured, controlId, controlName, row, updateRow } = props;
  const numeric = type === "number";
  return (
    <div className="mt-2 text-xs text-[var(--fs-text-primary)]">
      <label htmlFor={controlId}>{t("react.workflows.default_value", "Default value")}</label>
      <input aria-describedby={`${controlId}-default-help`} className={DEFAULT_FIELD_CLASS} disabled={!configured} id={controlId} inputMode={numeric ? "decimal" : undefined} name={controlName} onChange={(event) => updateRow({ default: event.target.value })} placeholder={numeric ? "3" : "eu-west-1"} type={numeric ? "number" : "text"} value={stringFromUnknownValue(row.default)} />
      <DefaultConfiguredControl {...props} />
    </div>
  );
}

function renderWorkflowFieldDefault(
  row: WorkflowInterfaceFieldDraft,
  updateRow: (patch: Partial<WorkflowInterfaceFieldDraft>) => void,
  controlId: string,
  controlName: string,
  catalog: SecretCatalogScope | null = null,
) {
  const type = textOrEmpty(row.type) || "string";
  const props = { configured: Boolean(row.defaultConfigured), controlId, controlName, row, updateRow };
  if (isSecretFieldType(type)) return <SecretDefaultControl {...props} catalog={catalog} />;
  if (isFileFieldType(type)) return <SampleFileControl {...props} />;
  if (type === "boolean") return <BooleanDefaultControl {...props} />;
  if (type === "select") return <SelectDefaultControl {...props} />;
  if (type === "object" || type === "array") return <StructuredDefaultControl {...props} type={type} />;
  return <ScalarDefaultControl {...props} type={type} />;
}

export function createWorkflowInterfaceRenderers(ctx: WorkflowInterfaceRenderContext, t: I18nContextValue["t"] = fallbackContext.t) {
  const workflowInterfaceReturnedFiles = contractRows(ctx.workflowInterfaceReturnedFiles);
  const workflowInterfaceControlIdPrefix =
    firstStringFromUnknownValue([ctx.workflowInterfaceControlIdPrefix, ""]);
  const nameValidationAttempted = Boolean(ctx.workflowInterfaceNameValidationAttempted);
  // The catalog binding of a secret input is its own request against the saved
  // workflow; it is never added to the rows this editor saves.
  const secretCatalog: SecretCatalogScope | null = ctx.secretCatalogWorkflowId
    ? {
        projectId: firstStringFromUnknownValue([ctx.secretCatalogProjectId, ""]),
        workflowId: firstStringFromUnknownValue([ctx.secretCatalogWorkflowId, ""]),
        revision: Number(ctx.secretCatalogRevision) || null,
      }
    : null;
  function renderContractChips(
    label: string,
    rows: Array<Record<string, unknown>>,
  ) {
    if (!rows.length) {
      return null;
    }
    return (
      <div>
        <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-primary)]">
          {label}
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          {rows.map((row, index) => {
            const name =
              contractItemName(row) || `${label.toLowerCase()}_${index + 1}`;
            const type = contractItemType(row);
            const required = Boolean(row.required);
            const defaultValue = textOrEmpty(row.default);
            return (
              <span
                className="rounded border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] px-2 py-1 text-[11px] text-[var(--fs-text-primary)]"
                key={`${label}-${name}-${index}`}
                title={contractItemDescription(row)}
              >
                <span className="font-mono text-[var(--fs-text-brand)]">{name}</span>
                <span className="text-[var(--fs-text-tertiary)]"> · {type}</span>
                {required ? (
                  <span className="text-[var(--fs-status-attention-ink)]">{" "}{t("react.workflows.required", "· required")}</span>
                ) : null}
                {defaultValue ? (
                  <span className="text-[var(--fs-text-tertiary)]">
                    {" "}{t("react.workflows.default", "· default")}{" "}{defaultValue}
                  </span>
                ) : null}
              </span>
            );
          })}
        </div>
      </div>
    );
  }

  function renderWorkflowInterfaceFieldEditor(
    title: string,
    rows: WorkflowInterfaceFieldDraft[],
    setRows: Dispatch<SetStateAction<WorkflowInterfaceFieldDraft[]>>,
    direction: "input" | "output"
  ) {
    const editorKey = trimRepeatedEdges(
      title.toLowerCase().replaceAll(/[^a-z0-9]+/g, "_"),
      "_",
    ) || "fields";
    const updateRow = (
      index: number,
      patch: Partial<WorkflowInterfaceFieldDraft>,
    ) => {
      setRows((current) => current.map((row, rowIndex) =>
        rowIndex === index ? { ...row, ...patch } : row,
      ));
    };
    const removeRow = (index: number) => {
      setRows((current) => current.filter((_, rowIndex) => rowIndex !== index));
    };
    const countText = direction === "input"
      ? rows.length === 1 ? t("react.workflows.contract_count_field_one", "1 field") : t("react.workflows.contract_count_fields", "{count} fields", { count: rows.length })
      : rows.length === 1 ? t("react.workflows.contract_count_output_one", "1 output") : t("react.workflows.contract_count_outputs", "{count} outputs", { count: rows.length });
    return (
      <ContractBlock
        addLabel={direction === "input" ? t("react.workflows.contract_add_input", "Add input") : t("react.workflows.contract_add_output", "Add output")}
        countText={countText}
        emptyText={direction === "input"
          ? t("react.workflows.contract_no_inputs", "No expected inputs. The workflow launches without data.")
          : t("react.workflows.contract_no_outputs", "No expected outputs.")}
        onAdd={() => setRows((current) => [...current, createWorkflowInterfaceFieldDraft()])}
        rows={rows.map((row, index) => {
          const rowKey = row.draftId || stringFromUnknownValue(index);
          const nameId = `${workflowInterfaceControlIdPrefix}-${direction}-${rowKey}-name`;
          const nameMissing = !textOrEmpty(row.name) && Boolean(row.nameTouched || nameValidationAttempted);
          const name = textOrEmpty(row.name).trim();
          return {
            key: rowKey,
            name: name || (direction === "input"
              ? t("react.workflows.contract_unnamed_input", "Unnamed input")
              : t("react.workflows.contract_unnamed_output", "Unnamed output")),
            unnamed: !name,
            required: Boolean(row.required),
            type: row.type,
            summary: textOrEmpty(row.label) || textOrEmpty(row.description),
            invalid: nameMissing,
            content: <>
              {direction === "output" && name ? (
                <p className="text-xs text-[var(--fs-text-tertiary)]">
                  {t("react.workflows.output_source_guidance", "Write this value to {path}, or use an Agent whose selected contract declares this output.", { path: `state.outputs.${name}` })}
                </p>
              ) : null}
              <ContractFieldControls
                controlId={`${workflowInterfaceControlIdPrefix}-${editorKey}-${rowKey}`}
                controlName={`workflow_interface_${editorKey}_${index}`}
                direction={direction}
                nameId={nameId}
                nameMissing={nameMissing}
                row={row}
                typeOptions={localizedWORKFLOW_INTERFACE_FIELD_TYPES(t).filter(
                  (option) => direction === "input" || !["secret", "file"].includes(option.value),
                )}
                updateRow={(patch) => updateRow(index, patch)}
              >
                {direction === "input"
                  ? renderWorkflowFieldDefault(
                      row,
                      (patch) => updateRow(index, patch),
                      `${workflowInterfaceControlIdPrefix}-${editorKey}-${rowKey}-default`,
                      `workflow_interface_${editorKey}_${index}_default`,
                      secretCatalog,
                    )
                  : null}
              </ContractFieldControls>
              <div>
                <Button onClick={() => removeRow(index)} size="sm" type="button" variant="ghost-danger">
                  {direction === "input"
                    ? t("react.workflows.contract_remove_input", "Remove input")
                    : t("react.workflows.contract_remove_output", "Remove output")}
                </Button>
              </div>
            </>,
          };
        })}
        testId={`workflow-contract-${direction}s`}
        title={title}
      />
    );
  }

  /**
   * The files the workflow returns. Each one is a step's file result ticked
   * "Return from workflow"; it is listed here, never edited here.
   */
  function renderWorkflowReturnedFiles() {
    const rows = workflowInterfaceReturnedFiles;
    return (
      <ContractBlock
        countText={rows.length === 1
          ? t("react.workflows.contract_count_returned_file_one", "1 file")
          : t("react.workflows.contract_count_returned_files", "{count} files", { count: rows.length })}
        emptyText={t("react.workflows.contract_no_returned_files", "No returned files.")}
        note={t("react.workflows.returned_files_note", "To return a file, tick “Return from workflow” on the step result that saves it.")}
        rows={rows.map((row) => {
          const source = objectOrEmpty(row.source);
          const stepName = textOrEmpty(source.step_name) || textOrEmpty(source.step_id);
          const from = t("react.workflows.returned_file_source", "{step} → {result}", {
            step: stepName,
            result: textOrEmpty(source.result),
          });
          return {
            key: contractItemName(row),
            name: contractItemName(row),
            required: Boolean(row.required),
            type: t("react.workflows.file", "file"),
            summary: from,
            content: <p className="text-xs text-[var(--fs-text-secondary)]">
              {t("react.workflows.returned_file_detail", "From step {step}, result {result} ({target}). Untick “Return from workflow” on the step to stop returning it.", {
                step: stepName,
                result: textOrEmpty(source.result),
                target: textOrEmpty(source.target),
              })}
            </p>,
          };
        })}
        testId="workflow-contract-returned-files"
        title={t("react.workflows.returned_files", "Returned files")}
      />
    );
  }


  return {
    renderContractChips,
    renderWorkflowInterfaceFieldEditor,
    renderWorkflowReturnedFiles,
  };
}
