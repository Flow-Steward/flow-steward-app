/**
 * Shared by the design-system selects and comboboxes: the option shape, the
 * keyboard index mover and the anchored menu position (split from
 * design-system-selects.tsx for Sonar's file-size limit).
 */
import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from "react";

/** `icon` is a small mark drawn before the label (an extension's logo tile). */
export type DsOption = { value: string; label: string; description?: string; disabled?: boolean; icon?: ReactNode };

export function createActiveIndexMover(
  optionCount: number,
  openMenu: () => void,
  setActiveIndex: (index: number) => void,
) {
  return (index: number) => {
    if (!optionCount) return;
    openMenu();
    setActiveIndex((index + optionCount) % optionCount);
  };
}

/**
 * Places a portalled menu against its anchor in the viewport layer. It opens
 * below and flips above when the room below — up to the viewport edge or the
 * footer of the dialog holding the anchor — is shorter than the menu and the
 * room above is larger. A press outside the anchor, the menu and `insideRefs`
 * dismisses it, since the portalled menu is not inside the component wrapper.
 */
export function useAnchoredMenuPosition({
  open,
  anchorRef,
  menuRef,
  insideRefs = [],
  onDismiss,
  maxHeightToken,
  remeasureKey,
  scrollContentRef,
}: Readonly<{
  open: boolean;
  anchorRef: RefObject<HTMLElement | null>;
  menuRef: RefObject<HTMLElement | null>;
  insideRefs?: ReadonlyArray<RefObject<HTMLElement | null>>;
  onDismiss: () => void;
  /** Token capping the menu height; without one the menu grows to its content. */
  maxHeightToken?: string;
  /** Re-measures when the menu content changes height, such as a filtered list. */
  remeasureKey?: unknown;
  /** A nested scrolling list contributes its natural, unconstrained height. */
  scrollContentRef?: RefObject<HTMLElement | null>;
}>): CSSProperties {
  const [position, setPosition] = useState<CSSProperties>({ top: 0, left: 0, width: 0 });
  const latest = useRef({ onDismiss, insideRefs });
  useLayoutEffect(() => {
    latest.current = { onDismiss, insideRefs };
  });
  useLayoutEffect(() => {
    if (!open) return;
    // Tokens are read once per open, not on every scroll or resize: computed
    // style is the expensive part of placing the menu.
    const styles = getComputedStyle(document.documentElement);
    const popupGap = Number.parseFloat(styles.getPropertyValue("--fs-popup-offset")) || 6;
    const viewportInset = Number.parseFloat(styles.getPropertyValue("--fs-space-2")) || 8;
    const configuredMaxHeight = maxHeightToken
      ? Number.parseFloat(styles.getPropertyValue(maxHeightToken)) || 188
      : Number.POSITIVE_INFINITY;
    const optionHeight = Number.parseFloat(styles.getPropertyValue("--fs-form-dialog-select-option-height")) || 32;
    const updatePosition = () => {
      const anchor = anchorRef.current;
      const menu = menuRef.current;
      if (!anchor || !menu) return;
      const rect = anchor.getBoundingClientRect();
      const footer = anchor.closest(".fs-dialog")?.querySelector<HTMLElement>(".fs-dialog-footer")?.getBoundingClientRect();
      const scrollContent = scrollContentRef?.current;
      const naturalHeight = menu.scrollHeight + (scrollContent ? scrollContent.scrollHeight - scrollContent.clientHeight : 0);
      const desiredHeight = Math.min(naturalHeight, configuredMaxHeight);
      const belowLimit = Math.min(footer?.top ?? window.innerHeight - viewportInset, window.innerHeight - viewportInset);
      const belowSpace = Math.max(0, belowLimit - rect.bottom - popupGap);
      const aboveSpace = Math.max(0, rect.top - popupGap - viewportInset);
      const opensAbove = belowSpace < desiredHeight && aboveSpace > belowSpace;
      const maxHeight = Math.max(optionHeight * 2, Math.min(desiredHeight, opensAbove ? aboveSpace : belowSpace));
      const left = Math.min(Math.max(viewportInset, rect.left), window.innerWidth - rect.width - viewportInset);
      const verticalAnchor = opensAbove
        ? { top: "auto", bottom: Math.round(window.innerHeight - rect.top + popupGap) }
        : { top: Math.round(rect.bottom + popupGap), bottom: "auto" };
      setPosition({ ...verticalAnchor, left: Math.round(left), width: Math.round(rect.width), maxHeight: Math.round(maxHeight) });
    };
    const close = (event: MouseEvent) => {
      const target = event.target as Node;
      const refs = [anchorRef, menuRef, ...latest.current.insideRefs];
      if (!refs.some((ref) => ref.current?.contains(target))) latest.current.onDismiss();
    };
    updatePosition();
    const positionFrame = window.requestAnimationFrame(updatePosition);
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    document.addEventListener("mousedown", close);
    return () => {
      window.cancelAnimationFrame(positionFrame);
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
      document.removeEventListener("mousedown", close);
    };
  }, [anchorRef, maxHeightToken, menuRef, open, remeasureKey, scrollContentRef]);
  return position;
}
