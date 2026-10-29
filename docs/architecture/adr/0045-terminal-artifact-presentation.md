# ADR 0045: Terminal presentation claims for prepared extension artifacts

- Status: Accepted by project owner for the bounded MAIL-IMAP001 correction
- Date: 2026-10-28
- Related: ADR 0039, ADR 0040

## Context

An extension can learn an attachment's filename and MIME only after reading it.
The SDK 0.4 output capability already fixes a generic filename and upload MIME.
Ordinary result metadata cannot amend that capability or confer artifact access.
Task 62 was a direction, not an adopted wire or persistence contract.

## Decision

An installed operation's artifact output item may opt one binding into
`presentation: {claim_output: attachment_presentation}`. There is at most one
opted-in presentation binding per operation/invocation; other outputs keep their
existing contracts. Invalid/multiple/conflicting declarations fail closed.
Caller output specs and terminal data cannot opt themselves in. The host captures
the declaration and exact fresh minted output ID/binding, original object key and
owner scope in an immutable preparation value before invoking the provider.
Later install edits do not reinterpret the in-flight invocation.

Only a successful terminal invocation with `response.ok=true` may return the
named `response.result` claim. Its exact six fields are `artifact_id`,
`binding_key`, `filename`, `content_type`, `size_bytes`, `sha256`. Identity must
match the captured fresh output and the host-finalized original row, including
owner, object key, size and SHA-256. The durable original owner and object key
are checked again immediately before and after the atomic presentation commit;
early-finalized cache rows cannot authorize a claim after owner transfer. By-reference/input/different artifacts are
not eligible. Size is an integer, never a boolean; digest is canonical lower hex.
Missing claims retain generic display for compatibility; malformed present claims
or persistence failures prevent final-success output publication.

The shared host finalizer covers direct actions, workflow connector operations
and direct invocations, including normalized remote terminal responses. Early
ID-only `output_ready` remains unchanged. Failed, timed-out and unknown outcomes
do not commit presentation. The public SDK 0.4 write receipt is sufficient; this
does not invent an SDK release or copy SDK code into application/extensions.

Accepted presentation is one immutable JSON sidecar under the original artifact
prefix, `artifacts/<id>/_presentation.json`. Existing atomic `put_if_absent`
provides one complete visible value on local filesystem and S3. Local conditional
publication writes and fsyncs a sibling temporary file, then atomically links it
to the final no-replace name and removes the temporary name. Failed publication
never exposes a partial final key. Byte-identical
replays succeed; conflicts fail. No multi-key transactional claim is made.
Original metadata, bytes, object key, upload MIME, ownership, producer provenance,
expiry, retention and signed grant truth stay unchanged. No database schema changes
are needed. Generic later finalization cannot erase the sidecar.

Readers first require the original artifact and its existing ownership/access
policy. A sidecar is usable only while original identity, finalized hash/size,
availability and expiry match. Canonical metadata lookup is sidecar-free. A separate reader is invoked only
after original authorization; it returns nested `presentation` in the public
display view and never replaces canonical capability fields. Public display/download views apply it only
after original authorization. The grant signer ignores nested presentation.
An orphan or mismatched sidecar cannot establish existence, availability, read
grants or ownership. Original lifecycle cleanup also removes the sidecar.

Filename is a bounded basename without path separators, traversal segments, NUL,
CR/LF or other control characters. MIME comes from an explicit non-active set;
HTML/SVG/script formats are not admitted. It is display/download metadata, never
authority for execution, content safety or access. Inline preview eligibility uses
the canonical original MIME, so a presentation claim cannot activate a new preview.
The existing preview allowlist, CSP and size limits remain unchanged. The host browser-S3 GET presigner
uses an explicit accepted response MIME override; signed PUT and runtime input
grant types are unaffected. Authorized local downloads use the same presentation.

## Verification

Owning tests cover real SDK bytes/receipts through a direct host terminal consumer,
fresh process readers, early-finalized cache reuse, installed-policy snapshotting,
unsafe or mismatched claims, identical/conflicting replay, no opt-in, failed and
unknown status, lifecycle deletion, and local/S3 persistence/download behavior.
Behavioral gate and UI acceptance evidence are recorded separately; this ADR is
not a claim those checks have run or passed.
