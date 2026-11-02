# ADR 0053: Hosted quality gates for the public source

Status: Accepted for pilot — owner authorized the experiment on 2026-11-02.
Amends the execution-location policy in ADRs 0021 and 0048; registry ownership
and the evidence fingerprint rules in ADR 0036 remain unchanged.

## Context

Parallel agents compete for the developer's CPU, memory and Docker disk. A queue
prevents oversubscription but makes each agent wait for long gates. Public
standard GitHub-hosted runners provide isolated execution without buying a
persistent machine. The public source has its own history; private development
history must never be sent to that source as part of verification.

## Decision

Keep narrow Tier 1 checks local. Run the canonical Tier 2 Docker gate after each
public branch push. Run the cumulative Tier 2/Tier 3 gate before merge to main;
the existing frontend/backend dependency order stays intact. Use only the fixed
standard ubuntu-24.04 runner, read-only repository permissions, pinned Actions
and no publication or deployment side effects.

The candidate must be clean, match the workflow SHA, and already include freshly
fetched main. Do not automatically merge or resolve conflicts in CI. Check this
again after expensive verification; the merging agent checks immediately before
merge as well. Main advancing invalidates a candidate that no longer contains it.

Build caches use dedicated remote scopes; package caches contain downloads,
not passed quality evidence. Every job validates its own evidence. Keep reports
three days by default. Use a run/attempt/job-specific Compose project, always
tear it down, and rely on hosted VM disposal for forced cancellation. Never
globally prune the developer's daemon or attach a self-hosted runner.

Full Sonar analysis runs on a second disposable runner with coverage downloaded
only from the same workflow run and SHA. Initially its Sonar server is temporary
and has no historical baseline. The result is explicitly a pilot: compare/migrate
the existing quality profiles, gates and new-code baseline before replacing the
existing Sonar release decision. Do not loosen the canonical scanner profile.

## Consequences

The pre-commit hook runs Tier 1; a committed push can exist while Tier 2 is
pending, but it cannot be reported as verified. Local Docker gates remain a
diagnostic/fallback route. An intermediate PR does not require the full suite;
merge/publication does. Branch protection and automatic merging are not changed.

The first pilot branch push runs the full gate and disposable Sonar experiment;
normal branches run Tier 2. Task-specific `codex/verify-release/` and
`codex/verify-sonar/` refs request the full modes through ordinary Git write
access, preserving the candidate SHA and avoiding a shared-ref race between
agents. Manual release modes become selectable after the workflow reaches main.
No successful hosted run is assumed until measured.

These source-control refs are external tooling metadata. The retired-executor
guard admits only their exact reviewed documentation lines; its runtime and
product-surface prohibition remains in force.

## Incremental local commit checks

Both local pre-commit test hooks use `auto:HEAD` to check the current commit's
change. Normal pre-commit stashing isolates tracked changes; untracked files can
also broaden the selection. This is not a strict index-only selector or proof
for the whole branch. An incoming merge is compared with its first-parent HEAD.
Manual `auto` still compares the accumulated branch change with `main`, and
explicit regression paths remain necessary for dependencies outside the graph.
A clean committed HEAD can select no tests locally; the remote exact-SHA
cumulative gate remains mandatory and retains its complete suite scope.
