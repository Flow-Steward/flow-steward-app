"""The role a person holds in one account (ADR 0085).

A person's role is stored on their membership of an account
(``user_account_memberships.role_id``). This is the one reader every principal
resolver uses: the session, the serve runtime, personal access tokens and inbound
OAuth bearer tokens. A person without a membership of the account, a membership
without a role, an inactive account, or an empty account has no role: the
installation account is never substituted.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

#: The role keys of ``%(user_id)s`` in ``%(account_id)s``: zero or one row.
MEMBERSHIP_ROLE_SQL = """
SELECT r.key
  FROM public.user_account_memberships AS m
  JOIN public.roles AS r ON r.id = m.role_id
  JOIN public.accounts AS a ON a.id = m.account_id AND a.is_active
 WHERE m.user_id = %s::uuid
   AND m.account_id = %s::uuid
"""


def _uuid_text(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except (ValueError, AttributeError, TypeError):
        return ""


def membership_role_keys(cur: Any, user_id: Any, account_id: Any) -> list[str]:
    """The person's role keys in the account through ``cur``; ``[]`` for no role."""
    user, account = _uuid_text(user_id), _uuid_text(account_id)
    if not user or not account:
        return []
    cur.execute(MEMBERSHIP_ROLE_SQL, (user, account))
    keys: list[str] = []
    for row in cur.fetchall() or ():
        value = next(iter(row.values()), None) if isinstance(row, Mapping) else row[0]
        key = str(value or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


__all__ = ["MEMBERSHIP_ROLE_SQL", "membership_role_keys"]
