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

## Leaving a company (2026-11-27, scenario tests)

`tests/db/test_people_in_several_companies_postgres.py` and
`tests/db/test_people_who_move_companies_postgres.py` run a person through several
companies and a move from one to another. They led to these changes (the owner's
decisions of 2026-11-27 included):

- **Approver assignments go with the membership, and with the role that decides.** A
  person is taken off the account's approval steps (`approval_step_approvers`,
  `core/application/auth/approver_release.py`) when their membership is removed, when
  they are deleted (a soft delete, so the row's `ON DELETE CASCADE` never fires; every
  account), and when their role there changes to one without `approvals.decide` or to
  none (`set_membership_role`, the one writer of a membership role: the users API, the
  CLI and `ExternalSignInPort.remove_role` alike). A later membership or role never
  brings an old assignment back.
- **A step whose last approver goes is handed to the administrators.** It is never
  opened to everyone who may decide approvals (an approver list only narrows, ADR 0066).
  In the same transaction the account's active administrators at that moment become the
  step's assigned approvers (rows with `created_by = 'approver-left'`): they decide it,
  or assign someone else through the approvers port. This was chosen over a "fall back
  to administrators" flag because the decision check, the approvals queue and the
  `can_decide` view stay exactly as they are and read one list; an administrator who
  joins later is not added (assign them through the port). With no other active
  administrator (the last-administrator rule keeps one, so only an inconsistent
  account), the step keeps no rows and is unrestricted, as before. The removal's audit
  entry (`account_member_remove`) lists the steps in `released_approval_steps` with the
  administrators each went to (`handed_to_admins`).
- **Approvers are listed by membership.** `ApprovalAssignmentsPort.list_approvers` with
  an account kept only people whose home account it was; an approver who lives in
  another company and works here too is listed now, and one who left is not.
- **The next home has a role.** When a person leaves their home company, the new home
  is an active company where they have a role, before the default and the oldest
  membership.
- **Every role switches and manages its own settings.** `POST /api/auth/switch-account`,
  `PATCH /api/users/me/(profile|timezone|locale|date-time-format)` and
  `POST /api/users/me/detect-timezone` needed `account.profile.write`, which a viewer
  does not hold: a viewer in the acting company could not move to another of their
  companies (while a person with no role at all could, P5) nor change their own
  language, time zone or date format. They need `account.self` now, which every role
  holds. The switch route still checks the role in the target company and refuses an
  external session; an external session's own-account writes keep needing
  `account.profile.write` (ADR 0083), which only that rule uses now.

What stays: workflows, runs, history and schedules the person made remain the
account's (a due scheduled run is started by its schedule, never with the authority of
whoever set it up). Service accounts and machine tokens belong to the account, not to
the person who created them (§4), so they are not revoked; the listings
(`GET /api/auth/service-accounts` and its `/tokens`) carry `created_by_has_left: true`
when the person named in `created_by` is no active member of the account any more, and
Settings → Service accounts shows "Created by a person who has left" on such an
identity or token.

## Company owner (2026-11-27)

The person who decides for a company may invite outside
help, a freelancer for example, as an administrator. If they fall out, the freelancer
must never be able to remove or lock out that person. Every company therefore has one
**owner**, who keeps the ultimate rights.

### Who the owner is

- `accounts.owner_user_id` (foreign key to `users`, `ON DELETE SET NULL`) names the
  owner: a person who is a member with the admin role there.
- The first active administrator of an account becomes its owner. A trigger on
  `user_account_memberships` (`trg_user_account_memberships_first_admin_owner`, after
  an insert or a role change) sets the owner of an account that has none when an
  active, undeleted person gets the admin role there. So the person who creates their
  own company (self-service sign-up: account, person, membership, admin role through
  `set_membership_role`), the first administrator an installation administrator adds
  to a new company and the installation's first administrator all become owners, with
  no code path of their own. An account without an active administrator has no owner
  until it gets one.
- The upgrade gives every account its earliest active administrator membership
  (membership `created_at`, then the person's `created_at`, then the person's id).
- `core.application.auth.account_owner.is_account_owner(cur, user_id, account_id)` is
  the predicate for anything only the owner may do (transferring ownership, and later
  owner-only actions a platform module adds). It reads the database in the caller's transaction;
  nothing caches it. Memberships carry `is_owner` (the users list, `/api/auth/me`).

### What protects the owner

Two rules in `core/application/auth/account_owner.py`, checked in the transaction that
writes, refusing with `AccountOwnerProtectedError` (403, code `account_owner_protected`):

- **The owner stays** (`require_owner_kept`). Nobody, the owner included, removes the
  owner from the company, changes the owner's role there (to any role, even admin, since
  a role change ends that account's sessions), deactivates or deletes the owner. The
  owner transfers ownership first. Checked by `set_membership_role` (the one writer of
  a membership role: users API, CLI, `ExternalSignInPort.remove_role`, a module's
  grants), `revoke_account_access` (what a removal or a role change ends, and
  `ExternalSignInRevocationPort.revoke_access_in_account`), `remove_member`,
  `update_auth_user` (deactivation, role change, memberships set) and `delete_auth_user`.
  The user commands (`UserAdminCommandService`) check it first, before the request's
  input is looked at; `remove_member` checks it after the last-administrator rule.
- **Only the owner touches their own sign-in** (`require_owner_consent`). No one but
  the owner sets or resets the owner's password, requires a password change, changes
  their display name or memberships (which ends every session), clears their sign-in
  lockout, revokes their sessions, personal tokens or OAuth grants, unlinks their
  external sign-in identities or changes their authenticator. Checked by
  `UserAdminCommandService` (update, force reset, unlock, authenticator) before any
  input validation, so a person who may not act learns nothing such as a
  password-policy hint; again by `update_auth_user` and `force_password_reset` in the
  writing transaction; and by `ExternalSignInAdmin.unlink_identity`.
  The command line and platform modules name no person, so they are never the owner.

The rules hold for every company the person owns, whichever company the acting
administrator manages: an administrator of B cannot deactivate, delete or reset the
password of a person who owns A, though they may change that person's role in B or
remove them from B. The owner removes and demotes any other administrator.

### Transferring ownership

- `POST /api/auth/users/owner/transfer` `{user_id}`: the owner offers ownership of the
  acting company to another active administrator of it. The offer waits in
  `account_owner_transfers` (`id, account_id, from_user_id, to_user_id, status,
  created_at, expires_at, decided_at`; one pending row per account, 14 days; status
  `pending`, `accepted`, `declined` or `cancelled`); a new offer cancels the open one.
- `POST /api/auth/users/owner/transfer/accept|decline` `{transfer_id}`: the recipient,
  signed in as a person (never a token) and still an active administrator there,
  answers. Accepting makes them the owner; the former owner stays an administrator and
  can now be demoted or removed like anyone else. An offer that is not open for the
  caller is answered 404 "This ownership transfer is no longer open".
- `POST /api/auth/users/owner/transfer/cancel`: the owner withdraws the offer.
- `GET /api/auth/users/owner`: the owner, whether the caller is the owner, the open offer
  and whether it is for the caller.
- Settings → Users shows "Owner" on the owner's row and offers no action on it to
  anyone else; the owner has "Transfer ownership" on the rows of active
  administrators; the recipient sees the offer with Accept and Decline, the owner sees
  whom it waits for with Cancel.

### Recovery

When the owner has lost access, the installation administrator reassigns the owner of
any company, always with a reason: `POST /api/auth/accounts/{account_id}/owner`
`{user_id, reason}` (installation administrators only; a company administrator is
refused with `installation_admin_required`) or
`flow-steward users reassign-owner --account-id … --email … --reason …` (the
installation operator). The new owner must be an active member of the company; a member
without the admin role there gets it (and loses any scope limit there). An open offer is
cancelled. This is the only way to change an owner without the owner's consent, and it
is not available to company administrators.

### Not available to an account administrator

Core has no route by which an account administrator deletes or disables their company:
creating, disabling and enabling accounts stay with the installation administrator
(ADR 0084 §2). A module that adds such an action, or any other owner-only action, checks
`is_account_owner`.

### Audit

`account_owner_transfer_offer`, `_cancel`, `_accept`, `_decline` and
`account_owner_reassign` (with the reason, the previous owner and whether the new owner
was promoted) are written to the auth audit like member changes. A refused attempt
changes nothing and is answered with the refusal.

### Schema

Migration `db/migrations/20261127220000_account_owner.sql`, the same block at the end of
`db/schema/001_schema_part_12.sql`: the column and its index, the backfill, the trigger
function `accounts_first_admin_becomes_owner` and its trigger, the transfers table, its
pending key (`account_owner_transfers_pending_key`, part of the runtime schema contract
with the table and the column) and a recipient index. Every statement is safe to run
again.

### Tests

`tests/db/test_company_owner_postgres.py` (the scenarios above on PostgreSQL, the
harness in `tests/db/_companies.py` gains `owner_of` and `make_owner`) and
`tests/unit/test_account_owner.py` (the predicate, each refusal path and the routes).

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
