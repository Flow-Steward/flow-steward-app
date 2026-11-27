# ADR 0085: Roles per account

- Status: Proposed
- Date: 2026-11-27
- Amends: [ADR 0066](0066-global-roles-and-secret-provider-boundary.md) (global
  roles), [ADR 0076](0076-permission-catalogue-and-role-permissions.md) §7 and §8
  (where a role is stored, scoped limits),
  [ADR 0084](0084-multiple-accounts-in-one-installation.md) (multiple accounts)
- Related: [ADR 0083](0083-external-sign-in.md) (external sign-in)

The number is provisional. It was the first free number on the integration branch
on the day of writing and must be rechecked when this record is merged.

## Context

ADR 0066 made a person's role global: one `user_roles` row without a workspace,
project or workflow was the person's role in every account they belong to. With one
account per installation that was the same thing as "the role in the account".
ADR 0084 allows several accounts in one installation, and a person may belong to
more than one of them (`user_account_memberships`). A global role then leaks: an
administrator of account A who is added to account B as a reader is an
administrator of B too, and an account administrator's user management reaches
every account they are a member of.

Four readers repeated the same "global role" query (session principal, the serve
runtime's copy of it, personal access tokens, inbound OAuth bearer tokens), and the
user administration, scoped limits, approval candidates and external sign-in each
probed `user_roles` with the global predicate.

## Decision

### 1. The membership carries the role

`user_account_memberships.role_id` (foreign key to `roles`) is the person's one
system role in that account. A membership without a role (`role_id IS NULL`)
grants nothing: the person may still be listed as a member, but every
role-based check refuses them in that account.

`user_roles` keeps only scoped rows (workspace, project, workflow). A check
constraint refuses a row without a scope, so no code path can write a global role
again.

### 2. The acting account's role applies

A request runs in one account: the session's current account (changed with
`POST /api/auth/switch-account`), the personal access token's account, the
inbound OAuth grant's account. The person's roles for that request are the role
of their membership in that account, read from the database on every request.
Nothing caches a role across requests; a future cache must be keyed by
`(user_id, account_id)` and invalidated on a membership change.

A principal without an account, or with an account the person is not a member of,
has no role. The installation account is never filled in as a fallback for a
person (the one documented default for callers without a session, ADR 0020, is
unchanged and never reaches a person's principal).

### 3. Installation administrator

Unchanged predicate (ADR 0082 §9): the admin role while acting in the installation
account. Since the role is now the membership role, "installation administrator"
is exactly "admin membership in the installation account, acting there". An
administrator of another account who is a reader of the installation account is
a reader there.

Bootstrap: the first administrator gets the installation account membership with
the admin role, in the same transaction that creates the person.

### 4. Tokens resolve their own account's role at use time

- Personal access token: the role of the owner's membership in the token's
  account, read when the token is used, not when it was minted. A downgrade or a
  removed membership takes effect on the next request. A token never acts in
  another account. A token with an empty account is refused.
- Inbound OAuth / MCP bearer: the role of the membership in the grant's account,
  read on every use (already the case for the membership itself).
- Service accounts and machine tokens: unchanged (already account-bound, no
  `user_roles`).

### 5. Scoped limits narrow within one account

`scoped_role_users` is keyed `(user_id, account_id)`: a person can be limited in
account B and unrestricted in account A. The membership role of the acting
account is the ceiling; scoped grants on places of that account narrow it as
before (ADR 0076 §8). Replacing or lifting a limit touches only the scoped rows on
places of that account.

### 6. User administration per account

- An administrator's user management reaches only the accounts where their
  membership role is admin (not every account they belong to). An external
  sign-in session stays pinned to its issuing account (ADR 0083).
- A role change names the account it applies to (`role_account_id`; the API
  default is the administrator's acting account, the CLI default is the person's
  home account) and changes only that membership.
- A role change in an account the administrator manages is allowed for a person
  who also belongs to other accounts; every other change (profile, password,
  activation, memberships, deletion) still needs every account of the person to be
  managed by the administrator. A person never changes their own role.
- The "last administrator" check is per account: an account keeps at least one
  active administrator through a demotion, a deactivation, a removed membership
  and a deletion. The installation account's check is the "last installation
  administrator" check.
- New memberships take the requested role, or viewer when none is given.
- Approval candidates are the account's members (by membership, not home account)
  whose membership role there decides approvals.

## Migration

`db/migrations/20261127170000_roles_per_account.sql`, the same change as the
baseline block at the end of `db/schema/001_schema_part_12.sql`:

1. `user_account_memberships.role_id uuid REFERENCES roles(id)`, nullable.
2. Every membership without a role gets the person's highest global system role
   (admin > operator > viewer). People with no global system role keep `NULL`
   (no access); the migration reports how many memberships that is with
   `RAISE NOTICE`, and how many global non-system role rows it removes.
3. The global `user_roles` rows are deleted after the backfill, in the same
   transaction, and the check constraint of §1 is added.
4. `scoped_role_users` primary key becomes `(user_id, account_id)`.

Every statement is safe to run again. Behaviour after the upgrade is identical to
before for every person: each membership carries the role the person had
everywhere.

### Why delete the global rows instead of keeping them read-only for one release

Keeping the old rows "read-only" would leave two sources of truth. Any reader
not converted in this change, or any later code written against the old shape,
would read the installation-wide role and grant it in every account, which is the
leak this record closes. Deleting them makes such a reader fail closed (no role)
and the check constraint makes such a writer fail loudly. Nothing is lost: the
backfill copies each person's global system role into every one of their
memberships, so the old rows can be rebuilt from the memberships (the highest
membership role per person) if a rollback is ever needed. Global non-system role
rows are counted in the migration's notice; no product path creates them.

## Stage 2 (2026-11-27)

- **Users page.** Settings → Users lists the people of the acting account
  (`GET /api/auth/users` filters by membership of the acting account) with their role
  there ("No role here" when the membership has none). An administrator changes a
  person's role in this account (`PUT /api/auth/users/{id}` with the role; the account
  defaults to the acting one).
- **Add an existing person.** `GET /api/auth/users/candidates?q=` finds active people
  who are not members yet, by name or email among people of the accounts the
  administrator manages (at least two characters, ten results).
  `POST /api/auth/users/{id}/membership` with `role` adds the membership; the server
  checks again that the person belongs to a managed account. Anyone else is invited
  (see "Review fixes", A). Adding someone as admin revokes what external sign-in issued
  to them in this account (ADR 0083; B below).
- **Remove from this company.** `DELETE /api/auth/users/{id}/membership` removes the
  membership of the acting account only: refused for the administrator themselves,
  for the person's only membership (delete the person instead) and for the last
  active administrator of the account. The person's scope limit there, their sessions
  and personal tokens of that account and their external identities of that account
  go with it; a home account moves to another membership.
- **Switching.** `POST /api/auth/switch-account` into an account where the person's
  membership has no role is refused with 403, code `no_role_in_account`, "You have no
  role in this company. Ask its administrator." The account menu shows the person's
  role in each account and hides accounts without one. After a switch the frontend
  drops every cached answer of the old account and reloads the bootstrap (which
  carries `/api/auth/me`'s user).
- **Upgrade repair.** `flow-steward database upgrade` repairs a person without any
  membership with a membership of their home account. No role can be derived for
  such a person: the migration copied every global role into the memberships it
  made, the global rows are gone (and refused by the check of §1), and a scoped row
  only narrows a role. The repaired membership therefore has no role (`NULL`, no
  access), never a guessed one such as viewer. The summary reports
  `repaired_user_memberships` and `repaired_user_memberships_without_role`, and the
  upgrade logs a warning naming the count; an administrator gives those people a
  role on the users page.
- **External sign-in.** ADR 0083 is amended: the "belongs to no other account" half
  of the person rule is removed for grants, links and sign-in; administrators of any
  account stay refused.

## Review fixes (2026-11-27, owner defaults)

An independent review found that the stage above let an administrator reach people
and sessions beyond the accounts they manage. The owner chose these defaults.

### A. Consent: invitations

- An administrator adds directly only people who already belong to an account the
  administrator manages. The candidate search no longer matches an exact email
  address among everyone: a person outside the managed accounts is never listed, and
  `POST /api/auth/users/{id}/membership` refuses them with 404 and accepts no `email`.
- Anyone else is invited by their exact email address:
  `POST /api/auth/users/invitations` with `email` and `role` stores a pending
  invitation in `account_invitations` (`id, account_id, invited_email, user_id, role,
  invited_by, created_at, expires_at, status`; one pending row per account and
  address, compared case-insensitively, 14 days, status `pending`, `accepted`,
  `declined` or `cancelled`). The row is stored whether or not anyone has the address:
  `invited_email` is the address as the administrator typed it and `user_id` stays
  empty until a person answers, then names them. The answer is always
  `{"status": "ok", "message": "If this person exists, they will get an invitation"}`:
  it is the same for an unknown address, a member and a repeated invitation (which
  renews the pending one with the new role). Only the auth audit records the outcome.
- The person answers an invitation through its single-use link (superseded by
  "Re-review fixes", C1: the earlier listing of invitations matched by the person's
  account email in the account menu is removed).
- Administrators see the acting account's open invitations on the Users page
  (`GET /api/auth/users/invitations`: id, email as typed, role, who invited, when,
  until when) and cancel one (`POST /api/auth/users/invitations/{id}/cancel`, status
  `cancelled`). The list shows every invitation sent, to existing people or not, so it
  never says whether a person exists; an invitation leaves the list when the person
  answers it, which is their own consent to being known.
- Expired invitations are hidden from both lists and cannot be answered; the account's
  next invitation deletes its expired pending ones.
- Candidate searches and invitations are rate-limited (shared store since P4 of the
  re-review below); over a limit the API answers 429 with `Retry-After`.
- Migration `db/migrations/20261127190000_account_invitations.sql`, the same block at
  the end of `db/schema/001_schema_part_12.sql`; the table and its pending key are
  part of the runtime schema contract. Verified with psql on PostgreSQL 17: fresh,
  re-run, and upgrade from the 0.9.23 baseline give the same table.

### B. A role change revokes only that account's grants

A change of the role alone revokes the person's sessions, personal access tokens and
inbound OAuth grants bound to that account, not those of their other accounts. Without
session revocation (the CLI) it revokes only what external sign-in issued in that
account. Adding a person as admin revokes only that account's external sign-in
sessions and tokens. Other changes (password, deactivation, memberships) still revoke
everything, as before.

### C. The users list stays in the managed accounts

User views returned by `GET/POST/PUT /api/auth/users` list only the memberships (and
account ids) of accounts the administrator manages; a home account outside them is
shown as the first visible one.
Their `roles` are recomputed for one visible account (re-review C3).

### D. Tokens manage their own account

For a personal access token or an inbound OAuth grant, the managed accounts are the
token's own account only (when its role there is admin), like a session of external
sign-in. A token never reaches the people of the owner's other accounts.

### E. Removed roles and sign-in without a role

- Removing a membership also revokes the person's OAuth grants of that account.
- A directory sync removes a role through `ExternalSignInPort.remove_role(account_id=,
  user_id=)`: the membership stays without a role, and the person's sessions, personal
  tokens and OAuth grants of that account are revoked; the account's last
  administrator is refused (`ValueError`). `ExternalSignInRevocationPort` gains
  `revoke_access_in_account(cur, user_id=, account_id=)` for a module that changes
  roles in its own transaction.
- Password sign-in lands only in an account where the person has a role: a chosen or
  default account without one is passed over for another with a role, the account
  choice lists only those, and a person with a role nowhere is refused with 403
  `no_role_in_account` ("You have no role in this company. Ask its administrator.").
- The MCP password bootstrap opens its session in the person's home account when they
  have a role there, else in another account with a role, else refuses.

### F. External sessions

A session of external sign-in is pinned to its account (ADR 0083): it cannot change
the person's profile (`PATCH /api/users/me/profile` answers 403
`external_session_profile_read_only`, because the name is shared by every account),
and `/api/auth/tokens` and `/api/auth/oauth-grants` list and revoke only the tokens
and grants of the pinned account.

### G. No Core writer depends on a global role row

Checked: `system_actor` is a principal type built in code (CLI, worker, MCP
development), never a stored role; service accounts and machine tokens carry no
`user_roles` rows; the only Core writer of `user_roles` is the scoped roles port,
whose rows always name a workspace, project or workflow. The migration's notice counts
global rows of other roles only for installations that created them by hand.

### Contract for platform modules

- Linking an existing person to an account (an "add member" in a module) follows the
  same rule as the API: only a person who already belongs to an account the acting
  administrator manages may be added directly. A person outside them is invited and
  becomes a member only by accepting; a module never creates a membership for someone
  who has not consented.
- `ExternalSignInPort.grant_roles` stays add-only in one account;
  `ExternalSignInPort.remove_role` is the one way to take a role away, and it revokes
  that account's access in the same transaction.

## Re-review fixes (2026-11-27, invitation links)

A second review found that an invitation could be accepted by anyone who signed in
with the typed address, and that some answers still told an administrator whether an
address exists. The owner's decisions:

### C1. The link token, not the email address, is the authority

- `POST /api/auth/users/invitations` makes a single-use, unguessable token
  (`secrets.token_urlsafe(32)`, 256 bits). Only its SHA-256 is stored
  (`account_invitations.token_hash text NOT NULL`, unique, 64 hex characters); the
  token itself is never stored, listed or written to the audit. It expires with the
  invitation (14 days).
- Delivery: Core's system email sender (the confirmed Settings → Email connection,
  `core.application.extensions.system_email`) sends "<inviter> invited you to
  <company>" with "… invited you to <company> as <role>" and the link
  `<public URL>/invitations/<token>` to the normalized address. When there is no
  system email or the send fails, the answer carries `invitation_link` once
  (`delivery: "link"`) for the administrator to deliver themselves; the Users page shows
  it once with a copy button, and it is never listed again. With email the answer is
  `delivery: "email"` and holds no link. Both answers carry the same
  `"If this person exists, they will get an invitation"`.
- `/invitations/<token>` is a React page outside the app shell. A signed-out visitor is
  sent to sign in first and back. The signed-in person sees the company, the role, who
  invited them and until when, and accepts or declines:
  `POST /api/auth/invitations/preview | accept | decline` with the token in the body
  (never in an API URL); `accept` also carries the role shown. The three routes are
  public auth paths that name no permission (ADR 0076 §6): the middleware still
  authenticates the caller and checks CSRF, and the route requires a person's session
  itself (401 without one). Any session that is the
  person answers: a password session or an external sign-in session, also one in an
  account where they have no role. A personal access token, an OAuth grant or a service
  account is refused (403). After accepting, the page switches the session into the
  joined company where the session may move (a pinned external session stays).
- Whoever holds the link and is signed in answers it: the typed address is kept only
  for the administrators' list and is compared with nobody. The answer closes the
  invitation for good and records who answered (`user_id`).
- A link that names no open invitation (answered, cancelled, expired, replaced, of an
  inactive account, or whose inviter is no longer its admin) is answered 404 "This
  invitation is no longer valid", whatever the reason.
- `GET /api/auth/invitations` and `POST /api/auth/invitations/{id}/accept|decline`
  (matched by username) are removed, with the account menu's invitations and count.

### C2. Uniform answers

- `POST /api/auth/users` with an address someone already has creates no user and
  confirms nothing: the person is invited to the account the user would have been
  created in, with the requested role (default viewer), and the answer is the
  invitation's own (`status`, `message`, `delivery`, once `invitation_link`). Residual:
  a new address still answers with the created user, so the two outcomes differ in
  shape; the invitation branch is rate-limited per administrator, account and
  recipient (P4), which bounds what that difference can be used for.
- `POST /api/users/me/email-change/request` answers the same whether or not the new
  address is taken: the request is stored and answered `verification_sent` either way.
  For a taken address its owner gets a notice that the address was entered for another
  sign-in ("email_address_in_use", no link) instead of a verification link, so the
  change can never be verified; without system email both cases answer the same 503.
  The verification route still refuses a taken address (`email_taken`).

### C3. Roles of the visible account only

`limit_view_to_accounts` recomputes `roles` from the membership of one visible
account: the account the change named (`PUT`: `role_account_id`, else the acting
account; `POST`: the account the person was created in; the list: the acting account),
else the view's visible account. A role of an account the administrator does not
manage is never returned; no membership role there is `[]`.

### C4. An invitation speaks for an active administrator

- An invitation is open only while its inviter is an active, undeleted person with the
  admin role in the invitation's account (`membership_roles.INVITER_IS_ADMIN_PREDICATE`).
  Preview, accept and the P5 sign-in check it; otherwise the link is "no longer valid".
- When a person stops being an administrator there (a demotion, deactivation, deletion,
  a removed membership, or `ExternalSignInPort.remove_role`), their pending invitations
  where they are no admin any more are cancelled in the same transaction
  (`cancel_invitations_of_former_admins`) and each is audited
  (`account_invitation_cancel`, reason `inviter_no_longer_admin`).

### P1. A new invitation replaces the token

Inviting the same address to the same account again (with another role or the same)
renews the pending row with a new token: the earlier link stops working. Accepting
sends the role the person was shown, and a role that differs from the invitation's is
refused with 409 "This invitation has changed".

### P2. Audit

`account_member_invite` records the address as typed, the role, the inviter's id, the
expiry, the delivery (`email` or `link`) and the email outcome. Expired invitations a new
one deletes are recorded as `account_invitation_expire` (id, address, role), and every
cancellation as `account_invitation_cancel` with the address, role and reason
(`cancelled_by_administrator` or `inviter_no_longer_admin`). Accept and decline record
who answered.

### P3. Addresses

An invited address is trimmed, must be plain ASCII and unchanged by NFKC plus case
folding apart from case (a Kelvin sign or a full-width `@` is refused: such an address
could look like another one), and must pass `normalize_email_address`. The normalized
address receives the email and keys the per-recipient limit; the address as typed is
stored for the list, and the pending key compares it case-insensitively.

### P4. Limits in the shared store

Candidate searches and invitations are counted in `public.auth_rate_limits` (the store
the sign-in and OAuth limiters share), in the caller's transaction, so the limits hold
across processes and a refused or rolled-back request counts nothing:

| What | Per administrator | Per account | Per recipient |
| --- | --- | --- | --- |
| Candidate searches (10 minutes) | 60 | 200 | — |
| Invitations (10 minutes; recipient: 24 hours) | 20 | 50 | 5 |

A store failure fails the request closed.

### P5. Signing in only to answer an invitation

A person with a role in no account is still refused at sign-in (`no_role_in_account`),
unless the sign-in carries the token of an open invitation (`invitation_token`, sent
by the sign-in page when it returns to `/invitations/<token>`). The session then lands
in the person's home account, where they have no role, and the login answer says
`invitation_only: true`. The middleware refuses every person principal without a
system role in its account (a session or a personal token) each route that needs a
permission, with 403 `no_role_in_account`: the permission catalogue reads "no role" as
a viewer (the ADR 0066 ladder), which ADR 0085 §1 forbids, so the refusal comes first.
Such a principal reaches only the routes any signed-in principal may call (its own
session reads such as `/api/auth/me` and the bootstrap, and the invitation link, whose
route refuses tokens); a session may also sign out and switch to an account where it
has a role (the switch route checks that).

### Schema

Migration `20261127190000_account_invitations.sql` was edited in place (unreleased):
`token_hash` with its unique key and length check; the lookup index by address
(`idx_account_invitations_pending_email`) is gone. Its checksum and the baseline
fingerprint are recomputed; `account_invitations_token_hash_key` joins the runtime
schema contract. Verified with psql on PostgreSQL 17
(`fs-postgres-extended:0.1.0-pgvector-0.8.5`): fresh, re-run, and upgrade from
2635b86f3d (migration applied twice) give the same columns, constraints and indexes;
a duplicate, short or missing token hash is refused.

## Not in this stage

- A `user_roles.account_id` column naming a scoped row's account explicitly (today
  the account is derived from the place).

## Security notes

- Switching accounts changes permissions on the next request because nothing
  caches roles; the proof is in `tests/db/test_roles_per_account_postgres.py`.
- A membership removed or downgraded takes effect for sessions, personal access
  tokens and OAuth grants at their next use, and what was issued in that account is
  revoked at once.
- A person joins another account only by accepting an invitation, unless they already
  belong to an account the administrator manages. Accepting needs the invitation's
  single-use link and a signed-in person session; the typed address grants nothing.
- A person who is admin in A and viewer in B cannot manage B's people, nor
  demote or delete a person of A from B.
- The last administrator of an account cannot be removed from that account by
  demotion, deactivation, membership removal or deletion.

## Consequences

- Single-account installations see no change in behaviour.
- `user_roles` holds scoped rows only; code that needs a person's role in an
  account reads it through `core.application.auth.membership_roles` (the
  infrastructure reader is `core.infrastructure.db.membership_role_reader`).
- Test fixtures that seeded a global `user_roles` row now set the membership
  role.
