import { useI18n } from "@/lib/i18n/useI18n";
import { useState, type Dispatch, type SetStateAction } from "react";

import { DsCheckbox, DsSelect } from "@/components/shared/design-system";
import type { WorkflowContractField } from "@/pages/projects/detail/api";
import {
  arrayRowsValue,
  fieldArrayRows,
  fieldLabel,
  fieldObjectRows,
  objectRowsValue,
} from "@/pages/projects/detail/project-detail-helpers";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

type ProjectWorkflowInputFieldsProps = {
  effectiveWorkflowInputValues: Record<string, string>;
  setWorkflowInputValues: Dispatch<SetStateAction<Record<string, string>>>;
  workflowInputFields: WorkflowContractField[];
  onValidationChange?: (name: string, error: string) => void;
};

type SetWorkflowInputValues = Dispatch<SetStateAction<Record<string, string>>>;

function RequiredMark({ required }: Readonly<{ required?: boolean }>) {
  return required ? <span aria-hidden="true" className="fs-required">*</span> : null;
}

function setInputValue(setValues: SetWorkflowInputValues, name: string, value: string): void {
  setValues((current) => ({ ...current, [name]: value }));
}

function ObjectWorkflowInput({
  field,
  label,
  name,
  setValues,
  value,
  onValidationChange,
}: Readonly<{
  field: WorkflowContractField;
  label: string;
  name: string;
  setValues: SetWorkflowInputValues;
  value: string;
  onValidationChange?: (name: string, error: string) => void;
}>) {
  const { t } = useI18n();
  const [rows, setRows] = useState(() => fieldObjectRows(value));
  const [error, setError] = useState("");
  const [sourceValue, setSourceValue] = useState(value);
  if (sourceValue !== value) {
    setSourceValue(value);
    setRows(fieldObjectRows(value));
    setError("");
  }
  const updateRow = (index: number, patch: { key?: string; value?: string }) => {
    const next = rows.map((current, rowIndex) => rowIndex === index ? { ...current, ...patch } : current);
    setRows(next);
    try {
      const serialized = objectRowsValue(next);
      setSourceValue(serialized);
      setInputValue(setValues, name, serialized);
      setError("");
      onValidationChange?.(name, "");
    } catch (cause) {
      const isKeyError = cause instanceof Error && (
        cause.message === "Enter a key for every non-empty value."
        || cause.message.startsWith("Duplicate object key:")
      );
      if (!(cause instanceof SyntaxError) && !isKeyError) throw cause;
      const message = cause instanceof SyntaxError
        ? t("react.projects.value1_values_must_be_valid_json", "{value1} values must be valid JSON.", { value1: label })
        : (cause as Error).message === "Enter a key for every non-empty value."
          ? t("react.projects.object_input_key_required", "Enter a key for every non-empty value.")
          : t("react.projects.object_input_duplicate_key", "Duplicate object key: {key}.", { key: (cause as Error).message.slice("Duplicate object key: ".length, -1) });
      setError(message);
      onValidationChange?.(name, message);
    }
  };
  return (
    <div className="fs-field md:col-span-2">
      <span className="fs-field-label">{label}<RequiredMark required={field.required} /></span>
      <div className="space-y-2 fs-surface-radius border border-subtle fs-surface-inset p-2">
        {rows.map((row, index) => (
          <div className="grid gap-2 sm:grid-cols-2" key={`${name}-object-${index}`}>
            <input
              aria-label={t("react.projects.value1_key_value2", "{value1} key {value2}", { value1: String(label), value2: String(index + 1) })}
              className="fs-input"
              onChange={(event) => updateRow(index, { key: event.target.value })}
              placeholder={t("react.projects.field", "field")}
              value={row.key}
            />
            <input
              aria-label={t("react.projects.value1_value_value2", "{value1} value {value2}", { value1: String(label), value2: String(index + 1) })}
              className="fs-input"
              onChange={(event) => updateRow(index, { value: event.target.value })}
              placeholder={t("react.projects.value", "value")}
              value={row.value}
            />
          </div>
        ))}
        <button
          className="fs-button"
          onClick={() => setRows([...rows, { key: "", value: "" }])}
          type="button"
        >
          {t("extensions.repeater.add_row", "Add row")} </button>
      </div>
      {error ? <p role="alert">{error}</p> : null}
    </div>
  );
}

function ArrayWorkflowInput({
  field,
  label,
  name,
  setValues,
  value,
  onValidationChange,
}: Readonly<{
  field: WorkflowContractField;
  label: string;
  name: string;
  setValues: SetWorkflowInputValues;
  value: string;
  onValidationChange?: (name: string, error: string) => void;
}>) {
  const { t } = useI18n();
  const [rows, setRows] = useState(() => fieldArrayRows(value));
  const [error, setError] = useState("");
  const [sourceValue, setSourceValue] = useState(value);
  if (sourceValue !== value) {
    setSourceValue(value);
    setRows(fieldArrayRows(value));
    setError("");
  }
  const publish = (next: ReturnType<typeof fieldArrayRows>) => {
    setRows(next);
    try {
      const serialized = arrayRowsValue(next);
      setSourceValue(serialized);
      setInputValue(setValues, name, serialized);
      setError("");
      onValidationChange?.(name, "");
    } catch (cause) {
      if (!(cause instanceof SyntaxError)) throw cause;
      const message = t("react.projects.value1_items_must_be_valid_json", "{value1} items must be valid JSON.", { value1: String(label) });
      setError(message);
      onValidationChange?.(name, message);
    }
  };
  const updateRow = (index: number, patch: { value?: string; asJson?: boolean }) => {
    publish(rows.map((current, rowIndex) => rowIndex === index ? { ...current, ...patch } : current));
  };
  return (
    <div className="fs-field md:col-span-2">
      <span className="fs-field-label">{label}<RequiredMark required={field.required} /></span>
      <div className="space-y-2 fs-surface-radius border border-subtle fs-surface-inset p-2">
        {rows.map((row, index) => (
          <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_8rem]" key={`${name}-array-${index}`}>
            <input
              aria-label={t("react.projects.value1_item_value2", "{value1} item {value2}", { value1: String(label), value2: String(index + 1) })}
              className="fs-input"
              onChange={(event) => updateRow(index, { value: event.target.value })}
              placeholder={t("react.projects.item_value1", "Item {value1}", { value1: String(index + 1) })}
              value={row.value}
            />
            <DsSelect
              ariaLabel={`${t("react.projects.value1_item_value2", "{value1} item {value2}", { value1: String(label), value2: String(index + 1) })} ${t("trash.columns.type", "Type")}`}
              onChange={(nextType) => updateRow(index, { asJson: nextType === "json" })}
              options={[
                { value: "text", label: t("react.workflows.text", "Text") },
                { value: "json", label: t("react.registry.contract_type_json", "JSON") },
              ]}
              value={row.asJson ? "json" : "text"}
            />
          </div>
        ))}
        <button
          className="fs-button"
          onClick={() => publish([...rows, { value: "" }])}
          type="button"
        >
          {t("react.projects.add_item", "Add item")} </button>
      </div>
      {error ? <p role="alert">{error}</p> : null}
    </div>
  );
}

function WorkflowInputField({
  field,
  setValues,
  value,
  onValidationChange,
}: Readonly<{
  field: WorkflowContractField;
  setValues: SetWorkflowInputValues;
  value: string;
  onValidationChange?: (name: string, error: string) => void;
}>) {
  const { t } = useI18n();
  const name = firstStringFromUnknownValue([field.name, ""]).trim();
  const type = firstStringFromUnknownValue([field.type, "string"]).toLowerCase();
  const label = fieldLabel(field);
  if (type === "boolean") {
    return (
      <div className="mt-6">
        <DsCheckbox checked={value === "true"} onChange={(checked) => setInputValue(setValues, name, String(checked))}>
          {label}<RequiredMark required={field.required} />
        </DsCheckbox>
      </div>
    );
  }
  if (type === "object") {
    return <ObjectWorkflowInput field={field} label={label} name={name} setValues={setValues} value={value} onValidationChange={onValidationChange} />;
  }
  if (type === "array") {
    return <ArrayWorkflowInput field={field} label={label} name={name} setValues={setValues} value={value} onValidationChange={onValidationChange} />;
  }
  if (type === "paragraph") {
    return (
      <label className="fs-field md:col-span-2">
        <span className="fs-field-label">{label}<RequiredMark required={field.required} /></span>
        <textarea className="fs-textarea" onChange={(event) => setInputValue(setValues, name, event.target.value)} value={value} />
      </label>
    );
  }
  if (type === "select" && Array.isArray(field.options) && field.options.length) {
    return (
      <div className="fs-field">
        <span className="fs-field-label">{label}<RequiredMark required={field.required} /></span>
        <DsSelect
          ariaLabel={label}
          onChange={(nextValue) => setInputValue(setValues, name, nextValue)}
          options={field.options.map((option) => ({ value: option, label: option }))}
          placeholder={t("react.projects.select", "Select")}
          value={value}
        />
      </div>
    );
  }
  return (
    <label className="fs-field">
      <span className="fs-field-label">{label}<RequiredMark required={field.required} /></span>
      <input className="fs-input" onChange={(event) => setInputValue(setValues, name, event.target.value)} type={type === "number" ? "number" : "text"} value={value} />
    </label>
  );
}

export function ProjectWorkflowInputFields({
  effectiveWorkflowInputValues,
  setWorkflowInputValues,
  workflowInputFields,
  onValidationChange,
}: Readonly<ProjectWorkflowInputFieldsProps>) {
  if (!workflowInputFields.length) {
    return null;
  }

  return (
    <div className="fs-surface-radius border border-subtle fs-surface-inset p-3">
      <div className="grid gap-3 md:grid-cols-2">
        {workflowInputFields.map((field) => {
          const name = firstStringFromUnknownValue([field.name, ""]).trim();
          return (
            <WorkflowInputField
              field={field}
              key={name}
              setValues={setWorkflowInputValues}
              onValidationChange={onValidationChange}
              value={effectiveWorkflowInputValues[name] ?? ""}
            />
          );
        })}
      </div>
    </div>
  );
}
