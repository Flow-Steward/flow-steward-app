import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import {
  firstStringFromUnknownValue,
  stringFromUnknownValue,
} from "@/lib/utils/unknown-values";

export type SpendRow = {
  name: string;
  spend_usd: number | null;
  availability: "measured" | "partial" | "unavailable";
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function spendRows(value: unknown): SpendRow[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((row) => {
      const record = asRecord(row);
      const name = firstStringFromUnknownValue([
        record.name,
        record.project_name,
        record.workflow,
        record.agent,
        "",
      ]).trim();
      const rawSpend = record.spend_usd ?? record.cost_usd ?? record.total_cost_usd;
      const parsedSpend = rawSpend === null || rawSpend === undefined ? null : Number(rawSpend);
      const spend = parsedSpend !== null && Number.isFinite(parsedSpend) ? parsedSpend : null;
      const rawAvailability = stringFromUnknownValue(record.availability).trim().toLowerCase();
      let availability: SpendRow["availability"] = "measured";
      if (rawAvailability === "partial") {
        availability = "partial";
      } else if (rawAvailability === "unavailable" || spend === null) {
        availability = "unavailable";
      }
      return { name, spend_usd: spend, availability } satisfies SpendRow;
    })
    .filter((row) => row.name)
    .slice(0, 8);
}

export function formatSpend(row: SpendRow, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (row.spend_usd === null || row.availability === "unavailable") {
    return t("dashboard.classic.error.kpi_value", "Unavailable");
  }
  const formatted = new Intl.NumberFormat(undefined, {
    currency: "USD",
    maximumFractionDigits: 2,
    style: "currency",
  }).format(row.spend_usd);
  return row.availability === "partial" ? `${formatted} (partial)` : formatted;
}
