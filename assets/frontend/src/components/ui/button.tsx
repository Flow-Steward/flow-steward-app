import type { ButtonHTMLAttributes, ReactNode } from "react";

import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { cn } from "@/lib/utils/cn";

/**
 * The published Button. This file used to carry scaffold styling — a cyan fill
 * on slate text — which is not in the Flow Steward palette at all. The variants
 * and sizes now map onto `.fs-button`, so every caller renders the real thing.
 *
 * `lg` is deliberately not a separate size: the button contract caps dashboard
 * controls at the 32px medium height.
 */
const buttonVariants = cva("fs-button", {
  variants: {
    variant: {
      primary: "primary",
      accent: "accent",
      secondary: "secondary",
      ghost: "ghost",
      "ghost-danger": "ghost-danger",
      danger: "danger",
    },
    size: {
      sm: "sm",
      md: "",
      lg: "",
    },
  },
  defaultVariants: {
    variant: "primary",
    size: "md",
  },
});

/**
 * The busy content of a text button (03_components/actions/button.md, Loading):
 * the resting content and the busy content — the round mono Loader in the
 * leading-icon position, then the present-progressive label — share one grid
 * cell, so the button keeps the wider of the two widths in both states and
 * centres what shows. `shown` comes from `useLoaderDelay(busy)` in the owner:
 * the busy label and the Loader wait 400ms and stay at least 500ms.
 */
export function ButtonBusyContent({ busyLabel, children, shown }: Readonly<{ busyLabel: ReactNode; children: ReactNode; shown: boolean }>) {
  return (
    <span className="fs-button-busy-stack">
      <span aria-hidden={shown || undefined} data-on={shown ? "false" : "true"}>{children}</span>
      <span aria-hidden={shown ? undefined : true} data-on={shown ? "true" : "false"}><DsLoader variant="orbit-inline" />{busyLabel}</span>
    </span>
  );
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean;
    /**
     * The button's own action is pending. `aria-busy` and ignoring repeat
     * presses start at once; with `busyLabel` the label and the Loader follow
     * after the loader delay. Do not also pass `disabled` for the pending
     * action: it would drop keyboard focus mid-action.
     */
    busy?: boolean;
    /** Present-progressive with an ellipsis: `Save` → `Saving…`. */
    busyLabel?: ReactNode;
  };

export function Button({ asChild = false, busy: busyProp, busyLabel, children, className, size, variant, onClickCapture, ...props }: Readonly<ButtonProps>) {
  const Component = asChild ? Slot : "button";
  const busy = busyProp ?? (props["aria-busy"] === true || props["aria-busy"] === "true");
  const withBusyLabel = busyLabel !== undefined && !asChild;
  const shown = useLoaderDelay(withBusyLabel && busy);
  const content = withBusyLabel ? <ButtonBusyContent busyLabel={busyLabel} shown={shown}>{children}</ButtonBusyContent> : children;

  return <Component {...props} aria-busy={busy || undefined} aria-disabled={busy ? true : props["aria-disabled"]} className={cn(buttonVariants({ className, size, variant }))} onClickCapture={(event) => {
    if (busy) { event.preventDefault(); event.stopPropagation(); return; }
    onClickCapture?.(event);
  }}>{content}</Component>;
}
