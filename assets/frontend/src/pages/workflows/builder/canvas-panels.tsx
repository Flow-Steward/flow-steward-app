import { useI18n } from "@/lib/i18n/useI18n";
import type { I18nContextValue } from "@/lib/i18n/context";
/* eslint-disable react-refresh/only-export-components */
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";

import { ListChecks, Search, Settings2 } from "lucide-react";

import type { WorkflowCanvasGraph, WorkflowCanvasIssue } from "@/pages/workflows/shared/api";
import { validationIssueActionLabel, validationIssueTargetLabel } from "@/pages/workflows/builder/canvas-core";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";

import { WorkflowStepIcon } from "@/pages/workflows/shared/workflow-step-icon";
import { PALETTE_FAMILIES, paletteEntries, paletteFamilyLabel } from "@/pages/workflows/builder/canvas-palette-catalog";

function issueSeverity(issues: WorkflowCanvasIssue[]): "error" | "warning" | "valid" {
  if (issues.some((issue) => issue.severity === "error")) return "error";
  return issues.length ? "warning" : "valid";
}

function issueCountLabel(count: number, kind: "error" | "warning", t: I18nContextValue["t"]): string {
  if (!count) return "";
  if (kind === "error") {
    return count === 1
      ? t("react.workflows.validation_error_one", "{count} error", { count })
      : t("react.workflows.validation_errors_count", "{count} errors", { count });
  }
  return count === 1
    ? t("react.workflows.validation_warning_one", "{count} warning", { count })
    : t("react.workflows.validation_warnings_count", "{count} warnings", { count });
}

function issueLevelClasses(level: "error" | "warning" | "valid"): string {
  if (level === "error") {
    return "border-[var(--fs-status-danger-border)] bg-[var(--fs-status-danger-bg)] text-[var(--fs-status-danger)] hover:bg-[var(--fs-status-danger-bg)]";
  }
  if (level === "warning") {
    return "border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] text-[var(--fs-status-attention-ink)] hover:bg-[var(--fs-status-attention-wash)]";
  }
  return "border-[var(--fs-status-success-border)] bg-[var(--fs-status-success-bg)] text-[var(--fs-status-success)] hover:bg-[var(--fs-status-success-bg)]";
}

export function ContractHeader({
  children,
  graph,
  hasUnappliedChanges = false,
  onOpenWorkflowAutomation,
  onOpenWorkflowSettings,
  onOpenWorkflowInterface,
  onOpenWorkflowSchedules,
  onValidationClick,
  onSearchChange,
  onSearchKeyDown,
  searchQuery,
  showSearchResults,
}: Readonly<{
  children?: ReactNode;
  graph: WorkflowCanvasGraph;
  hasUnappliedChanges?: boolean;
  onOpenWorkflowAutomation: () => void;
  onOpenWorkflowSettings: () => void;
  onOpenWorkflowInterface?: () => void;
  onOpenWorkflowSchedules?: () => void;
  onValidationClick: () => void;
  onSearchChange: (value: string) => void;
  onSearchKeyDown: (event: ReactKeyboardEvent<HTMLInputElement>) => void;
  searchQuery: string;
  showSearchResults: boolean;
}>) {
  const { t } = useI18n();
  const workflowAutomation = graph.automation_rules.filter((rule) => rule.scope === "workflow");
  const issues = graph.validation_issues;
  const errorCount = issues.filter((issue) => issue.severity === "error").length;
  const warningCount = issues.filter((issue) => issue.severity === "warning").length;
  const issueLevel = issueSeverity(issues);
  const validationLabel =
    errorCount || warningCount
      ? [
          issueCountLabel(errorCount, "error", t),
          issueCountLabel(warningCount, "warning", t),
        ]
          .filter(isTruthy)
          .join(" · ")
      : t("react.workflows.valid", "Valid");
  const validationScopeLabel = hasUnappliedChanges
    ? (issues.length ? t("react.workflows.saved_validation_issues", "Saved workflow: {issues}", { issues: validationLabel }) : t("react.workflows.saved_workflow_valid", "Saved workflow valid"))
    : validationLabel;
  return (
    <div className="flex flex-wrap items-start gap-3 px-3 py-3 text-[11px]">
      <button
        className="inline-flex shrink-0 cursor-pointer items-center gap-2 rounded-2xl border border-[var(--fs-border-selected)] bg-[var(--fs-action-primary-wash)] px-3 py-2 text-left transition hover:border-[var(--fs-border-selected)] hover:bg-[var(--fs-action-primary-wash)] hover:shadow-lg hover:shadow-cyan-950/20"
        onClick={onOpenWorkflowInterface || onOpenWorkflowSettings}
        title={t("react.workflows.open_interface", "Open interface")}
        type="button"
      >
        <span className="rounded border border-[var(--fs-border-selected)] bg-[var(--fs-action-primary-wash)] px-2 py-1 font-mono text-[var(--fs-text-brand)]">
          {t("react.workflows.workflow_interface", "Workflow interface")}</span>
        <span className="max-w-[240px] truncate text-xs font-medium text-[var(--fs-text-primary)]">
          {graph.workflow.display_name}
        </span>
        <Settings2 className="h-3.5 w-3.5 text-[var(--fs-text-brand)]" />
      </button>
      <button className="fs-button" onClick={onOpenWorkflowSettings} type="button">{t("react.workflows.manual_workflow_settings", "Workflow settings")}</button>
      {onOpenWorkflowSchedules ? <button className="fs-button" type="button" onClick={onOpenWorkflowSchedules}>{t("react.workflows.schedules", "Schedules")}</button> : null}
      <div className="grid min-w-0 basis-full gap-2 xl:grid-cols-[minmax(0,1fr)_minmax(280px,0.75fr)]">
        <div className="grid min-w-0 gap-2 grid-cols-[repeat(auto-fit,minmax(100px,1fr))]">
          <ContractSection
            empty={t("react.workflows.display_no_triggers", "no triggers")}
            items={(graph.interface_contract.trigger_channels || []).map((channel) => ({ name: channel }))}
            label={t("react.workflows.triggers", "triggers")}
          />
          <ContractSection
            empty={t("react.workflows.display_no_inputs", "no inputs")}
            items={graph.interface_contract.expected_inputs || []}
            label={t("react.workflows.inputs", "inputs")}
          />
          <ContractSection
            empty={t("react.workflows.display_no_outputs", "no outputs")}
            items={[...(graph.output_contract.expected_outputs || []), ...(graph.output_contract.returned_files || [])]}
            label={t("react.workflows.returns", "returns")}
          />
        </div>
        <div className="flex min-w-0 flex-wrap items-start justify-end gap-2">
          <div className="relative min-w-[240px] flex-1">
            <label className="sr-only" htmlFor="workflow-builder-search">{t("react.workflows.search_workflow_canvas", "Search workflow canvas")}</label>
            <div className="flex items-center gap-2 rounded-2xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] px-3 py-2">
              <Search className="h-4 w-4 text-[var(--fs-text-tertiary)]" />
              <input
                autoComplete="off"
                className="w-full bg-transparent text-sm text-[var(--fs-text-primary)] outline-none placeholder:text-[var(--fs-text-tertiary)]"
                id="workflow-builder-search"
                onChange={(event) => onSearchChange(event.target.value)}
                onFocus={() => {
                  if (searchQuery.trim()) {
                    onSearchChange(searchQuery);
                  }
                }}
                onKeyDown={onSearchKeyDown}
                placeholder={t("react.workflows.search_step_or_group", "Search step or group")}
                value={searchQuery}
              />
            </div>
            {showSearchResults ? children : null}
          </div>
          {workflowAutomation.length ? (
            <button
              className="group/contract-auto relative shrink-0 rounded-2xl border border-yellow-400/50 bg-yellow-950/60 px-2 py-2 text-[10px] uppercase tracking-wide text-yellow-200"
              data-canvas-secondary-action="true"
              onClick={(event) => {
                event.stopPropagation();
                onOpenWorkflowAutomation();
              }}
              type="button"
            >
              {workflowAutomation.length} {" "}{t("shell.theme.mode.auto", "auto")}<span className="pointer-events-none absolute right-0 top-11 z-30 hidden w-64 rounded-lg border border-yellow-500/40 bg-[var(--fs-bg-surface)] p-2 text-left text-[10px] leading-4 text-[var(--fs-text-primary)] shadow-xl group-hover/contract-auto:block">
                {workflowAutomation.slice(0, 4).map((rule) => (
                  <span className="block" key={`${rule.reaction_key}:${rule.display_name}`}>
                    {rule.display_name || rule.reaction_key || t("react.workflows.automation_rule", "Automation rule")}
                    {rule.event_type ? ` · ${rule.event_type}` : ""}
                    {rule.action_type ? ` -> ${rule.action_type}` : ""}
                  </span>
                ))}
              </span>
            </button>
          ) : null}
          <button
            className={[
              "inline-flex shrink-0 items-center gap-1 rounded-2xl border px-2.5 py-2 text-[10px] font-medium uppercase tracking-wide transition",
              issueLevelClasses(issueLevel),
            ].map((value) => stringFromUnknownValue(value)).join(" ")}
            onClick={onValidationClick}
            title={hasUnappliedChanges ? t("react.workflows.saved_validation_scope", "This badge describes the saved workflow. Unapplied changes are checked separately above.") : issues.length ? t("react.workflows.open_canvas_validation_issues", "Open canvas validation issues") : t("react.workflows.validation_is_currently_clean", "Validation is currently clean")}
            type="button"
          >
            <ListChecks className="h-3.5 w-3.5" />
            {validationScopeLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function ContractSection({
  empty,
  items,
  label,
}: Readonly<{
  empty: string;
  items: Array<Record<string, unknown>>;
  label: string;
}>) {
  const rows = Array.isArray(items) ? items : [];
  return (
    <div className="min-w-0 rounded-2xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] px-3 py-2">
      <p className="text-[9px] uppercase tracking-[0.14em] text-[var(--fs-text-tertiary)]">{label}</p>
      <div className="mt-1 flex flex-wrap gap-1 overflow-hidden">
        {rows.length ? (
          rows.slice(0, 3).map((item, index) => (
            <span
              className="max-w-[170px] truncate rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--fs-text-primary)]"
              key={`${label}:${contractItemText(item)}:${index}`}
              title={contractItemText(item)}
            >
              {contractItemText(item)}
            </span>
          ))
        ) : (
          <span className="text-[10px] text-[var(--fs-text-tertiary)]">{empty}</span>
        )}
        {rows.length > 3 ? (
          <span className="rounded border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface)] px-1.5 py-0.5 text-[10px] text-[var(--fs-text-tertiary)]">
            +{rows.length - 3}
          </span>
        ) : null}
      </div>
    </div>
  );
}

export function contractItemText(item: Record<string, unknown>) {
  const name = firstStringFromUnknownValue([item.name, item.kind, item.target, "item"]).trim();
  const type = firstStringFromUnknownValue([item.type, ""]).trim();
  const required = item.required === true ? "*" : "";
  let options = "";
  if (Array.isArray(item.options)) {
    const overflowMarker = item.options.length > 3 ? "/..." : "";
    options = ` [${item.options.slice(0, 3).map(String).join("/")}${overflowMarker}]`;
  }
  const typeDetail = type ? `:${type}` : "";
  return `${name}${typeDetail}${required}${options}`;
}

export function LegendPanel() {
  const { t } = useI18n();
  const legendKinds = paletteEntries({ sequenceForEach: true, allowGroup: true, allowForEach: true, allowHookDeclaration: true, inserting: false, connected: false }, t);
  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-3 text-sm text-[var(--fs-text-primary)]">
      <p className="text-xs font-medium uppercase tracking-[0.16em] text-[var(--fs-text-brand)]">{t("react.workflows.legend", "Legend")}</p>
      <p className="mt-1 text-[10px] text-[var(--fs-text-tertiary)]">
        {t("react.workflows.click_nodes_badges_issues_to_open_the_inspector", "Click nodes, badges and issues to open the inspector in context.")}</p>
      <div className="mt-4 space-y-4 text-[11px]">
        {/* The kinds come from Add step itself, so the legend never names a kind the builder no longer offers. */}
        {PALETTE_FAMILIES.map((family) => {
          const members = legendKinds.filter((entry) => entry.family === family);
          return members.length ? (
            <LegendSection key={family} title={paletteFamilyLabel(family, t)}>
              {members.map((entry) => (
                <div className="flex items-start gap-2" key={entry.choice.catalogKey}>
                  <WorkflowStepIcon kind={entry.iconKind} />
                  <p><span className="text-[var(--fs-text-secondary)]">{entry.choice.displayName}</span> · {entry.description}</p>
                </div>
              ))}
            </LegendSection>
          ) : null;
        })}
        <LegendSection title={t("react.workflows.edge_meaning", "Edge meaning")}>
          <LegendLine color="bg-[var(--fs-workflow-edge)]" label={t("react.workflows.legend_edge_next_outcome", "Runs next. A label names the outcome: True, False, a case, Default, Approved or Rejected.")} />
          <LegendLine color="border-t border-dashed border-[var(--fs-workflow-edge-failure)]" dashed label={t("react.workflows.legend_edge_on_error", "On error: where the workflow goes when the step fails.")} />
          <LegendLine color="border-t border-dashed border-[var(--fs-status-attention-border)]" dashed label={t("react.workflows.legend_edge_not_applied", "not applied yet")} />
          <LegendLine color="bg-[var(--fs-workflow-edge-ran)]" label={t("react.workflows.legend_edge_ran", "Taken in the last test run; a count shows repeated passes.")} />
        </LegendSection>
        <LegendSection title={t("react.workflows.legend_inside_groups", "Inside groups")}>
          <p>{t("react.workflows.legend_groups_sequence", "A group runs its steps one after another.")}</p>
          <p>{t("react.workflows.legend_groups_parallel", "A parallel group runs its lanes side by side and continues when all finish.")}</p>
          <p>{t("react.workflows.legend_groups_exit", "Inside a group, connect the last step to the group's exit handle.")}</p>
        </LegendSection>
        <LegendSection title={t("project_detail.tab.automation", "Automation")}>
          <p className="flex items-center gap-2">
            <span className="rounded border border-yellow-400/50 bg-yellow-950/60 px-1.5 py-0.5 text-yellow-200">{t("react.workflows.n_auto", "N auto")}</span>
            <span>{t("react.workflows.hover_for_rules_click_for_scoped_editor", "hover for rules, click for scoped editor")}</span>
          </p>
        </LegendSection>
        <LegendSection title={t("react.workflows.legend_building", "Building")}>
          <p>{t("react.workflows.legend_building_ports", "Drag from a named outcome to the step that runs next.")}</p>
          <p>{t("react.workflows.legend_building_apply", "Structure changes stay local until you press Apply changes.")}</p>
          <p>{t("react.workflows.legend_building_position", "Moving a node on the canvas never changes what runs next.")}</p>
        </LegendSection>
      </div>
    </div>
  );
}

export function LegendSection({ children, title }: Readonly<{ children: ReactNode; title: string }>) {
  return (
    <section>
      <h2 className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-[var(--fs-text-primary)]">{title}</h2>
      <div className="space-y-1 text-[var(--fs-text-tertiary)]">{children}</div>
    </section>
  );
}

export function LegendLine({
  animated = false,
  color,
  dashed = false,
  label,
}: Readonly<{
  animated?: boolean;
  color: string;
  dashed?: boolean;
  label: string;
}>) {
  return (
    <p className="flex items-center gap-2">
      <span
        className={[
          "relative inline-block h-0.5 w-10 overflow-hidden",
          color,
          dashed ? "border-t bg-transparent" : "",
        ].map((value) => stringFromUnknownValue(value)).join(" ")}
      >
        {animated ? <span className="absolute inset-y-0 left-1 h-1 w-3 rounded-full bg-[var(--fs-bg-surface-muted)]" /> : null}
      </span>
      {label}
    </p>
  );
}

export function ValidationPanel({
  graph,
  issues,
  onIssueClick,
}: Readonly<{
  graph: WorkflowCanvasGraph;
  issues: WorkflowCanvasIssue[];
  onIssueClick: (issue: WorkflowCanvasIssue) => void;
}>) {
  const { t } = useI18n();
  const groups = [
    {
      issues: issues.filter((issue) => issue.severity === "error"),
      label: t("registry.common.errors", "Errors"),
      severity: "error" as const,
    },
    {
      issues: issues.filter((issue) => issue.severity === "warning"),
      label: t("registry.common.warnings", "Warnings"),
      severity: "warning" as const,
    },
  ].filter((group) => group.issues.length);
  return (
    <section
      aria-label={t("react.workflows.validation_issues", "Validation issues")}
      className="min-h-0 flex-1 overflow-y-auto p-3 text-sm text-[var(--fs-text-primary)]"
    >
      <p className="text-xs font-medium uppercase tracking-[0.16em] text-[var(--fs-text-brand)]">{t("react.workflows.validation_issues", "Validation issues")}</p>
      <div className="mt-3 space-y-4">
        {issues.length ? (
          groups.map((group) => (
            <div className="space-y-2" key={group.severity}>
              <p
                className={[
                  "rounded-xl border px-3 py-1.5 text-[11px] font-medium uppercase tracking-[0.14em]",
                  group.severity === "error"
                    ? "border-[var(--fs-status-danger-border)] bg-[var(--fs-status-danger-bg)] text-[var(--fs-status-danger)]"
                    : "border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] text-[var(--fs-status-attention-ink)]",
                ].map((value) => stringFromUnknownValue(value)).join(" ")}
              >
                {group.label} · {group.issues.length}
              </p>
              {group.issues.map((issue) => {
                const targetLabel = validationIssueTargetLabel(issue, graph, t);
                const actionLabel = validationIssueActionLabel(issue, graph, t);
                const issueMessage = issue.message
                  ? (issue.message_key ? t(issue.message_key, issue.message, issue.message_params) : issue.message)
                  :
                  (issue.issues || [])
                    .map((item) => item.message_key ? t(item.message_key, item.message, item.message_params) : item.message)
                    .filter(isTruthy)
                    .join(" ");
                return (
                  <button
                    className="block w-full overflow-hidden rounded-2xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] text-left hover:border-[var(--fs-border-selected)]"
                    key={`${issue.scope}:${issue.authored_id || issue.scope_id}:${issue.code}:${issue.title}:${issue.message_key || ""}`}
                    onClick={() => onIssueClick(issue)}
                    type="button"
                  >
                    <div
                      className={[
                        "px-3 py-2 text-[11px] font-medium uppercase tracking-[0.1em]",
                        issue.severity === "error"
                          ? "bg-[var(--fs-status-danger-bg)] text-[var(--fs-status-danger)]"
                          : "bg-[var(--fs-status-attention-wash)] text-[var(--fs-status-attention-ink)]",
                      ].map((value) => stringFromUnknownValue(value)).join(" ")}
                    >
                      {issue.severity === "error" ? t("events.severity.error", "Error") : t("events.severity.warning", "Warning")}
                    </div>
                    <div className="px-3 py-3">
                      <p className="break-words font-medium leading-5 text-[var(--fs-text-primary)]">
                        {issue.title_key ? t(issue.title_key, issue.title, issue.title_params) : issue.title}
                      </p>
                      <p
                        className="mt-1 truncate text-xs text-[var(--fs-text-tertiary)]"
                        title={targetLabel}
                      >
                        {targetLabel}
                      </p>
                      <p className="mt-2 break-words text-xs text-[var(--fs-text-primary)]">
                        {issueMessage}
                      </p>
                      {actionLabel ? (
                        <p className="mt-2 break-words rounded border border-[var(--fs-border-selected)] bg-[var(--fs-action-primary-wash)] px-2 py-1 text-[11px] text-[var(--fs-text-brand)]">
                          {actionLabel}
                        </p>
                      ) : null}
                      {issue.code ? <p className="mt-2 break-words font-mono text-[10px] text-[var(--fs-text-tertiary)]">{issue.code}</p> : null}
                      {issue.field_path ? (
                        <p className="mt-2 truncate font-mono text-[10px] text-[var(--fs-text-tertiary)]">
                          {issue.field_path}
                        </p>
                      ) : null}
                    </div>
                  </button>
                );
              })}
            </div>
          ))
        ) : (
          <div className="rounded-2xl border border-[var(--fs-status-success-border)] bg-[var(--fs-status-success-bg)] px-3 py-3 text-[var(--fs-status-success)]">
            {t("react.workflows.no_validation_issues", "No validation issues.")}</div>
        )}
      </div>
    </section>
  );
}
