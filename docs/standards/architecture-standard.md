# Architecture Standard

This document defines the target architecture and module boundary rules for the Flow Steward repository.

## Purpose

The goal of this standard is to keep the system evolvable, testable, and understandable
while supporting growth across multiple domains and runtimes.

## System Style

- `MUST` treat Flow Steward as a modular monolith.
- `MUST` keep domain boundaries explicit even when code lives in one repository and one coordinated deployment stack.
- `MUST` treat the first-release structural cleanup as a clean-slate reset, not as a promise to preserve folders outside the canonical layout.
- `MUST NOT` retain obsolete product API fields, UI sections, or fallback paths solely for legacy compatibility before the first release. Remove the retired path and its stale contract tests; useful operator diagnostics with a current owner are not compatibility fallbacks.
- `SHOULD` optimize for clear ownership, low coupling, and incremental refactoring.
- `MAY` introduce narrower architectural patterns inside a business area when they fit the area and do not weaken the overall system model.

## Runtime Ownership

Runtime ownership and filesystem layout are different axes.

The canonical runtime split is:

- `app` for HTTP transport, HTML, WebSocket flows, and transport-coupled orchestration entrypoints
- `control` for singleton control loops
- `worker` for stateless background execution
- `mcp` for inbound knowledge and platform transport, including reads and bounded
  mutations delegated to the application API with the caller's bearer token;
  application services own domain logic and the API enforces authorization

Rules:

- `MUST` place runtime logic in the runtime that owns the responsibility.
- `MUST` avoid duplicating ownership across runtimes.
- `MUST` document any new runtime responsibility in an ADR.
- `SHOULD` keep transport-coupled streaming behavior in the transport-facing runtime unless a replacement protocol is intentionally introduced.

## Canonical Repository Layout

The target canonical repository layout is:

- `core/entrypoints/` for transport and controller surfaces
- `core/application/` for first-party application logic
- `core/infrastructure/` for shared technical infrastructure and adapters
- `extensions/` for external or self-hosted extensions
- `db/schema/` and `db/seeds/` for canonical database artifacts
- `assets/` for frontend static assets
- `docs/`, `tests/`, `infra/`, `logs/`, `backups/`, and `locales/` for supporting repository concerns (including `core/entrypoints/cli/scripts/` ops/CI wrappers)

Rules:

- `MUST` treat `core/entrypoints` as the only long-term home for transport and controller code.
- `MUST` treat `core/application` as the only long-term home for first-party business logic.
- `MUST` treat `core/infrastructure` as the only long-term home for shared technical infrastructure.
- `MUST` keep generated or runtime artifacts out of `core/`.
- `MUST` keep extension packages outside `core/` in `extensions/`.
- `MUST NOT` keep or recreate a root `templates/` directory.
- `MUST` treat `infra/` as deployment, environment, and operational wrapper configuration only.
- `MUST` treat `logs/` as the long-term home for generated operational artifacts such as runtime logs, audit outputs, health snapshots, coverage exports, and scanner results.

## Entrypoint Responsibilities

Entrypoints are transport adapters and controllers.

They may:

- define routes or command handlers
- parse HTTP, WebSocket, CLI, or MCP input
- enforce boundary auth and permission checks
- call application services
- map results into responses, templates, events, or CLI output

They must not:

- contain business rules
- contain persistence logic
- send mail directly
- execute long-running orchestration inline
- own domain state

Rules:

- `MUST` keep entrypoints thin.
- `MUST` keep admin, API, web, WebSocket, CLI, and MCP logic in separate entrypoint folders.
- `MUST` treat `core/entrypoints/web/` as the public or anonymous HTTP surface only.
- `MUST` treat `core/entrypoints/admin/` as the authenticated HTML/form surface for signed-in users.
- `MUST` place JSON and REST handlers under `core/entrypoints/api/`, even when they support admin pages.
- `MUST` group entrypoint code by business area within each transport surface.
- `SHOULD` move repeated transport logic into shared helpers or application services.

## Application Responsibilities

`core/application/` owns first-party business logic.

Application packages may contain:

- `service.py`
- `repository.py`
- `models.py`
- `schemas.py`
- `queries.py`
- `policies.py`
- `errors.py`

Rules:

- `MUST` organize application code by business area, not by global technical buckets alone.
- `MUST` keep one business area under one owning package.
- `MUST` keep cross-domain coordination explicit in a coordination package such as `orchestration/`, not as hidden coupling between unrelated packages.
- `SHOULD` keep naming aligned across layers, for example `entrypoints/api/jobs.py`, `entrypoints/admin/jobs.py`, and `application/jobs/`.
- `MAY` introduce `application/admin/` only for operator-specific cross-domain use cases that do not belong to a single business area.

## Infrastructure Responsibilities

`core/infrastructure/` owns shared technical infrastructure.

It includes:

- config
- database pool and transaction helpers
- mail delivery
- observability
- security helpers
- extension runtime support

Rules:

- `MUST` keep infrastructure reusable and technical.
- `MUST NOT` place domain business rules in infrastructure modules.
- `MUST NOT` treat infrastructure as a dumping ground for unrelated helpers.

## Domain Organization

- `MUST` organize new features and major rewrites around business-area ownership.
- `MUST` assign one owner package for each domain entity or long-lived concept.
- `MUST` keep cross-domain read and write paths explicit through queries, services, gateways, or orchestration packages.
- `SHOULD` keep all moving parts of a business area easy to find together.
- `SHOULD` avoid broad churn when a scoped move will achieve the same ownership clarity.

## Dependency Direction

- `MUST` keep dependency direction intentional.
- `MUST` prevent transport concerns from leaking deeper through service imports or adapter-specific data shapes.
- `MUST` avoid circular dependencies across business areas and layers.
- `SHOULD` keep shared modules narrow and stable.
- `SHOULD` introduce cross-domain dependencies only when the business relationship is real and durable.

## Service Layer Policy

- `MUST` keep business logic outside HTTP, WebSocket, admin UI, CLI, and MCP entrypoints.
- `MUST` route state-changing behavior through command-oriented services.
- `MUST` route read-side aggregation and retrieval through query-oriented services where practical.
- `SHOULD` keep services cohesive around business use cases rather than generic helper dumping grounds.
- `SHOULD` extract shared workflows before parallel entrypoint implementations diverge.

## Data and Persistence Boundaries

- `MUST` keep database access behind service or repository paths owned by the application package.
- `MUST` place generic DB pool, transaction, and driver helpers in `core/infrastructure/db/`.
- `MUST` avoid scattering persistence logic through entrypoints, templates, and CLI presentation code.
- `MUST` treat `db/` as schema and seed artifacts, not as the runtime repository layer.
- `SHOULD` introduce repository-style abstractions where they improve clarity, testability, or storage isolation.
- `MAY` use direct domain-aware queries when a repository layer would be artificial and ownership remains clear.

## Migration Rule

The repository still contains top-level folders outside the canonical layout.

Rules:

- Folders outside the canonical layout are not valid long-term homes for new permanent code.
- New permanent code `MUST NOT` be added to those folders as their long-term home.
- Structural tasks `MUST` move touched code toward the canonical layout.
- Narrow bug fixes `MAY` patch an existing out-of-layout file in place when moving it is out of scope, but `MUST NOT` deepen that layout or create new long-term buckets there.
- Large rewrites `MUST` use the canonical layout directly.
- New brownfield product frontend work `MUST` use `assets/frontend/` as the canonical React workspace.
- Once a product surface exists under the React shell, the matching old template
  or page-script surface `MUST` be removed rather than kept as fallback mode.

## ADR Requirement

An ADR is required for:

- a new architectural pattern
- a new runtime responsibility
- a significant cross-domain dependency
- a new shared abstraction that affects multiple business areas
- a repository-structure exception
- a deliberate exception to this standard
