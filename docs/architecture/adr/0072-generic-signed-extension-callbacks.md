# ADR 0072: Generic Signed Extension Callbacks

- Status: Proposed (generic profile and ADR number approved for release 0.9.18; final ADR text acceptance pending)
- Date: 2026-11-14
- Related: ADR 0012, ADR 0056, ADR 0057

## Context

An extension callback can already verify timestamped, delivery-id based HMAC or
use the existing `hub_signature` profile. Other providers sign the unmodified body
with a connection secret and use different headers, prefixes and subscription
verification query names. Provider routing and payload interpretation belong to
the extension; the host needs a bounded, declarative authentication protocol.

## Decision

Add `auth.type: raw_body_hmac_sha256` to the existing generic extension callback.
Explicit generic declarations require extension host contract `platform_min: 1.9.0`;
the host advertises `DEFAULT_FS_RUNTIME_VERSION = "1.9.0"`. This is the extension
API compatibility version, separate from application release `0.9.18`. No SDK
version change is required by this host-only protocol extension.

The new profile requires a connection-scoped callback and declares:

- `signature_header`: an HTTP token header name, at most 128 ASCII characters.
- `signature_prefix`: optional exact ASCII prefix, at most 64 characters; the
  default is the empty string. No whitespace trimming, interpolation or expression
  evaluation applies to the prefix.
- `secret_name`: a local secret field declared by an owning extension connection
  type's `secret_schema.properties`. It cannot name a project secret, arbitrary
  reference, provider locator, or another extension's secret.

The host verifies exactly 64 hexadecimal SHA-256 digest characters after the
exact prefix, using HMAC over the received raw bytes. The existing capped body
reader and connection secret resolver remain the only owners of body limits and
secret resolution. The registration's generated secret is never a signing key.
A missing or unavailable connection secret rejects the request. Signing material
is an opaque string: its leading/trailing whitespace is preserved exactly.

An optional `auth.verification` object declares a GET subscription handshake:

- `token_parameter` and `challenge_parameter`: distinct exact query names, each
  at most 64 ASCII identifier characters.
- `required_parameters`: an optional map of at most eight other exact query names
  to nonempty literal strings of at most 512 UTF-8 bytes. These names cannot
  overlap the token or challenge names.

The verify token is the existing registration secret. The challenge is returned
unchanged as `text/plain` only after constant-time token comparison and exact
required-parameter checks. Both token and challenge are nonempty and bounded to
512 UTF-8 bytes. The encoded query is bounded to 8192 bytes and 16 pairs; repeated
names are rejected so multiple consumers cannot disagree about the value. A GET
handshake never invokes extension code. Without a declared verification object,
GET follows ordinary signature authentication. The manifest must include GET
when declaring verification.

The legacy `hub_signature` manifest preset normalizes to this same generic auth
shape, with the existing header, `sha256=` prefix and `hub.*` query declarations.
Its deterministic normalization also emits the internal
`_legacy_compatibility: true` policy. The shared generic header decoder then
preserves optional/case-insensitive `sha256=` prefixes and existing whitespace
handling; the shared verification helper preserves existing query-value trimming.
Authored generic declarations cannot supply this internal policy. They retain
strict exact-prefix/exact-value semantics. There is one signer and one handshake
implementation; no provider-specific authentication engine remains.

Legacy source manifests keep their earlier compatibility floor. Bundle import
recompiles the original manifest before normalization, and installed compiled
artifacts load through the callback normalization boundary without reclassifying
the preset as a new explicit generic declaration. Existing persisted legacy
registration rows remain valid. `hmac_sha256`, shared-secret and OAuth callback
behavior stays compatible. Raw-body HMAC provides no host delivery-id replay protection;
extensions remain responsible for provider event deduplication where required.
This change adds no provider dispatch, routing, storage model or secret adapter.

All fields survive canonical compilation. Existing release descriptor hashes and
connection secret approvals bind the executable declaration; there is no separate
callback fingerprint system. Callback-family access log records redact the capability path segment and the
entire query, independently of the declared verification parameter names.

A registration remains scoped to its owning account,
project, connection, install and enabled state on both GET and POST.

## Database rollout

The callback registration auth-type CHECK gains `raw_body_hmac_sha256`. Fresh
installs receive the updated baseline, and installed 0.9.x releases receive the
same change through an idempotent SQL migration, pinned with its checksum and the
new baseline fingerprint in `db/released_migrations.json`. Earlier baseline
fingerprints remain supported upgrade sources. The existing database upgrade
command and forward-only recovery rules of ADRs 0012 and 0056 apply.

## Verification

Focused Docker tests cover valid raw-body signatures, custom and empty prefixes,
wrong keys, tampered bytes, missing keys, bounded and ambiguous verification
queries, registration lifecycle and ownership, compiler field validation and
round-trip preservation. Existing hub/OAuth callbacks remain in the owning test
selection. A disposable PostgreSQL test executes the migration twice, checks generic/legacy
INSERT and UPDATE behavior and unsupported-type rejection, and rolls back DDL and
fixture data. Migration immutability and baseline fingerprint tests additionally
cover the release artifacts.
