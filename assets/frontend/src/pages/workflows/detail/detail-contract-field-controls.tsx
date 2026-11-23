import type { ReactNode } from "react";

import { RequiredLabel } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";
import type { SelectOption } from "@/pages/workflows/detail/detail-interface";
import { textOrEmpty, type WorkflowInterfaceFieldDraft } from "@/pages/workflows/detail/detail-helpers";
import { FieldShapeControls } from "./detail-interface-shape-controls";

/**
 * The controls of one contract field: name, type, label, required, options,
 * declared shape and description. The workflow contract editor and the review
 * fields of an Approval step (ADR 0075) both describe a field with them, so a
 * field reads and validates the same in both places. `children` sits before the
 * description: the contract's default, or a review field's editability and source.
 */
export function ContractFieldControls({
  row,
  updateRow,
  controlId,
  controlName,
  nameId,
  nameMissing,
  typeOptions,
  direction,
  namePlaceholder = "weather_summary",
  labelPlaceholder,
  descriptionPlaceholder,
  children,
}: Readonly<{
  row: WorkflowInterfaceFieldDraft;
  updateRow: (patch: Partial<WorkflowInterfaceFieldDraft>) => void;
  /** The base of the controls' ids: `<controlId>-type`, `-label`, `-options`… */
  controlId: string;
  /** The base of the controls' names: `<controlName>_type`, `_label`… */
  controlName: string;
  nameId: string;
  nameMissing: boolean;
  typeOptions: SelectOption[];
  direction: "input" | "output";
  namePlaceholder?: string;
  labelPlaceholder?: string;
  descriptionPlaceholder?: string;
  children?: ReactNode;
}>) {
  const { t } = useI18n();
  const nameErrorId = `${nameId}-error`;
  return (
    <>
      <div className="grid gap-2 sm:grid-cols-2">
        <div>
          <label className="block text-xs text-[var(--fs-text-primary)]">
            <RequiredLabel>{t("settings.mcp.fields.name", "Name")}</RequiredLabel>
            <input
              aria-label={t("settings.mcp.fields.name", "Name")}
              aria-describedby={nameMissing ? nameErrorId : undefined}
              aria-invalid={nameMissing ? true : undefined}
              aria-required="true"
              className="fs-input mt-1 w-full font-mono"
              id={nameId}
              name={`${controlName}_name`}
              onChange={(event) => updateRow({ name: event.target.value })}
              onBlur={() => { if (!textOrEmpty(row.name)) updateRow({ nameTouched: true }); }}
              placeholder={namePlaceholder}
              required
              value={row.name}
            />
          </label>
          {nameMissing ? <span className="mt-1 block text-xs text-[var(--fs-status-danger)]" id={nameErrorId}>{t("react.registry.name_is_required", "Name is required.")}</span> : null}
        </div>
        <label className="block text-xs text-[var(--fs-text-primary)]">
          <span>{t("trash.columns.type", "Type")}</span>
          <select
            className="fs-input mt-1 w-full"
            id={`${controlId}-type`}
            name={`${controlName}_type`}
            onChange={(event) => updateRow({ type: event.target.value })}
            value={row.type}
          >
            {typeOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="grid items-end gap-2 sm:grid-cols-2">
        <label className="block text-xs text-[var(--fs-text-primary)]">
          <span>{t("react.workflows.label", "Label")}</span>
          <input
            className="fs-input mt-1 w-full"
            id={`${controlId}-label`}
            name={`${controlName}_label`}
            onChange={(event) => updateRow({ label: event.target.value })}
            placeholder={labelPlaceholder ?? t("react.workflows.weather_summary", "Weather summary")}
            value={row.label}
          />
        </label>
        <label className="flex min-h-8 items-center gap-2 text-xs text-[var(--fs-text-primary)]">
          <input
            checked={row.required}
            className="h-4 w-4 accent-[var(--fs-action-primary)]"
            id={`${controlId}-required`}
            name={`${controlName}_required`}
            onChange={(event) => updateRow({ required: event.target.checked })}
            type="checkbox"
          />
          <span>{t("project_edit.fields.required", "Required")}</span>
        </label>
      </div>
      {row.type === "select" ? (
        <div>
          <label className="block text-xs text-[var(--fs-text-primary)]">
            <span>{t("react.workflows.options", "Options")}</span>
            <input
              aria-describedby={`${controlId}-options-help`}
              className="fs-input mt-1 w-full"
              id={`${controlId}-options`}
              name={`${controlName}_options`}
              onChange={(event) => updateRow({ options: event.target.value })}
              placeholder="high, normal, low"
              value={row.options}
            />
          </label>
          <p className="fs-field-helper mt-1" id={`${controlId}-options-help`}>{t("react.workflows.contract_options_help", "Separate the choices with commas.")}</p>
        </div>
      ) : null}
      <FieldShapeControls
        controlId={controlId}
        controlName={controlName}
        direction={direction}
        row={row}
        updateRow={updateRow}
      />
      {children}
      <label className="block text-xs text-[var(--fs-text-primary)]">
        <span>{t("react.workflows.field_description", "Field description")}</span>
        <input
          className="fs-input mt-1 w-full"
          id={`${controlId}-description`}
          name={`${controlName}_description`}
          onChange={(event) => updateRow({ description: event.target.value })}
          placeholder={descriptionPlaceholder ?? t("react.workflows.shown_to_workflow_authors_and_launchers", "Shown to workflow authors and launchers")}
          value={row.description}
        />
      </label>
    </>
  );
}
