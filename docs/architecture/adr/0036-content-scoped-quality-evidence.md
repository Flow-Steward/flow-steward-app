# ADR 0036: Content-Scoped Quality Evidence and Shared Gate Caches

## Status

Accepted — 2026-10-14. Amends the exact-SHA reuse rule of ADR 0021.

## Context

ADR 0021 bound every evidence record to the commit that produced it. The
fingerprint already hashed each check's inputs, but the commit binding threw
that away: a commit that touched one frontend file reran mypy, Semgrep and
bandit over the whole Python tree. Every worktree also kept its own evidence,
npm cache and tool caches, and the practice of gating each commit from a fresh
worktree (Docker Desktop serves stale bind-mounted files) made every gate cold.
Tier 2 ran its fifteen checks one after another on a ten-core machine.
Measured in October 2026 (median of retained evidence): Tier 2 took about 13
minutes, one check at a time, and any new commit restarted it from scratch.

## Decision

1. Every registry check has a `reuse_scope`. `commit` (the default) keeps the
   ADR 0021 rule. `inputs` lets a passed result count for any commit whose
   fingerprint matches. A check may be `inputs`-scoped only when its `inputs`
   name every file its tool reads; the audit image inputs (`Dockerfile`, audit,
   Semgrep and CLI requirements) join every fingerprint so a tool upgrade
   invalidates the result. Static Tier 2 checks are `inputs`-scoped; checks
   whose inputs are not provably complete (for example `env-artifacts`,
   `ci-excludes`) and
   every Tier 3 suite stay `commit`-scoped.
2. The release gate still validates every check against the final SHA: a
   commit-scoped record must carry that SHA, an input-scoped record must match
   the fingerprint recomputed from that SHA's tree.
3. `fs-audit` mounts one named volume, `flow-steward-quality-cache`, for every
   checkout and compose project. Evidence writes go to the worktree and to the
   volume; a worktree miss is served from the volume and copied back, so the
   release gate keeps reading worktree evidence. npm keeps its package cache
   there. Per-file scanners record which file contents passed under which tool
   version and configuration and rescan only the rest.
4. Tier 2 starts its checks in registry order and runs up to four at once. After
   a failure nothing new starts; running checks finish and keep their evidence.
   Tier 3 stays serial: backend tests read the frontend shell that the frontend
   suite publishes.

## Consequences

- A change pays only for the checks whose inputs it touched, in any worktree.
- An incomplete `inputs` list on an `inputs`-scoped check is now a correctness
  defect, not a missed optimisation; adding a file a tool reads means adding it
  to the check's inputs.
- Removing the cache volume is always safe and only makes the next run cold.
- Tier 3 suites keep the per-commit cost until their real file dependencies are
  known; that is the next step, not part of this decision.
