import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
/* eslint-disable @typescript-eslint/no-explicit-any */
import type { ReactNode } from "react";

import {
  textOrEmpty,
  type WorkflowVariableSuggestionOption,
} from "@/pages/workflows/detail/detail-helpers";
import { isTruthy } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  insertTemplateVariable,
  openTemplateQuery,
  rankVariables,
  type RankedVariable,
} from "@/pages/workflows/detail/detail-variable-filter";

type WorkflowVariableSuggestionRenderContext = Record<string, any>;

type SuggestionGroup = { options: WorkflowVariableSuggestionOption[] } | null;
type SuggestionStructure = {
  inputsGroup?: SuggestionGroup;
  globalGroup: SuggestionGroup;
  steps: Array<{
    aliasOptions: WorkflowVariableSuggestionOption[];
    artifactOptions: WorkflowVariableSuggestionOption[];
    stepId: string;
  }>;
  workflowGroup: SuggestionGroup;
};

/** The same panel structure with only the variables ``accept`` allows. */
function filteredSuggestionStructure(
  structure: SuggestionStructure,
  accept: (path: string) => boolean,
): { count: number; structure: SuggestionStructure } {
  const keep = (options: WorkflowVariableSuggestionOption[]) =>
    options.filter((option) => accept(option.value));
  const group = (source: SuggestionGroup): SuggestionGroup => {
    const options = keep(source?.options ?? []);
    return source && options.length ? { ...source, options } : null;
  };
  const steps = structure.steps
    .map((step) => ({
      ...step,
      aliasOptions: keep(step.aliasOptions),
      artifactOptions: keep(step.artifactOptions),
    }))
    .filter((step) => step.aliasOptions.length + step.artifactOptions.length > 0);
  const filtered = {
    inputsGroup: group(structure.inputsGroup ?? null),
    globalGroup: group(structure.globalGroup),
    steps,
    workflowGroup: group(structure.workflowGroup),
  };
  const count =
    (filtered.inputsGroup?.options.length ?? 0) +
    (filtered.globalGroup?.options.length ?? 0) +
    (filtered.workflowGroup?.options.length ?? 0) +
    steps.reduce((total, step) => total + step.aliasOptions.length + step.artifactOptions.length, 0);
  return { count, structure: filtered };
}

export function createWorkflowVariableSuggestionRenderers(
  ctx: WorkflowVariableSuggestionRenderContext, t: I18nContextValue["t"] = fallbackContext.t
) {
  const {
    activeWorkflowSuggestionPanelId,
    availableProjectsQuery,
    availableWorkflowVariableSuggestionCount,
    expandedWorkflowSuggestionKeys,
    handleLinkProjectForBuilder,
    projectLinkDraft,
    projectLinkMessage,
    projectLinkPending,
    selectedProjectId,
    setActiveWorkflowSuggestionPanelId,
    setExpandedWorkflowSuggestionKeys,
    setProjectLinkDraft,
    workflowSuggestionStructure,
  } = ctx;
  // The highlighted match of the open panel (item 11); a caller without one gets 0.
  const highlight: number = ctx.workflowSuggestionHighlight ?? 0;
  const setHighlight: (index: number) => void = ctx.setWorkflowSuggestionHighlight ?? (() => {});
  // Opening a filled field shows every variable; typing in it narrows them (item 11).
  const typedSinceOpen: boolean = ctx.workflowSuggestionTyped ?? true;
  const setTypedSinceOpen: (typed: boolean) => void = ctx.setWorkflowSuggestionTyped ?? (() => {});
  const workflowGroupLabel = ctx.workflowGroupLabel || t("react.workflows.workflow_variables", "Workflow variables");

  // Every option the panel can show, in its order, with the group it belongs to.
  function allWorkflowVariables(includeAdvanced = expandedWorkflowSuggestionKeys.includes("group:global")): RankedVariable[] {
    const entries: RankedVariable[] = [];
    for (const [group, options] of [
      [t("react.workflows.workflow_inputs", "Inputs"), workflowSuggestionStructure.inputsGroup?.options ?? []],
      [t("react.workflows.advanced_values", "Advanced values"), includeAdvanced ? workflowSuggestionStructure.globalGroup?.options ?? [] : []],
      [workflowGroupLabel, workflowSuggestionStructure.workflowGroup?.options ?? []],
    ] as Array<[string, WorkflowVariableSuggestionOption[]]>) {
      for (const option of options) entries.push({ group, option });
    }
    for (const step of workflowSuggestionStructure.steps as Array<{
      aliasOptions: WorkflowVariableSuggestionOption[];
      artifactOptions: WorkflowVariableSuggestionOption[];
      stepId: string;
    }>) {
      for (const option of [...step.aliasOptions, ...step.artifactOptions]) {
        entries.push({ group: step.stepId, option });
      }
    }
    return entries;
  }

  function toggleWorkflowSuggestionKey(key: string) {
    setExpandedWorkflowSuggestionKeys((current: string[]) =>
      current.includes(key)
        ? current.filter((item) => item !== key)
        : [...current, key],
    );
  }

  function openWorkflowSuggestionPanel(panelId: string) {
    setActiveWorkflowSuggestionPanelId(panelId);
    setExpandedWorkflowSuggestionKeys((current: string[]) => [
      "group:inputs",
      "group:workflow",
      "group:previous-steps",
      ...(current.includes("group:global") ? ["group:global"] : []),
      ...workflowSuggestionStructure.steps.map(
        (step: { stepId: string }) => `step:${step.stepId}`,
      ),
    ]);
  }

  function renderWorkflowSuggestionOption(
    groupKey: string,
    option: WorkflowVariableSuggestionOption,
    onSelect: (value: string) => void,
    highlighted = false,
    optionId?: string,
  ) {
    return (
      <button
        aria-selected={highlighted}
        className={`flex w-full items-start justify-between gap-3 rounded-lg px-2 py-1.5 text-left hover:bg-[var(--fs-popover-option-hover-bg)] focus-visible:[box-shadow:var(--fs-popover-focus-ring)] focus-visible:outline-none${
          highlighted ? " bg-[var(--fs-popover-option-hover-bg)] fs-row-selected" : ""
        }`}
        id={optionId}
        key={`${groupKey}:${option.value}`}
        onMouseDown={(event) => {
          event.preventDefault();
          onSelect(option.value);
          setActiveWorkflowSuggestionPanelId("");
        }}
        role="option"
        type="button"
      >
        <span className="min-w-0">
          <span className="block truncate text-[12px] text-[var(--fs-popover-option-text)]">
            {option.label || option.value}
          </span>
          {option.label && option.label !== option.value ? (
            <span className="block truncate font-mono text-[10px] fs-ink-secondary">{option.value}</span>
          ) : null}
          {option.description ? (
            <span className="block text-[11px] fs-ink-secondary">
              {option.description}
            </span>
          ) : null}
        </span>
        {option.meta ? (
          <span className="shrink-0 text-[10px] uppercase tracking-[0.12em] text-[var(--fs-popover-secondary-text)]">
            {option.meta}
          </span>
        ) : null}
      </button>
    );
  }

  function renderWorkflowVariableMatches(
    panelId: string,
    onSelect: (value: string) => void,
    matches: RankedVariable[],
  ) {
    if (!matches.length) {
      return <p className="px-2 py-1.5 text-[12px] fs-ink-secondary">{t("react.workflows.no_matching_variables", "No matching variables")}</p>;
    }
    return (
      <div className="space-y-0.5">
        {matches.map((entry, index) =>
          renderWorkflowSuggestionOption(
            `match:${entry.group}`,
            { ...entry.option, meta: entry.option.meta || entry.group },
            onSelect,
            index === highlight,
            `${panelId}-option-${index}`,
          ),
        )}
      </div>
    );
  }

  function renderWorkflowVariableSuggestions(
    panelId: string,
    onSelect: (value: string) => void,
    query = "",
  ) {
    if (!availableWorkflowVariableSuggestionCount) {
      return null;
    }
    const isActive = activeWorkflowSuggestionPanelId === panelId;
    // Something typed: the matches, ranked; nothing typed: every group, as before.
    const filtered = query.trim()
      ? renderWorkflowVariableMatches(panelId, onSelect, rankVariables(allWorkflowVariables(), query))
      : null;
    return (
      <div
        aria-label={t("react.workflows.workflow_variable_suggestions", "Workflow variable suggestions")}
        className={
          isActive
            ? "absolute left-0 right-0 top-full z-30 mt-1 max-h-64 overflow-y-auto rounded-[var(--fs-popover-radius)] border border-[var(--fs-popover-border)] bg-[var(--fs-popover-bg)] p-2 [box-shadow:var(--fs-popover-shadow)]"
            : "hidden"
        }
        data-testid={panelId}
        id={panelId}
        role="listbox"
      >
        {ctx.workflowVariablePositionUnknown ? (
          <p className="px-2 pb-1.5 text-[11px] fs-ink-attention" data-testid="workflow-variables-position-unknown">
            {t("react.workflows.variables_position_unknown", "This step's position in the saved workflow is not known yet, so every saved step's outputs are listed. Place and apply it to see only what runs before it.")}
          </p>
        ) : null}
        {filtered ?? <div className="space-y-2">
          {(
            [
              workflowSuggestionStructure.inputsGroup
                ? {
                    childKey: "group:inputs",
                    count: workflowSuggestionStructure.inputsGroup.options.length,
                    content: workflowSuggestionStructure.inputsGroup.options.map(
                      (option: WorkflowVariableSuggestionOption) =>
                        renderWorkflowSuggestionOption("inputs", option, onSelect),
                    ),
                    label: t("react.workflows.workflow_inputs", "Inputs"),
                  }
                : null,
              workflowSuggestionStructure.globalGroup
                ? {
                    childKey: "group:global",
                    count: workflowSuggestionStructure.globalGroup.options.length,
                    content: workflowSuggestionStructure.globalGroup.options.map(
                      (option: WorkflowVariableSuggestionOption) =>
                        renderWorkflowSuggestionOption("global", option, onSelect),
                    ),
                    label: t("react.workflows.advanced_values", "Advanced values"),
                  }
                : null,
              workflowSuggestionStructure.workflowGroup
                ? {
                    childKey: "group:workflow",
                    count:
                      workflowSuggestionStructure.workflowGroup.options.length,
                    content:
                      workflowSuggestionStructure.workflowGroup.options.map(
                        (option: WorkflowVariableSuggestionOption) =>
                          renderWorkflowSuggestionOption(
                            "workflow",
                            option,
                            onSelect,
                          ),
                      ),
                    label: workflowGroupLabel,
                  }
                : null,
              workflowSuggestionStructure.steps.length
                ? {
                    childKey: "group:previous-steps",
                    count: workflowSuggestionStructure.steps.reduce(
                      (
                        total: number,
                        step: {
                          aliasOptions: WorkflowVariableSuggestionOption[];
                          artifactOptions: WorkflowVariableSuggestionOption[];
                        },
                      ) =>
                        total +
                        step.aliasOptions.length +
                        step.artifactOptions.length,
                      0,
                    ),
                    content: workflowSuggestionStructure.steps.map(
                      (step: {
                        aliasOptions: WorkflowVariableSuggestionOption[];
                        artifactOptions: WorkflowVariableSuggestionOption[];
                        stepId: string;
                      }) => {
                        const stepKey = `step:${step.stepId}`;
                        const expanded = expandedWorkflowSuggestionKeys.includes(
                          stepKey,
                        );
                        return (
                          <section
                            className="rounded-lg border border-default fs-surface-inset"
                            key={step.stepId}
                          >
                            <button
                              className="flex w-full items-center justify-between px-2 py-1.5 text-left text-[11px] font-medium uppercase tracking-[0.14em] fs-ink-primary"
                              onMouseDown={(event) => {
                                event.preventDefault();
                                toggleWorkflowSuggestionKey(stepKey);
                              }}
                              type="button"
                            >
                              <span>{step.stepId}</span>
                              <span className="text-[10px] text-[var(--fs-popover-secondary-text)]">
                                {step.aliasOptions.length +
                                  step.artifactOptions.length}
                              </span>
                            </button>
                            {expanded ? (
                              <div className="space-y-1 border-t border-subtle p-1">
                                {step.aliasOptions.length ? (
                                  <div className="rounded-md bg-[var(--fs-popover-bg)] p-1">
                                    <p className="px-1 pb-1 text-[10px] font-medium uppercase tracking-[0.16em] fs-ink-brand">{t("react.workflows.state_aliases", "State aliases")}</p>
                                    {step.aliasOptions.map((option) =>
                                      renderWorkflowSuggestionOption(
                                        `${stepKey}:alias`,
                                        option,
                                        onSelect,
                                      ),
                                    )}
                                  </div>
                                ) : null}
                                {step.artifactOptions.length ? (
                                  <div className="rounded-md bg-[var(--fs-popover-bg)] p-1">
                                    <p className="px-1 pb-1 text-[10px] font-medium uppercase tracking-[0.16em] fs-ink-brand">{t("react.workflows.step_artifacts", "Step artifacts")}</p>
                                    {step.artifactOptions.map((option) =>
                                      renderWorkflowSuggestionOption(
                                        `${stepKey}:artifact`,
                                        option,
                                        onSelect,
                                      ),
                                    )}
                                  </div>
                                ) : null}
                              </div>
                            ) : null}
                          </section>
                        );
                      },
                    ),
                    label: t("react.workflows.previous_steps", "Previous steps"),
                  }
                : null,
            ] as Array<{
              childKey: string;
              count: number;
              content: ReactNode;
              label: string;
            } | null>
          )
            .filter(
              isTruthy,
            )
            .map((section) => {
              const expanded = expandedWorkflowSuggestionKeys.includes(
                section.childKey,
              );
              return (
                <section
                  className="rounded-lg border border-subtle fs-surface-inset"
                  key={section.childKey}
                >
                  <button
                    className="flex w-full items-center justify-between px-2 py-1.5 text-left"
                    onMouseDown={(event) => {
                      event.preventDefault();
                      toggleWorkflowSuggestionKey(section.childKey);
                    }}
                    type="button"
                  >
                    <span className="text-[10px] font-medium uppercase tracking-[0.18em] fs-ink-brand">
                      {section.label}
                    </span>
                    <span className="text-[10px] text-[var(--fs-popover-secondary-text)]">
                      {section.count}
                    </span>
                  </button>
                  {expanded ? <div className="border-t border-subtle p-1">{section.content}</div> : null}
                </section>
              );
            })}
        </div>}
      </div>
    );
  }

  function renderWorkflowVariableInput({
    accept,
    ariaDescribedBy,
    ariaInvalid,
    ariaLabel,
    ariaRequired,
    className,
    mode = "path",
    multiline = false,
    name,
    onChange,
    panelId,
    placeholder,
    value,
  }: {
    /** Only these variables are offered, e.g. the ones a step kind can read. */
    accept?: (path: string) => boolean;
    ariaDescribedBy?: string;
    ariaInvalid?: boolean;
    ariaLabel?: string;
    ariaRequired?: boolean;
    className: string;
    /** "template": a text with {{state.x}} inside -- suggestions follow an open "{{". */
    mode?: "path" | "template" | "destination";
    multiline?: boolean;
    name?: string;
    onChange: (nextValue: string) => void;
    panelId: string;
    placeholder: string;
    value: string;
  }): ReactNode {
    if (accept) {
      const filtered = filteredSuggestionStructure(workflowSuggestionStructure, accept);
      return createWorkflowVariableSuggestionRenderers({
        ...ctx,
        availableWorkflowVariableSuggestionCount: filtered.count,
        workflowSuggestionStructure: filtered.structure,
      }, t).renderWorkflowVariableInput({
        ariaDescribedBy,
        ariaInvalid,
        ariaLabel,
        ariaRequired,
        className,
        mode,
        multiline,
        name,
        onChange,
        panelId,
        placeholder,
        value,
      });
    }
    const hasSuggestions = availableWorkflowVariableSuggestionCount > 0;
    const template = mode === "template";
    const destination = mode === "destination";
    const knownOption = !template ? allWorkflowVariables(true).find((entry) => entry.option.value === value)?.option : undefined;
    const typed = template ? openTemplateQuery(value) : value;
    const isActive = activeWorkflowSuggestionPanelId === panelId && (!template || typed !== null);
    // A template filters by what follows its open "{{"; a path by its text once typed in.
    const query = template || typedSinceOpen ? (typed ?? "") : "";
    const matches = isActive && query.trim() ? rankVariables(allWorkflowVariables(), query) : [];
    const choose = (chosen: string) => {
      onChange(template ? insertTemplateVariable(value, chosen) : chosen);
      setActiveWorkflowSuggestionPanelId("");
    };
    const open = () => {
      if ((hasSuggestions || destination) && activeWorkflowSuggestionPanelId !== panelId) {
        if (hasSuggestions) openWorkflowSuggestionPanel(panelId);
        else setActiveWorkflowSuggestionPanelId(panelId);
        setHighlight(0);
        setTypedSinceOpen(false);
      }
    };
    const shared = {
      "aria-activedescendant":
        isActive && matches.length ? `${panelId}-option-${Math.min(highlight, matches.length - 1)}` : undefined,
      "aria-autocomplete": hasSuggestions ? ("list" as const) : undefined,
      "aria-controls": hasSuggestions ? panelId : undefined,
      "aria-describedby": ariaDescribedBy,
      "aria-expanded": hasSuggestions ? isActive : undefined,
      "aria-invalid": ariaInvalid || undefined,
      "aria-label": ariaLabel,
      "aria-required": ariaRequired || undefined,
      className,
      name,
      onBlur: () => {
        if (destination) setActiveWorkflowSuggestionPanelId("");
        else globalThis.setTimeout(() => setActiveWorkflowSuggestionPanelId(""), 120);
      },
      onClick: open,
      onChange: (event: { target: { value: string } }) => {
        const next = event.target.value;
        onChange(destination && next.trim() && next !== "state" && !next.startsWith("state.")
          ? `state.${next}` : next);
        // Every keystroke and deletion reopens and re-ranks the list.
        if (hasSuggestions || destination) {
          setActiveWorkflowSuggestionPanelId(panelId);
          setHighlight(0);
          setTypedSinceOpen(true);
        }
      },
      onFocus: open,
      onKeyDown: (event: { key: string; preventDefault: () => void; stopPropagation: () => void }) => {
        // Keep drawer/canvas keyboard handlers from consuming free-form paths.
        event.stopPropagation();
        if (event.key === "Escape") {
          event.preventDefault();
          setActiveWorkflowSuggestionPanelId("");
          return;
        }
        if (!isActive || !matches.length) return;
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          const step = event.key === "ArrowDown" ? 1 : -1;
          setHighlight((Math.min(highlight, matches.length - 1) + step + matches.length) % matches.length);
          return;
        }
        if (event.key === "Enter" || event.key === "Tab") {
          event.preventDefault(); // the focus stays in the field
          choose(matches[Math.min(highlight, matches.length - 1)].option.value);
        }
      },
      placeholder,
      role: hasSuggestions ? "combobox" : undefined,
      value: destination
        ? isActive && value.startsWith("state.") ? value.slice(6)
          : knownOption?.label || (/^state\.[A-Za-z_]\w*$/.test(value)
            ? value.slice(6).replaceAll(/[_-]+/g, " ").replaceAll(/\b\w/g, (letter) => letter.toUpperCase()) : value)
        : !isActive && knownOption?.label ? knownOption.label : value,
    };
    return (
      <div className="relative">
        {multiline ? <textarea {...shared} /> : <input {...shared} readOnly={false} />}
        {hasSuggestions && (!template || typed !== null)
          ? renderWorkflowVariableSuggestions(panelId, choose, query)
          : null}
      </div>
    );
  }

  function renderWorkflowDestinationInput(args: Parameters<typeof renderWorkflowVariableInput>[0]): ReactNode {
    const options = (ctx.workflowDestinationOptions || []) as WorkflowVariableSuggestionOption[];
    return createWorkflowVariableSuggestionRenderers({ ...ctx,
      availableWorkflowVariableSuggestionCount: options.length,
      workflowGroupLabel: t("react.workflows.workflow_results", "Workflow results"),
      workflowSuggestionStructure: { inputsGroup: null, globalGroup: null,
        workflowGroup: options.length ? { options } : null, steps: [] },
    }, t).renderWorkflowVariableInput({ ...args, mode: "destination" });
  }

  function renderProjectLinkControl( ) {
    const projectOptions = availableProjectsQuery.data || [];
    if (selectedProjectId) {
      const selectedProject = projectOptions.find(
        (project: Record<string, unknown>) => textOrEmpty(project.id) === selectedProjectId,
      );
      const projectName = textOrEmpty(selectedProject?.name);
      const projectLabel = projectName || (
        availableProjectsQuery.isLoading ? t("react.workflows.loading_project", "Loading project...") : t("react.workflows.project_unavailable", "Project unavailable")
      );
      return (
        <label className="fs-field">
          <span>{t("trash.type.project", "Project")}</span>
          <input
            className="fs-input w-full"
            readOnly
            value={projectLabel}
          />
        </label>
      );
    }
    return (
      <div className="fs-form-section">
        <p className="fs-field-label">{t("react.workflows.project_context", "Project context")}</p>
        <label className="fs-field">
          <span>{t("react.workflows.choose_a_project", "Choose a project")}</span>
          <select
            className="fs-input w-full"
            onChange={(event) => setProjectLinkDraft(event.target.value)}
            value={projectLinkDraft}
          >
            <option value="">
              {availableProjectsQuery.isLoading
                ? t("react.workflows.loading_projects", "Loading projects...")
                : t("extensions.detail.select_project", "Select a project")}
            </option>
            {projectOptions.map((project: Record<string, unknown>) => {
              const projectId = textOrEmpty(project.id);
              return (
                <option key={projectId} value={projectId}>
                  {textOrEmpty(project.name) || t("react.workflows.unnamed_project", "Unnamed project")}
                </option>
              );
            })}
          </select>
        </label>
        <button
          className="fs-button secondary"
          disabled={projectLinkPending || !textOrEmpty(projectLinkDraft)}
          onClick={() => ignoreAsyncResult(handleLinkProjectForBuilder())}
          type="button"
        >
          {projectLinkPending ? t("react.workflows.linking_project", "Linking project...") : t("react.workflows.link_project_for_test_run", "Link project for Test Run")}
        </button>
        {projectLinkMessage ? (
          <p className="fs-field-helper">{projectLinkMessage}</p>
        ) : null}
      </div>
    );
  }

  return {
    renderProjectLinkControl,
    renderWorkflowVariableInput,
    renderWorkflowDestinationInput,
  };
}
