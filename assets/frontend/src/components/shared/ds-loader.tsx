import type { ReactElement } from "react";

export type DsLoaderVariant = "wide-page" | "wide-block" | "orbit-slot" | "orbit-inline";
export type DsLoaderProps = { variant: DsLoaderVariant; label?: string };

const loaderClasses: Record<DsLoaderVariant, string> = {
  "wide-page": "fs-loader lg",
  "wide-block": "fs-loader",
  "orbit-slot": "fs-loader-orbit md",
  "orbit-inline": "fs-loader-orbit",
};

/** Published asset variants. The request owner controls delay and lifetime. */
export function DsLoader({ variant, label }: Readonly<DsLoaderProps>): ReactElement {
  const mark = <span aria-hidden="true" className={loaderClasses[variant]} data-testid="loader-mark" />;
  return label ? <span className="fs-loader-with-label" data-loader-variant={variant} role="status">{mark}<span>{label}</span></span> : mark;
}
