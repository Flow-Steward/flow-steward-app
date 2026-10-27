"""Every store handed to a consumer must implement what that consumer calls.

This epic found the same defect six times, in six unrelated places: a store is
injected that does not implement a method its consumer calls, a
``getattr``/``hasattr`` guard swallows the AttributeError, and the code answers
with a degraded value instead of working. The variants differ only in how loudly
they fail:

* fails open — the connector catalog returned an empty connection list, so no
  connector step could be bound to a connection on any deployment, and the
  authoring surface looked correct (RS5-038);
* fails closed — POST /api/extensions/{id}/invoke answered 503 "Extension
  discovery is not available" on every deployment, because the tool execution
  service is wired with the job store;
* fails silently in the data plane — workspace deletion orphaned live rows
  because ``getattr(store, method, None)`` returned None and the caller returned
  an empty list.

Blanket-banning the pattern is not workable: the codebase has roughly 940
``getattr(store, ..., None)`` / ``hasattr(store, ...)`` sites, and most are
legitimate duck-typing over genuinely optional capabilities. What is checkable
precisely, without heuristics, is the thing that actually broke each time: does
the concrete class wired at this seam provide the methods the consumer calls?

So this table is deliberately explicit rather than inferred. Add a row when you
wire a store into a consumer, and list the methods that consumer calls on the
happy path. A missing method fails here, at import speed, instead of becoming a
503 or an empty list on a deployed candidate.

The MagicMock trap is why unit tests missed these: a bare ``MagicMock()``
auto-creates every attribute, so a test using one cannot represent a store that
lacks a method. These assertions run against the real classes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.infrastructure.db import platform_stores

# (seam, store class name, methods the consumer calls)
#
# "seam" names where the wiring lives, so a failure points at the line to fix.
WIRING_CONTRACTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        # RC7-001: the security-posture scan called bl._get_connection() behind a
        # hasattr guard. PlatformConnectionStore has no such method, so the guard
        # was False on every iteration and the scan returned an empty success -
        # the loop ran for the life of the process and wrote nothing. This seam
        # was not in the table, which is why the check did not catch it.
        "control_process: security_posture_scan -> project store",
        "PlatformProjectStore",
        (
            "list_active_project_ids",
            "record_security_posture_loop_attempt",
            "record_security_posture_loop_success",
            "record_security_posture_loop_failure",
            "get_security_posture_loop_status",
        ),
    ),
    (
        "app_factory: tool_execution_service.get_extension_store -> integration_store",
        "PlatformIntegrationStore",
        ("get_extension",),
    ),
    (
        "workflows_common._platform_logger_for_admin_catalog -> integration_store",
        "PlatformIntegrationStore",
        ("list_project_connections", "get_extension", "list_extensions"),
    ),
    (
        "workflows_routes_history.get_workflow_stats -> job_store",
        "PlatformJobStore",
        ("get_workflow_stats",),
    ),
    (
        "integrations_connectors: project connection routes -> integration_store",
        "PlatformIntegrationStore",
        ("get_project", "list_project_connections"),
    ),
    (
        "workflow entrypoint creation -> project_store",
        "PlatformProjectStore",
        ("get_project", "get_project_run_config", "save_project_secret"),
    ),
    (
        "project integration routes -> integration_store",
        "PlatformIntegrationStore",
        (
            "provider_configuration_transaction",
            "get_provider",
            "lock_provider_for_update",
            "save_provider",
            "get_provider_secret",
            "save_provider_secret",
            "log_action",
        ),
    ),
    (
        "readiness_runtime.worker_identity -> identity store surface",
        "PlatformIdentityStore",
        ("list_extensions",),
    ),
    (
        "extension lifecycle (install, uninstall, OAuth uninstall audit) -> identity_store",
        "PlatformIdentityStore",
        (
            "get_extension",
            "get_extension_install_for_operator",
            "log_command_execution",
        ),
    ),
    (
        "jobs routes -> job_store",
        "PlatformJobStore",
        ("get_job", "get_active_jobs"),
    ),
    (
        "job trace projection -> observability_store",
        "PlatformObservabilityStore",
        (
            "build_job_trace",
            "get_logs",
            "get_events",
            "get_agent_messages",
            "list_extension_invocations",
            "list_domain_events",
            "list_event_dispatches",
            "list_job_errors",
        ),
    ),
    (
        "workflow and job reactions -> reaction_store",
        "PlatformReactionStore",
        (
            "list_workflow_reactions",
            "save_workflow_reactions",
            "list_job_reaction_events",
            "log_job_reaction_event",
            "get_project",
        ),
    ),
    (
        "provider webhook authorization -> provider_webhook_store",
        "PlatformProviderWebhookStore",
        ("get_project", "get_provider", "get_project_connection"),
    ),
    (
        "command dispatch audit and job lifecycle -> job_store",
        "PlatformJobStore",
        (
            "get_job",
            "get_project",
            "update_job",
            "log_command_execution",
            "update_command_execution",
        ),
    ),
    (
        "command dispatch extension actions -> integration_store",
        "PlatformIntegrationStore",
        ("get_extension", "get_project"),
    ),
    (
        "project routes -> project_store",
        "PlatformProjectStore",
        ("get_project",),
    ),
    (
        "trash lifecycle project dependency projection -> project_store",
        "PlatformProjectStore",
        ("get_project", "get_project_workflows", "list_project_connections"),
    ),
)


# Stores that cannot be constructed cheaply, with the reason. A seam whose
# store lands here is checked against the class instead, which is weaker - so
# the list is asserted below and must not grow silently. "Could not check" must
# never read as "checked".
UNINSTANTIABLE_STORES: dict[str, str] = {}


def test_workflow_entrypoint_routes_use_the_project_store_contract() -> None:
    source = Path("core/entrypoints/api/route_registration.py").read_text(encoding="utf-8")
    wiring = source.split("_register_workflow_entrypoint_routes(", maxsplit=1)[1].split(
        "current_app.include_router", maxsplit=1
    )[0]

    assert '_composed_service(current_app, "project_store")' in wiring
    assert '_composed_service(current_app, "connection_store")' not in wiring


class _FakeCursor:
    """Answers the schema probes a store runs while constructing itself."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, *_args, **_kwargs) -> None:
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakeConnection:
    """Enough of a connection to construct a store.

    Construction legitimately probes the schema - _table_exists and the storage
    backfills both query - so the fake answers harmlessly instead of refusing.
    What this file checks is what the constructed object provides, not whether
    building it touches SQL.
    """

    autocommit = False

    def cursor(self, *_args, **_kwargs):
        return _FakeCursor()

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _construct(store_class, monkeypatch):
    """Build a real instance with the connection factory stubbed out.

    Checking the class was the flaw this test had: `conn` on
    PlatformConnectionStore is assigned in __init__, so a class-level hasattr
    answered False for an attribute every instance has - and would equally have
    answered True for nothing at all. The question the seam actually asks is
    what the object the consumer receives provides, so build that object.
    """
    # __init__ lives on the core mixin, so that is where connect and
    # pooling_enabled resolve from - patching the re-exporting module would have
    # looked right and changed nothing.
    from core.infrastructure.db import platform_logger_core_mixin as core_mixin

    monkeypatch.setattr(core_mixin, "pooling_enabled", lambda: False)
    monkeypatch.setattr(core_mixin.psycopg_runtime, "connect", lambda *_a, **_k: _FakeConnection())
    return store_class("dbname=contract user=contract host=localhost")


@pytest.mark.parametrize(
    ("seam", "store_class_name", "required_methods"),
    WIRING_CONTRACTS,
    ids=[row[0] for row in WIRING_CONTRACTS],
)
def test_wired_store_provides_what_its_consumer_uses(
    seam: str, store_class_name: str, required_methods: tuple[str, ...], monkeypatch
) -> None:
    store_class = getattr(platform_stores, store_class_name, None)
    assert store_class is not None, f"{seam}: unknown store class {store_class_name!r}"

    if store_class_name in UNINSTANTIABLE_STORES:
        target = store_class
        note = f" (class-level check only: {UNINSTANTIABLE_STORES[store_class_name]})"
    else:
        target = _construct(store_class, monkeypatch)
        note = ""

    # One mechanism for methods and attributes alike. Splitting them is how the
    # gap reappears at the next seam that reaches for something else.
    missing = [name for name in required_methods if not hasattr(target, name)]
    assert not missing, (
        f"{seam}: {store_class_name} does not provide {missing}{note}. "
        f"A guard will swallow this at runtime and answer with a degraded value "
        f"instead of failing, which is how this class of defect stays invisible."
    )


def test_no_seam_is_quietly_downgraded_to_a_class_check() -> None:
    """The exclusion list is evidence, not an escape hatch.

    A store that cannot be instantiated is checked more weakly, so the set of
    such stores has to stay visible and stay small. Growing it silently would
    put the very defect this file hunts inside the check itself.
    """
    assert UNINSTANTIABLE_STORES == {}, (
        "a store was excluded from instance checking; if that is genuinely "
        "necessary, say why here and keep the list under review"
    )


def test_control_composition_validates_concrete_loop_bindings(monkeypatch) -> None:
    from core.infrastructure.db import platform_logger_core_mixin as core_mixin
    from core.infrastructure.runtime.entrypoint_db_adapters import create_control_stores

    monkeypatch.setattr(core_mixin, "pooling_enabled", lambda: False)
    monkeypatch.setattr(
        core_mixin.psycopg_runtime,
        "connect",
        lambda *_args, **_kwargs: _FakeConnection(),
    )

    stores = create_control_stores("dbname=contract user=contract host=localhost")

    assert set(stores) == {
        "connection",
        "events",
        "observability",
        "projects",
        "scheduler",
        "worker",
    }


def test_the_known_wrong_pairings_are_still_wrong() -> None:
    """Pins the negative half, so the contracts above cannot pass vacuously.

    If PlatformJobStore ever grows get_extension, the tool-execution contract
    above stops proving anything about the wiring — it would pass either way.
    These assertions make that visible instead of silent.
    """
    assert not hasattr(platform_stores.PlatformJobStore, "get_extension"), (
        "the tool execution service is wired with the job store; the point of the "
        "separate extension-store accessor is that this store cannot resolve "
        "extensions"
    )
    assert not hasattr(platform_stores.PlatformIdentityStore, "list_project_connections"), (
        "the connector catalog fallback used to build the identity store; this is "
        "the method it could not answer"
    )


def test_every_contract_names_a_real_store_class() -> None:
    """The table must not rot into assertions about classes that no longer exist."""
    for seam, store_class_name, required_methods in WIRING_CONTRACTS:
        assert hasattr(platform_stores, store_class_name), f"{seam}: {store_class_name} is gone"
        assert required_methods, f"{seam}: contract lists no methods, so it asserts nothing"


def test_trash_dataset_projection_uses_the_concrete_registry_catalog() -> None:
    from core.application.registry.db_store import RegistryDBStore

    store = RegistryDBStore("dbname=contract user=contract host=localhost")

    assert callable(store.list_project_dataset_catalog)


def test_the_instance_check_sees_what_the_class_check_could_not(monkeypatch) -> None:
    """The regression for this file's own blind spot.

    RC7-001: the security-posture scan called bl._get_connection() behind a
    hasattr guard, got False on every iteration, and returned an empty success -
    the loop ran for the life of the process and wrote nothing. This file was
    supposed to catch exactly that and did not, for two reasons: the seam was
    not in the table, and the check asked the CLASS. `conn` is assigned in
    __init__, so a class-level hasattr answers False for an attribute every
    instance has - and would answer the same for one that exists nowhere.
    """
    store_class = platform_stores.PlatformConnectionStore

    assert not hasattr(store_class, "conn"), (
        "if conn ever becomes a class attribute this regression stops proving "
        "anything; the point is that the old check could not see it"
    )

    instance = _construct(store_class, monkeypatch)

    assert hasattr(instance, "conn"), "the consumer receives an object that has conn"
    assert not hasattr(instance, "_get_connection"), (
        "the method the scan used to call does not exist on the store it is "
        "handed; a seam declaring it must now fail here rather than at runtime"
    )
