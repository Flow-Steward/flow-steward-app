# ADR 0024: Provider-Declared OAuth Protocol Variance

## Status

Proposed — 2026-09-04

Proposed amendment to ADR 0023. The current implementation already includes the
variance described below; implementation does not establish project-owner
ratification. The extension-owned OAuth boundary stands; this record proposes
narrowing two of its clauses and recording the runtime responsibilities that
variance requires.

## Context

ADR 0023 assumed every provider speaks one dialect of OAuth 2.0: PKCE S256, fixed
absolute endpoint URLs, space-separated scopes, an RFC 6749 exchange body, and a
callback whose only proof is one-time state.

Real authorization servers diverge from that in ways an extension cannot absorb,
because the divergence is in the parts core owns:

- a server that rejects the PKCE parameters instead of ignoring them;
- a tenant-per-host model, where the endpoint is not knowable until the operator
  names the tenant;
- comma-separated scopes;
- an exchange that refuses `grant_type` and `redirect_uri` and takes its own
  fields instead;
- a token response that omits `token_type` and documents fields whose absence
  should be an error rather than a gap to fill;
- a signed callback, whose signature is the only evidence of which tenant the
  grant belongs to.

The previous options were both bad: put the provider's dialect in core with a
conditional, or let the extension take over parts of the flow that ADR 0023
deliberately keeps away from it — authorization codes, client secrets, tokens.

## Decision

1. Protocol variance is **declared data, not code**. A provider manifest may
   declare its PKCE mode, scope separator, exchange body profile, required
   token-response fields, and callback signature policy. Every one of these draws
   from a closed vocabulary that core validates at install; a manifest cannot
   introduce a value core does not already implement, and cannot supply an
   expression, a template, or a callable.

2. An endpoint is addressed one of exactly two ways: a fixed absolute HTTPS
   `url`, or a declared `origin_binding` plus a fixed absolute `path`. There is
   no third form. A bound endpoint's path may not carry a query, a fragment, a
   traversal segment, or a scheme-relative prefix.

3. A dynamic origin is **not** a URL template. The provider declares a fixed host
   suffix and a bounded label alphabet chosen from a closed set; core joins a
   validated label to that suffix and can reach no other host. No manifest text
   reaches a regular-expression engine, so no manifest can cost anything to
   evaluate.

4. The authorization input that determines the origin is **never** also emitted
   as an authorization query parameter. The value that decides where a request
   goes must not be reflected into the request it addresses.

5. The origin is resolved before the authorization URL is built, stored with the
   one-time state, and is the only origin the callback and the later refresh may
   use. Neither reads a host from workflow input, callback input, or a header.

6. A provider may declare that its callback is signed. Core computes one
   canonical message from the callback's own query, verifies it against the
   configuration's client secret, and treats the declared subject parameter as
   the verified identity of the grant. The subject is normalized through the
   provider's own origin policy and must equal the origin the flow started
   against.

7. Callback processing is ordered: read the one-time state **without** consuming
   it, verify signature and subject, then consume it atomically immediately
   before the exchange. A forged signature or a mismatched subject must not spend
   a state the legitimate callback still needs. Any repeated query parameter is
   refused, because a duplicate makes the canonical message ambiguous.

8. **New runtime responsibility.** A provider may declare that it keeps one token
   per configuration and subject. For such a provider every operation that mints a
   token — a new grant and a refresh alike — is serialized under one claim keyed
   on `(configuration_id, verified_subject)`. It is keyed on the subject rather
   than on a connection because the constraint belongs to the tenant, not to the
   row holding its token. A provider that declares the rule and finds no claim
   available fails closed.

9. Preservation of a prior authorization is decided by **phase**, not by error
   code. Only a provider that answered and refused before the exchange proves the
   previous credentials are intact. Once the exchange completes, nothing is
   promised about the authorization it replaces, whatever code follows.

10. **New shared abstraction.** One HMAC-SHA256 primitive serves both signed
    webhook ingress and signed OAuth callbacks. It exposes an incremental signer
    as well as a one-shot digest, because webhook ingress streams a body it must
    not buffer. It lives in `core/infrastructure/security/` with no HTTP
    dependency, so importing it does not pull the HTTP layer into roles that have
    none.

11. Core remains provider-neutral. No provider hostname, scope, endpoint, token
    field, or error code appears in core; the boundary check enforces this and
    every conformance test for these capabilities runs against a neutral fixture
    provider, never a real one.

## Consequences

- A provider whose dialect differs is onboarded by declaration. Core gains no
  conditional and no knowledge of who is asking.
- The closed vocabularies are the limit: a genuinely new variance still needs a
  core change and an amendment here, which is the intended cost.
- The security surface grows — a signature policy, a dynamic origin, and a
  cross-operation lock are each new things to get right — while the provider
  surface does not.
- Serializing grant and refresh introduces a lock held across an outbound
  request. It is a session-scoped advisory claim rather than a database
  transaction for that reason, and it is taken only by providers that ask for it.
- ADR 0023 clause 2 no longer reads "PKCE S256" unconditionally, and its clause 5
  covers bound origins as well as fixed ones.
