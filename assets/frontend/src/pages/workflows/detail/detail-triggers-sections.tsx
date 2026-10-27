import { ChevronRight } from "lucide-react";
import { type ReactNode } from "react";

import { DsRadio } from "@/components/shared/design-system";

export function TriggerChoice({
  checked,
  children,
  disabled = false,
  help,
  name,
  onChoose,
  value,
}: Readonly<{
  checked: boolean;
  children: ReactNode;
  disabled?: boolean;
  help?: string;
  name: string;
  onChoose: () => void;
  value: string;
}>) {
  return (
    <div className="workflow-triggers-choice" data-checked={checked ? "true" : "false"} data-disabled={disabled ? "true" : "false"}>
      <DsRadio checked={checked} name={name} onChange={() => { if (!disabled) onChoose(); }} value={value}>
        <span className="workflow-triggers-choice-text">
          <span>{children}</span>
          {help ? <span className="workflow-triggers-choice-help">{help}</span> : null}
        </span>
      </DsRadio>
    </div>
  );
}

export function SettingsSection({
  children,
  note,
  onToggle,
  open,
  title,
}: Readonly<{
  children: ReactNode;
  note?: string;
  onToggle?: (open: boolean) => void;
  open?: boolean;
  title: string;
}>) {
  return (
    <details
      className="fs-settings-section"
      onToggle={onToggle ? (event) => onToggle(event.currentTarget.open) : undefined}
      open={open}
    >
      <summary>
        <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
        {title}
        {note ? <span className="fs-settings-section-note">{note}</span> : null}
      </summary>
      <div className="fs-settings-section-body">{children}</div>
    </details>
  );
}

/**
 * Workflow settings › Triggers. The tab opens on the two lists — incoming
 * webhooks and provider triggers, each with its count and empty state — and
 * one trigger opens at a time: a webhook starts from who sends the requests
 * (a preset) and keeps auth and limits in Advanced settings; a provider
 * trigger starts from what happens in a connected app.
 */
