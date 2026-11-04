import { useI18n } from "@/lib/i18n/useI18n";
import { type RefObject } from "react";

import { SensitiveInput } from "@/components/shared/design-system";
import { StoredSecretSelect } from "@/pages/registry/secrets/stored-secret-select";

import { headerSecretHelper, type KeyValueRow, keyValueRowProblem, withHeaderSecretChoice } from "./key-value-rows";

/** The value cell of one row: the typed value, or a note that a stored secret provides it. */
function KeyValueValueCell({
  chosen,
  describedBy,
  invalid,
  label,
  onChange,
  row,
  sensitive,
}: Readonly<{
  chosen: boolean;
  describedBy: string | undefined;
  invalid: boolean;
  label: string;
  onChange: (patch: Partial<KeyValueRow>) => void;
  row: KeyValueRow;
  sensitive: boolean;
}>) {
  const { t } = useI18n();
  const className = `fs-input${invalid ? " fs-mcp-invalid-row" : ""}`;
  if (chosen) {
    // The cell keeps meaning "value": the stored secret chosen below provides it.
    return (
      <input
        aria-describedby={describedBy}
        aria-label={t("react.registry.value0_value", "{value0} value", { value0: label })}
        className={className}
        disabled
        placeholder={t("react.registry.mcp_header_value_from_secret", "From the stored secret below")}
        value=""
      />
    );
  }
  if (sensitive) {
    return (
      <SensitiveInput
        aria-describedby={describedBy}
        aria-invalid={invalid}
        ariaLabel={t("react.registry.value0_value", "{value0} value", { value0: label })}
        className={className}
        onChange={(event) => onChange({ value: event.target.value })}
        placeholder={t("project_edit.fields.value", "Value")}
        value={row.value}
      />
    );
  }
  return (
    <input
      aria-describedby={describedBy}
      aria-invalid={invalid}
      aria-label={t("react.registry.value0_value", "{value0} value", { value0: label })}
      className={className}
      onChange={(event) => onChange({ value: event.target.value })}
      placeholder={t("project_edit.fields.value", "Value")}
      value={row.value}
    />
  );
}

export function KeyValueRepeater({
  label,
  rows,
  onChange,
  reservedKeys,
  serverInvalid = false,
  sensitiveValues = false,
  storedSecrets = false,
  wrapperRef,
}: Readonly<{
  label: string;
  rows: KeyValueRow[];
  onChange: (rows: KeyValueRow[]) => void;
  reservedKeys?: ReadonlySet<string>;
  serverInvalid?: boolean;
  sensitiveValues?: boolean;
  /** Each row may send a stored secret from the catalog instead of a typed value. */
  storedSecrets?: boolean;
  wrapperRef: RefObject<HTMLDivElement | null>;
}>) {
  const { t } = useI18n();
  return (
    <div className="fs-field full" ref={wrapperRef}>
      <span className="fs-field-label">{label}</span>
      {rows.map((row, index) => {
        const problem = keyValueRowProblem(rows, index, label, reservedKeys, t);
        const invalid = Boolean(problem) || serverInvalid;
        const errorId = `mcp-create-${label.toLowerCase().replaceAll(" ", "-")}-${row.id}-error`;
        let describedBy: string | undefined;
        if (problem) {
          describedBy = errorId;
        } else if (serverInvalid) {
          describedBy = "mcp-create-error";
        }
        return (
          <div className={`fs-form-grid fs-mcp-key-value-row${invalid ? " fs-mcp-invalid-row" : ""}`} key={row.id}>
            <input
              aria-describedby={describedBy}
              aria-invalid={invalid}
              aria-label={t("react.registry.value0_key", "{value0} key", { value0: label })}
              className={`fs-input${invalid ? " fs-mcp-invalid-row" : ""}`}
              onChange={(event) => onChange(rows.map((item) => item.id === row.id ? { ...item, key: event.target.value } : item))}
              placeholder={t("react.registry.key", "Key")}
              value={row.key}
            />
            <KeyValueValueCell
              chosen={storedSecrets && Boolean(row.secretId)}
              describedBy={describedBy}
              invalid={invalid}
              label={label}
              onChange={(patch) => onChange(rows.map((item) => item.id === row.id ? { ...item, ...patch } : item))}
              row={row}
              sensitive={sensitiveValues}
            />
            <button className="fs-button secondary" onClick={() => onChange(rows.filter((item) => item.id !== row.id))} type="button">{t("extensions.repeater.remove", "Remove")}</button>
            {problem ? <span className="fs-field-error" id={errorId}>{problem}</span> : null}
            {storedSecrets ? (
              <div className="col-span-full">
                <StoredSecretSelect
                  fieldLabel={row.key.trim() || label}
                  helper={row.secretId ? headerSecretHelper(row, t) : undefined}
                  onChange={(secretId) => onChange(rows.map((item) => item.id === row.id ? withHeaderSecretChoice(item, secretId) : item))}
                  value={row.secretId ?? ""}
                />
              </div>
            ) : null}
          </div>
        );
      })}
      <button
        className="fs-button secondary"
        onClick={() => onChange([...rows, { id: Date.now(), key: "", value: "" }])}
        type="button"
      >
        {t("react.registry.mcp_final_add_row", "Add {label} row", { label: label.toLowerCase() })}</button>
    </div>
  );
}
