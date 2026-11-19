import type { DsFilterField } from "@/components/shared/design-system";
import { filterRangeParts } from "@/components/shared/filter-range";
import type { I18nContextValue } from "@/lib/i18n/context";
import { localizedStatusLabel } from "@/components/shared/localized-status-label";
import { AWAITING_REVIEW_STATUS } from "@/pages/jobs/list/job-filters";

import type { OperatorFilterView } from "./types";

/** A job status as the filter offers it; "awaiting review" is the queue, not a stored status. */
function statusOptionLabel(option: string, t: I18nContextValue["t"]): string {
  return option === AWAITING_REVIEW_STATUS ? t("react.operator.status_awaiting_review", "Awaiting review") : localizedStatusLabel(option, t);
}

/** The Jobs list's filter-panel key of a configured filter; its URL key too. */
export function operatorFilterKey(filter: Pick<OperatorFilterView, "id">): string {
  return `f.${filter.id}`;
}

/**
 * The admin's filters as fields of the existing filter panel: a choice from
 * the contract's options, yes/no, or a typed value (search text, a number or
 * date range).
 */
export function operatorFilterFields(filters: readonly OperatorFilterView[], t: I18nContextValue["t"]): DsFilterField[] {
  return filters.map((filter) => {
    const base = { key: operatorFilterKey(filter), label: filter.label || filter.path };
    const statusLabels = filter.source === "job" && filter.path === "status";
    const options = (filter.options ?? []).map((option) => ({
      value: option,
      label: statusLabels ? statusOptionLabel(option, t) : option,
    }));
    switch (filter.type) {
      case "search":
        return { ...base, options: [], input: "text" as const };
      case "number_range":
        return { ...base, options: [], input: "number_range" as const };
      case "date_range":
        return { ...base, options: [], input: "date_range" as const };
      case "boolean":
        return {
          ...base,
          options: [
            { value: "true", label: t("react.operator.filter_yes", "Yes") },
            { value: "false", label: t("react.operator.filter_no", "No") },
          ],
        };
      case "choice_multi":
        return { ...base, options, multiple: true };
      default:
        return { ...base, options };
    }
  });
}

function finiteNumber(text: string): number | undefined {
  if (!text) return undefined;
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : undefined;
}

/**
 * The `filters` query value of `/api/v2/jobs?operator_view=1`: configured
 * filter id → value, only for filters that hold something; null when none do.
 */
export function operatorFiltersParam(
  filters: readonly OperatorFilterView[],
  values: Readonly<Record<string, string>>,
): Record<string, unknown> | null {
  const param: Record<string, unknown> = {};
  for (const filter of filters) {
    const raw = (values[operatorFilterKey(filter)] ?? "").trim();
    if (!raw) continue;
    switch (filter.type) {
      case "search":
        param[filter.id] = raw;
        break;
      case "boolean":
        if (raw === "true" || raw === "false") param[filter.id] = raw === "true";
        break;
      case "choice_multi": {
        const chosen = raw.split(",").map((item) => item.trim()).filter(Boolean);
        if (chosen.length) param[filter.id] = chosen;
        break;
      }
      case "number_range": {
        const [from, to] = filterRangeParts(raw);
        const range: Record<string, number> = {};
        const min = finiteNumber(from);
        const max = finiteNumber(to);
        if (min !== undefined) range.min = min;
        if (max !== undefined) range.max = max;
        if (Object.keys(range).length) param[filter.id] = range;
        break;
      }
      case "date_range": {
        const [from, to] = filterRangeParts(raw);
        const range: Record<string, string> = {};
        if (/^\d{4}-\d{2}-\d{2}$/.test(from)) range.from = from;
        if (/^\d{4}-\d{2}-\d{2}$/.test(to)) range.to = to;
        if (Object.keys(range).length) param[filter.id] = range;
        break;
      }
      default:
        param[filter.id] = raw;
    }
  }
  return Object.keys(param).length ? param : null;
}
