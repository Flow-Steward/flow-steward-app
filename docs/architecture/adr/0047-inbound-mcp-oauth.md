# ADR 0047: Inbound OAuth for the Flow Steward MCP server

- Status: Proposed
- Date: 2026-10-31
- Related: ADR 0012, ADR 0020, ADR 0023, ADR 0029, ADR 0033, ADR 0034
- Scope: task 5, stage 5A (contract). Stage 5B implements exactly this record.

## Context

Cloud agents (ChatGPT custom MCP, Claude.ai custom connectors and Cowork, which
uses the Claude.ai connector path) cannot send a personal access token (PAT).
They connect to a remote MCP server only through OAuth 2.1 authorization code +
PKCE, and both prefer Client ID Metadata Documents (CIMD) for client
identification. The MCP specification 2026-07-28 makes RFC 9728 and RFC 8414
discovery mandatory, requires PKCE S256 and RFC 8707 `resource` binding,
recommends RFC 9207 `iss`, marks CIMD as SHOULD and Dynamic Client Registration
(DCR) as deprecated, and requires refresh-token rotation for public clients.

State of Flow Steward 0.9.6:

- `fs-mcp` is a resource server only. `server_runtime.py` builds
  `AuthSettings(issuer_url, resource_server_url, validate_token_resource=False)`
  and `AppApiTokenVerifier` (`server_api_auth.py`), which introspects every
  bearer through `GET /api/auth/introspect` with no cache and returns an
  `AccessToken` without `resource`, `subject` or `claims`.
- `server_public_identity.py::mcp_auth_urls` derives issuer = public origin of
  `FS_PUBLIC_BASE_URL` (no path, no default port) and resource = `{origin}/mcp`.
  Both ingresses (`infra/traefik/fs.dynamic.yaml`, Compact
  `core/infrastructure/runtime/compact/router.yaml.tmpl`) route only `/mcp*` and
  `/.well-known/oauth-protected-resource*` to `fs-mcp`; every other path,
  including `/.well-known/oauth-authorization-server`, reaches `fs-app`.
- The application has no authorization, token, revocation or registration
  endpoint. `mcp_connector_oauth_*` and `oauth_client*` in
  `core/application/auth/` are Flow Steward acting as an OAuth *client* of other
  services, including the public CIMD it publishes at
  `/api/mcp/oauth/client-metadata/<id>.json`; they are unrelated to this record.
- PATs (`db/schema/001_schema_part_02.sql`, `personal_access_tokens`) store a
  SHA-256 `token_hash` of an `fs_pat_` token, `scopes_jsonb`,
  `allowed_project_ids_jsonb`, `allowed_workflow_ids_jsonb`, `expires_at`,
  `last_used_at`, `revoked`. `resolve_pat`
  (`core/infrastructure/db/logger_extensions_accounts_pat_mixin.py`) returns
  principal type `user` with live roles, `auth_source="pat"`, and checks
  `is_active`, expiry, revocation and account membership on every request.
- `local_auth_middleware` (`core/application/auth/auth_http_runtime.py`)
  resolves a bearer as session, then PAT, then machine token. A principal whose
  `auth_source` is in a hard-coded token set is authorized by its stored scopes;
  every other principal is authorized by role. Eight places hold that set
  (listed in decision 12). An unlisted new source silently receives role
  authority, and `/api/auth/introspect` would synthesize `admin:all` for an
  administrator.
- `ResolvedPrincipal` (`core/application/auth/principal.py`) is
  `extra="forbid"` and `_normalize_principal_dict` copies named fields only, so
  token-specific keys such as `pat_id` never reach `request.state.auth_user`.
- In the middleware the local variable `auth_source` is `"local"` for every
  session, PAT and machine bearer, so `is_self_service_auth_write_route`
  (`/api/auth/tokens`, `/api/auth/tokens/{id}/revoke`, change-password,
  e-mail change, logout) skips the role gate for tokens. A token holding
  `command:*` or `admin:all` can mint a PAT with no expiry.
- MCP forwards the caller's own bearer to the canonical API
  (`server_caller_identity.py::current_caller_bearer`,
  `server_api_client.py`), only to the fixed `MCP_TOOL_ROUTES`. The MCP
  specification forbids token passthrough to an upstream API.
- The Scalable `fs-mcp` image (`infra/mcp/Dockerfile`) copies only
  `core/entrypoints/mcp/`, `core/infrastructure/runtime_env.py` and
  `core/infrastructure/mcp_dev_proxy.py`.

## Decision

### 1. Scope

Flow Steward implements a minimal OAuth 2.1 authorization server inside
`fs-app` for one protected resource, its own MCP endpoint. Supported: the
authorization code grant with PKCE S256 for public clients identified by CIMD,
the refresh token grant with rotation, and RFC 7009 revocation.

Not implemented: DCR (`/register`), pre-registered or confidential clients,
client secrets, `private_key_jwt`, `client_credentials`, JWT bearer / ID-JAG
(SEP-990), OpenID Connect (`id_token`, `openid-configuration`), token
introspection or other endpoints for third parties, external identity providers,
any second protected resource. Requirements of the public catalogs (universal
URL, verified review, template URL) are not part of this record.

Local clients that send a static bearer header keep using PATs. A local
client that happens to support CIMD with a loopback redirect may use OAuth as a
side effect.

### 2. Readiness and the blocker

The authorization server is available only when all of the following hold. One
application function, `inbound_oauth_readiness()`, evaluates them and returns
fixed blocker codes:

| Condition | Blocker code |
|---|---|
| `FS_PUBLIC_BASE_URL` yields an `https` origin whose host is not `localhost`, `*.localhost`, `*.local`, a single label, or a loopback, private, link-local or unspecified IP literal | `public_https_required` |
| Issuer and resource derived by the shared public-identity function equal `{origin}` and `{origin}/mcp`; any `FS_MCP_ISSUER_URL` / `FS_MCP_RESOURCE_URL` override visible to `fs-app` equals them | `issuer_mismatch` |
| `FS_MCP_FORWARD_SECRET` is set and at least 32 characters | `forward_secret_missing` |
| The five OAuth tables exist (lazy check, same pattern as `pat_tables_available`) | `storage_missing` |
| The account setting `mcp_oauth` has `enabled: true` (decision 16) | `disabled_by_admin` |

When not ready: `/.well-known/oauth-authorization-server` answers 404 with
`{"error": "oauth_unavailable"}`, `/oauth/authorize` redirects to the React
route `/oauth/consent?error=<blocker code>`, `/oauth/token` answers
`temporarily_unavailable`, and already issued OAuth tokens are rejected. The
Settings section (decision 16) and the task 4 wizard show the same blocker with
an explanation. PATs are never affected. The public-HTTPS test is the same
function task 4 uses for its cloud-client blocker; whichever task lands first
creates it in `core/application/auth/`.

### 3. Discovery

Issuer = `{origin}`, resource = `{origin}/mcp`, from one function moved from
`core/entrypoints/mcp/server_public_identity.py` to
`core/infrastructure/public_identity.py` (standard library only). The MCP
module re-exports it; `infra/mcp/Dockerfile` copies the new file. `fs-app`
imports the same function, so the two processes cannot disagree.

Protected resource metadata, served by `fs-mcp` at
`/.well-known/oauth-protected-resource/mcp`:

```json
{
  "resource": "https://fs.example.com/mcp",
  "authorization_servers": ["https://fs.example.com"],
  "scopes_supported": ["projects.read", "jobs.read", "jobs.write", "reports.read",
                       "reports.write", "workflows.read", "workflows.write", "admin:all"],
  "bearer_methods_supported": ["header"],
  "resource_name": "Flow Steward"
}
```

A 401 from `/mcp` carries
`WWW-Authenticate: Bearer error="invalid_token", resource_metadata="…", scope="<the same eight scopes>"`.
The SDK registers its own PRM route before any custom route and sets
`scopes_supported` only from `required_scopes`, which it then demands on every
request; neither can express this. `fs-mcp` therefore wraps the SDK Starlette
app in a thin ASGI adapter that answers the PRM `GET` itself (SDK
`ProtectedResourceMetadata` model) and appends `scope` to the SDK 401 header.
`server_main.py` builds the app with `mcp.streamable_http_app(...)` and serves
it with uvicorn exactly as `run_streamable_http_async` does. The stdio
transport is unchanged.

Authorization server metadata, served by `fs-app` at
`/.well-known/oauth-authorization-server` (GET, CORS `*`):

```json
{
  "issuer": "https://fs.example.com",
  "authorization_endpoint": "https://fs.example.com/oauth/authorize",
  "token_endpoint": "https://fs.example.com/oauth/token",
  "revocation_endpoint": "https://fs.example.com/oauth/revoke",
  "response_types_supported": ["code"],
  "response_modes_supported": ["query"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none"],
  "revocation_endpoint_auth_methods_supported": ["none"],
  "client_id_metadata_document_supported": true,
  "authorization_response_iss_parameter_supported": true,
  "scopes_supported": ["projects.read", "jobs.read", "jobs.write", "reports.read",
                       "reports.write", "workflows.read", "workflows.write", "admin:all"]
}
```

No `registration_endpoint`, no `offline_access` (a refresh token is always
issued; an `offline_access` scope in a request is accepted and ignored),
no `/.well-known/openid-configuration`.

### 4. Endpoints

| Path | Process | Method | Auth | Purpose |
|---|---|---|---|---|
| `/.well-known/oauth-protected-resource/mcp` | fs-mcp | GET | public | RFC 9728 (decision 3) |
| `/.well-known/oauth-authorization-server` | fs-app | GET | public | RFC 8414 (decision 3) |
| `/oauth/authorize` | fs-app | GET | public | validate, store a pending authorization, set the browser-binding cookie, 302 to `/oauth/consent?request=<id>` |
| `/oauth/consent` | fs-app (React shell) | GET | session (existing login redirect with `next`) | consent and error page |
| `GET /api/oauth/authorizations/{id}` | fs-app | GET | session cookie | consent view model |
| `POST /api/oauth/authorizations/{id}/decision` | fs-app | POST | session cookie + CSRF | approve or deny; returns `{"redirect_to": …}` |
| `/oauth/token` | fs-app | POST form | public client (`client_id`) | `authorization_code`, `refresh_token` |
| `/oauth/revoke` | fs-app | POST form | public client (`client_id`) | RFC 7009 |
| `GET /api/auth/oauth-grants` | fs-app | GET | session cookie | the user's grants plus readiness |
| `POST /api/auth/oauth-grants/{id}/revoke` | fs-app | POST | session cookie + CSRF | revoke one grant |
| `GET`/`PUT /api/settings/mcp-oauth` | fs-app | GET/PUT | admin session + CSRF | account switch (decision 16) |

`/.well-known/oauth-authorization-server`, `/oauth/authorize`, `/oauth/token`
and `/oauth/revoke` join `AUTH_PUBLIC_PATHS`. The `/oauth/*` prefix keeps the
inbound server apart from the outbound `/api/mcp/oauth/*` client paths and from
root paths such as `/token`. `/oauth/token` and `/oauth/revoke` never derive
identity from a cookie. The `/api/oauth/*`, `/api/auth/oauth-grants*` and
`/api/settings/mcp-oauth` routes accept interactive principals only
(`auth_source` `local` or `external`); any bearer token, PAT and OAuth included,
gets 403.

### 5. Client identification (CIMD only)

Owner default, questions file row 2. `client_id` is an HTTPS URL; the
authorization server fetches the document itself.

Amended by [ADR 0063](0063-microsoft-365-copilot-connection.md) decision 2: an
administrator can also register a public platform client from a fixed preset
(Microsoft 365 Copilot); its opaque `client_id` and preset redirect URIs replace
the CIMD fetch, and everything after the client check stays as written here.

- Allowed `client_id`: `https`, host exactly `chatgpt.com` or `claude.ai`
  (fixed in code, no subdomains; row 13), non-empty path, no userinfo, query or
  fragment, at most 2048 characters. Anything else gets the error page
  `client_not_allowed`.
- Fetch: through `core/application/extensions/url_safety` pinned-peer opening
  and `read_bounded_http_body`; `GET`, `Accept: application/json`, 5 s timeout,
  64 KiB limit, redirects are failures, public peers only regardless of
  `FS_ALLOW_PRIVATE_REMOTE_URLS`.
- Validation: a JSON object; `client_id` equal to the URL by exact string
  comparison; `redirect_uris` a non-empty list of absolute `https` URIs or
  `http` loopback URIs (`127.0.0.1`, `[::1]`, `localhost`), no fragments;
  `token_endpoint_auth_method` absent or `none`; `grant_types`, if present,
  within `authorization_code`, `refresh_token` and containing
  `authorization_code`; `response_types`, if present, containing `code`.
  `client_name` is optional; when absent the consent page shows the host. The
  document `scope`, `logo_uri` and other fields are ignored; no remote image is
  rendered.
- Cache: one `oauth_clients` row per `client_id`, refreshed at `/oauth/authorize`
  when `cache_expires_at` has passed. Lifetime from `Cache-Control: max-age`,
  clamped to 5 minutes – 24 hours, default 1 hour. A failed refresh keeps a
  still-valid cached row and otherwise refuses. `/oauth/token` and
  `/oauth/revoke` use the cached row and never fetch.
- Redirect URI matching: exact string equality with an entry of the document,
  except loopback URIs, which match ignoring the port (RFC 8252 §7.3).
- `fs-app` needs outbound HTTPS to `chatgpt.com` and `claude.ai`. A fetch
  failure is shown as `client_metadata_unavailable`.

### 6. Authorization request and consent

`/oauth/authorize` accepts `response_type=code`, `client_id`, `redirect_uri`
(required), `code_challenge` (43–128 characters, base64url),
`code_challenge_method=S256` (required; `plain` or absent → `invalid_request`),
`resource` (required), `scope` (optional), `state` (optional, at most 1024
characters, echoed verbatim).

Order of checks: readiness; client and document (errors before this point go to
the React error page, never to a redirect URI); `redirect_uri`; then every
remaining error is a redirect to `redirect_uri` with `error`, `state` and `iss`.

1. `resource` is compared after normalization (lower-case scheme and host,
   default port removed, one trailing `/` removed, no query or fragment). Not
   equal to this installation's resource → `invalid_target`.
2. `scope` split on spaces; `offline_access` dropped; every other value must be
   one of the eight scopes → otherwise `invalid_scope`. An empty or absent scope
   means the operational preset's seven scopes.
3. The pending authorization is stored (10 minutes) with a random 128-bit
   browser-binding value whose SHA-256 is stored and whose raw value is set as
   the `HttpOnly; Secure; SameSite=Lax` cookie `fs_oauth_ar`, path `/`. The
   response is a 302 to `/oauth/consent?request=<id>`. Nothing from the request
   is placed in the login `next` URL other than that path.
4. The consent page (after the existing login, if needed) loads
   `GET /api/oauth/authorizations/{id}`. The decision call requires the cookie to
   match the stored hash, the pending row to be unexpired and undecided, and an
   interactive session.
5. The consent contract is decision 15. Approve runs
   `enforce_token_scope_within_caller_authority(session_user, scopes, projects, [])`
   and, in one transaction that marks the pending row decided, stores the code
   hash and the agreed access. The response is
   `redirect_uri?code=…&state=…&iss=<issuer>`.
6. Deny → `error=access_denied` with `state` and `iss`. A flow abandoned before
   the decision leaves only the expiring pending row; no code and no grant
   exist.

### 7. Code exchange

`POST /oauth/token`, `application/x-www-form-urlencoded`, `grant_type=authorization_code`,
`code`, `client_id`, `redirect_uri`, `code_verifier`, optional `resource`.
Responses carry `Cache-Control: no-store` and `Pragma: no-cache`; errors are
RFC 6749 §5.2 JSON (400, or 401 for `invalid_client`).

One transaction:

1. `UPDATE oauth_authorizations SET code_consumed_at = now() WHERE code_hash = $1
   AND code_consumed_at IS NULL RETURNING …`. No row → look up by `code_hash`; if
   it was already consumed, revoke the grant created from it (reason
   `code_reuse`) and answer `invalid_grant`; if unknown, `invalid_grant`.
2. With the code now consumed whatever follows: expired, `client_id` different,
   `redirect_uri` not byte-equal to the authorize value, or
   `BASE64URL(SHA256(code_verifier))` not equal to the challenge (`code_verifier`
   43–128 unreserved characters) → `invalid_grant`.
3. `resource`, when sent, normalized and different from the code's →
   `invalid_target`. When absent the code's resource is used; it is already
   bound.
4. The user is re-resolved (active, member of the account, account active) →
   otherwise `invalid_grant`.
5. Exactly one grant, one access token and one refresh token are created. The
   grant id is written back to the authorization row.

Response: `access_token`, `token_type="Bearer"`, `expires_in`,
`refresh_token`, `scope` (granted scopes, space-separated).

### 8. Refresh, rotation and reuse detection

`grant_type=refresh_token`, `refresh_token`, `client_id`, optional `scope`,
optional `resource`. One transaction:

1. Lock the grant row (`SELECT … FOR UPDATE` through the refresh token's
   `grant_id`).
2. `UPDATE oauth_refresh_tokens SET consumed_at = now() WHERE token_hash = $1
   AND consumed_at IS NULL RETURNING …`. If the token exists but was already
   consumed: revoke the grant (reason `refresh_reuse`), audit, `invalid_grant`.
   Unknown token → `invalid_grant`.
3. Grant revoked or `expires_at <= now()`, client different, user or account
   inactive or membership gone, OAuth not ready → `invalid_grant` (readiness →
   `temporarily_unavailable`).
4. `scope`, when sent, must be a subset of the grant scopes (`invalid_scope`
   otherwise); the new access token carries that subset, the grant never
   changes. `resource`, when sent, must equal the grant's (`invalid_target`).
5. New access token and new refresh token (`parent_id` = the consumed one),
   both no later than `grant.expires_at`.

Refresh never extends `grant.expires_at`, never widens scopes or projects, and
never creates a grant. Concurrent refreshes with the same token: the first
`UPDATE` wins, the second is a reuse and revokes the grant. No grace window
(row 11). Refresh and revocation serialize on the grant row lock; a refresh
committed just before a revocation yields tokens that the revocation then
invalidates, because every use checks the grant.

Authority follows the PAT policy: live roles, `is_active`, membership and
account state on every request; scopes are capped against the caller's
authority only at consent. Losing a role takes effect immediately through the
role gates; the grant is not re-capped at refresh.

### 9. Revocation and expiry

- `/oauth/revoke` (`token`, `client_id`, optional `token_type_hint`): an access
  or refresh token of this client revokes its whole grant (reason
  `client_revoked`). Unknown token, other client's token or an already revoked
  grant → 200 with an empty body, per RFC 7009.
- Settings revocation sets `revoked_at` (reason `user_revoked`).
- Every resolution of an access token and every refresh checks
  `grant.revoked_at IS NULL AND grant.expires_at > now()` in the same query.
  There is no cache in `fs-app` or `AppApiTokenVerifier`; revocation is
  immediate for both access and refresh tokens.
- Deleting a user cascades to the grants.

### 10. Token formats and lifetimes

| Item | Format | Stored as | Lifetime |
|---|---|---|---|
| Pending authorization | UUID in the consent URL, plus `fs_oauth_ar` cookie (128 random bits) | row id; SHA-256 of the cookie value | 10 minutes, one decision |
| Authorization code | `fs_oac_` + `token_urlsafe(32)` | SHA-256 hex | 60 seconds, one use |
| Access token | `fs_oat_` + `token_urlsafe(32)` | SHA-256 hex | `min(60 minutes, grant remaining)` |
| Refresh token | `fs_ort_` + `token_urlsafe(32)` | SHA-256 hex | until `grant.expires_at`, one use |
| Grant | row | — | chosen at consent: 7, 30 or 90 days, maximum 90 (row 3) |

Tokens are opaque; no signing keys exist. Introspection is already the only
validation path, so revocation needs no denylist. Prefixes route a bearer: an
`fs_oat_` bearer is resolved only as an OAuth access token and never as a
session, PAT or machine token; `fs_oac_` and `fs_ort_` values are never accepted
as bearers. Lifetimes are constants, not configuration.

### 11. Stored records

Five protocol-owned tables, separate from `personal_access_tokens`:

| Table | Columns (all `NOT NULL` unless marked) |
|---|---|
| `oauth_clients` | `client_id text PK`, `client_name text` (default `''`), `redirect_uris_jsonb jsonb`, `metadata_sha256 text`, `fetched_at`, `cache_expires_at`, `first_seen_at`, `last_used_at` (null) |
| `oauth_authorizations` | `id uuid PK`, `client_id → oauth_clients`, `redirect_uri text`, `resource text`, `code_challenge text`, `state text` (null), `requested_scopes_jsonb`, `browser_binding_hash text`, `created_at`, `expires_at`, `decided_at` (null), `decision text` (null; `approved`/`denied`), `user_id → users ON DELETE CASCADE` (null), `account_id uuid` (null), `preset text` (null), `scopes_jsonb` (null), `allowed_project_ids_jsonb` (null), `grant_expires_at` (null), `code_hash text UNIQUE` (null), `code_expires_at` (null), `code_consumed_at` (null), `grant_id → oauth_grants ON DELETE SET NULL` (null) |
| `oauth_grants` | `id uuid PK`, `user_id → users ON DELETE CASCADE`, `account_id uuid`, `client_id → oauth_clients ON DELETE RESTRICT`, `client_name_snapshot text`, `resource text`, `preset text`, `scopes_jsonb`, `allowed_project_ids_jsonb`, `allowed_workflow_ids_jsonb` (always `[]` in 5B, kept for principal parity), `created_at`, `expires_at`, `last_used_at` (null), `revoked_at` (null), `revoked_reason text` (null); `CHECK (expires_at <= created_at + interval '90 days')` |
| `oauth_access_tokens` | `id uuid PK`, `token_hash text UNIQUE`, `grant_id → oauth_grants ON DELETE CASCADE`, `scopes_jsonb`, `created_at`, `expires_at` |
| `oauth_refresh_tokens` | `id uuid PK`, `token_hash text UNIQUE`, `grant_id → oauth_grants ON DELETE CASCADE`, `parent_id → oauth_refresh_tokens` (null), `created_at`, `expires_at`, `consumed_at` (null) |

Indexes: `oauth_grants (user_id, revoked_at)`, `oauth_access_tokens (grant_id)`,
`oauth_refresh_tokens (grant_id)`, `oauth_authorizations (expires_at)`. The
authorization code is a column of its pending authorization, so one row tracks
request → decision → code → exchange → grant. No client secret exists. Raw codes
and tokens are never stored or logged.

`last_used_at` on the grant (and on the client) is updated by the resolve query
only when null or older than 60 seconds.

Cleanup joins the existing scheduled retention
(`core/application/tooling/ops/retention_cleanup.py`,
`retention_repository.py`): pending authorizations and access tokens are deleted
after expiry; grants 30 days after `revoked_at` or `expires_at`, with their
tokens by cascade; clients with no grant and `last_used_at` older than 30 days.

### 12. Principal shape and the `oauth` auth source

An OAuth access token resolves to the PAT principal shape with
`auth_source="oauth"`:

`principal_type="user"`, `id`, `username`, `display_name`, `account_id`
(grant's), live `roles`, `scopes` (the access token's), `allowed_project_ids`,
`allowed_workflow_ids` (grant's), `is_active=true`, `expires_at` (access token
expiry), plus `oauth_grant_id`, `oauth_client_id`, `oauth_resource`.

`ResolvedPrincipal` gains three optional fields (`oauth_grant_id`,
`oauth_client_id`, `oauth_resource`, default `""`) and
`_normalize_principal_dict` copies them; otherwise `extra="forbid"` and the
named-field copy drop them before `request.state.auth_user`.

All eight hard-coded token sets are replaced by one predicate,
`is_token_scoped_principal(principal)` in `core/application/auth/principal.py`,
over a single constant `TOKEN_SCOPED_AUTH_SOURCES = {"pat", "machine", "token", "oauth"}`
or `principal_type == "service_account"`:

| # | Place (0.9.6 line) | Effect if `oauth` were missing |
|---|---|---|
| 1 | `core/application/auth/auth_http_runtime.py:137` `_principal_requires_http_scopes` | HTTP scope check skipped; role authority |
| 2 | `core/application/auth/principal.py:245` `_caller_is_scope_restricted` | ceiling computed from roles, not scopes |
| 3 | `core/application/auth/resolve.py:541` `_principal_requires_account_id` | missing `account_id` falls back to the default account instead of failing |
| 4 | `core/entrypoints/api/auth_users.py:186-191` introspect scope synthesis | `admin:all` synthesized for an administrator |
| 5 | `core/entrypoints/api/events_stream.py:202` `_is_external_principal` | event stream treats the token as a browser session |
| 6 | `core/entrypoints/api/jobs.py:53` `_is_token_scope` | browser approval gate payload instead of the token gate |
| 7 | `core/application/management/command_services_approval.py:292` | approval decision treated as a session decision |
| 8 | `core/application/management/query_services_approval.py:19` `_is_token_principal` | approval queries treated as session queries |

Places that already exclude `oauth` by allow-list and stay unchanged:
`workflows_builder_run.py:46` (`{"local", "external"}`),
`auth_users.py:236, 267, 397` (`== "local"`),
`command_services_approval.py:294` (`!= "machine"`),
`principal.py:338` `is_machine_principal`, and `resolve.py:503`, which recomputes
`principal_type` only for `""`, `local`, `external`.

Fail-closed rule: a guard test fails if any `"pat"`/`"machine"` set literal
appears outside `principal.py`, and if any `auth_source` value assigned anywhere
in `core/` is not classified in either `TOKEN_SCOPED_AUTH_SOURCES` or an explicit
`ROLE_AUTHORIZED_AUTH_SOURCES` constant. An unclassified new source therefore
fails the gate instead of receiving role authority. The remaining "role
authority by default" behaviour for unknown sources is not inverted in 5B,
because principals such as `project_connection` depend on it.

Additional denials for `auth_source="oauth"`, enforced in
`local_auth_middleware`: 403 on every `is_self_service_auth_write_route` path,
on `GET /api/auth/tokens`, and on the `/api/oauth/*`, `/api/auth/oauth-grants*`
and `/api/settings/mcp-oauth` routes. An OAuth grant cannot mint or list PATs
or manage grants, so it cannot outlive its own expiry or revocation.

OAuth access tokens are resolved only by `local_auth_middleware` through
`resolve_oauth_access_principal(bl, token)`. `resolve_bearer_principal` (exported,
no in-tree caller) does not resolve them.

### 13. MCP → API forwarding

`fs-mcp` and the HTTP API are one protected resource of one authorization
server with one policy; `/mcp` is its public face and the MCP adapter stays thin
(ADR 0034). The token's audience is that resource; the API accepts it only as
the MCP's own back end.

Owner default, row 4. A new internal shared secret `FS_MCP_FORWARD_SECRET`
(at least 32 characters). `AppApiClient` and `AppApiTokenVerifier` send it as
`X-Flow-Steward-Mcp-Forward: <secret>` on every request to
`FS_APP_INTERNAL_URL`, and only there. `AppApiClient` refuses to let
`additional_headers` set or override it.

`fs-app` accepts an `fs_oat_` bearer only when:

1. the path is `/api/auth/introspect` (owner default: no proof required; the
   answer describes only the caller's own principal), or
2. the header is present and equals the configured secret
   (`hmac.compare_digest`), the path starts with `/api/`, and the path is not
   one of the denials in decision 12.

Otherwise 401 `{"error": "invalid_token"}`. PATs, machine tokens and sessions do
not depend on the header in any way.

Both ingresses remove an inbound `X-Flow-Steward-Mcp-Forward` header on every
public router (Traefik `headers.customRequestHeaders` with an empty value) as
defence in depth; the secret is the control.

### 14. Resource validation in `fs-mcp` and PAT compatibility

`/api/auth/introspect` adds `resource`, `oauth_client_id` and `grant_id` to the
principal for `auth_source="oauth"` and nothing for other sources.
`AppApiTokenVerifier` then sets:

- OAuth: `AccessToken.resource` = the introspected resource (missing → reject),
  `subject` = user id, `claims = {"iss": issuer, "oauth_client_id": …, "grant_id": …}`.
- PAT, machine token, session: `AccessToken.resource` = this server's own
  `resource_server_url`. A PAT is valid at its own installation's MCP endpoint;
  that is the stated compatibility rule.
- `client_id` stays `user:<username>` / `sa:<key>`; scopes and `expires_at` as
  today.

In the same 5B change `server_runtime.py` sets `validate_token_resource=True`.
The SDK `BearerAuthBackend` then refuses an OAuth token bound to any other
resource, PATs keep working because the verifier vouches for them, and the SDK
3.0 default-change warning disappears. Enabling the flag before the verifier
sets `resource` for every token type would reject all PATs, so the two changes
ship together and a test pins both directions.

### 15. Consent contract (shared with task 4)

Presets live in one application-owned module,
`core/application/auth/agent_access_presets.py`, used by the task 4 wizard and
the consent screen:

| Preset id | Scopes | Offered when |
|---|---|---|
| `operational` | `projects.read`, `jobs.read`, `jobs.write`, `reports.read`, `reports.write`, `workflows.read`, `workflows.write` | always |
| `full_authoring` | `admin:all` | `admin:all` was requested |

- Granted scopes = selected preset ∩ requested scopes. A request for a strict
  subset of the operational preset is granted exactly that subset and the page
  lists it; a request is never widened to a broader preset. Full authoring is
  never offered unless requested.
- A preset that exceeds the user's authority
  (`enforce_token_scope_within_caller_authority`) is shown disabled with the
  reason. If nothing can be granted, only Deny is available, with the
  explanation.
- Full authoring carries the task 4 text that `admin:all` is full access, not a
  narrow permission.
- Projects: optional multi-select of projects the user can access; empty means
  every project within the user's authority, as for a PAT. The page states that
  project selection does not isolate account-wide agents and skills. Workflow
  allowlists are not offered.
- Expiry: required choice of 7, 30 or 90 days, default 30; no "never".
- Client identity: `client_name` (or host), the `client_id` host, the
  redirect URI host, and a warning when the redirect URI is loopback.
- Default selection is `operational`.

### 16. Settings, visibility and the account switch

Settings → Account → Personal tokens keeps the PAT list and adds "Connected AI apps"
directly below it, built from existing components (see "Stage 5B implementation notes"). Each row shows client name and host,
preset and exact scopes, projects, created, expires, last used ("Access used"
only when `last_used_at` is set, with no claim about the client), status
(active, expired, revoked) and a Revoke action with the existing confirmation
dialog. Revoked and expired grants stay visible until cleanup (30 days). A user
sees only their own grants, as for PATs. The section also shows the readiness
blocker. The task 4 wizard reads the same `GET /api/auth/oauth-grants`.

The account setting `mcp_oauth` (`system_settings`, `{"enabled": false}` by
default; row 12) is toggled by an administrator on Settings → Security. Off: the
authorization server is not ready for that account, and existing OAuth tokens of
that account are rejected by the resolve query; grants are not deleted.

### 17. MCP SDK `mcp==2.2.0`: reused vs written

Reused as-is: `TokenVerifier` protocol and `AccessToken`
(`resource`, `subject`, `claims`); `BearerAuthBackend` resource check through
`AuthSettings(validate_token_resource=True)`; `RequireAuthMiddleware` 401/403;
`ProtectedResourceMetadata` model and handler; `OAuthMetadata` and `OAuthToken`
models in the `fs-app` adapters (`mcp.shared.auth` only, for serialization).

Not used, with the reason verified in the 2.2.0 source:

| SDK piece | Why not |
|---|---|
| `create_auth_routes`, `build_metadata` | advertises only `client_secret_post`/`client_secret_basic` (no `none`, so Claude will not use CIMD) and neither `client_id_metadata_document_supported` nor `authorization_response_iss_parameter_supported` |
| `AuthorizationHandler` | error redirects carry no `iss`; `resource` optional |
| `TokenHandler` | does not pass the token-request `resource` to the provider for code or refresh; `load_authorization_code` then `exchange_authorization_code` is not atomic; redirect mismatch answers `invalid_request`; a failed PKCE check does not consume the code |
| `RevocationHandler` | `RevocationRequest.client_secret: str | None` has no default, so pydantic v2 requires it and a public client gets 400 |
| `OAuthClientInformationFull` | `validate_redirect_uri` is exact (no loopback port rule); `validate_scope` rejects every scope when `scope` is `None` |
| `ClientAuthenticator` | compares plaintext stored secrets; no secrets exist here |
| `RegistrationHandler` | no DCR |
| `OAuthAuthorizationServerProvider`, `ProviderTokenVerifier` | the handlers they serve are not used; token issuance stays in the application |
| `MCPServer.custom_route` for PRM | custom routes are mounted after the SDK PRM route and can never match it |

### 18. Layering

`core/application/auth/` owns all logic. `inbound_oauth_` prefixes keep it apart
from the outbound `oauth_client*` and `mcp_connector_oauth*` modules:

- `agent_access_presets.py` — presets (shared with task 4).
- `inbound_oauth_policy.py` — readiness, resource and scope normalization,
  redirect matching, PKCE verification, lifetimes, token prefixes and hashing,
  the forwarding check.
- `inbound_oauth_clients.py` — CIMD validation, fetch and cache policy.
- `inbound_oauth_service.py` — use cases: start authorization, consent view,
  decide, exchange code, refresh, revoke token, list and revoke grants,
  resolve access principal.
- `inbound_oauth_repository.py` — SQL for the five tables through
  `core/infrastructure/db/db_pool.transaction_connection`.

`core/entrypoints/api/` holds thin routes (`oauth_authorization_server_routes.py`,
`oauth_consent_routes.py`) that parse, call the service and serialize.
`core/entrypoints/mcp/` holds only the verifier, header and PRM adapter changes;
it never touches the database or issues tokens. `core/infrastructure/` gains only
`public_identity.py`.

### 19. Configuration

| Item | Compact | Scalable |
|---|---|---|
| `FS_MCP_FORWARD_SECRET` | generated by the Compact supervisor (`compact_runtime.py`, next to `FS_WORKER_MCP_BEARER_TOKEN`) at every container start with `secrets.token_urlsafe(32)` and given only to the app and mcp roles (`process_spec.py`); never written to `.env` | installer-generated leaf secret (`_PLACEHOLDER_SECRET_NAMES`, installer `gateway_support.py`, `lifecycle/configuration_store.py`, `.env.example.prod`); `docker-compose.prod.yaml` and `docker-compose.yaml` pass `${FS_MCP_FORWARD_SECRET:-}` to `fs-app` and `fs-mcp` only; the update path generates it when absent |
| `FS_MCP_ISSUER_URL`, `FS_MCP_RESOURCE_URL` | already derived for mcp; app derives the same | additionally passed to `fs-app` (`${…:-}`) so a mismatching override becomes the `issuer_mismatch` blocker |
| Header removal | `router.yaml.tmpl` | `infra/traefik/fs.dynamic.yaml` |

Every name is registered in `core/infrastructure/config/env_registry.py` and the
env artifacts are resynchronized. Nothing is `:?`-required: an install or update
without the secret starts normally with OAuth unavailable and PATs unaffected.
No new variable exists for lifetimes, the CIMD allowlist or the switch.

### 20. Database artifacts

The five tables, their constraints and indexes are in the `db/schema/` baseline,
which fresh installations load, and no seed is needed. 0.9.7 shipped them in the
baseline only, so an installation updated from 0.9.6 did not get them (ADR 0012,
2026-11-01 amendment). From 0.9.8 the migration
`db/migrations/20261101153533_inbound_oauth_tables.sql` carries the same DDL with
`IF NOT EXISTS`: an update stops at the release's migration check, and
`flow-steward database upgrade --maintenance-confirmed --confirm` creates the
tables; on a fresh installation the migration is a recorded no-op. The tables are
not added to `REQUIRED_RUNTIME_TABLES` (`schema_contract_catalog.py`): the startup
contract already refuses a database with a pending migration, and a database
without the tables still reports `storage_missing` rather than refusing to start.
Schema registry tests that cover the baseline are updated with the tables.

### 21. Logging and audit

Never logged: codes, access and refresh tokens, `code_verifier`, the cookie
value, the forward secret, `state`. The decision endpoint returns the redirect
URL in a JSON body; the code is not in any `fs-app` request URL. Auth audit
events through the existing `log_auth_audit_event`: `oauth_consent_approve`,
`oauth_consent_deny`, `oauth_grant_revoke` (with reason), `oauth_code_reuse`,
`oauth_refresh_reuse`, `oauth_client_rejected`. Details hold ids, client host
and scopes only. `/oauth/authorize` and `/oauth/token` use the existing
rate-limit store, keyed by client IP and by `client_id` + IP.

## Stage 5B change list

1. `core/infrastructure/public_identity.py` (moved derivation);
   `core/entrypoints/mcp/server_public_identity.py` re-export;
   `infra/mcp/Dockerfile` copy line.
2. `core/application/auth/agent_access_presets.py` (if task 4 has not created it),
   `inbound_oauth_policy.py`, `inbound_oauth_clients.py`,
   `inbound_oauth_service.py`, `inbound_oauth_repository.py`.
3. `core/application/auth/principal.py`: `TOKEN_SCOPED_AUTH_SOURCES`,
   `ROLE_AUTHORIZED_AUTH_SOURCES`, `is_token_scoped_principal`, three
   `ResolvedPrincipal` fields; `resolve.py` field copy; the eight places in
   decision 12 switched to the predicate.
4. `core/application/auth/auth_http_runtime.py`: `fs_oat_` routing to
   `resolve_oauth_access_principal`, the forwarding rule, the OAuth denials.
5. `core/application/auth/config.py`: four public paths.
6. `core/entrypoints/api/oauth_authorization_server_routes.py`,
   `oauth_consent_routes.py`; registration in `route_registration.py`;
   `auth_users.py` introspect fields; `core/entrypoints/web/frontend_shell.py`
   `/oauth/consent` route.
7. `core/entrypoints/mcp/server_api_auth.py` (resource, subject, claims, header),
   `server_api_client.py` (header), `server_runtime.py`
   (`validate_token_resource=True`), `server_main.py` (app build + PRM/401 adapter).
8. `db/schema/` five tables; schema tests.
9. `core/application/tooling/ops/retention_cleanup.py` and
   `retention_repository.py`: OAuth cleanup.
10. Configuration per decision 19: `env_registry.py`, `compact_runtime.py`,
    `process_spec.py`, installer and lifecycle secret lists,
    `.env.example.prod`, `docker-compose.prod.yaml`, `docker-compose.yaml`,
    `infra/traefik/fs.dynamic.yaml`, `router.yaml.tmpl`.
11. React: consent/error page under `assets/frontend/src/pages/oauth/consent/`,
    Settings → Security section and admin switch, `shared/api.ts` calls, locale
    strings.
12. Tests per `task5b-checklist.md`.

Anything beyond this list found during 5B is recorded separately and is not
implemented implicitly.

## Stage 5B implementation notes (corrections found in code)

5B implemented the change list above. Where the code showed this record to be wrong
or incomplete, the record was corrected here rather than worked around:

- Decision 16, location. In 0.9.6 the PAT list is not on Settings → Security (that
  page is the administrator's account security policy) but on Settings → Account →
  Personal tokens. "Connected AI apps" sits directly below the PAT table there; the
  administrator's `mcp_oauth` switch sits on Settings → Security, with the readiness
  blockers.
- Decision 2, readiness before sign-in. `/oauth/authorize` and the metadata document
  run before any user is known, so the account switch cannot be read for "the"
  account there: the installation counts as enabled when any account has
  `mcp_oauth.enabled`. Consent, the token endpoint and every access-token use check the
  user's (grant's) own account. On a single-account installation the two are the same.
- Decision 4, public paths. `/oauth/consent` also joins `AUTH_PUBLIC_PATHS`: its
  `?error=` variant is reached before sign-in (a client that is not allowed, a
  readiness blocker) and must render without a session. The `?request=` variant still
  requires a session: the React page redirects to sign-in and every call it makes is a
  session-only `/api/oauth/*` route.
- Decision 4, interactive-only routes. "Interactive principal" is enforced as a
  browser session cookie (or an external identity); a session token sent as a bearer is
  refused too, because the CSRF check applies only to cookies.
- Decision 15, presets. A request for `admin:all` alone also offers the operational
  preset (seven scopes): `admin:all` covers them, so it is a narrower alternative, not
  a widening.
- Decision 12, classification. `oauth_callback` (the outbound connector callback's
  system scope) is also assigned in `core/` and is classified role-authorized; the
  guard test found it.
- Decision 19, Scalable secret. The secret is not added to `_PLACEHOLDER_SECRET_NAMES`
  / installer-provisioned secrets: that would make `config apply` refuse every existing
  0.9.6 install without it (contradicting "nothing is required") and would make the
  Compact self-minted secrets persist it to the volume. The Scalable installer instead
  generates it in the runtime step that install, resume and update all run before
  `fs-app`/`fs-mcp` start. A Compact→Scalable profile switch done by `config apply`
  alone has no secret until the next `./install.sh`; OAuth reports
  `forward_secret_missing` meanwhile.
- Decision 13, header removal. Scalable in `external_proxy` mode (the operator's own
  nginx/apache) reaches `fs-app` without the bundled Traefik, so the header is not
  stripped there; the shared secret remains the control. The external-Traefik overlay
  strips it as well.
- Decision 20, existing databases. `flow-steward database upgrade` replays the
  canonical `db/schema/` (idempotent `CREATE TABLE`) when the schema fingerprint
  changes, so the five tables also reach an existing 0.9.x database through the normal
  upgrade, not only by hand.
- Decision 11, cleanup. Consumed refresh tokens stay until their grant is deleted, so
  reuse detection keeps working for the grant's whole life; only pending
  authorizations and expired access tokens are deleted early.

## Consequences

- ChatGPT and Claude.ai (and Cowork through it) can connect to an install with a
  public HTTPS origin after an administrator enables OAuth; local clients and
  PATs are unchanged.
- A new auth source can no longer silently gain role authority: one predicate
  and a guard test own the classification.
- OAuth tokens are bound to `{origin}/mcp`; `fs-mcp` validates the binding with
  the SDK, and the API accepts them only from `fs-mcp`.
- Strict refresh reuse detection can revoke a grant when a client races its own
  refresh; the user then reconnects.
- Only clients hosted on `chatgpt.com` and `claude.ai` can obtain tokens. If a
  target client's CIMD declares `private_key_jwt` or a non-loopback `http`
  redirect, or omits `resource`, 5B returns that as a concrete dependency
  instead of relaxing the contract.
- The authorization server depends on outbound HTTPS from `fs-app` to the CIMD
  hosts.

## Alternatives considered

- DCR as well as CIMD: deprecated in the MCP specification and both target
  clients prefer CIMD; it would add open registration and client storage.
- JWT access tokens: need key management and a revocation list; introspection is
  already the only validation path.
- Using the SDK `create_auth_routes` with a provider: blocked by the gaps in
  decision 17, and it would place token issuance next to the MCP runtime.
- Token exchange (RFC 8693) from the MCP token to an internal token: needs the
  same MCP authentication and a second token type without a security gain for a
  single-resource installation.
- A separate internal port of `fs-app` for MCP traffic: changes both profiles'
  networking and the Compact supervisor more than one header does.
- Storing OAuth grants in `personal_access_tokens`: mixes a public-client
  protocol state machine into a table with different semantics; the spec allows
  separate records behind one Settings view.

## Open questions

Owner decisions taken by default and logged in
`docs/plans/ai-agent-access-questions-2026-10-31.md`: rows 2–4 (CIMD only,
90-day maximum, forwarding secret) and rows 11–14 (no refresh grace window,
OAuth off by default per account, fixed CIMD host list, delivery of the new
tables to existing 0.9.x databases).

To be confirmed by the 5B live checks, not by the owner: the exact CIMD
documents of ChatGPT and Claude.ai (auth method, redirect URIs), whether both
send `resource` in the authorization request, and whether Cowork uses the
Claude.ai connector callback.

## Related decisions

- [ADR 0012: Post-1.0 SQL migrations](0012-post-1.0-sql-migrations.md)
- [ADR 0020: OSS single-tenant registry boundary](0020-oss-single-tenant-registry-boundary.md)
- [ADR 0023: Extension-owned OAuth boundary](0023-extension-owned-oauth-boundary.md) (outbound OAuth; unaffected)
- [ADR 0029: Compact and Scalable deployment](0029-compact-and-scalable-deployment.md)
- [ADR 0033: Workflow authoring application boundary](0033-workflow-authoring-application-boundary.md)
- [ADR 0034: MCP operations and shared service boundaries](0034-mcp-operations-and-shared-service-boundaries.md)
- [Security standard](../../standards/security-standard.md)
