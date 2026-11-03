# ADR 0055: Secret Catalog, Bindings and Approved Use

- Status: Accepted (owner approved the brief on 2026-11-02; record written 2026-11-03)
- Date: 2026-11-03
- Related: [ADR 0012](0012-post-1.0-sql-migrations.md), [ADR 0020](0020-oss-single-tenant-registry-boundary.md),
  [ADR 0054](0054-pre-1.0-released-database-upgrades.md)

## Context

Static credentials are typed into each connection and stored per connection
(`secret_refs` rows keyed `project_connection:<connection>:<field>`). The same API
key entered in five connections is five values, and replacing it means editing five
connections. People need to create a secret once, reuse it, replace it in one place
and see where it is used.

Reuse changes the risk. Today, using a credential requires knowing it. With a catalog,
choosing a secret is enough, so the right to choose one becomes the right to send it
somewhere. Whoever can then change the configuration around a chosen secret holds
the same power. The installation keeps one human trust domain with global
`admin / operator / viewer` roles. Machine tokens, MCP clients and LLM-driven
authoring are not part of that domain.

## Decision

### Model

- **`catalog_secrets`** holds the identity and metadata of one secret:
  - a stable `id`, `account_id`, `workspace_id`;
  - `project_id`: null for a workspace secret, set for a project secret;
  - name, description;
  - `storage_ref_key`;
  - who changed the value last, and when;
  - who changed the metadata last, and when.
  Names are not unique: two secrets with one name are two secrets, and nothing looks
  a secret up by name.
- **The value** stays in `secret_refs` under `storage_ref_key` and is read through the
  stored-secret reader (`core/infrastructure/security/stored_secrets.py`). A new
  secret uses `catalog_secret:<id>`. A migrated connection credential keeps its
  existing key, so moving it copies no ciphertext. The value is write-only for every
  role in every UI and API.
- **`catalog_secret_bindings`** records one consumer field using one secret. It is the
  only source of truth for usage. The secret is a foreign key with
  `ON DELETE RESTRICT`, so a used secret cannot be deleted even by concurrent calls.
  The consumer is a typed foreign key (in 2a, `project_connection_id` with
  `ON DELETE CASCADE`), so deleting a consumer removes its bindings in the same
  transaction and never deletes the secret.
- **The connection's `secret_refs.refs` map is a projection** of its bindings. It is
  written only by the binding service, in the same transaction as the binding row.
  Connection create, update, import and clone payloads cannot set it; a payload that
  tries is rejected.
- **`catalog_secret_approvals`** records, per consumer, the approved fingerprint of
  its executable configuration: the projection, destinations, extension release,
  actor and actor kind.

### Availability

| Secret | May be bound to |
| --- | --- |
| Project secret | a resource of the same project |
| Workspace secret | a resource of that workspace or of a project in it |
| Other workspace or account | never |

The rule is checked on the server when a binding is created and again when a value is
read. Moving a resource to another project re-checks every binding and is refused,
with the list of conflicts, when one would break.

### Authority

| Action | Interactive human | Machine token, service account, MCP, LLM authoring |
| --- | --- | --- |
| Read value | never | never (only the execution boundary reads values) |
| List, view metadata and usage | viewer and above | read scopes, as today |
| Create, replace value, edit metadata, delete | operator and above | exact `secrets:write` |
| Bind, unbind | operator and above | exact `secrets:bind` |
| Approve a configuration | operator and above | never |

- "Interactive human" means a trusted interactive session: `auth_source` is `local` or
  `external`, and the principal is not a service account or system actor. A
  client-supplied flag cannot claim it.
- `secrets:bind` and `secrets:write` are exact-only. `scope_granted` (and the copy used
  for token delegation) checks for them before `admin:all` and wildcards. `admin:all`,
  `*:*`, `secrets:*`, `*:bind` and `*:write` do not grant them, and no existing token
  receives them.
- Saving a value straight into a connection field creates a project secret and binds
  it. That is a create plus a bind, so it needs both rights. For a human, operator is
  enough, which keeps the operator's existing authority over connection credentials.

### Approved use

- A consumer with at least one catalog binding may use its credentials only while its
  current fingerprint matches its approval.
- **The fingerprint is a digest of the canonical executable projection.** For a
  connection the projection includes:
  - identity, project, extension and install;
  - connection type, runtime mode, `remote_base_url`;
  - `connection_config`, `connection_routes`, `ingress_config`, `workflow_mappings`;
  - the bindings (field → secret id);
  - the destinations;
  - the installed release: `release_id` and `descriptor_sha256`.
  It excludes display name, tags, health and runtime status.
- **Python computes it, only Python.** The runtime check and the upgrade phase call the
  same function.
- **Destinations.** For a remote connection they are the normalized origin of
  `remote_base_url`: scheme, host, effective port. For an extension that runs
  locally, approval means trusting that exact release; the code it runs is not
  confined. Only exact origins are approved; see the workflow destinations below.
- **What does not change the fingerprint:** replacing a secret's value, or editing a
  secret's metadata. Those are protected by `secrets:write` instead.
- **What makes the configuration unapproved:** any change to the projection, including
  a new extension release, a changed binding or a changed destination.
- **How approval works.** It is a separate, protected operation. The server approves
  exactly the fingerprint the person was shown and returns a conflict if the
  configuration changed meanwhile. Approving several connections for one new release
  uses the same service and fails as a whole on any conflict.
- **What happens when unapproved.** Resolution fails with `secret_configuration_unapproved`,
  `retryable=false`, before any secret is read. Schedules and triggers fail the same
  way.
- **Where it shows.** The consumer reports its approval status, and the status appears
  in the consumer's card and list.

### Workflow inputs (2b)

- **A secret input is bound only in `catalog_secret_bindings`** (`consumer_kind`
  `workflow_input`, with the launching project). The workflow definition never carries
  the binding, so saving, AI Builder, import, export, clone and revision restore cannot
  move or forge it. A clone or an imported copy starts unbound.
- **Existing secret-input defaults stay workflow-local.** A `default_secret_ref` belongs
  to its workflow and versions with it, as before. The upgrade does not move them into
  the catalog, so no existing workflow falls under approval at upgrade time. Only a
  catalog binding needs approval.
- **The workflow fingerprint** covers the executable settings, the trigger and the
  bindings. Canvas position, labels, notes and layout are excluded; any other change,
  including a changed HTTP URL, makes the workflow unapproved.
- **Destinations.** A literal HTTP step URL contributes its exact origin. A step whose
  URL is computed needs destinations that the approving person enters: `exact_host`
  rules of scheme, host, effective port and an optional path prefix matched on segment
  boundaries (`/api` admits `/api/x`, never `/api-x`). The `subdomains` rule type is
  refused, because a correct match needs a pinned Public Suffix List snapshot, which
  this release does not ship.
- **Launch.** Each launch, child launch and test run checks the approval of the
  definition being launched and the secret's availability to the launching project. It
  records the result in protected run state (`_workflow_catalog_secret_authority`),
  which steps cannot write and the replay snapshot does not carry. The HTTP and connector
  boundaries check that state before every stored read and refuse an unapproved
  destination.
- **Child workflows.** A catalog secret a parent passes on reaches a child only when a
  person approved the child's own definition too; every level is checked.
- **Structure-locked workflows** (installed from a catalog) keep their approval panel,
  but their binding controls are disabled with the rest of the locked definition.

### OAuth stays outside

`_oauth_*` grant values and the OAuth client lifecycle keep reading locally. They are
not catalog entries, are not bound, and are not part of the approval projection.

### Audit

Every create, value replacement, metadata change, bind, unbind, delete and approval
writes a `command_audit_log` row (`command_type` `secret.*`). The row records actor
kind and id, target ids, outcome and time. It never records values, ciphertext or
provider responses.

### Moving existing installations (ADR 0054)

- **SQL** creates the tables. For every non-OAuth connection reference whose
  `secret_refs` row exists, it inserts one project secret that points at the existing
  key, plus its binding.
  - The name is `<connection display name> / <field>`, at most 200 characters. When
    two moved secrets of one account would share a name, the connection id is
    appended, and the base is shortened first so the whole name still fits.
  - One stored value used by several connections of one project becomes one secret
    of that project, bound to each of them.
  - A stored value that connections of **different projects** share has no owner the
    move could pick without changing who may use it. Preflight lists such references
    and blocks the upgrade, and the migration itself refuses them. They are resolved
    explicitly first, for example by entering the value separately per connection.
  - References whose value row is missing are already unusable. They are left
    unbound and listed by preflight; they do not block.
  - No values are compared or merged.
- **The data phase `catalog_secret_initial_approvals`** creates an approval for the
  current fingerprint of every migrated connection. The actor kind is `upgrade`, the
  actor is `system:upgrade:catalog_secret_initial_approvals`, and an audit row records
  it. A rerun skips approved consumers and never approves a configuration that changed
  after the move.

## Consequences

- One secret serves many connections. A replaced value applies at the next use
  without touching consumers.
- Every new or changed credential-bearing configuration needs one human approval
  before use. That includes connections a person creates.
- Machine tokens that used to save connection credentials now need the two explicit
  scopes.
- A workflow that sends a catalog secret to a computed URL cannot be approved until a
  person lists its destinations, and a destination under a whole domain must be listed
  host by host until a Public Suffix List snapshot ships.
- Dynamic HTTP destinations in existing installations and the template catalog were
  not inventoried, and the session rule of ADR 0054 was not exercised on running
  Compact and Scalable installations. Both are follow-ups.
