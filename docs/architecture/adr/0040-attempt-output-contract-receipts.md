# ADR 0040: Execution-attempt output contracts and internal producer receipts

- Status: Accepted
- Date: 2026-10-24
- Related: ADR 0035 (workflow interface), ADR 0038 (pinned run definition)

## Context

A diagnostic artifact with the same name as a workflow output could satisfy the
workflow interface without any declaration by its producer. Looking up current
registry declarations at completion would reinterpret results after registry edits.
Workflow definitions are admission-pinned; external execution dependencies are
resolved for each attempt. The approved Task33 decision uses that same lifetime.

## Decision

The application registry owns a detached canonical output contract value. Readiness
and execution share the established published-block selection scores and tie order,
filtered to global or current-account rows. An applicable enabled published block's
exposed outputs are the entire surface, including an empty surface. An explicit
unavailable block fails resolution. Without a block, the selected authorized Skill
(including schema-only properties), genuine Agent-own fields, and legacy manifest
own output names form the contract. Source-erased inherited fields, other Skills,
tool diagnostics and mapping targets do not confer direct artifact authority.
Conflicting declaration types and failed lookups fail before Agent invocation.
Generic registry `json` retains its unknown/scalar-compatible producer-readiness
tag and JSON value validation; an authored or schema-only object stays precise.
Each normalized declaration captures two detached schema views in the digest:
`contract_schema` for declaration validation and `response_schema` for provider
formatting. Both PromptAgent and wrapper checks consume the first; the provider
response-format builder consumes the second. The existing pure format helper is
shared: legacy `json` requests object with additional properties, and `list`
requests an array of strings. Those formatting defaults do not invent validation
constraints: generic JSON scalars and generic lists remain accepted. Authored
schemas retain their exact constraints in both views. Schema-only unions and
compositions retain those exact constraints; when no single non-null type describes
them, readiness uses the existing unknown JSON classification. No first union
member or absent top-level type invents a narrower declaration. Direct non-workflow callers
retain their existing validation and format defaults.

Linked Data Contracts are read once at capture, with existing availability and
account scope checks. Their authored constraints stay precise, including external
derived contracts. Only a `derived` contract with `derived_from_kind=field_shape`,
an exact field-name reference, matching generic `json`/`list` kind, and schema
identical to the existing generated default is treated as formatting metadata.
Legacy Skill metadata aliases are normalized only when the host row's complete
schema exactly equals its existing contract-schema builder applied to those rows.
Manual invalid JSON Schema remains a resolution error. Neither receipt consumers
nor terminal collection reload these linked declarations.

Authored output descriptions are captured as presentation metadata in the same
immutable value and digest. Published blocks share the existing notes, field
description, then display-name fallback; Skill and Agent-own rows preserve their
authored descriptions, schema-only fields preserve their schema annotation, and
legacy name-only outputs have no invented description. Duplicate declarations keep
the first nonempty description. Editor projections never mark authored text as
localized platform copy. The existing response helper supplies a top-level
description only when the authored schema has none; validation schemas remain
exact. Response-view merge comparisons ignore only this top-level description
annotation when distinguishing formatting defaults from authored constraints;
all real constraints and an authored schema description remain intact. When
constraint shapes match, intact response/declaration equality distinguishes an
authored schema annotation from injected row text: the authored annotation wins
in the response view in either merge order, while editor metadata retains its
first nonempty row description.

The host wrapper captures once before invocation. Output prompt guidance, the
phase-result response schema, PromptAgent checks and wrapper checks consume that
same immutable value. Input guidance and authored system/Skill instructions remain
under their existing owners. Legacy direct callers keep their existing behavior.

A successful validated Agent message may carry an additive internal
`output_contract` receipt, version `agent_output_contract_v1`. It contains canonical
normalized declaration content and its digest, verified workflow definition digest
and snapshot identity, job, exact step/execution/attempt, and actual Agent/Skill.
The host constructs it; Agent metadata and model artifacts cannot populate it.
Values remain in `artifacts`. No SQL or admission snapshot protocol changes are needed.

Terminal validation retains its verified pinned definition and passes producer
context to collection. A bare artifact is eligible only with a matching receipt,
pass status, declared exact name and pinned producer binding. Runtime participants
remain supported; a completed producer need not be the terminal cursor. Collection
does not reload current registry contracts. Rejected direct candidates fall through.

Per-name precedence remains: state outputs, nested workflow-data outputs, workflow
data root, then each message newest first with structured state patch, eligible
direct artifact, and terminal outputs. Presence preserves false, zero, null and empty
values. Explicit structured paths remain supported; this decision does not harden
all state-write ingress. Returned files remain captured artifacts. Invoke keeps
validated child outputs authoritative, including absence of optional outputs.

Canonical MessageBus and checkpoint serialization retain the receipt. Display
redaction may change artifact values, but retains host contract metadata. Public
message and replay projections omit only this metadata and never mutate internal
history. Malformed optional historical receipts lose direct eligibility without
invalidating otherwise valid messages and their explicit structured fallback.

## Consequences

Already-finalized workflow outputs remain unchanged. An active historical message
without a receipt cannot establish a direct-only required output. Restarting or
rerunning with a new validated producer is necessary; today's registry is never
used to backfill historical authority. Fresh step reexecution captures its current
dependencies and publishes a fresh receipt; it does not prove an older contract.
The receipt is host provenance inside the trusted runtime, not a signature or a new
public input/API contract. Real provider and installed-worker acceptance remains a
separate release verification step from fake-provider transport unit coverage.
