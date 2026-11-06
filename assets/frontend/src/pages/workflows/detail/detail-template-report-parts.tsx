/**
 * The form parts of the report editor: bindings to the step's declared inputs,
 * value formats, table columns and chart series, and text segments. Every part
 * edits schema v1 fields directly; no library option is ever shown.
 */
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import type { ReportFormat } from "@/components/report/report-types";
import { useI18n } from "@/lib/i18n/useI18n";

import {
  formatOfType,
  moveItem,
  removeAt,
  replaceAt,
  SLOT_FORMATS,
  type DefinitionField,
  type DefinitionSegment,
  type FormatSlot,
  type ValueBinding,
  without,
} from "./detail-template-report-model";

export type ReportInput = { name: string; type: string; source?: string };

type DigitsFormat = ReportFormat & { maximum_fraction_digits?: number; minimum_fraction_digits?: number };

export function Field({ label, children }: Readonly<{ label: string; children: ReactNode }>) {
  return (
    <label className="block min-w-0 text-xs fs-ink-secondary">
      <span>{label}</span>
      {children}
    </label>
  );
}

function useFormatLabels(): Record<ReportFormat["type"], string> {
  const { t } = useI18n();
  return {
    text: t("react.workflows.report_format_text", "Text"),
    boolean: t("react.workflows.report_format_boolean", "Yes / no"),
    number: t("react.workflows.report_format_number", "Number"),
    currency: t("react.workflows.report_format_currency", "Currency"),
    percent: t("react.workflows.report_format_percent", "Percent"),
    date: t("react.workflows.report_format_date", "Date"),
    datetime: t("react.workflows.report_format_datetime", "Date and time"),
  };
}

export function FormatFields({
  format,
  slot,
  label,
  inputClass,
  onChange,
}: Readonly<{
  format: ReportFormat;
  slot: FormatSlot;
  label: string;
  inputClass: string;
  onChange: (next: ReportFormat) => void;
}>) {
  const { t } = useI18n();
  const names = useFormatLabels();
  const numeric = format.type === "number" || format.type === "currency" || format.type === "percent";
  const digits = numeric ? (format as DigitsFormat).maximum_fraction_digits : undefined;
  return (
    <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_96px_96px]">
      <Field label={label}>
        <select
          aria-label={label}
          className={inputClass}
          onChange={(event) => onChange(formatOfType(event.target.value as ReportFormat["type"], format))}
          value={format.type}
        >
          {SLOT_FORMATS[slot].map((type) => (
            <option key={type} value={type}>
              {names[type]}
            </option>
          ))}
        </select>
      </Field>
      {format.type === "currency" ? (
        <Field label={t("react.workflows.report_currency_code", "Currency code")}>
          <input
            aria-label={t("react.workflows.report_currency_code", "Currency code")}
            className={inputClass}
            maxLength={3}
            onChange={(event) => onChange({ ...format, currency: event.target.value.toUpperCase() })}
            placeholder="EUR"
            value={format.currency}
          />
        </Field>
      ) : null}
      {numeric ? (
        <Field label={t("react.workflows.report_decimals", "Decimals")}>
          <input
            aria-label={t("react.workflows.report_decimals", "Decimals")}
            className={inputClass}
            max={6}
            min={0}
            onChange={(event) => {
              const rest = without(
                format as DigitsFormat,
                "maximum_fraction_digits",
                "minimum_fraction_digits",
              ) as ReportFormat;
              const raw = event.target.value;
              onChange(raw === "" ? rest : { ...rest, maximum_fraction_digits: Math.min(6, Math.max(0, Number(raw))) });
            }}
            type="number"
            value={digits ?? ""}
          />
        </Field>
      ) : null}
    </div>
  );
}

export function BindingFields({
  binding,
  inputs,
  label,
  inputClass,
  onChange,
}: Readonly<{
  binding: { input: string; path?: string };
  inputs: readonly ReportInput[];
  label: string;
  inputClass: string;
  onChange: (next: ValueBinding) => void;
}>) {
  const { t } = useI18n();
  const named = inputs.filter((input) => input.name);
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      <Field label={label}>
        <select
          aria-label={label}
          className={inputClass}
          onChange={(event) => onChange({ ...binding, input: event.target.value })}
          value={binding.input}
        >
          <option value="">
            {named.length
              ? t("react.workflows.choose_an_input", "Choose an input…")
              : t("react.workflows.declare_an_input_first", "Declare an input first")}
          </option>
          {named.map((input) => (
            <option key={input.name} value={input.name}>
              {input.name} ({input.type})
            </option>
          ))}
        </select>
      </Field>
      <Field label={t("react.workflows.report_path_in_input", "Path inside the input (optional)")}>
        <input
          aria-label={`${label}: ${t("react.workflows.report_path_in_input", "Path inside the input (optional)")}`}
          className={inputClass}
          onChange={(event) => {
            const rest = without(binding, "path");
            const path = event.target.value.trim();
            onChange(path ? { ...rest, path } : rest);
          }}
          placeholder="revenue"
          value={binding.path ?? ""}
        />
      </Field>
    </div>
  );
}

type IdentifiedField = DefinitionField & { id: string };

/**
 * The known fields of a component's rows as a datalist, or a note that the
 * shape is not known yet: field names typed by hand are checked when it runs.
 */
export function RowFieldSuggestions({ id, fields }: Readonly<{ id: string; fields: readonly string[] }>) {
  const { t } = useI18n();
  if (!fields.length) {
    return (
      <p className="text-xs fs-ink-tertiary">
        {t(
          "react.workflows.report_fields_unknown",
          "The fields of these rows are not known before a run. Type each field name; the step checks them when it runs.",
        )}
      </p>
    );
  }
  return (
    <datalist id={id}>
      {fields.map((field) => (
        <option key={field} value={field} />
      ))}
    </datalist>
  );
}

export function FieldRows({
  rows,
  slot,
  legend,
  addLabel,
  inputClass,
  newRow,
  onChange,
  suggestionsId,
}: Readonly<{
  rows: readonly IdentifiedField[];
  slot: FormatSlot;
  legend: string;
  addLabel: string;
  inputClass: string;
  newRow: () => IdentifiedField;
  onChange: (next: IdentifiedField[]) => void;
  /** The id of a datalist of known row fields, when there is one. */
  suggestionsId?: string;
}>) {
  const { t } = useI18n();
  return (
    <fieldset className="space-y-2">
      <legend className="text-xs fs-ink-secondary">{legend}</legend>
      {rows.map((row, index) => {
        const name = row.label || row.field || String(index + 1);
        return (
          <div className="space-y-2 rounded-lg border border-default p-2" key={row.id}>
            <div className="grid gap-2 sm:grid-cols-2">
              <Field label={t("react.workflows.report_field", "Field in each row")}>
                <input
                  aria-label={t("react.workflows.report_field_of", "Field of {name}", { name })}
                  className={inputClass}
                  list={suggestionsId}
                  onChange={(event) => onChange(replaceAt(rows, index, { ...row, field: event.target.value }))}
                  placeholder="revenue"
                  value={row.field}
                />
              </Field>
              <Field label={t("react.workflows.report_label", "Label")}>
                <input
                  aria-label={t("react.workflows.report_label_of", "Label of {name}", { name })}
                  className={inputClass}
                  onChange={(event) => onChange(replaceAt(rows, index, { ...row, label: event.target.value }))}
                  value={row.label}
                />
              </Field>
            </div>
            <FormatFields
              format={row.format}
              inputClass={inputClass}
              label={t("react.workflows.report_format_of", "Format of {name}", { name })}
              onChange={(format) => onChange(replaceAt(rows, index, { ...row, format }))}
              slot={slot}
            />
            <ItemActions
              count={rows.length}
              index={index}
              name={name}
              onMove={(delta) => onChange(moveItem(rows, index, delta))}
              onRemove={() => onChange(removeAt(rows, index))}
            />
          </div>
        );
      })}
      <Button onClick={() => onChange([...rows, newRow()])} size="sm" type="button" variant="secondary">
        {addLabel}
      </Button>
    </fieldset>
  );
}

export function ItemActions({
  count,
  index,
  name,
  onMove,
  onRemove,
}: Readonly<{ count: number; index: number; name: string; onMove: (delta: number) => void; onRemove: () => void }>) {
  const { t } = useI18n();
  return (
    <div className="flex flex-wrap gap-2">
      <Button
        aria-label={t("react.workflows.report_move_up", "Move {name} up", { name })}
        disabled={index === 0}
        onClick={() => onMove(-1)}
        size="sm"
        type="button"
        variant="ghost"
      >
        {t("react.workflows.report_up", "Up")}
      </Button>
      <Button
        aria-label={t("react.workflows.report_move_down", "Move {name} down", { name })}
        disabled={index === count - 1}
        onClick={() => onMove(1)}
        size="sm"
        type="button"
        variant="ghost"
      >
        {t("react.workflows.report_down", "Down")}
      </Button>
      <Button
        aria-label={t("react.workflows.report_remove", "Remove {name}", { name })}
        onClick={onRemove}
        size="sm"
        type="button"
        variant="ghost-danger"
      >
        {t("extensions.repeater.remove", "Remove")}
      </Button>
    </div>
  );
}

export function SegmentRows({
  segments,
  inputs,
  inputClass,
  onChange,
}: Readonly<{
  segments: readonly DefinitionSegment[];
  inputs: readonly ReportInput[];
  inputClass: string;
  onChange: (next: DefinitionSegment[]) => void;
}>) {
  const { t } = useI18n();
  return (
    <fieldset className="space-y-2">
      <legend className="text-xs fs-ink-secondary">{t("react.workflows.report_text_parts", "Text parts")}</legend>
      {segments.map((segment, index) => {
        const name = t("react.workflows.report_part_n", "part {n}", { n: index + 1 });
        return (
          // Segments carry no id; their position is their identity while editing.
          <div className="space-y-2 rounded-lg border border-default p-2" key={index}>
            {"text" in segment ? (
              <Field label={t("react.workflows.report_fixed_text", "Fixed text")}>
                <input
                  aria-label={t("react.workflows.report_fixed_text_of", "Fixed text of {name}", { name })}
                  className={inputClass}
                  onChange={(event) => onChange(replaceAt(segments, index, { text: event.target.value }))}
                  value={segment.text}
                />
              </Field>
            ) : (
              <>
                <BindingFields
                  binding={segment.value}
                  inputClass={inputClass}
                  inputs={inputs}
                  label={t("react.workflows.report_value_of", "Value of {name}", { name })}
                  onChange={(value) => onChange(replaceAt(segments, index, { ...segment, value }))}
                />
                <FormatFields
                  format={segment.format}
                  inputClass={inputClass}
                  label={t("react.workflows.report_format_of", "Format of {name}", { name })}
                  onChange={(format) => onChange(replaceAt(segments, index, { ...segment, format }))}
                  slot="value"
                />
              </>
            )}
            <ItemActions
              count={segments.length}
              index={index}
              name={name}
              onMove={(delta) => onChange(moveItem(segments, index, delta))}
              onRemove={() => onChange(removeAt(segments, index))}
            />
          </div>
        );
      })}
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => onChange([...segments, { text: "" }])} size="sm" type="button" variant="secondary">
          {t("react.workflows.report_add_text", "Add fixed text")}
        </Button>
        <Button
          onClick={() => onChange([...segments, { value: { input: "" }, format: { type: "text" } }])}
          size="sm"
          type="button"
          variant="secondary"
        >
          {t("react.workflows.report_add_value", "Add a value")}
        </Button>
      </div>
    </fieldset>
  );
}
