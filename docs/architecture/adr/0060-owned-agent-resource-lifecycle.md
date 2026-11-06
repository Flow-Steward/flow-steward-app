# ADR 0060: Journal-owned disposable agent resource lifecycle

Status: Accepted

## Context

Manual development and verification create containers, source copies and caches outside the
scope of the existing logs/workspaces retention job. Written cleanup obligations do not
provide executable ownership checks, interruption recovery or consumer tracking. Time-based
sweeps and global Docker pruning cannot distinguish released task resources from live reviews,
other projects or concurrent quality gates.

## Decision

Use a versioned local resource journal and an explicit managed-command lifecycle. Application
policy lives in `core/application/tooling/resource_cleanup`; native filesystem, Docker,
process and journal adapters live in `core/infrastructure/agent_resources`; the developer CLI
is a thin adapter in `core/entrypoints/cli/agent_resources.py`.

Canonical quality runs receive a generated Compose override rather than edits to shared
Compose files. Containers/networks and audit images carry the ownership contract; build
exports and tool-cache bind mounts live inside the task cache. The tools bind directory is
writable by the audit container's UID beneath the host's private 0700 task cache; other host
users cannot traverse its parent. This scoped permission permits Docker Desktop/Linux UID
differences without widening permissions on source directories or shared caches.

A registry lock serializes journal changes and cleanup across owners using that registry.
A foreground execution has PID and process-group leases. Only released, disposable resources
with matching native identities, owner/token markers and no active consumers can be removed.
The automatic wrapper reconciles owned Docker output even after command failure; command and
cleanup results remain separate. Atomic journal writes and persistent receipts retain state
for recovery after termination. Default standalone cleanup is preview.

Scratch directories are newly created under an exclusive per-user temporary namespace,
outside whole-checkout bind mounts. Arbitrary directory adoption is forbidden; Git checkouts,
worktrees, unexpected contents and symlink substitutions are retained for verified archival.
Docker resource identity includes daemon identity. Image cleanup removes a recorded tag, never
force-deletes a shared ID. A private docker-container builder is created with owner/token
options and removed only with its exact identity and no active build. Before creating one,
managed quality searches local images by the canonical toolchain digest, pins a verified
match by immutable ID and borrows it without aliasing, relabelling or removal. The gate
revalidates that ID/digest and prohibits implicit builds on a hit; a miss retains the owned
build path. Borrowed claims cover native image identity across tags and end only after
the managed foreground process and its children exit. Shared builders and
BuildKit cache sweeps are outside this collector's authority.

## Consequences

Agents must use the same retained primary registry for ownership and borrowing, label
Docker resources with the task/token/disposable contract, and explicitly retain resources
needed beyond a command. Unlabelled resources and old ad hoc journals are never silently
adopted. Resource labels are not an authentication boundary against a hostile local user;
the journal and Docker access are trusted local operator capabilities.

SIGKILL cannot run an exit handler. On recovery, live leases and retained consumers continue
to block deletion; a dead task's resources can be explicitly released and reconciled. An
expiry timestamp never authorizes removal of another task's work. The CLI is not a scheduled
machine-wide janitor and does not install a background service or alter other projects.
