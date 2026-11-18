import { useId, type ReactNode } from "react";

import { DsCheckbox, DsRadio, DsSelect } from "@/components/shared/design-system";
import { DsSwitch } from "@/components/shared/ds-switch";
import { useI18n } from "@/lib/i18n/useI18n";
import { buildInputFields, type InputFieldModel } from "@/pages/projects/user-sections/schema";
import type { JsonSchema } from "@/pages/projects/user-sections/types";

export type InputValues = Record<string, unknown>;

type FieldProps = Readonly<{
  field: InputFieldModel;
  value: unknown;
  onChange: (value: unknown) => void;
  disabled?: boolean;
}>;

type FilesProps = Readonly<{
  files: Record<string, File[]>;
  onFiles: (name: string, files: File[]) => void;
  /** File inputs that take several files: one run per file (For each). */
  multipleFiles?: ReadonlySet<string>;
}>;

function enumOptions(schema: JsonSchema | undefined) {
  return (Array.isArray(schema?.enum) ? schema.enum : []).map((option) => ({
    value: String(option),
    label: String(option),
  }));
}

function numberOrEmpty(text: string): number | "" {
  if (!text.trim()) return "";
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : "";
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function FieldFrame({ field, children, labelId }: Readonly<{ field: InputFieldModel; children: ReactNode; labelId: string }>) {
  return (
    <div className="fs-field md:col-span-2" data-field={field.path}>
      <span className="fs-field-label" id={labelId}>
        {field.label}
        {field.required ? <span aria-hidden="true" className="fs-required">*</span> : null}
      </span>
      {children}
      {field.helpText ? <p className="text-sm text-muted">{field.helpText}</p> : null}
    </div>
  );
}

function ScalarList({ field, value, onChange, disabled }: FieldProps) {
  const { t } = useI18n();
  const rows = asList(value);
  const numeric = field.kind === "number_list";
  const update = (index: number, next: unknown) => onChange(rows.map((row, at) => (at === index ? next : row)));
  return (
    <div className="space-y-2">
      {rows.map((row, index) => (
        <div className="flex gap-2" key={`${field.path}-${index}`}>
          <input
            aria-label={t("react.user_sections.list_item", "{label} item {index}", { label: field.label, index: String(index + 1) })}
            className="fs-input"
            disabled={disabled}
            inputMode={numeric ? "decimal" : undefined}
            onChange={(event) => update(index, numeric ? numberOrEmpty(event.target.value) : event.target.value)}
            type={numeric ? "number" : "text"}
            value={row === null || row === undefined ? "" : String(row)}
          />
          <button className="fs-button" disabled={disabled} onClick={() => onChange(rows.filter((_, at) => at !== index))} type="button">
            {t("react.user_sections.remove", "Remove")}
          </button>
        </div>
      ))}
      <button className="fs-button" disabled={disabled} onClick={() => onChange([...rows, numeric ? "" : ""])} type="button">
        {t("react.user_sections.add_item", "Add item")}
      </button>
    </div>
  );
}

function EnumList({ field, value, onChange, disabled }: FieldProps) {
  const selected = new Set(asList(value).map(String));
  return (
    <div className="flex flex-col gap-1">
      {enumOptions(field.schema.items).map((option) => (
        <DsCheckbox
          checked={selected.has(option.value)}
          disabled={disabled}
          key={option.value}
          onChange={(checked) =>
            onChange(checked ? [...selected, option.value] : [...selected].filter((item) => item !== option.value))
          }
        >
          {option.label}
        </DsCheckbox>
      ))}
    </div>
  );
}

function ObjectGroup({ field, value, onChange, disabled }: FieldProps) {
  const record = asRecord(value);
  return (
    <div className="grid gap-3 fs-surface-radius border border-subtle fs-surface-inset p-3 md:grid-cols-2">
      <WorkflowInputFields
        disabled={disabled}
        fields={buildInputFields(field.schema)}
        onChange={(name, next) => onChange({ ...record, [name]: next })}
        values={record}
      />
    </div>
  );
}

function ObjectRepeater({ field, value, onChange, disabled }: FieldProps) {
  const { t } = useI18n();
  const rows = asList(value).map(asRecord);
  const itemFields = buildInputFields(field.schema.items ?? {});
  return (
    <div className="space-y-3">
      {rows.map((row, index) => (
        <div className="space-y-2 fs-surface-radius border border-subtle fs-surface-inset p-3" key={`${field.path}-${index}`}>
          <div className="grid gap-3 md:grid-cols-2">
            <WorkflowInputFields
              disabled={disabled}
              fields={itemFields}
              onChange={(name, next) => onChange(rows.map((item, at) => (at === index ? { ...item, [name]: next } : item)))}
              values={row}
            />
          </div>
          <button className="fs-button" disabled={disabled} onClick={() => onChange(rows.filter((_, at) => at !== index))} type="button">
            {t("react.user_sections.remove", "Remove")}
          </button>
        </div>
      ))}
      <button className="fs-button" disabled={disabled} onClick={() => onChange([...rows, {}])} type="button">
        {t("react.user_sections.add_row", "Add row")}
      </button>
    </div>
  );
}

function ScalarControl({ field, value, onChange, disabled, labelId }: FieldProps & { labelId: string }) {
  const { t } = useI18n();
  const text = value === null || value === undefined ? "" : String(value);
  switch (field.kind) {
    case "text":
    case "text_long":
      return field.widget === "textarea" ? (
        <textarea aria-labelledby={labelId} className="fs-input" disabled={disabled} onChange={(event) => onChange(event.target.value)} rows={4} value={text} />
      ) : (
        <input aria-labelledby={labelId} className="fs-input" disabled={disabled} onChange={(event) => onChange(event.target.value)} type="text" value={text} />
      );
    case "number":
      return (
        <input
          aria-labelledby={labelId}
          className="fs-input"
          disabled={disabled}
          inputMode="decimal"
          onChange={(event) => onChange(numberOrEmpty(event.target.value))}
          type="number"
          value={text}
        />
      );
    case "boolean":
      return field.widget === "switch" ? (
        <DsSwitch checked={value === true} disabled={disabled} label={field.label} onCheckedChange={(checked) => onChange(checked)} />
      ) : (
        <DsCheckbox checked={value === true} disabled={disabled} onChange={(checked) => onChange(checked)}>
          {field.label}
        </DsCheckbox>
      );
    case "enum":
      return field.widget === "radio" ? (
        <div className="flex flex-col gap-1" role="radiogroup" aria-labelledby={labelId}>
          {enumOptions(field.schema).map((option) => (
            <DsRadio checked={text === option.value} disabled={disabled} key={option.value} name={field.path} onChange={() => onChange(option.value)} value={option.value}>
              {option.label}
            </DsRadio>
          ))}
        </div>
      ) : (
        <DsSelect
          ariaLabel={field.label}
          disabled={disabled}
          labelledBy={labelId}
          onChange={(next) => onChange(next)}
          options={enumOptions(field.schema)}
          placeholder={t("react.user_sections.choose", "Choose…")}
          value={text}
        />
      );
    default:
      return null;
  }
}

function FieldControl({ field, value, onChange, disabled }: FieldProps) {
  const labelId = useId();
  let control: ReactNode;
  if (field.kind === "string_list" || field.kind === "number_list") {
    control = <ScalarList disabled={disabled} field={field} onChange={onChange} value={value} />;
  } else if (field.kind === "enum_list") {
    control = <EnumList disabled={disabled} field={field} onChange={onChange} value={value} />;
  } else if (field.kind === "object") {
    control = <ObjectGroup disabled={disabled} field={field} onChange={onChange} value={value} />;
  } else if (field.kind === "object_list") {
    control = <ObjectRepeater disabled={disabled} field={field} onChange={onChange} value={value} />;
  } else {
    control = <ScalarControl disabled={disabled} field={field} labelId={labelId} onChange={onChange} value={value} />;
  }
  return (
    <FieldFrame field={field} labelId={labelId}>
      {control}
    </FieldFrame>
  );
}

function FileControl({ field, files, onFiles, multiple, disabled }: Readonly<{
  field: InputFieldModel;
  files: File[];
  onFiles: (files: File[]) => void;
  multiple: boolean;
  disabled?: boolean;
}>) {
  const { t } = useI18n();
  const labelId = useId();
  return (
    <FieldFrame field={field} labelId={labelId}>
      {/* `accept` with images lets a phone offer its camera next to the gallery. */}
      <input
        accept="image/*,application/pdf"
        aria-labelledby={labelId}
        className="fs-input"
        disabled={disabled}
        multiple={multiple}
        onChange={(event) => onFiles(Array.from(event.target.files ?? []))}
        type="file"
      />
      {files.length ? (
        <p className="text-sm text-muted">
          {t("react.user_sections.files_selected", "{count} selected: {names}", {
            count: String(files.length),
            names: files.map((file) => file.name).join(", "),
          })}
        </p>
      ) : null}
    </FieldFrame>
  );
}

/**
 * The fields of a workflow's input contract as a form. Values are JSON values
 * (numbers, booleans, lists, objects); files are real File objects. Types and
 * requirements come from the contract; the section only chose labels and widgets.
 */
export function WorkflowInputFields({
  fields,
  values,
  onChange,
  disabled,
  files,
  onFiles,
  multipleFiles,
}: Readonly<{
  fields: InputFieldModel[];
  values: InputValues;
  onChange: (name: string, value: unknown) => void;
  disabled?: boolean;
}> & Partial<FilesProps>) {
  return (
    <>
      {fields.map((field) =>
        field.kind === "file" ? (
          <FileControl
            disabled={disabled}
            field={field}
            files={files?.[field.name] ?? []}
            key={field.path}
            multiple={Boolean(multipleFiles?.has(field.name))}
            onFiles={(next) => onFiles?.(field.name, next)}
          />
        ) : (
          <FieldControl disabled={disabled} field={field} key={field.path} onChange={(next) => onChange(field.name, next)} value={values[field.name]} />
        ),
      )}
    </>
  );
}
