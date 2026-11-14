"use strict";

// Earlier official runs whose check evidence and impact maps a new run imports
// (ADR 0068). Imported records are candidates only: verify.py reuses one only
// when its fingerprint matches, and a selection base only when its commit is an
// ancestor of this candidate. A miss costs a run, never a false pass.
const NAME = /^(?:quality|quality-stage|tier3-[a-z0-9-]+)-([0-9a-f]{40})-([1-9][0-9]*)$/;
const MAX_RUNS = 3;
const MAX_CANDIDATES = 60;

module.exports = async function selectHostedEvidenceSources({
  github, owner, repo, repositoryId, workflowId, branch, currentRunId,
}) {
  const positive = value => Number.isSafeInteger(value) && value > 0;
  if (owner !== "Flow-Steward" || repo !== "flow-steward-app" ||
      !positive(repositoryId) || !positive(workflowId)) return [];
  const actions = github.rest.actions;
  let artifacts;
  try {
    const response = await actions.listArtifactsForRepo({owner, repo, per_page: 100});
    artifacts = response.data.artifacts;
  } catch (_) {
    return [];
  }
  if (!Array.isArray(artifacts)) return [];
  const rank = artifact => artifact.workflow_run.head_branch === branch ? 0 : 1;
  const candidates = artifacts.filter(artifact => {
    const match = typeof artifact?.name === "string" && artifact.name.match(NAME);
    return match && positive(artifact.id) && artifact.expired === false &&
      positive(artifact.workflow_run?.id) && artifact.workflow_run.id !== Number(currentRunId) &&
      artifact.workflow_run.head_sha === match[1] &&
      [branch, "main"].includes(artifact.workflow_run.head_branch);
  }).sort((left, right) => rank(left) - rank(right) ||
    String(right.created_at || "").localeCompare(String(left.created_at || "")) ||
    right.id - left.id).slice(0, MAX_CANDIDATES);

  const verifiedRuns = new Map();
  const selected = [];
  for (const artifact of candidates) {
    const runId = artifact.workflow_run.id;
    if (!verifiedRuns.has(runId)) {
      if (verifiedRuns.size >= MAX_RUNS) continue;
      verifiedRuns.set(runId, await officialRun({actions, owner, repo, runId, workflowId, repositoryId}));
    }
    const run = verifiedRuns.get(runId);
    if (!run || run.head_sha !== artifact.workflow_run.head_sha) continue;
    selected.push({run_id: runId, artifact_id: artifact.id, name: artifact.name,
      source_sha: run.head_sha, branch: run.head_branch});
  }
  return selected;
};

async function officialRun({actions, owner, repo, runId, workflowId, repositoryId}) {
  try {
    const {data: run} = await actions.getWorkflowRun({owner, repo, run_id: runId});
    const valid = run.id === runId && run.workflow_id === workflowId &&
      run.repository?.id === repositoryId && run.head_repository?.id === repositoryId &&
      ["push", "workflow_dispatch", "schedule"].includes(run.event) &&
      run.status === "completed" && /^[0-9a-f]{40}$/.test(run.head_sha);
    return valid ? run : null;
  } catch (_) {
    // A missing run or a transient API failure only means no import.
    return null;
  }
}
