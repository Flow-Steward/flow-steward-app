# Agent Workflow Standard

This document defines how AI agents and automation contributors must work inside the Flow Steward repository.

## Purpose

The goal of this standard is to keep agent-driven work predictable, safe,
and aligned with repository architecture and quality expectations.

## Scope

This standard applies to:

- AI coding agents
- orchestration agents
- review and QA agents
- automation flows that modify code, docs, or configuration

## Required Context Gathering

- `MUST` gather repository context before changing code.
- `MUST` search the repository for the existing code before editing unfamiliar areas.
- `MUST` inspect direct callers, imports, tests, configuration owners, and generated artifacts before refactors, deletions, renames, or structural changes.
- `SHOULD` read the canonical standards before making changes in a new subsystem.

## Library and Framework Usage

- `MUST` consult current authoritative documentation before changing version-sensitive
  external library or framework usage.
- `SHOULD` avoid relying on memory alone for unstable or version-sensitive APIs.

## Change Planning

- `MUST` keep the planned change scoped to the task.
- `MUST` identify whether the change is local, cross-module, or architectural before editing.
- `SHOULD` prefer the smallest safe change that solves the problem.
- `SHOULD` separate cleanup from behavior changes unless they are tightly coupled.

## Risk and Approval

- `MUST` pause for explicit approval before destructive operations.
- `MUST` pause for explicit approval before high-impact architectural changes unless the task explicitly authorizes them.
- `MUST` pause for explicit approval before operations that can delete data, rewrite history, or broadly affect unrelated files.
- Task-authorized creation of disposable agent resources includes their bounded lifecycle
  cleanup under "Owned Runtime Resource Cleanup" below. That cleanup does not require
  another confirmation; deleting user data, unknown resources, or another owner's work does.
- `SHOULD` surface assumptions when a decision has non-obvious consequences.
- `SHOULD` escalate when confidence is low and the blast radius is high.

## Dirty Worktree Discipline

- `MUST` preserve unrelated existing changes.
- `MUST` avoid reverting, overwriting, or cleaning user changes that are outside the task.
- `MUST` work with existing changes when they do not block the task.
- `SHOULD` stop and ask for direction when unrelated in-progress edits directly conflict with the current task.

## Implementation Rules

- `MUST` follow the canonical standards in `docs/standards/*`.
- `MUST` place new first-party code under the canonical repository layout defined in `architecture-standard.md`.
- `MUST` keep business logic out of transport handlers.
- `MUST` avoid introducing retired or superseded naming.
- `MUST` avoid creating manual rule duplication outside the canonical standards.
- `MUST` treat folders outside the canonical layout as temporary transition surfaces.
- `MUST` format supported source files changed by the agent immediately after each coherent edit batch and before Tier 1 tests, following `engineering-standard.md`.
- `MUST` pass explicit owned file paths to edit-loop formatters and inspect the resulting diff.
- `MUST NOT` use repository-wide formatting as the default for a scoped change or modify unrelated dirty-worktree files.
- `SHOULD` move touched code toward the canonical layout when the task includes structural work.
- `SHOULD` add or update docs when the change affects workflow, contracts, or architecture.
- `SHOULD` use ADRs for architectural exceptions or new architectural patterns.

## Verification

- `MUST` follow the three Docker verification tiers and diff/dependency selection defined in `testing-standard.md`; a push, handoff, merge or publication does not request a complete tier.
- `MUST` identify whether reported evidence is Tier 1, 2, or 3, or an additional audit check.

AC17-R97: Every tier runs inside the disposable `fs-audit` container, through `lint.sh --checks …`. Tier 1 is no exception: being focused does not mean running on the host. A host interpreter makes the result depend on the machine — Tier 1 could not run at all where `python3` is 3.14, and a host run of a suite passed while the same suite failed in the gate, because the gate substitutes the tree through `run-core-only-tests.sh`. `infra/tooling/quality/check-registry.json` is the single list of commands, and `tests/unit/tooling/quality/test_check_registry.py` fails if any check escapes the container.
- `MUST` add a regression test for every bug fix and observe the expected failure before applying the fix.
- `MUST` select targeted tests from the diff, owning tests, direct consumers and change-type requirements. Use `lint.sh --checks targeted-tests --test-paths "auto:<accepted-sha> concrete-file-or-node"` and add known dynamic, database and extension consumers. Frontend tests use reviewed `src/` file lists or a separate `auto:<ref>` invocation; select affected Tier 2 checks by registry ID.
- `MUST` use fail-fast execution during sequential iteration and diagnose a failure before retrying it.
- On a shared Docker host, agents `MUST NOT` overlap independent quality-gate invocations across checkouts, worktrees, Compose projects or tiers, including targeted checks, static/security lint, frontend builds, `--fast`, `--full` and Sonar. Internal concurrency belongs to the canonical scheduler of the single active invocation.
- Before starting a gate, the coordinating agent `MUST` confirm that the previous invocation and its owned child processes have finished. An active or unknown run requires waiting. Record the active owner, checkout, candidate SHA and invocation identity; treat OOM as an infrastructure failure and apply scoped retry rules after the active invocation ends.
- `MUST` keep focused cycles narrow and fail-fast. An explicitly requested complete Tier 3 backend suite runs without `-x` or `--maxfail=1` and retains the complete failure list, grouped by root cause, for one coherent fix batch.
- After failure, timeout or interruption, `MUST` retain the log and candidate SHA and verify only failed/timed-out targets plus new impact against that SHA. Follow `testing-standard.md`; do not restart a complete tier or hosted quality job. Unknown and unrun coverage stays pending.
- `MUST` verify a pushed change's affected behavior and report its selected checks; a push does not authorize a complete remote `--fast` gate.
- `MUST` obtain a separate explicit request before any full, heavy, nightly or Sonar run. Handoff, merge, publication and substantial core edits do not authorize those runs; report unrun scopes accurately.
- `MUST` treat quality evidence as valid only for its recorded fingerprint and, for commit-scoped checks, its exact commit SHA; input-scoped checks are valid for any commit with the same fingerprint (`testing-standard.md`, ADR 0036). Inspect remote run provenance; do not claim a public run checked a different private SHA.
- `MUST` resolve the accepted source SHA and target branch before verification, recheck before merge, and verify new impact when the candidate changes. A changed SHA does not request another complete gate.
- `MUST` let each internal tier verifier execute only its registry-owned delta. When explicitly requested, `--full` validates or refreshes exact-SHA Tier 2 before Tier 3 and validates cumulative evidence; a targeted pass does not establish a full pass.
- `MUST` preserve actual check-level pass/fail evidence and its fingerprint; deferred no-op checks are not valid evidence. Existing `--checks` selections do not promise per-file execution or per-target recovery from a full timeout.
- `MUST` report the final release SHA, selected checks, achieved depth and unrun work. Publication does not implicitly request or require a new full gate; an explicitly requested full acceptance contract still requires complete matching evidence.
- Additional deep audit checks `MAY` be requested through `lint.sh --checks`; `--audit` is a compatibility alias for the final committed gate.
- GitHub quality execution `MUST` require a separate manual request; push, tag and
  publication events do not authorize a quality run. Official publication `MUST`
  preserve verification of the complete canonical image-role/platform set and source
  provenance in at least one configured registry. Unavailable mirrors remain visible
  in evidence; invalid identities and digest conflicts fail closed.
- `MUST` report exact commands, achieved tier, candidate provenance, and blocked or unrun verification.
- `MUST NOT` combine results from different commits or environments into one green result, except input-scoped evidence whose fingerprint matches the final tree.

## Owned Runtime Resource Cleanup

Cleanup is part of task completion, not optional follow-up work. This section applies to
resources created manually as well as resources created by scripts. A passing quality gate
does not prove that host directories, Docker resources, or caches were removed.

### Executable Lifecycle

Use `python -m core.entrypoints.cli.agent_resources --registry <primary-checkout>/logs/resource-ownership/v1
--owner <task-id> run -- <command>` for managed command execution (join the command onto one
shell line). It creates task-private scratch/cache directories and reconciles labelled Docker
resources on exit. See the [operator runbook](../operations/agent-resource-cleanup.md) for
registration, retention, supported resources, and explicit recovery. Legacy ad hoc JSON
journals are evidence only; they are not implicitly imported into the executable schema.
Before final handoff, agents `MUST` invoke `cleanup --apply` for their versioned owner journal
and inspect the persisted receipt, even if a managed command already performed cleanup.
Release only finished owned entries first; never clear live leases or another consumer to
make the receipt green. A written promise to clean later is not completion evidence.
The collector blocks Git checkouts/worktrees and ambiguous resources rather than claiming
that it has preserved their unique contents. Native worktree archival remains a separate step.

### Ownership Before Creation

- `MUST` maintain a resource ledger in the retained primary checkout at
  `logs/resource-ownership/v1/<task-id>.json`, outside disposable source/gate copies. The task ID
  identifies the owning chat or execution; parallel tasks use separate ledgers.
- `MUST` record the intended resource before creation and fill in its actual identity after
  creation: owner, purpose, creation time, lifecycle state, cleanup trigger, and exact path or
  native ID. States are `active`, `retained`, `released`, and `removed`.
- `MUST` cover containers, Compose projects, networks, volumes, image tags and IDs, builders,
  source/worktree/gate copies, temporary exports and database dumps, generated dependencies,
  `.flow-steward` build/quality contexts, tool caches, and task-private BuildKit cache records.
  Record the source commit and repository for source copies.
- `MUST` use an isolated task-specific Compose project and ownership labels where supported;
  record exact identities for resources without label support. Inventory pre-existing
  resources before a run so reused infrastructure cannot be mistaken for newly owned output.
- `MUST` record every shared resource as borrowed, with its owner and current consumers.
  Reusing a database, image, cache, certificate, or source path does not transfer ownership.
- `MUST NOT` store credentials or raw secret-bearing environment/inspection output in the
  ledger or cleanup receipt. Record references and sanitized metadata only.

### Cleanup Triggers and Handoffs

- `MUST` include evidence preservation, resource cleanup, and absence verification in the task
  plan and completion checklist. An orchestrator tracks its child agents' ledgers and checks
  their cleanup receipts; delegation does not make disposable resources ownerless.
- `MUST` remove disposable resources after each run, regardless of pass, failure, rejection,
  or cancellation, and before creating replacement resources. Wrappers that create resources
  `MUST` register cleanup on exit, error, and interruption and preserve the original result
  separately from the cleanup result.
- `MUST` reconcile the ledger on task resume and before a final response, PR handoff, or
  release handoff. An interrupted agent cleans its recorded residue on resume; it must not
  assume an exit handler ran. Merge/release ends the need for obsolete task stands and copies
  unless an active consumer is explicitly recorded.
- `MUST` retain only resources needed for current execution, a requested live review, or a
  documented shared dependency. Each retained entry needs a named responsible owner, current
  consumer, concrete reason, release event, and next review time. "For later", a failed test,
  or "already accepted" is not a retention reason by itself.
- `MUST` keep responsibility until a receiving agent acknowledges the exact retained resources
  and their cleanup trigger. On the consumer's release, the owner removes them and updates
  the ledger. A due review time triggers reassessment, not permission to delete foreign work.
- `MUST` preserve the minimal required evidence outside disposable copies before removal:
  receipts, relevant failure logs, checksums, selected screenshots, and required backups.
  Preserve unique tracked/untracked work as verified patches/archives and keep committed work
  reachable in a retained repository or verified Git bundle. Generated dependencies, complete
  checkout copies, image exports, and tool caches are not evidence to retain by default.

### Safe Removal and Cache Boundaries

- `MUST` revalidate ownership and consumers immediately before deletion using the ledger,
  native identities, and ownership labels where supported. Select exact targets; name globs,
  project-prefix guesses, global prune commands, age, and presence of a commit in `main` are
  not ownership proof. Unclear ownership blocks that target, not the rest of the cleanup.
- `MUST NOT` stop another task's checks or remove user/project databases, knowledge-base data,
  unrelated Compose projects, unrelated images, shared SonarQube data, or
  `flow-steward-quality-cache` during automatic task cleanup. A task-created volume is
  disposable only if its data is disposable; user-entered data needs separate preservation
  or explicit deletion authorization.
- `MUST` check mounts, TLS/config dependencies, symlinks, worktree registrations, and Git
  alternates before deleting a source directory. Preserve unique work and keep surviving
  checkouts' object stores reachable; a clean `git status` alone is insufficient.
- `MUST` remove task-private host build/tool caches, generated dependencies, contexts, dumps,
  and obsolete source/gate copies when their run or retained consumer releases them. Do not
  copy shared caches into each candidate or retain them as part of an evidence archive.
- `MUST` inventory BuildKit cache before/after task builds and record attributable cache IDs
  and dependency relationships. A new ID in a shared builder is not proof of ownership when
  other agents are building. Prune only proven task-private unused records, including their
  disposable descendants; preserve shared ancestors, in-use records, and foreign consumers.
  If provenance is ambiguous, retain and report the exact records for coordinated cleanup.
- `MUST` verify Docker/BuildKit filter semantics against the selected inventory before pruning
  and verify the resulting removals. BuildKit boolean fields are presence predicates;
  `inuse=false` and `shared=false` are not reliable selectors for unused/private records.
  Selecting a parent without its disposable children can return `0 B`; successful command
  exit or `Reclaimable: true` alone is not cleanup evidence. Never widen to global prune or
  restart a shared Docker daemon to bypass a failed scoped cleanup.
- `MUST` remove only an owned image tag when its image ID has another consumer or retained
  tag. Do not force-delete a shared image ID or another task's tag.

### Completion Receipt

- `MUST` save a sanitized cleanup receipt alongside the ledger, identifying exact removed and
  retained resources, native command outcomes, evidence locations, and observed disk-space
  change. Verify path/ID absence and compare Docker inventories before/after cleanup.
- `MUST` distinguish expected changes made concurrently by other owners from changes caused
  by cleanup; investigate unexplained differences rather than treating them as success.
- `MUST NOT` declare cleanup complete while owned residue is unaccounted for. Each resource
  must be verified `removed` or explicitly `retained` with the owner/consumer/release contract
  above. Failed cleanup remains a reported blocker with exact residue and next action; a
  successful test result remains recorded independently.
- `MUST` include cleanup status and any retained resources in the final handoff. Reviewers and
  release coordinators check the ledger and receipt before accepting task completion; an
  older task's resources are not silently inherited by the next task.

## Completion and Knowledge Capture

- `MUST` keep tracked `docs/` limited to current guides, reusable runbooks,
  normative contracts, standards, decision records and functional artifacts.
- `MUST NOT` commit task briefs, implementation plans, audit/review reports,
  dated acceptance evidence, screenshots or historical backlog under `docs/`.
- `MUST` keep open work in the issue tracker and disposable verification output
  in ignored `logs/` or `audit-artifacts/`. Skill-generated planning material
  follows this policy rather than creating `docs/superpowers/`.
- `MUST` update the current contract or ADR when a durable decision changes,
  without retaining obsolete designs as competing sources of truth.
- `MUST` check document consumers and links before deletion/moves. Configuration
  audit JSON and outbound HTTP inventory are functional artifacts, not old reports.

- `MUST` finish with a clear summary of what changed and how it was verified.
- `MUST` record durable lessons in project knowledge when the workflow requires it.
- `SHOULD` update or create ADRs for approved architectural decisions.
- `SHOULD` keep final reporting concise, concrete, and traceable to the change.

## Prohibited Behaviors

- `MUST NOT` perform destructive git operations without explicit approval.
- `MUST NOT` invent project rules outside the canonical standards.
- `MUST NOT` bypass verification when the task changes production behavior or contracts.
- `MUST NOT` hide uncertainty when risk is material.

## Review Expectations

- `MUST` reject agent changes that skip required context gathering on architectural or unfamiliar work.
- `MUST` reject unverified bug fixes and broad risky edits without approval.
- `SHOULD` treat agent-generated code by the same standard as human-written code.
