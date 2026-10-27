from __future__ import annotations

import pytest

from core.application.search.search_postgres_catalog import _catalog_query


@pytest.mark.parametrize("group", ["agents", "projects", "datasets"])
def test_catalog_sql_starts_with_select_not_suppression_comment(group: str) -> None:
    sql, _params, _route = _catalog_query(
        group=group,
        query="demo",
        account_id="00000000-0000-0000-0000-000000000002",
        tenant_id=None,
        workspace_id=None,
        project_ids=None,
    )

    assert sql.lstrip().startswith("SELECT")
    assert not sql.lstrip().startswith("#")
