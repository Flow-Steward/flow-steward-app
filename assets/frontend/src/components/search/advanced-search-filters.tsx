import { useI18n } from "@/lib/i18n/useI18n";
import { Check, ChevronDown, Plus, X } from "lucide-react";

import {
  availableFilterKeys,
  filterDefinition,
  filterValueLabel,
  isFilterRemovable,
  type FilterAction,
  type FilterDefinition,
  type FilterKey,
  type FilterState,
} from "@/components/search/advanced-search-state";

type AdvancedSearchFiltersProps = {
  canSearchAccountWide: boolean;
  definitions: FilterDefinition[];
  dispatch: (action: FilterAction) => void;
  state: FilterState;
};

export function AdvancedSearchFilters({
  canSearchAccountWide,
  definitions,
  dispatch,
  state,
}: Readonly<AdvancedSearchFiltersProps>) {
  const { t } = useI18n();
  const { active, menu } = state;
  const available = availableFilterKeys(definitions, active);
  const addMenuOpen = menu?.kind === "add";

  function renderValueMenu(index: number, key: FilterKey) {
    const definition = filterDefinition(definitions, key);
    if (!definition) {
      return null;
    }
    return (
      <div className="fs-advanced-search__menu" role="menu">
        <div className="fs-advanced-search__menu-label">{definition.label}</div>
        {definition.options.map((option) => {
          const selected = option.value === active[index]?.v;
          return (
            <button
              aria-checked={selected}
              className={`fs-advanced-search__menu-item${selected ? " is-selected" : ""}`}
              key={option.value}
              onClick={() => dispatch({ type: "set-value", index, value: option.value })}
              role="menuitemradio"
              type="button"
            >
              <span>{option.label}</span>
              <Check aria-hidden="true" className="fs-advanced-search__menu-check" />
            </button>
          );
        })}
      </div>
    );
  }

  return (
    <div className="fs-advanced-search__filter-zone">
      <div className="fs-advanced-search__filter-row">
        {active.map((filter, index) => {
          const definition = filterDefinition(definitions, filter.k);
          const open = menu?.kind === "value" && menu.index === index;
          const label = filterValueLabel(definitions, filter);
          const removable = isFilterRemovable(filter, { canSearchAccountWide });
          return (
            // The token clips its own overflow for the segmented pill, so the menu is
            // anchored to a wrapper instead and stays unclipped.
            <span className="fs-advanced-search__anchor" key={filter.k}>
              <span className={`fs-advanced-search__token${open ? " is-open" : ""}`}>
                <span className="fs-advanced-search__token-key">{filter.k}</span>
                <button
                  aria-expanded={open}
                  aria-haspopup="menu"
                  className="fs-advanced-search__token-value"
                  onClick={() => dispatch({ type: "open-value-menu", index })}
                  type="button"
                >
                  {label ?? t("react.shared.choose", "Choose…")}
                  <ChevronDown aria-hidden="true" />
                </button>
                {removable ? (
                  <button
                    aria-label={t("react.shared.remove_value1_filter", "Remove {value1} filter", { value1: String(definition?.label ?? filter.k) })}
                    className="fs-advanced-search__token-remove"
                    onClick={() => dispatch({ type: "remove-filter", index })}
                    type="button"
                  >
                    <X aria-hidden="true" />
                  </button>
                ) : null}
              </span>
              {open ? renderValueMenu(index, filter.k) : null}
            </span>
          );
        })}

        {available.length ? (
          <span className="fs-advanced-search__anchor">
            <button
              aria-expanded={addMenuOpen}
              aria-haspopup="menu"
              className={`fs-advanced-search__add-filter${addMenuOpen ? " is-open" : ""}`}
              onClick={() => dispatch({ type: "open-add-menu" })}
              type="button"
            >
              <Plus aria-hidden="true" />
              <span>{t("react.shared.add_filter", "Add filter")}</span>
            </button>
            {addMenuOpen ? (
              <div className="fs-advanced-search__menu" role="menu">
                <div className="fs-advanced-search__menu-label">{t("react.shared.add_filter", "Add filter")}</div>
                {available.map((key) => (
                  <button
                    className="fs-advanced-search__menu-item"
                    key={key}
                    onClick={() => dispatch({ type: "add-filter", key })}
                    role="menuitem"
                    type="button"
                  >
                    {filterDefinition(definitions, key)?.label ?? key}
                  </button>
                ))}
              </div>
            ) : null}
          </span>
        ) : null}

        {active.length ? (
          <button
            className="fs-advanced-search__clear-filters"
            onClick={() => dispatch({ type: "clear-filters" })}
            type="button"
          >
            {t("react.shared.clear_all", "Clear all")} </button>
        ) : null}
      </div>
    </div>
  );
}
