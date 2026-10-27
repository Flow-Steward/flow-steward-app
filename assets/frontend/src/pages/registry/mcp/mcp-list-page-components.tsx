import { useI18n } from "@/lib/i18n/useI18n";
import { type RefObject } from "react";

import { SensitiveInput } from "@/components/shared/design-system";

import { type KeyValueRow, keyValueRowProblem } from "./key-value-rows";

export function KeyValueRepeater({
  label,
  rows,
  onChange,
  reservedKeys,
  serverInvalid = false,
  sensitiveValues = false,
  wrapperRef,
}: Readonly<{
  label: string;
  rows: KeyValueRow[];
  onChange: (rows: KeyValueRow[]) => void;
  reservedKeys?: ReadonlySet<string>;
  serverInvalid?: boolean;
  sensitiveValues?: boolean;
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
            {sensitiveValues ? (
              <SensitiveInput
                aria-describedby={describedBy}
                aria-invalid={invalid}
                ariaLabel={t("react.registry.value0_value", "{value0} value", { value0: label })}
                className={`fs-input${invalid ? " fs-mcp-invalid-row" : ""}`}
                onChange={(event) => onChange(rows.map((item) => item.id === row.id ? { ...item, value: event.target.value } : item))}
                placeholder={t("project_edit.fields.value", "Value")}
                value={row.value}
              />
            ) : (
              <input
                aria-describedby={describedBy}
                aria-invalid={invalid}
                aria-label={t("react.registry.value0_value", "{value0} value", { value0: label })}
                className={`fs-input${invalid ? " fs-mcp-invalid-row" : ""}`}
                onChange={(event) => onChange(rows.map((item) => item.id === row.id ? { ...item, value: event.target.value } : item))}
                placeholder={t("project_edit.fields.value", "Value")}
                value={row.value}
              />
            )}
            <button className="fs-button secondary" onClick={() => onChange(rows.filter((item) => item.id !== row.id))} type="button">{t("extensions.repeater.remove", "Remove")}</button>
            {problem ? <span className="fs-field-error" id={errorId}>{problem}</span> : null}
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
