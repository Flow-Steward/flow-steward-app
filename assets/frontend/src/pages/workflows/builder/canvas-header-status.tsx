import { ListChecks, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DsLoader } from "@/components/shared/ds-loader";
import type { I18nContextValue } from "@/lib/i18n/context";
import type { WorkflowCanvasIssue } from "@/pages/workflows/shared/api";
import type { WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";
import { ignoreAsyncResult } from "@/lib/utils/async";

type Translate = I18nContextValue["t"];
type StructureStatus = { label: string; className: string };

type Props = Readonly<{
  t: Translate;
  structureStatus: StructureStatus;
  validationIssues: readonly WorkflowCanvasIssue[];
  changeSet: WorkflowChangeSetController;
  structureLocked: boolean;
  workflowId: string;
  applyingLoader: boolean;
  applyDisabled: boolean;
  showValidationPanel: boolean;
  onToggleValidationPanel: () => void;
}>;

/** The builder header's structure status, protected marker, and diagnostic summary. */
export function CanvasHeaderStatus({
  t,
  structureStatus,
  validationIssues,
  changeSet,
  structureLocked,
  workflowId,
  applyingLoader,
  applyDisabled,
  showValidationPanel,
  onToggleValidationPanel,
}: Props) {
  const errorCount = validationIssues.filter((issue) => issue.severity === "error").length;
  const warningCount = validationIssues.filter((issue) => issue.severity === "warning").length;
  const diagnosticsFacts = [
    errorCount ? t(errorCount === 1 ? "react.workflows.validation_error_one" : "react.workflows.validation_errors_count", errorCount === 1 ? "{count} error" : "{count} errors", { count: errorCount }) : "",
    warningCount ? t(warningCount === 1 ? "react.workflows.validation_warning_one" : "react.workflows.validation_warnings_count", warningCount === 1 ? "{count} warning" : "{count} warnings", { count: warningCount }) : "",
  ].filter(Boolean).join(" · ");
  const diagnosticsSummary = changeSet.hasChanges
    ? diagnosticsFacts
      ? t("react.workflows.saved_validation_issues", "Saved workflow: {issues}", { issues: diagnosticsFacts })
      : t("react.workflows.saved_workflow_valid", "Saved workflow valid")
    : diagnosticsFacts;
  const visibleStructureStatus = changeSet.ownerReady && changeSet.status === "clean" && !changeSet.hasChanges && diagnosticsFacts
    ? {
        ...structureStatus,
        label: t("react.workflows.status_structure_applied_with_issues", "Structure applied · {issues}", { issues: diagnosticsFacts }),
      }
    : structureStatus;

  return <>
    {!structureLocked || changeSet.hasChanges ? <>
      <div
        className={`workflow-structure-status ${visibleStructureStatus.className}`}
        data-testid="structure-status"
        role="status"
        aria-live="polite"
        title={t("react.workflows.structure_status_hint", "Structure changes stay local until you apply them.")}
      >
        {applyingLoader ? <DsLoader variant="orbit-inline" /> : <ListChecks className="h-3.5 w-3.5" />}
        <span>{visibleStructureStatus.label}</span>
      </div>
      {changeSet.hasChanges ? <Button variant="secondary" size="md" disabled={changeSet.status === "applying"} onClick={() => changeSet.discard()} type="button">
        <X aria-hidden="true" />
        {t("react.workflows.discard_changes", "Discard changes")}
      </Button> : null}
      <Button variant="primary" size="md" disabled={applyDisabled} onClick={() => ignoreAsyncResult(changeSet.apply())} type="button">
        {changeSet.status === "applying"
          ? t("react.workflows.applying_changes", "Applying…")
          : t("react.workflows.apply_changes", "Apply changes")}
      </Button>
    </> : null}
    {structureLocked ? <span
      className="inline-flex shrink-0 items-center gap-1 rounded-md border border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] px-2.5 py-1 text-[11px] text-[var(--fs-status-attention-ink)]"
      data-testid="structure-locked"
      aria-describedby={`workflow-protection-${workflowId}`}
      title={t("react.workflows.structure_locked_hint", "This workflow is protected by its extension. You can read it and connect your own workflows to its hooks.")}
    >
      {t("react.workflows.structure_locked", "Read-only")}
    </span> : null}
    <span className="workflow-header-divider" aria-hidden="true" />
    <Button variant="secondary" size="md" className="workflow-canvas-diagnostics"
      aria-label={`${t("react.workflows.diagnostics", "Diagnostics")}${diagnosticsSummary ? ` · ${diagnosticsSummary}` : ""}`}
      aria-expanded={showValidationPanel} onClick={onToggleValidationPanel}
      title={t("react.workflows.open_canvas_validation_issues", "Open canvas validation issues")}>
      <ListChecks aria-hidden="true" />
      {!changeSet.hasChanges && errorCount ? <span className="workflow-diagnostics-error">{t(errorCount === 1 ? "react.workflows.validation_error_one" : "react.workflows.validation_errors_count", errorCount === 1 ? "{count} error" : "{count} errors", { count: errorCount })}</span> : null}
      {!changeSet.hasChanges && errorCount && warningCount ? <span aria-hidden="true">·</span> : null}
      {!changeSet.hasChanges && warningCount ? <span className="workflow-diagnostics-warning">{t(warningCount === 1 ? "react.workflows.validation_warning_one" : "react.workflows.validation_warnings_count", warningCount === 1 ? "{count} warning" : "{count} warnings", { count: warningCount })}</span> : null}
      {changeSet.hasChanges ? diagnosticsSummary : !errorCount && !warningCount ? t("react.workflows.diagnostics", "Diagnostics") : null}
    </Button>
  </>;
}
