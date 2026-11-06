/**
 * The no-code editor of a Template step in report format (task 4): report
 * settings, rows sources and the ordered component list. It edits the
 * ReportDefinition v1 the step stores, the same one the AI Builder writes.
 */
import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";

import { ComponentList } from "./detail-template-report-components";
import {
  componentIds,
  datasetProblem,
  freeId,
  moveItem,
  readDefinition,
  removeAt,
  replaceAt,
  reportIssuesByPlace,
  rowFieldSuggestions,
  withDatasets,
  type DefinitionDataset,
  type ReportDefinition,
  without,
} from "./detail-template-report-model";
import { BindingFields, Field, ItemActions, type ReportInput } from "./detail-template-report-parts";

function DatasetRows({
  datasets,
  inputs,
  inputClass,
  onChange,
  serverIssues,
}: Readonly<{
  serverIssues: ReadonlyMap<string, readonly string[]>;
  datasets: readonly DefinitionDataset[];
  inputs: readonly ReportInput[];
  inputClass: string;
  onChange: (next: DefinitionDataset[]) => void;
}>) {
  const { t } = useI18n();
  return (
    <fieldset className="space-y-2">
      <legend className="text-xs fs-ink-secondary">{t("react.workflows.report_rows_sources", "Rows sources")}</legend>
      <p className="text-xs fs-ink-tertiary">
        {t(
          "react.workflows.report_rows_sources_help",
          "A rows source is a list of records from one input, such as the rows a dataset query or a Transform step produced. Tables and charts show its rows in order.",
        )}
      </p>
      {datasets.map((dataset, index) => {
        const problem = datasetProblem(dataset, inputs);
        return (
          <div className="space-y-2 rounded-lg border border-default p-2" key={dataset.id}>
            <p className="text-xs font-medium fs-ink-primary">
              <span className="font-mono">{dataset.id}</span>
            </p>
            <BindingFields
              binding={dataset.source}
              inputClass={inputClass}
              inputs={inputs}
              label={t("react.workflows.report_rows_from", "Rows from {name}", { name: dataset.id })}
              onChange={(next) => {
                // A rows source only ever defaults to the empty list; keep one the author set.
                const binding = without(next, "default");
                const source = dataset.source.default ? { ...binding, default: dataset.source.default } : binding;
                onChange(replaceAt(datasets, index, { ...dataset, source }));
              }}
            />
            {problem ? (
              <p className="text-xs fs-ink-attention" role="status">
                {problem === "input_unknown"
                  ? t("react.workflows.report_problem_input", "Choose one of the step's inputs.")
                  : t(
                      "react.workflows.report_problem_rows",
                      "This input cannot give a list of rows; declare it as array or json, or add a path to a list.",
                    )}
              </p>
            ) : null}
            {(serverIssues.get(dataset.id) ?? []).map((message) => (
              <p className="text-xs fs-ink-attention" key={message} role="status">
                {message}
              </p>
            ))}
            <ItemActions
              count={datasets.length}
              index={index}
              name={dataset.id}
              onMove={(delta) => onChange(moveItem(datasets, index, delta))}
              onRemove={() => onChange(removeAt(datasets, index))}
            />
          </div>
        );
      })}
      <Button
        onClick={() =>
          onChange([...datasets, { id: freeId("rows", datasets.map((dataset) => dataset.id)), source: { input: "" } }])
        }
        size="sm"
        type="button"
        variant="secondary"
      >
        {t("react.workflows.report_add_rows_source", "Add rows source")}
      </Button>
    </fieldset>
  );
}

export function ReportDefinitionEditor({
  value,
  inputs,
  inputClass,
  onChange,
  variablePaths = [],
  validationIssues,
  stepId = "",
}: Readonly<{
  value: unknown;
  inputs: readonly ReportInput[];
  inputClass: string;
  onChange: (next: ReportDefinition) => void;
  /** Workflow variable paths the builder knows at this step, for row field suggestions. */
  variablePaths?: readonly string[];
  /** The workflow's last validation issues; those of this step's report are shown in place. */
  validationIssues?: unknown;
  stepId?: string;
}>) {
  const { t } = useI18n();
  const definition = readDefinition(value);
  const datasets = definition.datasets ?? [];
  const serverIssues = reportIssuesByPlace(validationIssues, stepId, definition);
  const set = (patch: Partial<ReportDefinition>) => onChange({ ...definition, ...patch });
  return (
    <section aria-label={t("react.workflows.report_editor", "Report")} className="space-y-3">
      {serverIssues.report.length ? (
        <ul className="space-y-1 text-xs fs-ink-attention" role="status">
          {serverIssues.report.map((message) => (
            <li key={message}>{message}</li>
          ))}
        </ul>
      ) : null}
      <div className="grid gap-2 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)]">
        <Field label={t("react.workflows.report_report_title", "Report title")}>
          <input
            aria-label={t("react.workflows.report_report_title", "Report title")}
            className={inputClass}
            onChange={(event) => set({ title: event.target.value })}
            value={definition.title ?? ""}
          />
        </Field>
        <Field label={t("react.workflows.report_locale", "Language and region")}>
          <input
            aria-label={t("react.workflows.report_locale", "Language and region")}
            className={inputClass}
            onChange={(event) => set({ locale: event.target.value.trim() })}
            placeholder="en-US"
            value={definition.locale ?? ""}
          />
        </Field>
        <Field label={t("react.workflows.report_time_zone", "Time zone")}>
          <input
            aria-label={t("react.workflows.report_time_zone", "Time zone")}
            className={inputClass}
            onChange={(event) => set({ time_zone: event.target.value.trim() })}
            placeholder="Europe/Berlin"
            value={definition.time_zone ?? ""}
          />
        </Field>
      </div>
      <DatasetRows
        datasets={datasets}
        serverIssues={serverIssues.datasets}
        inputClass={inputClass}
        inputs={inputs}
        onChange={(next) => onChange(withDatasets(definition, next))}
      />
      <div className="space-y-2">
        <p className="text-xs fs-ink-secondary">{t("react.workflows.report_components", "Components")}</p>
        <ComponentList
          components={definition.components}
          ctx={{
            inputs,
            datasetIds: datasets.map((dataset) => dataset.id),
            takenIds: componentIds(definition.components),
            inputClass,
            serverIssues: serverIssues.components,
            fieldSuggestions: new Map(
              datasets.map((dataset) => [dataset.id, rowFieldSuggestions(dataset, inputs, variablePaths)]),
            ),
          }}
          depth={1}
          onChange={(components) => set({ components })}
        />
      </div>
      <p className="text-xs fs-ink-tertiary">
        {t(
          "react.workflows.report_data_help",
          "A report shows values as they arrive: totals, sorting and filters belong in earlier steps. A number stored as text is an error, and an empty value shows as a dash.",
        )}
      </p>
    </section>
  );
}
