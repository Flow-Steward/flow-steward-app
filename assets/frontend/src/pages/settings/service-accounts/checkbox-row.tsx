import { type ReactNode, type RefObject } from "react";
import { DsCheckboxMark } from "@/components/shared/design-system";

export function CheckboxRow({
  checked,
  children,
  disabled,
  id,
  inputRef,
  name,
  onChange,
  value,
  ariaLabel,
}: Readonly<{
  checked: boolean;
  children: ReactNode;
  disabled?: boolean;
  id?: string;
  inputRef?: RefObject<HTMLInputElement | null>;
  name?: string;
  onChange: (checked: boolean) => void;
  value?: string;
  ariaLabel?: string;
}>) {
  return (
    <label className="fs-checkbox-label">
      <input
        aria-label={ariaLabel}
        checked={checked}
        disabled={disabled}
        id={id}
        name={name}
        onChange={(event) => onChange(event.target.checked)}
        ref={inputRef}
        type="checkbox"
        value={value}
      />
      <DsCheckboxMark />
      <span>{children}</span>
    </label>
  );
}
