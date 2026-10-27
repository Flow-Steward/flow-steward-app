"""AC17-R134: a dependency on a service that reports readiness must wait for it.

``depends_on`` has two strengths. ``service_started`` means the container was
created; ``service_healthy`` means the thing inside it answered. Every database
and broker dependency in this repository used the weak one, and won its race only
because Postgres usually starts quickly.

The rule below is not a list of service names. A compose file that declares a
``healthcheck`` has said, in that file, that this service has a readiness signal
worth asking about -- so anything depending on it must wait for that signal. The
names appear only as the non-vacuity anchor: delete the healthchecks and the rule
above would be satisfied by an empty set.

Two of the assertions pin measurements rather than preferences, because both
obvious commands report ready too early. They are documented at their site.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = ROOT / "infra" / "docker"

RUNTIME_COMPOSE_FILES = (
    "docker-compose.yaml",
    "docker-compose.prod.yaml",
    "docker-compose.compact.yaml",
)

# The services every runtime compose file has to be able to wait for. This is the
# anchor that keeps the derived rule from being satisfied by an empty set.
SERVICES_THAT_MUST_REPORT_READINESS = ("pg-fs", "rabbitmq-fs")


def _services(filename: str) -> dict:
    document = yaml.safe_load((COMPOSE_DIR / filename).read_text())
    return dict(document.get("services") or {})


def _healthchecked(services: dict) -> set[str]:
    return {name for name, body in services.items() if (body or {}).get("healthcheck")}


def _weak_dependencies(services: dict) -> list[str]:
    """Every place a dependent settles for less than the readiness signal on offer."""

    healthchecked = _healthchecked(services)
    weak = []
    for name, body in services.items():
        depends_on = (body or {}).get("depends_on")
        if not depends_on:
            continue
        if isinstance(depends_on, list):
            # The short form cannot carry a condition at all: it is service_started.
            weak.extend(
                f"{name} -> {dep} (short form, no condition)"
                for dep in depends_on
                if dep in healthchecked
            )
            continue
        for dep, spec in depends_on.items():
            if dep not in healthchecked:
                continue
            condition = str((spec or {}).get("condition") or "").strip()
            if condition != "service_healthy":
                weak.append(f"{name} -> {dep} ({condition or 'no condition'})")
    return sorted(weak)


@pytest.mark.parametrize("filename", RUNTIME_COMPOSE_FILES)
def test_a_service_that_reports_readiness_is_waited_for(filename: str) -> None:
    assert _weak_dependencies(_services(filename)) == []


@pytest.mark.parametrize("filename", RUNTIME_COMPOSE_FILES)
def test_the_database_and_broker_report_readiness_and_are_depended_on(filename: str) -> None:
    """Without this the rule above passes on a file that deleted its healthchecks."""

    services = _services(filename)
    healthchecked = _healthchecked(services)
    for required in SERVICES_THAT_MUST_REPORT_READINESS:
        assert required in healthchecked, f"{filename}: {required} declares no healthcheck"
        dependents = [
            name
            for name, body in services.items()
            if required in ((body or {}).get("depends_on") or {})
        ]
        assert dependents, f"{filename}: nothing waits for {required}"


@pytest.mark.parametrize("filename", RUNTIME_COMPOSE_FILES)
def test_postgres_readiness_is_probed_over_tcp(filename: str) -> None:
    """Measured, not preferred.

    While ``/docker-entrypoint-initdb.d`` runs, the entrypoint has a temporary
    server up on the unix socket only. ``pg_isready`` without a host reaches that
    one and answers OK -- 22 seconds before the schema existed, in the run this
    was measured on. The temporary server sets ``listen_addresses=''``, so an
    answer over TCP is the one that cannot arrive early.
    """

    test = _services(filename)["pg-fs"]["healthcheck"]["test"]
    command = " ".join(test)

    assert "pg_isready" in command
    assert "-h 127.0.0.1" in command, f"{filename}: pg-fs would answer during initdb"


@pytest.mark.parametrize("filename", RUNTIME_COMPOSE_FILES)
def test_broker_readiness_is_the_listener_not_the_node(filename: str) -> None:
    """Measured: ``ping`` answers OK 4-5 seconds before the AMQP listener accepts.

    ``ping`` says the Erlang node is reachable. A dependent needs the port.

    AC17-R151: and not through ``rabbitmq-diagnostics`` at all -- each run boots an
    Erlang VM, and at a 2 s interval that held half a core on an idle server. A TCP
    connect to the listener was measured to succeed at the same moment the broker
    answers the AMQP header with Connection.Start.
    """

    healthcheck = _services(filename)["rabbitmq-fs"]["healthcheck"]
    test = " ".join(healthcheck["test"])

    assert "/dev/tcp/127.0.0.1/5672" in test
    assert "ping" not in test
    for erlang_vm_per_probe in ("rabbitmq-diagnostics", "rabbitmqctl", "escript"):
        assert erlang_vm_per_probe not in test


def test_the_rule_catches_each_way_of_settling_for_less() -> None:
    """The green direction is the easy one; these are the three red ones."""

    weak_condition = {
        "db": {"healthcheck": {"test": ["CMD", "true"]}},
        "app": {"depends_on": {"db": {"condition": "service_started"}}},
    }
    short_form = {
        "db": {"healthcheck": {"test": ["CMD", "true"]}},
        "app": {"depends_on": ["db"]},
    }
    no_condition = {
        "db": {"healthcheck": {"test": ["CMD", "true"]}},
        "app": {"depends_on": {"db": {}}},
    }
    correct = {
        "db": {"healthcheck": {"test": ["CMD", "true"]}},
        "app": {"depends_on": {"db": {"condition": "service_healthy"}}},
    }
    unhealthchecked = {
        "job": {},
        "app": {"depends_on": {"job": {"condition": "service_completed_successfully"}}},
    }

    assert _weak_dependencies(weak_condition) == ["app -> db (service_started)"]
    assert _weak_dependencies(short_form) == ["app -> db (short form, no condition)"]
    assert _weak_dependencies(no_condition) == ["app -> db (no condition)"]
    assert _weak_dependencies(correct) == []
    assert _weak_dependencies(unhealthchecked) == []
