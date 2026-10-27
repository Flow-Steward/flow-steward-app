from __future__ import annotations

from contextlib import contextmanager


def test_required_polling_secrets_never_fall_back_to_global_scope():
    from core.infrastructure.runtime.worker import worker_inbound_polling as polling

    calls = []

    class _Store:
        def get_provider_secret(self, **kwargs):
            calls.append(kwargs)
            return "global-token" if kwargs["include_global"] else ""

    assert (
        polling._required_provider_secrets_available(
            _Store(),
            extension_id="acme.notification-provider",
            project_id="project-b",
            secret_keys=["api_token"],
        )
        is False
    )
    assert calls[0]["include_global"] is False


def test_inbound_poll_store_owns_catalog_connection_and_event_persistence_contract():
    from core.infrastructure.db import platform_stores

    store_type = getattr(platform_stores, "PlatformInboundPollingStore", None)

    assert store_type is not None
    assert {
        name
        for name in (
            "get_extension",
            "get_projects",
            "list_project_connections",
            "get_job",
            "record_domain_event",
            "enqueue_domain_event",
        )
        if not callable(getattr(store_type, name, None))
    } == set()


def test_inbound_poll_store_dedicated_connection_is_non_pooled_and_closed(monkeypatch):
    from core.infrastructure.db import platform_stores, psycopg_runtime

    class _Connection:
        closed = False

        def close(self):
            self.closed = True

    connection = _Connection()
    connect_calls: list[tuple[object, bool]] = []
    monkeypatch.setattr(
        psycopg_runtime,
        "connect",
        lambda dsn, autocommit: connect_calls.append((dsn, autocommit)) or connection,
    )
    monkeypatch.setattr(platform_stores, "resolve_observability_dsn", lambda dsn: str(dsn))

    with platform_stores.PlatformInboundPollingStore.dedicated("postgresql://poll") as store:
        assert store.conn is connection
        assert store._pool is None
        assert connection.closed is False

    assert connect_calls == [("postgresql://poll", True)]
    assert connection.closed is True


def test_poll_supervisor_rejects_missing_resolved_install_and_continues(monkeypatch):
    from core.infrastructure.config import runtime_config
    from core.infrastructure.db import platform_stores
    from core.infrastructure.extension_runtime import extension_runtime, extension_runtime_helpers
    from core.infrastructure.runtime.worker import worker_inbound_polling as polling

    processed: list[str] = []
    resolved: list[tuple[str, str | None]] = []
    invoked: list[str] = []
    closed: list[bool] = []

    class _Store:
        def __init__(self, _dsn: str):
            raise AssertionError("polling must not use a shared/general store connection")

        @classmethod
        @contextmanager
        def dedicated(cls, dsn: str):
            assert dsn == "dsn"
            instance = cls.__new__(cls)
            try:
                yield instance
            finally:
                closed.append(True)

        def get_projects(self):
            return [{"id": "project-1"}]

        def list_project_connections(self, _project_id: str, *, enabled_only: bool):
            assert enabled_only is True
            return [
                {
                    "connection_id": "connection-fails",
                    "account_id": "account-fails",
                    "project_id": "project-1",
                    "extension_id": "ext-fails",
                    "extension_install_id": "pi_install-fails",
                    "connection_kind": "extension",
                    "ingress_config": {"inbound_mode": "polling"},
                },
                {
                    "connection_id": "connection-works",
                    "account_id": "account-works",
                    "project_id": "project-1",
                    "extension_id": "ext-works",
                    "extension_install_id": "pi_install-works",
                    "connection_kind": "extension",
                    "ingress_config": {"inbound_mode": "polling"},
                },
            ]

    class _Event:
        calls = 0

        def wait(self, _seconds: float) -> bool:
            self.calls += 1
            return self.calls > 1

        def set(self) -> None:
            return None

    class _InlineThread:
        def __init__(self, *, target, **_kwargs):
            self._target = target

        def start(self) -> None:
            self._target()

    def _resolve(install_id: str, *, account_id=None, **_kwargs):
        resolved.append((install_id, account_id))
        extension_id = {
            "pi_install-fails": "ext-fails",
            "pi_install-works": "ext-works",
        }[install_id]
        extension = {
            "extension_key": extension_id,
            "extension_install_id": install_id if extension_id == "ext-works" else "",
            "action_definitions": [
                {
                    "action_id": "poll_now",
                    "handler": {"config": {"runtime_resources": {}}},
                    "contract": {
                        "serialization": {"resource": "inbound_updates"},
                        "background": {
                            "kind": "connection_poll",
                            "interval_field": "polling_interval_seconds",
                            "mode_field": "inbound_mode",
                            "transport_policy": "webhook_preferred",
                        },
                    },
                }
            ],
        }
        return extension, None

    def _run(extension, **_kwargs):
        invoked.append(extension["extension_key"])
        return {"status": "succeeded", "response": {"result": {"processed_updates": 1}}}

    monkeypatch.setattr(polling, "_runtime_env", {"FS_EXTENSION_INBOUND_POLL_ENABLED": "1"})
    monkeypatch.setattr(polling.threading, "Event", _Event)
    monkeypatch.setattr(polling.threading, "Thread", _InlineThread)
    monkeypatch.setattr(runtime_config, "CONFIG", {"fs_db_dsn": "postgresql://test"})
    monkeypatch.setattr(platform_stores, "resolve_observability_dsn", lambda _dsn: "dsn")
    monkeypatch.setattr(platform_stores, "PlatformInboundPollingStore", _Store)
    monkeypatch.setattr(
        platform_stores,
        "PlatformIntegrationStore",
        lambda _dsn: (_ for _ in ()).throw(AssertionError("wrong persistence surface")),
    )
    monkeypatch.setattr(
        extension_runtime_helpers, "resolve_extension_runtime_record_by_install_id", _resolve
    )
    monkeypatch.setattr(extension_runtime, "run_extension_action", _run)

    handle = polling._start_extension_inbound_poll_loop(
        worker_id="worker-test",
        project_id=None,
        process_extension_response_envelope=lambda _result, extension, *_a, **_k: processed.append(
            extension["extension_key"]
        ),
    )

    assert handle is not None
    assert resolved == [
        ("pi_install-fails", "account-fails"),
        ("pi_install-works", "account-works"),
    ]
    assert invoked == ["ext-works"]
    assert processed == ["ext-works"]
    assert closed == [True]


def test_poll_supervisor_contains_response_processing_failure_and_closes_store(monkeypatch):
    from core.infrastructure.config import runtime_config
    from core.infrastructure.db import platform_stores
    from core.infrastructure.extension_runtime import extension_runtime, extension_runtime_helpers
    from core.infrastructure.runtime.worker import worker_inbound_polling as polling

    closed: list[bool] = []
    invocations: list[dict[str, object]] = []
    processed: list[str] = []
    processed_audits: list[dict[str, object]] = []

    class _Store:
        @classmethod
        @contextmanager
        def dedicated(cls, _dsn: str):
            try:
                yield cls()
            finally:
                closed.append(True)

        def get_projects(self):
            return [{"id": "project-1"}]

        def list_project_connections(self, _project_id: str, *, enabled_only: bool):
            assert enabled_only is True
            return [
                {
                    "connection_id": f"connection-{suffix}",
                    "account_id": f"account-{suffix}",
                    "project_id": "project-1",
                    "extension_id": f"ext-{suffix}",
                    "extension_install_id": f"pi_install-{suffix}",
                    "connection_kind": "extension",
                    "ingress_config": {"inbound_mode": "polling"},
                }
                for suffix in ("fails", "works")
            ]

    class _Event:
        calls = 0

        def wait(self, _seconds: float) -> bool:
            self.calls += 1
            return self.calls > 1

        def set(self) -> None:
            return None

    class _InlineThread:
        def __init__(self, *, target, **_kwargs):
            self._target = target

        def start(self) -> None:
            self._target()

    def _resolve(install_id: str, *, account_id: str, **_kwargs):
        suffix = install_id.removeprefix("pi_install-")
        assert account_id == f"account-{suffix}"
        return (
            {
                "extension_key": f"ext-{suffix}",
                "extension_install_id": install_id,
                "action_definitions": [
                    {
                        "action_id": "poll_now",
                        "handler": {"config": {"runtime_resources": {}}},
                        "contract": {
                            "background": {
                                "kind": "connection_poll",
                                "interval_field": "polling_interval_seconds",
                                "mode_field": "inbound_mode",
                                "transport_policy": "webhook_preferred",
                            }
                        },
                    }
                ],
            },
            None,
        )

    def _process(_result, extension, *_args, **kwargs):
        extension_id = extension["extension_key"]
        processed.append(extension_id)
        processed_audits.append(kwargs["audit"])
        if extension_id == "ext-fails":
            raise RuntimeError("response persistence failed")

    def _run(_extension, **kwargs):
        invocations.append(kwargs["runtime_context"])
        return {
            "status": "succeeded",
            "response": {"result": {"processed_updates": 1}},
        }

    monkeypatch.setattr(polling, "_runtime_env", {"FS_EXTENSION_INBOUND_POLL_ENABLED": "1"})
    monkeypatch.setattr(polling.threading, "Event", _Event)
    monkeypatch.setattr(polling.threading, "Thread", _InlineThread)
    monkeypatch.setattr(runtime_config, "CONFIG", {"fs_db_dsn": "postgresql://test"})
    monkeypatch.setattr(platform_stores, "resolve_observability_dsn", lambda _dsn: "dsn")
    monkeypatch.setattr(platform_stores, "PlatformInboundPollingStore", _Store)
    monkeypatch.setattr(
        extension_runtime_helpers, "resolve_extension_runtime_record_by_install_id", _resolve
    )
    monkeypatch.setattr(extension_runtime, "run_extension_action", _run)

    polling._start_extension_inbound_poll_loop(
        worker_id="worker-test",
        project_id=None,
        process_extension_response_envelope=_process,
    )

    assert processed == ["ext-fails", "ext-works"]
    assert [(row["account_id"], row["extension_install_id"]) for row in invocations] == [
        ("account-fails", "pi_install-fails"),
        ("account-works", "pi_install-works"),
    ]
    assert [(row["account_id"], row["extension_install_id"]) for row in processed_audits] == [
        ("account-fails", "pi_install-fails"),
        ("account-works", "pi_install-works"),
    ]
    assert closed == [True]
