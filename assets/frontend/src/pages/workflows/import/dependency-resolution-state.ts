import { useCallback, useMemo, useState } from "react";

import type {
  WorkflowImportDependency,
  WorkflowImportPreflight,
  WorkflowImportResolution,
} from "@/pages/workflows/import/api";
import { isTruthy } from "@/lib/utils/unknown-values";

/*
 * Dependency resolution state shared by every path that imports a workflow
 * bundle: the manual YAML import page and the "From catalog" dialog. Both call
 * the same backend preflight and apply, so the decisions an operator can make
 * about a missing child workflow, connection or extension must be the same in
 * both. The rows that edit this state live in `dependency-resolution.tsx`.
 */

export type ResolutionDraft = {
  action: string;
  target: string;
  projectId: string;
  extensionId: string;
  extensionInstallId: string;
};

const BLOCKING_STATUSES = new Set([
  "missing",
  "conflict",
  "requires_mapping",
  "requires_extension_install",
  "requires_project_connection",
  "protected_reference",
  "blocked",
]);

/** A definition the file carries: created from it, renamed, or reused under its own id. */
export function isBundledDefinition(dependency: WorkflowImportDependency) {
  return dependency.metadata?.definition === true;
}

/** A guardrail id is a UUID the system assigns; the operator never types one. */
export function systemAssignsTarget(dependency: WorkflowImportDependency, action: string) {
  return action === "rename" && dependency.dependency_type === "guardrail";
}

function draftTarget(dependency: WorkflowImportDependency, action: string, target: string) {
  if (dependency.dependency_type !== "guardrail" || !isBundledDefinition(dependency)) return target;
  if (!systemAssignsTarget(dependency, action)) return "";
  return target || globalThis.crypto.randomUUID();
}

export function dependencyRequiresTarget(dependency: WorkflowImportDependency, action: string) {
  if (systemAssignsTarget(dependency, action)) {
    return false;
  }
  if (action === "rename_workflow" || action === "rename" || action === "map_existing") {
    return true;
  }
  if (action === "use_existing") {
    // A carried child or definition whose id is taken reuses the record under that
    // same id, so there is nothing to pick; the server takes the ref as the target.
    if (dependency.dependency_type === "workflow" || isBundledDefinition(dependency)) {
      return false;
    }
    return dependency.status !== "resolved";
  }
  if (action === "install_extension_required") {
    return dependency.status !== "resolved";
  }
  return false;
}

export function defaultActionForDependency(dependency: WorkflowImportDependency) {
  const actions = dependency.resolution_actions || [];
  if (dependency.status === "conflict" && actions.includes("rename_workflow")) {
    return "rename_workflow";
  }
  if (dependency.status === "conflict" && isBundledDefinition(dependency)) {
    return actions.includes("use_existing") ? "use_existing" : "rename";
  }
  if (dependency.status === "requires_extension_install") {
    return "install_extension_required";
  }
  if (dependency.status === "requires_project_connection") {
    return actions.includes("map_existing") ? "map_existing" : "create_connection_placeholder";
  }
  if (actions.includes("map_existing")) {
    return "map_existing";
  }
  return actions[0] || "abort";
}

export function dependencyNeedsResolution(dependency: WorkflowImportDependency) {
  return Boolean(dependency.blocking || BLOCKING_STATUSES.has(dependency.status));
}

/**
 * A project connection resolves in the project selected for the import unless the
 * operator typed another. Drafts never copy the selection: it can change after the
 * first check, and a copied id would keep the earlier project (or none).
 */
function draftProjectId(
  dependency: WorkflowImportDependency,
  draft: ResolutionDraft,
  selectedProjectId: string,
) {
  if (dependency.dependency_type !== "project_connection") return draft.projectId.trim();
  return draft.projectId.trim() || selectedProjectId.trim();
}

export function resolutionComplete(
  dependency: WorkflowImportDependency,
  draft: ResolutionDraft | undefined,
  selectedProjectId = "",
) {
  if (!dependencyNeedsResolution(dependency)) {
    return true;
  }
  if (dependency.status === "protected_reference") {
    return false;
  }
  if (!draft?.action || draft.action === "abort") {
    return false;
  }
  if (dependencyRequiresTarget(dependency, draft.action)) {
    return Boolean(draft.target.trim());
  }
  if (draft.action === "create_connection_placeholder") {
    return Boolean(
      draftProjectId(dependency, draft, selectedProjectId) &&
        (draft.extensionId.trim() || draft.extensionInstallId.trim()),
    );
  }
  return true;
}

export function resolutionPayload(
  dependencies: WorkflowImportDependency[],
  drafts: Record<string, ResolutionDraft>,
  selectedProjectId = "",
) {
  return dependencies
    .map((dependency): WorkflowImportResolution | null => {
      const draft = drafts[dependency.dependency_key];
      if (
        !draft?.action ||
        draft.action === "abort" ||
        !resolutionComplete(dependency, draft, selectedProjectId)
      ) {
        return null;
      }
      const projectId = draftProjectId(dependency, draft, selectedProjectId);
      const payload: WorkflowImportResolution = {
        dependency_key: dependency.dependency_key,
        dependency_type: dependency.dependency_type,
        ref: dependency.ref,
        action: draft.action,
      };
      // Reusing a definition keeps its own id, so a target left over from a rename is dropped.
      const reusesDefinition = draft.action === "use_existing" && isBundledDefinition(dependency);
      if (draft.action === "rename_workflow") {
        payload.target_workflow_id = draft.target.trim();
      } else if (draft.target.trim() && !reusesDefinition) {
        payload.target_ref = draft.target.trim();
      }
      if (projectId) {
        payload.project_id = projectId;
      }
      if (draft.action === "create_connection_placeholder") {
        payload.project_id = projectId;
        if (draft.extensionId.trim()) {
          payload.extension_id = draft.extensionId.trim();
        }
        if (draft.extensionInstallId.trim()) {
          payload.extension_install_id = draft.extensionInstallId.trim();
        }
      }
      return payload;
    })
    .filter(isTruthy);
}

export function mergeDependencyBasis(
  original: WorkflowImportDependency[],
  checked: WorkflowImportDependency[],
): WorkflowImportDependency[] {
  const merged = [...original];
  const positions = new Map(original.map((dependency, index) => [dependency.dependency_key, index]));
  for (const dependency of checked) {
    const position = positions.get(dependency.dependency_key);
    if (position === undefined) {
      positions.set(dependency.dependency_key, merged.length);
      merged.push(dependency);
    } else if (!dependencyNeedsResolution(merged[position]) && dependencyNeedsResolution(dependency)) {
      // A changed target or server state may reveal a new decision on an
      // identity that was resolved in the original check.
      merged[position] = dependency;
    }
  }
  return merged;
}

export type UpdateResolution = (
  dependency: WorkflowImportDependency,
  patch: Partial<ResolutionDraft>,
) => void;

function initialResolutionDrafts(
  dependencies: WorkflowImportDependency[],
): Record<string, ResolutionDraft> {
  const drafts: Record<string, ResolutionDraft> = {};
  dependencies.forEach((dependency) => {
    if (!dependencyNeedsResolution(dependency)) {
      return;
    }
    const action = defaultActionForDependency(dependency);
    drafts[dependency.dependency_key] = {
      action,
      target: draftTarget(dependency, action, ""),
      projectId: "",
      extensionId: "",
      extensionInstallId: "",
    };
  });
  return drafts;
}

/** The operator's editable choices, keyed by the backend dependency identity. */
export function useDependencyResolution(
  preflight: WorkflowImportPreflight | null,
  projectId: string,
) {
  const [resolutions, setResolutions] = useState<Record<string, ResolutionDraft>>({});

  const dependencies = useMemo(() => preflight?.dependencies || [], [preflight]);
  const blockingDependencies = useMemo(
    () => dependencies.filter((dependency) => dependencyNeedsResolution(dependency)),
    [dependencies],
  );
  const requiredResolutionsReady = blockingDependencies.every((dependency) =>
    resolutionComplete(dependency, resolutions[dependency.dependency_key], projectId),
  );
  const resolutionList = useMemo(
    () => resolutionPayload(dependencies, resolutions, projectId),
    [dependencies, resolutions, projectId],
  );
  // Topology findings are independent of the editable dependency choices.
  // The caller combines local completeness with its verified server plan.
  const topologyIssues = useMemo(() => preflight?.topology_issues || [], [preflight]);
  const blockingTopologyIssues = useMemo(
    () => topologyIssues.filter((issue) => issue.blocking),
    [topologyIssues],
  );
  const resetResolutions = useCallback(
    (next: WorkflowImportPreflight | null) => {
      setResolutions(next ? initialResolutionDrafts(next.dependencies) : {});
    },
    [],
  );

  const addResolutionDependencies = useCallback(
    (nextDependencies: WorkflowImportDependency[]) => {
      const defaults = initialResolutionDrafts(nextDependencies);
      setResolutions((current) => ({ ...defaults, ...current }));
    },
    [],
  );

  const updateResolution = useCallback<UpdateResolution>((dependency, patch) => {
    setResolutions((current) => {
      const action =
        patch.action ?? current[dependency.dependency_key]?.action ?? defaultActionForDependency(dependency);
      const target = patch.target ?? current[dependency.dependency_key]?.target ?? "";
      return {
      ...current,
      [dependency.dependency_key]: {
        action,
        target: draftTarget(dependency, action, target),
        projectId: patch.projectId ?? current[dependency.dependency_key]?.projectId ?? "",
        extensionId: patch.extensionId ?? current[dependency.dependency_key]?.extensionId ?? "",
        extensionInstallId:
          patch.extensionInstallId ?? current[dependency.dependency_key]?.extensionInstallId ?? "",
      },
      };
    });
  }, []);

  return {
    addResolutionDependencies,
    blockingDependencies,
    blockingTopologyIssues,
    dependencies,
    requiredResolutionsReady,
    resetResolutions,
    resolutionList,
    resolutions,
    topologyIssues,
    updateResolution,
  };
}
