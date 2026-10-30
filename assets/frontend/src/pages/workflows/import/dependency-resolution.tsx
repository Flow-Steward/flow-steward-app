import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useId, useMemo, useState } from "react";

import { DsBadge } from "@/components/shared/design-system";
import type {
  WorkflowImportDependency,
  WorkflowImportDependencyOption,
} from "@/pages/workflows/import/api";
import {
  defaultActionForDependency,
  dependencyNeedsResolution,
  dependencyRequiresTarget,
  isBundledDefinition,
  optionsForDependency,
  type ResolutionDraft,
  resolutionComplete,
  systemAssignsTarget,
  type UpdateResolution,
} from "@/pages/workflows/import/dependency-resolution-state";
import { isTruthy } from "@/lib/utils/unknown-values";

/* The rows that edit the shared resolution state, used by the manual YAML
   import page and the "From catalog" dialog alike. */

const DEPENDENCY_LABELS: Record<string, string> = {
  agent: "agent",
  skill: "skill",
  tool: "tool",
  child_workflow: "workflow",
  published_block: "published block",
  extension: "extension install",
  project_connection: "project connection",
};

/**
 * Blocked and conflicting dependencies are broken; everything else that still
 * needs a decision is orange, which is what "a human is needed" means here.
 *
 * A resolved dependency is normal operation and stays neutral — the same reason
 * the summary header does not paint a valid workflow green. Measured, that is
 * also the readable choice: the published green pair is `#3fa88f` on the dark
 * inset surface, 4.18:1 at 11px against the 4.5 body threshold, and a wizard
 * showing twenty-eight of them would have been twenty-eight failures.
 */
function statusTone(status: string): "neutral" | "green" | "orange" | "danger" {
  if (status === "resolved" || status === "protected_reference") {
    return "neutral";
  }
  if (status === "blocked" || status === "conflict") {
    return "danger";
  }
  return "orange";
}

function dependencyLabel(dependency: WorkflowImportDependency, t: I18nContextValue["t"] = fallbackContext.t) {
  const labels: Record<string, string> = {
    agent: t("react.workflows.import_agent", "agent"),
    skill: t("react.workflows.import_skill", "skill"),
    tool: t("react.workflows.import_tool", "tool"),
    child_workflow: t("react.workflows.import_dependency_workflow", "workflow"),
    workflow: t("react.workflows.import_dependency_workflow", "workflow"),
    published_block: t("react.workflows.import_published_block", "published block"),
    extension: t("react.workflows.import_extension_install", "extension install"),
    project_connection: t("react.workflows.import_project_connection", "project connection"),
    hook_binding: t("react.workflows.import_hook_binding", "hook connection"),
    data_contract: t("react.workflows.import_data_contract", "data contract"),
    guardrail: t("react.workflows.import_guardrail", "guardrail"),
    guardrail_project: t("react.workflows.import_guardrail_project", "project for guardrails"),
  };
  return labels[dependency.dependency_type] || dependency.dependency_type.replaceAll("_", " ");
}

function optionBucketForDependency(dependency: WorkflowImportDependency, action: string) {
  if (action === "rename_workflow" || action === "rename" || action === "create_connection_placeholder") {
    return "";
  }
  if (dependency.dependency_type === "extension_workflow_ref") {
    return "extension";
  }
  if (dependency.dependency_type in DEPENDENCY_LABELS) {
    return dependency.dependency_type;
  }
  return "";
}

function actionLabel(action: string, t: I18nContextValue["t"] = fallbackContext.t) {
  const labels: Record<string, string> = {
    abort: t("react.workflows.import_abort", "Abort import"),
    create_connection_placeholder: t("react.workflows.import_create_connection", "Create connection placeholder"),
    install_extension_required: t("react.workflows.import_use_extension", "Use installed extension"),
    map_existing: t("react.workflows.import_map_existing", "Map to existing"),
    rename_workflow: t("react.workflows.import_rename", "Rename workflow"),
    rename: t("react.workflows.import_rename_definition", "Create under a new id"),
    skip_optional: t("react.workflows.import_skip", "Skip optional"),
    use_existing: t("react.workflows.import_use_existing", "Use existing"),
  };
  return labels[action] || action;
}

function dependencyStatusLabel(status: string, t: I18nContextValue["t"] = fallbackContext.t) {
  const labels: Record<string, string> = {
    resolved: t("react.workflows.import_status_resolved", "resolved"),
    create_from_bundle: t("react.workflows.import_status_from_file", "created from file"),
    missing: t("react.workflows.import_status_missing", "missing"),
    conflict: t("react.workflows.import_status_conflict", "conflict"),
    requires_mapping: t("react.workflows.import_status_mapping", "requires_mapping"),
    requires_extension_install: t("react.workflows.import_status_install", "requires_extension_install"),
    requires_project_connection: t("react.workflows.import_status_connection", "requires_project_connection"),
    protected_reference: t("react.workflows.import_status_protected", "protected_reference"),
    blocked: t("react.workflows.import_status_blocked", "blocked"),
  };
  return labels[status] || status;
}

function visibleResolutionActions(dependency: WorkflowImportDependency) {
  const actions = dependency.resolution_actions || [];
  if (dependency.status === "requires_mapping") {
    return actions.filter((action) => action !== "use_existing");
  }
  return actions;
}

function targetControlLabel(dependency: WorkflowImportDependency, action: string, t: I18nContextValue["t"] = fallbackContext.t) {
  if (action === "rename_workflow") {
    return t("react.workflows.new_workflow_id_for_value0", "New workflow id for {value0}", { value0: dependency.ref });
  }
  if (action === "rename") {
    return t("react.workflows.new_id_for_value0", "New id for {value0}", { value0: dependency.ref });
  }
  return t("react.workflows.import_existing_target", "Existing {type} for {ref}", { type: dependencyLabel(dependency, t), ref: dependency.ref });
}

function optionLabel(option: WorkflowImportDependencyOption) {
  return option.label === option.value ? option.label : `${option.label} (${option.value})`;
}

function optionSearchText(option: WorkflowImportDependencyOption) {
  return [option.label, option.value, option.description, option.status]
    .filter(isTruthy)
    .join(" ")
    .toLowerCase();
}

function SearchableDependencyPicker({
  ariaLabel,
  disabled = false,
  emptyLabel,
  onChange,
  options,
  placeholder,
  value,
}: Readonly<{
  ariaLabel: string;
  disabled?: boolean;
  emptyLabel: string;
  onChange: (value: string) => void;
  options: WorkflowImportDependencyOption[];
  placeholder: string;
  value: string;
}>) {
  const { t } = useI18n();
  const listboxId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const selected = options.find((option) => option.value === value);
  const normalizedQuery = query.trim().toLowerCase();
  const filteredOptions = useMemo(() => {
    if (!normalizedQuery) {
      return options.slice(0, 12);
    }
    return options
      .filter((option) => optionSearchText(option).includes(normalizedQuery))
      .slice(0, 12);
  }, [normalizedQuery, options]);
  let inputValue = "";
  if (open) {
    inputValue = query;
  } else if (selected) {
    inputValue = optionLabel(selected);
  }

  return (
    <div className="fs-combo">
      <input
        aria-controls={listboxId}
        aria-expanded={open}
        aria-label={ariaLabel}
        className="fs-input"
        disabled={disabled}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
        }}
        onFocus={() => {
          setQuery("");
          setOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape" && open) {
            // Handled here, so a dialog around the picker does not also close.
            event.preventDefault();
            setOpen(false);
            setQuery("");
          }
        }}
        placeholder={placeholder}
        role="combobox"
        value={inputValue}
      />
      {open && !disabled ? (
        <div className="fs-combo-menu" id={listboxId} role="listbox">
          {filteredOptions.length ? (
            filteredOptions.map((option) => (
              <button
                aria-selected={option.value === value}
                className="fs-combo-option"
                key={option.value}
                onClick={() => {
                  onChange(option.value);
                  setOpen(false);
                  setQuery("");
                }}
                role="option"
                title={option.description || undefined}
                type="button"
              >
                {/* One line per option, as the published option row is: the
                    identifier disambiguates inline and the description, which can
                    run long, goes to the title. */}
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className="truncate">{option.label}</span>
                  <span className="fs-field-helper truncate">{option.value}</span>
                </span>
              </button>
            ))
          ) : (
            <span className="fs-combo-empty">{emptyLabel}</span>
          )}
          {options.length > filteredOptions.length ? (
            <span className="fs-combo-empty">{t("react.workflows.type_to_narrow_results", "Type to narrow results.")}</span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function unresolvedHint(
  dependency: WorkflowImportDependency,
  action: string,
  needsTarget: boolean, t: I18nContextValue["t"] = fallbackContext.t
) {
  if (dependency.status === "blocked" || action === "abort") {
    return t("react.workflows.fix_the_source_yaml_before_import", "Fix the source YAML before import.");
  }
  if (dependency.status === "protected_reference") {
    return t("react.workflows.install_the_extension_before_importing_this_reference", "Install the extension before importing this reference.");
  }
  if (action === "create_connection_placeholder") {
    return t("react.workflows.enter_project_and_extension_details_before_import", "Enter project and extension details before import.");
  }
  if (needsTarget) {
    return t("react.workflows.choose_a_target_before_import", "Choose a target before import.");
  }
  return t("react.workflows.resolve_this_dependency_before_import", "Resolve this dependency before import.");
}

function ResolutionActionControl({
  actions,
  dependency,
  needsResolution,
  selectedAction,
  updateResolution,
}: Readonly<{
  actions: string[];
  dependency: WorkflowImportDependency;
  needsResolution: boolean;
  selectedAction: string;
  updateResolution: UpdateResolution;
}>) {
  const { t } = useI18n();
  if (!needsResolution) return <span className="fs-field-helper">{t("react.workflows.no_action", "No action")}</span>;
  if (actions.length <= 1) return <span>{actionLabel(actions[0] || selectedAction, t)}</span>;
  return (
    <select aria-label={t("react.workflows.resolution_for_value0", "Resolution for {value0}", { value0: dependency.ref })} className="fs-input" onChange={(event) => updateResolution(dependency, { action: event.target.value, target: "" })} value={selectedAction}>
      {actions.map((action) => <option key={action} value={action}>{actionLabel(action, t)}</option>)}
    </select>
  );
}

function ResolutionTargetControl({
  dependency,
  draft,
  error,
  needsResolution,
  needsTarget,
  optionBucket,
  selectedAction,
  targetOptions,
  updateResolution,
}: Readonly<{
  dependency: WorkflowImportDependency;
  draft?: ResolutionDraft;
  error?: string;
  needsResolution: boolean;
  needsTarget: boolean;
  optionBucket: string;
  selectedAction: string;
  targetOptions: WorkflowImportDependencyOption[];
  updateResolution: UpdateResolution;
}>) {
  const { t } = useI18n();
  if (!needsResolution) return <span className="fs-field-helper">{t("react.workflows.resolved", "Resolved")}</span>;
  if (needsTarget && optionBucket) {
    return (
      <div>
        <SearchableDependencyPicker
          ariaLabel={targetControlLabel(dependency, selectedAction, t)}
          disabled={!targetOptions.length}
          emptyLabel={t("react.workflows.import_no_matching", "No matching {type} found.", { type: dependencyLabel(dependency, t) })}
          onChange={(target) => updateResolution(dependency, { target })}
          options={targetOptions}
          placeholder={targetOptions.length ? t("react.workflows.import_search_type", "Search {type}", { type: dependencyLabel(dependency, t) }) : t("react.workflows.import_no_available", "No available items: {type}", { type: dependencyLabel(dependency, t) })}
          value={draft?.target || ""}
        />
        {targetOptions.length ? <span className="fs-field-helper">{t("react.workflows.import_search_help", "Search by name or id, then choose the replacement {type}.", { type: dependencyLabel(dependency, t) })}</span> : null}
      </div>
    );
  }
  if (systemAssignsTarget(dependency, selectedAction)) {
    return <span className="fs-field-helper">{t("react.workflows.import_guardrail_new_id", "Created as a new guardrail; its id is assigned automatically.")}</span>;
  }
  if (needsTarget) {
    return <input aria-invalid={error ? true : undefined} aria-label={targetControlLabel(dependency, selectedAction, t)} className="fs-input" onChange={(event) => updateResolution(dependency, { target: event.target.value })} placeholder={selectedAction === "rename" ? t("react.workflows.enter_a_new_id", "Enter a new id") : t("react.workflows.enter_a_new_workflow_id", "Enter a new workflow id")} value={draft?.target || ""} />;
  }
  if (selectedAction === "use_existing" && isBundledDefinition(dependency)) {
    return <span className="fs-field-helper">{t("react.workflows.uses_the_one_already_here_unchanged", "Uses the one already here, unchanged:")}{" "}{dependency.ref}.</span>;
  }
  if (selectedAction === "use_existing") {
    return <span className="fs-field-helper">{t("react.workflows.uses_the_workflow_already_here_as", "Uses the workflow already here as")}{" "}{dependency.ref}.</span>;
  }
  if (selectedAction !== "create_connection_placeholder") {
    return <span className="fs-field-helper">{t("react.workflows.cannot_be_resolved_in_this_wizard", "Cannot be resolved in this wizard.")}</span>;
  }
  return null;
}

function ConnectionPlaceholderFields({ dependency, draft, selectedProjectId, updateResolution }: Readonly<{
  dependency: WorkflowImportDependency;
  draft?: ResolutionDraft;
  selectedProjectId: string;
  updateResolution: UpdateResolution;
}>) {
  const { t } = useI18n();
  if (draft?.action !== "create_connection_placeholder") return null;
  return (
    <div className="mt-2 grid gap-2 sm:grid-cols-3">
      <input aria-label={t("react.workflows.project_id_for_value0", "Project id for {value0}", { value0: dependency.ref })} className="fs-input" onChange={(event) => updateResolution(dependency, { projectId: event.target.value })} placeholder={selectedProjectId || t("react.workflows.project_id", "Project id")} value={draft.projectId} />
      <input aria-label={t("react.workflows.extension_id_for_value0", "Extension id for {value0}", { value0: dependency.ref })} className="fs-input" onChange={(event) => updateResolution(dependency, { extensionId: event.target.value })} placeholder={t("react.workflows.extension_id", "Extension id")} value={draft.extensionId} />
      <input aria-label={t("react.workflows.extension_install_id_for_value0", "Extension install id for {value0}", { value0: dependency.ref })} className="fs-input" onChange={(event) => updateResolution(dependency, { extensionInstallId: event.target.value })} placeholder={t("react.workflows.extension_install_id", "Extension install id")} value={draft.extensionInstallId} />
    </div>
  );
}

function DependencyResolutionRow({ dependency, dependencyOptions, effectiveDependency, accepted, draft, error, selectedProjectId, updateResolution }: Readonly<{
  dependency: WorkflowImportDependency;
  dependencyOptions: Record<string, WorkflowImportDependencyOption[]>;
  effectiveDependency?: WorkflowImportDependency;
  accepted: boolean;
  draft?: ResolutionDraft;
  error?: string;
  selectedProjectId: string;
  updateResolution: UpdateResolution;
}>) {
  const { t } = useI18n();
  const actions = visibleResolutionActions(dependency);
  const selectedAction = draft?.action || defaultActionForDependency(dependency);
  const needsTarget = dependencyRequiresTarget(dependency, selectedAction);
  const optionBucket = optionBucketForDependency(dependency, selectedAction);
  const targetOptions = optionsForDependency(dependency, optionBucket ? dependencyOptions[optionBucket] || [] : []);
  const complete = resolutionComplete(dependency, draft, selectedProjectId);
  const needsResolution = dependencyNeedsResolution(dependency);
  // Keep controls and serialization tied to the original dependency identity.
  // The effective server row supplies only its checked status and message.
  const status = effectiveDependency?.status ?? (accepted && needsResolution ? "resolved" : dependency.status);
  // Answered by the project picker at the top of the page, not by a control in this row.
  const choosesProject = dependency.dependency_type === "guardrail_project";
  const message = effectiveDependency?.message ?? (status === "resolved" ? "" : dependency.message);
  return (
    <div className="fs-surface-radius fs-surface-inset border border-subtle grid gap-3 p-3 md:grid-cols-12">
      <div className="md:col-span-5 min-w-0">
        <div className="font-medium">{dependency.label || dependency.ref}</div>
        <div className="fs-field-helper">{dependency.dependency_type} / {dependency.ref}</div>
        {message ? <div className="fs-field-helper">{message}</div> : null}
      </div>
      <div className="md:col-span-3"><span className="fs-field-label">{t("extensions.runtime.status", "Status")}</span><DsBadge tone={statusTone(status)}>{dependencyStatusLabel(status, t)}</DsBadge></div>
      <div className="fs-field md:col-span-4">
        <span className="fs-field-label">{t("security_overview.columns.action", "Action")}</span>
        <ResolutionActionControl actions={actions} dependency={dependency} needsResolution={needsResolution} selectedAction={selectedAction} updateResolution={updateResolution} />
      </div>
      <div className="fs-field md:col-span-12">
        <span className="fs-field-label">{t("settings.notifications.columns.target", "Target")}</span>
        {choosesProject ? (
          <span className="fs-field-helper">{status === "resolved" ? t("react.workflows.resolved", "Resolved") : t("react.workflows.import_choose_project_above", "Choose the project above.")}</span>
        ) : (
          <ResolutionTargetControl dependency={dependency} draft={draft} error={error} needsResolution={needsResolution} needsTarget={needsTarget} optionBucket={optionBucket} selectedAction={selectedAction} targetOptions={targetOptions} updateResolution={updateResolution} />
        )}
        <ConnectionPlaceholderFields dependency={dependency} draft={draft} selectedProjectId={selectedProjectId} updateResolution={updateResolution} />
        {error ? <span className="fs-field-helper error">{error}</span> : null}
        {!choosesProject && !complete && !error && status !== "resolved" ? <span className="fs-field-helper error">{unresolvedHint(dependency, selectedAction, needsTarget, t)}</span> : null}
      </div>
    </div>
  );
}

/** One inset row per dependency, each with the controls its status allows. */
export function DependencyResolutionList({
  dependencies,
  dependencyOptions,
  effectiveDependencies,
  accepted = false,
  resolutions,
  selectedProjectId = "",
  updateResolution,
  targetErrors,
}: Readonly<{
  dependencies: WorkflowImportDependency[];
  dependencyOptions: Record<string, WorkflowImportDependencyOption[]>;
  effectiveDependencies?: WorkflowImportDependency[];
  accepted?: boolean;
  resolutions: Record<string, ResolutionDraft>;
  /** The project chosen for the import; a project connection resolves in it unless another is typed. */
  selectedProjectId?: string;
  updateResolution: UpdateResolution;
  /** A caller's own verdict on a chosen target, by dependency key (a taken id). */
  targetErrors?: Record<string, string>;
}>) {
  return (
    <>
      {dependencies.map((dependency) => (
        <DependencyResolutionRow
          dependency={dependency}
          dependencyOptions={dependencyOptions}
          effectiveDependency={effectiveDependencies?.find((checked) => checked.dependency_key === dependency.dependency_key)}
          accepted={accepted}
          draft={resolutions[dependency.dependency_key]}
          error={targetErrors?.[dependency.dependency_key]}
          key={dependency.dependency_key}
          selectedProjectId={selectedProjectId}
          updateResolution={updateResolution}
        />
      ))}
    </>
  );
}
