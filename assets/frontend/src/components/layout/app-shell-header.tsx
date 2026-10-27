import { useI18n } from "@/lib/i18n/useI18n";

import { Fragment } from "react";

import { Bell, ChevronRight, PanelLeft } from "lucide-react";
import { NavLink } from "react-router";

import type { ShellBreadcrumb } from "@/components/layout/app-shell-breadcrumbs";
import {
  approvalJobId,
  approvalJobSubtitle,
  approvalJobTitle,
  type AwaitingApprovalJob,
} from "@/components/layout/app-shell-helpers";
import { AppShellSearchTrigger } from "@/components/layout/app-shell-search-trigger";
import { IconButton } from "@/components/ui/icon-button";

export type AppShellHeaderProps = {
  approvalError: string;
  approvalJobs: AwaitingApprovalJob[];
  approvalLoaded: boolean;
  approvalLoading: boolean;
  approvalsOpen: boolean;
  breadcrumbs: ShellBreadcrumb[];
  onOpenApprovalJob: (job: AwaitingApprovalJob) => void;
  onOpenSearch: () => void;
  onRetryApprovals: () => void;
  onToggleApprovals: () => void;
  onToggleRail: () => void;
  pendingApprovals: number;
  railExpanded: boolean;
};

export function AppShellHeader({
  approvalError,
  approvalJobs,
  approvalLoaded,
  approvalLoading,
  approvalsOpen,
  breadcrumbs,
  onOpenApprovalJob,
  onOpenSearch,
  onRetryApprovals,
  onToggleApprovals,
  onToggleRail,
  pendingApprovals,
  railExpanded,
}: Readonly<AppShellHeaderProps>) {
  const { t } = useI18n();
  return (
    <header className="fs-app-header">
      <div data-testid="header-order-trigger">
        <IconButton
          aria-expanded={railExpanded}
          className="fs-app-header__trigger"
          icon={PanelLeft}
          iconSize={16}
          label={t("react.shared.toggle_sidebar", "Toggle sidebar")}
          onClick={onToggleRail}
          showTooltip={false}
          size="small"
        />
      </div>
      <span
        aria-hidden="true"
        className="fs-app-header__separator"
        data-testid="header-order-separator"
      />
      <nav
        aria-label={t("react.shared.breadcrumb", "Breadcrumb")}
        className="fs-app-header__breadcrumbs"
        data-testid="header-order-breadcrumbs"
      >
        {breadcrumbs.map((item, index) => (
          <Fragment key={`${item.to ?? "current"}:${item.label}`}>
            {index > 0 ? (
              <ChevronRight aria-hidden="true" size={12} strokeWidth={2} />
            ) : null}
            {item.to ? (
              <NavLink end to={item.to}>
                {item.label}
              </NavLink>
            ) : (
              <span aria-current="page">{item.label}</span>
            )}
          </Fragment>
        ))}
      </nav>
      <div
        className="fs-app-header__search-slot"
        data-testid="header-order-search"
      >
        <AppShellSearchTrigger onOpenSearch={onOpenSearch} />
      </div>
      <div
        className="fs-app-header__approvals"
        data-testid="header-order-approvals"
      >
        <IconButton
          aria-expanded={approvalsOpen}
          data-testid="approval-bell"
          icon={Bell}
          label={
            pendingApprovals > 0
              ? t("react.shared.open_approvals_value1_waiting", "Open approvals, {value1} waiting", { value1: String(pendingApprovals) })
              : t("react.shared.open_approvals", "Open approvals")
          }
          onClick={onToggleApprovals}
        />
        {pendingApprovals > 0 ? (
          <span
            aria-hidden="true"
            className="fs-app-header__approval-count"
            data-testid="approval-badge"
          >
            {pendingApprovals}
          </span>
        ) : null}
        {approvalsOpen ? (
          <dialog
            aria-label={t("react.shared.approvals", "Approvals")}
            className="fs-app-header__approval-menu"
            open
          >
            {approvalLoading ? <p>{t("react.shared.loading_approvals", "Loading approvals…")}</p> : null}
            {approvalError ? (
              <div>
                <p>{approvalError}</p>
                <button onClick={onRetryApprovals} type="button">
                  {t("react.shared.retry_approvals", "Retry approvals")} </button>
              </div>
            ) : null}
            {approvalLoaded &&
            !approvalLoading &&
            !approvalError &&
            approvalJobs.length === 0 ? (
              <p>{t("react.shared.no_approvals_are_waiting", "No approvals are waiting.")}</p>
            ) : null}
            {approvalJobs.map((job) => (
              <button
                data-testid="approval-job-link"
                key={approvalJobId(job)}
                onClick={() => onOpenApprovalJob(job)}
                type="button"
              >
                <span>{approvalJobTitle(job, t)}</span>
                {approvalJobSubtitle(job, t) ? (
                  <span>{approvalJobSubtitle(job, t)}</span>
                ) : null}
              </button>
            ))}
          </dialog>
        ) : null}
      </div>
    </header>
  );
}
