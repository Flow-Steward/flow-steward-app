import { fieldIdentity } from "@/pages/extensions/extension-locale";
import { useId, useMemo, useRef, useState } from "react";

import { DsAlert, DsCheckboxMark, DsFormSelect } from "@/components/shared/design-system";
import { useI18n } from "@/lib/i18n/useI18n";

import {
  booleanFrom,
  cloneWithPathValue,
  fieldOptions,
  listFromUnknown,
  objectOrEmpty,
  stringFrom,
  valueAtPath,
} from "@/pages/extensions/extension-manifest-utils";

type ManifestRepeaterFieldProps = {
  componentId: string;
  field: Record<string, unknown>;
  onChange: (fieldName: string, value: unknown) => void;
  value: unknown;
};

function displayRepeaterValue(
  value: unknown,
  t: (key: string, defaultText?: string) => string,
): string {
  if (typeof value === "boolean") {
    return value ? t("extensions.repeater.yes", "Yes") : t("extensions.repeater.no", "No");
  }
  return stringFrom(value, "—");
}

export function ManifestRepeaterField({
  componentId,
  field,
  onChange,
  value,
}: Readonly<ManifestRepeaterFieldProps>) {
  // Platform controls follow the interface language; labels come from the
  // extension's own dictionaries and arrive here already translated.
  const { t } = useI18n();
  const fieldId = fieldIdentity(field);
  const fieldName = stringFrom(field.path || field.name || fieldId);
  const label = stringFrom(field.label || fieldId, t("react.extensions.field", "Field"));
  const help = stringFrom(field.description || field.help);
  const itemFields = listFromUnknown(objectOrEmpty(field.props).fields).filter((item) => {
    const widget = stringFrom(item.widget || item.type || "text");
    return ["text", "boolean", "select"].includes(widget);
  });
  const items = useMemo(
    () =>
      Array.isArray(value)
        ? value.filter(
            (item): item is Record<string, unknown> =>
              Boolean(item) && typeof item === "object" && !Array.isArray(item),
          )
        : [],
    [value],
  );
  const [draftRow, setDraftRow] = useState<Record<string, unknown> | null>(null);
  const [draftError, setDraftError] = useState("");
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const idPrefix = useId();
  const nextRowKey = useRef(0);
  const [itemKeys, setItemKeys] = useState<string[]>(() =>
    items.map((_, index) => `${componentId}:${fieldId}:initial:${index + 1}`),
  );

  function createRowKey() {
    nextRowKey.current += 1;
    return `${componentId}:${fieldId}:row:${nextRowKey.current}`;
  }

  function updateDraftValue(rowField: Record<string, unknown>, nextValue: unknown) {
    const rowFieldName = stringFrom(
      rowField.path || rowField.name || rowField.field_id || rowField.id,
    );
    setDraftRow((current) => cloneWithPathValue(current || {}, rowFieldName, nextValue));
  }

  function emptyRow() {
    return itemFields.reduce<Record<string, unknown>>((acc, rowField) => {
      const rowFieldName = stringFrom(
        rowField.path || rowField.name || rowField.field_id || rowField.id,
      );
      const widget = stringFrom(rowField.widget || rowField.type || "text");
      if (widget === "boolean") {
        const fieldProps = objectOrEmpty(rowField.props);
        return cloneWithPathValue(
          acc,
          rowFieldName,
          booleanFrom(rowField.default ?? fieldProps.default, false),
        );
      }
      return cloneWithPathValue(
        acc,
        rowFieldName,
        stringFrom(rowField.default ?? objectOrEmpty(rowField.props).default),
      );
    }, {});
  }

  function openCreate() {
    setDraftError("");
    setEditingIndex(null);
    setDraftRow(emptyRow());
  }

  function openEdit(index: number) {
    setDraftError("");
    setEditingIndex(index);
    setDraftRow(structuredClone(items[index] || {}) as Record<string, unknown>);
  }

  function closeEditor() {
    setDraftError("");
    setEditingIndex(null);
    setDraftRow(null);
  }

  function saveDraft() {
    if (!draftRow) {
      return;
    }
    for (const rowField of itemFields) {
      const rowFieldName = stringFrom(
        rowField.path || rowField.name || rowField.field_id || rowField.id,
      );
      const fieldProps = objectOrEmpty(rowField.props);
      const required = Boolean(rowField.required ?? fieldProps.required);
      if (!required) {
        continue;
      }
      const candidateValue = valueAtPath(draftRow, rowFieldName);
      const widget = stringFrom(rowField.widget || rowField.type || "text");
      if (widget === "boolean") {
        continue;
      }
      if (!stringFrom(candidateValue)) {
        setDraftError(
          t("extensions.repeater.field_required", "{field} is required.", {
            field: stringFrom(rowField.label || rowFieldName, rowFieldName),
          }),
        );
        return;
      }
      if (fieldProps.unique_in_list) {
        const normalizedCandidate = stringFrom(candidateValue).toLowerCase();
        const duplicateIndex = items.findIndex((item, index) => {
          if (editingIndex !== null && index === editingIndex) {
            return false;
          }
          return stringFrom(valueAtPath(item, rowFieldName)).toLowerCase() === normalizedCandidate;
        });
        if (duplicateIndex >= 0) {
          setDraftError(
            t("extensions.repeater.field_unique", "{field} must be unique.", {
              field: stringFrom(rowField.label || rowFieldName, rowFieldName),
            }),
          );
          return;
        }
      }
    }
    setDraftError("");
    const nextItems =
      editingIndex === null
        ? [...items, draftRow]
        : items.map((item, index) => (index === editingIndex ? draftRow : item));
    if (editingIndex === null) {
      setItemKeys((current) => [
        ...items.map((_, index) => current[index] || createRowKey()),
        createRowKey(),
      ]);
    }
    onChange(fieldName, nextItems);
    closeEditor();
  }

  function removeRow(index: number) {
    setItemKeys((current) =>
      items
        .map((_, currentIndex) => current[currentIndex] || createRowKey())
        .filter((_, currentIndex) => currentIndex !== index),
    );
    onChange(
      fieldName,
      items.filter((_, currentIndex) => currentIndex !== index),
    );
    if (editingIndex === index) {
      closeEditor();
    }
  }

  return (
    <div className="fs-manifest-inset fs-manifest-repeater" key={`${componentId}:${fieldId}`}>
      <div className="fs-manifest-head">
        <div>
          <p className="fs-manifest-section-title">{label}</p>
          {help ? <p className="fs-manifest-description">{help}</p> : null}
        </div>
        <button className="fs-button sm" onClick={openCreate} type="button">
          {t("extensions.repeater.add_row_button", "+ Add row")}
        </button>
      </div>

      <div className="fs-manifest-list">
        {items.length ? (
          items.map((item, index) => {
            const rowKey = itemKeys[index] || `${componentId}:${fieldId}:pending:${index}`;
            return (
            <div
              className="fs-manifest-row is-top"
              data-row-key={rowKey}
              data-testid="manifest-repeater-row"
              key={rowKey}
            >
              <dl className="fs-manifest-repeater-values">
                {itemFields.map((rowField) => {
                  const rowFieldName = stringFrom(
                    rowField.path || rowField.name || rowField.field_id || rowField.id,
                  );
                  const rowLabel = stringFrom(rowField.label || rowFieldName);
                  const rowValue = valueAtPath(item, rowFieldName);
                  return (
                    <div key={`${fieldId}:${index}:${rowFieldName}`}>
                      <dt>{rowLabel}</dt>
                      <dd>{displayRepeaterValue(rowValue, t)}</dd>
                    </div>
                  );
                })}
              </dl>
              <div className="fs-manifest-actions">
                <button className="fs-button sm" onClick={() => openEdit(index)} type="button">
                  {t("extensions.repeater.edit", "Edit")}
                </button>
                <button className="fs-button sm ghost-danger" onClick={() => removeRow(index)} type="button">
                  {t("extensions.repeater.remove", "Remove")}
                </button>
              </div>
            </div>
            );
          })
        ) : (
          <div className="fs-manifest-empty">
            <p className="fs-empty-copy">{t("extensions.repeater.empty", "No rows yet.")}</p>
          </div>
        )}
      </div>

      {draftRow ? (
        <div className="fs-manifest-block">
          <div className="fs-manifest-head">
            <p className="fs-manifest-section-title">
              {editingIndex === null
                ? t("extensions.repeater.add_row", "Add row")
                : t("extensions.repeater.edit_row", "Edit row")}
            </p>
            <button className="fs-button sm ghost" onClick={closeEditor} type="button">
              {t("extensions.repeater.cancel", "Cancel")}
            </button>
          </div>
          <div className="fs-manifest-fields">
            {itemFields.map((rowField) => {
              const rowFieldName = stringFrom(
                rowField.path || rowField.name || rowField.field_id || rowField.id,
              );
              const rowLabel = stringFrom(rowField.label || rowFieldName, rowFieldName);
              const widget = stringFrom(rowField.widget || rowField.type || "text");
              const currentValue = valueAtPath(draftRow, rowFieldName);
              const fieldProps = objectOrEmpty(rowField.props);
              const immutableOnEdit = Boolean(fieldProps.immutable_on_edit);
              // Marked like every other required field, with the same rule saveDraft enforces.
              const isRequired = widget !== "boolean" && Boolean(rowField.required ?? fieldProps.required);
              const rowMeta = objectOrEmpty(draftRow._meta);
              const isLocked = Boolean(editingIndex !== null && immutableOnEdit && rowMeta.existing);
              const controlName = `${componentId}_${fieldId}_${rowFieldName}`
                .replaceAll(/[^A-Za-z0-9_:-]+/g, "_");
              const controlId = `${idPrefix}-${controlName}`;
              const lockedId = `${controlId}-locked`;
              return (
                <div className="fs-field" key={`${componentId}:${fieldId}:${rowFieldName}`}>
                  {widget === "boolean" ? (
                    <label className="fs-checkbox-label">
                      <input
                        aria-describedby={isLocked ? lockedId : undefined}
                        checked={Boolean(currentValue)}
                        disabled={isLocked}
                        id={controlId}
                        name={controlName}
                        onChange={(event) => updateDraftValue(rowField, event.target.checked)}
                        type="checkbox"
                      />
                      <DsCheckboxMark />
                      <span>{rowLabel}</span>
                    </label>
                  ) : (
                    <span className="fs-required-label">
                      <label className="fs-field-label" htmlFor={controlId}>{rowLabel}</label>
                      {isRequired ? <span aria-hidden="true" className="fs-required">*</span> : null}
                    </span>
                  )}
                  {widget === "select" ? (
                    <DsFormSelect
                      ariaLabel={rowLabel}
                      describedBy={isLocked ? lockedId : undefined}
                      disabled={isLocked}
                      id={controlId}
                      name={controlName}
                      onChange={(next) => updateDraftValue(rowField, next)}
                      options={fieldOptions(rowField)}
                      placeholder={t("extensions.repeater.select_placeholder", "Select…")}
                      value={stringFrom(currentValue)}
                    />
                  ) : null}
                  {widget !== "select" && widget !== "boolean" ? (
                    <input
                      aria-describedby={isLocked ? lockedId : undefined}
                      aria-required={isRequired || undefined}
                      className="fs-input"
                      disabled={isLocked}
                      id={controlId}
                      name={controlName}
                      onChange={(event) => updateDraftValue(rowField, event.target.value)}
                      placeholder={stringFrom(rowField.placeholder)}
                      type="text"
                      value={stringFrom(currentValue)}
                    />
                  ) : null}
                  {isLocked ? (
                    <span className="fs-field-helper" id={lockedId}>
                      {t("extensions.repeater.immutable", "This value is immutable after creation.")}
                    </span>
                  ) : null}
                </div>
              );
            })}
          </div>
          {draftError ? <DsAlert title={draftError} tone="danger" /> : null}
          <div className="fs-manifest-actions">
            <button className="fs-button sm primary" onClick={saveDraft} type="button">
              {editingIndex === null
                ? t("extensions.repeater.add_row", "Add row")
                : t("extensions.repeater.save_row", "Save row")}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
