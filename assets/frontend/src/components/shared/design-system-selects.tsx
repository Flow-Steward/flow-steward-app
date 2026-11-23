import { useI18n } from "@/lib/i18n/useI18n";
import { Check, ChevronDown, ChevronLeft, ChevronRight, Plus, Search, X } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

import { DsLoader } from "@/components/shared/ds-loader";
import { filterRangeParts, filterRangeValue } from "@/components/shared/filter-range";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";

/** `icon` is a small mark drawn before the label (an extension's logo tile). */
export type DsOption = { value: string; label: string; description?: string; disabled?: boolean; icon?: ReactNode };
/**
 * `multiple` lets one field hold several values: its value in
 * `AdvancedTableFilters` is then a comma-separated list of option values.
 */
export type DsFilterField = {
  key: string;
  label: string;
  options: DsOption[];
  multiple?: boolean;
  /**
   * A typed value instead of a choice: `text` holds the text, and a range holds
   * `from..to` (either side may be empty). `options` is then unused.
   */
  input?: "text" | "number_range" | "date_range";
};

const FILTER_SEARCH_MIN = 8;
const FILTER_TOOLTIP_DELAY = 500;

/** The arrow keys walk the back row, the search box and the options, in order. */
const FILTER_NAV_KEYS = new Set(["ArrowDown", "ArrowUp", "Home", "End"]);
/** Inputs whose arrow keys change their value natively (date segments, number steps). */
const NATIVE_STEP_INPUT_TYPES = new Set(["date", "datetime-local", "time", "month", "week", "number"]);

function FilterMenuText({ option, descriptionId }: Readonly<{ option: Pick<DsOption, "label" | "description">; descriptionId?: string }>) {
  return (
    <span className="fs-filter-menu-text">
      <span className="fs-filter-menu-label">{option.label}</span>
      {option.description ? <span className="fs-filter-menu-description" id={descriptionId}>{option.description}</span> : null}
    </span>
  );
}

/** How an applied typed filter reads in its chip: the text, `a – b`, `≥ a` or `≤ b`. */
function typedFilterLabel(field: DsFilterField, value: string): string {
  if (field.input === "text") return value;
  const [from, to] = filterRangeParts(value);
  if (from && to) return `${from} – ${to}`;
  return from ? `≥ ${from}` : `≤ ${to}`;
}

/**
 * The value stage of a typed filter: a text box, or a from/to pair of number or
 * date boxes, applied with one button (Enter applies too).
 */
function FilterInputStage({
  field,
  value,
  onApply,
}: Readonly<{ field: DsFilterField; value: string; onApply: (value: string) => void }>) {
  const { t } = useI18n();
  const [initialFrom, initialTo] = field.input === "text" ? [value, ""] : filterRangeParts(value);
  const [from, setFrom] = useState(initialFrom);
  const [to, setTo] = useState(initialTo);
  const inputType = field.input === "date_range" ? "date" : field.input === "number_range" ? "number" : "search";
  const apply = () => onApply(field.input === "text" ? from.trim() : filterRangeValue(from, to));
  return (
    <form
      className="fs-filter-menu-input"
      onSubmit={(event) => {
        event.preventDefault();
        apply();
      }}
    >
      {field.input === "text" ? (
        <input
          aria-label={field.label}
          autoComplete="off"
          className="fs-input"
          data-filter-nav=""
          data-filter-value=""
          onChange={(event) => setFrom(event.target.value)}
          type="search"
          value={from}
        />
      ) : (
        <>
          <label className="fs-field">
            <span className="fs-field-label">{t("react.shared.filter_from", "From")}</span>
            <input className="fs-input" data-filter-nav="" data-filter-value="" inputMode={inputType === "number" ? "decimal" : undefined} onChange={(event) => setFrom(event.target.value)} type={inputType} value={from} />
          </label>
          <label className="fs-field">
            <span className="fs-field-label">{t("react.shared.filter_to", "To")}</span>
            <input className="fs-input" data-filter-nav="" inputMode={inputType === "number" ? "decimal" : undefined} onChange={(event) => setTo(event.target.value)} type={inputType} value={to} />
          </label>
        </>
      )}
      <button className="fs-button primary" data-filter-nav="" type="submit">
        {t("react.shared.apply_filter", "Apply")}
      </button>
    </form>
  );
}

function filterMenuTitle(option: Pick<DsOption, "label" | "description">): string {
  return option.description ? `${option.label} · ${option.description}` : option.label;
}

/** The values a filter field holds; a `multiple` field keeps a comma list. */
function filterFieldValues(value: string | undefined): string[] {
  return (value || "").split(",").map((item) => item.trim()).filter(Boolean);
}

function createActiveIndexMover(
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
function useAnchoredMenuPosition({
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

export function DsSelect({
  ariaLabel,
  labelledBy,
  value,
  options,
  onChange,
  placeholder: suppliedPlaceholder,
  disabled,
  required,
  invalid,
  id,
  className = "",
  emptyLabel: suppliedEmptyLabel,
  describedBy,
  searchable = false,
}: Readonly<{
  ariaLabel: string;
  /** Uses the visible field label as the trigger name when one is present. */
  labelledBy?: string;
  value: string;
  options: DsOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  required?: boolean;
  /** Marks the trigger as holding a rejected value, like a native control. */
  invalid?: boolean;
  /** Addresses the trigger so a form can move focus to it after a failed submit. */
  id?: string;
  className?: string;
  /** Copy for the compact empty row when the menu has no options. */
  emptyLabel?: string;
  /** Ids of the helper and error text, so they are announced with the trigger. */
  describedBy?: string;
  /** Keep a search field above long option lists. */
  searchable?: boolean;
}>) {
  const { t } = useI18n();
  const placeholder = suppliedPlaceholder ?? t("react.shared.select_an_option", "Select an option");
  const emptyLabel = suppliedEmptyLabel ?? t("react.shared.no_options_available", "No options available");
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const optionsRef = useRef<HTMLDivElement>(null);
  const filteredOptions = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return searchable && needle
      ? options.filter((option) => `${option.label} ${option.value} ${option.description || ""}`.toLocaleLowerCase().includes(needle))
      : options;
  }, [options, query, searchable]);
  const listboxId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const selected = options.find((option) => option.value === value);
  const [activeIndex, setActiveIndex] = useState(-1);
  const menuRef = useRef<HTMLDivElement>(null);
  // Reset this component's transient menu state as the control becomes locked.
  // The state guard converges immediately and prevents a stale menu reopening.
  if (disabled && open) {
    setOpen(false);
    setActiveIndex(-1);
  }
  // The menu lives in the viewport layer, like the multi-Combobox's, so a
  // dialog body or footer cannot clip it and it flips above when the room
  // below is short.
  const menuPosition = useAnchoredMenuPosition({
    open: open && !disabled,
    anchorRef: triggerRef,
    menuRef,
    insideRefs: [rootRef],
    onDismiss: () => setOpen(false),
    remeasureKey: filteredOptions.length,
    maxHeightToken: searchable ? "--fs-combobox-menu-max-height" : undefined,
    scrollContentRef: searchable ? optionsRef : undefined,
  });
  useEffect(() => {
    if (open && searchable && !disabled) searchRef.current?.focus();
  }, [open, searchable, disabled]);
  function focusOption(index: number, direction = 1) {
    for (let offset = 0; offset < filteredOptions.length; offset += 1) {
      const normalized = ((index + offset * direction) % filteredOptions.length + filteredOptions.length) % filteredOptions.length;
      if (filteredOptions[normalized]?.disabled) continue;
      setActiveIndex(normalized);
      if (optionRefs.current[normalized]) optionRefs.current[normalized]?.focus();
      else window.requestAnimationFrame(() => optionRefs.current[normalized]?.focus());
      return;
    }
    setActiveIndex(-1);
  }
  function closeAndReturnFocus() {
    setOpen(false);
    setQuery("");
    setActiveIndex(-1);
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  }
  function handleMenuTab(event: React.KeyboardEvent<HTMLElement>) {
    if (searchable && event.shiftKey) {
      event.preventDefault();
      closeAndReturnFocus();
      return;
    }
    // Continue native tab order from the field, not from the body-level portal.
    if (searchable) triggerRef.current?.focus();
    setOpen(false);
    setQuery("");
    setActiveIndex(-1);
  }
  function handleTriggerKeyDown(event: React.KeyboardEvent<HTMLButtonElement>) {
    if (disabled) return;
    if (event.key === "Escape") {
      // Only an open menu owns Escape; on a closed trigger it belongs to the
      // surface around it, such as a dialog stepping back.
      if (!open) return;
      event.preventDefault();
      event.stopPropagation();
      closeAndReturnFocus();
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) || !options.length) return;
    event.preventDefault();
    setOpen(true);
    setQuery("");
    if (searchable) return;
    const selectedIndex = Math.max(0, options.findIndex((option) => option.value === value));
    let nextIndex = selectedIndex;
    if (event.key === "End" || event.key === "ArrowUp") nextIndex = options.length - 1;
    else if (event.key === "Home") nextIndex = 0;
    focusOption(nextIndex, event.key === "End" || event.key === "ArrowUp" ? -1 : 1);
  }
  function handleOptionKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, index: number) {
    if (disabled) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeAndReturnFocus();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      focusOption(index + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (searchable && index === 0) searchRef.current?.focus();
      else focusOption(index - 1, -1);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusOption(0);
    } else if (event.key === "End") {
      event.preventDefault();
      focusOption(filteredOptions.length - 1, -1);
    } else if (event.key === "Tab") {
      handleMenuTab(event);
    }
  }
  return (
    <div className={`fs-select ${className}`.trim()} ref={rootRef}>
      <button
        aria-expanded={open && !disabled}
        aria-controls={listboxId}
        aria-describedby={describedBy}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
        aria-labelledby={labelledBy}
        aria-required={required}
        aria-invalid={invalid || undefined}
        className="fs-select-trigger"
        disabled={disabled}
        id={id}
        onClick={() => { setOpen((current) => !current); setQuery(""); setActiveIndex(-1); }}
        onKeyDown={handleTriggerKeyDown}
        ref={triggerRef}
        role="combobox"
        type="button"
      >
        <span className={`fs-select-value${selected ? "" : " placeholder"}`} title={selected?.label || placeholder}>{selected?.icon ? <span className="fs-select-option-icon">{selected.icon}</span> : null}{selected?.label || placeholder}</span>
        <ChevronDown className="fs-select-chevron" aria-hidden="true" />
      </button>
      {open && !disabled ? createPortal(
        <div className={`fs-settings-portal fs-select-menu fs-multi-menu-layer${searchable ? " fs-select-menu-searchable" : ""}`} id={searchable ? undefined : listboxId} role={searchable ? undefined : "listbox"} aria-label={searchable ? undefined : t("react.shared.value1_options", "{value1} options", { value1: String(ariaLabel) })} ref={menuRef} style={menuPosition}>
          {searchable ? <label className="fs-search fs-select-search">
            <Search aria-hidden="true" />
            <input
              aria-label={t("react.shared.value1_search", "{value1} Search", { value1: ariaLabel })}
              aria-controls={listboxId}
              onChange={(event) => { setQuery(event.target.value); setActiveIndex(-1); }}
              onKeyDown={(event) => {
                if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeAndReturnFocus(); }
                else if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); focusOption(event.key === "ArrowDown" ? 0 : filteredOptions.length - 1, event.key === "ArrowDown" ? 1 : -1); }
                else if (event.key === "Tab") handleMenuTab(event);
              }}
              placeholder={t("react.shared.search_options", "Search options")}
              ref={searchRef}
              type="search"
              value={query}
            />
          </label> : null}
          <div className={searchable ? "fs-select-options" : "contents"} id={searchable ? listboxId : undefined} role={searchable ? "listbox" : undefined} aria-label={searchable ? t("react.shared.value1_options", "{value1} options", { value1: String(ariaLabel) }) : undefined} ref={optionsRef}>
          {/* The compact empty row from the shared popup contract, which the
              Select spec says its option rows follow. Without it an empty menu
              opens as a bare strip. */}
          {filteredOptions.length === 0 ? <span className="fs-combo-empty">{query.trim() ? t("react.shared.no_results", "No results") : emptyLabel}</span> : null}
          {filteredOptions.map((option, index) => (
            <button
              aria-selected={option.value === value}
              className="fs-select-option"
              disabled={option.disabled}
              key={option.value}
              onClick={() => {
                if (disabled || option.disabled) return;
                onChange(option.value);
                closeAndReturnFocus();
              }}
              onFocus={() => setActiveIndex(index)}
              onKeyDown={(event) => handleOptionKeyDown(event, index)}
              ref={(node) => { optionRefs.current[index] = node; }}
              role="option"
              tabIndex={!option.disabled && activeIndex === index ? 0 : -1}
              type="button"
            >
              <span className="fs-select-option-title" title={option.label}>{option.icon ? <span className="fs-select-option-icon">{option.icon}</span> : null}{option.label}</span><Check className="fs-select-check" aria-hidden="true" />
            </button>
          ))}
          </div>
        </div>,
        document.body,
      ) : null}
    </div>
  );
}

/**
 * Select for a form that is read through FormData. The trigger is a button, so
 * the value only reaches the submit handler through the hidden input.
 */
export function DsFormSelect({
  name,
  ...select
}: Readonly<{ name: string } & Parameters<typeof DsSelect>[0]>) {
  return (
    <>
      <input name={name} type="hidden" value={select.value} />
      <DsSelect {...select} />
    </>
  );
}

export function AdvancedTableFilters({
  fields,
  values,
  onChange,
  onClear,
  disabled = false,
  showAddFilter = true,
  fallbackFocusRef,
}: Readonly<{
  fields: DsFilterField[];
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  onClear: () => void;
  disabled?: boolean;
  showAddFilter?: boolean;
  fallbackFocusRef?: RefObject<HTMLElement | null>;
}>) {
  const { t } = useI18n();
  const [anchor, setAnchor] = useState<{ button: HTMLButtonElement; fieldKey: string | null } | null>(null);
  const [fieldKey, setFieldKey] = useState<string | null>(null);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const [query, setQuery] = useState("");
  const [tip, setTip] = useState<{ key: string; text: string; position: CSSProperties } | null>(null);
  const menuId = useId();
  const tipId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const anchorBox = useRef<DOMRect | null>(null);
  const tipTimer = useRef(0);
  const tipKey = useRef<string | null>(null);
  const pointerFocus = useRef(false);
  const field = fields.find((item) => item.key === fieldKey);
  const applied = fields.flatMap((item) => {
    const value = values[item.key];
    if (!value) return [];
    const chosen = item.multiple ? filterFieldValues(value) : [value];
    const labels = item.input
      ? [typedFilterLabel(item, value)]
      : chosen.map((selected) => item.options.find((option) => option.value === selected)?.label || selected);
    return labels.length ? [{ item, labels }] : [];
  });
  const chosen = field?.multiple ? filterFieldValues(values[field.key]) : [field ? values[field.key] : ""];
  const searchable = Boolean(field && !field.input && field.options.length >= FILTER_SEARCH_MIN);
  const searchLabel = field
    ? t("react.shared.search_value1", "Search {value1}", { value1: field.label.toLowerCase() })
    : "";
  const needle = query.trim().toLowerCase();
  const visibleOptions = searchable && needle
    ? field?.options.filter((option) => `${option.label} ${option.description || ""}`.toLowerCase().includes(needle)) ?? []
    : field?.options ?? [];

  useEffect(() => () => window.clearTimeout(tipTimer.current), []);

  useLayoutEffect(() => {
    if (!anchor) return;
    // Keep the opening coordinates when a new pill shifts or widens the trigger.
    const place = () => {
      const rect = anchorBox.current;
      const menu = menuRef.current?.getBoundingClientRect();
      if (!rect || !menu) return;
      const gap = 6;
      const inset = 8;
      const gutter = 16;
      const below = rect.bottom + gap;
      const above = rect.top - menu.height - gap;
      const fitsBelow = below + menu.height <= window.innerHeight - inset;
      const top = !fitsBelow && above >= inset ? above : Math.min(below, window.innerHeight - menu.height - inset);
      const left = Math.min(Math.max(gutter, rect.left), window.innerWidth - menu.width - gutter);
      setPosition({ top: Math.round(Math.max(inset, top)), left: Math.round(Math.max(gutter, left)) });
    };
    const reanchor = (event: Event) => {
      if (event.target instanceof Node && menuRef.current?.contains(event.target)) return;
      if (anchor.button.isConnected) {
        anchorBox.current = (anchor.fieldKey ? anchor.button.parentElement ?? anchor.button : anchor.button).getBoundingClientRect();
      }
      place();
    };
    const onOutsidePress = (event: MouseEvent) => {
      if (
        rootRef.current?.contains(event.target as Node) ||
        menuRef.current?.contains(event.target as Node)
      ) return;
      window.clearTimeout(tipTimer.current);
      tipKey.current = null;
      setTip(null);
      setAnchor(null);
      setFieldKey(null);
      setQuery("");
    };
    place();
    window.addEventListener("resize", reanchor);
    window.addEventListener("scroll", reanchor, true);
    document.addEventListener("mousedown", onOutsidePress);
    return () => {
      window.removeEventListener("resize", reanchor);
      window.removeEventListener("scroll", reanchor, true);
      document.removeEventListener("mousedown", onOutsidePress);
    };
  }, [anchor, fieldKey, visibleOptions.length]);

  function hideTip(key?: string) {
    if (key && tipKey.current !== key) return;
    window.clearTimeout(tipTimer.current);
    tipKey.current = null;
    setTip(null);
  }

  function tipEvents(key: string, text: string, truncatedOnly = false) {
    function reveal(button: HTMLButtonElement, immediate: boolean) {
      if (!text) return;
      if (truncatedOnly && !Array.from(button.querySelectorAll(".fs-filter-menu-label, .fs-filter-menu-description"))
        .some((label) => label.scrollWidth > label.clientWidth)) return;

      hideTip();
      tipKey.current = key;
      const show = () => {
        if (!button.isConnected) return;
        const rect = button.getBoundingClientRect();
        const position = {
          "--filter-tooltip-left": `${rect.left}px`,
          ...(rect.top > 64 ? { bottom: window.innerHeight - rect.top } : { top: rect.bottom }),
        } as CSSProperties;
        setTip({ key, text, position });
      };
      if (immediate) show();
      else tipTimer.current = window.setTimeout(show, FILTER_TOOLTIP_DELAY);
    }
    return {
      "aria-describedby": tip?.key === key ? tipId : undefined,
      onBlur: () => hideTip(key),
      onFocus: (event: React.FocusEvent<HTMLButtonElement>) => {
        if (!pointerFocus.current && event.currentTarget.matches(":focus-visible")) reveal(event.currentTarget, true);
      },
      onPointerEnter: (event: React.PointerEvent<HTMLButtonElement>) => {
        if (event.pointerType !== "touch") reveal(event.currentTarget, false);
      },
      onPointerLeave: () => hideTip(key),
    };
  }

  function focusFirst(selector: string, previousField?: string) {
    window.requestAnimationFrame(() => {
      const menu = menuRef.current;
      const candidates = Array.from(menu?.querySelectorAll<HTMLElement>(selector) ?? []);
      (candidates.find((item) => previousField && item.dataset.filterField === previousField) ?? candidates[0] ?? menu)?.focus();
    });
  }

  function showField(key: string) {
    hideTip();
    setFieldKey(key);
    setQuery("");
    focusFirst("[data-filter-search], [data-filter-value]:not(:disabled)");
  }

  function backToFields() {
    hideTip();
    setFieldKey(null);
    setQuery("");
    focusFirst("[data-filter-field]", fieldKey ?? undefined);
  }

  function returnFocus(button?: HTMLButtonElement) {
    (button?.isConnected ? button : triggerRef.current ?? fallbackFocusRef?.current)?.focus();
  }

  function closeMenu(restoreFocus = false) {
    const button = anchor?.button;
    hideTip();
    setAnchor(null);
    setFieldKey(null);
    setQuery("");
    if (restoreFocus) window.requestAnimationFrame(() => returnFocus(button));
  }

  function toggleMenu(button: HTMLButtonElement, key: string | null) {
    if (anchor?.button === button) {
      closeMenu();
      return;
    }
    anchorBox.current = (key ? button.parentElement ?? button : button).getBoundingClientRect();
    hideTip();
    setAnchor({ button, fieldKey: key });
    if (key) showField(key);
    else {
      setFieldKey(null);
      setQuery("");
      focusFirst("[data-filter-field]");
    }
  }

  function moveFocus(key: string) {
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLElement>("[data-filter-nav]:not(:disabled)") || []);
    if (!items.length) return;
    const current = items.indexOf(document.activeElement as HTMLElement);
    let next = 0;
    if (key === "End") next = items.length - 1;
    else if (key === "ArrowDown") next = (current + 1) % items.length;
    else if (key === "ArrowUp") next = current <= 0 ? items.length - 1 : current - 1;
    items[next]?.focus();
  }

  function handleMenuKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement;
    const input = target instanceof HTMLInputElement ? target : null;
    if (event.key === "Tab") {
      returnFocus(anchor?.button);
      closeMenu();
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (field && !anchor?.fieldKey) backToFields();
      else closeMenu(true);
    } else if ((event.key === "ArrowLeft" && field && !input) ||
      (event.key === "Backspace" && input?.hasAttribute("data-filter-search") && !input.value)) {
      event.preventDefault();
      backToFields();
    } else if (event.key === "ArrowRight" && target.dataset.filterField) {
      event.preventDefault();
      showField(target.dataset.filterField);
    } else if (FILTER_NAV_KEYS.has(event.key)) {
      // Preserve caret navigation and native stepping in typed filter fields.
      if (input && (event.key === "Home" || event.key === "End" || NATIVE_STEP_INPUT_TYPES.has(input.type))) return;
      event.preventDefault();
      moveFocus(event.key);
    } else if (searchable && target.hasAttribute("data-filter-value") &&
      event.key.length === 1 && event.key !== " " && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      setQuery((current) => current + event.key);
      menuRef.current?.querySelector<HTMLInputElement>("[data-filter-search]")?.focus();
    }
  }

  function focusPointerRow(event: React.PointerEvent<HTMLDivElement>) {
    if (event.pointerType === "touch") return;
    const row = (event.target as HTMLElement).closest<HTMLButtonElement>("button.fs-filter-menu-item:not(:disabled)");
    if (!row || document.activeElement === row) return;
    pointerFocus.current = true;
    row.focus({ preventScroll: true });
    pointerFocus.current = false;
  }

  function choose(option: DsOption) {
    if (!field) return;
    if (field.multiple) {
      const next = chosen.includes(option.value) ? chosen.filter((value) => value !== option.value) : [...chosen, option.value];
      onChange(field.key, next.join(","));
    } else {
      if (values[field.key] !== option.value) onChange(field.key, option.value);
      closeMenu(true);
    }
  }

  return (
    <div className="fs-active-filters" ref={rootRef}>
      {applied.map(({ item, labels }) => {
        const fullLabel = labels.join(", ");
        const label = labels.length >= 3 ? t("react.shared.selected_count", "{count} selected", { count: labels.length }) : fullLabel;
        const expanded = anchor?.fieldKey === item.key;
        return (
          <span className="fs-applied-filter" key={item.key}>
            <button
              {...(!anchor ? tipEvents(`pill-${item.key}`, fullLabel, labels.length < 3) : {})}
              aria-controls={expanded ? menuId : undefined}
              aria-expanded={expanded}
              aria-haspopup="menu"
              className="fs-applied-filter-label"
              disabled={disabled}
              onClick={(event) => toggleMenu(event.currentTarget, item.key)}
              type="button"
            >
              <span className="fs-filter-menu-label">{item.label} · {label}</span>
            </button>
            <button
              aria-label={t("react.shared.remove_value1_filter", "Remove {value1} filter", { value1: item.label })}
              onClick={() => {
                closeMenu();
                onChange(item.key, "");
                returnFocus();
              }}
              type="button"
            ><X aria-hidden="true" /></button>
          </span>
        );
      })}
      {showAddFilter ? <div className="fs-filter-popover-wrap">
        <button
          aria-controls={anchor && !anchor.fieldKey ? menuId : undefined}
          aria-expanded={Boolean(anchor && !anchor.fieldKey)}
          aria-haspopup="menu"
          className="fs-filter-chip add"
          disabled={disabled}
          onClick={(event) => toggleMenu(event.currentTarget, null)}
          ref={triggerRef}
          type="button"
        >
          <Plus aria-hidden="true" />
          {t("react.shared.add_filter", "Add filter")}
        </button>
      </div> : null}
      {anchor ? createPortal(
        <div
          aria-label={t("react.shared.choose_a_filter", "Choose a filter")}
          className="fs-filter-menu"
          id={menuId}
          onKeyDown={handleMenuKeyDown}
          onPointerMove={focusPointerRow}
          ref={menuRef}
          role="menu"
          style={position}
          tabIndex={-1}
        >
          {!field ? (
            <div role="none">
              <h2>{t("react.shared.filter_by_field", "Filter by field")}</h2>
              {fields.map((item) => {
                const count = applied.find((entry) => entry.item.key === item.key)?.labels.length;
                const countId = `${menuId}-field-${encodeURIComponent(item.key)}-count`;
                return (
                  <button
                    {...tipEvents(`field-${item.key}`, item.label, true)}
                    aria-describedby={count ? countId : undefined}
                    aria-label={item.label}
                    className="fs-filter-menu-item"
                    data-filter-field={item.key}
                    data-filter-nav=""
                    key={item.key}
                    onClick={() => showField(item.key)}
                    role="menuitem"
                    tabIndex={-1}
                    type="button"
                  >
                    <FilterMenuText option={item} />
                    {count ? <span className="fs-filter-field-count" id={countId}>{count}</span> : null}
                    <ChevronRight className="fs-filter-chevron" aria-hidden="true" />
                  </button>
                );
              })}
            </div>
          ) : (
            <div role="none">
              <button
                aria-label={`${t("react.shared.all_fields", "All fields")} · ${field.label}`}
                className="fs-filter-menu-item fs-filter-menu-back"
                data-filter-nav=""
                onClick={backToFields}
                role="menuitem"
                tabIndex={-1}
                type="button"
              >
                <ChevronLeft aria-hidden="true" />
                {field.label}
              </button>
              {field.input ? (
                <FilterInputStage
                  field={field}
                  key={field.key}
                  onApply={(value) => {
                    onChange(field.key, value);
                    closeMenu(true);
                  }}
                  value={values[field.key] || ""}
                />
              ) : null}
              {searchable ? (
                <div className="fs-combo-field fs-filter-menu-search">
                  <Search className="fs-multi-search" aria-hidden="true" />
                  <input
                    aria-label={searchLabel}
                    autoComplete="off"
                    data-filter-nav=""
                    data-filter-search=""
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder={searchLabel}
                    tabIndex={-1}
                    type="search"
                    value={query}
                  />
                </div>
              ) : null}
              {!field.input ? (
                <div className="fs-filter-menu-options" role="none">
                  {visibleOptions.length === 0 ? <span className="fs-combo-empty">{t("react.shared.no_matching_values", "No matching values")}</span> : null}
                  {visibleOptions.map((option) => {
                    const checked = chosen.includes(option.value);
                    const descriptionId = `${menuId}-option-${encodeURIComponent(option.value)}-description`;
                    return (
                      <button
                        {...tipEvents(`option-${field.key}-${option.value}`, filterMenuTitle(option), true)}
                        aria-checked={field.multiple ? checked : undefined}
                        aria-current={!field.multiple && checked ? true : undefined}
                        aria-describedby={option.description ? descriptionId : undefined}
                        aria-label={option.label}
                        className="fs-filter-menu-item"
                        data-filter-nav=""
                        data-filter-value={option.value}
                        disabled={option.disabled}
                        key={option.value}
                        onClick={() => choose(option)}
                        role={field.multiple ? "menuitemcheckbox" : "menuitem"}
                        tabIndex={-1}
                        type="button"
                      >
                        <FilterMenuText descriptionId={descriptionId} option={option} />
                        {checked ? <Check className="fs-filter-check" aria-hidden="true" /> : null}
                      </button>
                    );
                  })}
                </div>
              ) : null}
            </div>
          )}
        </div>,
        document.body,
      ) : null}
      {tip ? createPortal(
        <span className="fs-filter-tooltip" id={tipId} role="tooltip" style={tip.position}>{tip.text}</span>,
        document.body,
      ) : null}
      {applied.length ? (
        <button
          className="fs-clear-filters"
          onClick={() => {
            closeMenu();
            onClear();
            returnFocus();
          }}
          type="button"
        >{t("react.shared.clear_all", "Clear all")}</button>
      ) : null}
    </div>
  );
}

/**
 * Async options for a Combobox (`combobox.md` Loading state). The label names
 * what is loading ("Loading projects…"), never a bare "Loading…", so it comes
 * with `loading` and is required whenever `loading` is passed.
 */
type ComboboxLoadingProps =
  | { loading?: undefined; loadingLabel?: undefined }
  | { loading: boolean; loadingLabel: string };

/**
 * `busy` covers the request and the loader's minimum display time; the row
 * itself paints only after the shared 400 ms delay (`loaderShown`).
 */
function useComboboxLoading(loading: boolean | undefined) {
  const pending = Boolean(loading);
  const loaderShown = useLoaderDelay(pending);
  return { busy: pending || loaderShown, loaderShown };
}

/**
 * The first row of the open list while options load: the round mono S loader
 * and a named noun. It is not an option, so it is never highlighted or chosen;
 * the field's live region announces it once instead of a nested status role.
 */
function ComboboxLoadingRow({ label }: Readonly<{ label: string }>) {
  return (
    <div className="fs-combobox-loading" role="presentation">
      <DsLoader variant="orbit-inline" />
      <span>{label}</span>
    </div>
  );
}

export function SingleCombobox({
  ariaLabel,
  value,
  options,
  onChange,
  placeholder: suppliedPlaceholder,
  disabled,
  id,
  loading,
  loadingLabel,
}: Readonly<{
  ariaLabel: string;
  value: string;
  options: DsOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  /** Addresses the input so a visible `<label htmlFor>` names it. */
  id?: string;
} & ComboboxLoadingProps>) {
  const { t } = useI18n();
  const placeholder = suppliedPlaceholder ?? t("react.shared.search_options", "Search options");
  const { busy, loaderShown } = useComboboxLoading(loading);
  const [open, setOpen] = useState(false);
  const listboxId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const selected = options.find((option) => option.value === value);
  const [query, setQuery] = useState(selected?.label || value);
  const [activeIndex, setActiveIndex] = useState(-1);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle || needle === (selected?.label || "").toLowerCase()) return options;
    return options.filter((option) => `${option.label} ${option.description || ""}`.toLowerCase().includes(needle));
  }, [options, query, selected?.label]);
  const normalizedActiveIndex = activeIndex < filtered.length ? activeIndex : -1;
  const moveActive = createActiveIndexMover(filtered.length, () => setOpen(true), setActiveIndex);
  // During the loader's 400 ms delay there is nothing honest to list yet: no
  // options and no "No results", so the list stays closed until the row paints.
  const menuHidden = busy && !loaderShown && filtered.length === 0;
  const menuOpen = open && !disabled && !menuHidden;
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  function choose(option: DsOption) {
    onChange(option.value);
    setQuery(option.label);
    setOpen(false);
    setActiveIndex(-1);
    window.requestAnimationFrame(() => inputRef.current?.focus());
  }
  return (
    <div className="fs-combo" ref={rootRef}>
      <div className="fs-combo-field">
        <Search className="fs-multi-search" aria-hidden="true" />
        <input
          aria-activedescendant={normalizedActiveIndex >= 0 ? `${listboxId}-option-${normalizedActiveIndex}` : undefined}
          aria-autocomplete="list"
          aria-controls={listboxId}
          aria-expanded={open && !menuHidden}
          aria-label={ariaLabel}
          autoComplete="off"
          disabled={disabled}
          id={id}
          onChange={(event) => { setQuery(event.target.value); setOpen(true); setActiveIndex(-1); }}
          onFocus={(event) => { setQuery(selected?.label || value); setOpen(true); setActiveIndex(-1); event.currentTarget.select(); }}
          onKeyDown={(event) => {
            if (event.key === "Escape") { event.preventDefault(); setOpen(false); setActiveIndex(-1); }
            else if (event.key === "ArrowDown") { event.preventDefault(); moveActive(normalizedActiveIndex + 1); }
            else if (event.key === "ArrowUp") { event.preventDefault(); moveActive(normalizedActiveIndex < 0 ? filtered.length - 1 : normalizedActiveIndex - 1); }
            else if (event.key === "Home" && open) { event.preventDefault(); moveActive(0); }
            else if (event.key === "End" && open) { event.preventDefault(); moveActive(filtered.length - 1); }
            else if (event.key === "Enter" && open && normalizedActiveIndex >= 0 && filtered[normalizedActiveIndex]) { event.preventDefault(); choose(filtered[normalizedActiveIndex]); }
          }}
          placeholder={placeholder}
          ref={inputRef}
          role="combobox"
          value={open ? query : selected?.label || value}
        />
      </div>
      {menuOpen ? (
        <div className="fs-combo-menu" id={listboxId} role="listbox" aria-busy={busy || undefined} aria-label={t("react.shared.value1_options", "{value1} options", { value1: String(ariaLabel) })}>
          {loaderShown ? <ComboboxLoadingRow label={loadingLabel ?? ""} /> : null}
          {filtered.map((option, index) => (
            <button aria-selected={option.value === value} className="fs-combo-option" data-active={normalizedActiveIndex === index ? "true" : undefined} id={`${listboxId}-option-${index}`} key={option.value} onClick={() => choose(option)} onMouseDown={(event) => event.preventDefault()} role="option" tabIndex={-1} type="button">
              <span title={option.label}>{option.label}</span><Check className="fs-select-check" aria-hidden="true" />
            </button>
          ))}
          {filtered.length || busy ? null : <span className="fs-combo-empty">{t("react.shared.no_results", "No results")}</span>}
        </div>
      ) : null}
      <span className="fs-sr-only" aria-live="polite">{menuOpen && loaderShown ? loadingLabel : ""}</span>
    </div>
  );
}

export function MultiCombobox({
  ariaLabel,
  options,
  selected,
  onChange,
  placeholder,
  disabled,
  required,
  loading,
  loadingLabel,
}: Readonly<{
  ariaLabel: string;
  options: DsOption[];
  selected: string[];
  onChange: (values: string[]) => void;
  placeholder: string;
  disabled?: boolean;
  required?: boolean;
} & ComboboxLoadingProps>) {
  const { t } = useI18n();
  const { busy, loaderShown } = useComboboxLoading(loading);
  const [open, setOpen] = useState(false);
  const listboxId = useId();
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const fieldRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [activeIndex, setActiveIndex] = useState(-1);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return options.filter((option) => !needle || `${option.label} ${option.description || ""}`.toLowerCase().includes(needle));
  }, [options, query]);
  const normalizedActiveIndex = activeIndex < filtered.length ? activeIndex : -1;
  const moveActive = createActiveIndexMover(filtered.length, () => setOpen(true), setActiveIndex);
  const selectedOptions = selected.map((value) => options.find((option) => option.value === value)).filter((option): option is DsOption => Boolean(option));
  // As in the single Combobox: nothing to list during the loader's delay.
  const menuHidden = busy && !loaderShown && filtered.length === 0;
  const menuOpen = open && !disabled && !menuHidden;
  const menuPosition = useAnchoredMenuPosition({
    open: open && !menuHidden,
    anchorRef: fieldRef,
    menuRef,
    insideRefs: [rootRef],
    onDismiss: () => setOpen(false),
    maxHeightToken: "--fs-combobox-menu-max-height",
    // The loading row adds or removes a row without changing the option count.
    remeasureKey: `${filtered.length}:${loaderShown}`,
  });
  function toggle(value: string) {
    onChange(selected.includes(value) ? selected.filter((item) => item !== value) : [...selected, value]);
  }
  return (
    <div className="fs-multi" data-has-query={query.length > 0 ? "true" : "false"} data-has-selection={selectedOptions.length > 0 ? "true" : "false"} ref={rootRef}>
      <div className="fs-multi-field" ref={fieldRef}>
        <Search className="fs-multi-search" aria-hidden="true" />
        {selectedOptions.length ? <span className="fs-multi-summary">{selectedOptions.map((option) => <span className="fs-multi-chip" key={option.value}><span className="fs-multi-chip-label">{option.label}</span><button aria-label={t("react.shared.remove_value1", "Remove {value1}", { value1: String(option.label) })} className="fs-multi-chip-remove" onClick={() => { onChange(selected.filter((value) => value !== option.value)); inputRef.current?.focus(); }} type="button"><X aria-hidden="true" /></button></span>)}</span> : null}
        <input
          aria-autocomplete="list"
          aria-activedescendant={normalizedActiveIndex >= 0 ? `${listboxId}-option-${normalizedActiveIndex}` : undefined}
          aria-controls={listboxId}
          aria-expanded={open && !menuHidden}
          aria-label={ariaLabel}
          aria-required={required}
          disabled={disabled}
          onChange={(event) => { setQuery(event.target.value); setOpen(true); setActiveIndex(-1); }}
          onFocus={() => { setOpen(true); setActiveIndex(-1); }}
          onKeyDown={(event) => {
            if (event.key === "Escape") { event.preventDefault(); setOpen(false); setActiveIndex(-1); }
            else if (event.key === "ArrowDown") { event.preventDefault(); moveActive(normalizedActiveIndex + 1); }
            else if (event.key === "ArrowUp") { event.preventDefault(); moveActive(normalizedActiveIndex < 0 ? filtered.length - 1 : normalizedActiveIndex - 1); }
            else if (event.key === "Home" && open) { event.preventDefault(); moveActive(0); }
            else if (event.key === "End" && open) { event.preventDefault(); moveActive(filtered.length - 1); }
            else if (event.key === "Enter" && open && normalizedActiveIndex >= 0 && filtered[normalizedActiveIndex]) { event.preventDefault(); toggle(filtered[normalizedActiveIndex].value); }
            else if (event.key === "Backspace" && !query && selected.length) onChange(selected.slice(0, -1));
          }}
          placeholder={selectedOptions.length ? "" : placeholder}
          ref={inputRef}
          role="combobox"
          value={query}
        />
        {/* Chosen values stay visible while options load; Clear does not. */}
        {selected.length && !loaderShown ? <button aria-label={t("react.shared.clear_value1", "Clear {value1}", { value1: String(ariaLabel.toLowerCase()) })} className="fs-icon-button" onClick={() => { onChange([]); setQuery(""); setActiveIndex(-1); inputRef.current?.focus(); }} type="button"><X aria-hidden="true" /></button> : null}
      </div>
      {menuOpen ? createPortal(
        <div className="fs-settings-portal fs-multi-menu fs-multi-menu-layer" id={listboxId} ref={menuRef} role="listbox" aria-busy={busy || undefined} aria-label={t("react.shared.value1_options", "{value1} options", { value1: String(ariaLabel) })} aria-multiselectable="true" style={menuPosition}>
          {loaderShown ? <ComboboxLoadingRow label={loadingLabel ?? ""} /> : null}
          {filtered.map((option, index) => (
            <button aria-selected={selected.includes(option.value)} className="fs-multi-option" data-active={normalizedActiveIndex === index ? "true" : undefined} id={`${listboxId}-option-${index}`} key={option.value} onClick={() => { toggle(option.value); inputRef.current?.focus(); }} onMouseDown={(event) => event.preventDefault()} role="option" tabIndex={-1} type="button">
              <span title={option.description ? `${option.label} · ${option.description}` : option.label}>{option.label}{option.description ? <small className="fs-field-helper"> · {option.description}</small> : null}</span>
              <Check className="fs-select-check" aria-hidden="true" />
            </button>
          ))}
          {filtered.length || busy ? null : <span className="fs-field-helper">{t("react.shared.no_results", "No results")}</span>}
        </div>,
        document.body,
      ) : null}
      <span className="fs-sr-only" aria-live="polite">{menuOpen && loaderShown ? loadingLabel : t("react.shared.selected_count", "{count} selected", { count: selected.length })}</span>
    </div>
  );
}
