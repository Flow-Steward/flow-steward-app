# ADR 0016: Host-Mediated Extension Data Migrations

## Status

Accepted

## Context

Extension ZIP updates preserve a canonical install identity and host-owned JSON
configuration. A newer bundle may require a different JSON shape, but extension
code must not access Core, PostgreSQL, secret storage, or host mutation grants.
Running arbitrary extension SQL or uninstall/update hooks would break that trust
boundary and make rollback unreliable.

## Decision

1. The integer `lifecycle.data_schema_version` identifies the stored-data version.
   Version 1 is the baseline and the current compiler/descriptor default when
   the field is absent. Higher versions declare a complete ordered chain of
   forward migration action IDs from version 1. The installer accepts only the
   canonical `extension-descriptor.yaml` filename for a bundled descriptor.
2. Migration actions are pure SDK transformations for `install_config`,
   `project_connection_config`, `workspace_data_connection_config`, or
   `project_state`. Input and output contain only bounded non-secret JSON and
   immutable record identity metadata.
3. The host snapshots values and revisions, runs actions sequentially from the
   staged candidate bundle without an open database transaction, and rejects
   external-effect actions or malformed results.
4. Candidate schemas and secret-name compatibility are validated after actions
   and before the final lifecycle transaction. That transaction locks source
   rows and membership, rechecks values, revisions, and secret refs, applies
   transformed values, and advances
   `extension_installs.applied_data_schema_version` atomically.
5. Secrets and secret refs never enter the migration envelope and cannot be
   added, renamed, or deleted by migration output. Extensions never own tables,
   SQL migrations, embedded databases, or private durable storage.
6. Before lifecycle commit is attempted, failures use transaction rollback and
   compensation to attempt restoration of prior state. Compensation failures
   are surfaced.
   Once `lifecycle_commit_attempted` is set, the host does not blindly roll back
   the package. Persisted database authority selects the immutable release, and
   publication recovery completes its filesystem activation.

## Consequences

- Package SemVer and stored data schema evolve independently while retaining one
  install identity.
- Runtime execution is isolated from database transactions; only the host writes
  durable state.
- Commit uncertainty does not guarantee restoration of the previous package;
  publication recovery follows the committed database-selected release.
- Migration batches and payloads remain bounded, testable, and portable across
  local subprocess and remote runtimes.
- Schema changes that cannot be expressed as non-secret JSON transformations
  require a host platform change, not extension-owned persistence.
