import type { ReactElement } from "react";

import "./ds-switch.css";

export type DsSwitchProps = {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  label: string;
  describedBy?: string;
  disabled?: boolean;
};

/** The published default Switch. Persistence and feedback belong to its row. */
export function DsSwitch({ checked, onCheckedChange, label, describedBy, disabled }: Readonly<DsSwitchProps>): ReactElement {
  return <span className="fs-switch">
    <input
      aria-describedby={describedBy}
      aria-label={label}
      checked={checked}
      className="fs-switch-input"
      disabled={disabled}
      onChange={(event) => onCheckedChange(event.currentTarget.checked)}
      role="switch"
      type="checkbox"
    />
    <span aria-hidden="true" className="fs-switch-track" />
  </span>;
}
