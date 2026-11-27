"""A person's approval steps when they leave a company or stop deciding there (ADR 0085)."""

from __future__ import annotations

from typing import Any

from core.application.auth.approver_release import (
    HANDED_TO_ADMINS_BY,
    release_approver,
    release_if_no_longer_deciding,
    role_decides_approvals,
)

USER = "11111111-1111-4111-8111-111111111111"
ADMIN = "22222222-2222-4222-8222-222222222222"
ACCOUNT = "33333333-3333-4333-8333-333333333333"


class _Cursor:
    """Answers by statement: released steps, the approvers left, the admins inserted."""

    def __init__(self, *, released: list[tuple], remaining: int, admins: list[str]) -> None:
        self.released, self.remaining, self.admins = released, remaining, admins
        self.permissions: list[tuple[str, str]] = []
        self.calls: list[tuple[str, Any]] = []
        self._rows: list[Any] = []

    def execute(self, sql: str, params: Any = None) -> None:
        text = " ".join(sql.split())
        self.calls.append((text, params))
        if text.startswith("DELETE FROM approval_step_approvers"):
            self._rows = list(self.released)
        elif text.startswith("SELECT count(*)"):
            self._rows = [(self.remaining,)]
        elif text.startswith("INSERT INTO approval_step_approvers"):
            self._rows = [(admin,) for admin in self.admins]
        elif "FROM role_permissions" in text:
            self._rows = list(self.permissions)
        else:
            self._rows = []

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


def test_a_sole_approver_hands_the_step_to_the_administrators() -> None:
    cur = _Cursor(released=[(ACCOUNT, "wf", "sign_off")], remaining=0, admins=[ADMIN])

    released = release_approver(cur, user_id=USER, account_id=ACCOUNT)

    assert released == [
        {
            "account_id": ACCOUNT,
            "workflow_id": "wf",
            "step_id": "sign_off",
            "handed_to_admins": [ADMIN],
        }
    ]
    insert = next(params for sql, params in cur.calls if sql.startswith("INSERT"))
    # Never the person who leaves, and marked as handed over.
    assert insert == ("wf", "sign_off", HANDED_TO_ADMINS_BY, ACCOUNT, USER)


def test_a_step_with_other_approvers_is_left_to_them() -> None:
    cur = _Cursor(released=[(ACCOUNT, "wf", "sign_off")], remaining=1, admins=[ADMIN])

    released = release_approver(cur, user_id=USER, account_id=ACCOUNT)

    assert released[0]["handed_to_admins"] == []
    assert not any(sql.startswith("INSERT") for sql, _ in cur.calls)


def test_release_without_an_account_covers_every_account() -> None:
    cur = _Cursor(released=[], remaining=0, admins=[])

    assert release_approver(cur, user_id=USER) == []
    assert cur.calls[0][1] == (USER, None, None)


def test_who_decides_is_read_from_the_stored_role_permissions() -> None:
    cur = _Cursor(released=[], remaining=0, admins=[])
    assert role_decides_approvals(cur, "admin") is True
    assert role_decides_approvals(cur, None) is False
    # No stored rows: the system roles' lists apply (operator decides, viewer does not).
    assert role_decides_approvals(cur, "operator") is True
    assert role_decides_approvals(cur, "viewer") is False
    # Stored rows decide: an operator without the permission does not.
    cur.permissions = [("operator", "jobs.read"), ("viewer", "jobs.read")]
    assert role_decides_approvals(cur, "operator") is False


def test_a_demotion_that_keeps_deciding_releases_nothing() -> None:
    cur = _Cursor(released=[(ACCOUNT, "wf", "sign_off")], remaining=0, admins=[ADMIN])

    assert (
        release_if_no_longer_deciding(cur, user_id=USER, account_id=ACCOUNT, role="operator") == []
    )
    assert release_if_no_longer_deciding(cur, user_id=USER, account_id=ACCOUNT, role="viewer")[0][
        "handed_to_admins"
    ] == [ADMIN]
