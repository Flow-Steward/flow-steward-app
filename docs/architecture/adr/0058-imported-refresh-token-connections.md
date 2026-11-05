# ADR 0058: Connections From an Imported Refresh Token

- Status: Proposed — 2026-11-05 (amends ADR 0023 clause 3; needs project-owner approval)
- Date: 2026-11-05
- Related: [ADR 0023](0023-extension-owned-oauth-boundary.md),
  [ADR 0024](0024-provider-declared-oauth-protocol-variance.md) (Proposed)

## Context

ADR 0023 lets an extension declare an OAuth provider while Core keeps every
credential: Core runs the authorization-code flow, stores the tokens, refreshes
them under a lock, and hands an operation only the current access token.

Some providers issue the refresh token outside any redirect. A private Amazon
Selling Partner API application is authorized by the seller in Seller Central
("self-authorization"); the seller copies a long-lived refresh token from there,
and the application exchanges it at the LWA token endpoint with
`grant_type=refresh_token`. There is no authorization endpoint the platform could
redirect to and no callback to receive. The same shape appears wherever a console
issues a refresh token for a first-party application.

Without Core support, such an extension has two bad options: keep the refresh token
and client secret as ordinary connection secrets and mint access tokens itself on
every call (the extension then holds exactly the credentials ADR 0023 keeps from
it, and nothing caches or serializes the refresh), or put provider code in Core.

Everything after the first token exchange already exists: encrypted token
references, refresh with expiry skew, the refresh claim, rotated-token storage,
access-token-only delivery, the post-connect action, and the disconnect and
uninstall lifecycle. What is missing is one way to start a grant.

## Decision

1. **A second flow type.** A provider may declare
   `flow: {type: refresh_token_import}`. The vocabulary stays closed:
   `authorization_code` (ADR 0023) and `refresh_token_import` are the only values.
   ADR 0023 clause 3 is amended accordingly; the other flows it excludes stay
   excluded.

2. **What the manifest declares for it.** An import provider declares
   `display_name`, `description`, `setup_page_id`, `help_page_id`,
   `credential_fields` (the client credentials, unchanged), `token_endpoint`, and
   optionally `error_help_anchors`, `post_connect_action_id` and
   `revocation_endpoint`. It must not declare `authorization_endpoint`,
   `callback_id`, `authorization_intents`, `origin_binding`, `code_exchange`,
   `callback_verification`, `scope_separator` or `pkce`, and
   `unique_subject_per_configuration` must be false: there is no redirect, no
   callback, no tenant host and no protocol variance to describe, and a subject is
   known only after the post-connect action runs. A connection type bound to an
   import provider may leave `scopes.required` empty; when it is empty, Core
   records no granted scopes rather than inventing them.

3. **How a grant starts.** An operator submits, for one configuration:
   a display name, the refresh token (write-only), the connection's non-secret
   settings, and optionally an existing connection to replace. Core then, in order:
   checks the operator role and project access; locks the configuration and checks
   its release, security fingerprint, status and credential fields exactly as
   authorization start does; validates the settings against the connection type's
   `config_schema` (the executed subset, nothing else stored); performs one
   refresh exchange at the declared token endpoint with the configuration's client
   credentials; and on success commits the grant through the same code path as
   the authorization-code callback — token write (the submitted token is kept when
   the provider does not rotate it), connection create or guarded update,
   configuration verified, post-connect action. The exchange happens before any
   row changes, so an invalid token leaves nothing behind.

4. **Replacing a grant.** Importing into an existing connection requires the
   connection's current credential revision, matched under lock, like
   reauthorization. The connection keeps its ID, so workflows that reference it
   keep working. Settings submitted with a replacement are validated and replace
   the stored ones; the `oauth` partition stays Core-owned.

5. **What does not change.** The extension never receives the refresh token, the
   client secret or the raw token response. Refresh, the refresh claim, rotated
   token storage, access-token delivery and the lifecycle are the existing ones.
   Core contains no provider name, endpoint or field. There is no generic HTTP
   proxy that substitutes secrets and no second token store.

6. **Browser projection.** The browser manifest projection exposes the flow type
   of each provider and the `config_schema` of its connection types, so the setup
   page can show a paste-token form instead of an authorize button. The refresh
   token is a write-only field and is never returned.

## Consequences

- A provider whose console issues refresh tokens can be supported by an extension
  alone, with Core keeping the credentials.
- The token a user pastes is as sensitive as one obtained by redirect and gets the
  same storage; it travels through one more API route, which is operator-only and
  never echoes it.
- A grant that skipped the provider's consent screen proves only that the token
  works. Identity (a seller or account ID) is whatever the post-connect action
  confirms, never the settings the user typed.
- Existing authorization-code providers are unaffected: their compiled flow and
  security fingerprint stay byte-identical.
- A provider that rotates the refresh token on every exchange can retire the pasted
  token even when the grant is then not committed (the configuration or connection
  changed between the exchange and the commit). Core re-checks the lineage
  immediately before the exchange to keep that window small, and when a commit
  fails after a response that carried a new refresh token it answers with the
  distinct `oauth_import_conflict_token_rotated`, telling the operator that a new
  token may be needed.
- Replacing a grant keeps the connection bound to the subject it had. If the
  post-connect action verifies a different subject, Core keeps the old one and
  marks the connection `needs_attention` with the closed code `subject_changed`;
  the same rule now applies to a redirect reauthorization whose provider does not
  sign its callback (a signed callback is already held to its subject). A failed
  verification of an imported replacement keeps the old subject as well.
- Only settings the connection type's `config_schema` declares are accepted and
  stored; a type without one accepts none. Replacing a grant replaces the declared
  settings and leaves any other existing key of `connection_config` untouched.
- An `invalid_client` answer during an import marks the configuration
  `needs_attention`, as runtime refresh and the callback do.
