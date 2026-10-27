from __future__ import annotations

import builtins

from core.infrastructure.db import model_provider_store


class _RecordingCursor:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self._calls = calls
        self._fetchone_results: list[object] = []
        self._fetchall_results: list[list[object]] = []
        self.rowcount = 1
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self._calls.append((str(sql), params))

    def fetchone(self):
        if self._fetchone_results:
            return self._fetchone_results.pop(0)
        return None

    def fetchall(self):
        if self._fetchall_results:
            return self._fetchall_results.pop(0)
        return []


class _RecordingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fetchone_results: list[object] = []
        self.fetchall_results: list[list[object]] = []
        self.last_cursor_kwargs: dict | None = None
        self.description = []

    def cursor(self, *args, **kwargs):
        self.last_cursor_kwargs = dict(kwargs)
        cursor = _RecordingCursor(self.calls)
        cursor._fetchone_results = self.fetchone_results
        cursor._fetchall_results = self.fetchall_results
        cursor.description = self.description
        return cursor


def test_list_enabled_model_providers_falls_back_without_psycopg_runtime_extras(monkeypatch):
    conn = _RecordingConn()
    conn.fetchall_results = [
        [
            {
                "id": 1,
                "name": "demo",
                "provider_type": "openai_compatible",
                "api_key_ref": "secret_ref",
                "enabled": True,
            }
        ]
    ]

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "psycopg_runtime.extras":
            raise ImportError("psycopg_runtime.extras unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    rows = model_provider_store.list_enabled_model_providers(conn, account_id="acc_default")

    assert conn.last_cursor_kwargs == {}
    assert rows == [
        {
            "id": "1",
            "name": "demo",
            "provider_type": "openai_compatible",
            "enabled": True,
        }
    ]


def test_list_enabled_model_providers_maps_tuple_rows_without_realdictcursor(monkeypatch):
    conn = _RecordingConn()
    conn.description = [("id",), ("name",), ("provider_type",), ("api_key_ref",), ("enabled",)]
    conn.fetchall_results = [[(7, "byok", "openai_compatible", "secret_ref", True)]]

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "psycopg_runtime.extras":
            raise ImportError("psycopg_runtime.extras unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    rows = model_provider_store.list_enabled_model_providers(conn, account_id="acc_default")

    assert rows == [
        {
            "id": "7",
            "name": "byok",
            "provider_type": "openai_compatible",
            "enabled": True,
        }
    ]


def test_list_enabled_model_providers_uses_database_clock_for_routable_rows():
    conn = _RecordingConn()

    model_provider_store.list_enabled_model_providers(
        conn,
        account_id="acc_default",
        routable_only=True,
    )

    sql = conn.calls[0][0]
    assert "verification_status = 'passed'" in sql
    assert "lifecycle = 'active'" in sql
    assert "verified_at >= now() - interval '30 days'" in sql
    assert "verified_at <= now()" in sql


def test_get_model_provider_returns_dict_from_tuple_row(monkeypatch):
    conn = _RecordingConn()
    conn.description = [("id",), ("api_key_ref",)]
    conn.fetchone_results = [(9, "provider_demo_key")]

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "psycopg_runtime.extras":
            raise ImportError("psycopg_runtime.extras unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    payload = model_provider_store.get_model_provider(conn, provider_id=9)

    assert payload == {"id": "9", "account_id": "", "api_key_ref": "provider_demo_key"}


def test_get_model_provider_adds_account_filter():
    conn = _RecordingConn()

    model_provider_store.get_model_provider(conn, provider_id=9, account_id="acc_alpha")

    sql, params = conn.calls[0]
    assert "AND account_id = %s" in sql
    assert params == ("9", "acc_alpha")


def test_lock_model_provider_for_update_is_account_scoped():
    conn = _RecordingConn()
    conn.fetchone_results = [
        {"id": 9, "account_id": "acc_alpha", "api_key_ref": "provider_old_key"}
    ]

    payload = model_provider_store.lock_model_provider_for_update(
        conn, provider_id=9, account_id="acc_alpha"
    )

    sql, params = conn.calls[0]
    assert "FOR UPDATE" in sql
    assert "account_id = %s" in sql
    assert params == ("9", "acc_alpha")
    assert payload == {
        "id": "9",
        "account_id": "acc_alpha",
        "api_key_ref": "provider_old_key",
    }


def test_display_only_provider_update_preserves_verification_columns():
    conn = _RecordingConn()
    conn.fetchone_results = [{"id": 9, "account_id": "acc_alpha"}]

    model_provider_store.update_model_provider(
        conn,
        provider_id=9,
        account_id="acc_alpha",
        name="Renamed",
        provider_type="openai_compatible",
        base_url="https://example.test/v1",
        api_key_ref=None,
        api_version=None,
        deployment_name=None,
        default_model="model-a",
        config={},
        invalidate_verification=False,
    )

    sql, params = conn.calls[0]
    assert "verification_status = CASE WHEN %s" in sql
    assert params[-8:-2] == (False, False, False, False, False, False)
    assert sql.count("%s") == len(params)


def test_verification_claim_is_bound_to_provider_configuration_and_secret():
    conn = _RecordingConn()
    conn.fetchone_results = [{"verification_generation": "gen-1"}]

    assert model_provider_store.claim_model_provider_verification(
        conn,
        account_id="acc_alpha",
        provider_id="9",
        generation="gen-1",
        expected_provider={
            "provider_type": "openai_compatible",
            "base_url": "https://example.test/v1",
            "api_key_ref": "secret-ref",
            "api_version": None,
            "deployment_name": None,
            "default_model": "model-a",
            "config_jsonb": {"header": "value"},
        },
    )

    sql, params = conn.calls[0]
    assert "config_jsonb IS NOT DISTINCT FROM %s::jsonb" in sql
    assert "SELECT encrypted_value FROM secret_refs" in sql
    assert params[-11:-3] == (
        "openai_compatible",
        "https://example.test/v1",
        "secret-ref",
        None,
        None,
        "model-a",
        '{"header": "value"}',
        None,
    )
    assert params[-3:] == ("acc_alpha", "secret-ref", None)


def test_claim_model_provider_verification_is_generation_bound():
    conn = _RecordingConn()
    conn.fetchone_results = [{"verification_generation": "gen-1"}]

    claimed = model_provider_store.claim_model_provider_verification(
        conn,
        account_id="acc_alpha",
        provider_id="9",
        generation="gen-1",
        expected_provider={
            "provider_type": "openai_compatible",
            "base_url": "https://example.test/v1",
            "api_key_ref": None,
            "api_version": None,
            "deployment_name": None,
            "default_model": "model-a",
            "config_jsonb": {},
            "upstream_revision": "revision-a",
        },
    )

    sql, params = conn.calls[0]
    assert "verification_status = 'pending'" in sql
    assert "verification_started_at < now() - interval '10 minutes'" in sql
    assert "upstream_revision IS NOT DISTINCT FROM %s" in sql
    assert params[-1] == "revision-a"
    assert claimed == {"verification_generation": "gen-1"}


def test_verification_publication_requires_owned_pending_generation():
    conn = _RecordingConn()

    assert model_provider_store.set_model_provider_verification(
        conn,
        account_id="acc_alpha",
        provider_id="9",
        generation="gen-1",
        status="passed",
        verified_capabilities=["tools", "chat"],
    )

    sql, params = conn.calls[0]
    assert "verification_status = 'pending'" in sql
    assert "verification_generation = %s" in sql
    assert params[-1] == "gen-1"


def test_delete_model_provider_adds_account_filter():
    conn = _RecordingConn()

    model_provider_store.delete_model_provider(conn, provider_id=9, account_id="acc_alpha")

    sql, params = conn.calls[0]
    assert "AND account_id = %s" in sql
    assert params == ("9", "acc_alpha")
