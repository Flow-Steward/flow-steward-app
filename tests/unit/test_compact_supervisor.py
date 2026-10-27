from __future__ import annotations

import os

import pytest

from core.infrastructure.config.compact_runtime import CompactRuntimeSettings
from core.infrastructure.runtime.compact.health import _payload, consume_observations, monitor_roles
from core.infrastructure.runtime.compact.process_spec import build_process_specs
from core.infrastructure.runtime.compact.readiness import ReadinessRegistry
from core.infrastructure.runtime.compact.supervisor import (
    CompactConfigurationError,
    CompactSupervisor,
    RestartBudget,
)


def test_compact_process_inventory_is_fixed_and_ordered() -> None:
    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "2"}
    )

    specs = build_process_specs(
        settings,
        parent_environment={
            "PATH": "/usr/bin",
            "CANARY_SECRET": "must-not-leak",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
        },
        public_port=8080,
        observation_fd=7,
    )

    assert [spec.role for spec in specs] == [
        "router",
        "app",
        "installer",
        "mcp",
        "control",
        "worker-0",
        "worker-1",
    ]
    assert [(spec.uid, spec.gid) for spec in specs] == [
        (10006, 10006),
        (10001, 10001),
        (10005, 10005),
        (10004, 10004),
        (10003, 10003),
        (10002, 10002),
        (10002, 10002),
    ]
    assert [spec.port for spec in specs] == [8080, 8099, 8101, 8100, 9102, 9200, 9201]
    assert specs[0].command[0] == "/usr/local/bin/traefik"
    assert specs[1].command[:3] == ("python", "-m", "uvicorn")
    assert specs[3].command[-4:] == ("--host", "127.0.0.1", "--port", "8100")
    assert all("CANARY_SECRET" not in spec.environment for spec in specs)
    assert specs[-1].environment["FS_COMPACT_OBSERVATION_FD"] == "7"
    assert specs[-1].pass_fds == (7,)
    assert {spec.role: spec.supplementary_gids for spec in specs} == {
        "router": (),
        # ADR 0029, decision B: 10103 is the installers' write group (AC17-R59); app
        # and workers read the extension tree through its "other" bits
        "app": (10100, 10102),
        "installer": (10103, 10102),
        "mcp": (10102,),
        "control": (10100, 10102),
        "worker-0": (10100, 10102),
        "worker-1": (10100, 10102),
    }


def test_compact_process_environments_copy_only_role_allowlisted_values() -> None:
    settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})
    parent = {
        "PATH": "/usr/bin",
        "PYTHONPATH": "/app",
        "FS_BUILD_SHA": "a" * 40,
        "FS_EMBEDDED_IMAGE_ROLE": "compact-runtime",
        "POSTGRES_PASSWORD": "database-secret",
        "RABBITMQ_PASS": "broker-secret",
        "FS_SECRET_KEY": "application-secret",
        "LANGGRAPH_POSTGRES_URI": "postgresql://checkpoint-secret",
        "FS_AUTH_BOOTSTRAP_ENABLED": "1",
        "FS_AUTH_BOOTSTRAP_TOKEN": "setup-secret",
        "FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT": "2099-01-01T00:00:00Z",
        "FS_AUTH_BOOTSTRAP_STATE_FILE": "/run/flow-steward-runtime/bootstrap-token-state.json",
        "OPENAI_API_KEY": "provider-secret",
    }

    specs = build_process_specs(
        settings,
        parent_environment=parent | {"FS_WORKER_MCP_BEARER_TOKEN": "worker-token"},
        public_port=8080,
        observation_fd=9,
    )
    by_role = {spec.role: spec for spec in specs}

    assert "POSTGRES_PASSWORD" not in by_role["router"].environment
    assert "RABBITMQ_PASS" not in by_role["router"].environment
    assert by_role["worker-0"].environment["RABBITMQ_PASS"] == "broker-secret"
    assert by_role["app"].environment["FS_SECRET_KEY"] == "application-secret"
    assert by_role["app"].environment["LANGGRAPH_POSTGRES_URI"] == (
        "postgresql://checkpoint-secret"
    )
    assert by_role["worker-0"].environment["LANGGRAPH_POSTGRES_URI"] == (
        "postgresql://checkpoint-secret"
    )
    assert by_role["app"].environment["FS_AUTH_BOOTSTRAP_TOKEN"] == "setup-secret"
    assert "FS_AUTH_BOOTSTRAP_TOKEN" not in by_role["installer"].environment
    assert by_role["installer"].environment["CONTROL_LOOPS_ENABLED"] == "0"
    assert by_role["app"].environment["FS_EXTENSION_INSTALLER_ORIGIN"] == ("http://127.0.0.1:8101")
    assert by_role["app"].command[-2:] == ("--workers", "1")
    assert by_role["worker-0"].environment["FS_PROCESS_METRICS_HOST"] == "127.0.0.1"
    assert by_role["worker-0"].environment["FS_BUILD_SHA"] == "a" * 40
    assert by_role["worker-0"].environment["FS_EMBEDDED_IMAGE_ROLE"] == "compact-runtime"
    assert all("OPENAI_API_KEY" not in spec.environment for spec in specs)
    assert by_role["worker-0"].environment["MCP_BEARER_TOKEN"] == "worker-token"
    assert by_role["worker-0"].environment["MCP_URL"] == "http://127.0.0.1:8100/mcp"
    assert by_role["worker-0"].environment["FS_MCP_GROUNDING_REQUIRED"] == "1"
    assert by_role["worker-0"].environment["FS_COMPACT_HEALTH_INTERVAL_SECONDS"] == "2"
    assert all(
        "MCP_BEARER_TOKEN" not in spec.environment
        for spec in specs
        if not spec.role.startswith("worker-")
    )


@pytest.mark.parametrize(
    ("configured", "expected_secure"), [("0", False), ("1", True), (None, True)]
)
def test_compact_cookie_security_reaches_only_app_and_retains_secure_default(
    configured: str | None, expected_secure: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.application.auth import config as auth_config
    from core.application.auth.csrf import auth_cookie_secure

    parent = {"FS_WORKER_MCP_BEARER_TOKEN": "worker-token"}
    if configured is not None:
        parent["FS_AUTH_COOKIE_SECURE"] = configured
    specs = build_process_specs(
        CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"}),
        parent_environment=parent,
        public_port=8080,
        observation_fd=7,
    )
    app = next(spec for spec in specs if spec.role == "app")

    if configured is None:
        assert "FS_AUTH_COOKIE_SECURE" not in app.environment
    else:
        assert app.environment.get("FS_AUTH_COOKIE_SECURE") == configured
    assert all(
        "FS_AUTH_COOKIE_SECURE" not in spec.environment for spec in specs if spec.role != "app"
    )
    monkeypatch.setattr(auth_config, "_runtime_env", app.environment)
    assert auth_cookie_secure() is expected_secure


def test_compact_app_worker_count_is_independent_from_queue_workers() -> None:
    settings = CompactRuntimeSettings.from_environment(
        {
            "FS_DEPLOYMENT_PROFILE": "compact",
            "FS_WORKER_PROCESSES": "4",
            "APP_WORKERS": "2",
        }
    )

    specs = build_process_specs(
        settings,
        parent_environment={"FS_WORKER_MCP_BEARER_TOKEN": "worker-token"},
        public_port=8080,
        observation_fd=9,
    )

    app = next(spec for spec in specs if spec.role == "app")
    workers = [spec for spec in specs if spec.role.startswith("worker-")]
    assert app.command[-2:] == ("--workers", "2")
    assert len(workers) == 4


@pytest.mark.parametrize(
    "identity",
    [
        {"FS_COMPOSE_PROJECT_NAME": "acceptance-compact"},
        {"FS_INSTALLATION_ID": "ef5c927a-a7d5-4b43-9187-051e38e7a389"},
    ],
)
def test_compact_roles_receive_complete_monitoring_configuration(identity) -> None:
    from core.application.lifecycle.runtime_installation_context import (
        resolve_projected_runtime_installation_context,
    )
    from core.infrastructure.config.env_registry import env_specs
    from core.infrastructure.config.monitoring_runtime import load_monitoring_runtime_config

    parent = {
        **identity,
        "FS_DEPLOYMENT_PROFILE": "compact",
        "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
        "RABBITMQ_URL": "amqp://monitoring-broker",
        "FS_MONITORING_EXPECTATIONS": '{"worker":2,"custom-role":null}',
        "FS_MONITORING_DEPENDENCIES": '["postgres","rabbitmq"]',
        "FS_MONITORING_INTERVAL_SECONDS": "11",
        "FS_MONITORING_STALE_SECONDS": "31",
        "FS_MONITORING_HISTORY_ENABLED": "1",
        "FS_MONITORING_CURRENT_RETENTION_DAYS": "9",
        "FS_MONITORING_HISTORY_RETENTION_DAYS": "12",
        "FS_MONITORING_CLEANUP_BATCH_SIZE": "13",
        "FS_MONITORING_QUEUE_MAX_MESSAGES": "14",
        "FS_MONITORING_QUEUE_MAX_BYTES": "1500",
        "FS_MONITORING_OPERATION_TIMEOUT_SECONDS": "3",
        "FS_MONITORING_CANARY_SECRET": "must-not-leak",
    }
    owned = {
        spec.name
        for spec in env_specs()
        if spec.owner == "core/infrastructure/config/monitoring_runtime.py"
    }
    assert owned <= parent.keys() | {"FS_INSTALLATION_ID"}
    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "2"}
    )
    specs = build_process_specs(
        settings, parent_environment=parent, public_port=8080, observation_fd=9
    )
    for spec in specs:
        assert "FS_MONITORING_CANARY_SECRET" not in spec.environment
        if spec.role == "router":
            assert not (owned | identity.keys()) & spec.environment.keys()
            assert "RABBITMQ_URL" not in spec.environment
            continue
        assert (owned & parent.keys()) <= spec.environment.keys()
        assert resolve_projected_runtime_installation_context(spec.environment) == (
            resolve_projected_runtime_installation_context(parent)
        )
        assert load_monitoring_runtime_config(spec.environment) == (
            load_monitoring_runtime_config(parent)
        )
        assert spec.environment["RABBITMQ_URL"] == parent["RABBITMQ_URL"]


def test_empty_compact_worker_count_is_rejected_before_process_creation() -> None:
    with pytest.raises(ValueError, match="decimal integer"):
        CompactRuntimeSettings.from_environment(
            {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": ""}
        )


def test_scalable_runtime_rejects_compact_worker_count() -> None:
    with pytest.raises(ValueError, match="Compact-only"):
        CompactRuntimeSettings.from_environment(
            {"FS_DEPLOYMENT_PROFILE": "scalable", "FS_WORKER_PROCESSES": "2"}
        )


def test_compact_router_trusts_only_configured_proxy_peers() -> None:
    settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})

    specs = build_process_specs(
        settings,
        parent_environment={
            "FS_PROXY_TRUSTED_IPS": "10.0.0.0/8,192.0.2.10/32",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
        },
        public_port=8080,
        observation_fd=9,
    )

    assert (
        "--entryPoints.compact.forwardedHeaders.trustedIPs=10.0.0.0/8,192.0.2.10/32"
        in specs[0].command
    )


def test_restart_budget_uses_bounded_exponential_delays() -> None:
    budget = RestartBudget(limit=3, window_seconds=300, initial_backoff=2, maximum_backoff=30)

    assert [budget.record_failure(now) for now in (0, 1, 2, 3)] == [2, 4, 8, None]


def test_restart_budget_expires_old_failures_but_readiness_does_not_reset_it() -> None:
    budget = RestartBudget(limit=3, window_seconds=300, initial_backoff=2, maximum_backoff=30)

    assert budget.record_failure(0) == 2
    assert budget.record_failure(1) == 4
    budget.record_ready(2)
    assert budget.record_failure(3) == 8
    assert budget.record_failure(301) == 8


def _registry_ready_but_workers(worker_processes: int = 2) -> ReadinessRegistry:
    registry = ReadinessRegistry(worker_processes=worker_processes)
    registry.set_bootstrap_ready(True)
    registry.set_schema_ready(True)
    for dependency in registry.snapshot().dependencies:
        registry.set_dependency(dependency, True)
    for role in registry.snapshot().roles:
        registry.set_role_ready(role, True)
    return registry


def test_aggregate_readiness_requires_every_service_role() -> None:
    """Derived from the registry's own role inventory, not a copy of it."""
    roles = tuple(_registry_ready_but_workers().snapshot().roles)
    assert {"router", "app", "installer", "mcp", "control"} <= set(roles)  # non-vacuity
    for missing in roles:
        registry = _registry_ready_but_workers()
        registry.record_worker(
            role="worker-0", worker_incarnation="one", state="active", sequence=1
        )
        registry.record_worker(
            role="worker-1", worker_incarnation="two", state="active", sequence=1
        )
        registry.set_role_ready(missing, False)
        assert registry.snapshot().ready is False, missing


def test_public_readiness_needs_one_ready_worker_and_details_report_the_whole_pool() -> None:
    """Public readiness is "can this installation serve and run a job": every service
    role and at least one worker. It was the exact pool, and the supervisor stops the
    whole container once readiness stays false past its budget -- so one worker
    crash-looping under its restart backoff took every role down with it. Whether the
    pool is complete stays visible in /health/details."""
    registry = _registry_ready_but_workers()
    assert registry.snapshot().ready is False  # no worker at all

    registry.record_worker(
        role="worker-0", worker_incarnation="one", state="registered", sequence=1
    )
    snapshot = registry.snapshot()
    assert (snapshot.ready, snapshot.workers_complete) == (True, False)

    registry.record_worker(role="worker-1", worker_incarnation="two", state="active", sequence=1)
    snapshot = registry.snapshot()
    assert (snapshot.ready, snapshot.workers_complete) == (True, True)

    registry.record_worker(
        role="worker-1",
        worker_incarnation="two",
        state="maintenance_paused",  # type: ignore[arg-type]
        sequence=2,
    )
    paused_snapshot = registry.snapshot()
    assert (paused_snapshot.ready, paused_snapshot.workers_complete) == (True, True)
    assert paused_snapshot.worker_states["worker-1"] == "maintenance_paused"

    registry.record_worker(role="worker-1", worker_incarnation="two", state="draining", sequence=3)
    snapshot = registry.snapshot()
    assert (snapshot.ready, snapshot.workers_complete) == (True, False)
    assert snapshot.worker_states == {"worker-0": "registered", "worker-1": "draining"}

    registry.record_worker(role="worker-0", worker_incarnation="one", state="draining", sequence=2)
    assert registry.snapshot().ready is False  # no worker left that would take a job


def test_health_details_report_whether_the_worker_pool_is_complete() -> None:
    registry = _registry_ready_but_workers()
    registry.record_worker(role="worker-0", worker_incarnation="one", state="active", sequence=1)

    public_status, public = _payload(registry, detailed=False)
    detail_status, details = _payload(registry, detailed=True)

    assert (public_status, public) == (200, {"ready": True})
    assert detail_status == 200
    assert details["workers_complete"] is False
    assert details["worker_states"] == {"worker-0": "active"}


def test_readiness_rejects_stale_or_unexpected_worker_observations() -> None:
    registry = ReadinessRegistry(worker_processes=1)
    registry.record_worker(
        role="worker-0", worker_incarnation="one", state="registered", sequence=2
    )

    registry.record_worker(role="worker-0", worker_incarnation="one", state="active", sequence=1)
    registry.record_worker(
        role="worker-2", worker_incarnation="other", state="registered", sequence=1
    )
    registry.record_worker(
        role="worker-0",
        worker_incarnation="one",
        state="invented",
        sequence=3,  # type: ignore[arg-type]
    )

    assert registry.snapshot().worker_states == {"worker-0": "registered"}


def test_worker_observation_expires_and_restart_clears_previous_incarnation() -> None:
    now = 0.0
    registry = ReadinessRegistry(
        worker_processes=1,
        stale_seconds=5,
        monotonic=lambda: now,
    )
    registry.record_worker(role="worker-0", worker_incarnation="old", state="active", sequence=1)
    assert registry.snapshot().worker_states == {"worker-0": "active"}

    now = 6
    assert registry.snapshot().worker_states == {}
    registry.reset_runtime()
    registry.record_worker(role="worker-0", worker_incarnation="old", state="active", sequence=2)
    assert registry.snapshot().worker_states == {}


def test_partial_process_creation_stops_previously_started_children() -> None:
    settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})
    specs = build_process_specs(
        settings,
        parent_environment={"PATH": "/usr/bin", "FS_WORKER_MCP_BEARER_TOKEN": "worker-token"},
        public_port=8080,
        observation_fd=7,
    )[:2]

    class _Process:
        pid = 101
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return int(self.returncode or 0)

    first = _Process()
    calls = 0

    def _factory(_spec):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("cannot start")
        return first

    def _signal(process, signum):
        del signum
        process.returncode = -15

    supervisor = CompactSupervisor(
        specs=specs,
        settings=settings,
        process_factory=_factory,
        signal_process=_signal,
    )

    with pytest.raises(CompactConfigurationError, match="app"):
        supervisor.start()

    assert first.returncode == -15


def test_supervisor_exits_after_fourth_failure_inside_restart_window() -> None:
    settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})
    spec = build_process_specs(
        settings,
        parent_environment={"PATH": "/usr/bin", "FS_WORKER_MCP_BEARER_TOKEN": "worker-token"},
        public_port=8080,
        observation_fd=7,
    )[0]
    now = 0.0
    created = 0

    class _ExitedProcess:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return 1

        def wait(self, timeout=None):
            del timeout
            return 1

    def _factory(_spec):
        nonlocal created
        created += 1
        return _ExitedProcess(created)

    def _sleep(seconds):
        nonlocal now
        now += seconds

    supervisor = CompactSupervisor(
        specs=(spec,),
        settings=settings,
        process_factory=_factory,
        monotonic=lambda: now,
        sleep=_sleep,
    )

    assert supervisor.run() == 1
    assert created == 4
    assert now == 14


def test_health_payload_exposes_only_aggregate_state_on_public_path() -> None:
    registry = ReadinessRegistry(worker_processes=1)

    status, public = _payload(registry, detailed=False)
    detailed_status, detailed = _payload(registry, detailed=True)

    assert status == detailed_status == 503
    assert public == {"ready": False}
    assert detailed["dependencies"] == {"postgres": False, "rabbitmq": False}


def test_role_monitor_uses_protocol_probe_result_instead_of_tcp_acceptance() -> None:
    settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})
    specs = build_process_specs(
        settings,
        parent_environment={"FS_WORKER_MCP_BEARER_TOKEN": "worker-token"},
        public_port=8080,
        observation_fd=9,
    )
    registry = ReadinessRegistry(worker_processes=1)
    stop = __import__("threading").Event()
    observed: list[tuple[str, int, float]] = []

    def probe(role: str, port: int, timeout: float) -> bool:
        observed.append((role, port, timeout))
        stop.set()
        return role != "app"

    monitor_roles(
        registry,
        specs=specs,
        stop=stop,
        interval_seconds=2,
        probe=probe,
    )

    assert registry.snapshot().roles["app"] is False
    assert {role for role, _port, _timeout in observed} == {
        "router",
        "app",
        "installer",
        "mcp",
        "control",
    }


def test_observation_reader_ignores_malformed_records_and_updates_registry() -> None:
    read_descriptor, write_descriptor = os.pipe()
    registry = ReadinessRegistry(worker_processes=1)
    os.write(
        write_descriptor,
        b'not-json\n{"role":"worker-0","worker_incarnation":"abc","state":"registered","sequence":1}\n',
    )
    os.close(write_descriptor)

    consume_observations(read_descriptor, registry)

    assert registry.snapshot().worker_states == {"worker-0": "registered"}


def test_supervisor_restarts_the_complete_group_after_mandatory_role_crash() -> None:
    settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})
    specs = build_process_specs(
        settings,
        parent_environment={"PATH": "/usr/bin", "FS_WORKER_MCP_BEARER_TOKEN": "worker-token"},
        public_port=8080,
        observation_fd=7,
    )[:2]
    generation = 0
    created: list[tuple[int, str]] = []

    class _Process:
        def __init__(self, role, pid):
            self.role = role
            self.pid = pid
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return int(self.returncode or 0)

    processes: list[_Process] = []

    def factory(spec):
        nonlocal generation
        generation += 1
        process = _Process(spec.role, generation)
        processes.append(process)
        created.append((generation, spec.role))
        if generation == 2:
            process.returncode = 1
        return process

    def signal_process(process, signum):
        del signum
        process.returncode = -15

    def sleep(_seconds):
        if len(created) >= 4:
            supervisor.request_shutdown()

    supervisor = CompactSupervisor(
        specs=specs,
        settings=settings,
        process_factory=factory,
        signal_process=signal_process,
        sleep=sleep,
    )
    # The group path: a role falls back to it when its own tree cannot be cleared alone.
    # Per-role restart is covered in test_compact_role_restart.py.
    supervisor._contain_role_failure = lambda _role: False  # type: ignore[method-assign]

    assert supervisor.run() == 0
    assert created == [(1, "router"), (2, "app"), (3, "router"), (4, "app")]
    assert all(process.returncode is not None for process in processes)


def test_supervisor_keeps_http_services_available_while_workers_drain() -> None:
    settings = CompactRuntimeSettings.from_environment({"FS_DEPLOYMENT_PROFILE": "compact"})
    specs = build_process_specs(
        settings,
        parent_environment={"FS_WORKER_MCP_BEARER_TOKEN": "worker-token"},
        public_port=8080,
        observation_fd=7,
    )
    signalled: list[str] = []

    class _Process:
        def __init__(self, role: str, pid: int) -> None:
            self.role = role
            self.pid = pid
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return int(self.returncode or 0)

    def signal_process(process, signum):
        assert signum == 15
        signalled.append(process.role)
        process.returncode = -15

    supervisor = CompactSupervisor(
        specs=specs,
        settings=settings,
        process_factory=lambda spec: _Process(spec.role, len(signalled) + 100),
        signal_process=signal_process,
    )
    supervisor.start()
    supervisor.shutdown()

    drain_end = max(signalled.index("control"), signalled.index("worker-0"))
    assert drain_end < signalled.index("app")
    assert drain_end < signalled.index("mcp")
    assert drain_end < signalled.index("installer")


def test_supervisor_exits_when_startup_readiness_budget_expires() -> None:
    settings = CompactRuntimeSettings.from_environment(
        {
            "FS_DEPLOYMENT_PROFILE": "compact",
            "FS_COMPACT_STARTUP_TIMEOUT_SECONDS": "2",
        }
    )
    spec = build_process_specs(
        settings,
        parent_environment={"PATH": "/usr/bin", "FS_WORKER_MCP_BEARER_TOKEN": "worker-token"},
        public_port=8080,
        observation_fd=7,
    )[0]
    now = 0.0

    class _Process:
        pid = 1
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return int(self.returncode or 0)

    process = _Process()

    def sleep(seconds):
        nonlocal now
        now += seconds

    def signal_process(target, signum):
        del signum
        target.returncode = -15

    supervisor = CompactSupervisor(
        specs=(spec,),
        settings=settings,
        process_factory=lambda _spec: process,
        signal_process=signal_process,
        monotonic=lambda: now,
        sleep=sleep,
        readiness=lambda: False,
    )

    assert supervisor.run() == 1
    assert now == 2


def test_compact_mcp_process_gets_public_oauth_discovery_urls() -> None:
    """AC17-R56: a 401 from a public MCP call must not point the client at its own machine.

    server_runtime falls back to http://fs-app:8099 and http://localhost:8100 — a Scalable
    service name and the caller's own host — and neither variable is declared in
    docker-compose.compact.yaml, so those fallbacks were what a public client received.
    """
    from core.infrastructure.runtime.compact.process_spec import _mcp_discovery_environment

    derived = _mcp_discovery_environment({"FS_PUBLIC_BASE_URL": "https://fs.example.test"})

    assert derived["FS_MCP_ISSUER_URL"] == "https://fs.example.test"
    assert derived["FS_MCP_RESOURCE_URL"] == "https://fs.example.test/mcp"


def test_compact_mcp_discovery_urls_are_absent_without_a_public_origin() -> None:
    from core.infrastructure.runtime.compact.process_spec import _mcp_discovery_environment

    assert _mcp_discovery_environment({}) == {}


def test_compact_mcp_discovery_rejects_a_credentialled_public_origin() -> None:
    from core.infrastructure.runtime.compact.process_spec import _mcp_discovery_environment

    with pytest.raises(ValueError):
        _mcp_discovery_environment({"FS_PUBLIC_BASE_URL": "https://user:pw@fs.example.test"})


def test_compact_mcp_spec_actually_carries_the_discovery_urls() -> None:
    """The helper being correct is not the same as the helper being used.

    AC17-R56. Two findings today turned on exactly this gap: `_application_service` existed
    and was unused (R51), and the install lookup existed on a store nobody handed to the
    probe (R67). So assert the wiring, not just the derivation.
    """
    settings = CompactRuntimeSettings.from_environment(
        {"FS_DEPLOYMENT_PROFILE": "compact", "FS_WORKER_PROCESSES": "1"}
    )

    specs = build_process_specs(
        settings,
        parent_environment={
            "PATH": "/usr/bin",
            "FS_WORKER_MCP_BEARER_TOKEN": "worker-token",
            "FS_PUBLIC_BASE_URL": "https://fs.example.test",
        },
        public_port=8080,
        observation_fd=7,
    )
    mcp_spec = next(spec for spec in specs if spec.role == "mcp")

    assert mcp_spec.environment["FS_MCP_ISSUER_URL"] == "https://fs.example.test"
    assert mcp_spec.environment["FS_MCP_RESOURCE_URL"] == "https://fs.example.test/mcp"
    assert mcp_spec.environment["FS_MCP_ALLOWED_HOSTS"] == "fs.example.test"


def test_compact_discovery_urls_drop_an_explicit_default_port() -> None:
    """AC17-R55/R56: the installer writes https://host:443, and that must not be published.

    A discovery document advertising an explicit :443 makes clients compare against a value
    they would never construct themselves.
    """
    from core.infrastructure.runtime.compact.process_spec import _mcp_discovery_environment

    derived = _mcp_discovery_environment({"FS_PUBLIC_BASE_URL": "https://fs.example.test:443"})

    assert derived["FS_MCP_ISSUER_URL"] == "https://fs.example.test"
    assert derived["FS_MCP_RESOURCE_URL"] == "https://fs.example.test/mcp"


def test_compact_discovery_urls_keep_a_non_default_port() -> None:
    from core.infrastructure.runtime.compact.process_spec import _mcp_discovery_environment

    derived = _mcp_discovery_environment({"FS_PUBLIC_BASE_URL": "https://fs.example.test:8443"})

    assert derived["FS_MCP_ISSUER_URL"] == "https://fs.example.test:8443"
    assert derived["FS_MCP_RESOURCE_URL"] == "https://fs.example.test:8443/mcp"
