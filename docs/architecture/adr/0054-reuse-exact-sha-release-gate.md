# ADR 0054: Reuse an exact-SHA hosted gate at publication

Status: Accepted (requested by the repository owner on 2026-11-03).

## Context

The tag publication workflow repeated the full quality gate even when the same
public commit had already passed the cumulative hosted gate. This delayed image
publication and reran expensive suites without a source change.

## Decision

Amend ADR 0048: before running the publication gate, query GitHub Actions for a
successful run of `remote-quality.yml` for the exact resolved tag commit. Reuse
only a completed successful run from the same public repository and head
repository, with a successful `quality` job whose Tier 2, cumulative Tier 3,
and final main-ancestry recheck steps all completed successfully. Record the
run ID, attempt, SHA, and URL in the publication evidence artifact. A failed,
incomplete, skipped, missing, differently sourced, or different-SHA result is
not a pass. A missing result or lookup error falls back to the complete tag gate.

This decision does not turn separately passing tests into a complete gate and
does not change native image builds, registry verification, or publication
checks. Individual Tier 3 suites remain commit-bound; separate reuse across
changed commits requires a future complete fingerprint and provenance design.

## Consequences

A release can publish after a previous cumulative gate without repeating it.
Publication depends on GitHub Actions read access to verify run identity; when
that lookup cannot be proven it repeats the normal gate. Only a workflow change
in a future commit can implement this decision; existing tags and runs are not
rewritten.
