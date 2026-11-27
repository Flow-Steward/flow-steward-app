# ADR 0020: OSS Single-Tenant Registry Boundary

## Status

Superseded by [ADR 0084](0084-multiple-accounts-in-one-installation.md)
(proposed 2026-11-27). Accepted — 2026-08-02. The Progress sections below remain
the implementation record of the steps ADR 0084 consolidates.

## Context

The first open-source release is a single-tenant deployment. Its canonical
`registry_fields` and `registry_data_contracts` tables are deployment-global:
they do not carry `account_id`, and their current keys are globally unique.
`registry_workflows` carries ownership metadata, but its generated
`workflow_id`/`workflow_key` remains globally unique and its dependent step,
group, For each, and runtime tables reference that global identifier. Those catalogs feed
published-block and skill input/output contracts, so simply
pretending that their rows are account-scoped would leave direct REST callers
able to read or mutate the same underlying definitions from dormant secondary
accounts.

The release-hardening decision explicitly defers full multi-account
tenant isolation. The OSS build must still fail closed when a secondary account is
created or selected through a dormant account API.

## Decision

1. The authoritative OSS account is `DEFAULT_ACCOUNT_ID`.
2. Deployment-global field and data-contract REST catalogs are reachable only
   from that account. Every list, detail, create, update, patch, toggle, and
   delete handler rejects a non-default account before it obtains the registry
   store.
3. Workflow creation, clone, and import-apply paths are likewise restricted to
   the authoritative account while workflow identity remains globally unique.
   Duplicate workflow identifiers fail explicitly with `409`; they are never
   silently renamed with a numeric suffix.
4. The current global tables and globally unique keys are **not** represented as
   multi-account-safe isolation. This is an intentional first-release OSS
   boundary, not a substitute for row ownership.
5. Internal system projection continues to populate the one deployment catalog
   under the authoritative account. A foreign interactive principal cannot use
   global-row semantics to claim or mutate it.
6. Full account-qualified registry storage is a separate multi-account
   migration and release gate.

## Multi-Account Migration Backlog

Before multi-account registry access can be enabled, a post-1.0 SQL migration
must:

- add non-null `account_id` ownership to `registry_fields` and
  `registry_data_contracts`, backfilling existing rows to the authoritative
  account;
- replace global key uniqueness with account-qualified uniqueness;
- replace global workflow identity and dependent workflow foreign-key contracts
  with account-qualified identity before enabling secondary-account workflow
  creation;
- make published-block and skill contract references enforce same-account
  ownership, not merely globally valid foreign keys;
- account-scope projection, changelog, trash, restore, retention, search, REST,
  MCP, CLI, and background reconciliation paths;
- prove two-account same-key create/read/update/toggle/delete/restore isolation
  against real PostgreSQL, including non-enumerating foreign-ID failures; and
- provide an explicit upgrade and rollback plan without clearing registry or
  customer data.

Until that backlog is complete, enabling non-default-account access to these
catalogs is an architectural violation.

## Progress

### 2026-11-26: explicit account on background work

Background units of work no longer fall back to `DEFAULT_ACCOUNT_ID` when
their account is missing. The shared contract is
`core/infrastructure/config/account_scope.py`: a job, event, message or
notification carries its account from the point it is created, and a consumer
that receives one without it logs `account_scope_missing` and fails that unit
of work.

- Domain event resume and redrive: a ledger row without `account_id` never
  matches an account-scoped lookup; the worker and HTTP resume paths stop
  injecting the default account.
- Notifications: the event's account, else its project's; neither refuses the
  dispatch before routes are read or the ledger is written.
- Jobs: a job takes its project's account; a project without one refuses the
  job.
- Extension runtime: record lookups by extension id or install id refuse a call
  without an account; extension actions and queries pass the caller's account.
- Provider responses applied by the worker and the MCP tool client's session
  bootstrap no longer use the default account.

In-flight rule: database rows already carry `account_id` (`NOT NULL`), so only
messages in flight when this rule arrived can lack it. Such a message is
accepted only when the installation has exactly one active account; with
several it is refused.

Deliberately kept defaults: single-account install bootstrap, CLI commands
that explicitly target the installation account (for example the worker
identity bootstrap's `--account-id` default), migrations and seeds, the
request scope normalisation for callers without a session, and the internal
registry projection of decision 5 (since step C5 the projection and the
workflow-knowledge sync work per account; see below). A request binds its
principal's account and a worker binds its job's; since step C5 a workflow read
or write that names no account outside a request or a job is refused instead of
using the default account (step C4b had used the default account there).

Open: the remaining defaults in request-time helpers (model selection
context, system settings normalisation, provider runtime configuration and
principal scope) still name the default account and are reviewed with the
account-scoped projection work.

### Step C4a — fields and data contracts (2026-11-26)

Migration `db/migrations/20261126170000_registry_account_ownership.sql` (and
the same change in `db/schema/`):

- `registry_fields.account_id` and `registry_data_contracts.account_id` are
  `uuid NOT NULL`, defaulting to the default account; every existing row is
  given to it.
- Uniqueness is per account: `UNIQUE (account_id, field_key)` and
  `PRIMARY KEY (account_id, contract_id)`. `field_id` stays the global
  surrogate that published block inputs and outputs reference.
- A field names a contract of its own account:
  `FOREIGN KEY (account_id, data_contract_id)` with
  `ON DELETE SET NULL (data_contract_id)`.
- Skill inputs and outputs carry `contract_account_id` next to `contract_id`
  (backfilled to the default account), a composite foreign key to the contract
  and a check that a contract reference always names its account.

Application rules:

- The store reads and writes fields and contracts of one account; a caller
  without an account (projection, seed, workflow import) acts for the default
  account.
- `/api/fields` and `/api/data-contracts` serve every account and work on the
  caller's account only; another account's row is a non-enumerating 404. The
  default-account guard is removed from these two catalogs only.
- Same-account references: a published block binds fields of its own account
  (a platform block without an account binds default-account fields), a skill
  names contracts of its own account (a platform skill names default-account
  contracts), and a step's output contract is resolved in the step's account
  and refused when it belongs to another. Contract item validation for skills
  checks the skill's account.
- Search returns the caller's own fields and contracts. Retention lists and
  purges expired contracts per account. The trash surface keeps working on the
  default account's contracts only, and its statements are now qualified by
  account so they never touch another account's row with the same id.

Evidence: `tests/unit/test_registry_account_ownership_migration.py`,
`tests/unit/test_single_tenant_registry_boundary.py` and
`tests/db/test_registry_account_ownership_postgres.py` (two accounts with the
same field key and contract id; create, read, update, toggle, delete; store and
database refuse cross-account references; the migration upgrades a database
with the previous global keys and runs again).

Rollback: the migration only adds columns and replaces constraints; it removes
no row. To return to the previous release, first remove or rename every field
and contract that is not owned by the default account and every duplicate key
(`SELECT field_key FROM registry_fields GROUP BY field_key HAVING count(*) > 1`,
likewise for `contract_id`), then, in one transaction: drop the three composite
foreign keys and the two owner checks, drop `contract_account_id` from skill
inputs and outputs, restore `PRIMARY KEY (contract_id)` and
`UNIQUE (field_key)` (constraint `registry_fields_field_key_key`), re-create the
single-column foreign keys with `ON DELETE SET NULL`, and drop both `account_id`
columns. Remove the ledger row of the migration from
`platform_schema_migrations` only together with that reverse change. While no
second account has written to these catalogs, the reverse change loses nothing.

### Step C4b — workflow identity (2026-11-26)

Migration `db/migrations/20261126180000_workflow_account_identity.sql` (and the
same change in `db/schema/`):

- `registry_workflows.account_id` is `uuid NOT NULL` (default: the default
  account). A row without an owner is given to the account of the extension install
  that projected it, otherwise to the default account. `UNIQUE (account_id,
  workflow_key)` and `UNIQUE (account_id, workflow_id)` replace the two global
  keys; `workflow_id` stays the generated copy of `workflow_key`.
- Groups, For each containers and steps carry `account_id NOT NULL` (backfilled
  from their workflow). Their keys are per account (`(account_id, workflow_id,
  group_id)`, `(account_id, workflow_id, for_each_id)`, `(account_id,
  owner_workflow_id, step_id)`), and every link between them and to the workflow is
  a composite foreign key, so a step can neither belong to nor sit in a Group of
  another account's workflow.
- Every other foreign key to a workflow follows the composite identity
  `(account_id, workflow_id)`: project bindings, schedules, entrypoints,
  reactions, runtime dependencies, notification routes, catalog secret bindings and
  approvals, the domain event ledger, event dispatches, extension invocation logs
  and job reaction events (`ON DELETE SET NULL (workflow_id)` for the logs). The
  duplicate `fk_scheduled_jobs_workflow` is gone. Tables that named a workflow
  without an account get one: `project_workflows.account_id` (a trigger fills it
  from the project; a second foreign key ties it to the project's account),
  `job_reaction_events.account_id` (the job's account), and
  `user_roles.workflow_account_id` (a trigger fills it from the assignment's
  project, so a workflow-scoped role names a workflow of that project's account).
  `scheduled_jobs.account_id` and `workflow_runtime_dependencies.account_id`
  become `NOT NULL`. Keys that named a workflow globally are per account:
  runtime dependencies, catalog secret bindings and approvals, and
  `approval_step_approvers` (new `account_id`, primary key `(account_id,
  workflow_id, step_id, user_id)`).
- A row that already names another account's workflow cannot be carried over
  without guessing: the migration stops, before any constraint changes, with the
  table name. Manual fix: move the row to the workflow's account or remove the
  reference, then run `database upgrade` again.
- Not given a foreign key, but keyed by account already: definition snapshots
  (`(account_id, workflow_registry_id, …)`), provider triggers (via the project
  binding), hook bindings, connection slot choices, kept test outputs, tabular
  snapshots. `registry_workflow_steps.workflow_id` (the child of an Invoke step)
  has no foreign key; its same-account check is step C4c.
- The earlier migration `20261120090000_role_permissions.sql` re-created
  `user_roles_workflow_id_fkey` on the global `registry_workflows(workflow_id)`,
  which a database built from this baseline no longer has, so a fresh installation
  failed at bootstrap. It was edited before 1.0 by owner decision (2026-11-26): it
  re-adds the global key only where that unique key exists (an older database,
  which this migration then converts) and the composite key otherwise. Its
  checksum in `db/released_migrations.json` is updated.

Application rules:

- The workflow store reads and writes one account: an explicit `account_id`,
  else the account bound to the current request or job
  (`core/application/registry/workflow_account.py`), else the default account.
  The authentication middleware and the `/api/workflows` router bind the caller's
  account for the request; a worker binds the job's account while it runs or
  resumes that job. The binding is a context variable, so it never reaches another
  request or job.
- The run pin, the owner lock of triggers and deletion, reaction scope checks,
  the consumer guard, runtime dependencies, approver assignments, the approval
  journal and route checks read the workflow of the owning account. Joins between
  workflows and the rows that name them compare the account as well as the id.
- `require_default_registry_account` no longer guards the workflow, step and
  Group routes; another account's workflow is simply not found. The trash
  surface keeps the guard and works on the default account's workflows only (the
  trash statements are qualified by account) until C5.
- The process-wide in-memory workflow registry holds the default account's
  workflows only, whatever account a reload runs under; other accounts' workflows
  are read from the store with their account.
- Platform objects: an agent, skill or published block without an account is a
  platform object, readable and usable by every account; an object with an account
  is readable and usable only by that account (the existing
  `row_in_account_scope` rule). Fields and data contracts of a platform skill or
  block remain the default account's (C4a). Workflows are never platform objects:
  every workflow has an owner.

Evidence: `tests/unit/test_workflow_account_identity_migration.py`,
`tests/unit/test_single_tenant_workflow_boundary.py` and
`tests/db/test_workflow_account_identity_postgres.py` (two accounts with the same
workflow key and the same step, Group and For each ids; read, update, toggle,
delete and the run pin stay in their account; definition rows, project bindings,
schedules and workflow-scoped roles naming another account's workflow are refused
by the database; approver assignments are kept per account; the migration upgrades
a database with the previous global identity, runs again, and stops on a binding
to another account's workflow).

Rollback: the migration adds columns, triggers and constraints and replaces keys;
it removes no row. To return to the previous release, first remove or rename every
workflow that is not owned by the default account and every duplicate key
(`SELECT workflow_key FROM registry_workflows GROUP BY workflow_key HAVING
count(*) > 1`), then, in one transaction: drop the two triggers and their
functions; drop `registry_workflows_account_id_workflow_id_key` and the two
per-account Group and For each `(account_id, workflow_id, id)` keys with
`CASCADE` (this drops every composite foreign key); drop the other per-account
keys, `approval_step_approvers_pkey`, `user_roles_workflow_account_check` and
`project_workflows_project_account_fkey`; drop the new columns
(`account_id` of Groups, For each containers, steps, project bindings, approvers
and job reaction events; `user_roles.workflow_account_id`); allow NULL again in
`registry_workflows.account_id`, `scheduled_jobs.account_id` and
`workflow_runtime_dependencies.account_id`; re-create the previous keys and the
single-column foreign keys exactly as the previous `db/schema/` defines them
(including `fk_scheduled_jobs_workflow` and `ON DELETE SET NULL` of
`registry_workflows_source_install_account_fkey`). Remove the ledger row of the
migration from `platform_schema_migrations` only together with that reverse
change. While no second account has written workflows, the reverse change loses
nothing.

### Step C4c — same-account references (2026-11-26)

Every reference a workflow's steps make stays in the workflow's account. Agents,
skills and published blocks without an account are platform objects, usable by
every account; workflows are never platform objects. Another account's object is
reported exactly like a missing one ("not found"), so a reference never tells an
account what another account owns.

Migration `db/migrations/20261126190000_registry_same_account_refs.sql` (and the
same change in `db/schema/001_schema_part_12.sql`):

- Trigger `trg_registry_workflow_steps_same_account` (function
  `registry_workflow_steps_same_account`) refuses, on insert and on update of the
  account or a reference, a step whose `agent_id`, `skill_id`, `block_key`,
  `override.published_block_key` or `override.block_key` names an object of
  another account.
- `registry_workflow_hook_bindings_project_account_fkey`: a hook binding names a
  project of its own account (`(project_id, account_id)` to `projects(id,
  account_id)`).
- `registry_workflow_hook_bindings_id_scope_key` (`UNIQUE (id, account_id,
  project_id)`) and `registry_workflow_hook_binding_revisions_scope_fkey`: a
  revision belongs to its binding's account and project.
- A row that already crosses accounts stops the migration before any change, with
  the table name. Manual fix: point the step at a platform object or one of its own
  account (or move the object), or move the binding or revision to the project it
  belongs to, then run `database upgrade` again. The migration changes no row.

Application rules:

- Saving a definition refuses another account's agent, skill or published block
  inside the write transaction (`core/application/registry/account_references.py`,
  called from the definition writer).
- The child of an Invoke step is a workflow of the parent's account. Readiness
  (enable, test, run) resolves it there; another account's row is an unknown
  workflow (`invoke.child_workflow_missing`). A draft may still name a child that
  does not exist yet, as before. Import resolves agents, skills, published blocks
  and child workflows in the importing account, so another account's object is a
  missing dependency, not an existing one. The lane-wait chain scan loads
  children of that account only, and the runtime refuses a resolved child whose
  account is not the run's.
- Readiness reports another account's agent, skill or published block as not
  found (`workflow.step_reference_not_found`). Readiness runs when the graph of a
  run is built, so a run is checked by the same rule.
- Hook targets were already checked in the binding's account by the binding
  service and pinned per job; the hook's storage child is now created, linked and
  bound with the binding's account bound, and its id is checked in that account.
- Database-level enforcement of the Invoke child is not feasible: a step may name
  a child that does not exist yet (a draft, an import that creates children later)
  or an installed extension's projected workflow, which has no registry row. The
  step's `workflow_id` is read in the step's account by identity, so it cannot
  name another account's workflow; every resolver reads it there.
- Code outside a request or a job: the scheduler binds each schedule's account
  (`scheduled_jobs.account_id` is NOT NULL) while it resolves and launches the
  run, so the workflow, its interface, defaults and secrets are that account's,
  for due runs and "run now" alike. Trash retention lists and purges expired
  workflows per account. The in-memory workflow registry keeps the default
  account's workflows only; under any other bound account it answers from the
  store with that account (`get_workflow`, `list_all`, `list_enabled`, project
  links), never from the cache.
- Reads that checked only a workflow id now name the account: the eval and probe
  cleanup tools (one account, the default one unless `--account-id` names
  another), the uninstall cleanup of projected workflow bindings (the install's
  account) and the dependents scan, the changelog filter that hides hook storage
  revisions (the revision's own account), and the entrypoint and provider trigger
  listings of a workflow.

Evidence: `tests/unit/test_registry_same_account_refs.py`,
`tests/unit/test_registry_same_account_refs_migration.py`,
`tests/unit/test_cleanup_eval_workflows.py`,
`tests/unit/test_cleanup_probe_workflows.py` and
`tests/db/test_registry_same_account_refs_postgres.py` (platform objects and an
account's own are usable; another account's agent, skill or block is refused by
the store and the database, also on update; an Invoke child that exists only in
another account is unknown; a hook binding cannot name another account's project
and its revisions stay in their binding's scope; a schedule of a second account
resolves its own workflow; the migration runs again and stops on a crossing step).

Rollback: the migration adds a trigger, a unique key and two foreign keys and
changes no row. To return to the previous release, in one transaction: drop the
trigger `trg_registry_workflow_steps_same_account` and the function
`registry_workflow_steps_same_account()`, drop
`registry_workflow_hook_binding_revisions_scope_fkey`, then
`registry_workflow_hook_bindings_id_scope_key` and
`registry_workflow_hook_bindings_project_account_fkey`. Remove the ledger row of
the migration from `platform_schema_migrations` only together with that reverse
change. The application changes need no data change to roll back.

### Step C5 — account-scoped registry operations (2026-11-27)

Migration `db/migrations/20261127090000_account_scoped_registry_ops.sql` (and the
same check in `db/schema/001_schema_part_12.sql`):

- Every history entry (`registry_changelog`) of a workflow, a field or a data
  contract names its account: `registry_changelog_account_keyed_owner_check`
  refuses one without `account_id`. Entries of agents, skills, tools, published
  blocks and datasets may stay without an account (a platform object's history).
- Older entries are given their account: the account their snapshot names when it
  is an existing account, otherwise (workflow, field, contract only) the
  installation account, the only owner these catalogs had before C4. The existing
  index `registry_changelog_account_entity_idx (account_id, entity_type,
  entity_id)` serves the per-account reads. The migration deletes no row and is a
  no-op on a fresh installation and on a second run.

Application rules:

- History by (account, entity id). Writes of workflows, fields and contracts record
  the owning account; other registry writes record the account their snapshot
  names (`core/application/registry/db_store_revisions_mixin.py`). Reads of a
  workflow's, field's or contract's history, one revision, a diff and the entry a
  restore applies name the account (the given one, else the bound one; none is
  refused), so account B never reads, compares or restores account A's history of
  the same key. The changelog REST routes and the workflow history routes pass the
  caller's account; a cross-entity changelog read of an account shows its own
  entries and those of platform objects. Lifecycle markers (trash, restore) carry
  the account of the row that changed. The hook-storage filter compares the
  revision's own account.
- Trash and restore per account. `require_default_registry_account` is removed:
  `/api/trash` lists every account's own trashed rows
  (`core/application/management/trash_account_scope.py`: the row's `account_id`,
  else its project's or workspace's account; rows with no owner at all, such as a
  platform agent, are listed for the installation account only). Trash, restore,
  plan execution and purge of workflows and data contracts address the caller's
  account (`_trash_key_params` takes the scope's account and refuses none), and a
  workflow's test-run files are read from the caller's account. Fields have no
  trash: a field delete is final and recorded in the field's account history.
  Retention was already per account (C4a/C4c).
- Search per account: `search_entities` returns the account's workflows and its own
  or platform agents, skills and tools; the workflow choices offered to a project
  are the account's own (the default-account-only fence of
  `list_workflow_choices` is removed).
- Projection per account (`core/application/registry/public_contract_projection.py`):
  a reconcile without an account reconciles every account that owns a source or a
  derived row, each with its account bound; the sources of an account are its own
  skills, agents, tools and workflows (platform skills, agents and tools are the
  installation account's), and derived fields and contracts are written only into
  that account. An account's agent paired with a platform skill gets that skill's
  fields in its own account; the platform skill's contract references stay the
  installation account's. Published blocks derived from an account's sources are
  owned by that account; because published block keys are still unique across the
  installation, a block key another account already owns is skipped and reported
  (`errors`), never taken over.
- Knowledge reconciliation per account: a sync binds its scope's account for its
  whole run (`workflow_knowledge_sync.py`), and a sync batch derives each scope
  from the changelog entry's own account before its snapshot.
- No silent default in library code. `workflow_owner()` and
  `registry_catalog_owner()` raise `WorkflowAccountMissingError` (a
  `MissingAccountScopeError`, so a `ValueError`) when no account is given or bound;
  `workflow_account_scope()` refuses an empty account; `registry_catalog_account()`
  refuses a request scope without an account (403). Documented installation paths
  bind the installation account explicitly with `installation_account_scope()`:
  database initialization and the canonical seed (`init_db.initialize_database`,
  `bootstrap_seed.apply_registry_seed`, `init_db.backfill_registry_changelog`), every
  platform CLI command (`ServiceRegistry.invoke`; the CLI scopes already name the
  installation account), and the process-wide workflow cache (unchanged since C4b).
  Unit tests that call the store directly opt in to the same binding with the
  `installation_account` fixture. A job or a resumed job binds its row's account
  (`account_scope_if_named`); a row without one binds nothing, so registry access
  inside it refuses rather than using the installation account. Platform objects keep
  their documented rule through `platform_catalog_owner()`: a skill or block
  without an account binds the installation account's fields and contracts.
- CLI: `sync_public_contract_projection` (`--account`, or `--all-accounts`),
  `sync_workflow_knowledge`, `cleanup_eval_workflows` and `cleanup_probe_workflows`
  take `--account` (`--account-id` stays accepted). Without it they work on the
  installation account and say so on stderr and in the JSON output
  (`account_id`, `account_named`). The cleanup library functions require the
  account they are given (`account_option.require_account`).
- In-memory registries: the workflow cache holds the installation account's
  workflows and is bypassed for any other bound account (C4b/C4c). The agent,
  skill and tool caches still hold every row: their ids are unique across the
  installation (a platform object or one account's object, never two accounts'
  rows with one id), and every use is checked by `row_in_account_scope`; keying
  them by account is not needed for isolation.

Evidence: `tests/unit/test_account_scoped_registry_ops_migration.py`,
`tests/unit/test_trash_lifecycle.py`, `tests/unit/test_installation_admin.py`,
`tests/unit/test_cleanup_eval_workflows.py`, `tests/unit/test_cleanup_probe_workflows.py`
and `tests/db/test_account_scoped_registry_ops_postgres.py` (two accounts with the
same workflow key, field key and contract id: history, one entry, diff and the
cross-entity changelog stay in their account; trash and restore through the trash
service touch only the caller's row and their markers carry its account; a
trashed workflow is restored in its own account only; the trash listing predicate
keeps one account's rows; search returns the caller's workflow; the projection
writes a derived field only into the source's account; an unbound read is
refused; the database refuses an unowned workflow, field or contract history
entry; the migration gives older entries their account and runs again).
The schema was checked with psql on a throwaway PostgreSQL 17 container
(`fs-postgres-extended:0.1.0-pgvector-0.8.5`): a fresh installation (schema,
pending migrations and canonical seed), a second run, and a 0.9.23 database
(initialized by the 0.9.23 code, with history entries that lack an account)
brought forward by the migration runner and run again: older workflow, field and
contract entries took the installation account, an entry naming an unknown account
stayed a platform entry, and the check refuses a new unowned entry.

Rollback: the migration adds one check and fills `account_id` of older history
entries; it removes no row. To return to the previous release, drop
`registry_changelog_account_keyed_owner_check` and remove the migration's ledger
row from `platform_schema_migrations` together with that change. The filled
accounts can stay: the previous release reads history by entity id and ignores
the column, except for guardrails, whose entries this migration does not touch.

Fences and defaults kept after C5, and why:

- Published block keys were unique across the installation; an account's derived
  block whose key another account owned (two accounts with the same callable
  workflow id) was not projected. The follow-up below makes them unique per owner.
- Platform objects (agents, skills, tools and published blocks without an account)
  remain installation-wide and are managed by the installation administrator
  (step C1); their fields and contracts are the installation account's.
- Trashed rows with no owner at all are listed and handled for the installation
  account only.
- Defaults outside the registry operations covered here stay as recorded above
  (request-time helpers) and in step C6 below: the extension install is
  installation-wide by design, and projected extension artifacts still name the
  installation account where a caller gives none.

### Step C6 — extensions per account (2026-11-27)

Decision. Three things that were one ("the extension is installed in an account")
are separated:

1. **Extension code is installed once per installation.** The package, its release,
   artifact snapshots, the managed extensions folder and the one
   `extension_installs` row live in the installation account
   (`core/application/auth/installation_admin.py`). Install, update, scan and
   uninstall stay installation-administrator actions
   (`require_global_extension_lifecycle_scope`): the supply chain, and the browser
   authority an extension is granted, are approved in one place. Enabling or
   disabling that install also stays there; it switches the code on or off for
   every account.
2. **Enablement and configuration are per account.** An administrator of any other
   account enables an installed extension "for this company" and disables it again;
   the row is `extension_account_enablements (account_id, extension_id)`, pointing at
   the installation's install. Its non-secret settings for that account live on the
   same row. Connections, credentials and secrets were already the account's own
   (`project_connections` and `secret_refs` are account-qualified) and stay so. The
   installation account itself needs no row: it owns the install, so its enablement
   is the install's own state, exactly as before.
3. **Catalogs are installation-level.** Where installable packages come from (the
   catalog sources, their tokens, update checks) is part of the supply chain and
   stays with the installation administrator. An account administrator sees what
   is installed and enables it; a new package is a request to the installation
   administrator.

Why this and not a code install per account: one copy of an extension's code per
installation keeps updates, health checks, the browser authority approval and the
uninstall plan in one place, and an account administrator could otherwise put code
nobody else approved on the shared runtime. Why not "installed means usable by
every account" (the old `visibility = 'global'`): one company's administrator would
silently give every company a new integration, its triggers and its UI. Enablement
is an explicit decision of each account.

Rules:

- Another account reaches an install only through an `enabled` row of its own, and
  only while the install itself is `active`. The install store's account listing
  (`include_global=True`), the runtime install lookup by install id, the search
  catalog and the project connection check read the same predicate. An account
  that did not enable an extension resolves it as absent ("not found"), never as
  "disabled by another account".
- Extension runtime lookups are by (account, extension): the caller's account is
  always given (C3), and for an account that does not own the install the runtime
  resolves the installation's code only after that account's enablement row says
  `enabled`. The filesystem fallback for an uncatalogued bundle serves the
  installation account only. The resolved record carries the caller's account and
  that account's own settings (`account_settings`).
- State, data and artifacts an extension keeps are keyed by the account or project
  that ran it (`extension_project_state`, `project_connections`,
  `extension_usage_events`, job artifacts), so one account's enablement never shows
  another's.
- Disabling for an account keeps its connections and settings, so enabling again
  restores them. Uninstalling the code removes every account's enablement (`ON
  DELETE CASCADE`), and the uninstall plan lists other accounts' connections as
  consumers, as it already did for a `global` install.
- `visibility = 'global'` keeps its old meaning (an install shared with every
  account by the installation administrator); nothing in Core sets it.

Implementation:

- Migration `db/migrations/20261127130000_extensions_per_account.sql` (and the same
  block at the end of `db/schema/001_schema_part_12.sql`): table
  `extension_account_enablements` (primary key `(account_id, extension_id)`, state
  `enabled`/`disabled`, `settings_jsonb` object), its foreign key `(install_id,
  extension_id)` to `extension_installs` (`ON UPDATE CASCADE ON DELETE CASCADE`, backed
  by the new unique index `ux_extension_installs_install_extension`) and the account
  foreign key `ON DELETE CASCADE`. The table starts empty; no row changes.
- `core/application/extensions/account_enablement.py`: the stores, the shared
  predicate (`install_reaches_account`, `install_serves_other_accounts`,
  `account_enablement_state`) and `ExtensionAccountEnablementService` (an
  administrator of the acting account; refused for the installation account, for an
  account with its own install, and for an install the installation turned off;
  settings refuse keys that name a credential).
- API: `GET /api/extensions/{id}/account-enablement`, `POST
  .../account-enablement/enable`, `POST .../account-enablement/disable`, `PUT
  .../account-enablement/settings`. `GET /api/extensions` and `GET
  /api/extensions/{id}` carry `account_enablement` for another account's view of an
  installation install (its administrators also see the ones not enabled yet);
  `GET /api/extensions/capabilities/archive-lifecycle` carries
  `can_manage_installs`.
- Runtime: `get_extension_account_access` (database) and
  `resolve_extension_runtime_record` (the gate above).
- UI: Extensions shows "Enable for this company" / "Disable" on such rows and on the
  extension's page; Install from file, catalog install and update, and Uninstall
  only to a caller that may manage installs.

Evidence: `tests/unit/test_extensions_per_account.py`,
`tests/unit/test_extensions_per_account_migration.py`,
`tests/db/test_extensions_per_account_postgres.py` (two accounts: enabled in A, not
in B; B does not list it, cannot resolve it by id or install id and never reads A's
settings; disabling keeps the settings; an install the installation turned off
cannot be enabled; uninstalling removes every enablement; the database refuses a row
naming another extension's install) and `assets/frontend/src/pages/extensions/api.test.ts`.
The schema was checked with psql on PostgreSQL 17: fresh baseline, the migration run
again, and an upgrade from the 0.9.23 baseline give the same table.

Rollback: in one transaction drop `extension_account_enablements` and the index
`ux_extension_installs_install_extension`, and remove the migration's ledger row from
`platform_schema_migrations` only together with that change. Other accounts then
lose their enablements and settings (their connections stay and are refused at run
time, as before C6); the installation account is unaffected.

Deliberately not in C6: registry projections of an extension's steps and blocks
are still built for the installation account (C5 made the registry's own
projection per account, not the projection of extension artifacts);
another account's runs of them are refused unless it enabled the extension. OAuth
client configurations stay owned by the install's account (their foreign key names
the install's account); a per-account OAuth client needs its own migration.

### Follow-up — published block keys per account (2026-11-27)

A published block key is unique within its owner: the primary key of
`registry_published_block_contracts` is `(owner_key, block_key)`, where `owner_key`
is a stored generated column (the block's account as text, empty for a platform
block). Inputs, outputs and workflow runtime dependencies name the block by
`(block_owner, block_key)`. A key is a platform key or an account key, never both
(trigger `trg_registry_published_block_key_scope`), and a step may name a key its
account can see: its own block or a platform block
(`registry_workflow_steps_same_account`). The store resolves a key within the
caller's (or the bound) account: its own block, else the platform block; another
account's block is never returned. The projection derives each account's blocks
whatever keys other accounts use; only a platform block keeps its key. Migration
`20261127150000_account_scoped_block_keys.sql` makes the same change, links older
child rows to the one block their key named, and unlinks (sets `block_key` to NULL
on) a runtime dependency that named another account's block; it deletes no row and
was checked on PostgreSQL 17 fresh, run again, and upgraded from the 0.9.23
baseline.

Rollback: the previous release needs installation-unique keys. With at most one
block per key, in one transaction drop the trigger and its function, the new
foreign keys and unique keys, the primary key and the `owner_key`/`block_owner`
columns, restore `PRIMARY KEY (block_key)`, `UNIQUE (block_key, field_id)` and the
`block_key` foreign keys, restore the C4c step function, and remove the migration's
ledger row from `platform_schema_migrations` together with that change. Blocks that
share a key across accounts must be removed (they are derived and are projected
again) before the primary key can be restored.

Next in this ADR's backlog: C7, an ADR that supersedes this one. Steps C5 and C6
were merged together into the integration branch on 2026-11-27.

## Consequences

- A dormant foreign-account session receives a controlled denial and never
  reaches the global registry store.
- Existing single-tenant workflows, projections, and globally referenced field
  IDs continue to work without a premature partial multi-tenant schema.
- The OSS release is honest about its boundary: safe for one authoritative
  account, not certified for multi-account tenant isolation.
- Future multi-tenant work must supersede this ADR rather than deleting the
  transport guard without replacing it with database-enforced ownership.
