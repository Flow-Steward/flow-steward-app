import { useState } from "react";

import type { I18nContextValue } from "@/lib/i18n/context";

/** The declared tag_picker widget stores string arrays; its draft is never form data. */
export function ManifestTagPicker({
  controlId, currentValue, describedBy, fieldName, isInvalid, isRequired,
  onFieldChange, t,
}: Readonly<{
  controlId: string;
  currentValue: unknown;
  describedBy?: string;
  fieldName: string;
  isInvalid: boolean;
  isRequired: boolean;
  onFieldChange: (fieldName: string, value: unknown) => void;
  t: I18nContextValue["t"];
}>) {
  const [draft, setDraft] = useState("");
  const items = Array.isArray(currentValue)
    ? currentValue.filter((item): item is string => typeof item === "string")
    : [];

  function addTag() {
    const value = draft.trim();
    if (value && !items.includes(value)) onFieldChange(fieldName, [...items, value]);
    setDraft("");
  }

  return (
    <div>
      <div className="fs-manifest-actions">
        {items.map((item, index) => (
          <span className="fs-manifest-actions" key={`${index}:${item}`}>
            <span>{item}</span>
            <button
              aria-label={t("react.extensions.remove_value0", "Remove {value0}", { value0: item })}
              className="fs-button sm ghost"
              onClick={() => onFieldChange(fieldName, items.filter((_, position) => position !== index))}
              type="button"
            >
              {t("extensions.repeater.remove", "Remove")}
            </button>
          </span>
        ))}
      </div>
      <div className="fs-manifest-actions">
        <input
          aria-describedby={describedBy}
          aria-invalid={isInvalid || undefined}
          aria-required={isRequired}
          className="fs-input"
          id={controlId}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            if (!event.nativeEvent.isComposing) addTag();
          }}
          required={isRequired && items.length === 0}
          type="text"
          value={draft}
        />
        <button className="fs-button sm" disabled={!draft.trim()} onClick={addTag} type="button">
          {t("registry.agent_detail.common.add", "Add")}
        </button>
      </div>
    </div>
  );
}
