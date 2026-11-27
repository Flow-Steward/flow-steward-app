# ADR 0066: Global Roles, Shared Team Trust and Secret Provider Boundary

- Status: Accepted
- Date: 2026-11-02 (recorded in this repository on 2026-11-12; drafted under a
  number this repository had already given to another record)
- Related: [ADR 0020](0020-oss-single-tenant-registry-boundary.md),
  [ADR 0018](0018-verified-llm-routing-and-extension-boundaries.md),
  [ADR 0023](0023-extension-owned-oauth-boundary.md),
  [ADR 0057](0057-secret-catalog.md)

ADR 0057 (secret catalog, bindings and approved use) builds on the trust domain and
the stored-secret boundary decided here. Where the two overlap, 0057 governs the
catalog's authority table, bindings and approval; this record governs the global
role model and the provider-independent resolution boundary.

## Context

The open-source deployment serves a team operating in one shared trust domain.
Its human roles are global `admin`, `operator` and `viewer`; `user_roles` does
not assign roles within individual projects or workspaces. Projects and
workspaces organize resources, not independent teams with separate human
membership policies. This complements, and does not relax, ADR 0020's
single-account boundary.

Secret values already have several owners and persistence paths. Connection
credentials and saved workflow input defaults can both use `secret_refs`;
JSON reference maps and workflow defaults are not necessarily separate stores
of values. `SecretReference` carries an opaque reference through execution,
and authorized connector/HTTP boundaries resolve it. The current stored-value
resolver accepts a ciphertext reader and decryptor, so its storage interface
still assumes locally encrypted values.

Adding a remote secret manager such as HashiCorp Vault must not require
rewriting workflow definitions or distributing its API and authentication
logic across core consumers. Vault KV v2 illustrates why a remote locator can
need a mount, path, selected field and version rather than just a key name.

## Decision

### Global roles and one team trust domain

1. Keep global `admin`, `operator` and `viewer`. Do not add project/workspace
   role assignments, per-secret user ACLs, visibility editors or environment
   permission policies to the current product.
2. Keep existing action permissions. In particular, `operator` currently has
   project-connection mutation and secret-save authority; this ADR does not
   silently convert those operations to admin-only. A future change to that
   action matrix requires its own explicit behavior change and verification.
3. A global role is not a resource grant. Keep account ownership, resource
   relationships, reference authority and all existing scope restrictions.
   Do not interpret the shared human trust domain as unrestricted cross-project
   credential resolution or access to other accounts.
4. Keep token scopes, service-account grants, extension runtime grants and
   workflow launch authority. Machine callers do not inherit unrestricted
   access from the team's trust model. A viewer remains read-only.
5. Do not claim that workspaces or projects isolate mutually untrusted human
   teams. Users able to author and execute arbitrary credential-bearing logic
   must be trusted with the credentials that logic is authorized to use.

### Authorization boundary that can be extended

Keep authorization in application-owned policies and services, shared by
transport adapters. Use the existing authenticated principal and `ScopeContext`
as the starting point, not a second identity or role system.

A sensitive decision needs the authenticated actor, explicit operation and
server-resolved resource identity/ownership. Account, workspace, project,
workflow and connection identifiers remain available as applicable even when
the human role is global. A client-supplied identifier, UI permission flag,
secret name or reference alone never authorizes access.

Additional policy implementations may later narrow access using that context
without changing transports or workflow definitions. They must not bypass
ownership checks, token limits, runtime grants or reference authorization.
Do not add unused policy registries, membership tables, configuration switches
or user-facing controls in anticipation of that work.
[ADR 0076](0076-permission-catalogue-and-role-permissions.md) narrows this
rule: the `role_permissions` table, the system-role flag and the optional
scope of a role assignment are allowed, because every permission check and the
open-source refusal of scoped assignments use them.

### Stable references and provider-independent resolution

The durable integration boundary is:

```text
workflow / connection -> opaque local reference -> authorization
    -> host-owned secret resolver -> storage adapter -> secret manager
```

The responsibilities are distinct:

- **Reference identity:** the existing account-qualified local reference is
  what consumers retain. It does not encode a provider URL, access token or
  remote path in workflow content. A reference is not permission to read.
- **Authorization:** core validates the caller, owning resource and execution
  authority before any provider request. The storage adapter cannot grant
  access that core refused.
- **Resolution:** a provider-independent stored-secret read takes an authorized
  account/reference and returns a value only to the approved server-side
  execution boundary. Local decryption belongs in the local storage adapter;
  remote adapters do not pretend their values are local ciphertext. This is
  separate from decrypting one-run inline secret inputs.
- **Provider configuration:** endpoint, trust configuration and authentication
  belong to host-managed storage configuration, not editable step parameters.
  Bootstrap credentials must come from approved deployment secret paths or
  workload identity, without depending on the provider they unlock.
- **Remote locator:** a host-owned mapping selects the provider configuration,
  remote object/path, optional field and version selector. These are adapter
  metadata, not new workflow fields. A version selector must distinguish an
  explicit version from a deliberate current-version policy.
- **Lifecycle:** remote reads do not imply remote create, rotate, delete or
  lease-management authority. Removing a local reference must not delete an
  externally owned secret. Dynamic credentials require a separately defined
  lease/renewal/revocation contract.

Implementations must resolve an explicitly selected reference. No name-based
project-to-workspace fallback, provider fallback, or local copy may silently
replace a missing, forbidden or unavailable secret. Do not expose remote paths,
provider responses or authentication material in public errors, logs, prompts,
checkpoints or step outputs. Keep bounded remote timeouts and safe errors in
the adapter; record reference/provider identity and outcome for audit without
recording values. Broader-policy changes must account for authorization caches.

For connection-based use, permitted destinations remain host-controlled; changing
the destination is a sensitive configuration change. A stored `remote_base_url`
alone is not proof of destination enforcement. Extensions receiving plaintext
are part of the credential trust boundary.

### Persistence and delivery boundary

This decision establishes an architectural contract, not a working Vault
integration. Reuse current scope and reference seams; do not add a disconnected
adapter interface or advertise remote resolution as already implemented.

When the first remote adapter is implemented, put its provider configuration and
reference-to-locator mapping behind the storage boundary. Preserve local
reference identity and existing ownership. Keep provider selection separate from
the secret value, and never overload `encrypted_value` with a URL or remote path.
That integration must evolve the current ciphertext-reader seam into the
provider-independent resolver above and wire it into real consumers.

No provider-specific columns, empty tables, SDK dependency, server or schema
migration are introduced by this ADR. The first implementation must supply the
concrete schema, transaction and deletion semantics with executable contract
tests. Follow ADR 0012: schema changes belong in `db/schema/` before 1.0;
post-1.0 changes use SQL migrations. Different persisted secret owners must be
inventoried before consolidation; their names alone do not prove duplicate
storage.

The open-source code and documentation use neutral capability and provider
names. They contain no commercial-edition terminology, licensing checks,
upgrade prompts or placeholders for separately distributed functionality.
Additional policies or providers can be supplied separately through the
application/infrastructure boundaries described here.

## Security invariants and verification

- Saved values remain write-only in UI and public APIs for every human role.
  Plaintext is available only at authorized execution boundaries.
- Preserve encryption, output redaction and audit; broader role controls are
  not substitutes for these protections. Workflow exports contain references
  or configuration requirements, not values or usable secret authority.
- Recovery backups may contain encrypted values; key protection and recovery
  remain a separate operational responsibility.
- Any implementation changing these boundaries must prove denial before
  provider I/O; account/reference isolation; unchanged viewer and machine-token
  limits; safe failure without fallback; redaction of values and provider
  errors; and continued resolution through both HTTP and connector consumers.
- A remote adapter must additionally test field/version selection, bounded
  failures, credential rotation behavior and removal without remote deletion.

## Consequences

The current product keeps a small global role model without pretending to
provide team isolation inside a deployment. The existing resource and machine
authority checks stay intact.

Future storage integrations can change how an authorized local reference is
resolved while preserving workflow-facing identity. Adapter and persistence
work is still required; this ADR does not promise that every secret path is
already unified or that a remote provider can be enabled by configuration today.

## Amendment (2026-11-18, proposed in ADR 0075): admin configures, operator works, viewer reads

Status: Proposed together with [ADR 0075](0075-project-user-sections.md). Until
it is accepted, decision 2 above stays in force.

This amendment replaces decision 2 with an explicit split of the three global
roles. The roles, the single team trust domain and every resource, account,
token-scope and machine-authority check above stay unchanged.

| Role | Responsibility |
| --- | --- |
| `admin` | Everything that configures the deployment: users, extensions, projects, workflows, triggers, connections, connection slots, secret bindings and approvals, user sections. Sees the administrative UI. |
| `operator` | Does the work the admin configured: starts workflows, uploads files, decides approvals, cancels and retries jobs, reads history, results and files. May create a catalog secret and replace a secret's value (for example a changed mailbox password) without changing where it is bound. Does not see the administrative UI. |
| `viewer` | Reads the same history, results and files. No writes. |

1. HTTP writes default to `admin`. The middleware keeps one explicit allowlist
   of operator writes instead of treating every write outside the admin-only
   patterns as operator-allowed. Self-service account actions stay available to
   every signed-in user.
2. The operator allowlist covers the work itself, in every channel:
   - starting a workflow, through the browser launch route and through
     `POST /api/workflow-runs` (API and MCP `workflow_trigger`);
   - approval decisions and decision overrides;
   - job cancel, restart, step retry, acknowledge and unacknowledge;
   - generating a workflow report and dispatching a notification through a
     route the admin configured;
   - the [ADR 0057](0057-secret-catalog.md) secret create, value-replace and metadata actions;
   - connecting the user's own AI agent (`/api/auth/agent-access/...`), which
     every signed-in role keeps because the token is capped at its owner's role;
   - the extension actions and queries [ADR 0067](0067-extension-page-queries-with-connection.md)
     already grants to operators.

   Step replay (`replay-step/execute`) re-executes captured inputs for diagnosis
   and moves to `admin`.
3. Moving to `admin`: creating and editing connections and their secret fields,
   choosing a workflow's connection slot, binding and unbinding secrets,
   approving a secret configuration, deleting a secret, and every project write
   including `PUT /api/projects/{id}`. The last one also closes a gap where an
   operator could change a project's workflow bindings and policy through the
   general project update although the dedicated routes already require `admin`.
4. The server enforces the split. Hiding menu entries is presentation only.
5. For people, the secret rules live in one place, `core/application/secrets/authority.py`:
   write and approval require operator today. The split raises delete, bind,
   unbind and approval to `admin` there, and leaves create, value replace and
   metadata at operator. Deleting stays with the admin because the delete
   confirmation already shows every connection and workflow that uses the
   secret and removes those bindings in the same transaction.
6. One rule for every channel. What a role may do in the UI it may do through
   the API and MCP, and nothing more: the same route check applies to a browser
   session and to a personal token, which acts with its owner's role. MCP exists
   so a person can work from any client without opening the product, not to
   carry different authority. Token scopes can only narrow a token further.
7. Machine authority is unchanged. The exact-only `secrets:write` and
   `secrets:bind` scopes are set on service-account tokens, which only an admin
   issues; personal agent tokens cannot carry them, so they do not bypass the split.

The product has just launched and no installation relies on operator
configuration writes yet, so the split ships directly, without a compatibility
mode or a transition period. Release notes describe the role model once. The
change needs denial tests for every moved route in addition to the allowlist.

## Amendment (2026-11-25, proposed with ADR 0082): parts that Core carries for a module

Status: Proposed together with
[ADR 0082](0082-platform-modules-and-feature-entitlements.md). Until it is
accepted, the two paragraphs it narrows stay in force.

Section 3a of ADR 0082 lets a table that Core's own check reads, the check itself
and a control inside Core's screens enter Core before Core has a way to write
that table. This narrows two rules above:

1. "Do not add unused policy registries, membership tables, configuration
   switches or user-facing controls in anticipation of that work." Such a part may
   now enter Core when, in the same change, a module that uses it is built and a
   reference module exercises it in Core's tests. Parts without that consumer are
   still not added.
2. "They contain no [...] licensing checks [...] or placeholders for separately
   distributed functionality." Core may check whether a named feature is
   available to an account through its entitlement service and hide a control
   when it is not. The open-source code and documentation still use neutral
   capability names, show no upgrade prompts, and never name a product edition.

The first part built under this amendment is the approver list of an approval
step (`approval_step_approvers`). It narrows who may decide a step and never
widens access: with no rows, or without a module, every decision behaves as
before. The decision journal (`approval_decision_journal`) is written by Core for
every installation; a module may read it through Core's port, and Core keeps rows
past their job only for an account entitled to that.

## Amendment (2026-11-26, proposed with ADR 0082): pluggable secret backends

Status: Proposed. Narrows "Persistence and delivery boundary" above: the first
remote adapter is delivered by a platform module, and Core now carries the seam,
its tables and its admin API, exercised in Core's tests by a reference backend.

**Every consumer reads through the seam.** LLM provider keys (including the
custom-provider verification probe), data connection secrets and typed MCP
connector header values used to decrypt `secret_refs` themselves. They now hand the
reference to `stored_secret_reader(conn)`. The local reader accepts the ciphertext a
consumer already read in its own scoped statement (`prefetched_ciphertexts`), so a
pointer and its value stay one atomic read and a project-scoped statement stays the
scope check. Local by design, never through the seam: OAuth grants, refresh tokens
and client secrets; authenticator, session and sign-in state; and a run's sealed
dispatch envelope (`run_authority.dispatch_extra_state`). Stored references a run
carries are resolved through the seam when a step uses them.

**Port.** `core/infrastructure/security/secret_backends.py`. A backend is
`kind` (lowercase id, never `local`), `feature` (the entitlement new values need),
`capabilities` ⊆ {`write`, `delete`, `rotate`}, `read(context, locator) -> str`
and `health(context) -> SecretBackendHealth(healthy, code)`; optionally
`write(context, locator, value) -> locator`, `delete`, `rotate`,
`validate_config(config)` and `validate_locator(locator)`. `SecretBackendContext`
carries the account, the configuration id, its non-secret settings (read-only) and
a monotonic deadline bounded to 5 s. A backend raises the existing
`StoredSecretMissing`, `StoredSecretUnavailable` (retryable) or the new
`StoredSecretStorageRefused` (code `secret_store_unavailable`, platform blocker:
not installed, turned off or refused); anything else counts as unavailable, and no
backend text reaches an error or a log. Backend credentials come from the
deployment (environment, mounted file, workload identity), never from Core's store.

**Registration.** A module declares `"provides": ["secret_backend"]` and calls
`register_secret_backend(backend)` on `container.secret_backends` (app) or
`context["secret_backends"]` (worker, control). The port is scoped by the loader to
the loading module, refuses an undeclared or identity-less module, a duplicate kind
and any call after the role seals it. The registry is process-wide and rolled back
with a failed module like the other Core global registries. A module must register
in every role that reads values: app, worker and control.

**Tables** (Core schema, empty without a module; migration
`20261126200000_secret_backends.sql`): `secret_backend_configs` (per account:
backend kind, name, non-secret settings, enabled) and `secret_backend_locators`
(account + `ref_key` → configuration, kind, backend locator; the composite foreign
key keeps a locator on a configuration of the same account and kind). A remote
value has no `secret_refs` row; `encrypted_value` never holds a locator.

**Routing.** Without a registered backend the reader is the local reader,
unchanged. With one, a reference that has a locator is read from that backend and
any other reference locally; there is no fallback in either direction. A failed
locator lookup is unavailable, never "local". Without its module in the process, a
moved value cannot be read (fail closed). Values read remotely are cached per
process for 60 s, keyed by account, reference, configuration and locator, never
persisted, and dropped when the admin service changes them. Each remote read is
logged with reference, backend kind and outcome only.

**Entitlement** (owner decision, ADR 0082 §4). The reader never asks it: when a
backend's feature ends, values already kept there stay readable. Creating a
configuration, turning one on or changing its settings, adopting, moving a value to
a backend and writing a new value there need the module's feature; renaming,
turning off, health and moving a value back do not.

**Admin API** (account admin: an interactive person with the admin role, route
permission `settings.manage`, admin-only): `GET /api/secret-backends`;
`POST /api/secret-backends/configs`; `PATCH /api/secret-backends/configs/{id}`;
`GET /api/secret-backends/configs/{id}/health`; `POST /api/secret-backends/adopt`
(the backend must read the value now; it is discarded, a catalog secret and a
locator are created); `POST /api/secret-backends/secrets/{id}/move` (backend with
`write` only); `POST /api/secret-backends/secrets/{id}/move-local` (writes the value
locally and forgets the locator; the remote secret is never deleted). Replacing the
value of a remote catalog secret writes it to its backend or is refused; deleting
one forgets its locator. A remote write that succeeds before the database
transaction fails leaves the remote value in place; it is never cleaned up
automatically. Settings never hold a credential: keys that name one are refused.

Examples of backends a module may provide (documentation only, not Core code):
a cloud key vault, a cloud secrets manager, a self-hosted vault.

## Amendment (2026-11-27): deployment policy per account, no silent overwrite, probe limits

Status: Proposed. Narrows the secret backend amendment above after an independent
review.

**Cross-account isolation: a deployment-operator allowlist.** A backend's
credentials come from the deployment and are shared by every account, so an account
administrator could otherwise point a configuration at another account's vault
path, key vault or project. The installation's operator states, outside every
account's reach, which configuration values each account may use:
`FS_SECRET_BACKEND_POLICY` (JSON) or `FS_SECRET_BACKEND_POLICY_FILE` (a path to the
same JSON; the variable wins). Shape:
`{"<account id>": {"<backend kind>": {"allowed_values": {"<config field>": [values]}}}}`.
Rules, all fail closed (`core/infrastructure/security/secret_backend_policy.py`):

- a listed account may use only the kinds listed under it; every field named in
  `allowed_values` must be present and each of its values (a list item by item) must
  be listed; other fields are left to the backend;
- an account that is not listed: the installation account is unrestricted, so a
  single-account installation is unaffected; any other account may use no external
  backend (a multi-account installation without a policy refuses them);
- a policy that cannot be read or parsed refuses every external configuration.

Core checks it when a configuration is created, changed or turned on, before every
adoption, move, value write and health check, and in the reader: a configuration
outside the policy is never read, whoever saved it (audit outcome
`policy_refused`). The per-kind entry (`None` when unrestricted) is also passed to
the backend as `validate_config(config, policy)` for rules Core cannot express, for
example path prefixes; a backend whose `validate_config` takes one argument keeps
working unchanged.

**No silent overwrite.** A move refuses (409) a name another catalog secret of the
account is already kept at in the same configuration (a stored locator that
contains the requested one). A first write never overwrites a secret that already
exists in the storage unless the administrator explicitly chose `replace` (default
`false`): a backend with the new optional capability `create_only` (requires
`write`) receives `write(context, locator, value, create_only=True)` and raises
`SecretBackendTargetExists` when the name is taken; otherwise Core asks the optional
`exists(context, locator) -> bool`, or, without it, reads the name and treats
`StoredSecretMissing` as free. When the storage cannot tell, the move is refused.
Adopting refuses a name another catalog secret is already kept at.

**Probe limits and audit.** Adopting and the health check answer whether something
exists in, or is reachable through, the storage. Each is limited per account by the
shared sliding-window limiter (`FS_SECRET_STORAGE_PROBE_MAX_ATTEMPTS`, default 20,
per `FS_SECRET_STORAGE_PROBE_WINDOW_SECONDS`, default 300; 429 with `retry_after`;
the limiter fails closed). Every attempt is audited with its outcome
(`secret.storage_adopt`, `secret.storage_health`), a refused one in its own
transaction so the row survives the refusal. Messages stay as they are: the callers
are administrators of the account.

## References

- [Security standard](../../standards/security-standard.md)
- [Vault KV v2 API](https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2)
