import type { ReactNode } from "react";

export const FILTER_FIELD_CLASS_NAME =
  "grid gap-1 text-xs uppercase tracking-[0.15em] text-slate-300";

export const FILTER_CONTROL_CLASS_NAME =
  "rounded-xl border border-white/10 bg-slate-900/70 px-3 py-2 text-sm normal-case tracking-normal text-white outline-none focus:border-cyan-300/40";

export const FILTER_CONTROL_ROUNDED_CLASS_NAME =
  "rounded-2xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white outline-none focus:border-cyan-300/40";

export const FILTER_ACTION_BUTTON_CLASS_NAME =
  "rounded-2xl border border-white/10 bg-white/5 px-3 py-2 text-sm text-slate-200 transition hover:border-cyan-300/40 disabled:opacity-50";

export function FilterField({
  children,
  label,
}: Readonly<{
  children: ReactNode;
  label: ReactNode;
}>) {
  return (
    <label className={FILTER_FIELD_CLASS_NAME}>
      {label}
      {children}
    </label>
  );
}
