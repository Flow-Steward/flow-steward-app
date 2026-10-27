from __future__ import annotations

from core.application.extensions.runtime_store_db import DBExtensionRuntimeStore


class _Cursor:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self._calls = calls
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None) -> None:
        self._calls.append((str(query), params))

    def fetchall(self) -> list:
        return []


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.calls)


def test_active_projection_lookup_requires_active_install_state() -> None:
    connection = _Connection()
    store = DBExtensionRuntimeStore(connection)

    assert (
        store.list_projections(
            account_id="00000000-0000-0000-0000-000000000002",
            artifact_kind="workflow",
            only_active=True,
        )
        == []
    )

    query, params = connection.calls[-1]
    assert "extension_installs" in query
    assert "i.state = 'active'" in query
    assert "i.install_id = p.install_id" in query
    assert params == ("00000000-0000-0000-0000-000000000002", "workflow")


def test_all_projection_lookup_retains_disabled_install_history() -> None:
    connection = _Connection()
    store = DBExtensionRuntimeStore(connection)

    assert (
        store.list_projections(
            account_id="00000000-0000-0000-0000-000000000002",
            only_active=False,
        )
        == []
    )

    query, params = connection.calls[-1]
    assert "extension_installs" not in query
    assert "p.is_active = true" not in query
    assert params == ("00000000-0000-0000-0000-000000000002",)
