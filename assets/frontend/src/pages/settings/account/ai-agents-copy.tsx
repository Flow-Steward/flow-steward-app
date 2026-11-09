import { useEffect, useRef, useState } from "react";

import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";

/** How long the button reads Copied before it reads Copy again. */
export const COPIED_FOR_MS = 1500;

/**
 * Copy on the AI agents tab: the word in the button turns into Copied for 1.5 s,
 * with no icon and no toast, and the button keeps its width. A screen reader
 * hears Copied once. When the clipboard is not available the button stays Copy
 * and a red line under it says so.
 */
export function AgentCopyButton({
  ariaLabel,
  disabled = false,
  text,
}: Readonly<{ ariaLabel?: string; disabled?: boolean; text: string }>) {
  const { t } = useI18n();
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => () => clearTimeout(timer.current), []);

  async function copy() {
    clearTimeout(timer.current);
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
      await navigator.clipboard.writeText(text);
      setState("copied");
      timer.current = setTimeout(() => setState("idle"), COPIED_FOR_MS);
    } catch {
      setState("failed");
    }
  }

  const copied = state === "copied";
  return (
    <>
      <button
        aria-label={ariaLabel}
        className="fs-button secondary sm fs-agent-copy"
        data-copied={copied ? "true" : undefined}
        disabled={disabled}
        onClick={() => ignoreAsyncResult(copy())}
        type="button"
      >
        {copied ? t("react.agent_access.copied_button", "Copied") : t("react.agent_access.copy", "Copy")}
      </button>
      <span aria-live="polite" className="fs-sr-only">{copied ? t("react.agent_access.copied_button", "Copied") : ""}</span>
      {state === "failed" ? <p className="fs-field-helper error fs-agent-copy-error" role="alert">{t("react.agent_access.copy_failed", "Couldn't copy. Select the text and copy it manually.")}</p> : null}
    </>
  );
}

/** A command or a prompt with its Copy button inside the block, top right. */
export function AgentCodeBlock({
  disabled = false,
  label,
  prose = false,
  text,
}: Readonly<{ disabled?: boolean; label: string; prose?: boolean; text: string }>) {
  return (
    <div className="fs-agent-code-block">
      <pre aria-label={label} className={prose ? "fs-agent-code prose" : "fs-agent-code"}>{text}</pre>
      <AgentCopyButton disabled={disabled} text={text} />
    </div>
  );
}
