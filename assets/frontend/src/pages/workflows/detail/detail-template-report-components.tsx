/**
 * The ordered component list of the report editor and the form of each
 * component type. Sections hold their own list, up to the schema's depth.
 */
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";

import {
  componentProblems,
  COMPONENT_TYPES,
  defaultFormat,
  freeId,
  MAX_DEPTH,
  moveItem,
  newComponent,
  removeAt,
  replaceAt,
  type ComponentProblem,
  type ComponentType,
  type DefinitionComponent,
  without,
} from "./detail-template-report-model";
import {
  BindingFields,
  Field,
  FieldRows,
  FormatFields,
  ItemActions,
  SegmentRows,
  type ReportInput,
} from "./detail-template-report-parts";

export type ListContext = {
  inputs: readonly ReportInput[];
  datasetIds: readonly string[];
  /** Every component id in the whole report, for new unique ids. */
  takenIds: readonly string[];
  inputClass: string;
};

function useTypeLabels(): Record<ComponentType, string> {
  const { t } = useI18n();
  return {
    text: t("react.workflows.report_component_text", "Text or heading"),
    metric: t("react.workflows.report_component_metric", "Key number"),
    table: t("react.workflows.report_component_table", "Table"),
    chart: t("react.workflows.report_component_chart", "Chart"),
    section: t("react.workflows.report_component_section", "Section"),
  };
}

function useProblemLabels(): Record<ComponentProblem, string> {
  const { t } = useI18n();
  return {
    input_unknown: t("react.workflows.report_problem_input", "Choose one of the step's inputs."),
    binding_impossible: t(
      "react.workflows.report_problem_binding",
      "This input's type cannot give this value; add a path, or declare the input as json.",
    ),
    dataset_unknown: t("react.workflows.report_problem_dataset", "Choose a rows source."),
    field_missing: t("react.workflows.report_problem_field", "Every column and series needs a field and a label."),
    label_missing: t("react.workflows.report_problem_label", "Add a label."),
    series_missing: t("react.workflows.report_problem_series", "Add at least one series."),
    columns_missing: t("react.workflows.report_problem_columns", "Add at least one column."),
  };
}

function DatasetSelect({
  value,
  ctx,
  onChange,
}: Readonly<{ value: string; ctx: ListContext; onChange: (next: string) => void }>) {
  const { t } = useI18n();
  const label = t("react.workflows.report_rows_source", "Rows source");
  return (
    <Field label={label}>
      <select aria-label={label} className={ctx.inputClass} onChange={(event) => onChange(event.target.value)} value={value}>
        <option value="">
          {ctx.datasetIds.length
            ? t("react.workflows.report_choose_rows", "Choose rows…")
            : t("react.workflows.report_add_rows_first", "Add a rows source first")}
        </option>
        {ctx.datasetIds.map((id) => (
          <option key={id} value={id}>
            {id}
          </option>
        ))}
      </select>
    </Field>
  );
}

function TextInput({
  label,
  value,
  ctx,
  onChange,
}: Readonly<{ label: string; value: string; ctx: ListContext; onChange: (next: string) => void }>) {
  return (
    <Field label={label}>
      <input aria-label={label} className={ctx.inputClass} onChange={(event) => onChange(event.target.value)} value={value} />
    </Field>
  );
}

type Comparison = NonNullable<Extract<DefinitionComponent, { type: "metric" }>["comparison"]>;

function ComparisonFields({
  comparison,
  ctx,
  onChange,
}: Readonly<{ comparison: Comparison; ctx: ListContext; onChange: (next: Comparison) => void }>) {
  const { t } = useI18n();
  return (
    <div className="space-y-2 rounded-lg border border-default p-2">
      <TextInput
        ctx={ctx}
        label={t("react.workflows.report_comparison_label", "Comparison label")}
        onChange={(label) => onChange({ ...comparison, label })}
        value={comparison.label}
      />
      <BindingFields
        binding={comparison.value}
        inputClass={ctx.inputClass}
        inputs={ctx.inputs}
        label={t("react.workflows.report_comparison_value", "Comparison value")}
        onChange={(value) => onChange({ ...comparison, value })}
      />
      <FormatFields
        format={comparison.format}
        inputClass={ctx.inputClass}
        label={t("react.workflows.report_comparison_format", "Comparison format")}
        onChange={(format) => onChange({ ...comparison, format })}
        slot="metric"
      />
    </div>
  );
}

function ComponentFields({
  component,
  depth,
  ctx,
  onChange,
}: Readonly<{
  component: DefinitionComponent;
  depth: number;
  ctx: ListContext;
  onChange: (next: DefinitionComponent) => void;
}>) {
  const { t } = useI18n();
  switch (component.type) {
    case "text":
      return (
        <div className="space-y-2">
          <Field label={t("react.workflows.report_text_style", "Style")}>
            <select
              aria-label={t("react.workflows.report_text_style", "Style")}
              className={ctx.inputClass}
              onChange={(event) => onChange({ ...component, style: event.target.value as "paragraph" | "heading" })}
              value={component.style}
            >
              <option value="paragraph">{t("react.workflows.report_paragraph", "Paragraph")}</option>
              <option value="heading">{t("react.workflows.report_heading", "Heading")}</option>
            </select>
          </Field>
          <SegmentRows
            inputClass={ctx.inputClass}
            inputs={ctx.inputs}
            onChange={(segments) => onChange({ ...component, segments })}
            segments={component.segments}
          />
        </div>
      );
    case "metric":
      return (
        <div className="space-y-2">
          <TextInput
            ctx={ctx}
            label={t("react.workflows.report_label", "Label")}
            onChange={(label) => onChange({ ...component, label })}
            value={component.label}
          />
          <BindingFields
            binding={component.value}
            inputClass={ctx.inputClass}
            inputs={ctx.inputs}
            label={t("react.workflows.report_value", "Value")}
            onChange={(value) => onChange({ ...component, value })}
          />
          <FormatFields
            format={component.format}
            inputClass={ctx.inputClass}
            label={t("react.workflows.report_format", "Format")}
            onChange={(format) => onChange({ ...component, format })}
            slot="metric"
          />
          <label className="flex items-center gap-2 text-xs fs-ink-secondary">
            <input
              checked={Boolean(component.comparison)}
              onChange={(event) => {
                const rest = without(component, "comparison");
                onChange(
                  event.target.checked
                    ? { ...rest, comparison: { label: "", value: { input: "" }, format: defaultFormat("metric") } }
                    : rest,
                );
              }}
              type="checkbox"
            />
            {t("react.workflows.report_comparison", "Show a comparison")}
          </label>
          {component.comparison ? (
            <ComparisonFields
              comparison={component.comparison}
              ctx={ctx}
              onChange={(comparison) => onChange({ ...component, comparison })}
            />
          ) : null}
        </div>
      );
    case "table":
      return (
        <div className="space-y-2">
          <TextInput
            ctx={ctx}
            label={t("react.workflows.report_caption", "Caption")}
            onChange={(caption) => onChange({ ...component, caption })}
            value={component.caption}
          />
          <DatasetSelect ctx={ctx} onChange={(dataset) => onChange({ ...component, dataset })} value={component.dataset} />
          <FieldRows
            addLabel={t("react.workflows.report_add_column", "Add column")}
            inputClass={ctx.inputClass}
            legend={t("react.workflows.report_columns", "Columns")}
            newRow={() => ({
              id: freeId("column", component.columns.map((column) => column.id)),
              field: "",
              label: "",
              format: defaultFormat("column"),
            })}
            onChange={(columns) => onChange({ ...component, columns })}
            rows={component.columns}
            slot="column"
          />
          <TextInput
            ctx={ctx}
            label={t("react.workflows.report_empty_text", "Text when there are no rows")}
            onChange={(empty_text) => onChange({ ...component, empty_text })}
            value={component.empty_text}
          />
        </div>
      );
    case "chart":
      return (
        <div className="space-y-2">
          <div className="grid gap-2 sm:grid-cols-2">
            <Field label={t("react.workflows.report_chart_type", "Chart type")}>
              <select
                aria-label={t("react.workflows.report_chart_type", "Chart type")}
                className={ctx.inputClass}
                onChange={(event) => onChange({ ...component, chart_type: event.target.value as "line" | "bar" })}
                value={component.chart_type}
              >
                <option value="bar">{t("react.workflows.report_bar_chart", "Bars")}</option>
                <option value="line">{t("react.workflows.report_line_chart", "Line")}</option>
              </select>
            </Field>
            <TextInput
              ctx={ctx}
              label={t("react.workflows.report_title", "Title")}
              onChange={(title) => onChange({ ...component, title })}
              value={component.title}
            />
          </div>
          <TextInput
            ctx={ctx}
            label={t("react.workflows.report_chart_description", "Description for screen readers")}
            onChange={(description) => onChange({ ...component, description })}
            value={component.description}
          />
          <DatasetSelect ctx={ctx} onChange={(dataset) => onChange({ ...component, dataset })} value={component.dataset} />
          <fieldset className="space-y-2 rounded-lg border border-default p-2">
            <legend className="text-xs fs-ink-secondary">
              {t("react.workflows.report_categories", "Categories (one per row)")}
            </legend>
            <div className="grid gap-2 sm:grid-cols-2">
              <TextInput
                ctx={ctx}
                label={t("react.workflows.report_category_field", "Category field")}
                onChange={(field) => onChange({ ...component, category: { ...component.category, field } })}
                value={component.category.field}
              />
              <TextInput
                ctx={ctx}
                label={t("react.workflows.report_category_label", "Category label")}
                onChange={(label) => onChange({ ...component, category: { ...component.category, label } })}
                value={component.category.label}
              />
            </div>
            <FormatFields
              format={component.category.format}
              inputClass={ctx.inputClass}
              label={t("react.workflows.report_category_format", "Category format")}
              onChange={(format) => onChange({ ...component, category: { ...component.category, format } })}
              slot="category"
            />
          </fieldset>
          <FieldRows
            addLabel={t("react.workflows.report_add_series", "Add series")}
            inputClass={ctx.inputClass}
            legend={t("react.workflows.report_series", "Series")}
            newRow={() => ({
              id: freeId("series", component.series.map((series) => series.id)),
              field: "",
              label: "",
              format: defaultFormat("series"),
            })}
            onChange={(series) => onChange({ ...component, series })}
            rows={component.series}
            slot="series"
          />
          <TextInput
            ctx={ctx}
            label={t("react.workflows.report_empty_text", "Text when there are no rows")}
            onChange={(empty_text) => onChange({ ...component, empty_text })}
            value={component.empty_text}
          />
        </div>
      );
    default:
      return (
        <div className="space-y-2">
          <TextInput
            ctx={ctx}
            label={t("react.workflows.report_title", "Title")}
            onChange={(title) => onChange({ ...component, title })}
            value={component.title}
          />
          <ComponentList
            components={component.children}
            ctx={ctx}
            depth={depth + 1}
            onChange={(children) => onChange({ ...component, children })}
          />
        </div>
      );
  }
}

export function ComponentList({
  components,
  depth,
  ctx,
  onChange,
}: Readonly<{
  components: readonly DefinitionComponent[];
  depth: number;
  ctx: ListContext;
  onChange: (next: DefinitionComponent[]) => void;
}>) {
  const { t } = useI18n();
  const typeLabels = useTypeLabels();
  const problemLabels = useProblemLabels();
  const [adding, setAdding] = useState<ComponentType>("metric");
  // A section at the deepest level can hold nothing, so it is not offered there.
  const offered = COMPONENT_TYPES.filter((type) => type !== "section" || depth < MAX_DEPTH - 1);
  return (
    <div className="space-y-2">
      {components.length ? null : (
        <p className="text-xs fs-ink-tertiary">{t("react.workflows.report_no_components", "No components yet.")}</p>
      )}
      <ol className="space-y-2">
        {components.map((component, index) => {
          const name = `${typeLabels[component.type]} ${component.id}`;
          const problems = componentProblems(component, ctx.inputs, ctx.datasetIds);
          return (
            <li aria-label={name} className="space-y-2 rounded-lg border border-default p-2" key={component.id}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs font-medium fs-ink-primary">
                  {typeLabels[component.type]} <span className="font-mono fs-ink-tertiary">{component.id}</span>
                </p>
                <ItemActions
                  count={components.length}
                  index={index}
                  name={name}
                  onMove={(delta) => onChange(moveItem(components, index, delta))}
                  onRemove={() => onChange(removeAt(components, index))}
                />
              </div>
              {problems.length ? (
                <ul className="space-y-1 text-xs fs-ink-attention" role="status">
                  {problems.map((problem) => (
                    <li key={problem}>{problemLabels[problem]}</li>
                  ))}
                </ul>
              ) : null}
              <ComponentFields
                component={component}
                ctx={ctx}
                depth={depth}
                onChange={(next) => onChange(replaceAt(components, index, next))}
              />
            </li>
          );
        })}
      </ol>
      <div className="flex flex-wrap items-end gap-2">
        <Field label={t("react.workflows.report_component_type", "Component")}>
          <select
            aria-label={t("react.workflows.report_component_type", "Component")}
            className={ctx.inputClass}
            onChange={(event) => setAdding(event.target.value as ComponentType)}
            value={adding}
          >
            {offered.map((type) => (
              <option key={type} value={type}>
                {typeLabels[type]}
              </option>
            ))}
          </select>
        </Field>
        <Button
          onClick={() => onChange([...components, newComponent(adding, ctx.takenIds, ctx.datasetIds[0] ?? "")])}
          size="sm"
          type="button"
          variant="secondary"
        >
          {t("react.workflows.report_add_component", "Add component")}
        </Button>
      </div>
    </div>
  );
}
