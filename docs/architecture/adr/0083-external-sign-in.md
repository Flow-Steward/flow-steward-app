# ADR 0083: External sign-in with pluggable sign-in methods

- Status: Proposed
- Date: 2026-11-26
- Builds on: [ADR 0066](0066-global-roles-and-secret-provider-boundary.md),
  [ADR 0076](0076-permission-catalogue-and-role-permissions.md),
  [ADR 0082](0082-platform-modules-and-feature-entitlements.md)
- Amended: 2026-11-27, the person rule after roles per account
  ([ADR 0085](0085-roles-per-account.md)); see "Amendment 2026-11-27" below.

The number is provisional. It was the first free number on `main` on the day of
writing and must be rechecked when this record is merged.

## Context

Sign-in is local only: `POST /api/auth/login` checks a password and, where required,
an authenticator code, inserts an `auth_sessions` row and sets an HttpOnly session
cookie and a readable CSRF cookie. Every session lookup re-applied the local
credential rules (password age, a pending authenticator reset, a required
authenticator). A person who signs in somewhere else has no local password to age
and no local authenticator, so such a session would expire with a password nobody
uses and be refused outright on an installation that requires an authenticator.

Organisations want their people to sign in with the identity service they already
run, and to keep a local password for a few administrators as a way in when that
service is unavailable. The session must stay Core's: platform modules cannot write
`auth_sessions` or replace Core routes (ADR 0082, section 2).

## Decision

### 0. Core keeps a neutral seam; sign-in protocols are provided by modules

Core implements no sign-in protocol and knows no identity service. Every protocol
that verifies a person elsewhere is a **sign-in method** that a platform module
registers. Core keeps only what every method shares, so nothing in Core changes when
a method is added:

- a registry of method kinds (section 3);
- the public routes, which are method-agnostic:
  `GET /api/auth/external/options[?account=<account id or key>]` (the sign-in page's
  buttons, section 4),
  `GET /api/auth/external/start?method=<kind>&key=<option>&next=<path>[&intent=link]`
  and `GET /api/auth/external/callback`, the one return address of every redirecting
  method (`FS_PUBLIC_BASE_URL` + `/api/auth/external/callback`, never derived from
  the request's `Host`);
- a generic single-use attempt store for methods that redirect;
- the identity resolver, the user and account checks, the identity link and the
  session.

The sign-in page renders one button for every option it is given, never a fixed
list; options that belong to one account are given only once the visitor names that
account (section 4). Core names the options path in the app bootstrap only when a
module registered a method or a trusted header source.

A method implements `kind`, `options()`, `option(key)`, `begin(key, callback_url,
state)` returning a redirect URL and data to keep (or no redirect, to complete at
once from the request), and `complete(key, params, headers, data, callback_url)`
returning a `VerifiedExternalIdentity` or raising `ExternalSignInError(reason)`. The
method never touches the attempt, the session or the cookies.

### 1. The attempt, the identity and the session

- **The attempt** (`external_sign_in_states`) keeps the hash of `state`, the method
  and option, the method's data encrypted with Core's secret codec, the return path
  and the hash of a browser-binding value. It expires after ten minutes and is
  consumed atomically before anything else is checked. The binding value travels in
  a cookie that is HttpOnly, `SameSite=Lax` (the method returns with a cross-site
  top-level GET) and limited to `/api/auth/external`; a callback from another browser
  is refused.
- **The identity key is `(issuer, subject)` within one account**: unique on
  `(account_id, issuer, subject)`, so one account can never block, or reach, another
  account's link. Core, not the method, sets the identity's method and stores every identity under an issuer prefixed with the
  method's kind, so one method can never name an identity another one verified. An
  email address is passed on as a claim and never used to find or link a user.
- **The session** is a normal `auth_sessions` row and the usual cookies, with a new
  token (never a reused one), `auth_method = 'external'` and the external identity
  it was issued for. `auth_sessions.auth_method` is `local` or `external`; the local
  password and authenticator rules apply only to `local` sessions. The session lookup
  reads `auth_method`, so an external session's principal has
  `auth_source = 'external'` for its whole life, not only for the session issue
  guards: every local-only check (linking a sign-in, changing the email address,
  managing the local authenticator) refuses it.
- **The session is pinned to the issuing account.** Its account is the account that
  issued the identity (the identity's account when the method names one, otherwise
  the link's). `POST /api/auth/switch-account` refuses an external session with 403
  `external_session_account_pinned`, and its update moves only rows whose
  `auth_method` is `local`. A person who belongs to several accounts signs in to each
  separately. The return path is a same-origin relative
  path; anything else becomes `/`.
- Every success and refusal is audited with a reason code, the method, the option,
  the issuer, the subject and the tenant; tokens, codes and other claims are never
  logged.

Deleting a user removes the user's external identities and tells the user-deleted
subscribers; an identity's removal deletes the sessions it issued.

### 2. What Core stores

| Table | Content |
| --- | --- |
| `external_identities` | `(account_id, issuer, subject)` unique, the account it was linked in, the method that verified it, the option, the user it signs in, last sign-in |
| `external_sign_in_states` | Single-use attempts of every redirecting method |
| `auth_sessions.auth_method`, `auth_sessions.external_identity_id` | Whether a session is `local` or `external`, and the identity that issued it |

Core stores no configuration of any sign-in method; a module that needs some keeps it
in its own storage and may encrypt secrets with the port's `encrypt_secret`.

A user created for external sign-in gets the password hash marker
`!external-sign-in`, which never verifies; `users.password_hash` stays `NOT NULL`.
The local sign-in answers such a user exactly like a wrong password, at the same
cost.

Links are per account. An administrator of an account (an account administrator in
the sense of ADR 0082 §9; the installation administrator only for the installation
account) lists and removes that account's identity links under
`/api/settings/external-sign-in/identities`, always in the account they act in. A
person's membership in a second account does not let that account's administrator
see or remove links made in another account. Without a module resolver an identity
signs in only through a link: made by a module (`link_identity`) or by the person
while signed in locally (`start?intent=link`), never automatically by a matching
email.

### 3. The seam for platform modules

`container.external_sign_in` (`ExternalSignInPort`) is set before modules load and is
rolled back by the loader when a module fails. A module may:

| Registration point | Core's behaviour without it |
| --- | --- |
| `register_sign_in_method(method)`: a new kind (section 0); `local`, `external` and `header` are reserved | No sign-in method; the sign-in page shows only the local form |
| `set_identity_resolver(resolver)`: `resolve(VerifiedExternalIdentity) -> ExternalIdentityResolution(user_id, account_id)` or `ExternalIdentityRefused(reason)` | `LinkedIdentityResolver`: only an identity already linked signs in, into the identity's account (or the link's) |
| `register_header_source(source)`: `feature`, `display_name`, `resolve(headers) -> VerifiedExternalIdentity | None`; Core's built-in `header` kind offers it as option `trusted` | The `header` kind offers no option |
| `subscribe_user_deleted(callback)` | Nothing is told |

and may call `linked_user(account_id, issuer, subject)` (one account's link only),
`link_identity` (stored in the method's issuer namespace, for a user of the given
account), `create_user` (a user of one account without a local password; refused for
the first user of an installation), `grant_roles` (operator or viewer only, add-only,
the highest system role kept) and `encrypt_secret` / `decrypt_secret`.

**The person rule.** An account's sign-in source may reach only a person that
account alone administers, the rule user administration applies
(`require_user_access`: every account the person belongs to is within the acting
account). Concretely the person's home account is the account and they belong to no
other account. `link_identity` and every sign-in (including a link made by the person
while signed in locally) also refuse a person holding the `admin` or `system_actor`
role. `grant_roles` follows the same membership rule, because roles are global
(ADR 0066): a grant to a member who also belongs to another account would raise
their rights there too. Refusals: `link_identity` and `grant_roles` raise
`PermissionError` with the code `user_in_other_accounts` or `admin_not_external` and
log it; sign-in is refused and audited with the same code.

Whatever the resolver answers, Core checks that the resolution names an account and,
when the identity names one, that it is that account (`account_mismatch`), that the
user exists, is active, is a member of that active account and passes the person
rule, runs the session issue guards, links the identity to that user (refusing one the
account linked to someone else) and issues the session. A resolver can therefore
widen who signs in only to people of the issuing account it could already name, and
never to an administrator.

The default resolver looks the identity up in the account its method names; for a
method that names no account it uses the one account that linked the identity and
refuses an identity two accounts linked (`identity_ambiguous`).

A trusted header source stands for a component in front of Core that the module
trusts to set request headers. The `header` kind does not redirect:
`start?method=header&key=trusted` completes at once from the request's headers. Its
identities are stored under issuers prefixed `header:`.

An option that names an account and a feature is offered and accepted only while that
account is entitled to the feature; every identity whose method or source names a
feature is accepted only for an account entitled to it.

Sign-in protocols, their configuration and administration screens, tenant
allow-lists, group or role mapping rules, creation on first sign-in and
deprovisioning belong to modules.

### 4. The public option list

`GET /api/auth/external/options` is public, so it must not tell a visitor which
accounts use which identity services. It answers
`{"options": [...], "account_hint_supported": <bool>}`:

- without a hint, only the options that name no account (installation-wide ones such
  as the trusted header kind);
- with `?account=<account id or account key>`, those plus the options of that active
  account; an unknown or inactive account adds nothing;
- `account_hint_supported` is true when some option belongs to one account. The
  sign-in page then shows a "Company sign-in" step that asks for the company
  identifier and lists that company's buttons.

There is no account-to-host mapping in Core, so the hint is the only way to name the
account before sign-in. `start` still accepts any option it is given; the hint only
scopes the listing. Lookups with a hint are throttled per client address (30 a minute,
then 429 with `Retry-After` for a minute; per process, Core's in-memory throttle); the
list without a hint is not. A trusted header source that names its account
(`account_id`) is listed only with that account's hint.

## Security checklist

- Users are never keyed or linked by email.
- `state` is single-use, expires, and is bound to the browser.
- The return path is a same-origin relative path.
- A method's data is stored encrypted and never logged or returned.
- Identities of different methods live in different issuer namespaces.
- No mapping grants the administrator role through the port.
- An account's sign-in source links, signs in and grants roles only to a member of
  that account, and never links or signs in an administrator of any account (the
  installation administrator included). Since roles are held per account (ADR 0085)
  the person's other memberships do not matter (amendment 2026-11-27).
- An external session is pinned to the issuing account and resolves as
  `auth_source = 'external'`; local-only self-service refuses it.
- Identity links are unique per account; one account cannot block another's.
- The public option list shows an account's options only to a visitor who names it.
- A principal without an account never counts as the installation account
  (`installation_admin`, principal normalisation). The only defaults left are the
  request scope normalisation of a caller without a session and Core's own
  `system_actor` (ADR 0020, deliberately kept defaults).
- The person rule holds after sign-in too: changing a person's global roles or
  account memberships, disabling the person or disabling their account revokes their
  sessions from external sign-in and, for a person who signs in externally (a linked
  identity or an external session), every personal token. Core does not record which
  session minted a token, so it revokes them all (fail closed). Project scope limits
  are read per request and need no revocation.
- An external session's own-account writes need `account.profile.write`; the
  middleware decides on the principal's source, not on how its cookie arrived.
- An administrator's external session manages users of the issuing account only.
- Refused links and role grants of the port and the switch-account refusal of an
  external session are written to the auth audit (status `rejected`).
- Principals Core builds for service accounts, personal and machine tokens and its own
  system actor name their account explicitly.
- A new session token is minted for every sign-in.
- A method is responsible for verifying what it returns; Core trusts a method as much
  as the module that registered it (ADR 0082).

## Consequences

- Core ships the seam only. Without a module the options route lists nothing and the
  sign-in page keeps the local form.
- A new kind of sign-in is a registered method. It changes none of Core's routes,
  tables, session handling or sign-in page.
- External sessions are not held to the local password age or authenticator rules.
  The method's identity service is trusted for authentication.
- Cookie sessions still need `FS_LOCAL_AUTH_ENABLED`; external sign-in issues the same
  kind of session.

## Open questions

- Request-scope helpers (`principal_account_id`, `normalize_scope`) still fill the
  installation account into a principal that names none (ADR 0020, open defaults).
  Sessions are refused without an account and the middleware refuses an inactive or
  empty account where account tables exist, but a header-only deployment relies on
  its trusted header source naming the account.

- A user created for external sign-in can still be given a local password by an
  administrator or through a password reset. Whether such users should be refused
  local passwords is not decided.
- A sign-in failure returns to the sign-in page with an English message; a translated
  message needs a frontend change.

## Amendment 2026-11-27: the person rule with roles per account

[ADR 0085](0085-roles-per-account.md) holds a person's role on the account
membership, and a request has the role of the account it acts in. The reason for
the "belongs to no other account" half of the person rule is gone: a role grant from
a sign-in module writes the membership role of the module's account only, and an
external session is pinned to the issuing account, so it holds the person's role
there and nothing else.

The person rule is now:

- `link_identity`, sign-in and a link made by a signed-in person need the person to
  be a member of the account (refused with `LookupError`, or `account_unavailable`
  at sign-in, otherwise), and refuse a person who is an administrator of **any**
  account, the installation administrator included, or holds a scoped administrator
  role (`admin_not_external`). An administrator still signs in locally.
- `grant_roles` needs the person to be a member of the account and writes that
  membership's role only (operator or viewer, add-only). A person who also belongs
  to other accounts may be granted a role; their role elsewhere does not change.
- The refusal code `user_in_other_accounts` is gone.

Why this is safe: the session the link signs in is pinned to the issuing account
(switching is refused), the role it carries is read from that account's membership on
every request, and user administration of an external session stays pinned to the
issuing account (ADR 0085 §6). The installation administrator and every account
administrator remain out of reach of any account's sign-in source.

Revocation is unchanged: a change of the person's roles or memberships, or disabling
the person or the account, still revokes their external sessions and, for a person
who signs in externally, their personal tokens. Adding someone as an administrator
of another account revokes them too, because such a person may no longer sign in
externally.
