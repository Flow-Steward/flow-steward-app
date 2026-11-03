"use strict";

// Scanner markers are optional input proofs, never whole-gate pass evidence.
// Bound both discovery and validation: a cache miss costs a scan, not more API pages.
module.exports = async function selectHostedCacheSource({
  github, owner, repo, repositoryId, workflowId, branch, currentRunId, requestedRunId = "",
}) {
  const positive = value => Number.isSafeInteger(value) && value > 0;
  const explicit = requestedRunId !== "";
  if (explicit && (typeof requestedRunId !== "string" ||
      !/^[1-9][0-9]*$/.test(requestedRunId) || !positive(Number(requestedRunId)))) {
    throw new Error("Explicit scanner source requires a positive safe decimal run ID string");
  }
  if (owner !== "Flow-Steward" || repo !== "flow-steward-app" ||
      !positive(repositoryId) || !positive(workflowId)) {
    if (explicit) throw new Error("Explicit scanner source requires official repository identity");
    return null;
  }
  const actions = github.rest.actions;
  if (explicit) {
    // An explicitly requested experiment must not silently become a cold scan.
    // Keep this path outside automatic discovery's cache-miss error handling.
    const runId = Number(requestedRunId);
    if (runId === Number(currentRunId)) throw new Error("Scanner source cannot be the current run");
    const {data: run} = await actions.getWorkflowRun({owner, repo, run_id: runId});
    if (run.id !== runId || run.workflow_id !== workflowId ||
        run.repository?.id !== repositoryId || run.head_repository?.id !== repositoryId ||
        !["push", "workflow_dispatch"].includes(run.event) ||
        run.status !== "completed" || !["success", "failure"].includes(run.conclusion) ||
        typeof run.head_sha !== "string" || !/^[0-9a-f]{40}$/.test(run.head_sha) ||
        typeof run.head_branch !== "string" || !positive(run.run_attempt)) {
      throw new Error("Explicit scanner source is not a completed official quality run");
    }
    const {data: inventory} = await actions.listWorkflowRunArtifacts({
      owner, repo, run_id: runId, per_page: 100,
    });
    if (!Array.isArray(inventory.artifacts) ||
        !Number.isSafeInteger(inventory.total_count) ||
        inventory.total_count !== inventory.artifacts.length) {
      throw new Error("Explicit scanner source artifact inventory is incomplete");
    }
    const artifactName = `secret-scan-files-${run.head_sha}-${run.run_attempt}`;
    const matching = inventory.artifacts.filter(artifact => artifact?.name === artifactName);
    if (matching.length !== 1 || !positive(matching[0].id) || matching[0].expired !== false ||
        matching[0].workflow_run?.id !== runId ||
        matching[0].workflow_run?.head_sha !== run.head_sha ||
        matching[0].workflow_run?.head_branch !== run.head_branch) {
      throw new Error("Explicit scanner source has no unique retained exact-SHA/attempt scanner artifact");
    }
    return {run_id: runId, artifact_id: matching[0].id,
      artifact_name: artifactName, source_sha: run.head_sha};
  }
  let artifacts;
  try {
    const response = await actions.listArtifactsForRepo({owner, repo, per_page: 100});
    artifacts = response.data.artifacts;
  } catch (_) {
    return null;
  }
  if (!Array.isArray(artifacts)) return null;
  const pattern = /^secret-scan-files-([0-9a-f]{40})-([1-9][0-9]*)$/;
  const rank = artifact => artifact.workflow_run.head_branch === branch ? 0
    : artifact.workflow_run.head_branch === "main" ? 1 : 2;
  const candidates = artifacts.filter(artifact => {
    if (!artifact || typeof artifact !== "object") return false;
    const match = typeof artifact.name === "string" && artifact.name.match(pattern);
    return match && positive(Number(match[2])) && positive(artifact.id) &&
      artifact.expired === false && positive(artifact.workflow_run?.id) &&
      artifact.workflow_run.id !== Number(currentRunId) &&
      artifact.workflow_run.head_sha === match[1] &&
      typeof artifact.workflow_run.head_branch === "string";
  }).sort((left, right) => rank(left) - rank(right) ||
    String(right.created_at || "").localeCompare(String(left.created_at || "")) ||
    right.id - left.id).slice(0, 20);

  for (const artifact of candidates) {
    try {
      const runId = artifact.workflow_run.id;
      const {data: run} = await actions.getWorkflowRun({owner, repo, run_id: runId});
      if (run.id !== runId || run.workflow_id !== workflowId ||
          run.repository?.id !== repositoryId || run.head_repository?.id !== repositoryId ||
          !["push", "workflow_dispatch"].includes(run.event) ||
          run.status !== "completed" || !["success", "failure"].includes(run.conclusion) ||
          !/^[0-9a-f]{40}$/.test(run.head_sha) || !positive(run.run_attempt) ||
          run.head_sha !== artifact.workflow_run.head_sha ||
          run.head_branch !== artifact.workflow_run.head_branch ||
          artifact.name !== `secret-scan-files-${run.head_sha}-${run.run_attempt}`) continue;
      const {data: inventory} = await actions.listWorkflowRunArtifacts({
        owner, repo, run_id: runId, per_page: 100,
      });
      // An incomplete inventory cannot prove uniqueness.
      if (!Array.isArray(inventory.artifacts) ||
          !Number.isSafeInteger(inventory.total_count) ||
          inventory.total_count !== inventory.artifacts.length) continue;
      const matching = inventory.artifacts.filter(item => item?.name === artifact.name);
      if (matching.length !== 1 || matching[0].id !== artifact.id ||
          matching[0].expired !== false) continue;
      return {run_id: runId, artifact_id: artifact.id,
        artifact_name: artifact.name, source_sha: run.head_sha};
    } catch (_) {
      // Missing runs, transient API failures or malformed responses never prove a pass.
    }
  }
  return null;
};
