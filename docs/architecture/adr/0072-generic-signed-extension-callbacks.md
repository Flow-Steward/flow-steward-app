# ADR 0072: Generic Signed Extension Callbacks

- Status: Accepted — authoring contract frozen for extension host 1.9.0; Base64/HMAC verification runtime implementation and integration verification pending
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

The authoring contract is accepted and frozen. Package authors may use the exact
fields below and the canonical example in
`docs/standards/extension-manifest-standard.yaml`. This decision does not assert
that pending runtime amendments or integration verification have completed.

The new profile requires a connection-scoped callback and declares:

- `signature_header`: an HTTP token header name, at most 128 ASCII characters.
- `signature_prefix`: optional exact ASCII prefix, at most 64 characters; the
  default is the empty string. No whitespace trimming, interpolation or expression
  evaluation applies to the prefix.
- `signature_encoding`: `hex` (default) or `base64`, encoding the same 32-byte
  digest. Base64 uses strict canonical standard padded Base64; malformed,
  truncated, noncanonical, wrong-key or incorrect digest values are rejected.
- `secret_name`: a local secret field declared by an owning extension connection
  type's `secret_schema.properties`. It cannot name a project secret, arbitrary
  reference, provider locator, or another extension's secret.

The host verifies the decoded 32-byte SHA-256 digest after the exact prefix
(64 hexadecimal characters for the default hex encoding), using HMAC over the
received raw bytes. The existing capped body reader and connection secret resolver
remain the only owners of body limits and secret resolution. The registration's generated secret is never a signing key.
A missing or unavailable connection secret rejects the request. Signing material
is an opaque string: its leading/trailing whitespace is preserved exactly.

An optional `auth.verification` object declares a GET subscription handshake:

- `response`: `echo` (default) or `hmac`.
- `challenge_parameter`: a required exact ASCII query name.
- `token_parameter`: required for echo and optional for hmac. If supplied, its
  declaration must be a valid nonempty name distinct from the challenge name,
  and the request must match the registration-generated verify token exactly.
  Supplied empty, null or malformed fields are invalid, rather than omitted.
- `response_field`: required for hmac; a literal ASCII JSON response field name.
- Query and response field names follow `[A-Za-z_][A-Za-z0-9_.-]{0,63}`.
- `required_parameters`: an optional map (default `{}`) of at most eight other
  exact query names to nonempty literal strings of at most 512 UTF-8 bytes.
  These names cannot overlap the token or challenge names.

Echo returns the unchanged challenge as `text/plain`, after constant-time
verify-token comparison and exact required-parameter checks. HMAC response mode
returns the `application/json` object `{<response_field>: auth.signature_prefix +
encode(HMAC-SHA256(connection_secret, challenge_utf8))}`. It uses exactly the same
owning `auth.secret_name`, `auth.signature_encoding` and `auth.signature_prefix`
as body signing. There are no independent response key, encoding or prefix fields.
Without `token_parameter`, hmac verification needs no generated registration
verify token; the owning connection key provides the proof. No OAuth configuration
secret lookup or new secret storage lineage is introduced.

Required token and challenge values are nonempty and bounded to 512 UTF-8 bytes. The encoded
query is bounded to 8192 bytes and 16 pairs; repeated names are rejected. In hmac
mode, the nonce must match `[A-Za-z0-9._~+/=\-]{1,512}`. Reject JSON structures,
quoted strings, controls, whitespace and Unicode before resolving a signing key.
In the same mode, every signed POST body must parse as a JSON object, regardless
of Content-Type. Reject a correctly signed nonce, scalar, string, array or invalid
JSON. The accepted GET/POST message languages are disjoint: the verification
endpoint cannot sign a forged POST event for an unauthenticated caller. There is
no bypass option. Echo mode retains its existing raw-body POST behavior.

Invalid verification queries/tokens/nonces return 403; invalid signature
authentication returns 401; a correctly signed non-object POST in hmac verification
mode returns 400. Rejected requests never dispatch extension actions/events/workflows.
A GET handshake never invokes extension code, emits events or triggers workflows.
Without a verification declaration, GET follows ordinary signature authentication.
The manifest must include GET when declaring either verification response mode.
Unknown encoding/response modes, authored compatibility flags and independent
response signing fields are rejected.

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

Pending focused tests cover valid hex/Base64 raw-body signatures, custom and empty prefixes,
wrong keys, tampered bytes, missing keys, bounded and ambiguous verification
queries, registration lifecycle and ownership, compiler field validation and
round-trip preservation, strict Base64 parsing, HMAC JSON responses, optional
verify tokens and disjoint message languages with nonce rejection before key lookup.
Existing hub/OAuth callbacks remain in the owning test
selection. A disposable PostgreSQL test executes the migration twice, checks generic/legacy
INSERT and UPDATE behavior and unsupported-type rejection, and rolls back DDL and
fixture data. Migration immutability and baseline fingerprint tests additionally
cover the release artifacts.

The accepted authoring schema is independent of execution evidence. Runtime
implementation and central integration verification of the encoding and HMAC
response options remain pending; release readiness must be established against
the eventual combined candidate. No additional SDK, host-version or SQL/auth-type
migration change is part of these options.
