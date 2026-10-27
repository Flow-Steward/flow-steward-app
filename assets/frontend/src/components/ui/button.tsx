import type { ButtonHTMLAttributes } from "react";

import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

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

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean;
  };

export function Button({ asChild = false, className, size, variant, onClickCapture, ...props }: Readonly<ButtonProps>) {
  const Component = asChild ? Slot : "button";
  const busy = props["aria-busy"] === true || props["aria-busy"] === "true";

  return <Component {...props} aria-disabled={busy ? true : props["aria-disabled"]} className={cn(buttonVariants({ className, size, variant }))} onClickCapture={(event) => {
    if (busy) { event.preventDefault(); event.stopPropagation(); return; }
    onClickCapture?.(event);
  }} />;
}
