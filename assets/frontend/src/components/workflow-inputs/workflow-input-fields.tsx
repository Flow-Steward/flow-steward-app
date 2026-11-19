import { Plus, Trash2, X } from "lucide-react";
import { useId, useState, type CSSProperties, type ReactNode } from "react";

import { DsCheckbox, DsRadio, DsSelect, MultiCombobox } from "@/components/shared/design-system";
import { DsSwitch } from "@/components/shared/ds-switch";
import { useI18n } from "@/lib/i18n/useI18n";
import { renderMarkdownBlocks } from "@/pages/extensions/extension-markdown";
import "@/pages/dashboard/dashboard-segmented.css";

import {
  asList,
  asRecord,
  isFileField,
  optionLabel,
  shownFields,
  type FormFieldView,
  type FormFiles,
  type FormValues,
} from "./form-field";
import "./workflow-input-fields.css";

export type InputValues = FormValues;

/** How a control is named and described: by the field's visible label, or alone in a table cell. */
type ControlProps = Readonly<{
  field: FormFieldView;
  value: unknown;
  onChange: (value: unknown) => void;
  disabled?: boolean;
  labelId?: string;
  ariaLabel?: string;
  describedBy?: string;
  invalid?: boolean;
}>;

const INPUT_TYPES: Record<string, string> = {
  text: "text",
  email: "email",
  url: "url",
  date: "date",
  date_time: "datetime-local",
  time: "time",
};

function textOf(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

function numberOrEmpty(text: string): number | "" {
  if (!text.trim()) return "";
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : "";
}

function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function sameFile(left: File, right: File): boolean {
  return left.name === right.name && left.size === right.size && left.lastModified === right.lastModified;
}

/** Prepend/append text around a single-line input, inside one input frame. */
function Affixed({ field, children }: Readonly<{ field: FormFieldView; children: (className: string | undefined) => ReactNode }>) {
  if (!field.prepend && !field.append) return <>{children("fs-input")}</>;
  return (
    <span className="fs-input-shell">
      {field.prepend ? <span className="fs-workflow-affix-text">{field.prepend}</span> : null}
      {children(undefined)}
      {field.append ? <span className="fs-workflow-affix-text">{field.append}</span> : null}
    </span>
  );
}

/** WYSIWYG: the repository has no rich-text editor, so Markdown with the Core renderer's preview. */
function MarkdownControl({ field, value, onChange, disabled, labelId, ariaLabel, describedBy, invalid }: ControlProps) {
  const { t } = useI18n();
  const [preview, setPreview] = useState(false);
  const text = textOf(value);
  return (
    <div className="grid gap-2">
      <div aria-label={t("react.operator_form.wysiwyg_mode", "Editor mode")} className="fs-seg" role="group">
        <button aria-pressed={!preview} className="fs-seg__item" onClick={() => setPreview(false)} type="button">
          {t("react.operator_form.wysiwyg_write", "Write")}
        </button>
        <button aria-pressed={preview} className="fs-seg__item" onClick={() => setPreview(true)} type="button">
          {t("react.operator_form.wysiwyg_preview", "Preview")}
        </button>
      </div>
      {preview ? (
        <div className="fs-workflow-markdown-preview">{text.trim() ? renderMarkdownBlocks(text) : null}</div>
      ) : (
        <textarea
          aria-describedby={describedBy}
          aria-invalid={invalid || undefined}
          aria-label={ariaLabel}
          aria-labelledby={labelId}
          className="fs-input"
          data-markdown=""
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
          placeholder={field.placeholder || t("react.operator_form.wysiwyg_placeholder", "Markdown works: **bold**, lists, links")}
          rows={6}
          value={text}
        />
      )}
    </div>
  );
}

function TextControl(props: ControlProps) {
  const { field, value, onChange, disabled, labelId, ariaLabel, describedBy, invalid } = props;
  if (field.widget === "wysiwyg") return <MarkdownControl {...props} />;
  const common = {
    "aria-describedby": describedBy,
    "aria-invalid": invalid || undefined,
    "aria-label": ariaLabel,
    "aria-labelledby": labelId,
    disabled,
    placeholder: field.placeholder || undefined,
    value: textOf(value),
  };
  if (field.widget === "textarea") {
    return <textarea {...common} className="fs-input" onChange={(event) => onChange(event.target.value)} rows={4} />;
  }
  return (
    <Affixed field={field}>
      {(className) => (
        <input {...common} className={className} onChange={(event) => onChange(event.target.value)} type={INPUT_TYPES[field.widget] ?? "text"} />
      )}
    </Affixed>
  );
}

function NumberControl({ field, value, onChange, disabled, labelId, ariaLabel, describedBy, invalid }: ControlProps) {
  return (
    <Affixed field={field}>
      {(className) => (
        <input
          aria-describedby={describedBy}
          aria-invalid={invalid || undefined}
          aria-label={ariaLabel}
          aria-labelledby={labelId}
          className={className}
          disabled={disabled}
          inputMode="decimal"
          onChange={(event) => onChange(numberOrEmpty(event.target.value))}
          placeholder={field.placeholder || undefined}
          type="number"
          value={textOf(value)}
        />
      )}
    </Affixed>
  );
}

function ChoiceControl({ field, value, onChange, disabled, labelId, ariaLabel }: ControlProps) {
  const { t } = useI18n();
  const name = useId();
  const options = (field.options ?? []).map((option) => ({ value: option, label: optionLabel(field, option) }));
  const text = textOf(value);
  if (field.widget === "radio") {
    return (
      <div aria-label={ariaLabel} aria-labelledby={labelId} className="flex flex-col gap-1" role="radiogroup">
        {options.map((option) => (
          <DsRadio checked={text === option.value} disabled={disabled} key={option.value} name={name} onChange={() => onChange(option.value)} value={option.value}>
            {option.label}
          </DsRadio>
        ))}
      </div>
    );
  }
  if (field.widget === "button_group") {
    return (
      <div aria-label={ariaLabel} aria-labelledby={labelId} className="fs-seg" role="group">
        {options.map((option) => (
          <button
            aria-pressed={text === option.value}
            className="fs-seg__item"
            disabled={disabled}
            key={option.value}
            // An optional choice can be taken back; a required one only changed.
            onClick={() => onChange(text === option.value && !field.required ? "" : option.value)}
            type="button"
          >
            {option.label}
          </button>
        ))}
      </div>
    );
  }
  return (
    <DsSelect
      ariaLabel={ariaLabel || field.label}
      disabled={disabled}
      labelledBy={labelId}
      onChange={(next) => onChange(next)}
      options={options}
      placeholder={field.placeholder || t("react.operator_form.choose", "Choose…")}
      value={text}
    />
  );
}

function MultiChoiceControl({ field, value, onChange, disabled, labelId, ariaLabel }: ControlProps) {
  const { t } = useI18n();
  const selected = asList(value).map(String);
  const options = (field.options ?? []).map((option) => ({ value: option, label: optionLabel(field, option) }));
  if (field.widget === "multiselect") {
    return (
      <MultiCombobox
        ariaLabel={ariaLabel || field.label}
        disabled={disabled}
        onChange={(next) => onChange(next)}
        options={options}
        placeholder={field.placeholder || t("react.operator_form.choose_several", "Choose one or more…")}
        selected={selected}
      />
    );
  }
  return (
    <div aria-label={ariaLabel} aria-labelledby={labelId} className="flex flex-col gap-1" role="group">
      {options.map((option) => (
        <DsCheckbox
          checked={selected.includes(option.value)}
          disabled={disabled}
          key={option.value}
          onChange={(checked) =>
            // The contract's order, not the click order.
            onChange((field.options ?? []).filter((item) => (item === option.value ? checked : selected.includes(item))))
          }
          value={option.value}
        >
          {option.label}
        </DsCheckbox>
      ))}
    </div>
  );
}

function GroupControl({ field, value, onChange, disabled, labelId, ariaLabel }: ControlProps) {
  const record = asRecord(value);
  return (
    <div aria-label={ariaLabel} aria-labelledby={labelId} className="fs-workflow-group" role="group">
      <WorkflowInputFields
        disabled={disabled}
        fields={field.sub_fields ?? []}
        onChange={(name, next) => onChange({ ...record, [name]: next })}
        values={record}
      />
    </div>
  );
}

function RepeaterControl({ field, value, onChange, disabled, labelId, ariaLabel }: ControlProps) {
  const { t } = useI18n();
  const rows = asList(value).map(asRecord);
  const subFields = (field.sub_fields ?? []).filter((sub) => !sub.hidden && !sub.fixed && !isFileField(sub));
  const min = typeof field.min_items === "number" ? field.min_items : 0;
  const max = typeof field.max_items === "number" ? field.max_items : Number.POSITIVE_INFINITY;
  const layout = field.layout || "table";
  const update = (index: number, name: string, next: unknown) =>
    onChange(rows.map((row, at) => (at === index ? { ...row, [name]: next } : row)));
  const rowLabel = (index: number) =>
    t("react.operator_form.row_number", "{label}, row {index}", { label: field.label, index: String(index + 1) });
  const removeButton = (index: number) => (
    <button
      aria-label={t("react.operator_form.remove_row", "Remove row {index}", { index: String(index + 1) })}
      className="fs-icon-button"
      disabled={disabled || rows.length <= min}
      onClick={() => onChange(rows.filter((_, at) => at !== index))}
      type="button"
    >
      <Trash2 aria-hidden="true" />
    </button>
  );
  let body: ReactNode = null;
  if (layout === "table" && rows.length) {
    body = (
      <div className="fs-workflow-repeater-scroll">
        <table className="fs-workflow-repeater-table">
          <thead>
            <tr>
              <th aria-label={t("react.operator_form.row", "Row")} />
              {subFields.map((sub) => (
                <th key={sub.name} scope="col">
                  {sub.label}
                  {sub.required ? <span aria-hidden="true" className="fs-required">*</span> : null}
                </th>
              ))}
              <th aria-label={t("react.operator_form.row_actions", "Row actions")} />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => {
              const shown = new Set(shownFields(subFields, row).map((sub) => sub.name));
              return (
                <tr data-row={index + 1} key={`${field.name}-${index}`}>
                  <td className="fs-workflow-repeater-index">{index + 1}</td>
                  {subFields.map((sub) => (
                    <td key={sub.name}>
                      {shown.has(sub.name) ? (
                        <FieldInput
                          ariaLabel={`${sub.label}, ${rowLabel(index)}`}
                          disabled={disabled}
                          field={sub}
                          onChange={(next) => update(index, sub.name, next)}
                          value={row[sub.name]}
                        />
                      ) : null}
                    </td>
                  ))}
                  <td className="fs-workflow-repeater-actions">{removeButton(index)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  } else if (layout !== "table") {
    body = rows.map((row, index) => (
      <div aria-label={rowLabel(index)} className="fs-workflow-repeater-row" data-layout={layout} data-row={index + 1} key={`${field.name}-${index}`} role="group">
        <div className="fs-workflow-repeater-row-head">
          <span>{t("react.operator_form.row_heading", "Row {index}", { index: String(index + 1) })}</span>
          {removeButton(index)}
        </div>
        <WorkflowInputFields disabled={disabled} fields={subFields} onChange={(name, next) => update(index, name, next)} values={row} />
      </div>
    ));
  }
  return (
    <div aria-label={ariaLabel} aria-labelledby={labelId} className="fs-workflow-repeater" data-layout={layout} role="group">
      {body}
      <div>
        <button className="fs-button" disabled={disabled || rows.length >= max} onClick={() => onChange([...rows, {}])} type="button">
          <Plus aria-hidden="true" />
          {field.button_label || t("react.operator_form.add_row", "Add row")}
        </button>
      </div>
    </div>
  );
}

/** The control of one non-file field, chosen by its kind and widget. */
function FieldInput(props: ControlProps) {
  const { field, value, onChange, disabled, ariaLabel, describedBy } = props;
  switch (field.kind) {
    case "string":
      return <TextControl {...props} />;
    case "number":
      return <NumberControl {...props} />;
    case "boolean":
      return (
        <DsSwitch
          checked={value === true}
          describedBy={describedBy}
          disabled={disabled}
          label={ariaLabel || field.label}
          onCheckedChange={(checked) => onChange(checked)}
        />
      );
    case "select":
      return <ChoiceControl {...props} />;
    case "select_list":
      return <MultiChoiceControl {...props} />;
    case "object":
      return <GroupControl {...props} />;
    case "object_list":
      return <RepeaterControl {...props} />;
    default:
      // A kind the form does not know: the server's issues already explain it.
      return null;
  }
}

function FileControl({
  field,
  files,
  onFiles,
  multiple,
  disabled,
  labelId,
  describedBy,
}: Readonly<{
  field: FormFieldView;
  files: File[];
  onFiles: (files: File[]) => void;
  multiple: boolean;
  disabled?: boolean;
  labelId: string;
  describedBy?: string;
}>) {
  const { t } = useI18n();
  return (
    <div className="grid gap-2">
      <input
        aria-describedby={describedBy}
        aria-labelledby={labelId}
        className="fs-input"
        disabled={disabled}
        multiple={multiple}
        onChange={(event) => {
          const picked = Array.from(event.target.files ?? []);
          // Several files add up across picks (a phone camera takes one at a time).
          onFiles(multiple ? [...files, ...picked.filter((file) => !files.some((known) => sameFile(known, file)))] : picked.slice(0, 1));
          event.target.value = "";
        }}
        type="file"
      />
      {files.length ? (
        <ul aria-label={t("react.operator_form.chosen_files", "Chosen files for {label}", { label: field.label })} className="fs-workflow-file-list">
          {files.map((file, index) => (
            <li key={`${file.name}-${file.size}-${file.lastModified}-${index}`}>
              <span title={file.name}>{file.name}</span>
              <span className="text-sm text-muted">{fileSize(file.size)}</span>
              <button
                aria-label={t("react.operator_form.remove_file", "Remove {name}", { name: file.name })}
                className="fs-icon-button"
                disabled={disabled}
                onClick={() => onFiles(files.filter((_, at) => at !== index))}
                type="button"
              >
                <X aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function FieldFrame({
  field,
  error,
  children,
}: Readonly<{
  field: FormFieldView;
  error?: string;
  children: (ids: { labelId: string; describedBy?: string; invalid: boolean }) => ReactNode;
}>) {
  const labelId = useId();
  const helpId = useId();
  const errorId = useId();
  const describedBy = [field.instructions ? helpId : "", error ? errorId : ""].filter(Boolean).join(" ") || undefined;
  const width = typeof field.width === "number" && field.width > 0 && field.width < 100 ? field.width : null;
  const style = width ? ({ "--fs-workflow-field-width": `${width}%` } as CSSProperties) : undefined;
  return (
    <div className="fs-field fs-workflow-field" data-field={field.name} data-widget={field.widget} data-width={width ?? undefined} style={style}>
      <span className="fs-field-label" id={labelId}>
        {field.label}
        {field.required ? <span aria-hidden="true" className="fs-required">*</span> : null}
      </span>
      {children({ labelId, describedBy, invalid: Boolean(error) })}
      {field.instructions ? (
        <p className="fs-field-helper" id={helpId}>
          {field.instructions}
        </p>
      ) : null}
      {error ? (
        <p className="fs-field-helper error" id={errorId}>
          {error}
        </p>
      ) : null}
    </div>
  );
}

/**
 * A workflow's input form from the server's `form_fields` (ADR 0075): each
 * field by its widget, in order, with its labels, instructions and width.
 * Hidden and fixed fields are never rendered; a field whose condition does not
 * hold is not rendered either. Values are JSON values; files are File objects,
 * kept apart because they travel as uploads.
 */
export function WorkflowInputFields({
  fields,
  values,
  onChange,
  disabled,
  files,
  onFiles,
  multipleFiles,
  errors,
}: Readonly<{
  fields: readonly FormFieldView[];
  values: InputValues;
  onChange: (name: string, value: unknown) => void;
  disabled?: boolean;
  files?: FormFiles;
  onFiles?: (name: string, files: File[]) => void;
  /** Single-file inputs that take several files here: For each sends one run per file. */
  multipleFiles?: ReadonlySet<string>;
  /** A message under a field, by field name. */
  errors?: Readonly<Record<string, string>>;
}>) {
  return (
    <div className="fs-workflow-form">
      {shownFields(fields, values, files).map((field) => {
        if (isFileField(field) && !onFiles) return null;
        return (
          <FieldFrame error={errors?.[field.name]} field={field} key={field.name}>
            {({ labelId, describedBy, invalid }) =>
              isFileField(field) ? (
                <FileControl
                  describedBy={describedBy}
                  disabled={disabled}
                  field={field}
                  files={files?.[field.name] ?? []}
                  labelId={labelId}
                  multiple={field.kind === "file_list" || Boolean(multipleFiles?.has(field.name))}
                  onFiles={(next) => onFiles?.(field.name, next)}
                />
              ) : (
                <FieldInput
                  describedBy={describedBy}
                  disabled={disabled}
                  field={field}
                  invalid={invalid}
                  labelId={labelId}
                  onChange={(next) => onChange(field.name, next)}
                  value={values[field.name]}
                />
              )
            }
          </FieldFrame>
        );
      })}
    </div>
  );
}
