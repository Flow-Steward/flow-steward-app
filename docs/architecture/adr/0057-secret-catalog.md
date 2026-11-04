# ADR 0057: Secret Catalog, Bindings and Approved Use

- Status: Accepted (owner approved the brief on 2026-11-02; record written 2026-11-03)
- Date: 2026-11-03
- Related: [ADR 0012](0012-post-1.0-sql-migrations.md), [ADR 0020](0020-oss-single-tenant-registry-boundary.md),
  [ADR 0056](0056-pre-1.0-released-database-upgrades.md)

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
  - a stable `id` and `account_id`;
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
  Deleting a used secret is a confirmed plan: `GET /api/secrets/{id}/delete-plan`
  lists its bindings with a revision over exactly that set, and
  `DELETE /api/secrets/{id}?plan_revision=` removes those bindings (rewriting each
  connection's projection) and the secret in one transaction. A changed set refuses
  with `stale_plan`; without a revision a used secret is still refused. The confirmed
  delete also unbinds, so it needs bind authority besides write.
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

A secret belongs to the installation (its account), not to a project or a workspace.
Every project may choose it, and one secret serves connections and workflow inputs of
any number of projects. Bindings still record the consumer and the consumer's project,
so the Secrets page shows where each secret is used. Another account's secret never
exists for a caller. Secrets created before 20261104 belonged to a project or a
workspace; `20261104120000_secret_catalog_installation_wide.sql` empties those two
columns and drops their foreign keys, keeping every secret, value and binding. The
columns stay, unused, until the 1.0 baseline, because the earlier released migration
names them and runs after `db/schema/` on a fresh install.

Because a secret serves every project, a caller restricted to some projects or
workspaces (a project-scoped token) may list secrets and bind them to its own
consumers, but may not create a standalone secret, replace a value, edit metadata or
delete one.

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
- Saving a value straight into a connection field creates a secret and binds
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
- **The workflow fingerprint** covers the definition a run executes: the same
  `frozen_definition` a job pins (steps, Groups, For each containers, the execution
  graph and the settings), plus the bindings. Canvas position, labels, notes and layout
  are excluded. Any other change, a step's URL, headers or body on the same host
  included, makes the workflow unapproved.
- **Destinations.** An HTTP step whose scheme and host are literal contributes its
  exact origin, even when its path or query is computed. A step with a computed
  scheme, host or port needs destinations that the approving person enters:
  `exact_host` rules of scheme, host, effective port and an optional path prefix. A
  path prefix is matched on segment boundaries (`/api` admits `/api/x`, never
  `/api-x`) after dot segments are resolved, and a path with an encoded dot, slash or
  backslash is refused. The `subdomains` rule type is refused, because a correct
  match needs a pinned Public Suffix List snapshot, which this release does not ship.
- **Computed origins only for catalog-only requests.** The HTTP step has required a
  literal HTTPS origin for any secret-bearing request since 0.9.0. A computed origin
  now passes only when every reference the request carries is a catalog secret, by
  the host database, and the rendered URL is HTTPS without userinfo; each read of
  those keys then checks the job's pin, its ancestors, the binding and the rendered
  URL against the approved destinations, and a key that is no longer a catalog
  secret is refused, never read without an allowlist. Inline values, workflow
  defaults, any mix with them, and basic or bearer auth keep the literal-origin rule.
  The SSRF checks, no-redirect and no-secret-in-controls rules are unchanged.
- **Nothing is decided at launch and carried in run state.** The worker derives a
  run's secret capabilities from host records of its own job
  (`core/application/secrets/run_authority.py`): the pinned definition and its
  workflow-local defaults, the catalog bindings of that workflow for the job's project,
  inline values in the job's own encrypted dispatch record, and, for an Invoke child,
  what its parent may read. The delivered message, job metadata a caller can set, run
  state a step can write and a storage key presented in launch inputs grant nothing.
- **Every stored read is checked against the job's pin.** Before a catalog value is
  read, the HTTP and connector boundaries look up the job's pinned definition, the
  workflow's current bindings and approval, and the secret's account. The read is refused
  unless the pinned definition is approved, the secret belongs to the job's account, the
  job holds the key (bound for the job's project), and the request goes to an approved
  destination. An
  edit after enqueue changes neither what the run executes nor what it may read; a new
  run of the edited definition reads nothing until it is approved. Redelivery, retry
  and resume of one job are checked again against the same pin; a restart is a new job
  with its own pin.
- **Connector steps pin their receiver before the read.** A connector step selects
  what its input goes to before any value is read, and dispatches to that same
  selection (`core/application/orchestration/connector_receiver.py`):
  - an extension action: the installed record captured to its immutable release
    directory, which must still be the install's canonical release; identity
    `install_id`, `release_id`, `descriptor_sha256`;
  - a built-in tool: host code, identified by its tool id and a digest of its
    executable manifest (implementation, module, config, interface);
  - a tool implemented by an extension: both of the above;
  - a remote MCP tool: the connector row read with the run's trusted account,
    project and tenant, identified by the manifest digest and a digest of its
    endpoint, transport, auth mode, credential revision, header reference map and
    allowlist. The call uses that row, after the same lineage checks.
  Identities carry no host paths, so one release is one identity on every host.
  An approval records the receivers per project, and the approval panel names each
  one. A read must match the approved entry of its own job's project; a new
  release, an edited tool manifest or a changed MCP connector needs a new approval.
  A step whose receiver cannot be selected never receives a catalog secret. A
  workflow-local secret reaches connector inputs as before.
- **Child workflows.** A catalog secret travels to an Invoke child only when the child
  records its parent and the parent records the child, the parent passed the key in,
  and the pinned definition of every level of the chain is approved. An approved child
  never revives the authority of an unapproved parent.
- **Diagnostic step replay** re-runs one agent step in isolation (no job, no project)
  and, as before, holds no secret authority.
- **Structure-locked workflows** (installed from a catalog) keep their approval panel,
  but their binding controls are disabled with the rest of the locked definition. The
  project's Workflows tab offers binding and approval for every linked workflow
  without the builder; the server checks role and interactive session for both.

### Data connections

A secret field of a workspace data connection may use a stored secret: the connection's
`secret_refs_jsonb.refs` keeps the secret's own storage key, written with the
`data_connection` binding, and the connection reads the value by that key when it
connects. A value typed later ends the field's binding; uninstall cleanup never
deletes a catalog value. Only an administrator configures a data connection, with no
separate approval step.

### LLM provider installs

An LLM provider extension's secret fields (an API key, a runtime bearer) may each use
a stored secret instead of a typed value. The install keeps `{secret name: storage
key}` under `llm_provider_catalog_refs` in its config, written in the same
transaction as the `llm_provider` binding, and drops any typed copy of that name.
Every reader (save, status, model discovery, model calls, the runtime handshake)
overlays the values read by those keys at use time and fails closed when one is
missing, so a replaced value reaches the provider at its next call. A changed choice
invalidates verified models like a changed key; binding needs bind authority on top of
the admin role, and a confirmed delete of the secret removes the name from the map and
leaves the typed values alone.

### MCP connector headers

A request header of an MCP connector (Registry → MCP: the create and edit forms and
the catalog install form) may send a catalog secret instead of a typed value, with an
optional literal prefix of at most 32 characters and no line breaks
(`Authorization` = `"Bearer "` + secret). The choice is a binding of kind
`mcp_connector` plus its projection in `header_secret_refs_jsonb`
(`{"catalog_ref": <storage key>, "prefix": ...}` beside the typed values' own keys),
written in the transaction that writes the connector; the value is read
account-scoped through the stored-secret reader only when the key is a catalog
secret of the connector's account, fails closed like a missing typed value, and never
reaches the browser. Choosing, changing or removing one needs bind authority, with no
separate approval step: only an operator configures a connector and its endpoint,
and moving the connector to Trash removes its bindings.

### OAuth stays outside

`_oauth_*` grant values and the OAuth client lifecycle keep reading locally. They are
not catalog entries, are not bound, and are not part of the approval projection.

### Audit

Every create, value replacement, metadata change, bind, unbind, delete and approval
writes a `command_audit_log` row (`command_type` `secret.*`). The row records actor
kind and id, target ids, outcome and time. It never records values, ciphertext or
provider responses.

### Moving existing installations (ADR 0056)

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
- A workflow that sends a catalog secret through a step with a computed host cannot be
  approved until a person lists its destinations, and a destination under a whole
  domain must be listed host by host until a Public Suffix List snapshot ships.
- Workflow secret inputs work on the worker again: the run's capabilities used to be
  cut from the delivered message along with every other reserved key, so a stored or
  inline secret input never reached a step. They are now derived on the worker.
- Dynamic HTTP destinations in existing installations and the template catalog were
  not inventoried, and the session rule of ADR 0056 was not exercised on running
  Compact and Scalable installations. Both are follow-ups.
