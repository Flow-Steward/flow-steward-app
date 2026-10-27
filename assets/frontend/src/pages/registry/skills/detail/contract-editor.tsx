import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable react-refresh/only-export-components */
import { useEffect, useRef, useState } from "react";

import { DsCheckbox, DsSelect } from "@/components/shared/design-system";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type ContractDraftItem = {
  contract_id: string;
  description: string;
  name: string;
  required: boolean;
  type: string;
};

export type CatalogFieldOption = {
  contractId: string;
  description: string;
  fieldKey: string;
  label: string;
  valueType: string;
};

export type DataContractOption = {
  id: string;
  label: string;
};

type SearchableOption = {
  description?: string;
  label: string;
  value: string;
};

const CONTRACT_VALUE_TYPES = ["text", "json", "list", "number", "boolean"] as const;
const CONTRACT_VALUE_TYPE_LABEL_KEYS = {
  text: "react.registry.text",
  json: "react.registry.contract_type_json",
  list: "react.workflows.list",
  number: "react.workflows.number",
  boolean: "react.workflows.boolean",
} as const;
const STRUCTURED_CONTRACT_VALUE_TYPES = new Set(["json", "list"]);

/**
 * The published inset surface plus layout utilities only. Column spans are the
 * overlap guard for the row, and a test pins two of them.
 */
const CONTRACT_EDITOR_ROW_CLASS =
  "fs-surface-radius fs-surface-inset border border-subtle grid gap-3 p-3 md:grid-cols-12 md:items-start";

export function emptyContractDraftItem(required: boolean): ContractDraftItem {
  return {
    contract_id: "",
    description: "",
    name: "",
    required,
    type: "text",
  };
}

export function isStructuredContractValueType(valueType: string): boolean {
  return STRUCTURED_CONTRACT_VALUE_TYPES.has(firstStringFromUnknownValue([valueType, ""]).trim().toLowerCase());
}

/**
 * A save result is either a confirmation or an error, and those are different
 * roles rather than two colours of the same one.
 */
function statusClass(status: string) {
  return /saved/i.test(status) ? "fs-field-helper" : "fs-ink-danger";
}

/**
 * Kept local on purpose: `SingleCombobox` opens only on focus and returns focus
 * to its own input, so a second mouse click never reopens it — a regression in an
 * editor where selection happens repeatedly. Unifying the two is a separate task.
 * The markup here is the published combo contract.
 */
function SearchablePicker({
  ariaLabel,
  disabled = false,
  emptyLabel,
  emptySearchMessage,
  onSelect,
  options,
  placeholder,
  searchPlaceholder,
  value,
}: Readonly<{
  ariaLabel: string;
  disabled?: boolean;
  emptyLabel: string;
  emptySearchMessage: string;
  onSelect: (value: string) => void;
  options: SearchableOption[];
  placeholder: string;
  searchPlaceholder: string;
  value: string;
}>) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) {
      return undefined;
    }

    function handlePointerDown(event: MouseEvent) {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }
      if (rootRef.current?.contains(target)) {
        return;
      }
      setQuery("");
      setOpen(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setQuery("");
        setOpen(false);
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  const selected = options.find((option) => option.value === value);
  const filteredOptions = options.filter((option) => {
    const needle = query.trim().toLowerCase();
    if (!needle) {
      return true;
    }
    return [option.label, option.value, option.description || ""].map((value) => stringFromUnknownValue(value)).join(" ")
      .toLowerCase()
      .includes(needle);
  });

  return (
    <div className="fs-combo" ref={rootRef}>
      <button
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
        className="fs-combo-field"
        disabled={disabled}
        onClick={() =>
          setOpen((current) => {
            const next = !current;
            if (!next) {
              setQuery("");
            }
            return next;
          })
        }
        type="button"
      >
        <span className="fs-select-value">{selected?.label || value || emptyLabel}</span>
        <svg aria-hidden="true" className="fs-select-chevron" viewBox="0 0 24 24">
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {open ? (
        <div aria-label={t("react.registry.value0_options", "{value0} options", { value0: ariaLabel })} className="fs-select-menu" role="listbox">
          <input
            aria-label={t("react.registry.value0_search", "{value0} Search", { value0: ariaLabel })}
            className="fs-input"
            onChange={(event) => setQuery(event.target.value)}
            placeholder={searchPlaceholder}
            type="text"
            value={query}
          />
          <button
            aria-selected={!value}
            className="fs-select-option"
            onClick={() => {
              onSelect("");
              setQuery("");
              setOpen(false);
            }}
            role="option"
            title={placeholder}
            type="button"
          >
            <span className="truncate">{emptyLabel}</span>
            {!value ? (
              <svg aria-hidden="true" className="fs-select-check" viewBox="0 0 24 24">
                <path d="M20 6 9 17l-5-5" />
              </svg>
            ) : null}
          </button>
          {filteredOptions.length ? (
            filteredOptions.map((option) => (
              <button
                aria-selected={option.value === value}
                className="fs-select-option"
                key={option.value}
                onClick={() => {
                  onSelect(option.value);
                  setQuery("");
                  setOpen(false);
                }}
                role="option"
                title={option.description || undefined}
                type="button"
              >
                {/* One line, because that is what the published option is: 32px
                    tall, `border: 0`, `padding: 0 8px`. Stacking three facts in
                    it produced a menu with no separation between entries — the
                    hairline the old markup drew cannot be restored with a
                    utility, since `.fs-select-option { border: 0 }` wins on
                    source order. The identifier disambiguates inline; the
                    description stays searchable and moves to the title. */}
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className="truncate">{option.label}</span>
                  <span className="fs-field-helper truncate">{option.value}</span>
                </span>
                {option.value === value ? (
                  <svg aria-hidden="true" className="fs-select-check" viewBox="0 0 24 24">
                    <path d="M20 6 9 17l-5-5" />
                  </svg>
                ) : null}
              </button>
            ))
          ) : (
            <span className="fs-combo-empty">{emptySearchMessage}</span>
          )}
        </div>
      ) : null}
    </div>
  );
}

export function ContractEditorCard({
  addLabel,
  contractOptions,
  emptyMessage,
  fieldOptions,
  items,
  onAdd,
  onChange,
  onRemove,
  onSave,
  requiredByDefault,
  saveLabel,
  saveStatus,
  saving,
  title,
}: Readonly<{
  addLabel: string;
  contractOptions: DataContractOption[];
  emptyMessage: string;
  fieldOptions: CatalogFieldOption[];
  items: ContractDraftItem[];
  onAdd: () => void;
  onChange: (index: number, next: Partial<ContractDraftItem>) => void;
  onRemove: (index: number) => void;
  onSave: () => void;
  requiredByDefault: boolean;
  saveLabel: string;
  saveStatus: string;
  saving: boolean;
  title: string;
}>) {
  const { t } = useI18n();
  return (
    <div className="fs-panel-group">
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-panel-title">{title}</h2>
          <p>{t("react.registry.link_canonical_fields_when_possible_to_keep_workflow_contracts_aligned", "Link canonical fields when possible to keep workflow contracts aligned.")}</p>
        </div>
        <button className="fs-button" onClick={onAdd} type="button">
          {addLabel}
        </button>
      </div>
      {items.map((item, index) => (
        <div className={CONTRACT_EDITOR_ROW_CLASS} key={`${title}:${index}:${item.name || "new"}`}>
          {(() => {
            const selectedField = fieldOptions.find((option) => option.fieldKey === item.name);
            const catalogFieldValue = selectedField ? selectedField.fieldKey : "";
            const structuredType = isStructuredContractValueType(item.type);

            return (
              <>
                <div
                  className="fs-field md:col-span-3"
                  data-testid="skill-contract-catalog-field"
                >
                  <span className="fs-field-label">{t("react.registry.catalog_field", "Catalog Field")}</span>
                  <SearchablePicker
                    ariaLabel={t("react.registry.catalog_field", "Catalog Field")}
                    emptyLabel={t("registry.skill_detail.io.custom_field", "Custom field...")}
                    emptySearchMessage={t("react.registry.no_catalog_fields_match_search", "No catalog fields match your search.")}
                    onSelect={(selectedValue) => {
                      if (!selectedValue) {
                        onChange(index, { contract_id: "", name: "", type: item.type });
                        return;
                      }
                      const selected = fieldOptions.find(
                        (option) => option.fieldKey === selectedValue,
                      );
                      if (!selected) {
                        return;
                      }
                      onChange(index, {
                        contract_id: selected.contractId,
                        description: selected.description,
                        name: selected.fieldKey,
                        type: selected.valueType,
                      });
                    }}
                    options={fieldOptions.map((option) => ({
                      description: option.description,
                      label: option.label,
                      value: option.fieldKey,
                    }))}
                    placeholder={t("react.registry.catalog_field_variant_b749eaf0", "Catalog field")}
                    searchPlaceholder={t("react.registry.search_catalog_fields", "Search catalog fields")}
                    value={catalogFieldValue}
                  />
                </div>
                <div className="fs-field md:col-span-4">
                  <label className="fs-field-label" htmlFor={`${title}-name-${index}`}>
                    {t("settings.mcp.fields.name", "Name")}</label>
                  <input
                    className="fs-input"
                    id={`${title}-name-${index}`}
                    onChange={(event) => onChange(index, { name: event.target.value })}
                    placeholder={t("settings.mcp.fields.name", "Name")}
                    type="text"
                    value={item.name}
                  />
                </div>
                <div className="fs-field md:col-span-2">
                  <span className="fs-field-label">{t("trash.columns.type", "Type")}</span>
                  <DsSelect
                    ariaLabel={t("trash.columns.type", "Type")}
                    onChange={(nextType) => {
                      onChange(index, {
                        contract_id: isStructuredContractValueType(nextType)
                          ? item.contract_id
                          : "",
                        type: nextType,
                      });
                    }}
                    options={CONTRACT_VALUE_TYPES.map((valueType) => ({
                      label: t(CONTRACT_VALUE_TYPE_LABEL_KEYS[valueType], valueType),
                      value: valueType,
                    }))}
                    value={item.type}
                  />
                </div>
                <div className="fs-field md:col-span-3">
                  <span className="fs-field-label">{t("trash.type.registry_data_contract", "Data Contract")}</span>
                  <SearchablePicker
                    ariaLabel={t("trash.type.registry_data_contract", "Data Contract")}
                    disabled={!structuredType}
                    emptyLabel={structuredType ? t("registry.skill_detail.io.no_contract", "No contract") : t("react.registry.only_for_json_list", "Only for JSON/list")}
                    emptySearchMessage={t("react.registry.no_data_contracts_match_search", "No data contracts match your search.")}
                    onSelect={(selectedValue) => onChange(index, { contract_id: selectedValue })}
                    options={contractOptions.map((option) => ({
                      label: option.label,
                      value: option.id,
                    }))}
                    placeholder={t("react.registry.data_contract", "Data contract")}
                    searchPlaceholder={t("react.registry.search_data_contracts", "Search data contracts")}
                    value={structuredType ? item.contract_id : ""}
                  />
                </div>
                <div
                  className="fs-field md:col-span-9"
                  data-testid="skill-contract-description-field"
                >
                  <label className="fs-field-label" htmlFor={`${title}-description-${index}`}>
                    {t("registry.workflows.simulation.description", "Description")}</label>
                  <input
                    className="fs-input"
                    id={`${title}-description-${index}`}
                    onChange={(event) => onChange(index, { description: event.target.value })}
                    placeholder={t("registry.workflows.simulation.description", "Description")}
                    type="text"
                    value={item.description}
                  />
                </div>
                <div className="flex items-center justify-between gap-3 border-t border-subtle pt-2 md:col-span-3 md:self-end md:border-t-0 md:pt-0">
                  <DsCheckbox
                    checked={item.required}
                    onChange={(checked) => onChange(index, { required: checked })}
                  >
                    {t("react.registry.req", "Req")}</DsCheckbox>
                  <button
                    className="fs-ink-danger"
                    onClick={() => onRemove(index)}
                    type="button"
                  >
                    {t("extensions.repeater.remove", "Remove")}</button>
                </div>
              </>
            );
          })()}
        </div>
      ))}
      {!items.length ? <div className="fs-table-state">{emptyMessage}</div> : null}
      <div className="flex flex-wrap items-center gap-2">
        <button className="fs-button primary" disabled={saving} onClick={onSave} type="button">
          {saveLabel}
        </button>
        {saveStatus ? <span className={statusClass(saveStatus)}>{saveStatus}</span> : null}
        {!items.length && requiredByDefault ? (
          <span className="fs-field-helper">{t("react.registry.outputs_default_to_required", "Outputs default to required.")}</span>
        ) : null}
      </div>
    </div>
  );
}
