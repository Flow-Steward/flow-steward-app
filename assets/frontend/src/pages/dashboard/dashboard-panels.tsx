import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useState } from "react";

import {
  Activity,
  AlertTriangle,
  ChevronRight,
  FolderPlus,
  ShieldCheck,
  Trash2,
  Wallet,
  Workflow,
} from "lucide-react";
import { Link } from "react-router";

import "@/pages/dashboard/dashboard-segmented.css";
import "@/pages/dashboard/dashboard-panels.css";
import { formatSpend, type SpendRow } from "@/pages/dashboard/cost-display";

export type SpendBreakdown = {
  project: SpendRow[];
  workflow: SpendRow[];
  agent: SpendRow[];
};



function getSpendCuts(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { key: "project", label: t("events.columns.project", "Project") },
  { key: "workflow", label: t("jobs.columns.workflow", "Workflow") },
  { key: "agent", label: t("events.columns.agent", "Agent") },
] as const;
}



function getProjectLink(t: I18nContextValue["t"] = fallbackContext.t) {
  return { icon: FolderPlus, label: t("react.dashboard.create_a_project", "Create a project"), to: "/projects" };
}



function getRegistryLinks(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { icon: Workflow, label: t("react.dashboard.build_a_workflow", "Build a workflow"), to: "/registry/workflows" },
  { icon: ShieldCheck, label: t("react.dashboard.review_guardrails", "Review guardrails"), to: "/registry/guardrails" },
];
}



function getAdminLinks(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { icon: Activity, label: t("react.dashboard.system_monitoring", "System monitoring"), to: "/monitoring" },
  { icon: Trash2, label: t("react.dashboard.trash_lifecycle", "Trash lifecycle"), to: "/trash" },
];
}

/** Five rows is the reserved height; more than that belongs in the job list. */
const SPEND_ROW_LIMIT = 5;

export function QuickLinks({
  canAdministerPlatform,
  canReadRegistry,
}: Readonly<{ canAdministerPlatform: boolean; canReadRegistry: boolean }>) {
  const { t } = useI18n();
  const quickLinks = [
    getProjectLink(t),
    ...(canReadRegistry ? getRegistryLinks(t) : []),
    ...(canAdministerPlatform ? getAdminLinks(t) : []),
  ];

  return (
    <section className="fs-dp">
      <header className="fs-dp__head">
        <h2 className="fs-dp__title">
          <span>{t("react.dashboard.move_the_work_forward", "Move the work forward")}</span>
        </h2>
      </header>
      <p className="fs-dp__note">{t("react.dashboard.start_the_next_piece_of_work_without_hunting_for_the_entry_point", "Start the next piece of work without hunting for the entry point.")}</p>

      <div className="fs-dp__body">
        {quickLinks.map((item) => (
          <Link className="fs-dp__link" key={item.to} to={item.to}>
            <span aria-hidden="true" className="fs-dp__link-icon">
              <item.icon />
            </span>
            <span className="fs-dp__link-label">{item.label}</span>
            <ChevronRight aria-hidden="true" className="fs-dp__chevron" />
          </Link>
        ))}
      </div>
    </section>
  );
}

export function ModelSpend({
  breakdown,
  total,
  rangeLabel,
}: Readonly<{ breakdown: SpendBreakdown; total: string; rangeLabel: string }>) {
  const { t } = useI18n();
  const [cut, setCut] = useState<keyof SpendBreakdown>("project");
  const rows = breakdown[cut].slice(0, SPEND_ROW_LIMIT);
  // Shares are relative to the biggest row in the current cut, so the widest
  // track is always full and the comparison stays readable at any spend level.
  const peak = rows.reduce((max, row) => Math.max(max, row.spend_usd ?? 0), 0);
  const caption = [rangeLabel, total ? t("react.dashboard.value1_total", "{value1} total", { value1: String(total) }) : ""].filter(Boolean).join(" · ");

  return (
    <section className="fs-dp">
      <header className="fs-dp__head">
        <h2 className="fs-dp__title">
          <Wallet aria-hidden="true" />
          <span>{t("react.dashboard.model_spend", "Model spend")}</span>
        </h2>
        <fieldset aria-label={t("react.dashboard.spend_breakdown", "Spend breakdown")} className="fs-seg">
          {getSpendCuts(t).map((item) => (
            <button
              aria-pressed={cut === item.key}
              className="fs-seg__item"
              key={item.key}
              onClick={() => setCut(item.key)}
              type="button"
            >
              {item.label}
            </button>
          ))}
        </fieldset>
      </header>
      {caption ? <p className="fs-dp__note">{caption}</p> : null}

      {/* Height is reserved so switching the cut does not move the page. */}
      <div className="fs-dp__spend">
        {rows.length ? (
          rows.map((row) => (
            <div className="fs-dp__spend-row" key={row.name}>
              <span className="fs-dp__spend-name">{row.name}</span>
              <span aria-hidden="true" className="fs-dp__spend-track">
                <span
                  className="fs-dp__spend-fill"
                  style={{
                    width: `${peak > 0 && row.spend_usd !== null ? Math.round((row.spend_usd / peak) * 100) : 0}%`,
                  }}
                />
              </span>
              <span className="fs-dp__spend-amount">{formatSpend(row, t)}</span>
            </div>
          ))
        ) : (
          <p className="fs-dp__empty">{t("react.dashboard.no_model_spend_in_this_range_widen_the_range_to_see_earlier_runs", "No model spend in this range. Widen the range to see earlier runs.")}</p>
        )}
      </div>
    </section>
  );
}

export function DashboardLoadError({ message }: Readonly<{ message: string }>) {
  const { t } = useI18n();
  return (
    <section aria-live="polite" className="fs-dp fs-dp--danger">
      <header className="fs-dp__head">
        <h2 className="fs-dp__title">
          <AlertTriangle aria-hidden="true" />
          <span>{t("react.dashboard.dashboard_load_failed", "Dashboard load failed")}</span>
        </h2>
      </header>
      <p className="fs-dp__note">{message}</p>
    </section>
  );
}
