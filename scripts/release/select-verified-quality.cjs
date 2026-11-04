"use strict";

// Only a completed public-source run of this exact commit with a successful
// explicit cumulative gate and exact-candidate boundaries is full acceptance.
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
  const succeeded = (name) => {
    const steps = job.steps.filter((step) => step.name === name);
    return steps.length === 1 && steps[0].status === "completed" && steps[0].conclusion === "success";
  };
  if (![
    "Require a clean exact-SHA candidate including current main",
    "Reject results if main advanced beyond the candidate",
  ].every(succeeded)) return false;
  const legacyFull = [
    "Run canonical Tier 2",
    "Recheck current main before starting Tier 3",
    "Run cumulative gate with Tier 3",
  ].every(succeeded);
  const explicitFull = [
    "Recheck current main before an explicitly requested full gate",
    "Run explicitly requested cumulative full gate",
  ].every(succeeded);
  return legacyFull || explicitFull;
};
