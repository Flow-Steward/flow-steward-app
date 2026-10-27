# ADR 0023: Extension-Owned OAuth Boundary

## Status

Accepted — 2026-08-30

The current implementation includes the provider-declared protocol variance
described by ADR 0024, which remains Proposed pending project-owner ratification.
Clause 2 describes the implemented PKCE modes, and clause 5's endpoint permissions
cover a declared dynamic origin as well as a fixed URL. This records current
behavior without treating ADR 0024 as an accepted amendment; the extension-owned
boundary itself is unchanged.

## Context

The predecessor design placed one motivating consumer's application credentials and
post-connect policy in provider-specific core services. That boundary made each new
OAuth provider require core code, global provider records, and generic UI branches. It
also prevented an extension from describing its complete project setup experience.

OAuth protocol handling still crosses a security boundary. Extensions cannot safely
receive authorization codes, client secrets, refresh tokens, reusable state, or
unrestricted endpoint URLs merely because they own provider-specific behavior.

## Decision

1. An installed extension owns provider display metadata, credential-field schemas,
   endpoint permissions, connection-type scopes, named authorization intents, setup
   content, and an optional post-connect action. Provider IDs are manifest-local and
   resolve only as `(extension_install_id, provider_id)`.
2. Core remains provider-neutral. It owns project-scoped configuration persistence,
   encrypted secret references, RBAC and lineage enforcement, one-time state,
   validation and enforcement of provider-declared PKCE (`s256` by default, or
   explicit `none` for authorization servers that reject PKCE parameters), callback
   ingress, bounded token exchange/refresh/revocation, token storage,
   and disable/delete/uninstall lifecycle enforcement.
3. V1 supports only OAuth 2.0 Authorization Code with project-owned configurations.
   OAuth 1.0, device flow, client credentials, service accounts, dynamic client
   registration, deployment credentials, shared configurations, and login federation
   are outside this decision.
4. `oauth_authorization` is a reserved, project-scoped callback purpose. Core consumes
   it without an extension action or ordinary callback authentication. The extension
   never receives the authorization code, client secret, refresh token, or raw token
   response.
5. Manifest endpoint origins and authorization parameter mappings are security
   permissions. Core validates them at install/upgrade, pins flows to the release and
   configuration authorization revision, and requires approval for security-impacting
   changes.
6. Each project connection references one opaque configuration ID and retains its own
   requested/granted scopes, tokens, health, and disconnect lifecycle. Core never
   unions scopes, rebinds a connection, or treats a display name or user-entered hint
   as identity.
7. A post-connect action receives only the selected connection's current scoped access
   token through the existing redacted runtime target. Its output is accepted only
   through a closed provider-neutral identity/health result contract. The referenced
   action must be extension-handled, non-mutating, workflow-hidden, parameterless,
   and absent from external-effect declarations; install validation rejects any
   broader automatic action profile.

## Consequences

- Provider setup and guidance can live with the declaring extension while generic core
  code serves unrelated OAuth providers without provider conditionals.
- Two extensions may use the same local `provider_id` without identity collision.
- Projects may hold multiple named configurations for one provider; names remain
  cosmetic and secrets remain write-only.
- Endpoint, credential, release, state, and lifecycle validation enlarge the generic
  core security surface, but provider APIs and UX do not.
- The superseded ADR 0022 was removed from the live documentation.
