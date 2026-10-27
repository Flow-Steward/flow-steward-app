# ADR 0013: Extension Package Identity And Dependencies

## Status

Accepted for pre-1.0.

## Context

Before 1.0, extension package identity and local folder names were effectively the
same value. Short IDs such as `notifier` or `sample_tools` were easy to type
but ambiguous once marketplace, private imports, generated bundles, and multiple
publishers share the same installation surface.

## Decision

`extension_id` is the canonical package identity. It must be lowercase ASCII,
dot-separated, and contain at least two segments. Segments may contain
`a-z`, `0-9`, and hyphen:

```text
^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$
```

Valid examples:

```text
acme.notifier
acme.sample-tools
acme.outgoing-email
com.acme.crm-sync
```

Invalid examples:

```text
notifier
sample_tools
SMTP
smtp mail
outgoing_email
```

Local folders are packaging/readability names, not identity. The standard folder
name is the canonical ID with dots and hyphens replaced by underscores:

```text
acme.notifier                 -> extensions/acme_notifier/
acme.sample-tools             -> extensions/acme_sample_tools/
acme.outgoing-email           -> extensions/acme_outgoing_email/
```

Flow Steward-published extensions use the `flowsteward.*` namespace and the
matching `extensions/flowsteward_<name>/` installation-folder convention.
Core bundles no extensions. Each first-party extension lives in its own
repository and installs from the
[extension catalog](https://github.com/Flow-Steward/extensions), following the
2026-10-01 amendment. Package identity and dependency rules apply equally to all
publishers.

Short provider or routing keys remain allowed only when they are explicitly not a
package identity. Examples include webhook `provider_key`, notification channel
keys, credential provider keys, and extension-internal event source labels such
as `notifier` or `sample_tools`. Those values are routing/configuration
identifiers within a manifest or runtime payload; they must not be used as
`extension_id`, bundle folder names, install identity, release identity, seed
manifest identity, or catalog keys.

No legacy alias layer is provided before 1.0. Old short IDs are invalid in
descriptors, manifests, tests, seeds, and examples.

## Collision Policy

The platform must block ambiguous package identity before it becomes install or
marketplace state.

New releases with the same `extension_id` are allowed only when they continue the
same package lineage: same `source_kind`, `source_ref`, and account scope. A
different source or publisher claim must hard-fail. `vendor.vendor_name` is
display metadata and is not a trusted ownership boundary.

Local discovery also blocks duplicate canonical `extension_id` values from
different folders.

## Dependencies

Descriptors and simple extension manifests may declare package-level extension
dependencies:

```yaml
dependencies:
  extensions:
    - extension_id: acme.outgoing-email
      min_version: 1.0.0
      max_version_exclusive: 2.0.0
      required: true
      reason: Used to send notification emails
```

Dependency IDs must be canonical. Required dependencies are satisfied only by an
active, accessible install for the current account. A marketplace release alone
does not satisfy a dependency. Optional dependencies produce warnings/degraded
metadata but do not block install.

Required dependency cycles are invalid.

## Portable Python Dependency Distribution

Distributable extensions with `python_requirements` must carry the complete
hash-pinned transitive wheel set for both supported Linux runtime architectures:
AMD64 (`x86_64`) and ARM64 (`aarch64`). The existing lock/bundle tooling defaults
to `manylinux2014_x86_64,manylinux2014_aarch64`. A universal `py3-none-any` wheel
may satisfy both; compiled dependencies require compatible wheels for each.
Docker Desktop hosts still run Linux containers, so macOS or Windows wheel tags
are not deployment targets.

Authors and authoring agents use `extensions lock-deps`, then
`extensions bundle --with-wheels`. The latter downloads prebuilt, hash-verified
wheels for both default platforms before archiving. It does not cross-compile
source distributions, and bare `bundle` does not fetch missing dependencies.
Missing wheels or hashes for either target must be resolved before a portable
release is published. `--platforms active` is a local development exception,
not the release recipe. Install/runtime must not become a second dependency
build or resolution engine.

This clarifies the existing packaging path rather than adding a new one. Native
executable payloads retain the per-target archive contract in ADR 0015.

## Runtime Semantics

The identity layers remain separate:

```text
extension_id = package identity
install_id   = concrete installed/runtime instance
release_id   = concrete release artifact
folder/module = local packaging/readability convention
```

Runtime bindings must use `install_id` or connection-scoped IDs where a concrete
installed instance is required. They must not collapse dev/prod or
multi-credential scenarios to package ID alone.
