# ADR 0082: Platform modules, extension page links and feature entitlements

- Status: Proposed
- Date: 2026-11-25
- Builds on: [ADR 0013](0013-extension-package-identity-and-dependencies.md),
  [ADR 0016](0016-host-mediated-extension-data-migrations.md),
  [ADR 0020](0020-oss-single-tenant-registry-boundary.md),
  [ADR 0028](0028-web-extension-installer.md),
  [ADR 0066](0066-global-roles-and-secret-provider-boundary.md),
  [ADR 0067](0067-extension-page-queries-with-connection.md),
  [ADR 0074](0074-automatic-release-upgrade.md)
- Amends: ADR 0028 (the 2026-10-01 statement that there is no publisher tier)

The number is provisional. It was the first free number on `main` on the day of
writing and must be rechecked when this record is merged.

## Context

Core already loads optional modules that live outside this repository:
`load_optional_private_modules` imports the packages named in
`FS_OPTIONAL_PRIVATE_BOOTSTRAP_MODULES` and calls their `register` entrypoint.
Such a module can register pages, API routes and its own schema. Today it reaches
a deployment only by being installed into a custom image, and its React pages are
compiled together with the Core frontend. Every combination of modules is therefore
a separate image that someone has to build, test and deliver.

Extensions take the opposite position. They are installed and updated from the
catalog, with versions, compatibility checks and SHA-256 verification, but their
contract is deliberately narrow: workflow steps, connections, declared pages with
queries (ADR 0067) and host-run JSON migrations (ADR 0016). They cannot add
navigation, platform routes or tables, and ADR 0028 records that there is no
publisher tier.

Work that extends the platform itself, rather than adding workflow steps, needs
the first kind of access with the second kind of delivery. Without a decision, each of those features either forks the
delivery path again or pushes platform authority into the extension contract.

A code survey on 2026-11-25 found that the existing seam is narrower
than its name suggests:

- Only the API application and the extension installer call it. Workers, the
  control plane and the MCP server never load a module, so a module cannot take
  part in workflow execution.
- On Compact every child process receives an allowlisted environment that does not
  contain `FS_OPTIONAL_PRIVATE_BOOTSTRAP_MODULES`. The seam does not work there.
- On Scalable each role runs its own image with its own dependency lock and its
  own slice of Core source. A module must import only what the role it loads in
  actually ships.
- There is no CLI hook and no way for a module to add an MCP tool; the MCP server
  only forwards to the API.
- Feature availability is one in-process `EntitlementService` with no account
  argument and no limits. Nothing stops a module from writing into that
  service's dictionary or from altering a Core table. This record rules out both.

ADR 0066 forbids adding policy registries, membership tables or switches in
anticipation of future work. This record does not lift that rule. It names the
seams and requires each one to arrive with its first real consumer.

## Decision

### 1. Two package classes, one catalog

1. An **extension** keeps its current contract, trust level and installer. Its
   rights still come from manifest `runtime_grants`. Publication in the catalog
   never widens them.
2. A **platform module** is a package trusted to extend the platform itself. Core
   loads it in-process at start through the existing optional bootstrap seam.
3. A package is a platform module only when its release description is signed by
   a publisher key that the Core release itself trusts. This is the one publisher
   tier and it applies to platform modules alone. The amended sentence in ADR 0028
   continues to hold for extensions. Publisher signing is planned and not yet
   verified by Core: the prototype loader only compares each file with the digest
   in the prepared list.
4. Both classes share catalog identity (ADR 0013), version and compatibility
   metadata, dependency declarations and the update view. They differ in installer:
   the extension installer handles extensions, the release lifecycle of ADR 0074
   handles platform modules.

### 2. What a platform module may do

A platform module may, through declared registration points only:

- register pages and settings resources in the page registry, with
  `required_permission` and `required_entitlement`;
- register API routes, with a required feature declared once per router;
- take part in the worker and control-plane roles through the role entrypoints
  `register_worker` and `register_control`, in addition to `register` for the
  API application;
- own tables in its own PostgreSQL schema, `mod_<module id>`, created and changed
  by host-run SQL migrations. They are tracked per module in
  `public.platform_module_migrations` and never in `platform_schema_migrations`,
  whose content must keep matching `db/released_migrations.json` exactly
  (ADR 0012).

Further registration points are added together with their first consumer.

MCP tools and CLI commands contributed by a module are not part of this decision.
Core has no registration point for either. A feature that is refused by the API
is refused over MCP as a consequence, because the MCP server forwards to the API.

A platform module must not:

- change, drop or add columns to Core tables; only Core migrations under
  `db/migrations/` do that;
- replace, wrap or patch Core functions, classes or routes outside a declared
  registration point;
- widen access that Core refused: ownership checks, token limits, runtime grants
  and reference authorization stay in force.

If a module needs behaviour that no registration point offers, the point is added
to Core first, with its default behaviour and a test, and the module then uses it.

**Trust model.** A platform module is trusted code running inside Core's processes.
Nothing in this record isolates it: in-process code can reach any Core object, and
a migration can read or write any row its database role may touch. The rules above
are to be enforced by who may publish a module (the publisher signature, once Core
verifies it) and by review of the module before release. The checks Core runs at install and at start exist to
catch mistakes and to stop one module from passing as another. They are not a
security boundary, and no documentation may describe them as one. The enforcing
boundary for row access would be a database role limited to the module's schema.

**API routes.** A module's routes live under `/api/modules/<module id>/`. Core binds
every registration to the identity it loaded the module under, so a module cannot
register a rule or a page resource in another module's tree. A rule names a
permission from Core's catalogue, and every kind of caller must hold it: a signed-in
user, a token, a service account and an internal principal alike. Whether a rule is
administrator-only follows from the permission and is not the module's choice. A
write in a module's tree that matches no rule is refused.

**Migrations.** A module migration is plain SQL that creates and changes objects in
the module's own schema. Functions, triggers, rules, procedural blocks, dynamic SQL,
session settings and file or server access are refused. The schema name is derived
from the module id without collisions and the schema carries its owner's id, so a
later module cannot adopt another module's schema. Module migrations only go
forward.

### 3. Seams that Core owns

Each seam below lives in Core, works without any module installed, and is
introduced together with its first consumer.

| Seam | Behaviour with no module installed |
| --- | --- |
| Feature entitlement service: answers whether a feature is entitled and what a numeric limit is, for a given account | No feature is entitled; limits are the instance defaults |
| Route rules for module API routes: a required permission and feature | No module routes; a write under `/api/modules/` is refused |
| Page registry with host renderers for module pages (section 6) | Core's own pages only |
| Module schema and its host-run migrations | No module schema |

Further seams are added together with their first consumer.

The entitlement service is the only place that decides whether a feature is
available. It grows out of the existing `EntitlementService` in
`core/application/settings/platform_contracts.py`, which gains an account argument
and numeric limits. Page visibility, API, workers and workflow execution all ask
it. A module does not replace the service and does not write into its state: it
registers an entitlement source, and the service stays the decider. The source's
input is a set of features and limits that applies either to the whole
installation or to one account. Where that input comes from is outside Core.

The per-install `extension_entitlements` table keeps its present meaning, the
billing state of one extension installation, and is not used for platform
features.

### 3a. What Core carries for a feature a module delivers

Decided by the owner on 2026-11-25. A feature that cuts across Core's own screens
and checks is split in a fixed way:

| Part | Lives in | Without the module |
| --- | --- | --- |
| Tables that Core's own checks read | Core schema and Core migrations | Present and empty; Core writes them only through a port that it offers to modules |
| The check itself | Core | Answers as today |
| Controls inside Core's screens | Core frontend, shown only when the feature is entitled | Hidden |
| Rules, write operations and the API that changes those tables | The module | Absent: no route accepts a change |
| Stand-alone administration screens and module-private data | The module, as registered pages and its own schema | Absent |

The port validates every write against the caller's account (the resource and every
referenced user must belong to it) and refuses a write that would widen access. A
module never writes a Core table directly; a module database role limited to its
own schema stays possible.

This narrows one rule of ADR 0066, which forbids adding membership tables,
switches or user-facing controls in anticipation of future work. Such a part may
now enter Core when the module feature that uses it is built in the same change
and a reference module exercises it in Core's tests. Parts with no consumer are
still not added.

A module-owned schema (section 2) is for data that only the module reads. Data
that a Core check depends on belongs to Core's schema, so that the check, its
migration and its tests stay together.

### 3b. Amendment 2026-11-26: one entitlement source and the module ports

**One designated entitlement source.** A deployment has at most one entitlement
source. A module declares the role in its descriptor, `"provides":
["entitlement_source"]`; the descriptor digest covers the declaration, so it is
pinned with the module. `provides` is optional and lists only capabilities Core
knows. Activating a set in which two modules declare it is refused
(`release_module_set_rejected`). Should a list still name two, the second one to
load in a role is skipped and listed as failed. While Core's loader runs a module's
hook it tells the entitlement service which module registers and what it declared:
a module that did not declare the capability, a module without a platform module
identity (one listed by `FS_OPTIONAL_PRIVATE_BOOTSTRAP_MODULES`) and any second
source are refused with `EntitlementSourceRefusedError`, which fails that module's
loading. After loading every role seals the service, so no later code can register
a source. No module can therefore grant itself a feature or raise a limit: the
features and limits Core decides on come from the one declared source.

**Installation id.** Core resolves the installation id once per role, from the
same projection that monitoring uses (`projected_installation_id`), and hands it to
modules read-only: `container.installation_id` in the app role and
`context["installation_id"]` in the worker and control hooks. It is `None` where
the deployment projects none.

**Worker and control ports.** Both background roles now give their hooks
`entitlement_service`, the role's own decider (sealed after loading), and `db`, a
factory that returns the role's connection store on the role's own database DSN
(`None` without one), the same kind of object as the app role's
`service_factories["db"]`. The store is created on first call, so a hook must not
query the database while it registers.

**Installation administrator on a module route.** A module rule may set
`requires_installation_admin=True`. Its permission must then be admin-only, so the
existing admin check applies to every caller, and the authentication middleware
also asks an installation-administrator predicate (`installation_admin_fn`), which
falls back to the admin role when none is passed. A registered page shown only to an
installation administrator declares `required_permission="installation_admin"`;
the authorization service decides it with its `installation_admin_predicate`,
which falls back to the admin role as well, so the bootstrap and the page registry
hide such a page from an administrator of another account. A page whose `required_permission` the authorization service does not know
(for example `"admin"`) would be hidden from everybody, so registering it is
refused with an error that names the known values. The app composition passes
Core's installation-administrator predicate, `is_installation_admin` (§9), as both
predicates; the route refusal carries the code `installation_admin_required`.

### 4. Installed, entitled, enabled

A feature is usable only when all three hold, and each is reported separately:

1. **Installed** — the module that carries it is loaded in every process role
   that executes it.
2. **Entitled** — the entitlement service allows it for the account.
3. **Enabled** — an administrator with the required permission configured and
   turned it on.

When an entitlement ends, configuration and data are kept, new operations that
need the feature are refused with a stated reason, and access never becomes wider
than it was: an access restriction that the feature imposed keeps applying.

### 5. Installation and update of platform modules

1. A deployment runs one version of each platform module for all its accounts.
2. The deployment manager, not the application administrator and not the
   extension installer, downloads the module, verifies its digest (and the
   publisher signature, once Core verifies one), stages it and hands the release
   lifecycle a list of prepared modules.
3. The prepared set is a file, `modules.json`, in the runtime directory that the
   app, installer, worker and control-plane roles can read and only the operator
   can write, on both profiles. An environment variable cannot carry it, because
   Compact does not pass one to its child processes. Each role reads the same
   file at start. No file means plain Core.
4. At start a role checks every listed file against the digests in the list,
   refuses a path that leaves the module root, a `.pth` file, an interpreter or
   architecture mismatch, and a bundled dependency whose version differs from
   the one in the role's image. A prepared module that fails any of these checks
   is skipped before any of its paths is used, and modules that conflict with
   each other are all skipped (see the 2026-11-26 amendment); the operator CLI
   and the database step still refuse such a set as a whole. A verified module
   that then fails to load (its import fails, or its role entrypoint raises, for
   example because its migrations were not applied) is skipped: Core rolls back everything it
   registered (routes, route rules, pages, feature endpoints, entitlement
   sources), logs the error with its id and version, lists it as failed with that
   error, and the role starts without it, so its features stay off. A failed
   rollback stops the process. Module paths are appended after Core's, so
   Core's pinned dependencies always win. A module may bundle a dependency
   that a role's image already has at the same version, because roles on
   Scalable ship different dependency sets; the image's copy is then used. A
   top-level package name may come from one module only, unless two modules bundle
   the same dependency with identical content. A process that loaded modules does
   not write bytecode next to them.
5. Activation restarts the affected process roles through the ADR 0074 journal,
   maintenance, readiness and backup rules. The target module set is pinned in
   the journal before any change, and a module-only change goes through the same
   journaled path as a release change. A module that changes its own schema
   follows the same migration rules as Core.
6. The order of a module-only change is: maintenance and drain, backup, stop the
   roles, write the list, database upgrade (Core migrations, then module
   migrations), start the roles, health check, reopen. The list is written only
   while the roles are stopped, so a role of the running release can never start
   on a set meant for another one, and no role starts on a module schema older
   than its code.
7. When the roles fail to start or to become healthy after a module-only change,
   or start without a module of the new set (see the 2026-11-26 amendment), the
   operation activates the previous set again and restarts the roles, and
   maintenance stays on for the operator to lift. It does not do so when the new
   set brought a module migration: the operation then stops and names the two
   choices, repeat the operation or restore the backup taken at its start.
8. Core's image is the same for every deployment. A module is never a reason to
   build a deployment-specific image.
9. The catalog shows installed and available module versions and compatibility
   with the running Core. A module whose declared Core range excludes the target
   release blocks that release until resolved.

### 6. Module pages without a frontend rebuild

A platform module's page is rendered by a renderer that ships in the Core bundle,
selected by a declared `control_surface.kind`. The first supported kind is
`resource_admin`, backed by `RegisteredSettingsResourceAdminPage`. The router
resolves such a page from the declaration and does not require a `component_key`
compiled by Vite.

The second kind is `resource_report`: a read-only, paged table with declared
columns (`text`, `datetime`, `badge`, `link`) and filters (`text`, `date`,
`select`) over one `list_path`, with no write route and no `item_path`. The
browser sends each filter by its key plus `limit` and `offset`. A link column
leads only to an in-app path such as `/jobs/{job_id}`; Core checks this when the
page is registered and again when a row is drawn. Labels carry an optional
`label_key` that Core resolves through the UI catalogue, as it does page titles.

The third kind is `status_document`: one status object and one text document.
The page reads `status_path` (GET, a JSON object, or the object under an
optional `status_key`) and shows the declared `fields` (`text`, `datetime`,
`badge`) as label and value rows. Below them a text box, which a file picker
can fill (`accept`, at most `max_bytes`, default 64 KiB), sends
`{"<upload_field>": "<text>"}` (field `document` by default) to `upload_path`
with PUT, after which the status is read again. Both paths are called verbatim
and lie in the module's API tree; the page's `required_permission`, including
`installation_admin`, decides who sees it, and the module checks the write again.

Core registers its own pages, loads the modules and then seals the page
registries; a registration after that point is refused. A page key or path that
belongs to Core or to another module cannot be replaced. A resource path of a
module's page lies in that module's API tree.

Module-specific React code is not part of this decision. If a real page cannot be
expressed with host renderers, loading prebuilt, versioned module assets through
the host registry is decided in a follow-up record after a prototype.

### 7. What extensions gain

1. An extension may declare a link to one of its own pages in a placement that
   the host defines. The first placement is a dedicated section of a project.
2. The manifest gives a local page identifier, a label and the placement. The
   host builds the route and the navigation entry, per request, for the current
   user, account and project, and checks access again on a direct request.
3. Disabling or removing the installation removes the link and the handler.
4. Extensions get no free-form navigation, no platform routes, no SQL and no
   tables. A declarative, host-applied collection for extension data is a
   separate decision under ADR 0016.

### 8. Amendment 2026-11-26: one failing module never stops Core

Owner decision. Platform modules are trusted code, but one of them must never take
Core down. Each role loads modules one by one and isolates each failure:

1. A module that fails its activation checks (digests, interpreter or machine,
   layout, a name or dependency conflict), its import, or its role entrypoint
   (including a port that an older image does not offer) is skipped. Its routes,
   rules, pages, hooks and entitlement sources are rolled back or never
   registered, and the role starts with the other modules. Two modules that
   conflict with each other are both skipped, so the result does not depend on the
   order of the list.
2. The failure is logged with the module id, version and role; the traceback goes
   to the log only. The record an administrator sees carries the exception type and
   the first line of its message, with detected credentials and connection URIs
   masked and the length bounded.
3. Fail closed: the module's features stay hidden and refused, and the role's
   entitlement service answers "unknown" instead of a definite "not entitled", so
   nothing that deletes data on "not entitled" runs while a module is missing.
4. Administrators see the app role's failures on the monitoring page as one
   degraded `platform_modules` readiness check, one
   `<id> <version>: Failed to load: <reason>` entry per module, and a "Checks to review" badge in the status strip of
   every monitoring tab. `/health/deep` carries the same records; the public
   `/health/ready` probe reports `degraded: true` and only the number of failed
   modules. A degraded app stays ready (HTTP 200): it serves everything except the
   failed modules. Worker and control roles log their own failures and carry them
   in every heartbeat they publish; the monitoring page adds a current (fresh, not
   superseded) worker or control report's failures to the same check as
   `<id> <version> (<role> role): Failed to load: <reason>`, one entry per role and
   module however many replicas report it. `/health/deep` and `/health/ready` still
   describe the app only.
5. What still stops a role: a module list that cannot be read (it names no module
   to skip) and a rollback or degraded marking that fails (the role would run half
   registered).
6. Because a module-only change no longer fails the roles' start, the module change
   operation (§5 item 7) reads which modules the restarted roles skipped and treats
   a new failure as a failed change. Each role that had a module list writes what it
   loaded and skipped to `/dev/shm/flow-steward-platform-module-status-<role>.json` in its
   own container once its modules are registered; Docker gives every container start
   a fresh `/dev/shm`, so the file always belongs to the current start, and no shared
   or persisted path is needed. The operator CLI reads the files with
   `docker compose exec -T <service> python -m core.entrypoints.cli.ops.platform_modules load-status`
   (`fs-app`, `fs-worker` and `control-plane` on Scalable, `compact-runtime` with all
   three roles on Compact). It reads them once before it stops the roles, so a
   failure that was already there (same role, module and version) does not count,
   and again after the restart, waiting up to 60 seconds for a role that has not
   reported yet. A new failure fails the change with `release_module_load_failed`,
   naming the module, version, role and short reason, and rolls back under the same
   rules as a failed restart: the previous set is activated and the roles restart,
   unless the new set brought a module migration, in which case the operation stops
   with the two choices. Only a reported failure counts: a service that cannot be
   asked or a role that never reports is logged, and the check passes for it.

   Still open: a release change (a new Core release with a module set) restarts the
   roles through the installer handoff and does not read the load status, and the
   app's `/health/deep` does not include the worker and control failures that the
   monitoring page shows.

### 9. Amendment 2026-11-26: installation administrator and the account limit

**Installation administrator.** Roles are global (ADR 0066), so the admin role
says only that a person administers the account they act in. Resources that
belong to the whole installation need more: an **installation administrator** is
an administrator acting in the installation account, which is the default account
of ADR 0020. An administrator acting in any other account is an account
administrator, with full rights inside that account and none over
installation-wide resources. The acting account is the request's account (the
session's current account or the token's account), so an installation
administrator who switched into another account switches back first. The rule is
one predicate, `core/application/auth/installation_admin.py`, and Core uses it for:
the system email sender, extension install, update, uninstall, enable, disable
and scan, the global field and data-contract catalogues (ADR 0020 fence), listing,
creating and disabling accounts, Core update notices, system monitoring, storage
cleanup and the diagnostic report, stopping a worker process, applying a
watchdog scan, and the global provider catalogue with its project-less secrets.
A refusal over HTTP is 403 with code `installation_admin_required`; the extension
lifecycle and system email keep their earlier error codes. With one account
nothing changes.

The predicate fails closed: a principal that names no account never acts in the
installation account, so it is never an installation administrator. Only a
`ScopeContext` built for a caller without a session (the CLI, the worker, the
single-account bootstrap; ADR 0020's deliberately kept defaults) and Core's own
`system_actor` get the installation account filled in. A session from external
sign-in is pinned to the account that issued it and cannot switch into the
installation account (ADR 0083).

A module route requires an installation administrator with
`requires_installation_admin=True` and a module page with
`required_permission="installation_admin"` (§3b); the app composition passes
`is_installation_admin` as both the middleware's `installation_admin_fn` and the
authorization service's `installation_admin_predicate`. Core's ports that act on
installation-wide resources also check it themselves: `container.accounts` (`core/application/auth/account_lifecycle.py`) takes the
module route's authenticated principal as the keyword `caller` and refuses anyone
who is not an installation administrator, whatever rule guards the route. It
offers `list_accounts` (with a `user_count` per account), `create_account`,
`rename_account`, `enable_account` and `disable_account`. Creating or enabling
passes the account limit below exactly as Core's own routes do, and the port
writes the `auth` audit event itself, in the same transaction as the change,
naming the `created_by` or `actor` the module passes. A `resource_admin`
resource may set `allow_delete: false` so its page offers no delete action.

**Account limit.** Creating an account asks the entitlement service for the
numeric limit `accounts`, read as `limit(<account of the person creating>,
"accounts")`; only an installation administrator may create accounts, so this is
the installation account. `None` (no source speaks for it) means the instance
default of **one** account, the installation account itself: Core alone keeps
the single-account boundary of ADR 0020, and `None` never means unlimited. A
limit `N` allows at most `N` **active** accounts, the installation account
included. Disabled accounts do not count, but a new account is refused while `N`
accounts are active even when it would be created disabled, so no later enable
step can pass the limit. A refusal is HTTP 409 with code `account_limit_reached`
and the limit and active count in `details`. When the limit or the active count
cannot be read, creation is refused with 503 and code `account_limit_unavailable`;
a failing source makes the service answer `0`, which refuses as well. Enabling
a disabled account takes a place and is refused the same way. Counting the
active accounts and inserting or enabling one is one decision: it runs in one
transaction holding a PostgreSQL advisory lock
(`account_lifecycle_transaction()` on the account store), so two requests racing
for the last place are serialized and the second is refused.

**Worker processes.** Workers serve every account. `GET /api/processes` shows the
installation administrator every account's jobs; anyone else sees only the jobs
and project leases of the account they act in, the workers serving them, and the
subprocesses that can only belong to those jobs. Worker processes and suspected
orphans carry no account and are left out for them.

### 10. Amendment 2026-11-27: module dependencies, "any of" page entitlements, effective field values

Owner decision: an add-on may be sold separately from the module it extends, so
Core needs three small additions.

1. **Module dependencies.** A module descriptor (`module.json`, each `modules.json`
   entry, and the builder's `module-source.json`) may declare
   `"requires": ["<module id>", ...]`; an item may add a minimum version,
   `"<module id>>=1.2"` (numeric parts, compared part by part). The loader
   activates modules in dependency order: topologically, module id breaking ties.
   Without a failure list (operator CLI, database step) a set in which a required
   module is missing or older than required, or that contains a cycle, is refused
   as a whole with `PlatformModuleDependencyError`. A starting role (§8) skips the
   dependent module instead and lists it as failed with the reason
   `requires <id> (...)`; this also applies when the required module failed its
   checks, its import or its entrypoint. The module store refuses such a set at
   install time with `release_module_set_rejected` before anything is written. The
   key is part of the descriptor digest only when present, so the digests of
   modules that declare no dependency are unchanged.
2. **"Any of" page entitlements.** A page's `required_entitlement` is one feature
   name or a list of names; with a list the page is visible when the account holds
   any of them. The page registry filters bootstrap pages and navigation this way,
   and the browser applies the same rule to the bootstrap's feature flags. The
   single-string form is unchanged.
3. **Effective field values in `visible_when`.** A `resource_admin` field's
   `visible_when` condition reads the referenced field at the value the form will
   send: a choice fixed to its single allowed option (`hidden_when_single` or the
   read-only single option, from declared options or an `options_path`) counts as
   that option, and a stored value the known options do not offer counts as no
   choice.

### 11. Amendment 2026-11-27: module settings set by the installation operator

A module sometimes needs a deployment-level setting -- a development-only switch,
the path of a policy file -- that the installation operator sets, never an account
administrator. Core's host settings are a fixed list from the environment manifest,
and Compact passes a fixed allowlist to its child processes, so a module-specific
variable set on the container never reached the app, worker or control process.

1. **Declaration.** A module descriptor may declare
   `"settings": [{"name", "description", "default"?, "allowed_values"?, "secret"?}]`.
   Every name matches `FS_MODULE_[A-Z0-9][A-Z0-9_]*`: the `FS_MODULE_` prefix is
   reserved for module settings, so a module can never name a Core setting. The
   prefix continues with the declaring module's id in upper snake case (each run of
   `.` or `-` becomes `_`) and an underscore, so a module names only settings of its
   own: module `acme.ledger` declares `FS_MODULE_ACME_LEDGER_*`, `acme.sign-in`
   declares `FS_MODULE_ACME_SIGN_IN_*`. An operator value is one line of at most
   4096 characters, as a default is.
   `description` is required text; `default` is text (empty when absent);
   `allowed_values` is a non-empty list of distinct values that contains the
   default; `secret` may only be `false` -- this version carries no secrets. A
   malformed declaration is refused by the loader and the module store
   (`release_module_artifact_invalid`). Like `provides` and `requires`, the key is in
   the descriptor digest only when present, so the digests of modules that declare
   no setting are unchanged.
2. **The active set records them.** The `modules.json` entry carries the
   declaration; a set in which two modules declare the same name is refused
   (`release_module_set_rejected` in the store, a list error in the loader).
   `PlatformModuleStore.declared_settings()` reports the settings of the active set;
   on Compact the in-image command `platform_modules settings` reports them, run in
   a disposable container of the installed runtime image like the other module
   commands.
3. **Operator surface.** `flow-steward config set FS_MODULE_NAME=VALUE` and, on
   Compact, `./install.sh --setting FS_MODULE_NAME=VALUE` accept a module setting
   only while an active module declares it, and only a value from its
   `allowed_values` when it has some; any other `FS_MODULE_*` name is refused like an
   unknown setting (`configuration_setting_invalid`, `installer_setting_invalid`).
   An empty value resets a setting to the module's default; resetting a stored value
   no active module declares any more is accepted, so a leftover can be cleared.
   Accepted values are stored with the other host settings
   (`DeploymentConfig.operator_settings`), so pending changes, `config apply`,
   rollback and `stack.toml` treat both alike. `flow-steward config show` lists every
   declared module setting with its value or default, its allowed values and the
   module that declared it, and a stored value no active module declares.
4. **Delivery to the roles.** `config apply` and `./install.sh --setting` render all
   stored module settings into one runtime variable, `FS_PLATFORM_MODULE_SETTINGS`
   (`base64:` followed by the base64 of a JSON object of name to value, so no quote,
   `#` or space of a value can be misread by Compose's dotenv reader; empty when none
   is set). A role that cannot read the variable logs a warning and starts every
   module setting on its default. Compose passes it to the
   role containers on Scalable and to the Compact runtime container, whose supervisor
   gives it to the app, worker and control processes only -- the roles that load
   platform modules. Each role, while it activates its modules
   (`activate_prepared_platform_modules` into its own `sys.path`), publishes the
   names its loaded modules declare as ordinary process variables: the operator's
   value, else a value the process already carries (a development run), else the
   declared default. A name no loaded module declares is never published, and no
   Core allowlist names a module setting. The roles read the new values on their
   next start, which `config apply` performs.

### 12. Amendment 2026-11-27: background jobs and ports in the control role, row actions

A module that keeps its own data in step with Core (a periodic sync, an expiry
sweep) needs to run work in the background, write the same audit events as the
app, use Core's secret codec and revoke what external sign-in issued. Three
additions give it that without new Core tables.

1. **Periodic jobs.** The control hook's context carries `schedule`:
   `context["schedule"](name, interval_seconds, fn, timeout_seconds=None)`
   (`core/infrastructure/platform_modules/module_jobs.py`). `name` matches
   `[a-z][a-z0-9_.-]{0,63}` and is unique within the module; the interval is at
   least 60 seconds; the timeout defaults to the interval. A job can be scheduled
   only while the module's hook runs and only under a platform module identity;
   the scheduler is sealed after loading, and a module whose loading fails loses
   its jobs with the rest of what it registered. The control process runs the jobs
   on one scheduler thread next to its other loops:
   - the first run comes after a random delay of up to a tenth of the interval (at
     most 60 seconds), and each later run one interval plus such a delay after the
     previous start; no run starts while maintenance is active, and a run that is
     going when maintenance starts is asked to stop (`should_stop` reads the signal
     live). The signal is the one the worker and Core's other control loops use
     (`public_submission_pause_active`): the lifecycle pause marker or the
     maintenance marker in `/run/flow-steward-runtime`. Compact runs the control role
     in the one runtime container that owns that directory; on Scalable every
     `control-plane` container mounts the same host directory
     (`.flow-steward/runtime`, read-only), so every control replica sees the marker as
     soon as the operator CLI writes it. Core keeps maintenance state only in that
     marker (there is no database copy to read), and a marker that cannot be read
     counts as maintenance. A deployment that ran control replicas on hosts that do
     not share the directory would not see it; no supported profile does;
   - a job runs at most once at a time in a process, and across control replicas
     only on the replica that holds its PostgreSQL advisory lock
     (`pg_try_advisory_lock(hashtextextended('flow-steward:platform-module-job:<module id>:<name>', 0))`
     on one dedicated session per process). The lock is kept between runs, so one
     replica runs the job; when that session ends, another replica takes the lock
     at its next due time. Before every run, and every 30 seconds while a run is
     going, the replica confirms in `pg_locks` that its own backend still holds the
     lock (a session that answers but lost its locks is caught too); without it the
     job does not start, and a running job is asked to stop and recorded as failed
     (`LockLost`). A job must still be idempotent: between two checks a replica that
     lost its session may overlap with the one that takes over;
   - when the control role stops, it stops starting jobs, asks every running job to
     stop, waits for each up to the rest of its timeout and only then closes the
     lock session, so another replica cannot start a job this one still runs;
   - a run that exceeds its timeout is recorded as failed and asked to stop through
     `ctx["should_stop"]()`; a thread cannot be killed, so no new run of the job
     starts until it returns;
   - `fn(ctx)` receives a read-only context: the control ports (below), `role`,
     `platform_module`, `job`, `should_stop` and `for_each_account(fn, *,
     feature=None)`, which calls `fn(account_id)` for every active account (with
     `feature`, only for the accounts the role's `entitlement_service` says are
     entitled to it; without an entitlement service none is), logs an account that
     raises, carries on with the next and returns `{"processed", "failed",
     "skipped"}` (skipped: not entitled; the counts are logged);
   - an exception, a timeout or a failed account is logged with the module id,
     version and job name and never stops the scheduler or the control loop. The
     module is listed in every control heartbeat with `status: "job_failed"` and
     the reason `job <name>: <short reason>` (redacted and bounded like a load
     failure), and the monitoring page shows it in the `platform_modules` check as
     `<id> <version> (control role): Background job failing: <reason>`. The next
     clean run clears it. A job failure is not a load failure: the release
     workflow's load-status check (§8 item 6) does not read it.
2. **Control ports.** Besides `entitlement_service`, `secret_backends` and `db`, the
   control hook (and every job) receives `log_auth_audit_event(bl=None, *, action,
   username="", actor="", status="ok", detail=None)`, which writes the same
   `auth_<action>` event as the app role's `auth_helpers["log_auth_audit_event"]`
   (through the role's identity store when `bl` is omitted); `encrypt_secret`,
   `decrypt_secret` and `reseal_legacy_secrets`, Core's secret codec bound to the
   module (item 4) as `container.external_sign_in` offers it in the app role; and
   `external_sign_in_revocation`
   (`ExternalSignInRevocationPort`: `revoke_grants(cur, user_ids)`,
   `revoke_grants_of_account(cur, account_id)`, `storage_available(cur)`), which
   revokes inside the caller's transaction exactly as Core does on a role or
   membership change (ADR 0083). `core/application/auth/external_session_revocation.py`
   is part of the control role's source slice.
3. **Row actions.** A `resource_admin` resource may declare
   `row_actions: [{key, label, label_key?, method: "POST", path_template,
   confirm?, confirm_key?, success_text?, success_text_key?}]`. `key` matches
   `[a-z][a-z0-9_]{0,63}` and is unique in the resource; `method` may only be
   `POST` (the default); `path_template` lies in the page's API tree (the module's
   own tree for a module page) and carries exactly one `{name}` token as a whole
   path segment. The page registry refuses anything else, and the browser leaves
   out an action that fails the same checks. Each action is a button in the row's
   actions next to Edit and Delete; the token is filled with the row's record id
   (refused like an unusable id for Edit and Delete), `confirm` asks first in a
   confirmation dialog, and the outcome (`success_text`, else "Done.", or the
   error) is shown in the page's alert, after which the list is read again. The
   POST body is `{}`; the module's route rule decides who may call it.
4. **The secret codec is bound to the module.** Decision (review 2026-11-27): a
   module encrypts values bound to itself and decrypts only values it encrypted, or
   that a module it declares under `requires` encrypted
   (`core/infrastructure/security/module_secret_codec.py`). The value is
   `fsmod1:<module id>:<base64>`, AES-256-GCM under a key derived from Core's
   `ENCRYPTION_KEY` and the module id, with the same context as associated data, so
   a relabelled value fails authentication. Values Core encrypted (connection,
   provider and token secrets) and values of unrelated modules are refused
   (`ModuleSecretScopeError`). The ports are bound per module by the loader: in the
   app role each module's container view carries `external_sign_in` bound to it
   (Core's own port refuses `encrypt_secret` / `decrypt_secret`); in the control
   role the hook's and every job's context carry `encrypt_secret`, `decrypt_secret`
   and `reseal_legacy_secrets` bound to it, and the unbound port refuses. A port
   value that offers `for_platform_module(module_id, requires)` is how Core marks a
   port as identity-bound. `requires` grants read access because an add-on that
   requires a module is built on its data (a directory sync reading the sign-in
   engine's provider client secret); the grant is one way. **A module has no
   legitimate need to read Core's connection secrets and gets no port that does**;
   a module that needs one would need a new, explicit port and an amendment here.
   Values a module stored through the earlier unbound port are not accepted by
   `decrypt_secret`; the module re-encrypts them once with
   `reseal_legacy_secrets(cursor, table=, column=, json_key=None)`, which reads and
   writes only a table of the module's own schema (`mod_<module id>`) in the
   caller's transaction, locks the rows, decrypts each unlabelled value with Core's
   codec and stores it bound to the module (run again, it changes nothing). Because
   it only touches the module's own schema, the owning module is the only one that
   can turn an unbound value into one it can read. The binding is an interface
   contract with trusted in-process code, not isolation.
5. **Module setting names do not overlap.** A module set is refused (by the module
   store before anything is written and by the loader) when two module ids have
   the same upper-snake prefix (`acme.b-c` and `acme.b.c` both own
   `FS_MODULE_ACME_B_C_`), or when a module declares a name that starts with
   another module's longer prefix (`acme.login` declaring `FS_MODULE_ACME_LOGIN_SYNC_*`
   while `acme.login-sync` is in the set).

## Before this record is accepted

A prototype must show, on one unmodified Core image:

- two platform modules installed from prepared artifacts, with their Python
  dependencies, loaded in the app, worker and control-plane roles;
- a module settings page reachable through the `resource_admin` renderer with the
  shared React bundle unchanged;
- a module-owned table created and upgraded by the host migrator, including an
  upgrade from the previous module version;
- a feature that is installed but not entitled refused over API and MCP, and
  absent from navigation;
- an interrupted module update resumed from the journal;
- the measured interruption on Compact and on Scalable.

Status: prototype; acceptance criteria are open.

## Consequences

- One Core image and one release line serve every deployment; what differs
  between deployments is the set of installed modules and entitlements, both of
  which are data.
- Platform authority stays out of the extension contract. Extensions gain a
  page link and nothing that touches authentication, access control or shared schema.
- Core takes on a compatibility duty: registration points and seams become an
  interface that modules depend on, so a change to one needs a declared Core
  range and a contract test with a reference module.
- Installing or updating a platform module costs a restart. Turning an already
  installed feature on for an account does not.
- Code that only a module needs must not enter Core, and code that enforces a
  boundary must not live only in a module. Reviews check both directions.
