# Backend Python Standard

This document defines the backend implementation rules for the Flow Steward repository.

## Purpose

The goal of this standard is to keep backend code consistent across all transport surfaces:
admin UI, web UI routes, REST API, WebSocket, MCP, webhook entrypoints, and CLI commands.

This document is the single source of truth for Python-specific conventions in
the standards extension. Other standards may reference it, but should not duplicate
the detailed Python style rules.

## Backend Surfaces

The target canonical backend surfaces are:

- admin authenticated HTTP handlers under `core/entrypoints/admin/`
- public or anonymous web routes under `core/entrypoints/web/`
- JSON and REST endpoints under `core/entrypoints/api/`
- WebSocket handlers under `core/entrypoints/ws/`
- MCP handlers under `core/entrypoints/mcp/`
- CLI commands under `core/entrypoints/cli/`

Rules:

- `MUST` treat HTTP, WebSocket, webhook, and CLI as transport layers.
- `MUST` treat MCP as a transport surface, not as a business-logic home.
- `MUST` keep transport-specific concerns at the transport layer.
- `MUST` keep business logic shared across transports when the use case is the same.
- `MUST` keep public login, bootstrap, password-reset, and other anonymous HTML flows under `core/entrypoints/web/`.
- `MUST` keep authenticated HTML and form-post controllers under `core/entrypoints/admin/`.
- `MUST` keep `/api/*` JSON handlers under `core/entrypoints/api/` rather than colocating them under `web/` or `admin/`.
- `SHOULD` place new endpoints and commands in the transport module that matches their interface.

## Transition Rule

The repository still contains transport folders outside the target layout such as `routes/`,
`routes/api/`, `routes/websockets/`, `cli/`, `admin/`, and `mcp_server/`.

Rules:

- These folders are outside the target layout.
- New permanent transport code `MUST NOT` use them as the long-term destination.
- Narrow bug fixes `MAY` patch them in place when a structural move is out of scope.
- Structural tasks `MUST` move touched transport code toward `core/entrypoints/`.

## Python Conventions

- `MUST` use `snake_case` for modules, functions, variables, and instance attributes.
- `MUST` use `PascalCase` for classes.
- `MUST` use `UPPER_CASE` for module-level constants.
- `MUST` prefer absolute imports by default and keep import ordering Ruff-compatible.
- `MUST` type public functions, methods, classes, and transport/service contracts.
- `SHOULD` use `TypedDict` for lightweight dict-shaped internal contracts.
- `SHOULD` use `dataclass` for structured in-process records with simple behavior.
- `SHOULD` use `Enum` for closed sets of symbolic values that cross module boundaries.
- `SHOULD` use Pydantic models for validated transport or persisted boundary schemas.
- `SHOULD` prefer composition over inheritance.
- `SHOULD` keep inheritance shallow and justify every non-framework base class.
- `SHOULD` add concise docstrings to public modules, classes, and functions when they define a stable contract, entrypoint, or non-obvious behavior.
- `MAY` rely on clear naming instead of docstrings for narrow private helpers.
- `SHOULD` add `from __future__ import annotations` to new or substantially touched Python modules when it simplifies forward references or typing; this is guidance, not a repo-wide enforced gate.

## Database Naming

- `MUST` use `snake_case` for table names and column names.
- `SHOULD` match the surrounding schema's prevailing plural-table style unless a domain already establishes a different convention.
- `SHOULD` prefer `idx_` for indexes, `uq_` for named unique constraints, `chk_` or `<table>_<field>_check` for check constraints, and `<table>_<column>_fkey` for foreign keys.
- `SHOULD` keep migration object names descriptive enough that operators can understand failures from database error text alone.

**Example — migration naming pattern:**

```sql
CREATE TABLE project_connections (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL,
    connection_kind TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_connections_project
    ON project_connections (project_id);
```

## Transport Handler Responsibilities

- `MUST` keep handlers thin.
- `MUST` limit handlers to auth, request parsing, validation, dependency wiring, service calls, and response mapping.
- `MUST` avoid embedding business rules, persistence orchestration, or cross-domain workflow logic in handlers.
- `SHOULD` move repeated handler behavior into shared dependencies or services.
- `SHOULD` keep root composition files and app bootstrap code focused on assembly and runtime wiring, not feature logic.

## Schemas and Contracts

- `MUST` define request and response contracts explicitly.
- `MUST` use structured schemas instead of raw dictionaries at transport boundaries.
- `MUST` validate external input at the boundary where it enters the system.
- `SHOULD` keep transport schemas separate from internal service models when the concerns differ.
- `SHOULD` make public contracts stable and intentional.

## Service Layer

- `MUST` place business logic in service or domain modules.
- `MUST` keep write behavior in command-oriented service flows.
- `MUST` keep read aggregation in query-oriented service flows where practical.
- `SHOULD` keep services organized around domain use cases, not generic helpers.
- `SHOULD` extract shared use cases before route, webhook, or CLI implementations diverge.

## Persistence and Data Access

- `MUST` access persistence through application-owned service or repository paths.
- `MUST` avoid direct database logic in handlers, templates, and CLI presentation code.
- `MUST` place repository code in the owning `core/application/<area>/` package.
- `MUST` place generic DB pool, transaction, and driver helpers in `core/infrastructure/db/`.
- `SHOULD` introduce repository-style abstractions only when they improve clarity, testability, or storage isolation.
- `SHOULD` keep query behavior explicit and local to the owning domain.

## Async and Execution Boundaries

- `MUST` keep async I/O explicit.
- `MUST` use async-safe primitives and interfaces at async boundaries.
- `MUST` keep long-running or background execution out of request-response handlers.
- `MUST` set explicit timeouts for outbound I/O on request paths and other operator-facing flows.
- `SHOULD` hand off background work to the owning runtime or queue path.
- `SHOULD` make retry, timeout, and degraded-mode behavior explicit.
- `SHOULD` use `async def` at boundaries that await network, database, filesystem, or queue I/O, while keeping pure CPU-only helpers synchronous unless an async interface is required.
- `SHOULD` prefer `asyncio.gather()` or `TaskGroup` only for independent I/O that can fail or cancel together cleanly.
- `SHOULD` bound concurrency rather than fanning out unbounded background work from handlers.
- `SHOULD` offload blocking library calls with `asyncio.to_thread()` or another explicit executor path when an async alternative is unavailable.

## Error Mapping

- `MUST` map service and validation errors to transport-appropriate responses.
- `MUST` keep internal exception details out of public payloads.
- `SHOULD` standardize error mapping instead of re-implementing it per route.
- `SHOULD` preserve useful context for logs and operator debugging.

## CLI Rules

- `MUST` treat CLI commands as first-class transport surfaces.
- `MUST` keep CLI command handlers thin and service-driven.
- `MUST` keep CLI output formatting separate from business logic.
- `SHOULD` reuse the same service paths that power API or operator flows when the use case matches.
- `SHOULD` support both human-readable and machine-readable output where the command is operationally relevant.

## Migrations and Storage Evolution

The database artifact policy (see ADR 0012) is:

- `MUST` treat `db/schema/` as the 1.0 baseline and `db/seeds/` as canonical seed
  data. Fresh installs initialize from `db/schema/`.
- `MUST` express every post-1.0 database change as a SQL file under
  `db/migrations/`. `db/migrations/` is the only allowed migration directory.
- `MUST NOT` create or allow a root-level `migrations/` directory.
- `MUST NOT` ship per-migration Python files; individual migrations are SQL only.
- `MUST` name migrations `YYYYMMDDHHMMSS_slug.sql` and let them be applied in
  lexicographic order by `flow-steward database upgrade --maintenance-confirmed --confirm`.
- `MUST` track applied migrations in `public.platform_schema_migrations` with a
  per-file checksum, and `MUST NOT` silently re-run or ignore an applied
  migration whose checksum changed.
- `MUST` keep transaction control in the runner: SQL files declare
  `-- transaction: true` (default) or `-- transaction: false` (for operations
  such as `CREATE INDEX CONCURRENTLY`) and contain no explicit transaction-control
  statements.
- `MUST` make destructive schema changes through a staged migration strategy.
- `MUST` document non-trivial storage changes when they affect contracts,
  rollout, or rollback.
- `MUST` keep the operator upgrade path on `flow-steward database upgrade --maintenance-confirmed --confirm`; runtime request
  handling `MUST NOT` apply arbitrary migrations.
- `SHOULD` keep migrations forward-only and small; recovery is restore-from-backup,
  not an implied downgrade.
- `SHOULD` verify schema changes with database-level tests when risk is non-trivial.

## Transport Placement

- `MUST` place new transport code under the matching `core/entrypoints/` folder.
- `MUST` group transport code by business area within that folder.
- `SHOULD` start with one file per business area, for example `jobs.py`, `workflows.py`, or `registry.py`.
- `MAY` split a business area into a package when one file becomes too large.

## Application Placement

- `MUST` place first-party business rules, use cases, and repositories under `core/application/<area>/`.
- `MUST` keep transport handlers calling application services instead of embedding the logic inline.
- `MUST` keep repository code inside the owning application package, not in entrypoint or infrastructure folders.
- `SHOULD` create a focused application package before extending an oversized mixed-responsibility module that still sits outside the target layout.

## Admin UI and Auth Placement

- `MUST` treat admin UI as a transport surface, not as a business domain by itself.
- `MUST` place admin routes and controllers under `core/entrypoints/admin/`.
- `MUST` keep public auth pages out of `core/entrypoints/admin/`; they belong to `core/entrypoints/web/`.
- `MUST NOT` add admin/product HTML templates; admin/product UI belongs in React and API controllers.
- `MUST` place shared authentication, login, session, and password-reset logic under `core/application/auth/`.
- `MAY` introduce `core/application/admin/` only for operator-specific cross-domain use cases that do not belong to a single business area.
