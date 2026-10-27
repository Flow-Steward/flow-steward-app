import { useI18n } from "@/lib/i18n/useI18n";
import { useId, useState } from "react";

import { Search } from "lucide-react";

type AppShellSearchTriggerProps = {
  onOpenSearch: () => void;
};

export function AppShellSearchTrigger({
  onOpenSearch,
}: Readonly<AppShellSearchTriggerProps>) {
  const { t } = useI18n();
  const tooltipId = useId();
  const [focused, setFocused] = useState(false);
  const [hovered, setHovered] = useState(false);
  const tooltipVisible = focused || hovered;

  return (
    <span className="fs-app-header__search-trigger-wrap">
      <button
        aria-describedby={tooltipVisible ? tooltipId : undefined}
        aria-haspopup="dialog"
        aria-label={t("shell.search.open_title", "Global search")}
        className="fs-app-header__search-trigger"
        onBlur={() => setFocused(false)}
        onClick={onOpenSearch}
        onFocus={() => setFocused(true)}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        type="button"
      >
        <Search aria-hidden="true" size={16} strokeWidth={2} />
        <span className="fs-app-header__search-label">{t("events.actions.search", "Search")}</span>
        <kbd className="fs-app-header__search-shortcut">{t("react.shared.k", "⌘K")}</kbd>
      </button>
      {tooltipVisible ? (
        <span
          className="fs-shell-tooltip fs-app-header__search-tooltip"
          id={tooltipId}
          role="tooltip"
        >
          {t("shell.search.open_title", "Global search")} </span>
      ) : null}
    </span>
  );
}
