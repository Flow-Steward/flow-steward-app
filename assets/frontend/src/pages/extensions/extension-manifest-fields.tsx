import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { fieldIdentity, sourceDeclaration } from "@/pages/extensions/extension-locale";
import { ManifestTagPicker } from "@/pages/extensions/extension-manifest-tag-picker";
import { ManifestRepeaterField } from "@/pages/extensions/extension-manifest-repeater-field";
import { fieldOptions, objectOrEmpty, stringFrom } from "@/pages/extensions/extension-manifest-utils";

import { DsCheckboxMark, DsFormSelect } from "@/components/shared/design-system";
import "@/pages/extensions/extension-manifest.css";

/**
 * The DOM id of one manifest field's control.
 *
 * Exported so a failed save can move focus to the field it named, instead of
 * leaving the user to find it from a message at the top of the form.
 */
/** The value path a manifest field reads and writes. */
export function manifestFieldName(field: Record<string, unknown>): string {
  const source = sourceDeclaration(field);
  return stringFrom(
    source.path || source.name || source.field_id || source.id || source.label,
  );
}

export function manifestFieldControlId(componentId: string, fieldName: string): string {
  return `extension-field-${controlIdSegment(componentId)}-${controlIdSegment(fieldName)}`;
}

function controlIdSegment(value: string): string {
  return Array.from(value)
    .map((character) =>
      /^[A-Za-z0-9_-]$/.test(character)
        ? character
        : `~${character.codePointAt(0)?.toString(16)}~`,
    )
    .join("");
}

type ManifestControlProps = {
  autoComplete: string;
  controlId: string;
  currentValue: unknown;
  describedBy: string | undefined;
  field: Record<string, unknown>;
  fieldName: string;
  fieldProps: Record<string, unknown>;
  isInvalid: boolean;
  isRequired: boolean;
  label: string;
  numericMax: unknown;
  numericMin: unknown;
  numericStep: unknown;
  onFieldChange: (fieldName: string, value: unknown) => void;
};

function renderManifestSelectControl(props: Readonly<ManifestControlProps>) {
  const options = fieldOptions(props.field);
  const current = stringFrom(props.currentValue);
  // A native select with no matching option shows, and submits, the first one.
  // The hidden input keeps that contract so FormData reads what the user sees.
  const value = options.some((option) => option.value === current)
    ? current
    : options[0]?.value ?? "";
  return (
    <DsFormSelect
      ariaLabel={props.label}
      describedBy={props.describedBy}
      id={props.controlId}
      invalid={props.isInvalid}
      name={props.fieldName}
      onChange={(next) => props.onFieldChange(props.fieldName, next)}
      options={options}
      required={props.isRequired}
      value={value}
    />
  );
}

function renderManifestBooleanControl(props: Readonly<ManifestControlProps>) {
  // The checkbox carries the field label itself, so a boolean reads as one
  // statement rather than a heading over a lone "Enabled".
  return (
    <span className="fs-required-label">
      <label className="fs-checkbox-label">
        <input
          aria-describedby={props.describedBy}
          aria-invalid={props.isInvalid || undefined}
          aria-required={props.isRequired}
          checked={props.currentValue === true || props.currentValue === "true"}
          id={props.controlId}
          name={props.fieldName}
          onChange={(event) =>
            props.onFieldChange(props.fieldName, event.target.checked)
          }
          required={props.isRequired}
          type="checkbox"
          value="true"
        />
        <DsCheckboxMark />
        <span>{props.label}</span>
      </label>
      {props.isRequired ? <span aria-hidden="true" className="fs-required">*</span> : null}
    </span>
  );
}

function renderManifestTextareaControl(props: Readonly<ManifestControlProps>) {
  return (
    <textarea
      aria-describedby={props.describedBy}
      aria-invalid={props.isInvalid || undefined}
      aria-required={props.isRequired}
      autoComplete={props.autoComplete || undefined}
      className={props.field.secret ? "fs-textarea fs-manifest-secret-text" : "fs-textarea"}
      id={props.controlId}
      name={props.fieldName}
      onChange={(event) =>
        props.onFieldChange(props.fieldName, event.target.value)
      }
      placeholder={stringFrom(props.field.placeholder)}
      required={props.isRequired}
      rows={Number(props.fieldProps.rows || 4)}
      spellCheck={props.field.secret ? false : undefined}
      value={stringFrom(props.currentValue)}
    />
  );
}

function renderManifestInputControl({
  widget,
  ...props
}: Readonly<ManifestControlProps & { widget: string }>) {
  const inputType =
    { secret: "password", integer: "number" }[widget] || "text"; // pragma: allowlist secret
  return (
    <input
      aria-describedby={props.describedBy}
      aria-invalid={props.isInvalid || undefined}
      aria-required={props.isRequired}
      autoComplete={props.autoComplete || undefined}
      className="fs-input"
      id={props.controlId}
      max={
        props.numericMax === undefined ? undefined : Number(props.numericMax)
      }
      min={
        props.numericMin === undefined ? undefined : Number(props.numericMin)
      }
      name={props.fieldName}
      onChange={(event) =>
        props.onFieldChange(props.fieldName, event.target.value)
      }
      placeholder={stringFrom(props.field.placeholder)}
      required={props.isRequired}
      step={
        props.numericStep === undefined
          ? undefined
          : Number(props.numericStep)
      }
      type={inputType}
      value={stringFrom(props.currentValue)}
    />
  );
}

function renderManifestFieldControl({
  widget,
  ...props
}: Readonly<ManifestControlProps & { widget: string }>) {
  if (widget === "select") return renderManifestSelectControl(props);
  if (widget === "boolean") return renderManifestBooleanControl(props);
  if (widget === "textarea") return renderManifestTextareaControl(props);
  return renderManifestInputControl({ ...props, widget });
}

export function renderManifestField({
  componentId,
  field,
  values,
  onFieldChange,
  invalidFieldPath = "",
  invalidMessage = "",
  savedSecretPaths = new Set<string>(),
  t = fallbackContext.t,
}: Readonly<{
  componentId: string;
  field: Record<string, unknown>;
  values: Record<string, unknown>;
  onFieldChange: (fieldName: string, value: unknown) => void;
  invalidFieldPath?: string;
  invalidMessage?: string;
  savedSecretPaths?: ReadonlySet<string>;
  t?: I18nContextValue["t"];
}>) {
  const fieldId = fieldIdentity(field);
  const fieldName = manifestFieldName(field) || fieldId;
  const label = stringFrom(field.label || fieldId, t("react.extensions.field", "Field"));
  const widget = stringFrom(field.widget || field.type || "text");
  const currentValue = values[fieldName];
  const controlId = manifestFieldControlId(componentId, fieldName);
  // A secret the connection already holds is not something the user has to
  // supply again. Marking it required implied the stored value had been lost.
  const hasSavedSecret = savedSecretPaths.has(fieldName);
  const isRequired = field.required === true && !hasSavedSecret;
  const isInvalid = Boolean(fieldName) && fieldName === invalidFieldPath;
  const errorId = `${controlId}-error`;
  const helpId = `${controlId}-help`;
  const help = hasSavedSecret
    ? stringFrom(
        field.saved_secret_help,
        t("react.extensions.saved_leave_blank_to_keep_the_stored_value_or_type_a_new_one_to_replace_it", "Saved. Leave blank to keep the stored value, or type a new one to replace it."),
      )
    : stringFrom(field.description || field.help);
  const fieldProps = objectOrEmpty(field.props);
  const autoComplete = stringFrom(
    field.autocomplete || field.autoComplete || (widget === "secret" ? "new-password" : ""),
  );
  const numericMin = field.minimum ?? field.min ?? fieldProps.minimum ?? fieldProps.min;
  const numericMax = field.maximum ?? field.max ?? fieldProps.maximum ?? fieldProps.max;
  const numericStep = field.step ?? fieldProps.step;
  // Hint text that is never announced is not a hint, so both it and the error
  // are attached to the control rather than left floating beside it.
  const describedBy =
    [isInvalid ? errorId : "", help ? helpId : ""].filter(Boolean).join(" ") || undefined;

  if (widget === "repeater") {
    return (
      <ManifestRepeaterField
        componentId={componentId}
        field={field}
        key={fieldId || label}
        onChange={onFieldChange}
        value={currentValue}
      />
    );
  }

  const control = widget === "tag_picker" ? (
    <ManifestTagPicker
      controlId={controlId}
      currentValue={currentValue}
      describedBy={describedBy}
      fieldName={fieldName}
      isInvalid={isInvalid}
      isRequired={isRequired}
      key={stringFrom(values.connection_id)}
      onFieldChange={onFieldChange}
      t={t}
    />
  ) : renderManifestFieldControl({
    autoComplete,
    controlId,
    currentValue,
    describedBy,
    field,
    fieldName,
    fieldProps,
    isInvalid,
    isRequired,
    label,
    numericMax,
    numericMin,
    numericStep,
    onFieldChange,
    widget,
  });

  return (
    <div className="fs-field" key={fieldId || label}>
      {widget === "boolean" ? null : (
        // The mark sits beside the label, not in it, so the label's text stays
        // the field's name exactly.
        <span className="fs-required-label">
          <label className="fs-field-label" htmlFor={controlId}>{label}</label>
          {isRequired ? <span aria-hidden="true" className="fs-required">*</span> : null}
        </span>
      )}
      {control}
      {isInvalid ? (
        <span className="fs-field-helper error" id={errorId}>
          {invalidMessage}
        </span>
      ) : null}
      {help ? (
        <span className="fs-field-helper" id={helpId}>
          {help}
        </span>
      ) : null}
    </div>
  );
}
