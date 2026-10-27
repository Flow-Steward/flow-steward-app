"""The legacy-schema account projection treats configured identity as data."""

from unittest.mock import MagicMock

import pytest

from core.application.notifications import repository


@pytest.mark.parametrize(
    "account_id",
    ["account-default", "account'quoted", "account' || current_user || '"],
)
@pytest.mark.parametrize("enabled_only", [False, True])
def test_reaction_account_projection_quotes_configured_default(
    monkeypatch, account_id: str, enabled_only: bool
) -> None:
    store = repository.NotificationRepositoryMixin()
    store.conn = MagicMock()
    store._table_exists = lambda _table: True
    store._column_exists = lambda _table, _column: False
    monkeypatch.setattr(repository, "_default_account_id", lambda: account_id)
    cursor = store.conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    workflow_id = "workflow' OR true --"

    assert store.list_workflow_reactions(workflow_id, enabled_only=enabled_only) == []

    statement, params = cursor.execute.call_args.args
    rendered = statement if isinstance(statement, str) else statement.as_string()
    normalized = " ".join(rendered.split())
    expected_literal = "'" + account_id.replace("'", "''") + "'"
    projection = normalized.split("SELECT id, ", 1)[1].split(", workflow_id", 1)[0]
    assert projection == expected_literal + "::text AS account_id"
    assert params == (workflow_id,)
    assert workflow_id not in rendered
    assert rendered.count("%s") == 1
    assert "WHERE workflow_id = %s" in normalized
    assert ("AND enabled = true" in normalized) is enabled_only
    assert normalized.endswith("ORDER BY enabled DESC, event_type, reaction_key")
