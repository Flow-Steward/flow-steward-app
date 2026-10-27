# ADR 0019: Dataset Connection Actions Belong to the Application Layer

## Status

Accepted — 2026-07-29

## Context

`core/application/datasets/connection_actions.py` executes actions against
persisted data connections by resolving the runtime kind and secret refs.
For `runtime_kind=platform`, it delegates to the existing shared infrastructure
adapter in `core/infrastructure/db/platform_connection_actions.py`.
For `local_extension` or `remote_extension`, it resolves and invokes the installed
extension runtime, normalizes the response, and applies any returned
project-state updates.

A directory-level ban on dataset runtime code would also ban this host-owned
orchestration. The architecture must distinguish application coordination from
shared technical adapters and extension-owned external integration protocols.

## Decision

**Dataset connection actions stay in the application layer**, and the guardrail
is narrowed rather than deleted.

The boundary the original guardrail was protecting is real, but it was expressed
as a path ban, which is a blunt instrument. The property that actually matters
is:

> The application layer orchestrates data-connection actions. Connector
> protocols, drivers, and vendor-specific transport do not belong in
> `core/application/datasets/`. Existing platform connection actions are
> delegated to the shared infrastructure adapter; extension-backed external
> integrations stay behind installed extension contracts.

`connection_actions.py` satisfies that property. It contains no driver, no
protocol, and no vendor-specific transport. It routes platform actions to
`run_platform_connection_action` in the existing infrastructure adapter and
extension-backed actions to the installed extension runtime. For extension
responses, it normalizes the result and applies returned project-state updates.
That orchestration is application work by the platform's own layering standard:
it coordinates infrastructure and extensions on behalf of an entrypoint, and it
has no business living inside an extension, which cannot see the connection
registry or the secret store.

Accordingly:

1. `core/application/datasets/` is a legitimate application-layer package.
2. The guardrail is rewritten to assert the **semantic** boundary — no driver,
   protocol, or vendor transport implementation in that package — instead of
   banning the path.
3. The other forbidden paths in that guardrail are unchanged. Those still assert
   genuine "partial runtime leftover" states.

## Consequences

- The guardrail now fails for the reason it was written for. A future change that
  imports a database driver, opens a socket, or hand-rolls a vendor protocol
  inside `core/application/datasets/` is caught; simply having files there is not.
- A path ban is easy to satisfy by moving code somewhere else without changing
  its nature. A semantic assertion is harder to satisfy dishonestly, which is the
  point.
- If dataset execution later grows genuine protocol work, that work goes behind
  an extension contract and this ADR is superseded rather than quietly widened.
