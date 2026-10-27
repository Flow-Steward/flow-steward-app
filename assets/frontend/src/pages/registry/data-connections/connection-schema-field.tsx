import { useI18n } from "@/lib/i18n/useI18n";
import { DsSelect, RequiredLabel, SensitiveInput } from "@/components/shared/design-system";
import { type ConnectionFormField } from "./data-connection-helpers";
import { type ConnectionDraft, connectionFieldInputType } from "./connection-page-helpers";

type ConnectionSchemaFieldProps = {
  field: ConnectionFormField;
  draft: ConnectionDraft;
  updateDraft: (patch: Partial<ConnectionDraft>) => void;
};

export function ConnectionSchemaField({ field, draft, updateDraft }: ConnectionSchemaFieldProps) {
  const { t } = useI18n();
    const values =
      field.storage === "connection" ? draft.connection_values : draft.secret_values;
    const value = values[field.key] || "";
    const changeValue = (next: string) =>
      updateDraft(
        field.storage === "connection"
          ? {
              connection_values: { ...draft.connection_values, [field.key]: next },
              touched_connection_fields: Array.from(new Set([
                ...(draft.touched_connection_fields || []), field.key,
              ])),
            }
          : { secret_values: { ...draft.secret_values, [field.key]: next } },
      );
    const storageNote =
      field.storage === "secret"
        ? draft.connection_id
          ? t("react.registry.stored_securely_in_secret_refs_leave_blank_to_keep_the_existing_stored_secret", "Stored securely in secret refs. Leave blank to keep the existing stored secret.")
          : t("react.registry.stored_securely_in_secret_refs", "Stored securely in secret refs.")
        : "";
    const helper = [field.description, storageNote].filter(Boolean).join(" ");
    // A multi-line control spans the grid: beside a single-line field it breaks
    // the row's baseline and leaves a ragged column.
    const multiline = field.type === "object" || field.type === "array";
    const labelNode = field.required ? (
      <RequiredLabel>{field.label}</RequiredLabel>
    ) : (
      <span className="fs-field-label">{field.label}</span>
    );
    const control = field.enum_values.length ? (
      <DsSelect
        ariaLabel={field.label}
        onChange={changeValue}
        options={field.enum_values.map((option) => ({ value: option, label: option }))}
        placeholder={t("react.registry.select_a_value", "Select a value")}
        required={field.required}
        value={value}
      />
    ) : field.storage === "secret" ? (
      <SensitiveInput
        ariaLabel={field.label}
        className="fs-input"
        onChange={(event) => changeValue(event.target.value)}
        value={value}
      />
    ) : field.type === "object" || field.type === "array" ? (
      // These values are JSON, and `valueForSchemaField` parses them as such.
      // A single-line input cannot hold them legibly.
      <textarea
        className="fs-textarea"
        onChange={(event) => changeValue(event.target.value)}
        value={value}
      />
    ) : (
      <input
        className="fs-input"
        max={field.maximum}
        min={field.minimum}
        onChange={(event) => changeValue(event.target.value)}
        type={connectionFieldInputType(field)}
        value={value}
      />
    );
    // A DsSelect and a SensitiveInput carry their own accessible name, so they
    // sit beside the label rather than inside it.
    const wrapped = field.enum_values.length || field.storage === "secret";
    const className = `fs-field${multiline ? " full" : ""}`;
    return wrapped ? (
      <div className={className} key={`${field.storage}:${field.key}`}>
        {labelNode}
        {control}
        {helper ? <span className="fs-field-helper">{helper}</span> : null}
      </div>
    ) : (
      <label className={className} key={`${field.storage}:${field.key}`}>
        {labelNode}
        {control}
        {helper ? <span className="fs-field-helper">{helper}</span> : null}
      </label>
    );
  }
