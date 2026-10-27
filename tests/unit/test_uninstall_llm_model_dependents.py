"""The uninstall plan of an LLM provider extension names who uses its models.

Uninstalling the provider deletes its provider sources, so an agent whose model
is one of them, or a project whose default model is, stops working. The plan
used to list nothing under "affected" for them.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.extensions.uninstall_inventory_repository import (
    ExtensionUninstallInventoryRepository,
)
from core.application.extensions.uninstall_llm_model_dependents import (
    collect_llm_model_dependents,
)
from core.application.extensions.uninstall_service import ExtensionUninstallService
from core.application.management.errors import ServiceValidationError

_KEY = "llmext_acme_owner_pi_acme_0123456789ab"


class _Cursor:
    """Answers each statement by the table it reads, like the production store."""

    def __init__(self) -> None:
        self.description = None
        self._rows: list[dict] = []
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, statement: str, params: tuple = ()) -> None:
        self.statements.append((" ".join(statement.split()), params))
        if "FROM llm_provider_sources" in statement:
            self._rows = [{"provider_key": _KEY, "display_label": "Acme (Acme LLM)"}]
        elif "FROM registry_agents" in statement:
            self._rows = [
                {
                    "agent_id": "acme_answerer",
                    "display_name": "Acme Answerer",
                    "account_id": "account-1",
                    "provider_key": _KEY,
                    "model_id": "acme/small",
                }
            ]
        elif "FROM projects" in statement:
            self._rows = [
                {
                    "project_id": "project-1",
                    "display_name": "Acme Project",
                    "account_id": "account-1",
                    "provider_key": _KEY,
                    "model_id": "acme/large",
                }
            ]
        else:
            self._rows = []

    def fetchall(self) -> list[dict]:
        return self._rows


def _store(cursor: _Cursor, tables: set[str]) -> SimpleNamespace:
    @contextmanager
    def _cursor():
        yield cursor

    return SimpleNamespace(cursor=_cursor, _table_exists=lambda table: table in tables)


_ALL = {"llm_provider_sources", "registry_agents", "projects"}


def test_agents_and_projects_using_the_providers_models_are_listed_by_name() -> None:
    cursor = _Cursor()

    affected = collect_llm_model_dependents(
        _store(cursor, _ALL),
        install_id="pi_acme",
        account_id="account-1",
        include_consumer_accounts=False,
    )

    assert affected == [
        {
            "kind": "agent",
            "id": "acme_answerer",
            "display_name": "Acme Answerer",
            "owner_account_id": "account-1",
            "action": "stops_working",
            "reason": (
                "Uses the model acme/small (Acme (Acme LLM)); the agent can't run until it "
                "is given another model."
            ),
        },
        {
            "kind": "project",
            "id": "project-1",
            "display_name": "Acme Project",
            "owner_account_id": "account-1",
            "action": "stops_working",
            "reason": (
                "Its default model is acme/large (Acme (Acme LLM)); runs in this project fail "
                "until another default model is chosen."
            ),
        },
    ]
    sources_sql, sources_params = cursor.statements[0]
    assert "extension_install_id = %s" in sources_sql
    assert sources_params == ("pi_acme", False, "account-1")
    agents_sql, agents_params = cursor.statements[1]
    assert "config->>'provider' = ANY(%s)" in agents_sql
    assert agents_params == ([_KEY],)
    projects_sql, _ = cursor.statements[2]
    assert "default_provider" in projects_sql
    assert "model_provider_id" in projects_sql


def test_a_provider_with_no_sources_lists_nothing_and_reads_nothing_more() -> None:
    cursor = _Cursor()
    cursor.execute = MagicMock(wraps=cursor.execute)  # type: ignore[method-assign]
    cursor.fetchall = list  # type: ignore[method-assign]

    assert (
        collect_llm_model_dependents(
            _store(cursor, _ALL),
            install_id="pi_other",
            account_id="account-1",
            include_consumer_accounts=False,
        )
        == []
    )
    assert cursor.execute.call_count == 1


def test_a_non_llm_extension_has_no_provider_sources_table_to_read() -> None:
    assert (
        collect_llm_model_dependents(
            _store(_Cursor(), set()),
            install_id="pi_acme",
            account_id="account-1",
            include_consumer_accounts=False,
        )
        == []
    )


def test_the_uninstall_plan_carries_them_under_affected() -> None:
    affected = ExtensionUninstallInventoryRepository().collect_affected(
        bl=_store(_Cursor(), _ALL),
        extension_id="acme.llm",
        install_id="pi_acme",
        account_id="account-1",
        include_consumer_accounts=False,
        runtime_store=SimpleNamespace(list_projections=lambda **_kwargs: []),
        install_store=MagicMock(),
    )

    assert [(row["kind"], row["display_name"]) for row in affected] == [
        ("agent", "Acme Answerer"),
        ("project", "Acme Project"),
    ]


@pytest.mark.parametrize("with_workflow", [False, True])
def test_listed_model_users_do_not_block_the_uninstall(with_workflow: bool) -> None:
    rows = [
        {"kind": "agent", "id": "acme_answerer", "action": "stops_working"},
        {"kind": "project", "id": "project-1", "action": "stops_working"},
    ]
    if with_workflow:
        rows.append({"kind": "workflow", "id": "acme_flow", "action": "preserve_and_disable"})

    ExtensionUninstallService._reject_required_dependents_in_plan(
        {"affected": rows}, extension_id="acme.llm"
    )


def test_a_required_dependent_still_blocks_beside_listed_model_users() -> None:
    with pytest.raises(ServiceValidationError, match="required dependents"):
        ExtensionUninstallService._reject_required_dependents_in_plan(
            {
                "affected": [
                    {"kind": "agent", "id": "acme_answerer", "action": "stops_working"},
                    {"kind": "dataset", "id": "acme_data", "action": "preserve_and_disable"},
                ]
            },
            extension_id="acme.llm",
        )
