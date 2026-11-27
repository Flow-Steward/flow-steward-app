# ADR 0084: Multiple accounts in one installation

- Status: Proposed
- Date: 2026-11-27
- Supersedes: [ADR 0020](0020-oss-single-tenant-registry-boundary.md) (the
  single-account registry boundary)
- Builds on: [ADR 0066](0066-global-roles-and-secret-provider-boundary.md),
  [ADR 0076](0076-permission-catalogue-and-role-permissions.md),
  [ADR 0082](0082-platform-modules-and-feature-entitlements.md),
  [ADR 0083](0083-external-sign-in.md)

The number is provisional. It was the first free number on `main` on the day of
writing and must be rechecked when this record is merged.

## Context

ADR 0020 made the first open-source release safe for one account: the field,
data-contract and workflow catalogs had installation-wide keys, so every route
that reached them refused any account other than the default one. Its backlog
named what had to happen before a second account could use them: account
ownership of the registry, account-qualified keys and foreign keys, same-account
references, account-scoped projection, history, trash, search, CLI and
reconciliation, a two-account proof on real PostgreSQL, and an upgrade and
rollback plan.

That backlog was carried out in steps C1 to C6 between 2026-11-26 and
2026-11-27, together with changes recorded in other ADRs:

| Step | What landed | Detailed record |
| --- | --- | --- |
| C1 | Installation administrator distinct from account administrator | ADR 0082 §9 |
| C2 | Account limit on creating and enabling accounts | ADR 0082 §9 |
| C3 | Background work carries its account; no silent default | ADR 0020, Progress "explicit account on background work" |
| C4a | Fields and data contracts owned per account | ADR 0020, Progress C4a |
| C4b | Account-qualified workflow identity and every foreign key to it | ADR 0020, Progress C4b |
| C4c | Same-account references from workflow steps and hook bindings | ADR 0020, Progress C4c |
| C5 | History, trash, search, projection, knowledge sync and CLI per account | ADR 0020, Progress C5 |
| C6 | Extension code per installation, enablement per account | ADR 0020, Progress C6 |
| — | Workspace, project and workflow access limits | ADR 0076 §8b |
| — | External sign-in pinned to the issuing account | ADR 0083 |
| — | Secret backends per account under an operator policy | ADR 0066 amendments of 2026-11-26 and 2026-11-27 |
| — | Module settings set only by the installation operator | ADR 0082 §11 |

The Progress sections of ADR 0020 hold each step's migration, application rules,
evidence and rollback. Steps C5 and C6 were developed on their own branches and
are merged into this line (2026-11-27); their records are in ADR 0020. This
record does not repeat the details. It states the resulting model in one place
and replaces ADR 0020's decision, which no longer describes the code.

## Decision

### 1. The account model

An **account** (`public.accounts`) is one company or organisation that shares an
installation with others. The word "tenant" is not used in Core.

- The **installation account** is the default account of ADR 0020
  (`DEFAULT_ACCOUNT_ID`). It exists on every installation, owns what belongs to
  the installation as a whole (section 2) and is the only account of a
  single-account installation, which behaves exactly as before.
- Every other account is a **company account**. It owns its own work and data
  and nothing installation-wide.
- An **installation administrator** is an administrator acting in the
  installation account. An **account administrator** is an administrator acting
  in any other account: full rights inside that account, none over
  installation-wide resources. The acting account is the request's account (the
  session's current account or the token's account). One predicate decides it,
  `core/application/auth/installation_admin.py`, and it fails closed: a principal
  that names no account is never an installation administrator (ADR 0082 §9).
- The **installation operator** is whoever runs the deployment (environment,
  `flow-steward config`, `install.sh`). Operator settings are outside every
  account's reach, including the installation administrator's interface: the
  secret backend policy (ADR 0066) and module settings (ADR 0082 §11).
- People are installation-wide rows (`users`) that belong to one or more accounts
  (`user_account_memberships`). Roles stay global (ADR 0066); a role says what a
  person may do in the account they act in. Access inside an account can be
  narrowed to workspaces, projects and workflows (section 3).

### 2. What is per account and what is installation-wide

**Installation-wide** (one copy, managed by the installation administrator or
the operator):

- Code: the Core image and its release line, platform modules and their settings,
  and the code of every extension (one install per extension; install, update,
  scan, uninstall and turning the install on or off).
- Supply: extension catalog sources, their tokens and update checks.
- Platform objects: agents, skills, tools and published blocks that have no
  account. Every account may read and use them; their fields and contracts are
  the installation account's.
- Accounts themselves: listing, creating, renaming, enabling and disabling them,
  within the account limit (section 4).
- Operations: workers and the process list (every account's jobs are visible
  only to the installation administrator), system monitoring, storage cleanup,
  the diagnostic report, Core update notices, the system email sender, and the
  global provider catalogue with its project-less secrets.
- The entitlement source and the operator's secret backend policy.

**Per account** (owned by one account, never readable or writable from another):

- Workspaces, projects, jobs, runs, schedules, approvals and their assigned
  approvers, notification routes and deliveries.
- Workflows with their steps, Groups and For each containers, project bindings,
  entrypoints, reactions, runtime dependencies and hook bindings. Workflows are
  never platform objects: every workflow has an owner.
- Fields and data contracts, and agents, skills and published blocks that name
  the account.
- History (`registry_changelog`), trash and restore, retention, search and the
  workflow choices offered to a project.
- Extensions: enablement of an installed extension "for this company", the
  account's non-secret extension settings, connections, extension state, usage
  and job artifacts. An account that did not enable an extension resolves it as
  absent.
- Secrets: catalog secrets and stored references, secret backend configurations
  and locators, all within what the operator policy allows that account.
- Sign-in: external identity links, sign-in options that name the account, and
  the sessions they issue. People limits (`scoped_role_users`) and grants.
- Projection and knowledge reconciliation run per account, each with its account
  bound.

### 3. Access inside an account

ADR 0076 §8b applies within one account: a grant names a workspace, a project or
a workflow of a project; the global role is the ceiling; the most specific grant
decides; a limited person sees nothing outside their grants; a limit applies only
in the account it was set in and is read only for places of that account; the
check fails closed. A workflow grant uses the account-qualified workflow identity
of section 5, so another account's workflow with the same id never names, keeps
or removes a grant.

### 4. Limits come from entitlements

- The number of active accounts is the numeric limit `accounts`, asked of the
  entitlement service for the installation account (ADR 0082 §9). No answer means
  one account: Core alone keeps a single-account installation, and no answer
  never means unlimited. The limit counts active accounts, the installation
  account included; disabled accounts do not count. Creating or enabling an
  account is refused with `account_limit_reached` when the limit is reached and
  with `account_limit_unavailable` when it cannot be read; counting and writing
  happen in one transaction under an advisory lock.
- Lowering the limit disables nothing. It only refuses the next create or enable.
- Features an account uses (for example access limits, assigned approvers,
  external sign-in methods, an external secret backend) are entitled per account
  (ADR 0082 §4). When an entitlement ends, configuration and data are kept, new
  operations are refused with a reason, and an access restriction the feature
  imposed keeps applying.
- Only the one declared entitlement source decides features and limits; no
  module can grant itself a feature or raise a limit (ADR 0082 §3b).

### 5. Isolation guarantees and how they are enforced

Isolation rests on two layers. Neither is enough alone.

**Database.** Ownership is a column and a key, not a convention:

- `account_id NOT NULL` on fields, data contracts, workflows and their child rows
  (steps, Groups, For each containers), project bindings, schedules and runtime
  dependencies (C4a, C4b).
- Keys are per account: `UNIQUE (account_id, field_key)`, `PRIMARY KEY
  (account_id, contract_id)`, `UNIQUE (account_id, workflow_key)`, `UNIQUE
  (account_id, workflow_id)` and the per-account keys of child rows.
- Every link is a composite foreign key that includes the account: a field to
  its contract, skill inputs and outputs to their contract, every table that
  names a workflow to `(account_id, workflow_id)`, a project binding to its
  project's account, a workflow-scoped role to the workflow of its project's
  account, a hook binding to a project of its own account and its revisions to
  their binding's scope, an extension enablement to the installation's install.
- A trigger refuses a step that names another account's agent, skill or
  published block (C4c). A check refuses a workflow, field or contract history
  entry without an account (C5).

**Application.**

- A request binds its principal's account and a worker binds its job's account
  for the duration of that job, in a context variable that never reaches another
  request or job. Library code that needs an account and has none raises
  `MissingAccountScopeError` instead of using the installation account (C3, C5).
  Documented installation paths (database initialisation, the canonical seed,
  platform CLI commands, the process-wide workflow cache) bind the installation
  account explicitly.
- Stores read and write one account. Another account's row is answered exactly
  like a missing one (a non-enumerating "not found"), in REST, readiness,
  import and runtime alike, so a reference never tells an account what another
  owns.
- `row_in_account_scope` decides platform objects (no account: everyone) and
  account objects (that account only).
- Installation-wide actions go through the installation administrator predicate,
  and Core's ports for them check it themselves whatever rule guards the route.
- External sessions are pinned to the account that issued them and cannot switch
  accounts; identity links are unique per account; the public option list shows
  an account's sign-in options only to a visitor who names that account
  (ADR 0083).
- Secret backends: an account may use only the backend kinds and configuration
  values the operator policy lists for it; with several accounts and no policy,
  company accounts may use no external backend; the reader refuses a
  configuration outside the policy whoever saved it (ADR 0066).

**Evidence.** Each step has unit tests and a PostgreSQL test with two accounts
holding the same keys (same workflow key, same step, Group and For each ids, same
field key and contract id): create, read, update, toggle, delete, history, diff,
trash, restore, search, projection and extension resolution stay in their
account, and the database refuses cross-account rows. The test files are listed
in each Progress section of ADR 0020. Migrations were checked on a fresh
installation, on a second run, and on a 0.9.23 database brought forward.

### 6. What ADR 0020's decisions become

| ADR 0020 decision | Now |
| --- | --- |
| 1. The authoritative account is `DEFAULT_ACCOUNT_ID` | It is the installation account; it owns installation-wide resources, not every catalog |
| 2. Field and contract catalogs only from that account | Every account uses its own (C4a) |
| 3. Workflow creation only in that account | Every account creates its own workflows; duplicate keys are refused per account with `409`, never renamed (C4b) |
| 4. Global tables are not multi-account safe | Replaced by section 5 |
| 5. Internal projection under the authoritative account | Projection runs per account (C5); platform objects stay installation-wide |
| 6. Account-qualified storage is a separate migration | Done in C4a to C6 |

The guard `require_default_registry_account` is gone. A route that still serves
only the installation account does so through the installation administrator
predicate, because the resource it serves is installation-wide.

## Known gaps

These are deliberate and recorded so that nobody reads more into the guarantees
than section 5 states:

- **Published block keys are unique across the installation.** A block derived
  from one account's workflow is skipped and reported when another account already
  owns that key. Account-qualified block keys need a later migration.
- **In-memory registries.** The workflow cache holds only the installation
  account's workflows and is bypassed for every other account. The agent, skill
  and tool caches hold every row; their ids are unique across the installation and
  each use is checked by `row_in_account_scope`.
- **Request-time helpers** (model selection context, system settings
  normalisation, provider runtime configuration, principal scope normalisation)
  still fill in the installation account for a caller without one, and a request
  scope without a session still gets it.
- **Extensions.** Registry projections of an extension's steps and blocks are
  built for the installation account; another account's runs of them are refused
  unless it enabled the extension. OAuth client configurations of an extension
  stay owned by the install's account. Projected extension artifacts and
  connection validation still name the installation account where a caller gives
  none.
- **Invoke children** are not checked by a database key, because a draft or an
  import may name a child that does not exist yet; every resolver reads the child
  in the step's account.
- **Trashed rows with no owner** (for example a platform agent) are handled for
  the installation account only.
- **Roles are global.** A person who belongs to several accounts has the same
  role in each, and cannot use external sign-in at all (ADR 0083 person rule).
  User names are unique across the installation.
- **No row-level security** in PostgreSQL. Isolation is the keys above plus the
  application checks; a direct database session sees every account.
- **Shared runtime.** Workers, queues, object storage and the database serve every
  account. Object keys carry no account prefix; access is decided through the
  owning job or project row. There are no per-account resource quotas, and one
  account's load affects the others.
- **Scale is unmeasured.** No load test covers many accounts or many people.

## Upgrade and rollback

**Upgrade.** The changes arrive as SQL migrations applied by `flow-steward
database upgrade` (and the same change in `db/schema/` for a fresh installation):
`20261126150000_external_sign_in.sql`,
`20261126170000_registry_account_ownership.sql`,
`20261126180000_workflow_account_identity.sql`,
`20261126190000_registry_same_account_refs.sql`,
`20261126200000_secret_backends.sql`,
`20261126210000_multilevel_scoped_roles.sql`,
`20261127090000_account_scoped_registry_ops.sql` and
`20261127130000_extensions_per_account.sql`. Every existing row is given to the
installation account, or to the account that already owns its parent. A row that
already crosses accounts cannot be carried over without guessing: the migration
stops before any change and names the table; the administrator moves or removes
the row and runs the upgrade again. No migration deletes a row, and each runs
again without change. A single-account installation sees no behaviour change:
the account limit defaults to one and every resource stays the installation
account's.

**Rollback.** Each step has a reverse change recorded in its ADR 0020 Progress
section, applied in the reverse order of the list above, each in one transaction,
with its ledger row removed from `platform_schema_migrations` only together with
that change. The reverse changes remove columns, keys and tables, so they lose
nothing only while no company account has written data. With company accounts in
use, rolling back first means exporting and removing (or renaming) every
workflow, field and contract that is not the installation account's and every
key that would collide, as each Progress section describes; extension
enablements, settings and history accounts of company accounts are lost by the
reverse change.

## Open questions

- Account-scoped roles: a role per account would let a person work in several
  accounts with different rights and use external sign-in there. Not decided.
- Account-qualified published block keys and per-account extension OAuth client
  configurations: both need a migration; not scheduled.
- Whether request-time helpers should refuse a missing account instead of filling
  in the installation account, once every caller names one.
- Whether PostgreSQL row-level security should back the composite keys as a second
  database guard, and at what cost.
- Per-account resource quotas (jobs, storage, workers) and whether a busy account
  may be isolated onto its own workers.
- Workflow template catalog sources were not reviewed in C1 to C6: whether they
  stay installation-wide like extension catalog sources.
- Deprovisioning: revoking a person's access when the identity service removes
  them is a module's job (ADR 0083); Core offers no directory synchronisation.
- Load evidence for many accounts and many people before this record is accepted.

## Consequences

- One installation can serve several companies, each with its own workflows,
  catalogs, history, extensions enablement, secrets and sign-in, while code,
  supply and operations stay in one place under one administrator.
- A single-account installation is unchanged.
- New code that stores account data must give it `account_id`, key it per
  account, link it with composite foreign keys, read it with the bound account and
  refuse a missing account. A new installation-wide resource must be guarded by
  the installation administrator predicate. Reviews check both.
- ADR 0020 is superseded. Its Progress sections stay as the implementation record
  of C3 to C6.
