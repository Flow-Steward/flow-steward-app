import { useI18n } from "@/lib/i18n/useI18n";
import { type Dispatch, type SetStateAction, type SyntheticEvent } from "react";

import { ChevronRight } from "lucide-react";

import {
  DsCheckbox,
  DsSelect,
  MultiCombobox,
  SensitiveInput,
  SingleCombobox,
} from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import type { ChoiceOption } from "@/pages/settings/hub/choice-options";
import {
  type ControlSurfaceField,
  type FormResource,
  type FormValue,
  type FormValues,
  type OptionsByPath,
  fieldControlId,
  fieldOptions,
  fixedOption,
  resourceSaveLabel,
  visibleFormFields,
} from "@/pages/settings/hub/resource-admin-fields";

/** The host-rendered form of a settings record; its field rules live in resource-admin-fields. */

export function ResourceFieldControl({
  controlId,
  editingId,
  field,
  options,
  setFieldValue,
  value,
}: Readonly<{
  controlId: string;
  editingId: string;
  field: ControlSurfaceField;
  options: ChoiceOption[];
  setFieldValue: (value: FormValue) => void;
  value: FormValue;
}>) {
  const { t } = useI18n();
  // Untrimmed: trimming while typing would swallow every space between words.
  const textValue = typeof value === "string" ? value : "";
  if (field.type === "select") {
    return (
      <DsSelect
        ariaLabel={field.label}
        describedBy={field.help ? `${controlId}-help` : undefined}
        id={controlId}
        onChange={setFieldValue}
        options={options}
        required={field.required}
        value={textValue}
      />
    );
  }
  if (field.type === "reference") {
    return (
      <SingleCombobox
        ariaLabel={field.label}
        id={controlId}
        onChange={setFieldValue}
        options={options}
        placeholder={field.placeholder || t("react.settings.choose_option", "Choose…")}
        value={textValue}
      />
    );
  }
  if (field.type === "multi_choice") {
    return (
      <MultiCombobox
        ariaLabel={field.label}
        onChange={setFieldValue}
        options={options}
        placeholder={field.placeholder || t("react.settings.choose_options", "Choose one or more…")}
        required={field.required}
        selected={Array.isArray(value) ? value : []}
      />
    );
  }
  if (field.type === "secret") {
    return (
      <SensitiveInput
        ariaLabel={field.label}
        autoComplete="new-password"
        className="fs-input"
        id={controlId}
        name={field.key}
        onChange={(event) => setFieldValue(event.target.value)}
        placeholder={
          field.placeholder ||
          (editingId ? t("react.settings.secret_keep_blank", "Leave blank to keep the saved value") : "")
        }
        required={field.required && !editingId}
        value={textValue}
      />
    );
  }
  if (field.type === "textarea" || field.type === "json") {
    return (
      <textarea
        className={field.type === "json" ? "fs-textarea fs-code-value" : "fs-textarea"}
        id={controlId}
        name={field.key}
        onChange={(event) => setFieldValue(event.target.value)}
        placeholder={field.placeholder}
        required={field.required && !editingId}
        value={textValue}
      />
    );
  }
  return (
    <input
      className="fs-input"
      id={controlId}
      name={field.key}
      onChange={(event) => setFieldValue(event.target.value)}
      placeholder={field.placeholder}
      required={field.required}
      type={field.type === "number" ? "number" : "text"}
      value={textValue}
    />
  );
}

export function ResourceFormField({
  editingId,
  field,
  formValues,
  optionsByPath,
  resource,
  setFormValues,
}: Readonly<{
  editingId: string;
  field: ControlSurfaceField;
  formValues: FormValues;
  optionsByPath: OptionsByPath;
  resource: FormResource;
  setFormValues: Dispatch<SetStateAction<FormValues>>;
}>) {
  const controlId = fieldControlId(resource, field, editingId);
  const helpId = `${controlId}-help`;
  const setFieldValue = (value: FormValue) => setFormValues((current) => ({ ...current, [field.key]: value }));
  const help = field.help ? <span className="fs-field-helper" id={helpId}>{field.help}</span> : null;
  if (field.type === "checkbox") {
    // The checkbox carries its own label, the published control anatomy.
    return (
      <div className="fs-field">
        <DsCheckbox checked={Boolean(formValues[field.key])} name={field.key} onChange={setFieldValue}>
          {field.label}
        </DsCheckbox>
        {help}
      </div>
    );
  }
  const fixed = fixedOption(field, optionsByPath);
  return (
    <div className="fs-field">
      <label className={field.required && !fixed ? "fs-field-label fs-required-label" : "fs-field-label"} htmlFor={controlId}>
        {field.label}
        {field.required && !fixed ? <span aria-hidden="true" className="fs-required">*</span> : null}
      </label>
      {fixed ? (
        // One allowed value: nothing to choose, so it is shown, not offered.
        <input
          aria-describedby={field.help ? helpId : undefined}
          className="fs-input"
          id={controlId}
          name={field.key}
          readOnly
          value={fixed.label}
        />
      ) : (
        <ResourceFieldControl
          controlId={controlId}
          editingId={editingId}
          field={field}
          options={fieldOptions(field, optionsByPath)}
          setFieldValue={setFieldValue}
          value={formValues[field.key]}
        />
      )}
      {help}
    </div>
  );
}

export function ResourceForm({
  editingId,
  formValues,
  onCancel,
  onSubmit,
  optionsByPath,
  resource,
  saving,
  setFormValues,
}: Readonly<{
  editingId: string;
  formValues: FormValues;
  onCancel: () => void;
  onSubmit: (event: SyntheticEvent<HTMLFormElement>) => void;
  optionsByPath: OptionsByPath;
  resource: FormResource;
  saving: boolean;
  setFormValues: Dispatch<SetStateAction<FormValues>>;
}>) {
  const { t } = useI18n();
  const visibleFields = visibleFormFields(resource, formValues, editingId, optionsByPath);
  const renderField = (field: ControlSurfaceField) => (
    <ResourceFormField
      editingId={editingId}
      field={field}
      formValues={formValues}
      key={field.key}
      optionsByPath={optionsByPath}
      resource={resource}
      setFormValues={setFormValues}
    />
  );
  const basicFields = visibleFields.filter((field) => !field.advanced);
  const advancedFields = visibleFields.filter((field) => field.advanced);
  return (
    <form
      className="fs-surface-radius fs-surface-inset border border-subtle fs-panel-group p-4"
      noValidate
      onSubmit={onSubmit}
    >
      <div className="fs-form-grid">{basicFields.map(renderField)}</div>
      {advancedFields.length ? (
        <details className="fs-disclosure">
          <summary>
            <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
            {t("react.settings.advanced_settings", "Advanced settings")}
          </summary>
          <div className="fs-disclosure-body fs-form-grid">{advancedFields.map(renderField)}</div>
        </details>
      ) : null}
      <div className="fs-footer-actions">
        <Button busy={saving} busyLabel={t("react.settings.saving", "Saving…")} type="submit" variant="primary">
          {resourceSaveLabel(Boolean(editingId), t)}
        </Button>
        <button className="fs-button" onClick={onCancel} type="button">
          {t("common.cancel", "Cancel")} </button>
      </div>
    </form>
  );
}
