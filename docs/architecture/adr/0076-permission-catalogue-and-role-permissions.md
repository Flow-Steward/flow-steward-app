# ADR 0076 Permission catalogue and role permissions in the database

Status: Proposed

Date: 2026-11-19

Related: [ADR 0066](0066-global-roles-and-secret-provider-boundary.md) (changes
one item), [ADR 0075](0075-project-user-sections.md),
[ADR 0057](0057-secret-catalog.md), [ADR 0047](0047-inbound-mcp-oauth.md) (agent access presets),
[ADR 0020](0020-oss-single-tenant-registry-boundary.md),
[ADR 0012](0012-post-1.0-sql-migrations.md)

## Context

The roles `admin`, `operator` and `viewer` are global (ADR 0066). Who has which
role is stored in the database: `roles` and `user_roles`. What a role can do is
described in code, in three places:

| Where | What it describes |
|---|---|
| `core/application/auth/operator_work.py`, `auth/config.py`, `auth_http_runtime.py` | Which writes operator may make; which routes are admin-only; by default a person's write requires admin |
| `assets/frontend/src/lib/auth/capabilities.ts` (`CAPABILITY_ROLES`) | What the interface shows to each role |
| `_required_http_scopes`, `core/entrypoints/mcp/server_tool_scopes.py`, `agent_access_presets.py` | Which scopes a token needs for a route and an MCP tool; AI agent access presets |

The three descriptions diverge. The ADR 0075 review found such cases: the
"Users & access" menu item shown to operator although the route is admin-only;
an agent preset choice that operator does not understand and may get wrong. The
rule "a role has the same permissions in the UI, API and MCP" now rests on
discipline, not on how the system is built.

The product needs:

- the same role permissions in every interface, checked by a test;
- a simple AI agent connection for operator and viewer: one button, the
  permissions of their own role, no choice of preset or lifetime;
- groundwork for a private edition: additional roles (for example, a workspace
  administrator), role assignment at workspace or project level, a permission
  editor. The open-source version will not have this until the community asks
  for it, but the core must not need rework when it arrives.

ADR 0066 forbids adding "unused policy registries, membership tables, switches
and user-facing controls in anticipation". This ADR changes that item for two
things: the "role → permissions" link and an optional scope for a role
assignment. Both are used right away: the first by every permission check, the
second by the check that assignments in open source are global.

## Decision

### 1. Permission catalogue in code

The module `core/application/auth/permissions.py` is the only list of
permissions. A permission is a key of the form `<area>.<action>` with a
description and flags:

- keys match the existing token scope taxonomy (`jobs.read`, `jobs.write`,
  `projects.read`, `workflows.read`, `registry.read`, `secrets:bind` and so
  on); missing ones are added to the same taxonomy: for example,
  `workflows.run`, `approvals.decide`, `secrets.value.write`, `users.manage`,
  `settings.manage`;
- the "exact match only" flag is kept for permissions such as `secrets:bind`
  and `secrets:write` (ADR 0057): neither `admin:all` nor the `*` pattern
  grants them;
- permissions stay in code because each one is tied to a specific check. A
  permission without a check is meaningless; a check without a permission is an
  error, and a test catches it (section 6).

### 2. Role permissions in the database

New table `role_permissions(role_id, permission_key)`. The `roles` table gets
`is_system boolean` and `description`.

- **System roles** `admin`, `operator`, `viewer` are filled by a migration and
  reproduce today's matrix with no change in behaviour. In open source their
  permissions cannot be changed through the UI or the API: this protects admin
  from accidentally locking themselves out.
- **Admin** gets every permission in the catalogue implicitly, not as a list. A
  new permission is available to admin at once, without a migration.
- **Operator and viewer** get an explicit list. By default a new permission
  belongs only to admin; to give it to operator, a migration must record that
  explicitly. The safe default is "denied".
- A key in the table that is not in the catalogue is ignored and logged; it
  does not affect permissions.
- The schema changes in `db/schema` and in a separate idempotent migration in
  `db/migrations`, with `baseline_schema_fingerprint` updated (ADR 0012,
  2026-11-01 amendment).

### 3. One effective-permissions function

`effective_permissions(principal)` computes the permission set once per request
and puts it in `ScopeContext`:

| Who | Effective permissions |
|---|---|
| A person in the browser | The permissions of their roles from `role_permissions` |
| A person's personal token (API, MCP, OAuth client) | The owner's role permissions ∩ the token's scopes. A token is never wider than its owner |
| Service account | As now: its grants and scopes (ADR 0066, item 4) |
| An extension at runtime | As now: its runtime grants |

Permissions say **what** may be done ("decide approvals"). Resource checks —
account, workspace, project, workflow, ownership, the token restrictions
`allowed_project_ids` / `allowed_workflow_ids` — say **on what**. They do not
change and are not replaced by permissions.

### 4. Every interface takes permissions from one place

- **HTTP.** A "route → permission" table replaces the `operator_work.py` list,
  the list of admin-only routes and the "a write defaults to admin" rule. A
  write with no permission in the table is rejected: closed by default.
- **MCP.** `server_tool_scopes.py` refers to the same keys. A tool still
  proxies the call to the API with the caller's token, so there is one final
  check. Discovery follows the role too (2026-11-27): after the token-scope
  filter, `tools/list` drops every tool whose permission the caller's role does
  not hold in the account the token is bound to (ADR 0085). The MCP server reads
  those keys from `/api/auth/introspect` with the caller's own token, which now
  returns `permissions` for a person and a person's token, and caches them per
  token for under a minute. This is presentation only: if the lookup fails the
  scope-only list is shown and the API still refuses what the role does not
  allow. Service accounts, which are not role based, keep the scope-only list;
  a person limited to some projects or workflows sees the tools of their role,
  and the API decides which places each call reaches.
- **Interface.** Bootstrap returns the current user's `permissions`.
  `CAPABILITY_ROLES` is removed; the visibility of menu items, buttons and tabs
  is computed from permissions.

### 5. AI agent connection without choices

- Operator and viewer connect an agent with one button. The token gets the
  permissions of their role: the "same as me" preset. The server sets the
  lifetime, and the person gets a reminder before it expires. They have no
  choice of preset, permissions or lifetime.
- After connecting, they see the address, a "Copy" button and one short
  instruction for their client.
- Admin keeps the advanced settings (full authoring access, lifetime,
  restriction by projects and workflows), collapsed by default.

### 6. Test matrix

One test walks every HTTP route from OpenAPI and every MCP tool and checks
that:

- every write route and every tool has a permission in the table;
- for each system role, the interface, HTTP and MCP give the same "allowed /
  denied" answer;
- until `operator_work.py` is removed, the matrix from the database matches its
  list (a transition with no change in behaviour).

### 7. One system role per user

System roles are a ladder: viewer < operator < admin (`_ROLE_LEVELS` in
`core/application/auth/principal.py`). Checks take the highest role, so an
admin with operator and viewer also ticked has the same permissions as a plain
admin. The checkboxes in the user form hide this and make people guess.

- The user form offers a choice of one role out of three, with a short
  description of each role's permissions, instead of checkboxes.
- CLI `--role` accepts one value.
- The server stores one system role per user; if several arrive, the highest
  is kept.
- The migration leaves users with several system roles the highest one.
  Permissions do not change: the highest role is already the one in effect.
- `user_roles` stays a list of assignments. Several assignments are needed for
  different scopes (item 8) and for roles outside the ladder in the private
  edition, not for several system roles in one scope.

### 8. Role assignment scope (schema now, enabling it — private edition)

A role assignment can apply to the whole installation or to a part of it.
`user_roles` gets optional `workspace_id`, `project_id`, `workflow_id`. The more
fields are filled, the narrower the scope:

| Assignment | What it grants |
|---|---|
| No scope | The role across the whole installation (as everyone has now) |
| `workspace_id` | The role in every project of the workspace |
| `project_id` | The role in one project, in all its workflows |
| `project_id` + `workflow_id` | The role in one workflow of one project — exactly an ADR 0075 section |

Examples:

- operator only in project 1 — one assignment `(operator, project 1)`;
- operator in projects 1 and 2 — two assignments;
- operator in all projects — one assignment with no scope;
- operator in project 1 only for workflows X and Y — two assignments
  `(operator, project 1, X)` and `(operator, project 1, Y)`;
- different roles in different places: `(operator, project 1)` and
  `(viewer, project 2)`.

The item 7 rule applies to each scope separately: in one scope a user has one
system role. If the global role and the role in a scope differ, the higher one
applies.

Checks are not rewritten. Narrowing by projects and workflows already works for
tokens through `allowed_project_ids` / `allowed_workflow_ids` in
`ScopeContext`: lists, section menus, jobs, `require_project_access`,
`require_workflow_access`. User assignments fill the same fields. The one new
thing: when roles in different scopes differ, effective permissions are
computed for the request's resource (project, workflow), not as one set per
request; `effective_permissions` takes the resource.

In open source the service rejects a scoped assignment, and only the "no scope"
option applies — the single team trust zone of ADR 0066. The private edition
enables scopes, roles such as "workspace administrator", an assignment editor
and change auditing on top of these tables, without changing the catalogue,
the checks or the interfaces. If the community asks for project restrictions in
open source too, that is one decision — lift the ban — with no core rework.

#### 8a. Narrowing project scope (2026-11-26)

This amends item 8 for project-scoped assignments written through the scoped
roles port that Core offers to platform modules (ADR 0082 §3a). For these
assignments "the higher one applies" is replaced by narrowing:

- The global role is the ceiling. A scoped assignment never grants more than it.
- A person is limited when `scoped_role_users` holds a row for them. A limited
  person reaches only the projects they hold a project-scoped assignment for; in
  each of them the effective role is the lower of the global role and the scoped
  role. A person without that row is not limited, and their global role applies in
  every project as before.
- Only `operator` and `viewer` can be held for a project, and a person whose
  global role is `admin` cannot be limited.
- The limit row is separate from the assignments, so deleting a project — even
  the last one a person holds — never turns a limited person into an unrestricted
  one.
- A stored limit keeps applying when the feature that writes limits is no longer
  entitled (ADR 0082 §4). Only writes stop; the check and the interface still read
  the limit.
- The check fails closed: when the limit cannot be read, the request is answered
  with 503, as when role permissions cannot be read.

The middleware fills `allowed_project_ids` and per-project permissions
(`ScopeContext.project_permissions`) from the limit; a personal token acts within
the intersection of its own project list and its owner's limit. The app
bootstrap sends `auth.user.project_permissions` (a map of project id to sorted
permission keys, or `null` without a limit) so the interface disables actions per
project; it is sent whether or not the feature is entitled. Workspace- and
workflow-scoped grants are added by 8b.

#### 8b. Workspaces, projects and workflows (2026-11-26)

A company's departments (accounting, marketing, sales, IT, business lines) are
workspaces. 8a extends to three levels; its other rules (global role as ceiling,
durable limit row, `operator` / `viewer` only, admins never limited, fail closed,
limits kept after the entitlement ends) stand.

- A grant names one place: a workspace (`workspace_id`), a project (`project_id`)
  or a workflow of a project (`project_id` + `workflow_id`). The port takes
  `{scope: "workspace" | "project" | "workflow", id, role}`; a workflow grant also
  names its project, and the workflow must be a workflow of the account linked to
  that project. Every place must belong to the caller's account.
- A workspace grant covers every live project of the workspace and all their
  workflows. The projects are read on every request, so a project created later is
  covered.
- A workflow grant makes its project reachable, but in that project only the
  granted workflows (and their runs, approvals and search results) are reachable,
  unless a project or workspace grant also covers the project. The project itself
  holds read access (the viewer role, capped by the global role); acting on a
  workflow is checked against the workflow's grant.
- The effective role in a place is the most specific grant covering it — workflow,
  then project, then workspace — capped by the global role. A more specific grant
  may narrow as well as widen within the ceiling (a project viewer grant in an
  operator workspace makes the person a viewer there).
- A workspace without a covering grant, and every project in it, is invisible:
  lists, search, the dashboard and navigation. A workspace holding a reachable
  project is visible with read access.
- A limit applies only in the account it was set in (`scoped_role_users.account_id`):
  the person's grants are read only for places of that account.

The middleware fills `allowed_workspace_ids`, `allowed_project_ids` and, on the
scope, `project_permissions`, `workspace_permissions`, `workflow_permissions`
(granted workflows) and `project_workflow_ids` (projects reached only through
workflow grants). Guards: `require_workspace_access`, `require_project_permission`
(inheriting from the workspace; with an optional workflow) and
`require_workflow_permission`. The run query predicate, the dashboard, search, a
project's workflow list and the operator sections apply the workflow-only rule.
The bootstrap sends `workspace_permissions`, `workflow_permissions` and
`project_workflow_ids` beside `project_permissions`, each `null` without a limit.
Migration `20261126210000_multilevel_scoped_roles.sql` indexes the workspace and
workflow foreign keys of `user_roles`. A workflow grant uses the account-qualified
workflow identity of ADR 0020 step C4b: the trigger fills `workflow_account_id`
from the project, every read joins `registry_workflows` on
(`workflow_account_id`, `workflow_id`), and the workflow index has the same two
columns, so another account's workflow with the same id never names, keeps or
removes a grant.

Project-level listings in a project reached only through workflow grants
(2026-11-27). Each listing is decided by what its items belong to:

- An item that belongs to a workflow (runs, the queue, schedules, notification
  routes and deliveries, entrypoints, workflow secret approvals, project reports)
  is kept only when its workflow is granted. Acting on it is checked in its own
  workflow: changing, pausing, deleting and "run now" of a schedule load the
  schedule and check the permission in the schedule's workflow
  (`require_workflow_permission`: workflow, then project, then workspace), and a
  schedule of a workflow out of reach is answered as not found.
- Project-wide configuration that any workflow may use (connections, their secret
  approval status, the provider inventory built from connections, and extension
  project state) is shown read-only, and only the part a granted workflow needs:
  a connection one of its steps is bound to, a connection of an extension install
  one of its steps or Invokes calls, a connection of the install a granted
  projected workflow (`extension.<install>.<id>`) belongs to, or a connection that
  maps an event to a granted workflow. The needs are read from
  `workflow_runtime_dependencies`. Connections are returned as the viewer summary
  whatever the global role, and every write stays refused by the project-level
  permission (the project itself holds read access only). Everything else is
  hidden; one item asked for by id is answered as not found.
- An item that belongs to no workflow and is not configuration (direct external
  effects of extension page actions, `source_type = 'api_action'`) is out of reach,
  like a run without a workflow.
- Fail closed: when the dependency read fails, no configuration is shown.

### Change to ADR 0066

The item "Do not add unused policy registries, membership tables, configuration
switches or user-facing controls in anticipation of that work" no longer
forbids `role_permissions`, the system role flag and the optional role
assignment scope from this ADR. The rest of ADR 0066 stands: roles are global,
there is one team trust zone, and open source has no per-project assignments
and no user-facing permission editors.

## Compatibility

- Role behaviour does not change: the migration carries over today's matrix,
  and the section 6 test compares it with the old list.
- Tokens keep their scopes and patterns (`jobs:*`, `*:read`, `admin:all`). An
  existing personal token whose scopes are wider than its owner's permissions
  acts within the owner's permissions after the transition. This is a
  tightening, not a widening; it is listed in the release notes.
- Service accounts and extension runtime grants do not change.
- 0.9.x installations get the tables through a migration; `user_roles` data is
  kept.

## Rejected options

- **Keep permissions only in code.** The divergence between UI, API and MCP
  would remain, and the private edition would need core rework.
- **Store the permission catalogue itself in the database.** A key without a
  check in code means nothing; this creates permissions that look configured
  but do not work.
- **An external policy engine (OPA, Casbin).** Excessive for three roles and
  global assignment; it can be plugged in later behind the same
  `effective_permissions` function.
- **Enable per-project assignments in open source now.** It contradicts the
  single trust zone of ADR 0066 and the community has not asked for it; the
  schema and checks are ready, enabling them is a separate decision.

## Implementation plan

1. The permission catalogue and the `effective_permissions` function (still
   reading permissions from code); the test matrix on current behaviour.
2. Tables and the migration with system roles; the function reads permissions
   from the database; the test comparing with the old list.
3. HTTP routes on the "route → permission" table; removal of `operator_work.py`
   and the list of admin-only routes.
4. MCP tools on the same keys.
5. `permissions` in bootstrap; the interface without `CAPABILITY_ROLES`.
6. One-button agent connection for operator and viewer.
7. One system role per user: form, CLI, server, migration.
8. Scope columns in `user_roles` (`workspace_id`, `project_id`, `workflow_id`),
   `effective_permissions` with a resource, rejection of scoped assignments in
   open source.

## Definition of done

- Code has one list of permissions; `CAPABILITY_ROLES` and `operator_work.py`
  are removed.
- The test matrix passes for all routes and MCP tools; a write route without a
  permission fails the test.
- For each system role, the UI, API and MCP give the same answer for the same
  actions.
- Operator and viewer connect an AI agent with one button and no choices; the
  token is not wider than the owner's permissions.
- Upgrading a 0.9.x installation keeps users and their roles; role behaviour
  does not change.
- The user form and the CLI assign one system role; users with several system
  roles keep the highest after the migration.

## Existing owners

- [Roles, tokens and middleware](../../../core/application/auth/auth_http_runtime.py).
- [Operator writes](../../../core/application/auth/operator_work.py).
- [Admin-only routes](../../../core/application/auth/config.py).
- [Scope matching](../../../core/application/auth/scope_contracts.py).
- [MCP tool scopes](../../../core/entrypoints/mcp/server_tool_scopes.py).
- [Agent access presets](../../../core/application/auth/agent_access_presets.py).
- [Permissions in the interface](../../../assets/frontend/src/lib/auth/capabilities.ts).
- Tables `roles`, `user_roles`: `db/schema/001_schema_part_04.sql`.
