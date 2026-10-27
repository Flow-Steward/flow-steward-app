import { useI18n } from "@/lib/i18n/useI18n";
import { Check, ChevronDown, ChevronLeft, Search, X } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

/** `icon` is a small mark drawn before the label (an extension's logo tile). */
export type DsOption = { value: string; label: string; description?: string; disabled?: boolean; icon?: ReactNode };
/**
 * `multiple` lets one field hold several values: its value in
 * `AdvancedTableFilters` is then a comma-separated list of option values.
 */
export type DsFilterField = { key: string; label: string; options: DsOption[]; multiple?: boolean };

/** A filter field with more options than this gets a search box above its list. */
const FILTER_SEARCH_THRESHOLD = 10;

/** The arrow keys walk the back row, the search box and the options, in order. */
const FILTER_NAV_KEYS = new Set(["ArrowDown", "ArrowUp", "Home", "End"]);

/** One line per label: a long one ends in an ellipsis, and `title` keeps it whole. */
function FilterMenuText({ option }: Readonly<{ option: Pick<DsOption, "label" | "description"> }>) {
  return (
    <span className="fs-filter-menu-text">
      <span className="fs-filter-menu-label">{option.label}</span>
      {option.description ? <span className="fs-filter-menu-description">{option.description}</span> : null}
    </span>
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
        <span className={`fs-select-value${selected ? "" : " placeholder"}`}>{selected?.icon ? <span className="fs-select-option-icon">{selected.icon}</span> : null}{selected?.label || placeholder}</span>
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
              <span className="fs-select-option-title">{option.icon ? <span className="fs-select-option-icon">{option.icon}</span> : null}{option.label}</span><Check className="fs-select-check" aria-hidden="true" />
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
}: Readonly<{
  fields: DsFilterField[];
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
  onClear: () => void;
  disabled?: boolean;
}>) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [fieldKey, setFieldKey] = useState<string | null>(null);
  const menuId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const field = fields.find((item) => item.key === fieldKey);
  const activeFields = fields.filter((item) => values[item.key]);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const [query, setQuery] = useState("");
  const searchable = Boolean(field && field.options.length > FILTER_SEARCH_THRESHOLD);
  const visibleOptions = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!field || !searchable || !needle) return field?.options || [];
    return field.options.filter((option) => `${option.label} ${option.description || ""}`.toLowerCase().includes(needle));
  }, [field, query, searchable]);

  /** Every stage change starts the value list unfiltered. */
  function showField(key: string | null) {
    setFieldKey(key);
    setQuery("");
  }

  useEffect(() => {
    if (!open) return;
    // The toolbar sits inside `.fs-table-card`, which is `overflow: hidden`, so
    // an inline popup is cut off at the card's bottom edge. Canon requires a
    // table popup to live in a viewport layer and flip when it would collide,
    // which is the same contract the row menu already follows.
    const update = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      const menuRect = menuRef.current?.getBoundingClientRect();
      if (!rect || !menuRect) return;
      const popupGap = 6;
      const viewportInset = 8;
      // The menu is as wide as a phone allows minus a 16px gutter each side,
      // so it keeps that gutter when it is pushed in from an edge.
      const viewportGutter = 16;
      const belowTop = rect.bottom + popupGap;
      const aboveTop = rect.top - menuRect.height - popupGap;
      const collidesWithViewport = belowTop + menuRect.height > window.innerHeight - viewportInset;
      const opensAbove = aboveTop >= viewportInset && collidesWithViewport;
      const top = opensAbove
        ? aboveTop
        : Math.min(belowTop, window.innerHeight - menuRect.height - viewportInset);
      const left = Math.min(
        Math.max(viewportGutter, rect.left),
        window.innerWidth - menuRect.width - viewportGutter,
      );
      setPosition({ top: Math.round(Math.max(viewportInset, top)), left: Math.round(left) });
    };
    const close = (event: MouseEvent) => {
      // The menu is portalled out of the wrapper, so it needs its own hit test.
      if (
        !rootRef.current?.contains(event.target as Node) &&
        !menuRef.current?.contains(event.target as Node)
      ) {
        setOpen(false);
        showField(null);
      }
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    document.addEventListener("mousedown", close);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      document.removeEventListener("mousedown", close);
    };
    // `fieldKey` re-runs the measurement because the value stage is a different
    // height than the field stage, and so does a search that shortens the list.
  }, [fieldKey, open, visibleOptions.length]);

  function focusFirst(selector: string) {
    window.requestAnimationFrame(() => menuRef.current?.querySelector<HTMLElement>(selector)?.focus());
  }

  function closeAndReturnFocus() {
    setOpen(false);
    showField(null);
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  }

  function moveFocus(key: string) {
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLElement>("[data-filter-nav]") || []);
    if (!items.length) return;
    const current = items.indexOf(document.activeElement as HTMLElement);
    let next = 0;
    if (key === "End") next = items.length - 1;
    else if (key === "ArrowDown") next = current < 0 ? 0 : (current + 1) % items.length;
    else if (key === "ArrowUp") next = current <= 0 ? items.length - 1 : current - 1;
    items[next]?.focus();
  }

  function handleMenuKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (FILTER_NAV_KEYS.has(event.key)) {
      // Home and End belong to the caret while the search box has focus.
      if ((event.key === "Home" || event.key === "End") && event.target instanceof HTMLInputElement) return;
      event.preventDefault();
      moveFocus(event.key);
      return;
    }
    if (event.key !== "Escape") return;
    event.preventDefault();
    if (fieldKey) {
      showField(null);
      focusFirst("[data-filter-field]");
    } else {
      closeAndReturnFocus();
    }
  }

  return (
    <>
      <div className="fs-active-filters">
        {activeFields.map((item) => {
          const chosen = item.multiple ? filterFieldValues(values[item.key]) : [values[item.key]];
          const chosenLabels = chosen.map((value) => item.options.find((option) => option.value === value)?.label || value);
          return (
            <span className="fs-applied-filter" key={item.key}>
              <span>{item.label} · {chosenLabels.join(", ")}</span>
              <button aria-label={t("react.shared.remove_value1_filter", "Remove {value1} filter", { value1: String(item.label) })} onClick={() => onChange(item.key, "")} type="button"><X aria-hidden="true" /></button>
            </span>
          );
        })}
      </div>
      <div className="fs-filter-popover-wrap" ref={rootRef}>
        <button
          aria-controls={menuId}
          aria-expanded={open}
          className="fs-filter-chip add"
          disabled={disabled}
          onClick={() => {
            const nextOpen = !open;
            setOpen(nextOpen);
            showField(null);
            if (nextOpen) focusFirst("[data-filter-field]");
          }}
          ref={triggerRef}
          type="button"
        >
          <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>
          {t("react.shared.add_filter", "Add filter")} </button>
        {open ? createPortal(
          <div aria-label={t("react.shared.choose_a_filter", "Choose a filter")} className="fs-filter-menu" id={menuId} onKeyDown={handleMenuKeyDown} ref={menuRef} role="menu" style={position} tabIndex={-1}>
            {!field ? (
              <div role="none">
                <h2>{t("react.shared.filter_by_field", "Filter by field")}</h2>
                {fields.map((item) => (
                  /* No glyph: the field stage carried two hardcoded icons keyed
                     on the field name, so every field but "role" got a person.
                     The value stage has never had icons either. */
                  <button className="fs-filter-menu-item" data-filter-field={item.key} data-filter-nav="" key={item.key} onClick={() => { showField(item.key); focusFirst("[data-filter-search], [data-filter-value]"); }} role="menuitem" title={item.label} type="button">
                    <FilterMenuText option={item} />
                  </button>
                ))}
              </div>
            ) : (
              <div role="none">
                <button className="fs-filter-menu-item fs-filter-menu-back" data-filter-nav="" onClick={() => { showField(null); focusFirst("[data-filter-field]"); }} role="menuitem" type="button"><ChevronLeft aria-hidden="true" />{t("react.shared.all_fields", "All fields")}</button>
                <h2>{t("react.shared.filter_by", "Filter by")} {field.label.toLowerCase()}</h2>
                {/* A long list gets the Combobox search field; a short one is
                    read at a glance and would only lose a row to it. */}
                {searchable ? (
                  <div className="fs-combo-field fs-filter-menu-search">
                    <Search className="fs-multi-search" aria-hidden="true" />
                    <input
                      aria-label={t("react.shared.search_value1", "Search {value1}", { value1: field.label.toLowerCase() })}
                      autoComplete="off"
                      data-filter-nav=""
                      data-filter-search=""
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder={t("react.shared.search_options", "Search options")}
                      type="search"
                      value={query}
                    />
                  </div>
                ) : null}
                <div className="fs-filter-menu-options" role="none">
                  {visibleOptions.length === 0 ? <span className="fs-combo-empty">{t("react.shared.no_results", "No results")}</span> : null}
                  {field.multiple
                    ? visibleOptions.map((option) => {
                      // Several values: each item toggles, and the menu stays open.
                      const chosen = filterFieldValues(values[field.key]);
                      const checked = chosen.includes(option.value);
                      const next = checked ? chosen.filter((value) => value !== option.value) : [...chosen, option.value];
                      return <button aria-checked={checked} className="fs-filter-menu-item" data-filter-nav="" data-filter-value={option.value} key={option.value} onClick={() => onChange(field.key, next.join(","))} role="menuitemcheckbox" title={filterMenuTitle(option)} type="button"><FilterMenuText option={option} />{checked ? <Check aria-hidden="true" /> : null}</button>;
                    })
                    : visibleOptions.map((option) => <button className="fs-filter-menu-item" data-filter-nav="" data-filter-value={option.value} key={option.value} onClick={() => { onChange(field.key, option.value); closeAndReturnFocus(); }} role="menuitem" title={filterMenuTitle(option)} type="button"><FilterMenuText option={option} /></button>)}
                </div>
              </div>
            )}
          </div>,
          document.body,
        ) : null}
      </div>
      {activeFields.length ? <button className="fs-clear-filters" onClick={onClear} type="button">{t("react.shared.clear_all", "Clear all")}</button> : null}
    </>
  );
}

export function SingleCombobox({
  ariaLabel,
  value,
  options,
  onChange,
  placeholder: suppliedPlaceholder,
  disabled,
}: Readonly<{
  ariaLabel: string;
  value: string;
  options: DsOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
}>) {
  const { t } = useI18n();
  const placeholder = suppliedPlaceholder ?? t("react.shared.search_options", "Search options");
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
          aria-expanded={open}
          aria-label={ariaLabel}
          autoComplete="off"
          disabled={disabled}
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
      {open && !disabled ? (
        <div className="fs-combo-menu" id={listboxId} role="listbox" aria-label={t("react.shared.value1_options", "{value1} options", { value1: String(ariaLabel) })}>
          {filtered.length ? filtered.map((option, index) => (
            <button aria-selected={option.value === value} className="fs-combo-option" data-active={normalizedActiveIndex === index ? "true" : undefined} id={`${listboxId}-option-${index}`} key={option.value} onClick={() => choose(option)} onMouseDown={(event) => event.preventDefault()} role="option" tabIndex={-1} type="button">
              <span>{option.label}</span><Check className="fs-select-check" aria-hidden="true" />
            </button>
          )) : <span className="fs-combo-empty">{t("react.shared.no_results", "No results")}</span>}
        </div>
      ) : null}
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
}: Readonly<{
  ariaLabel: string;
  options: DsOption[];
  selected: string[];
  onChange: (values: string[]) => void;
  placeholder: string;
  disabled?: boolean;
  required?: boolean;
}>) {
  const { t } = useI18n();
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
  const menuPosition = useAnchoredMenuPosition({
    open,
    anchorRef: fieldRef,
    menuRef,
    insideRefs: [rootRef],
    onDismiss: () => setOpen(false),
    maxHeightToken: "--fs-combobox-menu-max-height",
    remeasureKey: filtered.length,
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
          aria-expanded={open}
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
        {selected.length ? <button aria-label={t("react.shared.clear_value1", "Clear {value1}", { value1: String(ariaLabel.toLowerCase()) })} className="fs-icon-button" onClick={() => { onChange([]); setQuery(""); setActiveIndex(-1); inputRef.current?.focus(); }} type="button"><X aria-hidden="true" /></button> : null}
      </div>
      {open && !disabled ? createPortal(
        <div className="fs-settings-portal fs-multi-menu fs-multi-menu-layer" id={listboxId} ref={menuRef} role="listbox" aria-label={t("react.shared.value1_options", "{value1} options", { value1: String(ariaLabel) })} aria-multiselectable="true" style={menuPosition}>
          {filtered.length ? filtered.map((option, index) => (
            <button aria-selected={selected.includes(option.value)} className="fs-multi-option" data-active={normalizedActiveIndex === index ? "true" : undefined} id={`${listboxId}-option-${index}`} key={option.value} onClick={() => { toggle(option.value); inputRef.current?.focus(); }} onMouseDown={(event) => event.preventDefault()} role="option" tabIndex={-1} type="button">
              <span>{option.label}{option.description ? <small className="fs-field-helper"> · {option.description}</small> : null}</span>
              <Check className="fs-select-check" aria-hidden="true" />
            </button>
          )) : <span className="fs-field-helper">{t("react.shared.no_results", "No results")}</span>}
        </div>,
        document.body,
      ) : null}
      <span className="fs-sr-only" aria-live="polite">{t("react.shared.selected_count", "{count} selected", { count: selected.length })}</span>
    </div>
  );
}
