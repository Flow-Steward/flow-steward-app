/** The viewer's strings for a locale, from viewer-labels.json (kept equal to the app catalog). */
import type { ReportLabels } from "@/components/report/report-labels";

import catalog from "./viewer-labels.json";

export type ViewerLabels = ReportLabels & { waiting: string; unavailable: string; cancelled: string };

type CatalogLocale = keyof typeof catalog.locales;

export function viewerLabels(...candidates: Array<string | undefined>): ViewerLabels {
  for (const candidate of candidates) {
    const language = String(candidate ?? "").split("-")[0].toLowerCase();
    if (Object.hasOwn(catalog.locales, language)) {
      return catalog.locales[language as CatalogLocale];
    }
  }
  return catalog.locales.en;
}
