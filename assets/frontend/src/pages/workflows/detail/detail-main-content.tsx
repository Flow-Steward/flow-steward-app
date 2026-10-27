import { WorkflowStepIcon } from "@/pages/workflows/shared/workflow-step-icon";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { AlertTriangle, CheckCircle2 } from "lucide-react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type {
  WorkflowGroupRow,
  WorkflowStepRow,
  WorkflowValidationIssueGroup,
} from "@/pages/workflows/detail/detail-helpers";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { applyRefusalMessage, changeIssueMessage, type WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";
import type { GroupDraftDefaults } from "@/pages/workflows/detail/detail-drawer-controller";

type ValidationIssueAction = (group: WorkflowValidationIssueGroup) => void;

export function WorkflowDetailMainContent({ ctx }: Readonly<{ ctx: Record<string, any> }>) {
  const { t } = useI18n();
  const rootSteps = (ctx.definitionRows?.rootSteps || []) as WorkflowStepRow[];
  const groupRows = (ctx.definitionRows?.groupRows || []) as WorkflowGroupRow[];
  const stepRowsById = (ctx.definitionRows?.stepRowsById || new Map()) as Map<string, WorkflowStepRow>;
  const entryKey = typeof ctx.entryKey === "string" ? ctx.entryKey : "";
  const changeSet = ctx.changeSet as WorkflowChangeSetController | undefined;
  const selectedStep = ctx.selectedStep as WorkflowStepRow | null;
  const isValid = Boolean(ctx.isValid);
  const validationErrorGroups =
    ctx.validationErrorGroups as WorkflowValidationIssueGroup[];
  const validationErrors = ctx.validationErrors as unknown[];
  const validationWarningGroups =
    ctx.validationWarningGroups as WorkflowValidationIssueGroup[];
  const validationWarnings = ctx.validationWarnings as unknown[];
  const openGroupDrawer = ctx.openGroupDrawer as (groupId: string, tab: string) => void;
  const openNewStepDrawer = ctx.openNewStepDrawer as (context: { groupId?: string; laneId?: string }) => void;
  const openNewGroupDrawer = ctx.openNewGroupDrawer as (defaults?: GroupDraftDefaults) => void;
  const openStepDrawer = ctx.openStepDrawer as (stepId: string, blockKey?: string) => void;
  const openValidationIssueStep =
    ctx.openValidationIssueStep as ValidationIssueAction;
  const openValidationIssueGroup =
    ctx.openValidationIssueGroup as ValidationIssueAction;
  const openValidationIssueSourceStep =
    ctx.openValidationIssueSourceStep as ValidationIssueAction;

  function renderValidationIssueGroup(group: WorkflowValidationIssueGroup) {
    const isError = group.severity === "error";
    const sourceStepExists = Boolean(group.sourceStepId && stepRowsById.has(group.sourceStepId));
    const borderClass = isError
      ? "border-rose-300/30 bg-rose-500/10 text-rose-100"
      : "border-amber-300/30 bg-amber-500/10 text-amber-100";
    const primaryMessage =
      group.messages[0] ||
      (isError ? t("react.workflows.validation_error", "Validation error") : t("react.workflows.validation_warning", "Validation warning"));

    return (
      <div
        aria-label={group.title}
        className={`rounded-xl border px-3 py-3 text-xs ${borderClass}`}
        key={group.key}
        role="group"
      >
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="text-[11px] font-medium uppercase tracking-[0.12em] opacity-80">
              {group.category}
            </p>
            <p className="mt-1 text-sm font-medium">{group.title}</p>
          </div>
          {group.issues.length > 1 ? (
            <span className="rounded-full border border-white/15 px-2 py-0.5 text-[11px] text-slate-100/90">
              {group.issues.length} {" "}{t("react.workflows.checks_grouped", "checks grouped")}</span>
          ) : null}
        </div>
        <p className="mt-2 leading-5 text-slate-100/90">{primaryMessage}</p>
        <div className="mt-2 grid gap-1 text-[11px] leading-5 text-slate-300">
          {group.inputName ? <p>{t("react.workflows.field", "Field:")}{" "}{group.inputName}</p> : null}
          {group.source ? <p>{t("react.workflows.source_variant_c707ee4e", "Source:")}{" "}{group.source}</p> : null}
          {group.affectedLabels.length ? (
            <p>{t("react.workflows.affected", "Affected:")}{" "}{group.affectedLabels.join(", ")}</p>
          ) : null}
          {group.sourceStepId && !sourceStepExists ? (
            <p>
              {t("react.workflows.referenced_step_missing", "The referenced step is not in this workflow. Add it back or map this field from a step that runs earlier.")}</p>
          ) : null}
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          {group.targetStep ? (
            <button
              className="rounded-lg border border-white/20 bg-slate-950/40 px-2 py-1 text-xs text-slate-100 hover:border-cyan-300/50"
              onClick={() => openValidationIssueStep(group)}
              type="button"
            >
              {t("react.workflows.open_step_mapping", "Open step mapping")}</button>
          ) : null}
          {group.targetGroup ? (
            <button
              className="rounded-lg border border-white/20 bg-slate-950/40 px-2 py-1 text-xs text-slate-100 hover:border-cyan-300/50"
              onClick={() => openValidationIssueGroup(group)}
              type="button"
            >
              {t("react.workflows.open_affected_group", "Open affected group")}</button>
          ) : null}
          {sourceStepExists ? (
            <button
              className="rounded-lg border border-white/20 bg-slate-950/40 px-2 py-1 text-xs text-slate-100 hover:border-cyan-300/50"
              onClick={() => openValidationIssueSourceStep(group)}
              type="button"
            >
              {t("react.workflows.open_source_step", "Open source step")}</button>
          ) : null}
        </div>
      </div>
    );
  }

  function renderStepButton(step: WorkflowStepRow) {
    const selected = selectedStep?.runtimeId === step.runtimeId;
    return (
      <button
        className={[
          "workflow-step-card w-full rounded-xl border px-2 py-1.5 text-left text-xs transition",
          selected
            ? "border-cyan-300/45 bg-cyan-500/10 text-cyan-100"
            : "border-white/10 bg-slate-950/60 text-slate-200 hover:border-cyan-300/30",
        ].map((value) => stringFromUnknownValue(value)).join(" ")}
        data-step-block-key={step.blockKey}
        data-step-group-id={step.groupId}
        data-step-id={step.runtimeId}
        key={step.stepKey}
        onClick={() => openStepDrawer(step.runtimeId, step.blockKey)}
        type="button"
      >
        <div className="flex items-center gap-1.5">
          <WorkflowStepIcon kind={step.stepKind || "agent"} />
          {entryKey === `step:${step.runtimeId}` ? (
            <span className="rounded border border-emerald-300/35 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] text-emerald-100">
              {t("react.workflows.entry_badge", "Entry")}
            </span>
          ) : null}
          {step.local ? (
            <span className="rounded border border-amber-300/35 bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-100">
              {t("react.workflows.not_applied_badge", "Not applied")}
            </span>
          ) : null}
          {step.laneId ? (
            <span
              className="rounded border border-purple-300/35 bg-purple-500/10 px-1.5 py-0.5 text-[10px] text-purple-100"
              title={t("react.workflows.parallel_lane", "Parallel lane")}
            >
              {t("react.workflows.lane_variant_096bcd6a", "Lane:")}{" "}{step.laneId}
            </span>
          ) : null}
          <span className="step-primary-label truncate font-medium">
            {step.primaryLabel}
          </span>
        </div>
        {step.secondaryLabel ? (
          <p className="mt-0.5 truncate text-[11px] text-slate-400">
            {step.secondaryLabel}
          </p>
        ) : null}
      </button>
    );
  }

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      <Card className="border-white/10 bg-white/5">
        <CardHeader>
          <CardTitle className="text-xl">{t("react.workflows.workflow_steps", "Workflow steps")}</CardTitle>
          <CardDescription>
            {t("react.workflows.workflow_steps_description", "Steps run in the order their connections define. Loops and parallel lanes are real groups.")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4" id="workflow-structure">
          {changeSet && (changeSet.hasChanges || changeSet.status === "conflict" || changeSet.applyIssues.length) ? (
            <div
              className="space-y-2 rounded-xl border border-amber-300/35 bg-amber-500/10 px-3 py-3 text-xs text-amber-50"
              data-testid="workflow-change-set-bar"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="font-medium">
                  {changeSet.hasChanges
                    ? t("react.workflows.status_structure_not_applied_count", "Structure not applied: {count} change(s)", {
                        count: changeSet.operations.length,
                      })
                    : t("react.workflows.status_structure_applied", "Structure applied")}
                </p>
                {changeSet.hasChanges ? (
                  <div className="flex gap-2">
                    <button
                      className="rounded-lg border border-emerald-300/45 bg-emerald-500/15 px-3 py-1 text-xs font-medium text-emerald-50 disabled:opacity-50"
                      disabled={changeSet.status === "applying" || changeSet.status === "conflict"}
                      onClick={() => ignoreAsyncResult(changeSet.apply())}
                      type="button"
                    >
                      {changeSet.status === "applying"
                        ? t("react.workflows.applying_changes", "Applying…")
                        : t("react.workflows.apply_changes", "Apply changes")}
                    </button>
                    <button
                      className="rounded-lg border border-white/20 px-3 py-1 text-xs text-slate-100 disabled:opacity-50"
                      disabled={changeSet.status === "applying"}
                      onClick={() => changeSet.discard()}
                      type="button"
                    >
                      {t("react.workflows.discard_changes", "Discard changes")}
                    </button>
                  </div>
                ) : null}
              </div>
              {changeSet.status === "conflict" ? (
                <div className="rounded-lg border border-rose-300/40 bg-rose-500/10 px-3 py-2 text-rose-50" role="alert">
                  <p>
                    {t(
                      "react.workflows.change_set_conflict",
                      "The workflow changed after it was loaded. Your unapplied changes are kept; reload the workflow to review them against the new version.",
                    )}
                  </p>
                  <button
                    className="mt-2 rounded-lg border border-rose-200/45 px-3 py-1 text-xs text-rose-50"
                    onClick={() => ignoreAsyncResult(changeSet.reload())}
                    type="button"
                  >
                    {t("react.workflows.reload_workflow", "Reload workflow")}
                  </button>
                </div>
              ) : null}
              {changeSet.applyError ? (
                <p className="rounded-lg border border-rose-300/40 bg-rose-500/10 px-3 py-2 text-rose-50" data-testid="change-set-apply-error" role="alert">
                  {applyRefusalMessage(changeSet.applyError, changeSet.applyRefused, t)}
                </p>
              ) : null}
              {(changeSet.applyRefused ? [] : changeSet.applyIssues)
                .map((issue) => changeIssueMessage(issue, t))
                .filter(Boolean)
                .slice(0, 8)
                .map((message, index) => (
                  <p className="text-amber-100/90" key={`${message}:${index}`}>{message}</p>
                ))}
              {changeSet.hasChanges && changeSet.checkState === "checking" ? (
                <p className="text-amber-100/70" data-testid="change-check-status">
                  {t("react.workflows.change_check_running", "Checking the changes against the server…")}
                </p>
              ) : null}
              {changeSet.hasChanges && changeSet.checkState === "unavailable" ? (
                <p className="text-amber-100/70" data-testid="change-check-status">
                  {t("react.workflows.change_check_unavailable", "The server check is unavailable; these issues are a local estimate.")}
                </p>
              ) : null}
              {changeSet.blockingIssueMessages.length ? (
                <div className="space-y-1 rounded-lg border border-rose-300/40 bg-rose-500/10 px-3 py-2 text-rose-50" data-testid="change-check-blocking">
                  <p className="font-medium">{t("react.workflows.change_check_blocking", "Apply will be refused:")}</p>
                  {changeSet.blockingIssueMessages.slice(0, 8).map((message, index) => (
                    <p key={`${message}:${index}`}>{message}</p>
                  ))}
                </div>
              ) : null}
              {changeSet.keptIssueMessages.length ? (
                <div className="space-y-1" data-testid="change-check-kept">
                  <p className="font-medium">{t("react.workflows.change_check_kept", "Issues the saved definition keeps:")}</p>
                  {changeSet.keptIssueMessages.slice(0, 8).map((message, index) => (
                    <p className="text-amber-100/90" key={`${message}:${index}`}>{message}</p>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
          <div className="space-y-1.5" data-testid="workflow-root-steps">
            {rootSteps.map((step) => renderStepButton(step))}
            {!rootSteps.length && !groupRows.length ? (
              <p className="rounded-xl border border-white/10 bg-slate-950/60 px-3 py-2 text-xs text-slate-400">
                {t("react.workflows.workflow_has_no_steps", "This workflow has no steps yet.")}
              </p>
            ) : null}
          </div>
          {groupRows.map((group) => (
            <div
              className="workflow-group-card rounded-2xl border border-white/10 bg-slate-900/50 px-3 py-3"
              data-group-id={group.groupId}
              key={group.groupId}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-100">
                    {group.displayName}
                    {entryKey === `${group.containerKind}:${group.groupId}` ? (
                      <span className="ml-2 rounded border border-emerald-300/35 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] text-emerald-100">
                        {t("react.workflows.entry_badge", "Entry")}
                      </span>
                    ) : null}
                  </p>
                  <p className="mt-1 text-[11px] text-slate-400">
                    {group.containerKind === "for_each"
                      ? t("react.workflows.for_each_badge", "For each item")
                      : group.bodyKind === "lanes"
                        ? t("react.workflows.group_kind_parallel", "Parallel lanes")
                        : t("react.workflows.group_body_one_after_another", "One after another")}
                    {group.containerKind === "for_each" && group.over
                      ? ` · ${t("react.workflows.for_each_source_item", "{item} in {over}", { item: group.itemName, over: group.over })}`
                      : ""}
                  </p>
                </div>
                <button
                  className="rounded-full border border-white/15 px-2 py-0.5 text-[11px] text-slate-200"
                  onClick={() => openGroupDrawer(group.groupId, "settings")}
                  title={group.containerKind === "for_each" ? t("react.workflows.edit_for_each", "Edit For each") : t("react.workflows.edit_group", "Edit group")}
                  type="button"
                >
                  {t("extensions.repeater.edit", "Edit")}</button>
              </div>
              <div className="mt-3 space-y-1.5">
                {group.steps.map((step) => renderStepButton(step))}
              </div>
              <button
                className="mt-3 w-full rounded border border-indigo-300/35 bg-indigo-500/10 px-2 py-1.5 text-xs font-medium text-indigo-100 hover:border-indigo-300/50"
                onClick={() =>
                  openNewStepDrawer({
                    ...(group.containerKind === "for_each" ? { forEachId: group.groupId } : { groupId: group.groupId }),
                    laneId: group.lanes[0] || "",
                  })
                }
                type="button"
              >
                {t("registry.workflow_detail.step.add", "+ Add Step")}</button>
            </div>
          ))}
          <div className="flex flex-wrap gap-2">
            <button
              className="rounded border border-indigo-300/35 bg-indigo-500/10 px-3 py-1.5 text-xs font-medium text-indigo-100 hover:border-indigo-300/50"
              onClick={() => openNewStepDrawer({})}
              type="button"
            >
              {t("registry.workflow_detail.step.add", "+ Add Step")}</button>
            <button
              className="rounded border border-pink-300/35 bg-pink-500/10 px-3 py-1.5 text-xs font-medium text-pink-100 hover:border-pink-300/50"
              onClick={() => openNewGroupDrawer({ containerKind: "group" })}
              type="button"
            >
              {t("react.workflows.add_group", "+ Add group")}</button>
            <button
              className="rounded border border-pink-300/35 bg-pink-500/10 px-3 py-1.5 text-xs font-medium text-pink-100 hover:border-pink-300/50"
              onClick={() => openNewGroupDrawer({ containerKind: "for_each" })}
              type="button"
            >
              {t("react.workflows.add_for_each", "+ Add For each")}</button>
          </div>
        </CardContent>
      </Card>

      <Card className="border-white/10 bg-white/5">
        <CardHeader>
          <CardTitle className="text-xl">{t("react.workflows.validation_overview", "Validation overview")}</CardTitle>
          <CardDescription>
            {t("react.workflows.readiness_checks_for_saving_enabling_and_running_this_workflow", "Readiness checks for saving, enabling, and running this workflow.")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <div
            className={[
              "inline-flex items-center gap-2 rounded-2xl border px-3 py-2 text-sm",
              isValid
                ? "border-emerald-300/30 bg-emerald-500/10 text-emerald-100"
                : "border-amber-300/30 bg-amber-500/10 text-amber-100",
            ].map((value) => stringFromUnknownValue(value)).join(" ")}
          >
            {isValid ? (
              <CheckCircle2 className="h-4 w-4" />
            ) : (
              <AlertTriangle className="h-4 w-4" />
            )}
            {isValid
              ? t("react.workflows.workflow_contract_is_valid", "Workflow contract is valid.")
              : t("react.workflows.workflow_has_contract_issues", "Workflow has contract issues.")}
          </div>

          <div className="grid gap-3 xl:grid-cols-2">
            <Card className="border-white/10 bg-white/5">
              <CardHeader>
                <CardTitle className="text-base">{t("registry.common.errors", "Errors")}</CardTitle>
                <CardDescription>
                  {validationErrorGroups.length
                    ? t("react.workflows.value0_grouped_issues_from_value1_checks", "{value0} grouped issues from {value1} checks", { value0: validationErrorGroups.length, value1: validationErrors.length })
                    : t("react.workflows.no_grouped_issues", "No grouped issues")}
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-2">
                {validationErrorGroups.map((group) =>
                  renderValidationIssueGroup(group),
                )}
                {!validationErrorGroups.length ? (
                  <div className="rounded-xl border border-white/10 bg-slate-900/40 px-2 py-2 text-xs text-slate-300">
                    {t("react.workflows.no_validation_errors", "No validation errors.")}</div>
                ) : null}
              </CardContent>
            </Card>

            <Card className="border-white/10 bg-white/5">
              <CardHeader>
                <CardTitle className="text-base">{t("registry.common.warnings", "Warnings")}</CardTitle>
                <CardDescription>
                  {validationWarningGroups.length
                    ? t("react.workflows.value0_grouped_issues_from_value1_checks", "{value0} grouped issues from {value1} checks", { value0: validationWarningGroups.length, value1: validationWarnings.length })
                    : t("react.workflows.no_grouped_issues", "No grouped issues")}
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-2">
                {validationWarningGroups.map((group) =>
                  renderValidationIssueGroup(group),
                )}
                {!validationWarningGroups.length ? (
                  <div className="rounded-xl border border-white/10 bg-slate-900/40 px-2 py-2 text-xs text-slate-300">
                    {t("react.workflows.no_validation_warnings", "No validation warnings.")}</div>
                ) : null}
              </CardContent>
            </Card>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
