import { useI18n } from "@/lib/i18n/useI18n";
import { useRef, useState } from "react";

export type ExternalResourcePickerBridge = {
  open: () => Promise<Array<Record<string, unknown>>>;
};

type SelectionField = {
  source: string;
  target: string;
  required?: boolean;
  max_length: number;
};

export type ExternalResourcePickerDeclaration = {
  button_label: string;
  busy_label?: string;
  error_message?: string;
  selection: {
    maximum: number;
    fields: SelectionField[];
  };
};

export type ExternalResourcePickerHostDeclaration = ExternalResourcePickerDeclaration & {
  allowed_connect_origins?: readonly string[];
  allowed_frame_origins?: readonly string[];
  allowed_script_origins: readonly string[];
  bootstrap_action_id: string;
  bridge_global: string;
  options?: Record<string, unknown>;
  scripts: readonly ({ src: string } | { asset_ref: string })[];
  target_component_id: string;
};

export function UnavailableExternalResourcePicker() {
  const { t } = useI18n();
  return (
    <p className="text-sm text-amber-100" role="alert">
      {t("react.shared.this_resource_picker_is_unavailable_enter_or_map_the_resource_id_instead", "This resource picker is unavailable. Enter or map the resource ID instead.")} </p>
  );
}

type ExternalResourcePickerProps = {
  bridge: ExternalResourcePickerBridge;
  declaration: ExternalResourcePickerDeclaration;
  disabled: boolean;
  onSelection: (rows: Array<Record<string, string>>) => void;
};

function projectSelection(
  value: unknown,
  fields: SelectionField[],
): Record<string, string> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const source = value as Record<string, unknown>;
  const projected: Record<string, string> = {};
  for (const field of fields) {
    const raw = source[field.source];
    if (raw === undefined || raw === null || raw === "") {
      if (field.required) {
        return null;
      }
      continue;
    }
    if (
      typeof raw !== "string"
      || raw.length > field.max_length
      || !field.source
      || !field.target
    ) {
      return null;
    }
    projected[field.target] = raw;
  }
  return projected;
}

export function ExternalResourcePicker({
  bridge,
  declaration,
  disabled,
  onSelection,
}: Readonly<ExternalResourcePickerProps>) {
  const { t } = useI18n();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function open() {
    if (disabled || busy) {
      return;
    }
    setBusy(true);
    setError("");
    try {
      const raw = await bridge.open();
      if (!Array.isArray(raw) || raw.length > declaration.selection.maximum) {
        throw new Error("invalid_selection");
      }
      const projected = raw.map((row) => projectSelection(row, declaration.selection.fields));
      if (projected.includes(null)) {
        throw new Error("invalid_selection");
      }
      onSelection(projected as Array<Record<string, string>>);
    } catch {
      setError(declaration.error_message || t("react.shared.selection_could_not_be_applied_try_again", "Selection could not be applied. Try again."));
    } finally {
      setBusy(false);
      globalThis.setTimeout(() => triggerRef.current?.focus(), 0);
    }
  }

  return (
    <div className="space-y-1">
      <button
        className="rounded-lg border border-cyan-300/30 bg-cyan-500/10 px-3 py-1.5 text-sm text-cyan-100 disabled:cursor-not-allowed disabled:opacity-50"
        disabled={disabled || busy}
        onClick={() => void open()}
        ref={triggerRef}
        type="button"
      >
        {busy ? declaration.busy_label || t("react.shared.opening", "Opening…") : declaration.button_label}
      </button>
      {error ? <p className="text-sm text-rose-100" role="alert">{error}</p> : null}
    </div>
  );
}
