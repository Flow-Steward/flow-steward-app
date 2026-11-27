/** Single and multi-value comboboxes of the design system. */
import { useI18n } from "@/lib/i18n/useI18n";
import { Check, Search, User, X } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { createActiveIndexMover, useAnchoredMenuPosition, type DsOption } from "@/components/shared/design-system-menu";
import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";

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
  selectionLayout = "chips",
}: Readonly<{
  ariaLabel: string;
  options: DsOption[];
  selected: string[];
  onChange: (values: string[]) => void;
  placeholder: string;
  disabled?: boolean;
  required?: boolean;
  /**
   * Where chosen values show. "chips" (default) puts them in the field. "list"
   * keeps the field for search only, always empty with its placeholder, and
   * lists the chosen values under it as a Selected list (icon, label, its
   * description, a remove X), so a long choice never crowds out the search.
   */
  selectionLayout?: "chips" | "list";
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
  // Set while remove() hands focus back to the search in list layout, so that
  // focus does not open the menu over the Selected list.
  const focusWithoutMenuRef = useRef(false);
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
  const listLayout = selectionLayout === "list";
  function toggle(value: string) {
    onChange(selected.includes(value) ? selected.filter((item) => item !== value) : [...selected, value]);
  }
  function remove(value: string) {
    onChange(selected.filter((item) => item !== value));
    const input = inputRef.current;
    if (listLayout) {
      // The menu opens over the Selected list: left open, the next click on
      // another row's X would land on an option and toggle someone else.
      setOpen(false);
      setActiveIndex(-1);
      if (input && document.activeElement !== input) focusWithoutMenuRef.current = true;
    }
    input?.focus();
  }
  const removeLabel = (option: DsOption) => t("react.shared.remove_value1", "Remove {value1}", { value1: String(option.label) });
  return (
    <div className="fs-multi" data-has-query={query.length > 0 ? "true" : "false"} data-has-selection={selectedOptions.length > 0 ? "true" : "false"} data-selection-layout={selectionLayout} ref={rootRef}>
      <div className="fs-multi-field" ref={fieldRef}>
        <Search className="fs-multi-search" aria-hidden="true" />
        {!listLayout && selectedOptions.length ? <span className="fs-multi-summary">{selectedOptions.map((option) => <span className="fs-multi-chip" key={option.value}><span className="fs-multi-chip-label">{option.label}</span><button aria-label={removeLabel(option)} className="fs-multi-chip-remove" onClick={() => remove(option.value)} type="button"><X aria-hidden="true" /></button></span>)}</span> : null}
        <input
          aria-autocomplete="list"
          aria-activedescendant={normalizedActiveIndex >= 0 ? `${listboxId}-option-${normalizedActiveIndex}` : undefined}
          aria-controls={listboxId}
          aria-expanded={open && !menuHidden}
          aria-label={ariaLabel}
          aria-required={required}
          disabled={disabled}
          onChange={(event) => { setQuery(event.target.value); setOpen(true); setActiveIndex(-1); }}
          onFocus={() => {
            setActiveIndex(-1);
            if (focusWithoutMenuRef.current) { focusWithoutMenuRef.current = false; return; }
            setOpen(true);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") { event.preventDefault(); setOpen(false); setActiveIndex(-1); }
            else if (event.key === "ArrowDown") { event.preventDefault(); moveActive(normalizedActiveIndex + 1); }
            else if (event.key === "ArrowUp") { event.preventDefault(); moveActive(normalizedActiveIndex < 0 ? filtered.length - 1 : normalizedActiveIndex - 1); }
            else if (event.key === "Home" && open) { event.preventDefault(); moveActive(0); }
            else if (event.key === "End" && open) { event.preventDefault(); moveActive(filtered.length - 1); }
            else if (event.key === "Enter" && open && normalizedActiveIndex >= 0 && filtered[normalizedActiveIndex]) { event.preventDefault(); toggle(filtered[normalizedActiveIndex].value); }
            // In list layout the chosen values sit outside the field, so an
            // empty Backspace must not silently drop the last one.
            else if (event.key === "Backspace" && !listLayout && !query && selected.length) onChange(selected.slice(0, -1));
          }}
          placeholder={listLayout || !selectedOptions.length ? placeholder : ""}
          ref={inputRef}
          role="combobox"
          value={query}
        />
        {/* Chosen values stay visible while options load; Clear does not. */}
        {!listLayout && selected.length && !loaderShown ? <button aria-label={t("react.shared.clear_value1", "Clear {value1}", { value1: String(ariaLabel.toLowerCase()) })} className="fs-icon-button" onClick={() => { onChange([]); setQuery(""); setActiveIndex(-1); inputRef.current?.focus(); }} type="button"><X aria-hidden="true" /></button> : null}
      </div>
      {listLayout && selectedOptions.length ? (
        <ul aria-label={ariaLabel} className="fs-selected-list">
          {selectedOptions.map((option) => (
            <li className="fs-selected-row" key={option.value}>
              <User aria-hidden="true" className="fs-selected-row-icon" />
              <span className="fs-selected-row-text" title={option.description ? `${option.label} · ${option.description}` : option.label}>
                <span className="fs-selected-row-label">{option.label}</span>
                {option.description ? <span className="fs-selected-row-description">{option.description}</span> : null}
              </span>
              <button aria-label={removeLabel(option)} className="fs-multi-chip-remove" disabled={disabled} onClick={() => remove(option.value)} type="button"><X aria-hidden="true" /></button>
            </li>
          ))}
        </ul>
      ) : null}
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
