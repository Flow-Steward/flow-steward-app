"""Persistence operations for local-auth user administration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.auth.account_memberships import sync_user_account_memberships
from core.application.auth.audit import log_auth_audit_event
from core.application.auth.external_session_revocation import (
    accounts_changed,
    membership_account_ids,
    revoke_external_sign_in_grants,
    roles_changed,
)
from core.application.auth.external_sign_in_repository import AWAITING_INVITATION_MARKER
from core.application.auth.installation_admin import INSTALLATION_ACCOUNT_ID
from core.application.auth.local_credentials import authenticator_policy_available
from core.application.auth.membership_roles import (
    LastAccountAdminError,
    cancel_invitations_of_former_admins,
    keep_one_role,
    membership_role_keys,
    require_other_admins,
    revoke_account_access,
    roles_by_account,
)
from core.application.auth.password import password_hash
from core.application.auth.password_history import require_unused_password
from core.application.auth.permissions import ADMIN_ROLE
from core.application.auth.role_assignments import (
    global_assignments,
    replace_user_role_assignments,
)
from core.application.auth.scoped_role_ports import (
    ACCOUNT_SCOPED_ROW_PREDICATE,
    SCOPE_LIMIT_LIFT_AUDIT_ACTION,
    record_scope_limit_audit,
)
from core.application.management.errors import ServiceConflictError, ServiceNotFoundError
from core.infrastructure.db.db_pool import transaction_connection


def _commit_if_possible(bl: Any) -> None:
    commit = getattr(getattr(bl, "conn", None), "commit", None)
    if callable(commit):
        commit()


def _lock_user_lifecycle(cur: Any) -> None:
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        ("flow-steward:user-lifecycle",),
    )


def _lift_scope_limit(
    cur: Any, user_id: str, account_id: str, *, actor: str, username: str
) -> bool:
    """Remove the person's scope limit in the account; ``True`` if one existed.

    An administrator is never limited (ADR 0076 section 8): promoting a limited person
    to admin of an account lifts their limit there (and only there, ADR 0085), and
    records the lift in the auth audit, in the same transaction. The caller holds the
    user-lifecycle lock.
    """
    cur.execute(
        "DELETE FROM scoped_role_users WHERE user_id = %s AND account_id = %s RETURNING user_id",
        (user_id, account_id),
    )
    if cur.fetchone() is None:
        return False
    cur.execute(
        f"DELETE FROM user_roles AS ur WHERE ur.user_id = %s AND {ACCOUNT_SCOPED_ROW_PREDICATE}",  # nosec B608 - a constant predicate.
        (user_id, account_id, account_id),
    )
    record_scope_limit_audit(
        cur,
        action=SCOPE_LIMIT_LIFT_AUDIT_ACTION,
        actor=actor,
        username=username,
        detail={
            "user_id": user_id,
            "account_id": account_id,
            "was_limited": True,
            "reason": "promoted_to_admin",
        },
    )
    return True


def _require_admins_kept(
    cur: Any,
    *,
    user_id: str,
    deactivated: bool,
    role_account: str | None,
    new_role: str | None,
    kept_accounts: list[Any] | None,
) -> None:
    """Every account keeps an active administrator through the change (ADR 0085).

    The person stops administering an account when they are deactivated, when their
    role there changes to another one, or when that membership is removed.
    """
    admin_of = {
        account for account, role in roles_by_account(cur, user_id).items() if role == ADMIN_ROLE
    }
    if not admin_of:
        return
    losing: set[str] = set()
    if deactivated:
        losing |= admin_of
    if role_account is not None and new_role != ADMIN_ROLE:
        losing |= admin_of & {role_account}
    if kept_accounts is not None:
        losing |= admin_of - {str(item or "").strip() for item in kept_accounts}
    try:
        require_other_admins(cur, user_id=user_id, account_ids=losing)
    except LastAccountAdminError as exc:
        raise ServiceConflictError(str(exc)) from exc


#: The role every installation's first user gets, whatever the channel (CLI, API).
FIRST_USER_ROLE = "admin"
#: Roles of a user created without explicit roles once an installation has users.
DEFAULT_USER_ROLES = ("viewer",)


class FirstUserMustBeAdminError(ValueError):
    """Explicit roles without admin for the first user would lock everyone out."""


def _roles_for_new_user(cur: Any, roles: list[str]) -> list[str]:
    """The roles to grant, decided under the user-lifecycle lock.

    The first user of an installation is always an administrator: without roles
    they get admin, and roles without admin are refused, so nobody ends up with
    an installation no one can administer.
    """
    cur.execute("SELECT EXISTS (SELECT 1 FROM users)")
    row = cur.fetchone()
    users_exist = bool(row[0]) if row else False
    if users_exist:
        return list(roles) or list(DEFAULT_USER_ROLES)
    if not roles:
        return [FIRST_USER_ROLE]
    if FIRST_USER_ROLE not in roles:
        raise FirstUserMustBeAdminError(
            "The first user must be an administrator: choose the admin role or leave the role out"
        )
    return list(roles)


def _table_exists(bl: Any, table_name: str) -> bool:
    checker = getattr(bl, "_table_exists", None)
    if not callable(checker):
        return True
    try:
        return bool(checker(table_name))
    except Exception:
        return False


def _revoke_user_auth_material(cur: Any, bl: Any, user_id: str) -> int:
    cur.execute(
        """
        UPDATE auth_sessions
        SET revoked = true, updated_at = now()
        WHERE user_id = %s
          AND revoked = false
        """,
        (user_id,),
    )
    revoked_sessions = int(getattr(cur, "rowcount", 0) or 0)
    if _table_exists(bl, "personal_access_tokens"):
        cur.execute(
            """
            UPDATE personal_access_tokens
            SET revoked = true, updated_at = now()
            WHERE user_id = %s
              AND revoked = false
            """,
            (user_id,),
        )
    return revoked_sessions


def list_auth_users(
    bl: Any,
    *,
    search: str,
    include_inactive: bool,
    include_deleted: bool,
    limit: int,
    offset: int,
    role: str,
    active_filter: bool | None,
    managed_account_ids: list[str] | None,
    role_account_id: str | None = None,
    member_account_id: str | None = None,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """People the administrator manages, with their role in ``role_account_id``.

    A role is held per account (ADR 0085): the listed role and the role filter are
    those of the person's membership of ``role_account_id`` (the administrator's
    acting account), or of their home account when none is given (the CLI).
    ``member_account_id`` lists only the members of that account (the users page
    lists the people of the acting account).
    """
    normalized_limit = max(1, min(int(limit or 50), 200))
    normalized_offset = max(0, int(offset or 0))
    normalized_search = str(search or "").strip()
    normalized_role = str(role or "").strip().lower()
    conditions: list[str] = [
        # People created only to answer an invitation (their own marker), waiting with
        # no company and no role in the installation account, are nobody's users yet.
        # Other people who sign in externally are listed as always.
        """
        AND NOT (
            u.password_hash = %s AND u.account_id::text = %s
            AND NOT EXISTS (
                SELECT 1 FROM user_account_memberships m_wait
                WHERE m_wait.user_id = u.id AND m_wait.role_id IS NOT NULL
            )
        )
        """
    ]
    params: list[Any] = [AWAITING_INVITATION_MARKER, str(INSTALLATION_ACCOUNT_ID)]

    if not include_deleted:
        conditions.append("AND u.deleted_at IS NULL")
    if active_filter is not None:
        conditions.append("AND u.is_active = %s")
        params.append(bool(active_filter))
    elif not include_inactive:
        conditions.append("AND u.is_active = true")
    if normalized_search:
        like = f"%{normalized_search}%"
        conditions.append("AND (u.username ILIKE %s OR COALESCE(u.display_name, '') ILIKE %s)")
        params.extend([like, like])
    role_account = str(role_account_id or "").strip() or None
    if normalized_role:
        # The membership role only: a project-scoped row (ADR 0076 section 8) narrows a
        # person inside one project and is not the person's role.
        conditions.append(
            """
            AND EXISTS (
                SELECT 1 FROM user_account_memberships m_scope
                JOIN roles r_scope ON r_scope.id = m_scope.role_id
                WHERE m_scope.user_id = u.id
                  AND m_scope.account_id = COALESCE(%s::uuid, u.account_id)
                  AND r_scope.key = %s
            )
            """
        )
        params.extend([role_account, normalized_role])
    if managed_account_ids is not None:
        normalized_account_ids = [
            str(account_id or "").strip()
            for account_id in managed_account_ids
            if str(account_id or "").strip()
        ]
        if not normalized_account_ids:
            conditions.append("AND FALSE")
        else:
            conditions.append(
                """
                AND (
                    u.account_id = ANY(%s)
                    OR EXISTS (
                        SELECT 1 FROM user_account_memberships uam_scope
                        WHERE uam_scope.user_id = u.id
                          AND uam_scope.account_id = ANY(%s)
                    )
                )
                """
            )
            params.extend([normalized_account_ids, normalized_account_ids])
    member_account = str(member_account_id or "").strip()
    if member_account:
        conditions.append(
            """
            AND EXISTS (
                SELECT 1 FROM user_account_memberships uam_member
                WHERE uam_member.user_id = u.id
                  AND uam_member.account_id::text = %s
            )
            """
        )
        params.append(member_account)

    where_sql = " ".join(conditions)
    authenticator_required = (
        "u.authenticator_required" if authenticator_policy_available(bl) else "false"
    )
    with _dict_cursor(bl) as cur:
        cur.execute(
            f"SELECT COUNT(*) AS total FROM users u WHERE 1 = 1 {where_sql}",  # nosec B608 - fixed predicates.
            tuple(params),
        )
        total_row = cur.fetchone() or {}
        if isinstance(total_row, dict):
            total = int(total_row.get("total") or 0)
        else:
            positional_total = total_row[0] if total_row else 0
            total = int(positional_total or 0)
        cur.execute(
            f"""
            SELECT u.id, u.username, u.display_name, u.is_active,
                   u.must_change_password AS must_change_password,
                   {authenticator_required} AS authenticator_required,
                   u.deleted_at AS deleted_at,
                   u.account_id AS account_id,
                   u.created_at, u.updated_at,
                   COALESCE(array_remove(array_agg(r.key ORDER BY r.key), NULL), ARRAY[]::text[]) AS roles
              FROM users u
              LEFT JOIN user_account_memberships rm
                ON rm.user_id = u.id AND rm.account_id = COALESCE(%s::uuid, u.account_id)
              LEFT JOIN roles r ON r.id = rm.role_id
             WHERE 1 = 1 {where_sql}
             GROUP BY u.id
             ORDER BY u.id
             LIMIT %s OFFSET %s
            """,  # nosec B608 - fixed projections and predicates.
            (role_account, *params, normalized_limit, normalized_offset),
        )
        rows = [dict(row or {}) for row in (cur.fetchall() or [])]
    return rows, total, normalized_limit, normalized_offset


def _dict_cursor(bl: Any):
    return _dict_cursor_for_connection(bl.conn)


def _dict_cursor_for_connection(conn: Any):
    try:
        from core.infrastructure.db.psycopg_runtime import extras as _pge2

        return conn.cursor(cursor_factory=_pge2.RealDictCursor)
    except Exception:
        return conn.cursor()


def create_auth_user(
    bl: Any,
    *,
    email: str,
    password_hash_value: str,
    display_name: str,
    is_active: bool,
    must_change_password: bool,
    roles: list[str],
    primary_account_id: str,
    requested_account_ids: list[str],
    seed_default_roles: Callable[[Any], None],
) -> str:
    seed_default_roles(bl)
    with bl.conn.cursor() as cur:
        _lock_user_lifecycle(cur)
        roles = _roles_for_new_user(cur, roles)
        cur.execute(
            """
            INSERT INTO users (
                username,
                password_hash,
                display_name,
                is_active,
                must_change_password,
                account_id
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                str(email or "").strip(),
                str(password_hash_value or ""),
                str(display_name or "").strip(),
                bool(is_active),
                bool(must_change_password),
                str(primary_account_id or "").strip(),
            ),
        )
        row = cur.fetchone()
        user_id = str((row[0] if row else 0) or "").strip()
        if not user_id:
            raise RuntimeError("failed to create user")
        # Every membership of the new person carries the requested role (ADR 0085).
        sync_user_account_memberships(
            bl,
            user_id=user_id,
            account_ids=requested_account_ids,
            primary_account_id=primary_account_id,
            new_membership_role=keep_one_role(roles),
        )
    _commit_if_possible(bl)
    return user_id


def update_auth_user(
    bl: Any,
    *,
    user_id: Any,
    existing_user: dict[str, Any],
    display_name: str | None,
    is_active: bool | None,
    password: str | None,
    must_change_password: bool | None,
    roles: list[str] | None,
    requested_account_ids: list[str] | None,
    primary_account_id: str | None,
    revoke_sessions: bool,
    password_hasher: Callable[[str], str] = password_hash,
    on_scope_limit_lifted: Callable[[], None] | None = None,
    actor: str = "",
    role_account_id: str | None = None,
    new_membership_role: str | None = "viewer",
) -> int:
    """Apply the changes in one transaction; returns the number of revoked sessions.

    ``roles`` sets the person's role in ``role_account_id`` (their home account when
    empty; ADR 0085). Promoting a person to admin of an account lifts their scope
    limit there, audited as done by ``actor``, in the same transaction;
    ``on_scope_limit_lifted`` is then called. Added memberships get
    ``new_membership_role``. Refused with :class:`ServiceConflictError` when an
    account would lose its last active administrator: by a demotion, a deactivation
    or a removed membership.

    A change of the role alone revokes only what the person holds in that account:
    its sessions, personal tokens and OAuth grants (ADR 0085, owner decision B).
    """
    normalized_user_id = str(user_id or "").strip()
    revoke_auth_material = (
        is_active is not None
        or password is not None
        or must_change_password is not None
        or roles is not None
        or requested_account_ids is not None
    )
    role_only = roles is not None and all(
        value is None
        for value in (
            display_name,
            is_active,
            password,
            must_change_password,
            requested_account_ids,
            primary_account_id,
        )
    )
    role_account = (
        str(role_account_id or "").strip() or str(existing_user.get("account_id") or "").strip()
    )
    with transaction_connection(bl.conn) as conn, conn.cursor() as cur:
        locked = is_active is not None or roles is not None or requested_account_ids is not None
        if locked:
            _lock_user_lifecycle(cur)
        if password is not None:
            cur.execute(
                "SELECT password_hash FROM users WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (normalized_user_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ServiceNotFoundError(f"User id={normalized_user_id} not found")
            current_hash = row["password_hash"] if isinstance(row, dict) else row[0]
            require_unused_password(
                cur,
                user_id=normalized_user_id,
                password=password,
                current_hash=str(current_hash or ""),
            )
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (password_hasher(password), normalized_user_id),
            )
        if display_name is not None:
            cur.execute(
                "UPDATE users SET display_name = %s WHERE id = %s",
                (
                    str(display_name or "").strip() or existing_user.get("username"),
                    normalized_user_id,
                ),
            )
        if is_active is not None:
            cur.execute(
                "UPDATE users SET is_active = %s WHERE id = %s",
                (bool(is_active), normalized_user_id),
            )
        if must_change_password is not None:
            cur.execute(
                "UPDATE users SET must_change_password = %s WHERE id = %s",
                (bool(must_change_password), normalized_user_id),
            )
        if locked:
            # Checked in the same transaction as the writes above: a refusal undoes them.
            _require_admins_kept(
                cur,
                user_id=normalized_user_id,
                deactivated=is_active is False,
                role_account=role_account if roles is not None else None,
                new_role=keep_one_role(roles or []),
                kept_accounts=(
                    [*requested_account_ids, primary_account_id]
                    if requested_account_ids is not None and primary_account_id is not None
                    else None
                ),
            )
        stored_roles: list[str] = []
        synced_accounts: list[str] | None = None
        if requested_account_ids is not None and primary_account_id is not None:
            cur.execute(
                "UPDATE users SET account_id = %s WHERE id = %s",
                (str(primary_account_id or "").strip(), normalized_user_id),
            )
            synced_accounts = [
                *sync_user_account_memberships(
                    bl,
                    user_id=normalized_user_id,
                    account_ids=requested_account_ids,
                    primary_account_id=primary_account_id,
                    new_membership_role=keep_one_role(roles or []) or new_membership_role,
                ),
                str(primary_account_id or "").strip(),
            ]
        roles_before: list[str] | None = None
        if roles is not None:
            roles_before = membership_role_keys(cur, normalized_user_id, role_account)
            try:
                stored_roles = replace_user_role_assignments(
                    cur,
                    user_id=normalized_user_id,
                    account_id=role_account,
                    assignments=global_assignments(roles),
                )
            except LookupError as exc:
                raise ServiceNotFoundError(
                    "The person is not a member of the account whose role was to change"
                ) from exc
            if (
                ADMIN_ROLE in stored_roles
                and _lift_scope_limit(
                    cur,
                    normalized_user_id,
                    role_account,
                    actor=actor,
                    username=str(existing_user.get("username") or ""),
                )
                and on_scope_limit_lifted is not None
            ):
                on_scope_limit_lifted()
        revoked_sessions = 0
        if role_only:
            if revoke_sessions:
                revoked_sessions = revoke_account_access(
                    cur,
                    user_id=normalized_user_id,
                    account_id=role_account,
                    reason="account_role_changed",
                )
            elif roles_changed(roles_before, stored_roles):
                revoke_external_sign_in_grants(cur, [normalized_user_id], account_id=role_account)
        elif revoke_sessions and revoke_auth_material:
            revoked_sessions = _revoke_user_auth_material(cur, bl, normalized_user_id)
        elif _external_sign_in_rules_may_change(
            existing_user,
            is_active=is_active,
            roles=stored_roles if roles is not None else None,
            account_ids=synced_accounts,
        ):
            revoke_external_sign_in_grants(cur, [normalized_user_id])
        cancelled_invitations = (
            cancel_invitations_of_former_admins(cur, normalized_user_id)
            if is_active is False or roles is not None or requested_account_ids is not None
            else []
        )
    _audit_cancelled_invitations(bl, cancelled_invitations, actor=actor)
    return revoked_sessions


def _audit_cancelled_invitations(bl: Any, cancelled: list[dict[str, str]], *, actor: str) -> None:
    """Record invitations that ended with their inviter's admin role (ADR 0085, C4)."""
    for item in cancelled:
        log_auth_audit_event(
            bl,
            action="account_invitation_cancel",
            username="",
            actor=str(actor or "").strip() or "admin",
            status="ok",
            detail={
                "account_id": item.get("account_id", ""),
                "invitation_id": item.get("id", ""),
                "invited_email": item.get("invited_email", ""),
                "role": item.get("role", ""),
                "reason": "inviter_no_longer_admin",
            },
        )


def _external_sign_in_rules_may_change(
    existing_user: dict[str, Any],
    *,
    is_active: bool | None,
    roles: list[str] | None,
    account_ids: list[str] | None,
) -> bool:
    """Whether the change touches what external sign-in checked (ADR 0083): the
    person's global roles, their accounts, or whether they may sign in at all."""
    if is_active is False:
        return True
    if roles is not None and roles_changed(
        existing_user.get("roles") if "roles" in existing_user else None, roles
    ):
        return True
    return account_ids is not None and accounts_changed(
        membership_account_ids(existing_user), account_ids
    )


def force_password_reset(
    bl: Any,
    *,
    user_id: Any,
) -> int:
    with bl.conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET must_change_password = true WHERE id = %s",
            (str(user_id or "").strip(),),
        )
        revoked_sessions = _revoke_user_auth_material(cur, bl, str(user_id or "").strip())
    _commit_if_possible(bl)
    return revoked_sessions


def delete_auth_user(
    bl: Any,
    *,
    user_id: Any,
    deleted_by: str,
) -> int:
    normalized_user_id = str(user_id or "").strip()
    with (
        transaction_connection(bl.conn) as conn,
        _dict_cursor_for_connection(conn) as cur,
    ):
        _lock_user_lifecycle(cur)
        cur.execute(
            """
            SELECT u.id::text AS id, u.is_active
            FROM users u
            WHERE u.id = %s AND u.deleted_at IS NULL
            FOR UPDATE OF u
            """,
            (normalized_user_id,),
        )
        target = cur.fetchone()
        if not target:
            raise ServiceNotFoundError(f"User id={normalized_user_id!s} not found")

        cur.execute("SELECT COUNT(*) AS total FROM users WHERE deleted_at IS NULL")
        total_row = cur.fetchone() or {}
        total = int((total_row.get("total") if isinstance(total_row, dict) else total_row[0]) or 0)
        if total <= 1:
            raise ServiceConflictError("The last user cannot be deleted")

        if bool(target.get("is_active")):
            # Every account the person administers keeps another active
            # administrator (ADR 0085: the last-administrator rule is per account).
            admin_of = [
                account
                for account, role in roles_by_account(cur, normalized_user_id).items()
                if role == ADMIN_ROLE
            ]
            try:
                require_other_admins(cur, user_id=normalized_user_id, account_ids=admin_of)
            except LastAccountAdminError as exc:
                raise ServiceConflictError(
                    "The last active administrator of an account cannot be deleted"
                ) from exc

        revoked_sessions = _revoke_user_auth_material(cur, bl, normalized_user_id)
        cur.execute(
            """
            UPDATE users
            SET is_active = false,
                deleted_at = now(),
                deleted_by = %s,
                updated_at = now()
            WHERE id = %s AND deleted_at IS NULL
            """,
            (str(deleted_by or "").strip(), normalized_user_id),
        )
        identities_checker = getattr(bl, "_table_exists", None)
        if callable(identities_checker) and identities_checker("external_identities") is True:
            # A deleted user's external identity must not sign anyone in again
            # (ADR 0083); its sessions go with it.
            # Compare as text: a malformed id must not abort the deletion transaction.
            cur.execute(
                "DELETE FROM external_identities WHERE user_id::text = %s",
                (normalized_user_id,),
            )
        cancelled_invitations = cancel_invitations_of_former_admins(cur, normalized_user_id)
    _audit_cancelled_invitations(bl, cancelled_invitations, actor=deleted_by)
    from core.application.auth.user_lifecycle_events import notify_user_deleted

    notify_user_deleted(normalized_user_id)
    return revoked_sessions
