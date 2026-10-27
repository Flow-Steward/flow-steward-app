# Decision — Which definition findings block a workflow import

**Status:** ACCEPTED. Enforced by `WorkflowPortabilityImportMixin.DRAFT_DEFINITION_CODES`
in `core/application/workflows/workflow_portability_import.py` and pinned by
tests in `tests/unit/test_workflow_portability.py` and
`assets/frontend/src/pages/workflows/import/index.test.tsx`.
**Date:** 2026-09-04; revised 2026-10-12 for workflows without phases
([ADR 0035](../architecture/adr/0035-workflow-steps-without-phases.md)).
**Scope:** workflow bundle import (`platform.workflow_bundle.v2`) — preflight,
apply, and the import UI.

---

## TL;DR

Import runs the same definition validator as a change set over every workflow
in the bundle. An import is refused when the bundle describes a definition **no
valid workflow has in any state**. It is allowed, and the findings reported,
when the bundle describes a definition that is merely **unfinished**.

| Code | Import | Why |
| --- | --- | --- |
| `workflow.graph.missing` | allowed as a draft | The workflow has no graph yet; the author builds it in the editor. |
| `workflow.graph.entry_missing` | allowed as a draft | No entry has been chosen yet. |
| `workflow.graph.outcome_missing` | allowed as a draft | An outcome has no edge yet: the ending or a branch is not written, not wrong. |
| `workflow.graph.unreachable` | allowed as a draft | A node was authored without a connection; the author connects it in the editor. |
| every other definition code | **blocked** | For example `workflow.graph.target_unknown` (an edge points at a node that does not exist), `workflow.graph.outcome_unsupported` (an edge out of a Stop step, or a port the step does not have), `workflow.graph.cross_scope`, `workflow.graph.cycle_without_rework`, a placement refusal such as `workflow.placement.lane_kind_unsupported`, or `workflow.definition.legacy_key` (a phase-era field). |

A phase-based `platform.workflow_bundle.v1` bundle never reaches this check: it
is refused before anything is read as a definition, with
`bundle_schema_unsupported` and the `required_schema_version`.

## Why the split, rather than blocking everything

Blocking every definition finding is the tempting rule, and it is wrong in a
way that only shows up on real bundles: exporting a workflow that is still being
authored and importing it into another environment is the ordinary use of this
feature, and such a draft routinely has steps that are not connected yet. A
round-trip test (export an ordinary draft, import it back) fails under the
strict rule. So the boundary is whether an author can finish the workflow in the
editor after the import, or whether the bundle is describing something
incoherent that will fail validation no matter what they do. An imported draft
is stored disabled with its issues; enabling or running it requires a valid
definition.

## Why the permitted ones are still reported

The draft codes do not block, but they are returned in
`preflight.topology_issues` with `blocking: false` and rendered by the import UI
under "Workflow structure". The predecessor behaviour — accept silently, then
reject the same definition at readiness — is what made the defect expensive:
the import said success and the failure surfaced later, detached from its cause.

## What the UI must do with this

The import page gates Review and Apply on blocking findings as well as on
dependency resolutions, because `summary.blocking_count` counts dependencies
only: a structurally invalid bundle has nothing to resolve and would otherwise
walk the operator to an enabled Apply button that the backend refuses
(`workflow_import_topology_invalid`). A resolution on that page cannot change a
definition's shape, and the copy says so.
