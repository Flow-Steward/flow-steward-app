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

## References

- [Security standard](../../standards/security-standard.md)
- [Vault KV v2 API](https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2)
