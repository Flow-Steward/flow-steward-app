# ADR 0007: Canonical Repository Structure

- Status: Proposed
- Date: 2026-04-12

## Context

The repository still reflects multiple historical layouts at once: transport-first
folders, large global service buckets, mixed infrastructure buckets, and generated
artifacts living under broad operational directories. This makes the codebase hard
to navigate for contributors and AI agents, and it conflicts with the first-release
goal of presenting one clean, understandable architecture in open source.

The project also needs a structure that separates:

- transport surfaces such as admin, API, web, WebSocket, CLI, and MCP
- first-party application logic
- technical infrastructure
- extension packages
- generated operational artifacts

## Decision

Flow Steward uses one canonical repository structure for first-party code:

- `core/entrypoints/` for transport and controller code
- `core/application/` for first-party business logic
- `core/infrastructure/` for shared technical infrastructure and adapters
- `extensions/` for external or self-hosted extensions
- `db/schema/` and `db/seeds/` for canonical database artifacts
- `assets/` for frontend static assets
- `docs/`, `tests/`, `infra/`, `logs/`, `backups/`, and `locales/` (including `core/entrypoints/cli/scripts/` wrappers) for supporting repository concerns

Older top-level folders outside that layout are migration-only surfaces. They are
not part of the intended long-term public architecture.
Root `templates/` is forbidden. Product/admin UI lives in React under
`assets/frontend/`; non-UI templates live under their owning infrastructure package,
as specified by [ADR 0008](0008-react-fastapi-frontend-migration.md).

## Consequences

- New contributors should be able to understand the repository root quickly.
- Transport code becomes easier to find because admin, API, web, WebSocket, CLI, and MCP have explicit homes.
- Business logic and repositories become easier to find because they live under one first-party application tree.
- Infrastructure concerns stop competing with business modules for top-level namespace.
- Large migration work is required to move existing code into the canonical layout.
- Narrow bug fixes may still touch older-layout files in place during the transition, but new permanent code should not deepen the old structure.

## Alternatives Considered

- Keep the current hybrid layout indefinitely
- Adopt pure layer-based top-level folders such as `routes/`, `services/`, `models/`, and `schemas/`
- Adopt a full bounded-context DDD layout with domain packages as the only top-level organizing principle
- Keep broad `ops/` and `projects/` style buckets for mixed operational and local artifacts

## Rollout Notes

Apply incrementally through explicitly scoped migration tasks.

During the transition:

- standards and new structural work should target the canonical layout
- older-layout folders may be patched for narrow fixes when a move is out of scope
- large rewrites should land directly in the canonical layout
- exceptions require ADR-backed approval

## Related Standards and Docs

- `docs/standards/README.md`
- `docs/standards/architecture-standard.md`
- `docs/standards/backend-python-standard.md`
- `docs/standards/configuration-standard.md`
- `docs/standards/agent-workflow-standard.md`
