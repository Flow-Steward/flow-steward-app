import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { type ChangeEvent, type ReactNode, useEffect, useRef, useState } from "react";

import { CheckCircle2, FileText, Upload } from "lucide-react";
import { Link, useSearchParams } from "react-router";
import { useQuery } from "@tanstack/react-query";

import { fetchProjectsList } from "@/pages/projects/list/api";

import {
  DsAlert,
  DsBadge,
  DsMetricCard,
  DsPageHeader,
  PageContainer,
} from "@/components/shared/design-system";
import {
  applyWorkflowImport,
  fetchWorkflowImportOptions,
  preflightWorkflowImport,
  type WorkflowImportDependencyOption,
  type WorkflowImportResolution,
  type WorkflowImportPreflight,
  type WorkflowImportResult,
} from "@/pages/workflows/import/api";
import { DependencyResolutionList } from "@/pages/workflows/import/dependency-resolution";
import { mergeDependencyBasis, useDependencyResolution, type UpdateResolution } from "@/pages/workflows/import/dependency-resolution-state";
import { ignoreAsyncResult } from "@/lib/utils/async";

function renderWhen<T>(value: T | null | undefined | false, render: (value: T) => ReactNode) {
  if (!value) return null;
  return render(value);
}

/** How long the page waits after a changed choice before checking it again. */
const AUTO_PREFLIGHT_DELAY_MS = 600;

function readImportError(error: unknown, t: I18nContextValue["t"] = fallbackContext.t) {
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  return t("react.workflows.workflow_import_failed", "Workflow import failed.");
}

type ImportPlan = {
  bundleYaml: string;
  targetWorkflowId: string;
  projectId: string;
  resolutions: WorkflowImportResolution[];
};

type VerifiedPlan = { identity: string; generation: number; plan: ImportPlan };

function planIdentity(plan: ImportPlan) {
  return JSON.stringify([
    plan.bundleYaml,
    plan.targetWorkflowId.trim(),
    plan.projectId,
    plan.resolutions,
  ]);
}

/** "1 agent, 2 skills" for the definitions an import created, in a fixed kind order. */
function createdDefinitionCounts(created: Array<{ kind: string }>, t: I18nContextValue["t"]) {
  const labels: Record<string, [string, string, string, string]> = {
    agent: ["react.workflows.import_count_agent", "{count} agent", "react.workflows.import_count_agents", "{count} agents"],
    skill: ["react.workflows.import_count_skill", "{count} skill", "react.workflows.import_count_skills", "{count} skills"],
    data_contract: ["react.workflows.import_count_data_contract", "{count} data contract", "react.workflows.import_count_data_contracts", "{count} data contracts"],
    guardrail: ["react.workflows.import_count_guardrail", "{count} guardrail", "react.workflows.import_count_guardrails", "{count} guardrails"],
  };
  return Object.entries(labels)
    .map(([kind, [oneKey, one, manyKey, many]]) => {
      const count = created.filter((item) => item.kind === kind).length;
      if (!count) return "";
      return count === 1 ? t(oneKey, one, { count }) : t(manyKey, many, { count });
    })
    .filter(Boolean)
    .join(", ");
}

export function WorkflowsImportPage() {
  const { t } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const projectId = searchParams.get("project_id")?.trim() || "";
  // Arriving without a project is normal: the catalog carries one only when the
  // operator was already looking at a project. Project connections resolve
  // against it, so offer the choice here rather than importing without one.
  const projectPickerQuery = useQuery({
    queryKey: ["workflows", "import", "project-picker"],
    queryFn: () => fetchProjectsList({ page: 1, perPage: 200, search: "" }),
    enabled: !projectId,
  });
  const projectPickerOptions = projectPickerQuery.data?.projects || [];
  const [step, setStep] = useState(1);
  const [bundleYaml, setBundleYaml] = useState("");
  const [targetWorkflowId, setTargetWorkflowId] = useState("");
  // The first checked dependencies remain editable even when a later server
  // response omits a resolved root row.
  const [sourcePreflight, setSourcePreflight] = useState<WorkflowImportPreflight | null>(null);
  const [preflight, setPreflight] = useState<WorkflowImportPreflight | null>(null);
  const [checkedIdentity, setCheckedIdentity] = useState<string | null>(null);
  const [verified, setVerified] = useState<VerifiedPlan | null>(null);
  const [result, setResult] = useState<WorkflowImportResult | null>(null);
  const [dependencyOptions, setDependencyOptions] = useState<Record<string, WorkflowImportDependencyOption[]>>({});
  const [notice, setNotice] = useState("");
  const [preflighting, setPreflighting] = useState(false);
  const [applying, setApplying] = useState(false);
  const generation = useRef(0);
  const mounted = useRef(true);
  const verifiedRef = useRef<VerifiedPlan | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current += 1;
    };
  }, []);

  const {
    addResolutionDependencies,
    dependencies,
    requiredResolutionsReady,
    resetResolutions,
    resolutionList,
    resolutions,
    updateResolution,
  } = useDependencyResolution(sourcePreflight, projectId);
  const currentPlan: ImportPlan = {
    bundleYaml,
    targetWorkflowId: targetWorkflowId.trim(),
    projectId,
    resolutions: resolutionList,
  };
  const currentIdentity = planIdentity(currentPlan);
  const currentIdentityRef = useRef(currentIdentity);
  currentIdentityRef.current = currentIdentity;
  const currentIsChecked = checkedIdentity === currentIdentity;
  const canApply = Boolean(
    verified &&
    verifiedRef.current === verified &&
    verified.generation === generation.current &&
    verified.identity === currentIdentity &&
    currentIsChecked &&
    preflight?.can_apply &&
    !preflighting &&
    !applying,
  );
  const visiblePreflight = preflight ?? sourcePreflight;
  // A file's project guardrails attach to the chosen project, so the choice is required.
  const needsGuardrailProject = Boolean(
    (visiblePreflight?.dependencies || []).some((dependency) => dependency.dependency_type === "guardrail_project"),
  );
  const topologyIssues = visiblePreflight?.topology_issues ?? [];
  const blockingTopologyIssues = topologyIssues.filter((issue) => issue.blocking);
  const status = preflighting
    ? t("react.workflows.running_preflight", "Running preflight…")
    : canApply
      ? t("react.workflows.display_ready", "Ready")
      : currentIsChecked && visiblePreflight && !visiblePreflight.can_apply
        ? t("react.workflows.display_blocked", "Blocked")
        : requiredResolutionsReady
          ? t("react.workflows.display_needs_check", "Needs check")
          : t("react.workflows.display_needs_resolution", "Needs resolution");

  function invalidate(clearSource = false) {
    generation.current += 1;
    verifiedRef.current = null;
    setVerified(null);
    setCheckedIdentity(null);
    setNotice("");
    setResult(null);
    setPreflighting(false);
    if (clearSource) {
      setSourcePreflight(null);
      setPreflight(null);
      setDependencyOptions({});
      resetResolutions(null);
      setStep(1);
    } else {
      setPreflight(sourcePreflight);
    }
  }

  const onUpdateResolution: UpdateResolution = (dependency, patch) => {
    invalidate();
    updateResolution(dependency, patch);
  };

  async function runPreflight() {
    const plan: ImportPlan = {
      bundleYaml,
      targetWorkflowId: targetWorkflowId.trim(),
      projectId,
      resolutions: resolutionList.map((resolution) => ({ ...resolution })),
    };
    const identity = planIdentity(plan);
    const ticket = ++generation.current;
    verifiedRef.current = null;
    setVerified(null);
    setCheckedIdentity(null);
    setNotice("");
    setResult(null);
    setPreflight(sourcePreflight);
    setPreflighting(true);
    try {
      const [payload, optionsPayload] = await Promise.all([
        preflightWorkflowImport({
          bundleYaml: plan.bundleYaml,
          targetWorkflowId: plan.targetWorkflowId,
          resolutions: plan.resolutions,
          ...(plan.projectId ? { projectId: plan.projectId } : {}),
        }),
        fetchWorkflowImportOptions({ projectId: plan.projectId }),
      ]);
      if (!mounted.current || ticket !== generation.current || identity !== currentIdentityRef.current) return;
      if (sourcePreflight) {
        setSourcePreflight({
          ...sourcePreflight,
          dependencies: mergeDependencyBasis(sourcePreflight.dependencies, payload.dependencies),
        });
        addResolutionDependencies(payload.dependencies);
      } else {
        setSourcePreflight(payload);
        resetResolutions(payload);
      }
      setPreflight(payload);
      setDependencyOptions(optionsPayload.options || {});
      setCheckedIdentity(identity);
      if (payload.can_apply) {
        const accepted = { identity, generation: ticket, plan };
        verifiedRef.current = accepted;
        setVerified(accepted);
      }
      const hasBlockingTopology = (payload.topology_issues || []).some((issue) => issue.blocking);
      setStep(payload.summary.blocking_count > 0 || hasBlockingTopology ? 3 : 4);
    } catch (error) {
      if (!mounted.current || ticket !== generation.current || identity !== currentIdentityRef.current) return;
      setNotice(readImportError(error, t));
    } finally {
      if (mounted.current && ticket === generation.current && identity === currentIdentityRef.current) {
        setPreflighting(false);
      }
    }
  }

  // After the first preflight, a changed choice is checked again on its own, so the
  // Import button follows the operator's choices without another click. Each choice
  // is checked once: a failed check waits for the operator instead of retrying.
  const runPreflightRef = useRef(runPreflight);
  runPreflightRef.current = runPreflight;
  const autoCheckedIdentity = useRef<string | null>(null);
  useEffect(() => {
    if (!sourcePreflight || currentIsChecked || preflighting || applying || !requiredResolutionsReady) return;
    if (autoCheckedIdentity.current === currentIdentity) return;
    const timer = globalThis.setTimeout(() => {
      autoCheckedIdentity.current = currentIdentity;
      ignoreAsyncResult(runPreflightRef.current());
    }, AUTO_PREFLIGHT_DELAY_MS);
    return () => globalThis.clearTimeout(timer);
  }, [sourcePreflight, currentIdentity, currentIsChecked, preflighting, applying, requiredResolutionsReady]);

  async function runApply() {
    const accepted = verifiedRef.current;
    if (
      accepted?.generation !== generation.current ||
      accepted.identity !== currentIdentityRef.current ||
      !preflight?.can_apply ||
      preflighting ||
      applying
    ) return;
    setNotice("");
    setApplying(true);
    try {
      const payload = await applyWorkflowImport(accepted.plan);
      if (mounted.current && accepted.generation === generation.current) {
        setResult(payload);
        setStep(5);
      }
    } catch (error) {
      if (mounted.current && accepted.generation === generation.current) {
        setNotice(readImportError(error, t));
      }
    } finally {
      if (mounted.current) setApplying(false);
    }
  }

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    invalidate(true);
    const ticket = generation.current;
    const yaml = await file.text();
    if (mounted.current && ticket === generation.current) setBundleYaml(yaml);
  }

  return (
    <PageContainer width="standard">
    <section className="fs-page-sections">
      <DsPageHeader
        description={t("react.workflows.preflight_map_dependencies_and_create_disabled_drafts", "Preflight, map dependencies, and create disabled drafts.")}
        eyebrow={t("shell.nav.workflows", "Workflows")}
        title={t("react.workflows.import_workflow_yaml", "Import workflow YAML")}
      />

      {/* The progress chips report which step is current; the badge carries the
          state and nothing here mutates. */}
      <div className="flex flex-wrap gap-2">
        {["YAML", t("react.workflows.import_preflight", "Preflight"), t("react.workflows.import_resolve", "Resolve"), t("react.workflows.review", "Review"), t("jobs.detail.inspector.result", "Result")].map((label, index) => (
          <DsBadge key={label} tone={step === index + 1 ? "green" : "neutral"}>
            {index + 1}. {label}
          </DsBadge>
        ))}
      </div>

      {renderWhen(notice, (visibleNotice) => (
        <DsAlert title={t("react.workflows.import_could_not_continue", "Import could not continue")} tone="danger">
          {visibleNotice}
        </DsAlert>
      ))}

      {renderWhen(!projectId, () => (
        <article className="fs-card">
          <header className="fs-card-header">
            <h2 className="fs-card-title">{t("trash.type.project", "Project")}</h2>
          </header>
          <div className="fs-card-body">
            <p className="fs-muted">
              {t("react.workflows.choose_the_project_this_workflow_belongs_to_its_connections_are_what_project_dependencies_resolve_against", "Choose the project this workflow belongs to. Its connections are what project dependencies resolve against.")}</p>
            {renderWhen(needsGuardrailProject, () => (
              <DsAlert title={t("react.workflows.guardrails_need_a_project", "This file has guardrails for a project. Choose the project to attach them to.")} tone="warning" />
            ))}
            <select
              aria-label={t("trash.type.project", "Project")}
              className="fs-input"
              onChange={(event) => {
                const chosen = event.target.value.trim();
                const next = new URLSearchParams(searchParams);
                if (chosen) {
                  next.set("project_id", chosen);
                } else {
                  next.delete("project_id");
                }
                invalidate();
                setSearchParams(next);
              }}
              value=""
            >
              <option value="">{t("react.workflows.select_a_project", "Select a project…")}</option>
              {projectPickerOptions.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </div>
        </article>
      ))}

      <article className="fs-card">
        <header className="fs-card-header">
          <h2 className="fs-card-title">{t("react.workflows.1_paste_or_upload_yaml", "1. Paste or upload YAML")}</h2>
        </header>
        <div className="fs-card-body">
          {/* A YAML bundle is code, so it takes the code face — `.fs-table-mono`
              would put it on DM Sans, which is proportional. */}
          <textarea
            aria-label={t("react.workflows.workflow_bundle_yaml", "Workflow bundle YAML")}
            className="fs-textarea fs-code-value min-h-72"
            onChange={(event) => {
              invalidate(true);
              setBundleYaml(event.target.value);
            }}
            value={bundleYaml}
          />
          <div className="flex flex-wrap items-center gap-3">
            <label className="fs-button cursor-pointer">
              <Upload aria-hidden="true" />
              <span>{t("react.workflows.upload_yaml", "Upload YAML")}</span>
              <input
                accept=".yaml,.yml,text/yaml,application/x-yaml"
                className="sr-only"
                onChange={(event) => ignoreAsyncResult(handleFileChange(event))}
                type="file"
              />
            </label>
            <div className="fs-field min-w-60 flex-1">
              <label className="fs-field-label" htmlFor="import_target_workflow_id">
                {t("react.workflows.target_workflow_id", "Target workflow ID")}</label>
              <input
                className="fs-input"
                id="import_target_workflow_id"
                onChange={(event) => {
                  invalidate();
                  setTargetWorkflowId(event.target.value);
                }}
                placeholder={t("react.workflows.optional_rename", "Optional rename")}
                type="text"
                value={targetWorkflowId}
              />
            </div>
            <button
              className="fs-button primary"
              disabled={!bundleYaml.trim() || preflighting || applying}
              onClick={() => ignoreAsyncResult(runPreflight())}
              type="button"
            >
              <FileText aria-hidden="true" />
              {preflighting ? t("react.workflows.running_preflight", "Running preflight…") : t("react.workflows.run_preflight", "Run preflight")}
            </button>
          </div>
        </div>
      </article>

      {renderWhen(visiblePreflight, (visiblePreflight) => (
        <article className="fs-card">
          <header className="fs-card-header">
            <h2 className="fs-card-title">{t("react.workflows.2_preflight_summary", "2. Preflight summary")}</h2>
          </header>
          <div className="fs-card-body">
            {!currentIsChecked ? <p className="fs-muted">{t("react.workflows.original_preflight_snapshot", "Original preflight snapshot. Your choices are checked again automatically; you can also press Run preflight.")}</p> : null}
            <div className="fs-metric-grid" data-columns="4">
              <DsMetricCard
                density="compact"
                figure={String(visiblePreflight.summary.workflow_count)}
                label={t("react.workflows.workflows", "workflows")}
                wave={false}
              />
              <DsMetricCard
                density="compact"
                figure={String(visiblePreflight.summary.dependency_count)}
                label={t("react.workflows.dependencies", "dependencies")}
                wave={false}
              />
              <DsMetricCard
                attention={visiblePreflight.summary.blocking_count > 0}
                density="compact"
                figure={String(visiblePreflight.summary.blocking_count)}
                label={t("react.workflows.dependency_blockers", "Dependency blockers")}
                wave={false}
              />
              <DsMetricCard
                attention={status !== t("react.workflows.display_ready", "Ready")}
                density="compact"
                figure={status}
                label={t("react.workflows.display_status", "status")}
                wave={false}
              />
            </div>
            <p className="fs-muted">{t("react.workflows.structure_blockers", "Structure blockers")}: {blockingTopologyIssues.length}</p>
          </div>
        </article>
      ))}

      {renderWhen(topologyIssues.length, () => (
        <article className="fs-card">
          <header className="fs-card-header">
            <h2 className="fs-card-title">{t("react.workflows.workflow_structure", "Workflow structure")}</h2>
          </header>
          <div className="fs-card-body">
            {/* A structural finding is a property of the graph in the bundle, so
                no dependency resolution on this page can clear it. Blocking ones
                stop the import; the rest are reported because a draft that does
                not yet reach a terminal step is a normal thing to import and
                finish in the editor. */}
            <p className="fs-muted">
              {blockingTopologyIssues.length
                ? t("react.workflows.this_bundle_cannot_be_imported_until_its_structure_is_fixed_at_the_source_resolving_dependencies_below_will_no", "This bundle cannot be imported until its structure is fixed at the source. Resolving dependencies below will not clear these.")
                : t("react.workflows.these_are_reported_for_review_the_bundle_imports_as_a_draft_and_can_be_finished_in_the_editor", "These are reported for review. The bundle imports as a draft and can be finished in the editor.")}
            </p>
            <ul className="grid gap-2">
              {topologyIssues.map((issue, index) => (
                <li key={`${issue.workflow_id || ""}:${issue.code}:${issue.path || index}`}>
                  <DsBadge tone={issue.blocking ? "danger" : "orange"}>
                    {issue.blocking ? t("project_edit.options.blocking", "blocking") : t("react.workflows.review_variant_c97ace4c", "review")}
                  </DsBadge>{" "}
                  <code>{issue.code}</code> {issue.message}
                  {issue.path ? (
                    <>
                      {" \u2014 "}
                      <code>{issue.path}</code>
                    </>
                  ) : null}
                  {issue.workflow_id ? (
                    <>
                      {t("react.workflows.in", "in")}
                      <code>{issue.workflow_id}</code>
                    </>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        </article>
      ))}

      {renderWhen(sourcePreflight, () => (
        <article className="fs-card">
          <header className="fs-card-header">
            <h2 className="fs-card-title">{t("react.workflows.3_dependency_resolution", "3. Dependency resolution")}</h2>
          </header>
          {/* Not a `.fs-table`: these rows are editable, carry two to four
              controls each, wrap helper text, and put the widest column last.
              `.fs-table td` sets `white-space: nowrap` and its last cell is a
              48px sticky action cell — the published table contract is the wrong
              surface for a form. One inset row per dependency instead. */}
          <div className="fs-card-body">
            <DependencyResolutionList
              dependencies={dependencies}
              dependencyOptions={dependencyOptions}
              effectiveDependencies={currentIsChecked ? visiblePreflight?.dependencies : undefined}
              accepted={currentIsChecked && Boolean(visiblePreflight?.can_apply)}
              resolutions={resolutions}
              selectedProjectId={projectId}
              updateResolution={onUpdateResolution}
            />
          </div>
        </article>
      ))}

      {renderWhen(sourcePreflight, () => (
        <article className="fs-card">
          <header className="fs-card-header">
            <h2 className="fs-card-title">{t("react.workflows.4_final_review", "4. Final review")}</h2>
            <p className="fs-card-description">
              {t("react.workflows.imported_workflows_are_saved_disabled_with_draft_portability_state", "Imported workflows are saved disabled with draft portability state.")}</p>
          </header>
          {/* `.fs-card-body` is a grid declared after Tailwind's utilities, so the
              row is its child: a `flex` on the body itself loses the cascade and
              the hint drops onto its own full-width line. */}
          <div className="fs-card-body">
            <div className="flex flex-wrap items-center gap-3">
              <button
                className="fs-button primary"
                disabled={!canApply}
                onClick={() => ignoreAsyncResult(runApply())}
                type="button"
              >
                <CheckCircle2 aria-hidden="true" />
                {applying ? t("react.workflows.importing", "Importing…") : t("react.workflows.import_workflow", "Import workflow")}
              </button>
              {!requiredResolutionsReady && !canApply ? (
                <span className="fs-field-helper error">
                  {t("react.workflows.select_required_dependency_resolutions_first", "Select required dependency resolutions first.")}</span>
              ) : null}
            </div>
          </div>
        </article>
      ))}

      {renderWhen(result, (visibleResult) => (
        <article className="fs-card fs-card-success">
          <header className="fs-card-header">
            <h2 className="fs-card-title">{t("react.workflows.5_import_result", "5. Import result")}</h2>
          </header>
          <div className="fs-card-body">
            <p>
              {t("react.workflows.imported", "Imported")}{" "}{visibleResult.imported_workflows.length} {" "}{t("react.workflows.workflow_variant_da7f739f", "workflow")}{visibleResult.imported_workflows.length === 1 ? "" : "s"}.
            </p>
            {renderWhen(visibleResult.definitions?.created.length ? visibleResult.definitions.created : null, (created) => (
              <p>{t("react.workflows.import_created_from_file", "Created from the file: {items}.", { items: createdDefinitionCounts(created, t) })}</p>
            ))}
            {renderWhen(visibleResult.definitions?.agents_left_disabled.length ? visibleResult.definitions.agents_left_disabled : null, (agents) => (
              <DsAlert title={t("react.workflows.import_agents_left_disabled", "Some agents were left disabled")} tone="warning">
                <ul>
                  {agents.map((agent) => <li key={agent.agent_id}>{agent.agent_id}: {agent.reason}</li>)}
                </ul>
              </DsAlert>
            ))}
            <div>
              <Link
                className="fs-button"
                to={`/registry/workflows/${encodeURIComponent(visibleResult.workflow_id)}/builder`}
              >
                {t("react.workflows.open_imported_workflow", "Open imported workflow")}</Link>
            </div>
          </div>
        </article>
      ))}
    </section>
    </PageContainer>
  );
}
