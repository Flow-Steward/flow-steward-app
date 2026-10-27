# ADR 0015: Atomic Extension Package and Artifact Lifecycle

## Status

Accepted

## Context

An extension package may contain executable runtime files, an `extension.yaml`
manifest, an optional co-located `extension-descriptor.yaml`, Python wheels, and
declarative registry artifacts. Treating those pieces as separate installs can
publish empty or mismatched projections and can leave runtime files active after
artifact publication fails.

Extensions are installed globally. Projects opt into projected workflows and
datasets separately and select project-scoped connections at execution time.
Installation must not create projects, workspaces, connections, or assignments.

## Decision

1. A package is one install payload and receives one `install_id` through one
   host-owned lifecycle operation. Runtime files and dependencies are staged and
   validated before database publication. The descriptor release, immutable
   artifact snapshots, and registry projections are published transactionally
   in the database. Filesystem activation follows commit, directly or through
   an after-commit callback.
2. Every installable ZIP contains exactly one canonical `extension.yaml` with
   `manifest_version: 2`. A co-located current descriptor is optional when the
   package publishes declarative artifacts; its extension ID and version must
   match the manifest. Descriptor-only archives and pre-v2 manifests are rejected
   before files or database state are published.
   Next to that manifest the package carries its logo, `icon.png`: a square PNG
   of 32-512 px and at most 100 KB, the same rules as a catalog icon. The file
   name is the contract (the manifest has no icon field). Validation, packaging
   and install refuse a package without a valid one, before anything is
   published; there is no fallback logo for a package.
3. Descriptor refs are relative bundle-local `.yaml`, `.yml`, or `.json` paths.
   Remote URLs, absolute paths, traversal, missing files, malformed documents,
   oversized documents, excessive nesting, and artifact-ID mismatches fail the
   complete install before publication.
4. Snapshots contain the complete parsed artifact document. Published workflows,
   agents, skills, tools, datasets, and saved queries are immutable projections
   owned by the install. Editing starts by cloning into a canonical user-owned
   artifact.
5. Artifact readiness is evaluated independently. Optional extension dependencies
   may disable one projected artifact without disabling its owning package.
6. Uninstall is previewed as a revisioned impact plan. Confirmation recomputes the
   plan and rejects stale revisions. Confirmed removal deletes only install-owned
   state and bindings in one transaction. User-owned and foreign-extension
   dependents remain but are marked invalid or disabled with structured reasons.
7. Package compatibility uses the canonical runtime targets `linux-amd64` and
   `linux-arm64`. Target-specific archives contain only the matching binary and
   are rejected before publication when the server target differs.
   Python dependency wheel portability is separate: ADR 0013 requires the
   complete locked wheel set for both architectures in distributable Python
   packages (one universal wheel can serve both). Native executable releases
   remain two explicit `--runtime-target` exports, one for `linux-amd64` and one
   for `linux-arm64`; a wheel-platform list does not cross-compile a binary.
8. Extensions may not own embedded databases or private durable storage. Durable
   extension state is written only through host-owned public SDK/storage
   contracts. Test doubles must be stateless.

## Consequences

- Before lifecycle commit is attempted, failures invoke staged cleanup or
  compensation; update compensation failures are surfaced. After commit is
  attempted, the host avoids blind rollback. Publication recovery follows the
  committed database-selected immutable release.
- Runtime packages can be built and validated without leaking host or kernel
  details beyond the canonical target.
- Global installation remains separate from project assignment and connection
  selection.
- Third-party binaries are pinned, checksum-verified at build time, bundled in the
  archive, and never downloaded during install or runtime.
