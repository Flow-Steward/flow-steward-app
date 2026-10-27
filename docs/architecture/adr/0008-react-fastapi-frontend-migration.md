# ADR 0008: React + FastAPI Frontend Migration

- Status: Accepted
- Date: 2026-04-14
- Supersedes: `0006-frontend-stack.md`

## Context

Flow Steward's former frontend baseline used server-rendered Jinja2 templates with
Tailwind, vanilla JavaScript, HTMX, and `platform-kit`. That baseline worked
for the early product, but the product surface grew into a large UI
footprint with dozens of templates, custom page scripts, and state-heavy
surfaces such as jobs, settings, registry, and workflow authoring.

The accepted frontend architecture is:

- FastAPI remains the backend source of truth
- the product frontend uses `Vite + React + TypeScript`
- UI composition uses `shadcn/ui`
- server state uses `TanStack Query`
- forms use `React Hook Form + zod`
- workflow authoring uses `React Flow`
- authentication remains same-origin cookie + CSRF

This replaces the prior template-first frontend decision for new product
surface work.

## Decision

The product frontend lives in the dedicated React workspace under
`assets/frontend/` and is served by the existing FastAPI runtime.

The approved stack is:

- `Vite`
- `React`
- `TypeScript`
- `shadcn/ui`
- `TanStack Query`
- `React Hook Form`
- `zod`
- `React Flow` for workflow authoring

The migration contract is:

- FastAPI remains responsible for business logic, validation, persistence,
  orchestration, realtime events, and transport surfaces
- React owns route composition, page state, form state, and presentation for
  migrated product surfaces
- React owns public product routes and runs under `/app/*` alias namespace
- public product URLs are preserved where practical at final cutover
- auth remains same-origin cookie-based with CSRF
- `Next.js` is not introduced

## Consequences

- New product-surface work must target the React workspace.
- A React replacement retires the corresponding template-era Jinja/HTMX
  product pages and page scripts; no parallel product fallback is retained.
- Shared frontend infrastructure now includes React-specific runtime support,
  typed HTTP helpers, query client setup, and page-group routing under
  `assets/frontend/src/`.
- FastAPI must serve the React shell and built assets predictably for all
  product routes.
- Contributor guidance and quality gates must prioritize React workspace
  completeness and remove fallback coupling from product route ownership.

## Alternatives Considered

- Keep extending the current template-first product UI
- Big-bang frontend rewrite
- Adopt `Next.js`
- Move auth to a cross-origin SPA or token-first model

## Why Next.js Was Not Chosen

The migration does not need a second backend runtime, server components,
framework-coupled routing, or a separate auth model. The existing FastAPI
runtime already owns auth, transport, realtime, and business logic. `Vite` is a
better fit for a brownfield SPA mounted into the current backend without
expanding the runtime footprint.

## Why Auth Stays Same-Origin Cookie + CSRF

The current backend already owns sessions, login/logout, account switching, and
CSRF behavior. Replacing that model during the UI migration would widen risk and
couple frontend delivery changes to security architecture changes. The migration
keeps auth semantics stable while the UI stack changes.

## Workflow Authoring

Workflow authoring uses the React shell and `React Flow` canvas. It shares the
same transport, form, realtime and authentication contracts as the other product
surfaces. Builder execution follows [ADR 0010](0010-flowsteward-builder-test-run-rule.md).

## Route Ownership

- Keep `/app/*` as a stable React alias namespace
- keep public product routes React-owned
- retire template-era product assets once equivalent React ownership is in place

## Related Standards and Docs

- `docs/standards/frontend-standard.md`
- `docs/standards/architecture-standard.md`
