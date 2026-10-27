import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  createWorkflowFromCatalogItem,
  preflightWorkflowCatalogItem,
  type WorkflowCatalogCreateResult,
  type WorkflowCatalogExtensionResolution,
  type WorkflowCatalogItem,
  type WorkflowCatalogPreflight,
} from "@/pages/workflows/catalog/api";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { freeWorkflowId } from "@/pages/workflows/catalog/catalog-model";
import {
  fetchWorkflowImportOptions,
  type WorkflowImportDependency,
  type WorkflowImportDependencyOption,
  type WorkflowImportResolution,
} from "@/pages/workflows/import/api";
import {
  dependencyNeedsResolution,
  mergeDependencyBasis,
  resolutionComplete,
  resolutionPayload,
  useDependencyResolution,
  type UpdateResolution,
} from "@/pages/workflows/import/dependency-resolution-state";
import { WORKFLOW_ID_PATTERN } from "@/pages/workflows/list/create-workflow-state";
import { fetchWorkflows } from "@/pages/workflows/shared/api";

/** Workflow id -> enabled for every workflow already here; null when it could not be read. */
async function loadExistingWorkflows(): Promise<Map<string, boolean> | null> {
  try {
    const rows = await fetchWorkflows({ enabledOnly: false, search: "" });
    return Array.isArray(rows) ? new Map(rows.map((row) => [row.workflow_id, Boolean(row.enabled)])) : null;
  } catch {
    return null;
  }
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

/** Where the bundle's own root workflow sits in a preflight. */
function rootWorkflowId(preflight: WorkflowCatalogPreflight, item: WorkflowCatalogItem): string {
  const declared = (preflight as { root_workflow_id?: string }).root_workflow_id;
  if (declared) return declared;
  const root = preflight.workflows.find((entry) => (entry as { role?: string }).role === "root");
  return root?.workflow_id || preflight.workflows[0]?.workflow_id || item.item_id;
}

/** The root's own id conflict is answered by the Workflow ID field, not by a row. */
function isRootConflict(dependency: WorkflowImportDependency, rootId: string) {
  return (
    dependency.dependency_type === "workflow" &&
    dependency.status === "conflict" &&
    dependency.ref === rootId
  );
}

/** A carried child whose id is already taken here. */
function isChildConflict(dependency: WorkflowImportDependency, rootId: string) {
  return (
    dependency.dependency_type === "workflow" &&
    dependency.status === "conflict" &&
    dependency.ref !== rootId
  );
}

/**
 * An extension dependency the catalog's own extension check already reports:
 * its row would repeat that card under the exporting machine's install id. An
 * ambiguous one keeps its row, because that row is where the install is picked.
 */
function coveredByExtensionCheck(
  dependency: WorkflowImportDependency,
  issues: readonly WorkflowCatalogExtensionResolution[],
) {
  if (dependency.dependency_type !== "extension") return false;
  const declared = dependency.metadata?.extension_id;
  const logicalId = typeof declared === "string" && declared ? declared : dependency.ref;
  return issues.some(
    (issue) => issue.outcome !== "ambiguous" && (issue.extension_id === logicalId || issue.extension_id === dependency.ref),
  );
}

/** Why a dependency cannot be created here, in the words the preflight used. */
function blockedReason(dependency: WorkflowImportDependency, t: I18nContextValue["t"] = fallbackContext.t): string {
  const metadata = dependency.metadata ?? {};
  const detail = typeof metadata.message === "string" ? metadata.message : "";
  const path = typeof metadata.path === "string" ? metadata.path : "";
  const text = detail || dependency.message || t("react.workflows.catalog_dependency_unresolved", "{name} cannot be resolved here.", { name: dependency.label || dependency.ref });
  return path ? `${text} (${path})` : text;
}

/** Statuses no choice on this screen can clear. */
const CANNOT_CREATE = new Set(["blocked", "protected_reference"]);

type SetupPhase = "checking" | "error" | "ready";
type CheckedCatalogPlan = {
  identity: string;
  sequence: number;
  itemId: string;
  projectId: string;
  targetWorkflowId: string;
  resolutions: WorkflowImportResolution[];
};

function catalogPlanIdentity(
  itemId: string,
  projectId: string,
  workflowId: string,
  resolutions: WorkflowImportResolution[],
) {
  return JSON.stringify([itemId, projectId, workflowId.trim(), resolutions]);
}


/**
 * One template's set-up: the preflight against this installation, the choices
 * the operator makes about it, and the create request. Every choice goes
 * through the shared dependency-resolution state the manual YAML import uses,
 * so both paths resolve a bundle the same way.
 */
export function useCatalogSetup(item: WorkflowCatalogItem | null, initialProjectId: string) {
  const { t } = useI18n();
  const [phase, setPhase] = useState<SetupPhase>("checking");
  const [checkError, setCheckError] = useState("");
  const [preflight, setPreflight] = useState<WorkflowCatalogPreflight | null>(null);
  const [options, setOptions] = useState<Record<string, WorkflowImportDependencyOption[]>>({});
  const [projectId, setProjectId] = useState(initialProjectId);
  const [workflowId, setWorkflowId] = useState("");
  const [checkedWorkflowId, setCheckedWorkflowId] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");
  // The workflows already here, read with every check, so a suggested id is
  // one that is actually free and a reused child's state is known.
  const [existing, setExisting] = useState<ReadonlyMap<string, boolean> | null>(null);
  const resolution = useDependencyResolution(preflight, projectId);
  const {
    addResolutionDependencies,
    resetResolutions,
    resolutions,
    updateResolution: applyResolution,
  } = resolution;
  const sequence = useRef(0);
  const checkedRef = useRef<CheckedCatalogPlan | null>(null);
  const [checkedPlan, setCheckedPlan] = useState<CheckedCatalogPlan | null>(null);
  const basisRef = useRef<WorkflowCatalogPreflight | null>(null);
  const resolutionsRef = useRef(resolutions);
  const preflightRef = useRef<WorkflowCatalogPreflight | null>(null);
  useEffect(() => {
    resolutionsRef.current = resolutions;
    preflightRef.current = preflight;
  }, [preflight, resolutions]);

  const invalidateCheck = useCallback(() => {
    sequence.current += 1;
    checkedRef.current = null;
    setCheckedPlan(null);
    setCheckError("");
    setCreateError("");
    if (preflightRef.current) setPhase("ready");
  }, []);

  const updateResolution = useCallback<UpdateResolution>(
    (dependency, patch) => {
      invalidateCheck();
      applyResolution(dependency, patch);
    },
    [applyResolution, invalidateCheck],
  );

  const runCheck = useCallback(
    async (
      target: WorkflowCatalogItem | null,
      next: { projectId: string; workflowId: string },
    ): Promise<WorkflowCatalogPreflight | null> => {
      if (!target) return null;
      sequence.current += 1;
      const ticket = sequence.current;
      checkedRef.current = null;
      setCheckedPlan(null);
      setPhase("checking");
      setCheckError("");
      // A re-check sends what was already chosen, so the server can say
      // whether a rename or a mapping now resolves.
      // Only finished choices go; the root's own id travels as the target id.
      const previousPreflight = basisRef.current;
      const previousRoot = previousPreflight ? rootWorkflowId(previousPreflight, target) : "";
      const chosen = previousPreflight
        ? resolutionPayload(
            previousPreflight.dependencies.filter(
              (dependency) =>
                !isRootConflict(dependency, previousRoot) &&
                resolutionComplete(dependency, resolutionsRef.current[dependency.dependency_key]),
            ),
            resolutionsRef.current,
          )
        : [];
      try {
        const [payload, optionsPayload, existingNow] = await Promise.all([
          preflightWorkflowCatalogItem(target.item_id, {
            projectId: next.projectId || undefined,
            targetWorkflowId: next.workflowId || undefined,
            ...(chosen.length ? { resolutions: chosen } : {}),
          }),
          fetchWorkflowImportOptions({ projectId: next.projectId || undefined }),
          loadExistingWorkflows(),
        ]);
        if (ticket !== sequence.current) return null;
        const previous = resolutionsRef.current;
        const previousBasis = basisRef.current;
        if (previousBasis) {
          basisRef.current = {
            ...previousBasis,
            dependencies: mergeDependencyBasis(previousBasis.dependencies, payload.dependencies),
          };
          addResolutionDependencies(payload.dependencies);
        } else {
          basisRef.current = payload;
          resetResolutions(payload);
        }
        setPreflight(payload);
        setOptions(optionsPayload.options || {});
        setExisting(existingNow);
        const checked = next.workflowId || rootWorkflowId(payload, target);
        const payloadRoot = rootWorkflowId(payload, target);
        // A re-check keeps what was already chosen; a carried child whose id is
        // taken starts with an id that is free here and not promised to
        // another workflow of this template.
        const promised = new Set<string>([...(existingNow?.keys() ?? []), checked]);
        for (const dependency of payload.dependencies) {
          const draft = previous[dependency.dependency_key];
          if (draft?.target) promised.add(draft.target.trim());
        }
        for (const dependency of payload.dependencies) {
          const draft = previous[dependency.dependency_key];
          if (draft && !previousBasis) applyResolution(dependency, draft);
          else if (!draft && isChildConflict(dependency, payloadRoot)) {
            const suggestion = freeWorkflowId(dependency.ref, promised);
            promised.add(suggestion);
            applyResolution(dependency, { action: "rename_workflow", target: suggestion });
          }
        }
        setWorkflowId((current) => current || checked);
        setCheckedWorkflowId(checked);
        if (payload.can_apply) {
          const accepted = {
            identity: catalogPlanIdentity(target.item_id, next.projectId, checked, chosen),
            sequence: ticket,
            itemId: target.item_id,
            projectId: next.projectId,
            targetWorkflowId: next.workflowId.trim(),
            resolutions: chosen,
          };
          checkedRef.current = accepted;
          setCheckedPlan(accepted);
        }
        setPhase("ready");
        return payload;
      } catch (error) {
        if (ticket !== sequence.current) return null;
        setCheckError(errorMessage(error, t("react.workflows.this_template_could_not_be_checked", "This template could not be checked.")));
        setPhase("error");
        return null;
      }
    },
    [addResolutionDependencies, applyResolution, resetResolutions, t],
  );

  const rootId = preflight && item ? rootWorkflowId(preflight, item) : "";
  const trimmedId = workflowId.trim();
  const idValid = WORKFLOW_ID_PATTERN.test(trimmedId);

  // A typed Workflow ID is checked against this installation once typing pauses.
  useEffect(() => {
    if (phase !== "ready" || !idValid || trimmedId === checkedWorkflowId) return undefined;
    const timer = globalThis.setTimeout(() => {
      ignoreAsyncResult(runCheck(item, { projectId, workflowId: trimmedId }));
    }, 500);
    return () => globalThis.clearTimeout(timer);
  }, [checkedWorkflowId, idValid, item, phase, projectId, runCheck, trimmedId]);

  const dependencies = useMemo(() => preflight?.dependencies ?? [], [preflight]);
  const rootConflict = dependencies.some((dependency) => isRootConflict(dependency, rootId));
  const extensionIssues = (preflight?.catalog_extension_resolutions ?? []).filter(
    (entry) => entry.outcome !== "resolved",
  );
  const editableDependencies = basisRef.current?.dependencies ?? dependencies;
  const decisionRows = editableDependencies.filter(dependencyNeedsResolution).filter(
    (dependency) =>
      !isRootConflict(dependency, rootId) &&
      !CANNOT_CREATE.has(dependency.status) &&
      !coveredByExtensionCheck(dependency, extensionIssues),
  );
  const blockedDependencyReasons = dependencies
    .filter((dependency) => CANNOT_CREATE.has(dependency.status))
    .map((dependency) => blockedReason(dependency, t));
  const topologyIssues = preflight?.topology_issues ?? [];
  // Graph findings explain a blocked template even when they are not blocking
  // on their own, so they are listed whenever the template is blocked.
  const blockedReasons = [
    ...new Set([
      ...(blockedDependencyReasons.length || resolution.blockingTopologyIssues.length
        ? topologyIssues.map((issue) => issue.message)
        : []),
      ...blockedDependencyReasons,
    ]),
  ];
  const workflows = preflight?.workflows.filter(
    (entry) => (entry as { portability_kind?: string }).portability_kind !== "extension_workflow_ref",
  ) ?? [];
  // A carried child that reuses the workflow already here is not created again.
  const reusedIds = editableDependencies
    .filter(
      (dependency) =>
        dependency.dependency_type === "workflow" &&
        dependency.ref !== rootId &&
        resolutions[dependency.dependency_key]?.action === "use_existing",
    )
    .map((dependency) => dependency.ref);
  const carried = workflows.length || 1 + (item?.includes?.length ?? 0);
  const draftCount = Math.max(1, carried - reusedIds.length);

  // Carried children created under another id, and whether that id is free.
  const renames = editableDependencies
    .filter((dependency) => dependency.dependency_type === "workflow" && dependency.ref !== rootId)
    .map((dependency) => {
      const draft = resolutions[dependency.dependency_key];
      return draft?.action === "rename_workflow" && draft.target.trim()
        ? { dependency, from: dependency.ref, to: draft.target.trim() }
        : null;
    })
    .filter((entry): entry is NonNullable<typeof entry> => entry !== null);
  const targetErrors: Record<string, string> = {};
  for (const rename of renames) {
    const clashes = renames.filter((other) => other.to === rename.to).length > 1 || rename.to === trimmedId;
    if (existing?.has(rename.to)) targetErrors[rename.dependency.dependency_key] = t("react.workflows.value0_already_exists", "{value0} already exists.", { value0: rename.to });
    else if (clashes) targetErrors[rename.dependency.dependency_key] = t("react.workflows.catalog_id_used", "{id} is used by another workflow of this template.", { id: rename.to });
    else if (!WORKFLOW_ID_PATTERN.test(rename.to)) targetErrors[rename.dependency.dependency_key] = t("react.workflows.catalog_rename_id_rule", "Use lowercase letters, digits and underscores, starting with a letter.");
  }
  const takenTargets = renames.filter((rename) => targetErrors[rename.dependency.dependency_key]);

  const basis = basisRef.current;
  const basisRoot = basis && item ? rootWorkflowId(basis, item) : "";
  const chosenResolutions = basis
    ? resolutionPayload(
        basis.dependencies.filter(
          (dependency) =>
            !isRootConflict(dependency, basisRoot) &&
            resolutionComplete(dependency, resolutions[dependency.dependency_key]),
        ),
        resolutions,
      )
    : [];
  const currentIdentity = item
    ? catalogPlanIdentity(item.item_id, projectId, trimmedId, chosenResolutions)
    : "";
  const checkedCurrent = Boolean(
    checkedPlan &&
    checkedRef.current === checkedPlan &&
    checkedPlan.sequence === sequence.current &&
    checkedPlan.identity === currentIdentity &&
    preflight?.can_apply,
  );

  let blocker = "";
  if (phase !== "ready") blocker = "";
  else if (blockedReasons.length) blocker = t("react.workflows.catalog_blocked_here", "This template can't be created here.");
  else if (!idValid) blocker = t("react.workflows.catalog_valid_id_required", "Choose a valid workflow ID.");
  else if (trimmedId !== checkedWorkflowId) blocker = t("react.workflows.catalog_checking_id", "Checking the workflow ID…");
  else if (rootConflict) blocker = t("react.workflows.catalog_free_id_required", "Choose a free workflow ID.");
  else if (takenTargets.length) blocker = t("react.workflows.catalog_free_child_ids", "Choose a free id for {names}.", { names: takenTargets.map((rename) => rename.from).join(", ") });
  else if (!checkedCurrent) {
    const open = decisionRows.length + extensionIssues.filter((entry) => entry.outcome !== "ambiguous").length;
    blocker = open
      ? open === 1
        ? t("react.workflows.catalog_resolve_one", "Resolve {count} item above.", { count: open })
        : t("react.workflows.catalog_resolve_many", "Resolve {count} items above.", { count: open })
      : t("react.workflows.catalog_check_again", "Check again to verify these choices.");
  }

  async function create(): Promise<WorkflowCatalogCreateResult | null> {
    const accepted = checkedRef.current;
    if (!item || blocker || phase !== "ready" || !accepted || !checkedCurrent) return null;
    setCreating(true);
    setCreateError("");
    try {
      const result = await createWorkflowFromCatalogItem(accepted.itemId, {
        projectId: accepted.projectId || undefined,
        targetWorkflowId: accepted.targetWorkflowId || undefined,
        resolutions: accepted.resolutions,
      });
      return checkedRef.current === accepted && accepted.sequence === sequence.current ? result : null;
    } catch (error) {
      if (checkedRef.current !== accepted || accepted.sequence !== sequence.current) return null;
      const message = errorMessage(error, t("react.workflows.the_workflow_was_not_created", "The workflow was not created."));
      // Check again with the same choices, so the rows the server still
      // refuses show as such and can be named here.
      const after = await runCheck(item, { projectId, workflowId: trimmedId });
      const refused = (after?.dependencies ?? [])
        .filter((dependency) => dependency.blocking)
        .map((dependency) => dependency.label || dependency.ref);
      if (accepted.sequence + 1 === sequence.current) setCreateError(refused.length ? t("react.workflows.value0_still_to_resolve_value1", "{value0} Still to resolve: {value1}.", { value0: message, value1: refused.join(", ") }) : message);
      return null;
    } finally {
      setCreating(false);
    }
  }

  return {
    blockedReasons,
    blocker,
    checkAgain: () => {
      if (creating) return;
      ignoreAsyncResult(runCheck(item, { projectId, workflowId: trimmedId && idValid ? trimmedId : "" }));
    },
    renames,
    targetErrors,
    checkError,
    checkedCurrent,
    create,
    createError,
    creating,
    decisionRows,
    existing,
    draftCount,
    extensionIssues,
    idValid,
    options,
    phase,
    preflight,
    projectId,
    resolution: { ...resolution, updateResolution },
    reusedIds,
    rootConflict,
    rootId,
    setProjectId: (value: string) => {
      invalidateCheck();
      setProjectId(value);
      ignoreAsyncResult(runCheck(item, { projectId: value, workflowId: trimmedId && idValid ? trimmedId : "" }));
    },
    setWorkflowId: (value: string) => {
      invalidateCheck();
      setWorkflowId(value);
    },
    /** Begins a fresh set-up for `target`; nothing from an earlier template carries over. */
    start: (target: WorkflowCatalogItem) => {
      invalidateCheck();
      basisRef.current = null;
      setPreflight(null);
      setWorkflowId("");
      setCheckedWorkflowId("");
      setCreateError("");
      setProjectId(initialProjectId);
      resolutionsRef.current = {};
      resetResolutions(null);
      ignoreAsyncResult(runCheck(target, { projectId: initialProjectId, workflowId: "" }));
    },
    suggestedId: rootConflict
      ? freeWorkflowId(rootId || trimmedId, new Set([...(existing?.keys() ?? []), trimmedId, ...renames.map((rename) => rename.to)]))
      : "",
    workflowId,
  };
}

export type CatalogSetup = ReturnType<typeof useCatalogSetup>;
