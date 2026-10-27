import { DsFormSelect, OptionalLabel, RequiredLabel } from "@/components/shared/design-system";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useStableNestedRowKeys, useStableRowKeys } from "@/lib/utils/stable-row-keys";
import { jsonText, listFromUnknown, objectOrEmpty, textOrEmpty } from "@/pages/workflows/detail/detail-helpers";
import { TabularJsonField } from "@/pages/workflows/detail/detail-tabular-json-field";
import { inspectWorkflowTabularFeed } from "@/pages/workflows/shared/api";
import { useState, type ReactNode } from "react";

type WorkflowStepSmartSetupContext = Record<string, any>;

const TABULAR_FEED_OPERATIONS = [
  "inspect",
  "read",
  "transform",
  "concat",
  "checksum",
  "write",
  "validate",
  "diff",
  "commit_snapshot",
  "read_rows",
  "merge_rows",
  "create_dataset",
];

const TABULAR_FEED_FORMATS = ["csv", "tsv", "json", "ndjson", "parquet", "xlsx"];

const TABULAR_FEED_RELATIONAL_OPS = [
  "filter",
  "dedupe_by",
  "sort",
  "group_by",
  "explode",
  "unnest",
];

function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

function stringListFromUnknown(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => textOrEmpty(item)).filter(Boolean)
    : [];
}

function listText(value: unknown): string {
  return stringListFromUnknown(value).join("\n");
}

function listFromText(value: string): string[] {
  return value
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function defaultRelationalOperation(op: string): Record<string, unknown> {
  if (op === "dedupe_by") return { op, keys: [], keep: "first" };
  if (op === "sort") return { op, by: [], descending: false, nulls_last: true };
  if (op === "group_by") return { op, keys: [], aggregations: [] };
  if (op === "explode" || op === "unnest") return { op, column: "" };
  return { op: "filter", when: { column: "", equals: "" } };
}

type DraftError = { path: string; message: string };

function tabularDisplayLabel(label: string, t: I18nContextValue["t"]): string {
  switch (label) {
    case "Artifact handle": return t("react.workflows.display_artifact_handle", "Artifact handle");
    case "Dataset handle": return t("react.workflows.display_dataset_handle", "Dataset handle");
    case "Baseline dataset handle": return t("react.workflows.display_baseline_dataset_handle", "Baseline dataset handle");
    case "Profile key": return t("react.workflows.profile_key", "Profile key");
    case "Key column": return t("react.workflows.key_column", "Key column");
    case "Snapshot artifact": return t("react.workflows.display_snapshot_artifact", "Snapshot artifact");
    case "Result target": return t("react.workflows.result_target_variant_6c946b46", "Result target");
    case "SHA-256 checksum": return t("react.workflows.display_sha_checksum", "SHA-256 checksum");
    default: return label;
  }
}

const TABULAR_INPUT_REQUIREMENTS: Record<string, [string, string]> = {
  inspect: ["artifact_handle", "Artifact handle"],
  read: ["artifact_handle", "Artifact handle"],
  transform: ["dataset_handle", "Dataset handle"],
  write: ["dataset_handle", "Dataset handle"],
  validate: ["dataset_handle", "Dataset handle"],
  diff: ["dataset_handle", "Dataset handle"],
  read_rows: ["dataset_handle", "Dataset handle"],
  merge_rows: ["dataset_handle", "Dataset handle"],
};

const TABULAR_OPERATION_INPUT_FIELDS: Record<string, readonly string[]> = {
  inspect: ["artifact_handle"],
  read: ["artifact_handle"],
  transform: ["dataset_handle"],
  concat: ["dataset_handles"],
  checksum: ["profile_key", "raw_sha256"],
  write: ["dataset_handle"],
  validate: ["dataset_handle"],
  diff: ["dataset_handle", "baseline_dataset_handle", "expected_snapshot_ref"],
  commit_snapshot: ["profile_key", "snapshot_artifact_id"],
  read_rows: ["dataset_handle"],
  merge_rows: ["dataset_handle"],
  create_dataset: [],
};

function requiredTextError(
  value: unknown,
  path: string,
  label: string, t: I18nContextValue["t"] = fallbackContext.t
): DraftError[] {
  return textOrEmpty(value)
    ? []
    : [{ path, message: t("react.workflows.value0_is_required", "{value0} is required.", { value0: tabularDisplayLabel(label, t) }) }];
}

function transformDraftErrors(config: Record<string, any>, t: I18nContextValue["t"] = fallbackContext.t): DraftError[] {
  const actions = listFromUnknown(config.actions);
  const relational = listFromUnknown(config.relational);
  const errors: DraftError[] = [];
  if (!actions.length && !relational.length) {
    errors.push({
      path: "actions",
      message: t("react.workflows.add_at_least_one_action_or_relational_operation", "Add at least one action or relational operation."),
    });
  }
  actions.forEach((action, index) => {
    if (textOrEmpty(action.action) && !textOrEmpty(action.target)) {
      errors.push({
        path: `actions[${index}].target`,
        message: t("react.workflows.target_column_is_required", "Target column is required."),
      });
    }
    const when = objectOrEmpty(action.when);
    const incompleteCondition =
      Object.keys(when).length > 0 &&
      (!textOrEmpty(when.column) || Object.keys(when).length < 2);
    if (incompleteCondition) {
      errors.push({
        path: `actions[${index}].when`,
        message: t("react.workflows.choose_a_column_operator_and_value", "Choose a column, operator, and value."),
      });
    }
  });
  return errors;
}

function diffDraftErrors(config: Record<string, any>, t: I18nContextValue["t"] = fallbackContext.t): DraftError[] {
  return [
    ...requiredTextError(config.input?.baseline_dataset_handle, "input.baseline_dataset_handle", "Baseline dataset handle", t),
    ...requiredTextError(config.profile_key, "profile_key", "Profile key", t),
    ...requiredTextError(config.key_column, "key_column", "Key column", t),
  ];
}

function checksumDraftErrors(
  config: Record<string, any>,
  input: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t
): DraftError[] {
  return [
    ...requiredTextError(
      input.profile_key || config.profile_key,
      "profile_key",
      "Profile key", t
    ),
    ...requiredTextError(
      input.raw_sha256 || config.raw_sha256,
      "raw_sha256",
      "SHA-256 checksum", t
    ),
  ];
}

function commitSnapshotDraftErrors(
  config: Record<string, any>,
  input: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t
): DraftError[] {
  return [
    ...requiredTextError(
      input.profile_key || config.profile_key,
      "profile_key",
      "Profile key", t
    ),
    ...requiredTextError(
      input.snapshot_artifact_id || config.snapshot_artifact_id,
      "snapshot_artifact_id",
      "Snapshot artifact", t
    ),
  ];
}

function operationDraftErrors(
  operation: string,
  config: Record<string, any>,
  input: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t
): DraftError[] {
  if (operation === "transform") return transformDraftErrors(config, t);
  if (operation === "concat") {
    return stringListFromUnknown(input.dataset_handles).length >= 2
      ? []
      : [
          {
            path: "input.dataset_handles",
            message: t("react.workflows.at_least_two_dataset_handles_are_required", "At least two dataset handles are required."),
          },
        ];
  }
  if (operation === "write") {
    return TABULAR_FEED_FORMATS.includes(textOrEmpty(config.format))
      ? []
      : [{ path: "format", message: t("react.workflows.choose_an_output_format", "Choose an output format.") }];
  }
  if (operation === "validate") {
    return Object.keys(objectOrEmpty(config.checks)).length
      ? []
      : [{ path: "checks", message: t("react.workflows.add_validation_checks", "Add validation checks.") }];
  }
  if (operation === "diff") return diffDraftErrors(config, t);
  if (operation === "checksum") return checksumDraftErrors(config, input, t);
  if (operation === "commit_snapshot") {
    return commitSnapshotDraftErrors(config, input, t);
  }
  return [];
}

function tabularFeedDraftErrors(config: Record<string, any>, t: I18nContextValue["t"] = fallbackContext.t): DraftError[] {
  const operation = textOrEmpty(config.operation) || "read";
  const input = objectOrEmpty(config.input);
  const requirement = TABULAR_INPUT_REQUIREMENTS[operation];
  const requiredInputErrors = requirement
    ? requiredTextError(input[requirement[0]], `input.${requirement[0]}`, requirement[1], t)
    : [];
  return [
    ...requiredTextError(config.result_target, "result_target", "Result target", t),
    ...requiredInputErrors,
    ...operationDraftErrors(operation, config, input, t),
  ];
}

export function TabularFeedBuilder({ ctx, inputClass }: Readonly<{ ctx: WorkflowStepSmartSetupContext; inputClass: string }>) {
  const { t } = useI18n();
  const {
    renderWorkflowVariableInput,
    replaceStepKindConfigField,
    stepKindConfig,
    selectedProjectId,
    transformActionsQuery,
    updateStepKindConfig,
    workflowId,
  } = ctx;
  const operation = textOrEmpty(stepKindConfig.operation) || "read";
  const input = objectOrEmpty(stepKindConfig.input);
  const hiddenSnapshotRef = "expected_snapshot_ref" in input
    ? input.expected_snapshot_ref : stepKindConfig.expected_snapshot_ref;
  const snapshotMode = textOrEmpty(stepKindConfig.snapshot_mode) ||
    (hiddenSnapshotRef != null ? "publish" : "compare");
  const options = objectOrEmpty(stepKindConfig.options);
  const actions = listFromUnknown(stepKindConfig.actions);
  const relational = listFromUnknown(stepKindConfig.relational);
  const catalogActions = (transformActionsQuery?.data?.actions || []).filter(
    (action: Record<string, any>) => action.backends?.polars === true,
  );
  const schemaColumns = listFromUnknown(objectOrEmpty(stepKindConfig.schema).columns);
  const previewRows = listFromUnknown(stepKindConfig.preview_rows).slice(0, 10);
  const errors = tabularFeedDraftErrors(stepKindConfig, t);
  const [inspectPending, setInspectPending] = useState(false);
  const [inspectError, setInspectError] = useState("");
  const actionKeyState = useStableRowKeys(actions.length, "tabular-feed-action");
  const relationalKeyState = useStableRowKeys(
    relational.length,
    "tabular-feed-relational",
  );
  const aggregationKeyState = useStableNestedRowKeys(
    relational.map((row) => listFromUnknown(row.aggregations).length),
    "tabular-feed-aggregation",
  );
  const actionsWithKeys = actions.map((action, index) => ({
    action,
    index,
    key: actionKeyState.keys[index],
  }));
  const relationalWithKeys = relational.map((row, index) => ({
    index,
    key: relationalKeyState.keys[index],
    row,
  }));
  const patchConfig = (patch: Record<string, unknown>) => updateStepKindConfig("tabular_feed", patch);
  const changeOperation = (nextOperation: string) => {
    const allowedFields = TABULAR_OPERATION_INPUT_FIELDS[nextOperation] || [];
    patchConfig({
      operation: nextOperation,
      input: Object.fromEntries(
        Object.entries(input).filter(([field]) => allowedFields.includes(field)),
      ),
    });
  };
  const patchInput = (patch: Record<string, unknown>) =>
    patchConfig({ input: { ...input, ...patch } });
  const patchOptions = (patch: Record<string, unknown>) =>
    patchConfig({ options: { ...options, ...patch } });
  const updateAction = (index: number, patch: Record<string, unknown>) => {
    const next = actions.map((row, rowIndex) => {
      if (rowIndex !== index) return row;
      const nextAction = "action" in patch ? textOrEmpty(patch.action) : textOrEmpty(row.action);
      if ("action" in patch && nextAction !== textOrEmpty(row.action)) {
        return { action: nextAction, target: textOrEmpty(row.target), params: {} };
      }
      return { ...row, ...patch };
    });
    patchConfig({ actions: next });
  };
  const updateRelational = (index: number, patch: Record<string, unknown>) => {
    const next = relational.map((row, rowIndex) => {
      if (rowIndex !== index) return row;
      const nextOp = "op" in patch ? textOrEmpty(patch.op) : textOrEmpty(row.op);
      if ("op" in patch && nextOp !== textOrEmpty(row.op)) {
        return defaultRelationalOperation(nextOp);
      }
      return { ...row, ...patch };
    });
    patchConfig({ relational: next });
  };
  const handleInput = (field: string, label: string, placeholder: string) => (
    <label className="block text-xs text-[var(--fs-text-secondary)]">
      <RequiredLabel>{tabularDisplayLabel(label, t)}</RequiredLabel>
      {renderWorkflowVariableInput({
        ariaLabel: tabularDisplayLabel(label, t),
        ariaRequired: true,
        className: inputClass,
        name: `tabular-feed-input-${field}`,
        onChange: (nextValue: string) => patchInput({ [field]: nextValue }),
        panelId: `tabular-feed-${field}-suggestions`,
        placeholder,
        value: textOrEmpty(input[field]),
      })}
    </label>
  );
  const inspectArtifact = async () => {
    const artifactHandle = textOrEmpty(input.artifact_handle);
    if (!artifactHandle) return;
    setInspectPending(true);
    setInspectError("");
    try {
      const response = await inspectWorkflowTabularFeed(
        textOrEmpty(selectedProjectId),
        textOrEmpty(workflowId),
        artifactHandle,
        options,
      );
      const envelope = objectOrEmpty(response);
      const result = objectOrEmpty(envelope.result);
      if (envelope.ok === false) {
        const errors = listFromUnknown(envelope.errors);
        throw new Error(textOrEmpty(errors[0]?.message) || t("react.workflows.inspect_failed", "Inspect failed."));
      }
      patchConfig({
        schema: { columns: Array.isArray(result.columns) ? result.columns : [] },
        preview_rows: Array.isArray(result.preview_rows) ? result.preview_rows : [],
      });
    } catch (error) {
      setInspectError(error instanceof Error ? error.message : t("react.workflows.inspect_failed", "Inspect failed."));
    } finally {
      setInspectPending(false);
    }
  };

  return (
    <div className="space-y-4 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
      <div className="grid gap-2 sm:grid-cols-3">
        <label className="block text-xs text-[var(--fs-text-secondary)]">
          <span>{t("react.workflows.operation", "Operation")}</span>
          <DsFormSelect
            ariaLabel={t("react.workflows.operation", "Operation")}
            className="mt-1"
            name="tabular-feed-operation"
            onChange={(nextValue) => changeOperation(nextValue)}
            value={operation}
            options={[...TABULAR_FEED_OPERATIONS.map((item) => (
              ({ value: item, label: item })
            ))]}
          />
        </label>
        <label className="block text-xs text-[var(--fs-text-secondary)] sm:col-span-2">
          <RequiredLabel>{t("react.workflows.result_target_variant_6c946b46", "Result target")}</RequiredLabel>
          {renderWorkflowVariableInput({
            ariaLabel: t("react.workflows.result_target_variant_6c946b46", "Result target"),
            ariaRequired: true,
            className: inputClass,
            name: "tabular-feed-result-target",
            onChange: (nextValue: string) =>
              replaceStepKindConfigField("tabular_feed", "result_target", nextValue),
            panelId: "tabular-feed-result-target-suggestions",
            placeholder: "state.tabular_feed_result",
            value: textOrEmpty(stepKindConfig.result_target),
          })}
        </label>
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        {renderWhen(
          ["inspect", "read"].includes(operation),
          handleInput("artifact_handle", "Artifact handle", "state.feed_artifact"),
        )}
        {renderWhen(
          ["transform", "write", "validate", "diff", "read_rows", "merge_rows"].includes(operation),
          handleInput("dataset_handle", "Dataset handle", "state.feed_dataset.dataset_handle"),
        )}
        {renderWhen(
          operation === "diff",
          handleInput("baseline_dataset_handle", "Baseline dataset handle", "state.baseline_dataset.dataset_handle"),
        )}
        {renderWhen(operation === "diff", (
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <RequiredLabel>{t("react.workflows.snapshot_mode", "Comparison purpose")}</RequiredLabel>
            <DsFormSelect
              ariaLabel={t("react.workflows.snapshot_mode", "Comparison purpose")}
              className="mt-1"
              name="tabular-feed-snapshot-mode"
              onChange={(nextValue) => {
                if (nextValue === "compare") {
                  const rest = { ...input };
                  delete rest.expected_snapshot_ref;
                  patchConfig({
                    snapshot_mode: nextValue,
                    input: rest,
                    ...("expected_snapshot_ref" in stepKindConfig
                      ? { expected_snapshot_ref: undefined } : {}),
                  });
                } else patchConfig({ snapshot_mode: nextValue });
              }}
              required
              value={snapshotMode}
              options={[
                { value: "compare", label: t("react.workflows.compare_datasets", "Compare datasets") },
                { value: "publish", label: t("react.workflows.prepare_feed_for_publication", "Prepare feed for publication") },
              ]}
            />
          </label>
        ))}
        {renderWhen(operation === "concat", (
          <label className="block text-xs text-[var(--fs-text-secondary)] sm:col-span-2">
            <span>{t("react.workflows.dataset_handles", "Dataset handles")}</span>
            <textarea
              className={`${inputClass} min-h-20`}
              name="tabular-feed-input-dataset-handles"
              onChange={(event) => patchInput({ dataset_handles: listFromText(event.target.value) })}
              placeholder="state.first_dataset&#10;state.second_dataset"
              value={listText(input.dataset_handles)}
            />
          </label>
        ))}
        {renderWhen(
          operation === "commit_snapshot",
          handleInput("snapshot_artifact_id", "Snapshot artifact", "state.diff.snapshot_artifact_id"),
        )}
        {renderWhen(["diff", "checksum", "commit_snapshot"].includes(operation), (
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <RequiredLabel>{t("react.workflows.profile_key", "Profile key")}</RequiredLabel>
            <input
              className={inputClass}
              name="tabular-feed-profile-key"
              required
              onChange={(event) => patchConfig({ profile_key: event.target.value })}
              placeholder="supplier-price-feed"
              value={textOrEmpty(stepKindConfig.profile_key)}
            />
          </label>
        ))}
        {renderWhen(["checksum", "diff"].includes(operation), (
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            {operation === "checksum"
              ? <RequiredLabel>{t("react.workflows.raw_sha_256", "Raw SHA-256")}</RequiredLabel>
              : <span>{t("react.workflows.raw_sha_256", "Raw SHA-256")}</span>}
            {renderWorkflowVariableInput({
              ariaLabel: t("react.workflows.raw_sha_256", "Raw SHA-256"),
              ariaRequired: operation === "checksum",
              className: inputClass,
              name: "tabular-feed-raw-sha256",
              onChange: (nextValue: string) => patchConfig({ raw_sha256: nextValue }),
              panelId: "tabular-feed-raw-sha256-suggestions",
              placeholder: "state.source.sha256",
              value: textOrEmpty(stepKindConfig.raw_sha256),
            })}
          </label>
        ))}
        {renderWhen(["read", "diff", "merge_rows"].includes(operation), (
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            {operation !== "read" ? <RequiredLabel>{t("react.workflows.key_column", "Key column")}</RequiredLabel> : <span>{t("react.workflows.key_column", "Key column")}</span>}
            <input
              className={inputClass}
              name="tabular-feed-key-column"
              required={operation !== "read"}
              onChange={(event) => patchConfig({ key_column: event.target.value })}
              placeholder="sku"
              value={textOrEmpty(stepKindConfig.key_column)}
            />
          </label>
        ))}
      </div>

      {renderWhen(["inspect", "read"].includes(operation), (
        <div className="grid gap-2 sm:grid-cols-3">
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <span>{t("react.workflows.input_format", "Input format")}</span>
            <DsFormSelect
              ariaLabel={t("react.workflows.input_format", "Input format")}
              className="mt-1"
              name="tabular-feed-input-format"
              onChange={(nextValue) => patchOptions({ format: nextValue })}
              value={textOrEmpty(options.format) || "auto"}
              options={[...["auto", ...TABULAR_FEED_FORMATS].map((item) => (
                ({ value: item, label: item === "auto" ? t("shell.theme.mode.auto", "auto") : item })
              ))]}
            />
          </label>
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <span>{t("react.workflows.sheet", "Sheet")}</span>
            <input
              className={inputClass}
              name="tabular-feed-input-sheet"
              onChange={(event) => patchOptions({ sheet: event.target.value })}
              placeholder="Sheet1"
              value={textOrEmpty(options.sheet)}
            />
          </label>
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <span>{t("react.workflows.delimiter", "Delimiter")}</span>
            <input
              className={inputClass}
              name="tabular-feed-input-delimiter"
              onChange={(event) => patchOptions({ delimiter: event.target.value })}
              placeholder=","
              value={textOrEmpty(options.delimiter)}
            />
          </label>
        </div>
      ))}

      {renderWhen(["inspect", "read"].includes(operation), (
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="fs-button sm secondary"
            disabled={inspectPending || !textOrEmpty(input.artifact_handle)}
            onClick={() => void inspectArtifact()}
            type="button"
          >
            {inspectPending ? t("react.workflows.inspecting", "Inspecting…") : t("react.workflows.inspect_artifact", "Inspect artifact")}
          </button>
          {inspectError ? <span className="text-xs text-[var(--fs-status-danger)]">{inspectError}</span> : null}
        </div>
      ))}

      {renderWhen(operation === "write", (
        <div className="grid gap-2 sm:grid-cols-3">
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <span>{t("react.workflows.output_format", "Output format")}</span>
            <DsFormSelect
              ariaLabel={t("react.workflows.output_format", "Output format")}
              className="mt-1"
              name="tabular-feed-output-format"
              onChange={(nextValue) => patchConfig({ format: nextValue })}
              value={textOrEmpty(stepKindConfig.format)}
              options={[({ value: "", label: t("react.workflows.choose_output_format", "Choose output format") }), ...(textOrEmpty(stepKindConfig.format) && !TABULAR_FEED_FORMATS.includes(textOrEmpty(stepKindConfig.format)) ? [({ value: textOrEmpty(stepKindConfig.format), label: [t("react.workflows.invalid_format", "Invalid format:"), " ", textOrEmpty(stepKindConfig.format)].join("") })] : []), ...TABULAR_FEED_FORMATS.map((item) => (
                ({ value: item, label: item })
              ))]}
            />
          </label>
          <label className="block text-xs text-[var(--fs-text-secondary)] sm:col-span-2">
            <span>{t("react.workflows.filename_variant_971b9742", "Filename")}</span>
            <input
              className={inputClass}
              name="tabular-feed-output-filename"
              onChange={(event) => patchOptions({ filename: event.target.value })}
              placeholder="feed-export.parquet"
              value={textOrEmpty(options.filename)}
            />
          </label>
        </div>
      ))}

      {renderWhen(operation === "transform", (
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">{t("extensions.table.actions", "Actions")}</p>
            <button
              className="fs-button sm secondary"
              onClick={() =>
                {
                  actionKeyState.append();
                  patchConfig({
                    actions: [
                      ...actions,
                      { action: catalogActions[0]?.action_id || "", target: "", params: {} },
                    ],
                  });
                }
              }
              type="button"
            >
              {t("react.workflows.add_action", "Add action")}</button>
          </div>
          {actionsWithKeys.map(({ action, index, key }) => {
            const definition = catalogActions.find(
              (item: Record<string, any>) => item.action_id === action.action,
            );
            const params = objectOrEmpty(action.params);
            const properties = objectOrEmpty(definition?.params_schema).properties || {};
            return (
            <div className="grid gap-2 rounded border border-[var(--fs-border-default)] p-2 sm:grid-cols-7" key={key}>
              <label className="block text-xs text-[var(--fs-text-secondary)] sm:col-span-2">
                <span>{t("security_overview.columns.action", "Action")}</span>
                <DsFormSelect
                  ariaLabel={t("security_overview.columns.action", "Action")}
                  className="mt-1"
                  name={`tabular-feed-action-${index}-id`}
                  onChange={(nextValue) => updateAction(index, { action: nextValue })}
                  value={textOrEmpty(action.action) || catalogActions[0]?.action_id || ""}
                  options={[...catalogActions.map((item: Record<string, any>) => (
                    ({ value: item.action_id, label: [item.category, " / ", item.display_name].join("") })
                  ))]}
                />
              </label>
              <label className="block text-xs text-[var(--fs-text-secondary)]">
                <span>{t("react.workflows.target_column", "Target column")}</span>
                <input
                  className={inputClass}
                  name={`tabular-feed-action-${index}-target`}
                  onChange={(event) => updateAction(index, { target: event.target.value })}
                  placeholder="price"
                  value={textOrEmpty(action.target)}
                />
              </label>
              <div className="space-y-2 text-xs text-[var(--fs-text-secondary)]">
                <span>{t("react.workflows.parameters", "Parameters")}</span>
                {Object.entries(properties).length ? Object.entries(properties).map(([name, schema]: [string, any]) => {
                  const kind = textOrEmpty(schema?.type) || "string";
                  const value = params[name];
                  const updateParam = (raw: string) => {
                    let next: unknown = raw;
                    if (kind === "number") next = raw === "" ? "" : Number(raw);
                    if (kind === "boolean") next = raw === "true";
                    updateAction(index, { params: { ...params, [name]: next } });
                  };
                  if (kind === "object") {
                    return <TabularJsonField className={`${inputClass} min-h-12 font-mono text-[11px]`} key={name} label={name} name={`tabular-feed-action-${key}-param-${name}`} onChange={(next) => updateAction(index, { params: { ...params, [name]: next } })} onValidationChange={ctx.onStepInputValidationChange} placeholder={'{"key":"value"}'} shape="object" value={value} />;
                  }
                  if (kind === "boolean") {
                    return <label className="block" key={name}><span>{name}</span><DsFormSelect
    ariaLabel={name}
    className="mt-1"
    name={`tabular-feed-action-${index}-param-${name}`}
    onChange={(nextValue) => updateParam(nextValue)}
    value={value === undefined ? "" : value === true ? "true" : "false"}
    options={[...(value === undefined ? [({ value: "", label: t("react.workflows.runtime_default_not_set", "Runtime default (not set)"), disabled: true })] : []), ({ value: "false", label: "false" }), ({ value: "true", label: "true" })]}
  /></label>;
                  }
                  return <label className="block" key={name}><span>{name}</span><input className={inputClass} name={`tabular-feed-action-${index}-param-${name}`} onChange={(event) => updateParam(event.target.value)} type={kind === "number" ? "number" : "text"} value={textOrEmpty(value)} /></label>;
                }) : <span className="block text-[var(--fs-text-tertiary)]">{t("react.workflows.no_parameters", "No parameters")}</span>}
              </div>
              <label className="block text-xs text-[var(--fs-text-secondary)]">
                <span>{t("react.workflows.when_column", "When column")}</span>
                <input
                  className={inputClass}
                  name={`tabular-feed-action-${index}-when-column`}
                  onChange={(event) =>
                    updateAction(index, {
                      when: { ...objectOrEmpty(action.when), column: event.target.value },
                    })
                  }
                  placeholder="supplier"
                  value={textOrEmpty(objectOrEmpty(action.when).column)}
                />
              </label>
              <label className="block text-xs text-[var(--fs-text-secondary)]">
                <span>{t("react.workflows.when_operator", "When operator")}</span>
                <DsFormSelect
                  ariaLabel={t("react.workflows.when_operator", "When operator")}
                  className="mt-1"
                  name={`tabular-feed-action-${index}-when-operator`}
                  onChange={(nextValue) => {
                    const when = objectOrEmpty(action.when);
                    const next = { column: textOrEmpty(when.column) } as Record<string, unknown>;
                    if (nextValue) next[nextValue] = textOrEmpty(when.equals ?? when.not_equals);
                    updateAction(index, { when: next });
                  }}
                  value={Object.keys(objectOrEmpty(action.when)).find((key) => key !== "column") || ""}
                  options={[({ value: "", label: t("react.workflows.always", "Always") }), ({ value: "equals", label: t("react.workflows.equals_variant_f939ae3d", "Equals") }), ({ value: "not_equals", label: t("react.workflows.not_equals_variant_91c659e9", "Not equals") })]}
                />
              </label>
              <label className="block text-xs text-[var(--fs-text-secondary)]">
                <span>{t("react.workflows.when_value", "When value")}</span>
                <input
                  className={inputClass}
                  name={`tabular-feed-action-${index}-when-value`}
                  onChange={(event) => {
                    const when = objectOrEmpty(action.when);
                    const operator = Object.keys(when).find((key) => key !== "column") || "equals";
                    updateAction(index, { when: { column: textOrEmpty(when.column), [operator]: event.target.value } });
                  }}
                  value={textOrEmpty(objectOrEmpty(action.when).equals ?? objectOrEmpty(action.when).not_equals)}
                />
              </label>
              <div className="flex items-end">
                <button
                  className="fs-button sm ghost-danger w-full"
                  onClick={() => {
                    actionKeyState.remove(index);
                    patchConfig({ actions: actions.filter((_, rowIndex) => rowIndex !== index) });
                  }}
                  type="button"
                >
                  {t("extensions.repeater.remove", "Remove")}</button>
              </div>
            </div>
            );
          })}
        </div>
      ))}

      {renderWhen(["transform", "concat"].includes(operation), (
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">{t("react.workflows.relational_operations", "Relational operations")}</p>
            <button
              className="fs-button sm secondary"
              onClick={() => {
                relationalKeyState.append();
                aggregationKeyState.appendGroup();
                patchConfig({ relational: [...relational, defaultRelationalOperation("filter")] });
              }}
              type="button"
            >
              {t("react.workflows.add_operation", "Add operation")}</button>
          </div>
          {relationalWithKeys.map(({ index, key, row }) => {
            const rowOperation = textOrEmpty(row.op);
            let operationFields: ReactNode = null;
            if (rowOperation === "filter") {
              operationFields = <label className="block text-xs text-[var(--fs-text-secondary)]">
                <span>{t("react.workflows.filter_column", "Filter column")}</span>
                <input
                  className={inputClass}
                  name={`tabular-feed-relational-${index}-filter-column`}
                  onChange={(event) =>
                    updateRelational(index, {
                      when: { ...objectOrEmpty(row.when), column: event.target.value },
                    })
                  }
                  placeholder="company"
                  value={textOrEmpty(objectOrEmpty(row.when).column)}
                />
              </label>;
            } else if (rowOperation === "sort") {
              operationFields = <>
                <label className="block text-xs text-[var(--fs-text-secondary)]"><span>{t("react.workflows.descending", "Descending")}</span><DsFormSelect
    ariaLabel={t("react.workflows.descending", "Descending")}
    className="mt-1"
    name={`tabular-feed-relational-${index}-descending`}
    onChange={(nextValue) => updateRelational(index, { descending: nextValue === "true" })}
    value={row.descending ? "true" : "false"}
    options={[({ value: "false", label: t("extensions.repeater.no", "No") }), ({ value: "true", label: t("extensions.repeater.yes", "Yes") })]}
  /></label>
                <label className="block text-xs text-[var(--fs-text-secondary)]"><span>{t("react.workflows.nulls", "Nulls")}</span><DsFormSelect
    ariaLabel={t("react.workflows.nulls", "Nulls")}
    className="mt-1"
    name={`tabular-feed-relational-${index}-nulls`}
    onChange={(nextValue) => updateRelational(index, { nulls_last: nextValue === "true" })}
    value={row.nulls_last !== false ? "true" : "false"}
    options={[({ value: "true", label: t("react.workflows.last", "Last") }), ({ value: "false", label: t("react.workflows.first_variant_a151ceb1", "First") })]}
  /></label>
              </>;
            } else if (rowOperation === "dedupe_by") {
              operationFields = <label className="block text-xs text-[var(--fs-text-secondary)]"><span>{t("react.workflows.keep", "Keep")}</span><DsFormSelect
    ariaLabel={t("react.workflows.keep", "Keep")}
    className="mt-1"
    name={`tabular-feed-relational-${index}-keep`}
    onChange={(nextValue) => updateRelational(index, { keep: nextValue })}
    value={textOrEmpty(row.keep) || "first"}
    options={[({ value: "first", label: t("react.workflows.first_variant_a151ceb1", "First") }), ({ value: "last", label: t("react.workflows.last", "Last") })]}
  /></label>;
            } else if (rowOperation === "unnest") {
              operationFields = (
                <label className="fs-field">
                  <OptionalLabel>{t("react.workflows.prefix", "Prefix")}</OptionalLabel>
                  <input
                    className="fs-input"
                    name={`tabular-feed-relational-${index}-prefix`}
                    onChange={(event) => updateRelational(index, { prefix: event.target.value })}
                    placeholder="variant_"
                    value={textOrEmpty(row.prefix)}
                  />
                </label>
              );
            }
            return (
            <div className="grid gap-2 rounded border border-[var(--fs-border-default)] p-2 sm:grid-cols-5" key={key}>
              <label className="block text-xs text-[var(--fs-text-secondary)]">
                <span>{t("react.workflows.operation", "Operation")}</span>
                <DsFormSelect
                  ariaLabel={t("react.workflows.operation", "Operation")}
                  className="mt-1"
                  name={`tabular-feed-relational-${index}-op`}
                  onChange={(nextValue) => updateRelational(index, { op: nextValue })}
                  value={textOrEmpty(row.op) || "filter"}
                  options={[...TABULAR_FEED_RELATIONAL_OPS.map((item) => (
                    ({ value: item, label: item })
                  ))]}
                />
              </label>
              <label className="block text-xs text-[var(--fs-text-secondary)]">
                <span>{["explode", "unnest"].includes(rowOperation) ? t("react.workflows.column", "Column") : t("react.workflows.columns_keys", "Columns / keys")}</span>
                <input
                  className={inputClass}
                  name={`tabular-feed-relational-${index}-columns`}
                  onChange={(event) => {
                    const op = textOrEmpty(row.op);
                    const value = event.target.value;
                    let update = { keys: listFromText(value), by: undefined } as Record<string, unknown>;
                    if (["explode", "unnest"].includes(op)) {
                      update = { column: value, keys: undefined };
                    } else if (op === "sort") {
                      update = { by: listFromText(value), keys: undefined };
                    }
                    updateRelational(index, update);
                  }}
                  placeholder="sku, supplier"
                  value={textOrEmpty(row.column) || listText(row.by ?? row.keys)}
                />
              </label>
              {operationFields}
              {textOrEmpty(row.op) === "filter" ? <><label className="block text-xs text-[var(--fs-text-secondary)]"><span>{t("users.role.operator", "Operator")}</span><DsFormSelect
    ariaLabel={t("users.role.operator", "Operator")}
    className="mt-1"
    name={`tabular-feed-relational-${index}-operator`}
    onChange={(nextValue) => { const when = objectOrEmpty(row.when); updateRelational(index, { when: { column: textOrEmpty(when.column), [nextValue]: textOrEmpty(when.equals ?? when.not_equals) } }); }}
    value={Object.keys(objectOrEmpty(row.when)).find((key) => key !== "column") || "equals"}
    options={[({ value: "equals", label: t("react.workflows.equals_variant_f939ae3d", "Equals") }), ({ value: "not_equals", label: t("react.workflows.not_equals_variant_91c659e9", "Not equals") })]}
  /></label><label className="block text-xs text-[var(--fs-text-secondary)]"><span>{t("project_edit.fields.value", "Value")}</span><input className={inputClass} name={`tabular-feed-relational-${index}-value`} onChange={(event) => { const when = objectOrEmpty(row.when); const operator = Object.keys(when).find((key) => key !== "column") || "equals"; updateRelational(index, { when: { column: textOrEmpty(when.column), [operator]: event.target.value } }); }} value={textOrEmpty(objectOrEmpty(row.when).equals ?? objectOrEmpty(row.when).not_equals)} /></label></> : null}
              {textOrEmpty(row.op) === "group_by" ? <div className="space-y-2 text-xs text-[var(--fs-text-secondary)]">
                <span>{t("react.workflows.aggregations", "Aggregations")}</span>
                {listFromUnknown(row.aggregations).map((aggregation, aggregationIndex) => {
                  const aggregationKey = aggregationKeyState.groups[index]?.[aggregationIndex];
                  return (
                  <div className="grid gap-1 sm:grid-cols-[1fr_1fr_1fr_auto]" key={aggregationKey}>
                    <input className={inputClass} name={`tabular-feed-relational-${index}-aggregation-${aggregationIndex}-column`} placeholder={t("react.workflows.column", "Column")} onChange={(event) => updateRelational(index, { aggregations: listFromUnknown(row.aggregations).map((item, itemIndex) => itemIndex === aggregationIndex ? { ...item, column: event.target.value } : item) })} value={textOrEmpty(aggregation.column)} />
                    <DsFormSelect
                      ariaLabel={t("react.workflows.aggregations", "Aggregations")}
                      className="mt-1"
                      name={`tabular-feed-relational-${index}-aggregation-${aggregationIndex}-agg`}
                      onChange={(nextValue) => updateRelational(index, { aggregations: listFromUnknown(row.aggregations).map((item, itemIndex) => itemIndex === aggregationIndex ? { ...item, agg: nextValue } : item) })}
                      value={textOrEmpty(aggregation.agg) || "sum"}
                      options={[({ value: "count", label: t("react.workflows.count", "Count") }), ({ value: "sum", label: t("react.workflows.sum", "Sum") }), ({ value: "min", label: t("react.workflows.min", "Min") }), ({ value: "max", label: t("react.workflows.max", "Max") }), ({ value: "first", label: t("react.workflows.first_variant_a151ceb1", "First") }), ({ value: "last", label: t("react.workflows.last", "Last") })]}
                    />
                    <input className={inputClass} name={`tabular-feed-relational-${index}-aggregation-${aggregationIndex}-as`} placeholder={t("react.workflows.output_name", "Output name")} onChange={(event) => updateRelational(index, { aggregations: listFromUnknown(row.aggregations).map((item, itemIndex) => itemIndex === aggregationIndex ? { ...item, as: event.target.value } : item) })} value={textOrEmpty(aggregation.as)} />
                    <button className="fs-button sm ghost-danger" onClick={() => {
                      aggregationKeyState.remove(index, aggregationIndex);
                      updateRelational(index, { aggregations: listFromUnknown(row.aggregations).filter((_, itemIndex) => itemIndex !== aggregationIndex) });
                    }} type="button">{t("extensions.repeater.remove", "Remove")}</button>
                  </div>
                  );
                })}
                <button className="fs-button sm secondary" onClick={() => {
                  aggregationKeyState.append(index);
                  updateRelational(index, { aggregations: [...listFromUnknown(row.aggregations), { column: "", agg: "sum", as: "" }] });
                }} type="button">{t("react.workflows.add_aggregation", "Add aggregation")}</button>
              </div> : null}
              <div className="flex items-end">
                <button
                  className="fs-button sm ghost-danger w-full"
                  onClick={() => {
                    relationalKeyState.remove(index);
                    aggregationKeyState.removeGroup(index);
                    patchConfig({ relational: relational.filter((_, rowIndex) => rowIndex !== index) });
                  }}
                  type="button"
                >
                  {t("extensions.repeater.remove", "Remove")}</button>
              </div>
            </div>
            );
          })}
        </div>
      ))}

      {renderWhen(["validate", "merge_rows", "create_dataset"].includes(operation), (
        <div className="grid gap-2 sm:grid-cols-2">
          {operation === "validate" ? (
              <TabularJsonField label={t("react.workflows.validation_checks", "Validation checks")} shape="object"
                className={`${inputClass} min-h-24 font-mono text-[11px]`}
                name="tabular-feed-validation-checks"
                onChange={(checks) => patchConfig({ checks })}
                onValidationChange={ctx.onStepInputValidationChange}
                placeholder='{"required_columns":["sku"],"unique":["sku"]}'
                value={stepKindConfig.checks}
              />
          ) : null}
          {["merge_rows", "create_dataset"].includes(operation) ? (
              <TabularJsonField label={t("react.workflows.rows", "Rows")} shape="rows"
                className={`${inputClass} min-h-24 font-mono text-[11px]`}
                name="tabular-feed-rows"
                onChange={(rows) => patchConfig({ rows })}
                onValidationChange={ctx.onStepInputValidationChange}
                placeholder='[{"sku":"A1","price":"10"}]'
                value={stepKindConfig.rows}
              />
          ) : null}
        </div>
      ))}

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="rounded border border-[var(--fs-border-default)] p-2">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">{t("registry.data_contracts.column.schema", "Schema")}</p>
          {schemaColumns.length ? (
            <div className="mt-2 flex flex-wrap gap-1">
              {schemaColumns.map((column) => (
                <span className="rounded border border-[var(--fs-border-default)] px-2 py-1 text-xs text-[var(--fs-text-primary)]" key={textOrEmpty(column.name) || jsonText(column)}>
                  {textOrEmpty(column.name) || textOrEmpty(column)}
                </span>
              ))}
            </div>
          ) : (
            <p className="mt-2 text-xs text-[var(--fs-text-tertiary)]">{t("react.workflows.no_inspected_schema_captured_yet", "No inspected schema captured yet.")}</p>
          )}
        </div>
        <div className="rounded border border-[var(--fs-border-default)] p-2">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">{t("react.workflows.10_row_preview", "10-row preview")}</p>
          <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-[11px] text-[var(--fs-text-secondary)]">
            {previewRows.length ? jsonText(previewRows) : t("react.workflows.display_no_preview_rows", "No preview rows captured yet.")}
          </pre>
        </div>
      </div>

      {renderWhen(Boolean(errors.length), (
        <div className="rounded border border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] p-2 text-xs text-[var(--fs-status-attention-ink)]">
          <p className="font-medium">{t("react.workflows.validation_errors", "Validation errors")}</p>
          <ul className="mt-1 space-y-1">
            {errors.map((error) => (
              <li key={`${error.path}-${error.message}`}>
                <span className="font-mono">{error.path}</span>: {error.message}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
