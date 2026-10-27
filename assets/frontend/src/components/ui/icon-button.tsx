import type { ButtonHTMLAttributes } from "react";
import { useId, useState } from "react";
import type { LucideIcon } from "lucide-react";

import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";

import { cn } from "@/lib/utils/cn";

type IconButtonProps = Omit<
  ButtonHTMLAttributes<HTMLButtonElement>,
  "children" | "size"
> & {
  icon: LucideIcon;
  /** Glyph size when it differs from the button size's default (12 small, 16 medium). */
  iconSize?: number;
  label: string;
  showTooltip?: boolean;
  size?: "small" | "medium";
};

export function IconButton({
  "aria-describedby": describedBy,
  className,
  icon: Icon,
  iconSize,
  label,
  onBlur,
  onClickCapture,
  onFocus,
  onMouseEnter,
  onMouseLeave,
  showTooltip = true,
  size = "medium",
  type = "button",
  ...props
}: Readonly<IconButtonProps>) {
  const tooltipId = useId();
  const busy = props["aria-busy"] === true || props["aria-busy"] === "true";
  const loaderVisible = useLoaderDelay(busy);
  const [focused, setFocused] = useState(false);
  const [hovered, setHovered] = useState(false);
  const tooltipVisible = showTooltip && (focused || hovered);

  return (
    <span className="fs-icon-button-wrap">
      <button
        {...props}
        aria-describedby={[describedBy, tooltipVisible ? tooltipId : undefined].filter(Boolean).join(" ") || undefined}
        aria-disabled={busy ? true : props["aria-disabled"]}
        aria-label={label}
        className={cn("fs-icon-button", className)}
        data-size={size}
        onClickCapture={(event) => {
          if (busy) { event.preventDefault(); event.stopPropagation(); return; }
          onClickCapture?.(event);
        }}
        onBlur={(event) => {
          if (showTooltip) setFocused(false);
          onBlur?.(event);
        }}
        onFocus={(event) => {
          if (showTooltip) setFocused(true);
          onFocus?.(event);
        }}
        onMouseEnter={(event) => {
          if (showTooltip) setHovered(true);
          onMouseEnter?.(event);
        }}
        onMouseLeave={(event) => {
          if (showTooltip) setHovered(false);
          onMouseLeave?.(event);
        }}
        type={type}
      >
        {loaderVisible ? <DsLoader variant="orbit-inline" /> : <Icon
          aria-hidden="true"
          size={iconSize ?? (size === "small" ? 12 : 16)}
          strokeWidth={2}
        />}
      </button>
      {tooltipVisible ? (
        <span className="fs-shell-tooltip" id={tooltipId} role="tooltip">
          {label}
        </span>
      ) : null}
    </span>
  );
}
