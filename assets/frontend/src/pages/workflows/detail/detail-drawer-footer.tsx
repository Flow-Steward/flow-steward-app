import { createContext, useContext, type ReactNode } from "react";
import { createPortal } from "react-dom";

/**
 * The builder drawer's sticky footer. The drawer shell owns one slot under the
 * scrolling body; an editor surface renders its actions into it, so Delete and
 * Save stay in view however long the form is, and no tab repeats them.
 */
const DrawerFooterSlotContext = createContext<HTMLElement | null>(null);

export function DrawerFooterSlotProvider({ value, children }: Readonly<{ value: HTMLElement | null; children: ReactNode }>) {
  return <DrawerFooterSlotContext.Provider value={value}>{children}</DrawerFooterSlotContext.Provider>;
}

export function DrawerFooter({ children, testId }: Readonly<{ children: ReactNode; testId?: string }>) {
  const slot = useContext(DrawerFooterSlotContext);
  const row = <div className="workflow-builder-drawer-footer-row" data-testid={testId}>{children}</div>;
  // A surface rendered outside the shell (a focused test, an embedded page) keeps its actions inline.
  return slot ? createPortal(row, slot) : <div className="workflow-builder-drawer-footer">{row}</div>;
}

/** Pushes what follows to the footer's end: destructive actions start, Save ends. */
export function DrawerFooterSpacer() {
  return <span aria-hidden="true" className="workflow-builder-drawer-footer-space" />;
}
