import type { ButtonHTMLAttributes } from "react";
import { useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
  /** Use a viewport layer when the button lives in a clipping container. */
  tooltipInViewport?: boolean;
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
  tooltipInViewport = false,
  size = "medium",
  type = "button",
  ...props
}: Readonly<IconButtonProps>) {
  const tooltipId = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const tooltipRef = useRef<HTMLSpanElement>(null);
  const busy = props["aria-busy"] === true || props["aria-busy"] === "true";
  const loaderVisible = useLoaderDelay(busy);
  const [focused, setFocused] = useState(false);
  const [hovered, setHovered] = useState(false);
  const tooltipVisible = showTooltip && (focused || hovered);

  useLayoutEffect(() => {
    if (!tooltipInViewport || !tooltipVisible) return;
    const button = buttonRef.current;
    const tooltip = tooltipRef.current;
    if (!button || !tooltip) return;
    const position = () => {
      const anchor = button.getBoundingClientRect();
      const { width, height } = tooltip.getBoundingClientRect();
      const gap = Number.parseFloat(getComputedStyle(button).getPropertyValue("--fs-tooltip-offset")) || 8;
      const inset = 8;
      const { clientWidth, clientHeight } = document.documentElement;
      const below = anchor.bottom + gap;
      const top = below + height <= clientHeight - inset ? below : anchor.top - height - gap;
      const left = anchor.left + (anchor.width - width) / 2;
      tooltip.style.top = `${Math.max(inset, top)}px`;
      tooltip.style.left = `${Math.max(inset, Math.min(left, clientWidth - width - inset))}px`;
      tooltip.style.visibility = anchor.bottom < 0 || anchor.top > clientHeight ? "hidden" : "";
    };
    position();
    window.addEventListener("scroll", position, true);
    window.addEventListener("resize", position);
    return () => {
      window.removeEventListener("scroll", position, true);
      window.removeEventListener("resize", position);
    };
  }, [label, tooltipInViewport, tooltipVisible]);

  const tooltip = tooltipVisible ? (
    <span
      className={cn("fs-shell-tooltip", tooltipInViewport && "fs-icon-button-viewport-tooltip")}
      id={tooltipId}
      ref={tooltipRef}
      role="tooltip"
    >
      {label}
    </span>
  ) : null;

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
        ref={buttonRef}
        type={type}
      >
        {loaderVisible ? <DsLoader variant="orbit-inline" /> : <Icon
          aria-hidden="true"
          size={iconSize ?? (size === "small" ? 12 : 16)}
          strokeWidth={2}
        />}
      </button>
      {tooltip && tooltipInViewport ? createPortal(tooltip, document.body) : tooltip}
    </span>
  );
}
