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
| Tables that Core's own checks read | Core schema and Core migrations | Present and empty; nothing in Core writes them |
| The check itself | Core | Answers as today |
| Controls inside Core's screens | Core frontend, shown only when the feature is entitled | Hidden |
| Rules, write operations and the API that changes those tables | The module | Absent: no route accepts a change |
| Stand-alone administration screens and module-private data | The module, as registered pages and its own schema | Absent |

This narrows one rule of ADR 0066, which forbids adding membership tables,
switches or user-facing controls in anticipation of future work. Such a part may
now enter Core when the module feature that uses it is built in the same change
and a reference module exercises it in Core's tests. Parts with no consumer are
still not added.

A module-owned schema (section 2) is for data that only the module reads. Data
that a Core check depends on belongs to Core's schema, so that the check, its
migration and its tests stay together.

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
   the one in the role's image. A prepared module that fails any check or cannot
   be imported stops the process; it is never skipped. Module paths are appended
   after Core's, so Core's pinned dependencies always win. A module may bundle a
   dependency that a role's image already has at the same version, because roles
   on Scalable ship different dependency sets; the image's copy is then used. A
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
   the operation activates the previous set again and restarts the roles, and
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
