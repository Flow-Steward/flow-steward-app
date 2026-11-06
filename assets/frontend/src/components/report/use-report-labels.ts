/**
 * Report renderer strings from the app catalog, for product surfaces. The MCP
 * Apps viewer passes its own labels instead: it cannot load this catalog.
 */
import { useMemo } from "react";

import { useI18n } from "@/lib/i18n/useI18n";

import type { ReportLabels } from "./report-labels";

export function useReportLabels(): ReportLabels {
  const { t } = useI18n();
  return useMemo(
    () => ({
      empty: t("react.report.empty_value", "—"),
      yes: t("react.report.yes", "Yes"),
      no: t("react.report.no", "No"),
      showData: t("react.report.show_data", "Show data"),
      chartDataCaption: t("react.report.chart_data_caption", "Data: {title}"),
      chartUnavailable: t(
        "react.report.chart_unavailable",
        "The chart could not be drawn. Its values are in the table below.",
      ),
      unsupported: t(
        "react.report.unsupported_version",
        "This report uses a format this version cannot show.",
      ),
      invalid: t("react.report.invalid", "This result is not a readable report."),
    }),
    [t],
  );
}
