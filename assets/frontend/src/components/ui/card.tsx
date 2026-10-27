import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/utils/cn";

export function Card({ className, ...props }: Readonly<HTMLAttributes<HTMLDivElement>>) {
  return <div className={cn("fs-card-radius border p-[var(--fs-card-padding)]", className)} {...props} />;
}

/* The header-to-content gap belongs to the header, not to the content. Carried
   on the content it survived as dead space above every card that has no header:
   24px padding plus a 24px orphan margin read as 49px against 25px below.
   `last:mb-0` keeps a header-only card symmetric too. */
export function CardHeader({ className, ...props }: Readonly<HTMLAttributes<HTMLDivElement>>) {
  return <div className={cn("space-y-3 mb-[var(--fs-card-gap)] last:mb-0", className)} {...props} />;
}

type CardTitleProps = HTMLAttributes<HTMLHeadingElement> & {
  children: ReactNode;
};

export function CardTitle({ className, children, ...props }: Readonly<CardTitleProps>) {
  return (
    <h2 className={cn("text-2xl font-medium tracking-tight text-white", className)} {...props}>
      {children}
    </h2>
  );
}

export function CardDescription({ className, ...props }: Readonly<HTMLAttributes<HTMLParagraphElement>>) {
  return <p className={cn("text-sm leading-6 text-slate-300", className)} {...props} />;
}

export function CardContent({ className, ...props }: Readonly<HTMLAttributes<HTMLDivElement>>) {
  return <div className={cn(className)} {...props} />;
}
