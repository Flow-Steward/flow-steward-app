"""An extension's agent and skill must save through both authoring paths — WF-003.

The catalog advertises host-owned and extension-projected agents and skills side
by side. Authoring a step that names a projected one used to fail two different
ways: the incremental route claimed the agent "has no primary skill", and bulk
creation wrote the projected id into a column whose foreign key points at
`registry_skills` and returned an empty 500.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.application.registry import step_skill_resolution
from core.application.registry.step_skill_resolution import (
    agent_primary_skill_id,
    catalog_skill_row,
    skill_is_host_owned,
)

HOST_AGENT = "support_agent"
HOST_SKILL = "write_reply"
PROJECTED_AGENT = "extension.acme.triage_agent"
PROJECTED_SKILL = "extension.acme.triage_question"


class _Db:
    """Only what the resolver asks of a store."""

    def __init__(self) -> None:
        self.skills = {HOST_SKILL: {"skill_id": HOST_SKILL, "enabled": True}}
        self.agents = {HOST_AGENT: {"agent_id": HOST_AGENT, "enabled": True}}
        self.agent_skills = {HOST_AGENT: [{"skill_id": HOST_SKILL, "sort_order": 0}]}

    def get_skill(self, skill_id: str):
        return self.skills.get(skill_id)

    def get_agent(self, agent_id: str):
        return self.agents.get(agent_id)

    def get_agent_skills(self, agent_id: str):
        return self.agent_skills.get(agent_id, [])


@pytest.fixture
def db(monkeypatch) -> _Db:
    store = _Db()
    projected = {
        ("skill", "skill_id"): [{"skill_id": PROJECTED_SKILL, "enabled": True}],
        ("agent", "agent_id"): [
            {
                "agent_id": PROJECTED_AGENT,
                "enabled": True,
                "base_skill": PROJECTED_SKILL,
                "skills": [{"skill_id": PROJECTED_SKILL, "sort_order": 0}],
            }
        ],
    }

    def fake_get_registry_artifact_row(
        _store: Any,
        *,
        artifact_kind: str,
        id_key: str,
        artifact_id: str,
        platform_getter: str,
        **_kw,
    ):
        getter = getattr(store, platform_getter, None)
        if callable(getter):
            row = getter(artifact_id)
            if isinstance(row, dict):
                return dict(row)
        return next(
            (
                dict(row)
                for row in projected.get((artifact_kind, id_key), [])
                if row.get(id_key) == artifact_id
            ),
            None,
        )

    monkeypatch.setattr(
        step_skill_resolution, "get_registry_artifact_row", fake_get_registry_artifact_row
    )
    return store


def test_a_projected_agent_has_a_primary_skill(db: _Db) -> None:
    """The message an author used to get — "has no primary skill" — was untrue."""
    assert agent_primary_skill_id(db, PROJECTED_AGENT) == PROJECTED_SKILL


def test_a_host_agent_still_resolves_from_its_bound_skills(db: _Db) -> None:
    assert agent_primary_skill_id(db, HOST_AGENT) == HOST_SKILL


def test_an_unknown_agent_resolves_to_nothing(db: _Db) -> None:
    assert agent_primary_skill_id(db, "no_such_agent") == ""


def test_a_projected_skill_is_visible_to_the_authoring_check(db: _Db) -> None:
    assert catalog_skill_row(db, PROJECTED_SKILL) == {
        "skill_id": PROJECTED_SKILL,
        "enabled": True,
    }
    assert catalog_skill_row(db, "no_such_skill") is None


def test_only_a_host_owned_skill_may_enter_the_foreign_key_column(db: _Db) -> None:
    """The distinction that keeps bulk authoring from dying on the FK."""
    assert skill_is_host_owned(db, HOST_SKILL) is True
    assert skill_is_host_owned(db, PROJECTED_SKILL) is False
    assert skill_is_host_owned(db, "") is False


def test_the_resolver_honours_a_non_default_account(monkeypatch, db: _Db) -> None:
    """Requirement 8: a projected capability installed for another account.

    Resolving in the default account is why authoring answered 404 for something
    the operator could see in their own account's catalog.
    """
    seen: list[str] = []
    real = step_skill_resolution.get_registry_artifact_row

    def recording(store, *, account_id="", **kwargs):
        seen.append(account_id)
        return real(store, account_id=account_id, **kwargs)

    monkeypatch.setattr(step_skill_resolution, "get_registry_artifact_row", recording)

    agent_primary_skill_id(db, PROJECTED_AGENT, account_id="account-non-default")

    assert seen == ["account-non-default"], "the caller's account must reach the lookup"


def test_catalog_lookups_pass_their_account_through(monkeypatch, db: _Db) -> None:
    seen: list[str] = []
    real = step_skill_resolution.get_registry_artifact_row

    def recording(store, *, account_id="", **kwargs):
        seen.append(account_id)
        return real(store, account_id=account_id, **kwargs)

    monkeypatch.setattr(step_skill_resolution, "get_registry_artifact_row", recording)

    catalog_skill_row(db, PROJECTED_SKILL, account_id="account-non-default")
    step_skill_resolution.catalog_agent_row(db, PROJECTED_AGENT, account_id="account-non-default")

    assert seen == ["account-non-default", "account-non-default"]
