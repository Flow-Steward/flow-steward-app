# Flow Steward Engineering Standards

This directory contains the canonical engineering standards for the repository.
These documents apply to both human contributors and AI agents.

## Purpose

The standards exist to keep architecture, code quality, testing, security,
configuration, frontend behavior, and agent execution consistent across the codebase.
Rules are written once here and referenced from `AGENTS.md`.

## Normative Language

- `MUST` means the rule is mandatory.
- `SHOULD` means the rule is the default and requires a clear reason to deviate.
- `MAY` means the rule is optional.

## Document Order

1. `engineering-standard.md`
2. `architecture-standard.md`
3. `backend-python-standard.md`
4. `configuration-standard.md`
5. `testing-standard.md`
6. `security-standard.md`
7. `frontend-standard.md`
8. `agent-workflow-standard.md`
9. `extension-manifest-standard.yaml`
10. `enforcement-matrix.md`

## Change Control

- Add a new rule only when it solves a repeated real problem or clarifies an approved architectural decision.
- Update canonical documents here first.
- Do not maintain separate manual rule sets for specific AI tools.
- If a tool-specific format is ever needed, generate it from these documents rather than editing it by hand.

## Canonical Repository Layout

The target canonical repository layout for the first-release structural migration is:

- `core/entrypoints` for transport and controller surfaces
- `core/application` for first-party application logic
- `core/infrastructure` for shared technical infrastructure
- `extensions` for external or self-hosted extensions
- `db/schema` and `db/seeds` for canonical database artifacts
- `assets` for frontend static assets
- `assets/frontend` for the brownfield React product frontend workspace during migration
- `docs`, `tests`, `infra`, `logs`, `backups`, and `locales` for supporting repository concerns (`core/entrypoints/cli/scripts/` for ops/CI wrappers)

Rules:

- New standards and code placement decisions `MUST` align with this layout.
- Folders outside this layout are temporary transition surfaces, not a second valid long-term structure.
- Root `templates/` `MUST NOT` exist. Product/admin UI belongs in React, and non-UI templates must live under their owning infrastructure package.
- If a new architectural exception is needed, it `MUST` be approved by ADR.

## Canonical Terminology

The standards extension imports these terms as canonical shorthand from the approved
architecture policies:

| Canonical term | Avoid in new work | Meaning |
|---|---|---|
| `connection` | `binding` | Project-scoped activation/configuration unit such as `project_connection`, `connection_config`, and `connection_id`. |
| `skill` | `capability` in user-facing copy | Public UI/docs/product term for a reusable agent ability. |
| `capability` | informal mixing with `skill` in internal code | Internal/runtime/database term, including existing `capability_id` identifiers. |
| `FS` | new neutral identifiers containing retired project codenames or prior `platform` naming | Canonical short stem for new internal/runtime naming; retired project codenames and prior `platform` naming belong only to explicitly preserved surfaces. |
| `backend surface` | “API-only backend” | Backend includes web routes, JSON/API endpoints, WebSocket handlers, webhook ingress, MCP, and CLI transports. |

## Current Repository Reality

These standards describe the target direction for new work and meaningful
refactors. They are not a claim that the entire repository is already fully aligned.

- Some oversized hotspots still exist, including `core/infrastructure/runtime/worker/worker.py`, `core/infrastructure/db/platform_logger.py`, `assets/platform-kit/components/platform-grid.js`, and `assets/platform-renderer.js`.
- The service-account settings page remains an oversized UI hotspot, and machine-token authority policy still spans `core/entrypoints/api/auth_tokens_accounts.py` and `core/infrastructure/db/logger_extensions_accounts_service_tokens_mixin.py`; bounded fixes should not treat that current layering as the target architecture.
- Retired root-level buckets such as `routes/`, `services/`, `models/`, `schemas/`, `integrations/`, `admin/`, `worker/`, `control/`, `orchestrator/`, `registry/`, `ops/`, and `projects/` are absent. Do not recreate them; new permanent code must follow the target layout.

## ADR Policy

Use an ADR for:

- architecture changes
- new runtime responsibilities
- new shared abstractions
- significant security decisions
- frontend stack changes
- migration strategy changes
- explicit exceptions to a `MUST` rule

## Enforcement

Every `MUST` rule should have one of these enforcement paths:

- automated check
- automated test
- architecture test
- code review
- ADR approval

See `enforcement-matrix.md` for the rule-to-enforcement mapping.
