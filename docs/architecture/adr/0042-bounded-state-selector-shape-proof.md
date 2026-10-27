# ADR 0042: Bounded state selector shape proof

- Status: Accepted
- Date: 2026-10-25
- Related: ADR 0035 (workflow interface), ADR 0038 (pinned workflow definition)

## Context

Invoke availability formerly rejected descendants of the current ForEach item,
although runtime mapping resolves them. Conversely, an authored state selector
could traverse a known Batch chunk list as an object field and fail at runtime.
Availability maps describe names and declared types; they do not prove which
producer supplies a value on the current execution path.

## Decision

The registry owns `StateSelectorShape`, shared by Invoke input validation and
Template/Transform readiness. It uses the canonical state path parser, existing
execution graph queries and IOResolver write declarations. Shape evidence requires
a unique exact producer on an unambiguous successful predecessor chain, including
supported sequence and ForEach ancestry. Joins, rework (including returns to a container entry), overlapping or competing
writers and unsupported graphs leave shape unknown. Loop publication shadows
outside writes to the item/index names; body writes invalidate inferred item shape.
Exact producer leaf declarations retain their precision. Numeric index publication
applies until current body writes replace it; ambiguous writes remain unknown.
Publication shadows all earlier state bindings, including callable-input aliases,
while explicit producer message bindings remain available.

Batch proves a collection of chunk lists only when its source has a proven list
producer. Dataset and unknown sources do not supply that proof. The same bounded
fact rejects impossible member traversal and refines whole-item Invoke type
compatibility for both explicit state selectors and supported bare whole names.
Ordinary array elements remain unknown. Object numeric keys and
explicit array indices retain the canonical parser's distinct semantics.

An explicit parsed descendant of the actual current loop item may pass Invoke
availability with a warning that existence and type remain unknown. The existing
mapper and child input validator enforce the actual value at runtime. Other roots,
exited scopes, malformed selectors and bare dotted paths gain no availability.

## Consequences and limits

Conservative unknown results can defer errors in valid but unsupported graphs to
runtime. Known list/scalar traversal errors fail authoring at the source selector,
including optional Template inputs. Unknown object members remain deferred. Local
Template expressions are outside selector proof: passing a whole list into `b`
does not make `{{ b.size }}` valid, while its length filter remains available.

This is an authoring proof policy, not a graph dominator engine, element schema
inference, Jinja type checker or new runtime protocol. Runtime empty-collection,
child validation and final output authority contracts remain with their existing
owners. No persistent schema or execution semantics change.
