"use strict";

// Only a completed public-source run of this exact commit, with both
// cumulative gate steps successful, can replace a repeated release gate.
module.exports = function selectVerifiedQuality(run, jobs, sha, repositoryId, workflowId) {
  if (!run || run.head_sha !== sha || run.workflow_id !== workflowId ||
      !["push", "workflow_dispatch"].includes(run.event) ||
      run.status !== "completed" || run.conclusion !== "success" ||
      run.repository?.id !== repositoryId || run.head_repository?.id !== repositoryId ||
      !Array.isArray(jobs)) return false;
  const matches = jobs.filter((job) => job.name === "quality");
  if (matches.length !== 1) return false;
  const job = matches[0];
  if (job.status !== "completed" || job.conclusion !== "success" || !Array.isArray(job.steps)) return false;
  return [
    "Require a clean exact-SHA candidate including current main",
    "Run canonical Tier 2",
    "Recheck current main before starting Tier 3",
    "Run cumulative gate with Tier 3",
    "Reject results if main advanced beyond the candidate",
  ].every((name) => {
    const steps = job.steps.filter((step) => step.name === name);
    return steps.length === 1 && steps[0].status === "completed" && steps[0].conclusion === "success";
  });
};
