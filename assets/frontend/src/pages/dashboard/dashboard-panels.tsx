import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";

import {
  Activity,
  AlertTriangle,
  ChevronRight,
  FolderPlus,
  ShieldCheck,
  Trash2,
  Workflow,
} from "lucide-react";
import { Link } from "react-router";

import "@/pages/dashboard/dashboard-segmented.css";
import "@/pages/dashboard/dashboard-panels.css";
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
