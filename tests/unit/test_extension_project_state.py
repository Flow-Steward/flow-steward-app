from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID


def test_project_state_schema_enforces_non_null_validated_install_owner():
    schema_dir = Path(__file__).resolve().parents[2] / "db/schema"
    table_sql = (schema_dir / "001_schema_part_04.sql").read_text(encoding="utf-8")
    start = table_sql.index("CREATE TABLE public.extension_project_state (")
    table = table_sql[start : table_sql.index(");", start)]
    fk_sql = (schema_dir / "001_schema_part_10.sql").read_text(encoding="utf-8")

    assert "extension_install_id text NOT NULL" in table
    assert (
        "ADD CONSTRAINT extension_project_state_install_id_fkey FOREIGN KEY "
        "(extension_install_id) REFERENCES public.extension_installs(install_id)"
    ) in fk_sql


class _ProjectOwnerDb:
    def __init__(self, account_id: str | None) -> None:
        self.account_id = account_id

    def get_project(self, project_id):
        if self.account_id is None:
            return None
        return {"id": str(project_id), "account_id": self.account_id}


def test_extension_project_state_rejects_secretish_identity():
    from core.application.extensions.extension_project_state import (
        ExtensionProjectStateError,
        put_state,
    )

    _raises_input_37_1 = object()
    with pytest.raises(ExtensionProjectStateError):
        put_state(
            _raises_input_37_1,
            account_id="acc_a",
            project_id="project_1",
            extension_id="example.host-fixture",
            extension_install_id="install_fixture",
            namespace="secrets",
            state_key="api_token",
            value_jsonb={"value": "Hello"},
        )


def test_extension_project_state_rejects_secretish_value():
    from core.application.extensions.extension_project_state import (
        ExtensionProjectStateError,
        put_state,
    )

    _raises_input_56_1 = object()
    with pytest.raises(ExtensionProjectStateError):
        put_state(
            _raises_input_56_1,
            account_id="acc_a",
            project_id="project_1",
            extension_id="example.host-fixture",
            extension_install_id="install_fixture",
            namespace="settings",
            state_key="setting:primary",
            value_jsonb={"password": "do-not-store"},
        )


def test_extension_project_state_requires_install_identity():
    from core.application.extensions.extension_project_state import (
        ExtensionProjectStateError,
        put_state,
    )

    _raises_input_75_1 = object()
    with pytest.raises(ExtensionProjectStateError, match="extension_install_id"):
        put_state(
            _raises_input_75_1,
            account_id="acc_a",
            project_id="project_1",
            extension_id="example.host-fixture",
            namespace="settings",
            state_key="setting:primary",
            value_jsonb={"value": "Hello"},
        )


def test_extension_project_state_enforces_value_size_limit(monkeypatch):
    from core.application.extensions.extension_project_state import (
        ExtensionProjectStateError,
        put_state,
    )

    monkeypatch.setenv("FS_EXTENSION_PROJECT_STATE_MAX_BYTES", "1024")

    _raises_input_95_1 = object()
    with pytest.raises(ExtensionProjectStateError):
        put_state(
            _raises_input_95_1,
            account_id="acc_a",
            project_id="project_1",
            extension_id="example.host-fixture",
            extension_install_id="install_fixture",
            namespace="settings",
            state_key="setting:large",
            value_jsonb={"value": "x" * 2000},
        )


def test_extension_project_state_revision_conflict(monkeypatch):
    from core.application.extensions import extension_project_state

    monkeypatch.setattr(
        extension_project_state.extension_project_state_store,
        "put_extension_project_state",
        lambda *args, **kwargs: None,
    )

    _raises_input_117_1 = object()
    with pytest.raises(extension_project_state.ExtensionProjectStateRevisionConflict):
        extension_project_state.put_state(
            _raises_input_117_1,
            account_id="acc_a",
            project_id="project_1",
            extension_id="example.host-fixture",
            extension_install_id="install_fixture",
            namespace="settings",
            state_key="setting:primary",
            value_jsonb={"value": "Hello"},
            expected_revision=3,
        )


def test_extension_project_state_rejects_account_project_mismatch():
    from core.application.extensions.extension_project_state import (
        ExtensionProjectStateError,
        put_state,
    )

    _raises_input_137_1 = _ProjectOwnerDb("acc_owner")
    with pytest.raises(ExtensionProjectStateError, match="does not belong"):
        put_state(
            _raises_input_137_1,
            account_id="acc_other",
            project_id="project_1",
            extension_id="example.host-fixture",
            extension_install_id="install_fixture",
            namespace="settings",
            state_key="setting:primary",
            value_jsonb={"value": "Hello"},
        )


def test_extension_project_state_store_rejects_account_project_mismatch_before_write(
    monkeypatch,
):
    from core.infrastructure.db import extension_project_state_store

    def _unexpected_write(*args, **kwargs):
        raise AssertionError("mismatched account/project must fail before store write")

    monkeypatch.setattr(
        extension_project_state_store,
        "_update_extension_project_state",
        _unexpected_write,
    )
    monkeypatch.setattr(
        extension_project_state_store,
        "insert_extension_project_state",
        _unexpected_write,
    )

    _raises_input_169_1 = _ProjectOwnerDb("acc_owner")
    with pytest.raises(
        extension_project_state_store.ExtensionProjectStateOwnershipError,
        match="does not belong",
    ):
        extension_project_state_store.put_extension_project_state(
            _raises_input_169_1,
            account_id="acc_other",
            project_id="project_1",
            extension_id="example.host-fixture",
            namespace="settings",
            state_key="setting:primary",
            value_jsonb={"value": "Hello"},
        )


def test_extension_project_state_store_expected_revision_missing_row_does_not_insert(
    monkeypatch,
):
    from core.infrastructure.db import extension_project_state_store

    monkeypatch.setattr(
        extension_project_state_store,
        "_update_extension_project_state",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        extension_project_state_store,
        "get_extension_project_state",
        lambda *args, **kwargs: None,
    )

    def _unexpected_insert(*args, **kwargs):
        raise AssertionError("expected_revision replay must not recreate missing state")

    monkeypatch.setattr(
        extension_project_state_store,
        "insert_extension_project_state",
        _unexpected_insert,
    )

    row = extension_project_state_store.put_extension_project_state(
        object(),
        account_id="acc_a",
        project_id="project_1",
        extension_id="example.host-fixture",
        extension_install_id="install_fixture",
        namespace="settings",
        state_key="setting:primary",
        value_jsonb={"value": "Hello"},
        expected_revision=3,
    )

    assert row is None


def test_extension_project_state_put_passes_nullable_connection_id(monkeypatch):
    from core.application.extensions import extension_project_state

    captured: dict = {}

    def _put(*args, **kwargs):
        captured.update(kwargs)
        return {"state_id": "state_1", "revision": 1, **kwargs}

    monkeypatch.setattr(
        extension_project_state.extension_project_state_store,
        "put_extension_project_state",
        _put,
    )

    row = extension_project_state.put_state(
        object(),
        account_id="acc_a",
        project_id="project_1",
        extension_id="example.host-fixture",
        extension_install_id="install_fixture",
        namespace="settings",
        state_key="setting:primary",
        value_jsonb={"value": "Hello"},
        connection_id="",
    )

    assert captured["connection_id"] is None
    assert row["extension_id"] == "example.host-fixture"


def test_extension_project_state_list_defaults_to_project_wide_rows(monkeypatch):
    from core.application.extensions import extension_project_state

    captured: dict = {}

    def _list(*args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        extension_project_state.extension_project_state_store,
        "list_extension_project_state",
        _list,
    )

    extension_project_state.list_state(
        object(),
        account_id="acc_a",
        project_id="project_1",
        extension_id="example.host-fixture",
        connection_id="",
    )

    assert captured["connection_id"] is None
    assert captured["include_all_connections"] is False


def test_extension_project_state_create_uses_atomic_upsert():
    from core.infrastructure.db import extension_project_state_store

    class _Cursor:
        def __init__(self) -> None:
            self.description = [("state_id",), ("revision",)]
            self.sql = ""
            self.params = ()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql, params=()):
            self.sql = sql
            self.params = params

        def fetchone(self):
            return {"state_id": "state_1", "revision": 1}

    class _Db:
        def __init__(self) -> None:
            self.cursor_instance = _Cursor()

        def get_project(self, project_id):
            return {"id": str(project_id), "account_id": "acc_a"}

        def cursor(self):
            return self.cursor_instance

    db = _Db()
    row = extension_project_state_store.put_extension_project_state(
        db,
        account_id="acc_a",
        project_id="project_1",
        extension_id="example.host-fixture",
        extension_install_id="install_fixture",
        namespace="settings",
        state_key="setting:primary",
        value_jsonb={"value": "Hello"},
    )

    assert row["state_id"] == "state_1"
    assert "ON CONFLICT (" in db.cursor_instance.sql
    assert "COALESCE(connection_id, ''::text)" in db.cursor_instance.sql
    assert "revision = extension_project_state.revision + 1" in db.cursor_instance.sql


def test_extension_project_state_write_persists_exact_install_owner():
    from core.infrastructure.db import extension_project_state_store

    class _Cursor:
        def __init__(self):
            self.description = [("state_id",), ("revision",)]
            self.sql = ""
            self.params = ()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql, params=()):
            self.sql = sql
            self.params = params

        def fetchone(self):
            return {"state_id": "state_exact", "revision": 1}

    class _Db:
        def __init__(self):
            self.cursor_instance = _Cursor()

        def get_project(self, project_id):
            return {"id": str(project_id), "account_id": "acc_1"}

        def cursor(self):
            return self.cursor_instance

    db = _Db()
    extension_project_state_store.put_extension_project_state(
        db,
        account_id="acc_1",
        project_id="project_1",
        extension_id="acme.provider",
        extension_install_id="install_exact",
        namespace="sync",
        state_key="cursor",
        value_jsonb={"cursor": 1},
    )

    assert "extension_install_id" in db.cursor_instance.sql
    assert "COALESCE(extension_install_id, ''::text)" in db.cursor_instance.sql
    assert "install_exact" in db.cursor_instance.params
    assert "SELECT" not in db.cursor_instance.sql.upper()


def test_extension_project_state_postgres_upsert_create_and_update(pg_conn, test_project):
    from core.infrastructure.db import extension_project_state_store

    state_key = f"setting:{uuid.uuid4().hex}"
    extension_id = f"example.host-fixture-{uuid.uuid4().hex}"
    install_id = f"install-state-{uuid.uuid4().hex}"
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extension_installs (extension_id, account_id, install_id)
            VALUES (%s, %s, %s)
            """,
            (extension_id, DEFAULT_ACCOUNT_ID, install_id),
        )

    created = extension_project_state_store.put_extension_project_state(
        pg_conn,
        account_id=DEFAULT_ACCOUNT_ID,
        project_id=test_project,
        extension_id=extension_id,
        extension_install_id=install_id,
        namespace="settings",
        state_key=state_key,
        value_jsonb={"value": "Hello"},
    )
    updated = extension_project_state_store.put_extension_project_state(
        pg_conn,
        account_id=DEFAULT_ACCOUNT_ID,
        project_id=test_project,
        extension_id=extension_id,
        extension_install_id=install_id,
        namespace="settings",
        state_key=state_key,
        value_jsonb={"value": "Updated"},
    )

    assert str(updated["state_id"]) == str(created["state_id"])
    assert int(updated["revision"]) == int(created["revision"]) + 1
    assert updated["value_jsonb"] == {"value": "Updated"}
