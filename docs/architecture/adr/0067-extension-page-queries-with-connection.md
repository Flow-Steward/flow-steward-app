# ADR 0067: Extension page queries with a project connection

- Status: Accepted
- Date: 2026-11-13
- Builds on: [ADR 0057](0057-secret-catalog.md), [ADR 0065](0065-composite-extension-actions.md),
  [ADR 0066](0066-global-roles-and-secret-provider-boundary.md),
  [ADR 0023](0023-extension-owned-oauth-boundary.md)

## Context

An extension page shows grids, stats rows and markdown fed by the extension's
queries. A query gets no project connection: the host passes it no target, and
runtime resources give a connection's config and secret references, never its
values. An extension whose data lives in its own remote service, behind a token
stored on a project connection, cannot show that data on a page. It has to build
the page from a connection form and action buttons whose answers appear in the
action-result panel.

Actions already receive a connection: the person picks it, the browser sends
`target.connection_id`, and the host checks project access, account, project,
extension, install, enabled state, connection type and the approval of the
connection's configuration (ADR 0057) before it reads any secret. Resource
queries for picker fields already run a query with a connection resolved the
same way and placed in the envelope's root `target`.

## Decision

A query may declare that it runs with a project connection. The host resolves
that connection exactly as it resolves an action target and gives it to the
query in the envelope.

### Declaration

- A query in `ui/queries/queries.yaml` declares
  `connection: {connection_type_ids: [<type>, ...]}`. The types are this
  extension's own connection types (a non-empty list of unique ids, plural even
  for one type, no other key). Such a query always runs with one connection;
  there is no optional form. A query that also works without a connection is an
  ordinary query; an extension that needs both declares two queries.
- A connection query has `handler.mode: extension`, is not named in
  `external_effects`, and requires `runtime.compatibility.platform_min: 1.7.0`.
  An older host would ignore the key and run the query without a connection;
  the minimum makes it refuse the bundle instead.
- A page that uses connection queries declares
  `connection: {connection_type_ids: [...]}`. Every connection query the page
  references must accept every type of the page. A connection query may not
  appear on a page without it, and a page with it is project-scoped. One
  connection per page; no component-level connection. A query without
  `connection` may still appear on such a page and runs without a target.
- A page may declare `refresh_seconds` (10..3600) to reload its queries while it
  is visible. Only a page with at least one query may declare it.
- The compiler refuses a bundle that breaks any of these rules, with the codes
  `query_connection_invalid`, `query_connection_type_unknown`,
  `query_connection_handler_mode`, `query_connection_external_effect`,
  `query_connection_host_minimum`, `page_connection_invalid`,
  `page_connection_requires_project`, `page_connection_type_mismatch`,
  `page_connection_missing` and `page_refresh_invalid`.

### Request and envelope

- `POST /api/extensions/{extension_id}/query/{query_id}` accepts
  `target: {connection_id}`. The browser sends nothing else about the
  connection. A connection query without it is refused with 400
  `query_connection_required`; a target with any other key, or a target sent to
  a query without `connection`, is refused with 400 `query_target_invalid`.
- The host puts the resolved connection (identity, type, config, secret
  references and secret values, or the extension-owned OAuth token) at the
  envelope's root `target.connection`, the place resource queries already use,
  and adds `project_id`, `connection_id` and `extension_install_id` to the
  runtime context.
- The SDK reads the connection of an action and of a query through one
  accessor, `connection_from_payload`, so an extension has one way to read it.

### Authority and checks

- The connection is resolved by the same host function as an action target
  (`resolve_operation_connection`): project access, account, project,
  extension, install, enabled state and type are checked, and a catalog secret
  is read only while the configuration matches its approval. An unapproved
  configuration fails with 409 and code `secret_configuration_unapproved`
  before any secret is read; actions and resource queries now carry the same
  code.
- Every query follows the project rule of actions: an operator, viewer or
  service account needs project context, and every project the request names
  must exist and pass the project access check.
- Running any extension query (`POST .../query/...`) needs the operator or
  admin role, or the token scopes of an action write (`projects.write`,
  `projects`, `command`, `admin:all`), as running an action does. A viewer
  cannot run a query; connection-query page data is operator and admin only.
- The result passes the reflected-secret guard (the same budgets and check as
  an action result) before it is logged or returned.
- A connection query cannot change the platform: a response with commands,
  events or project-state updates fails with
  `query_connection_effects_forbidden` and nothing is applied.
- A connection query runs at most 30 seconds, below any `timeout_policy`.
- Remote runtime installs cannot run connection queries in this release
  (`query_connection_runtime_unsupported`); a remote action does not forward a
  target either.
- Each connection query writes one extension invocation row with its project
  and connection.

### Page

- The host, not the extension, renders the connection selector of a page,
  beside the project selector, built from the existing design-system select,
  alert, badge and secret-approval controls. One connection of the page types
  is selected automatically; with several, nothing runs until the person picks
  one, and the choice is kept in the URL (`extension_connection`). When none
  exists the page says so; when the chosen one needs approval the page shows the
  existing approval control.
- Connection queries, query buttons, the page's actions and grid row and bulk
  actions use the selected connection; a row value never overrides it.
- A page reloads its queries every `refresh_seconds` while the browser tab is
  visible and no load is running, and stops after a failed load until the person
  presses Refresh.
- URL page parameters: query-string keys `p.<name>` of an extension page URL
  (for example `extension_page=run_detail&p.run_id=42`) are merged into the
  `params` of every query on that page. They override the component's declared
  params and are overridden by the component's own interactive state (paging,
  filters). Names are lowercase identifiers, values are bounded text; they are
  untrusted input the extension validates. They are dropped when the person
  switches to another page.

### Versions

- Host compatibility version 1.7.0. The wire contract stays `extension_host_v2`.
  SDK `flowsteward-extension-sdk` 0.5.0.

### Not in this release

- A per-principal or per-install server-side limiter for queries; the 30-second
  ceiling and the page limits apply instead.
- Rendering the grid `link` column as a link; markdown links cover it.
- Refusing commands, events and project-state updates from queries without a
  connection.

## Consequences

- Extension pages can show data from the extension's own service with grids,
  stats rows and markdown, including links to a detail page through URL page
  parameters.
- A query that holds a connection is a read by declaration only. The host cannot
  stop extension code from writing to its own service with the token; that is
  the trust already given to installed extension code (`egress` is declared,
  not enforced), and the declaration, the manifest standard and the invocation
  log are the controls. Effects belong to actions, which carry the
  external-effect ledger.
- Page queries share the install's runtime slots with workflow steps for up to
  30 seconds each; refresh is limited in the page and stops after a failure. An
  extension returns a domain failure (exit 2) for an unreachable service so the
  circuit breaker is not tripped by service outages.
- Operators can now load the data of every extension page they reach; before,
  every query write needed the admin role.
- Resource queries for picker fields already pass the reflected-secret guard
  (0.9.17); actions and resource queries now also report an unapproved
  configuration as `secret_configuration_unapproved`.
- Not decided here: returning files from a page, install-level settings,
  connections for protected extension workflows, connection queries on remote
  runtimes, OAuth scope requirements on queries, and server-side rate limits.
