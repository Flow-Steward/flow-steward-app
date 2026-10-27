import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useId, useState } from "react";

type Props = {
  className: string;
  label: string;
  name: string;
  onChange: (value: unknown) => void;
  onValidationChange?: (name: string, error: string) => void;
  placeholder?: string;
  shape: "object" | "rows";
  value: unknown;
};

/** Keep an invalid editing draft separate from the last valid saved value. */
export function TabularJsonField({
  className, label, name, onChange, onValidationChange, placeholder, shape, value,
}: Readonly<Props>) {
  const { t } = useI18n();
  const id = useId();
  const labelId = `${id}-label`;
  const errorId = `${id}-error`;
  const sourceText = shape === "rows" && typeof value === "string"
    ? value
    : JSON.stringify(value ?? (shape === "rows" ? [] : {}), null, 2);
  const [source, setSource] = useState(sourceText);
  const [draft, setDraft] = useState(sourceText);
  const [error, setError] = useState("");
  if (source !== sourceText) {
    setSource(sourceText);
    setDraft(sourceText);
    setError("");
  }
  useEffect(() => () => onValidationChange?.(name, ""), [name, onValidationChange]);

  function update(raw: string) {
    setDraft(raw);
    try {
      const text = raw.trim();
      const parsed: unknown = shape === "rows" && text.startsWith("state.") && text.length > 6
        ? text
        : JSON.parse(text);
      if (shape === "object" && (parsed === null || typeof parsed !== "object" || Array.isArray(parsed))) {
        throw new Error(t("react.workflows.value0_must_be_a_json_object", "{value0} must be a JSON object.", { value0: label }));
      }
      if (shape === "rows" && !Array.isArray(parsed) && typeof parsed !== "string") {
        throw new Error(t("react.workflows.value0_must_be_a_json_array_or_state_selector", "{value0} must be a JSON array or state selector.", { value0: label }));
      }
      if (shape === "rows" && typeof parsed === "string" && !text.startsWith("state.")) {
        throw new Error(t("react.workflows.value0_must_be_a_json_array_or_state_selector", "{value0} must be a JSON array or state selector.", { value0: label }));
      }
      setSource(shape === "rows" && typeof parsed === "string" ? parsed : JSON.stringify(parsed, null, 2));
      setError("");
      onValidationChange?.(name, "");
      onChange(parsed);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : `Invalid ${label} JSON.`;
      setError(message);
      onValidationChange?.(name, message);
    }
  }

  return (
    <label className="block text-xs fs-ink-secondary">
      <span id={labelId}>{label}</span>
      <textarea aria-describedby={error ? errorId : undefined} aria-invalid={Boolean(error)} aria-labelledby={labelId} className={className} name={name}
        onChange={(event) => update(event.target.value)} placeholder={placeholder} value={draft} />
      {error ? <span className="block text-red-300" id={errorId} role="alert">{error}</span> : null}
    </label>
  );
}
