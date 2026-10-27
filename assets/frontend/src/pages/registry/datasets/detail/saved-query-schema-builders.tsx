import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useMemo, useRef } from "react";

import { DsAlert, DsCheckbox, DsSelect } from "@/components/shared/design-system";
import {
  type DatasetResource,
  type QueryParameterField,
  outputSchemaWithField,
  outputSchemaWithFormat,
  outputSchemaFromFields,
  queryOutputFields,
  queryParameterFields,
  paramsSchemaForSql,
  objectValue,
  type QuerySchemaType,
} from "./dataset-detail-helpers";

function localizedPARAMETER_TYPES(t: I18nContextValue["t"] = fallbackContext.t): Array<{ label: string; value: QuerySchemaType }> {
  return [
  { value: "string", label: t("react.registry.text", "Text") },
  { value: "integer", label: t("react.registry.whole_number", "Whole number") },
  { value: "number", label: t("react.registry.decimal_number", "Decimal number") },
  { value: "boolean", label: t("react.registry.yes_no", "Yes / no") },
];
}

function localizedSTRING_FORMATS(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { value: "", label: t("react.registry.no_special_format", "No special format") },
  { value: "date", label: t("react.registry.date", "Date") },
  { value: "date-time", label: t("react.registry.date_and_time", "Date and time") },
  { value: "email", label: t("react.registry.email_address", "Email address") },
  { value: "uri", label: t("react.registry.web_address", "Web address") },
  { value: "uuid", label: "UUID" },
];
}

type SavedQuerySchemaBuildersProps = {
  outputSchema: Record<string, unknown>;
  paramsSchema: Record<string, unknown>;
  resources: DatasetResource[];
  sql: string;
  selectAllOutputByDefault: boolean;
  onOutputSchemaChange: (schema: Record<string, unknown>) => void;
  onParamsSchemaChange: (schema: Record<string, unknown>) => void;
};

function parameterTypeLabel(type: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const preservedTypeLabels: Record<string, string> = { object: "Object", array: "Array", null: "Null" };
  return localizedPARAMETER_TYPES(t).find((item) => item.value === type)?.label || preservedTypeLabels[type] || t("react.registry.unsupported_type", "Unsupported type");
}

export function SavedQuerySchemaBuilders({
  outputSchema,
  paramsSchema,
  resources,
  selectAllOutputByDefault,
  sql,
  onOutputSchemaChange,
  onParamsSchemaChange,
}: Readonly<SavedQuerySchemaBuildersProps>) {
  const { t } = useI18n();
  const parameterFields = useMemo(() => queryParameterFields(sql, paramsSchema), [paramsSchema, sql]);
  const outputFields = useMemo(() => queryOutputFields(resources), [resources]);
  const outputProperties = useMemo(() => {
    const value = outputSchema.properties;
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  }, [outputSchema]);
  const outputDefaultApplied = useRef(false);
  const selectedOutputCount = Object.keys(outputProperties).length;
  const allOutputSelected = outputFields.length > 0 && selectedOutputCount === outputFields.length;

  useEffect(() => {
    if (!selectAllOutputByDefault || outputDefaultApplied.current || !outputFields.length) {
      return;
    }
    outputDefaultApplied.current = true;
    onOutputSchemaChange(outputSchemaFromFields(outputFields));
  }, [onOutputSchemaChange, outputFields, selectAllOutputByDefault]);

  function updateParameter(name: string, patch: Partial<QueryParameterField>) {
    const base = paramsSchemaForSql(sql, paramsSchema);
    const properties = { ...objectValue(base.properties) };
    const property = { ...objectValue(properties[name]) };
    if (patch.type !== undefined) property.type = patch.type;
    if (patch.format !== undefined) {
      if (patch.format) property.format = patch.format;
      else delete property.format;
    }
    properties[name] = property;
    const next = { ...base, properties };
    if (patch.required !== undefined) {
      const required = new Set(Array.isArray(base.required) ? base.required : []);
      if (patch.required) required.add(name);
      else required.delete(name);
      onParamsSchemaChange({ ...next, required: [...required] });
    } else onParamsSchemaChange(next);
  }

  function storedParameterType(field: QueryParameterField): string {
    const properties = objectValue(paramsSchemaForSql(sql, paramsSchema).properties);
    const type = objectValue(properties[field.name]).type;
    return typeof type === "string" ? type : "";
  }

  function parameterTypeEditable(field: QueryParameterField): boolean {
    return localizedPARAMETER_TYPES(t).some((item) => item.value === storedParameterType(field));
  }

  return (
    <div className="grid gap-5">
      <section className="fs-panel-stack">
        <h3 className="fs-panel-title">{t("react.registry.input_parameters", "Input parameters")}</h3>
        <p className="fs-field-helper">
          {t("react.registry.parameters_appear_automatically_from", "Parameters appear automatically from")}{" "}<code className="fs-code-value">:name</code> {" "}{t("react.registry.in_your_sql_choose_what_each_value_represents", "in your SQL. Choose what each value represents.")}</p>
        {parameterFields.length ? (
          <div className="grid gap-3">
            {parameterFields.map((field) => (
              <div
                className="fs-surface-radius fs-surface-inset border border-subtle p-3"
                key={field.name}
              >
                <code className="fs-code-value">:{field.name}</code>
                <p className="fs-field-helper">{t("react.registry.used_in_sql_as_a_query_value", "Used in SQL as a query value.")}</p>
                <div className="fs-form-grid">
                <div className="fs-field">
                  <span className="fs-field-label">{t("trash.columns.type", "Type")}</span>
                  {parameterTypeEditable(field) ? (
                  <DsSelect
                    ariaLabel={t("react.registry.parameter_type_for_value0", "Parameter type for {value0}", { value0: field.name })}
                    onChange={(value) =>
                      updateParameter(field.name, { format: "", type: value as QuerySchemaType })
                    }
                    options={localizedPARAMETER_TYPES(t).map((type) => ({
                      value: type.value,
                      label: type.label,
                    }))}
                    value={field.type}
                  />
                  ) : (
                    <output aria-label={t("react.registry.parameter_type_for_value0", "Parameter type for {value0}", { value0: field.name })} className="fs-field-helper">
                      {parameterTypeLabel(storedParameterType(field), t)}{" "}{t("react.registry.read_only", "(read-only)")}</output>
                  )}
                </div>
                {storedParameterType(field) === "string" ? (
                  <div className="fs-field">
                    <span className="fs-field-label">{t("component.json.format", "Format")}</span>
                    <DsSelect
                      ariaLabel={t("react.registry.parameter_format_for_value0", "Parameter format for {value0}", { value0: field.name })}
                      onChange={(value) => updateParameter(field.name, { format: value })}
                      options={localizedSTRING_FORMATS(t)}
                      value={field.format}
                    />
                  </div>
                ) : (
                  <p className="fs-field-helper">
                    {parameterTypeEditable(field)
                      ? t("react.registry.value0_values_do_not_need_a_format", "{value0} values do not need a format.", { value0: parameterTypeLabel(field.type, t) })
                      : t("react.registry.the_existing_type_and_definition_are_kept_unchanged", "The existing type and definition are kept unchanged.")}
                  </p>
                )}
                <div className="fs-field">
                  <DsCheckbox
                    checked={field.required}
                    onChange={(checked) => updateParameter(field.name, { required: checked })}
                  >
                    {t("project_edit.fields.required", "Required")}</DsCheckbox>
                </div>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <DsAlert title={t("react.registry.no_input_parameters_yet", "No input parameters yet")} tone="neutral">
            {t("react.registry.add_a_named_value_such_as_where_status_status_to_create_an_input_parameter", "Add a named value such as WHERE status = :status to create an input parameter.")}</DsAlert>
        )}
      </section>

      <section className="fs-panel-stack">
        <h3 className="fs-panel-title">{t("react.registry.output_columns", "Output columns")}</h3>
        <p className="fs-field-helper">
          {t("react.registry.select_the_dataset_columns_returned_by_this_query_their_types_come_from_the_connection_introspection", "Select the dataset columns returned by this query. Their types come from the connection introspection.")}</p>
        {outputFields.length ? (
          <>
            <div className="fs-surface-radius fs-surface-inset border border-subtle p-3 fs-footer-actions">
              <span className="fs-field-helper">
                {selectedOutputCount} {" "}{t("react.registry.of", "of")}{" "}{outputFields.length} {" "}{t("react.registry.columns_selected", "columns selected")}</span>
              <button
                className="fs-button"
                disabled={allOutputSelected}
                onClick={() => onOutputSchemaChange(outputSchemaFromFields(outputFields))}
                type="button"
              >
                {t("react.registry.select_all", "Select all")}</button>
              <button
                className="fs-button"
                disabled={!selectedOutputCount}
                onClick={() => onOutputSchemaChange({ type: "object", properties: {} })}
                type="button"
              >
                {t("react.registry.clear_selection", "Clear selection")}</button>
            </div>
            <div className="grid gap-2">
              {outputFields.map((field) => {
                const property = outputProperties[field.name];
                const propertyRecord =
                  property && typeof property === "object" && !Array.isArray(property)
                    ? (property as Record<string, unknown>)
                    : {};
                const selected = Object.hasOwn(outputProperties, field.name);
                const format =
                  typeof propertyRecord.format === "string" ? propertyRecord.format : field.format;
                return (
                  <div
                    className="fs-surface-radius fs-surface-inset border border-subtle p-3"
                    key={`${field.resourceId}.${field.name}`}
                  >
                    <div>
                      <DsCheckbox
                        checked={selected}
                        onChange={(checked) =>
                          onOutputSchemaChange(outputSchemaWithField(outputSchema, field, checked))
                        }
                      >
                        {`${field.resourceId}.${field.name}`}
                      </DsCheckbox>
                      <span className="fs-field-helper">{parameterTypeLabel(field.type, t)}</span>
                    </div>
                    {selected && field.type === "string" ? (
                      <div className="fs-field">
                        <span className="fs-field-label">{t("component.json.format", "Format")}</span>
                        <DsSelect
                          ariaLabel={t("react.registry.output_format_for_value0", "Output format for {value0}", { value0: field.name })}
                          onChange={(value) =>
                            onOutputSchemaChange(outputSchemaWithFormat(outputSchema, field.name, value))
                          }
                          options={localizedSTRING_FORMATS(t)}
                          value={format}
                        />
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </>
        ) : (
          <DsAlert title={t("react.registry.no_introspected_columns_yet", "No introspected columns yet")} tone="neutral">
            {t("react.registry.refresh_this_dataset_s_schema_before_choosing_output_columns", "Refresh this dataset's schema before choosing output columns.")}</DsAlert>
        )}
      </section>
    </div>
  );
}
