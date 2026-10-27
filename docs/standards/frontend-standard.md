# Frontend Standard

This document defines the frontend implementation rules for the Flow Steward repository.

## Purpose

The goal of this standard is to keep Flow Steward frontend code consistent, resilient,
and maintainable across React product surfaces.

## Frontend Stack Baseline

Flow Steward product frontend is React-first.

Active frontend delivery mode is:

- the React product workspace under `assets/frontend/` using `Vite + React +
  TypeScript`, `shadcn/ui`, `TanStack Query`, `React Hook Form`, and `zod`

Rules:

- `MUST` treat the React stack as the default product frontend baseline.
- `MUST` build new migrated product surfaces in `assets/frontend/`.
- `MUST` keep public product routes React-owned by default.
- `MUST` keep `/app/*` as a valid React shell alias.
- `MUST NOT` gate public React route ownership behind environment flags or
  route-group cutover toggles.
- `MUST NOT` introduce fallback template ownership for product routes.
- `MUST NOT` introduce `Next.js` for the product frontend.
- `MUST` preserve same-origin cookie + CSRF auth for the React frontend.

## Template Rules

- `MUST NOT` add product or admin web UI screens to Jinja templates.
- `MUST` keep product/admin UI in the React workspace.
- `MUST NOT` recreate a root `templates/` directory for product/admin UI.
- `MUST` keep remaining non-UI templates, such as email templates, isolated under their owning infrastructure package.
- `MUST` preserve escaping and safe rendering defaults for non-UI templates.
- `MUST` keep page-level data requirements in React/API contracts, not server-rendered template contexts.

## JavaScript Organization

- `MUST` keep complex client behavior out of sprawling inline scripts.
- `MUST` place new reusable client logic in `assets/` modules or the owning frontend layer.
- `MUST` keep transport calls, DOM wiring, and UI state handling explicit.
- `SHOULD` use module-based JS for reusable or growing behavior.
- `SHOULD` keep classic script usage narrow and page-specific when modules are not needed.

## React Workspace Rules

- `MUST` place the React product workspace under `assets/frontend/`.
- `MUST` keep route-level screens under `assets/frontend/src/pages/`.
- `MUST` keep reusable React UI under `assets/frontend/src/components/`.
- `MUST` keep shared React infrastructure only under `assets/frontend/src/lib/`.
- `MUST` keep page-local request modules, forms, schemas, hooks, and mappers next to the owning page group.
- `MUST NOT` create a catch-all `src/lib/api/` bucket for domain request code.
- `MUST NOT` let React page components call raw `fetch(...)` directly.
- `MUST` route shared transport concerns through shared low-level HTTP helpers.
- `MUST` keep workflow-specific editor state and builder code under `src/pages/workflows/`.

## Platform Kit and Reusable UI

- `MUST` add new reusable product UI primitives through React shared component
  layers (for example `assets/frontend/src/components/`) or another approved
  React-native shared layer.
- `MUST` keep shared frontend assets under `assets/`.
- `MUST` treat `platform-kit` as maintenance-only and keep new product UI in
  React components.
- `MUST` build existing `platform-kit` components on the `PlatformElement` base class from `assets/platform-kit/platform-base.js`.
- `MUST` register custom elements with the repo's `platform-*` tag naming convention.
- `MUST NOT` introduce any retired old-brand custom-element prefix for new frontend work.
- `MUST` treat `assets/platform-kit/tokens.css` as the shared token source.
- `MUST` keep shared components generic enough to serve more than one page or domain.
- `MUST` avoid duplicating the same interactive behavior across unrelated pages when a shared primitive is appropriate.
- `SHOULD` prefer composition of existing primitives over one-off ad hoc widgets.
- `SHOULD` document new reusable UI patterns when they become part of the platform surface.
- `SHOULD` keep component event names aligned with established `platform-*` patterns such as `platform-action`, `platform-query`, `platform-save`, `platform-validate`, `platform-test`, `platform-detail`, `platform-confirm`, `platform-cancel`, and `platform-navigate`.
- `SHOULD` keep event payloads explicit around identifiers such as `component_id`, `query_id`, `action_id`, `context`, and `item_ids` when those concepts apply.
- `MUST` use `shadcn/ui` as the shared React UI foundation for migrated product surfaces.
- `MUST` avoid expanding `platform-kit` usage for new product route behavior.

## Styling and Tokens

- `MUST` keep visual styling aligned with the existing Tailwind and token-driven system.
- `MUST` treat shared tokens and design variables as the canonical source for repeated visual values.
- `MUST` avoid scattering hardcoded visual constants when a shared token or utility is the right home.
- `SHOULD` group shared visual decisions into stable tokens or utilities.
- `SHOULD` keep design changes consistent across related surfaces.

## Accessibility and UX Baseline

- `MUST` preserve semantic HTML, keyboard accessibility, and focus visibility on interactive flows.
- `MUST` keep labels, button names, status messages, and error states understandable.
- `SHOULD` use progressively enhanced interactions rather than making core workflows JS-dependent without need.
- `SHOULD` avoid introducing HTMX dependencies on React-owned product routes.
- `SHOULD` keep UI behavior predictable across operator-critical screens.

## Workflow Builder UX Rules

- `MUST` launch whole-workflow `Test Run` from the lower canvas toolbar through the shared Form Dialog and the existing page-owned Test Run controller, keep run status on the canvas and results in a separate Results drawer using shared Job Details/log semantics, and preserve project, dirty-form, applied-snapshot, expected-revision and validation guards.
- `MUST` keep step-level `Test Run` inside the step drawer rather than routing users to a separate page.
- `MUST` reuse the workflow contract forms in the Interface drawer's Inputs and Outputs tabs. Visual Start opens Inputs, and Stop Success opens the shared workflow Outputs contract. Returned files are listed in Outputs read only; they are set only by a step's "Return from workflow" setting (ADR 0039), never by a second editable declaration.
- `MUST` apply workflow step-family colors only to the icon and its tile; card surfaces remain neutral and use shared theme tokens. Selection, warning, error and execution states retain their semantic colors in both themes.
- `MUST` keep step input visibility builder-native with an always-visible summary covering `Inputs`, `Mapped from`, `Missing`, and `Outputs`.
- `MUST` keep advanced-only controls such as `Parallel Group` and `Optional step` editable only when advanced mode is on.

## Runtime Quality and Resilience

- `MUST` keep core frontend flows free of console errors and page errors.
- `MUST` keep user-facing failures visible and recoverable where possible.
- `MUST` avoid JS behavior that silently fails on missing globals or mismatched page wiring.
- `SHOULD` fail clearly when required runtime data is absent.
- `SHOULD` keep client-side state and server-side contract aligned.
- `MUST` keep React asset serving explicit and cache-safe.
- `MUST` keep the React build pipeline independent from template-era asset build tooling.

## Frontend Testing Expectations

- `MUST` verify UI-critical changes at the affected layer.
- `MUST` test JS-critical flows with runtime-error awareness for the affected path.
- `MUST` verify changed UI contracts when components, routes, or page wiring are modified.
- `SHOULD` prefer focused UI or integration tests over broad end-to-end tests when the risk is localized.
- `SHOULD` update tests when the approved UI contract changes.
- `MUST` verify the React workspace with frontend-native checks when it is touched, including lint, unit/integration tests, and build validation.
- `SHOULD` add browser coverage for migrated product flows under the React shell.

## Review Expectations

- `MUST` reject new complex inline JS where shared or modular code is the correct path.
- `MUST` reject frontend changes that introduce console/page errors on core flows.
- `MUST` reject repeated UI logic that should live in a shared component or pattern.
- `SHOULD` flag visual or interaction drift from the platform baseline.

**Example — shared component interaction pattern:**

```html
<platform-grid id="projects-grid"></platform-grid>
```

```js
const grid = document.getElementById("projects-grid");

grid.addEventListener("platform-action", async (event) => {
  const { action_id, item_ids } = event.detail;
  if (action_id === "archive_selected") {
    await archiveProjects(item_ids);
  }
});
```
