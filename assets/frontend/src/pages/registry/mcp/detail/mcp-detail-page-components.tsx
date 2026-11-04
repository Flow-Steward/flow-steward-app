import { useI18n } from "@/lib/i18n/useI18n";
import { type ChangeEvent, type RefObject } from "react";

import { SensitiveInput } from "@/components/shared/design-system";
import { StoredSecretSelect } from "@/pages/registry/secrets/stored-secret-select";

import { withHeaderSecretChoice, headerSecretHelper } from "../key-value-rows";
import { type KeyValueRow, keyValueRowProblem } from "./mcp-detail-page-helpers";

/** A key-value fact: the published field pair, label over value. The design
 *  system publishes no code block, so a technical value is ordinary text that
 *  only borrows the code face. */
export function Fact({
  code = false,
  full = false,
  helper,
  label,
  value,
}: Readonly<{
  code?: boolean;
  full?: boolean;
  helper?: string;
  label: string;
  value: string;
}>) {
  return (
    <div className={full ? "fs-field full" : "fs-field"}>
      <span className="fs-field-label">{label}</span>
      <span className={code ? "font-[var(--ff-code)] break-all fs-ink-primary" : "fs-ink-primary"}>
        {value}
      </span>
      {helper ? <span className="fs-field-helper">{helper}</span> : null}
    </div>
  );
}

export function KeyValueEditor({
  addLabel,
  keyAriaLabel,
  label,
  onChange,
  rows,
  reservedKeys,
  serverInvalid = false,
  sensitiveValues = false,
  storedSecrets = false,
  valueAriaLabel,
  wrapperRef,
}: Readonly<{
  addLabel: string;
  keyAriaLabel: string;
  label: string;
  onChange: (rows: KeyValueRow[]) => void;
  rows: KeyValueRow[];
  reservedKeys?: ReadonlySet<string>;
  serverInvalid?: boolean;
  sensitiveValues?: boolean;
  /** Every row sends a stored secret chosen below; the system adds "Bearer " for Authorization. */
  storedSecrets?: boolean;
  valueAriaLabel: string;
  wrapperRef: RefObject<HTMLFieldSetElement | null>;
}>) {
  const { t } = useI18n();
  return (
    <fieldset
      aria-label={label}
      className={`fs-field full m-0 min-w-0 border-0 p-0${serverInvalid ? " fs-field-error-state" : ""}`}
      ref={wrapperRef}
      tabIndex={serverInvalid ? -1 : undefined}
    >
      <legend className="fs-field-label">{label}</legend>
      {rows.map((row, index) => {
        const problem = keyValueRowProblem(rows, index, label, reservedKeys, t);
        const invalid = Boolean(problem) || serverInvalid;
        const errorId = `mcp-settings-${label.toLowerCase().replaceAll(" ", "-")}-${row.id}-error`;
        let describedBy: string | undefined;
        if (problem) {
          describedBy = errorId;
        } else if (serverInvalid) {
          describedBy = "mcp-settings-error";
        }
        const inputClass = `fs-input${invalid ? " fs-mcp-invalid-row" : ""}`;
        const valueProps = {
          "aria-describedby": describedBy,
          "aria-invalid": invalid,
          className: inputClass,
          onChange: (event: ChangeEvent<HTMLInputElement>) => onChange(
            rows.map((item) => item.id === row.id ? { ...item, value: event.target.value } : item),
          ),
          placeholder: t("project_edit.fields.value", "Value"),
          value: row.value,
        };
        const fieldLabel = row.key.trim() || label;
        let valueCell = sensitiveValues
          ? <SensitiveInput ariaLabel={valueAriaLabel} {...valueProps} />
          : <input aria-label={valueAriaLabel} {...valueProps} />;
        if (storedSecrets) {
          // The stored secret chosen below provides the value; the system adds "Bearer ".
          valueCell = (
            <input
              aria-describedby={describedBy}
              aria-label={valueAriaLabel}
              className={inputClass}
              disabled
              placeholder={t("react.registry.mcp_header_value_from_secret", "From the stored secret below")}
              value=""
            />
          );
        }
        return (
          <div className={`fs-form-grid fs-mcp-key-value-row${invalid ? " fs-mcp-invalid-row" : ""}`} key={row.id}>
            <input
              aria-describedby={describedBy}
              aria-invalid={invalid}
              aria-label={keyAriaLabel}
              className={inputClass}
              onChange={(event) => onChange(rows.map((item) => item.id === row.id ? { ...item, key: event.target.value } : item))}
              placeholder={t("react.registry.key", "Key")}
              value={row.key}
            />
            {valueCell}
            <button className="fs-button secondary" onClick={() => onChange(rows.filter((item) => item.id !== row.id))} type="button">{t("extensions.repeater.remove", "Remove")}</button>
            {problem ? <span className="fs-field-error" id={errorId}>{problem}</span> : null}
            {storedSecrets ? (
              <div className="col-span-full">
                <StoredSecretSelect
                  fieldLabel={fieldLabel}
                  helper={row.secretId ? headerSecretHelper(row, t) : undefined}
                  onChange={(secretId) => onChange(rows.map((item) => item.id === row.id ? withHeaderSecretChoice(item, secretId) : item))}
                  value={row.secretId ?? ""}
                />
              </div>
            ) : null}
          </div>
        );
      })}
      <button className="fs-button secondary" onClick={() => onChange([...rows, { id: Date.now(), key: "", value: "" }])} type="button">{addLabel}</button>
    </fieldset>
  );
}
