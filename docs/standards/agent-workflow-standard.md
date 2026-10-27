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

- `MUST` follow the three local verification tiers defined in `testing-standard.md`.
- `MUST` identify whether reported evidence is Tier 1, 2, or 3, or an additional audit check.

AC17-R97: Every tier runs inside the disposable `fs-audit` container, through `lint.sh --checks …`. Tier 1 is no exception: being focused does not mean running on the host. A host interpreter makes the result depend on the machine — Tier 1 could not run at all where `python3` is 3.14, and a host run of a suite passed while the same suite failed in the gate, because the gate substitutes the tree through `run-core-only-tests.sh`. `infra/tooling/quality/check-registry.json` is the single list of commands, and `tests/unit/tooling/quality/test_check_registry.py` fails if any check escapes the container.
- `MUST` add a regression test for every bug fix and observe the expected failure before applying the fix.
- `MUST` select targeted tests from changed behavior, owning tests, direct consumers, blast radius, and change-type requirements; start from `--test-paths auto` and add what the import graph cannot see.
- `MUST` use fail-fast execution during sequential iteration and diagnose a failure before retrying it.
- `MUST` keep Tier 1 focused cycles narrow and fail-fast, while the Tier 3 backend unit suite runs without `-x` or `--maxfail=1`, retains the complete failure list, and reports failures grouped by root cause for one coherent in-scope fix batch.
- `MUST` run `./core/entrypoints/cli/scripts/lint.sh --fast` before each intermediate commit.
- `MUST` run one clean `./core/entrypoints/cli/scripts/lint.sh --full` from the final committed state before handoff, pull request, or merge.
- `MUST` treat quality evidence as valid only for its recorded fingerprint and, for commit-scoped checks, its exact commit SHA; input-scoped checks are valid for any commit with the same fingerprint (`testing-standard.md`, ADR 0036). Local Docker evidence is sufficient; GitHub CI evidence is not required.
- `MUST` let each internal tier verifier execute only its registry-owned delta; the final `--full` wrapper validates or refreshes exact-SHA Tier 2 before Tier 3 and then validates their cumulative evidence.
- `MUST` start Tier 2 dependency audits before slower static checks and preserve their actual pass/fail evidence; deferred no-op checks are not valid evidence.
- `MUST` require complete, current Tier 2 and Tier 3 evidence for the final SHA before declaring a candidate publishable (input-scoped checks matched through that SHA's fingerprint); `--full` succeeds only after that cumulative validation.
- Additional deep audit checks `MAY` be requested through `lint.sh --checks`; `--audit` is a compatibility alias for the final committed gate.
- `MUST NOT` wait for GitHub Actions or report their absence as a blocker; workflows are disabled.
- `MUST` report exact commands, achieved tier, candidate provenance, and blocked or unrun verification.
- `MUST NOT` combine results from different commits or environments into one green result, except input-scoped evidence whose fingerprint matches the final tree.

## Owned Runtime Resource Cleanup

- `MUST` record exact ownership for every Docker container, network, volume, image tag, and
  candidate directory created during agent work.
- `MUST` clean up agent-owned disposable resources immediately after each completed test or
  verification run, whether it passed, failed, was accepted, or was rejected. Cleanup must
  happen before creating replacement resources, not wait until final handoff.
- `MUST` remove stopped or unused owned containers, networks, disposable volumes, and unused
  owned images, together with obsolete source/gate copies, generated dependencies, caches,
  temporary files, and logs no longer needed for diagnosis or required evidence.
- `MUST` preserve the minimal required evidence, receipts, checksums, screenshots, and backups;
  before deleting source copies, preserve unique uncommitted work as verified patches/archives
  and ensure committed work is reachable in a retained repository or verified Git bundle.
  Do not retain full source copies or caches merely to keep evidence.
- `MUST` treat an explicit user instruction to clean owned disposable resources as continuing
  authorization for that housekeeping; do not request confirmation again for the same scope.
- `MUST` preserve resources still required for active work or live review, including TLS/config
  dependencies, and record their exact identity, purpose, and cleanup trigger. Acceptance
  status alone is not a reason to retain an obsolete environment.
- `MUST` verify that removed resources are absent and record exact removed/retained paths or
  IDs, native outcomes, and the observed disk-space change.
- `MUST` select cleanup targets by exact recorded identity plus ownership labels; name globs,
  project-prefix guesses, and global prune commands are prohibited.
- `MUST` inventory Docker state before and after cleanup and treat any unrelated change or owned
  residue as a failure.
- `MUST NOT` remove the shared development database, knowledge-base volumes, unrelated Compose
  projects, unrelated images, or the shared BuildKit cache as part of automatic task cleanup.
- `MUST` report any intentionally retained candidate and the reason it remains.

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
