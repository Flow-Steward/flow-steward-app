# ADR 0049: Verified Semgrep audit distribution

Status: Accepted

## Context

The audit-only Semgrep 1.183.0 distribution pins MCP 1.29.0 and OpenTelemetry
1.37.x. Published fixes require MCP 1.30.0 and OpenTelemetry 1.45.1. The normal
resolver cannot satisfy those fixes together with the upstream metadata. Risk
acceptance is not an acceptable substitute for this task's dependency remediation.
Product runtime dependencies and the scanner's analysis rules are separate concerns.

## Decision

Build `semgrep==1.183.0+fs.1` in the existing isolated audit environment from the
SHA256-pinned official Linux wheel. Change only distribution metadata and RECORD:
replace the MCP requirement with 1.30.0, the OpenTelemetry API/SDK/HTTP exporter
requirements with 1.45.1, and instrumentation requirements with 0.66b1. Preserve
all executable and data bytes, wheel tags, and executable permissions. Give the
result an explicit local identity; do not install an incompatible graph over an
upstream pin or suppress `pip check`.

The builder rejects unexpected source hashes, metadata, duplicate or unsafe ZIP
members. A receipt records the original wheel hash and installed file hashes.
Inventory generation validates that receipt and installed Semgrep files before
mapping its local packaging version to the unchanged upstream code version for
advisory lookup. Every other installed package is queried at its actual version.
Audit validation rejects skipped, missing, duplicated, malformed, or substituted
package identities. Upstream Semgrep advisories remain blocking.

The Dockerfile, builder, and inventory policy are quality fingerprint inputs.
The existing registry remains the only owner of scanner and dependency checks.
Compatibility evidence includes `pip check`, MCP registration, tracing lifecycle,
and positive/negative engine scans, followed by repository quality gates.

## Consequences

This is a maintained audit packaging patch, not an upstream-supported dependency
set. Its scope is limited to the disposable audit image. No vulnerability
acceptance or product dependency override is introduced. Remove the patch when
an upstream Semgrep release permits the fixed dependency set; an upstream version
change requires renewed artifact hashes and compatibility validation.
