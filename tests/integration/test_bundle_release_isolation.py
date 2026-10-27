"""A bundle's release checks must not be able to reach past the candidate.

The suites are extension code. Before this, the platform's smoke loaded them with
`importlib` into its own trusted process — the one holding the candidate's admin
session, the platform DSN and the Docker socket — and the "public API only" rule
was a substring search in each bundle's own test. A substring search is a
guardrail against the honest mistake; it is not a boundary.

These tests run a deliberately hostile `acme.*` fixture suite through the real
runner and check that each thing it reaches for is *denied at run time*, not
merely absent from its source.

They need Docker and the runner image; they skip when either is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "smoke"))

from bundle_release_checks import (
    DEFAULT_RUNNER_IMAGE,
    RUNNER_PIDS,
    RUNNER_USER,
    discover_bundle_suites,
    run_bundle_suite,
    suite_network,
)

#: Set by a stand that wants the network half proved against its real candidate:
#: the app container, the alias its URL names, and the services a suite may
#: reach. Without them the reachability tests skip rather than pretend.
APP_CONTAINER_ENV = "FS_SMOKE_CANDIDATE_APP_CONTAINER"
APP_ALIAS_ENV = "FS_SMOKE_CANDIDATE_APP_ALIAS"
INTERNAL_BASE_ENV = "FS_SMOKE_CANDIDATE_INTERNAL_BASE"
#: A token carrying only `NARROW_SUITE_SCOPES`, so the refusal proves the scopes
#: rather than the absence of a token.
SUITE_TOKEN_ENV = "FS_SMOKE_SUITE_TOKEN"

HOSTILE_CHECKS = '''
"""A suite that tries to reach everything it must not."""

import json

RELEASE_SCOPES = ["admin:all"]
RELEASE_INPUTS: list[str] = []


def _record(evidence, name, reached, detail):
    # `passed=True` means "it reached it" — the test asserts these are all False.
    evidence.add(name, bool(reached), str(detail)[:200])


def read_core_source(api, evidence):
    import pathlib

    hits = [str(p) for p in pathlib.Path("/").glob("app/core/*.py")]
    hits += [str(p) for p in pathlib.Path("/").glob("core/*.py")]
    _record(evidence, "reached_core_source", bool(hits), hits[:3])


def read_platform_dsn(api, evidence):
    """Nothing of the platform's environment may be here.

    `GPG_KEY` and friends belong to the base image, not to Flow Steward; what
    must be absent is anything the platform was configured with, plus any
    `FS_*` name that is not one of the four the runner declares.
    """
    import os

    declared = {
        "FS_RELEASE_BASE_URL",
        "FS_RELEASE_TOKEN",
        "FS_RELEASE_ACCOUNT",
        "FS_RELEASE_CHECK_IDS",
    }
    leaked = sorted(
        name
        for name in os.environ
        if (name.startswith(("FS_", "POSTGRES", "PG"))and name not in declared
            and not name.startswith("FS_RELEASE_INPUT_"))
        or any(marker in name.upper() for marker in ("DSN", "_PASSWORD", "PASSWORD_"))
    )
    _record(evidence, "reached_platform_secrets", bool(leaked), leaked)


def read_docker_socket(api, evidence):
    import pathlib

    socket = pathlib.Path("/var/run/docker.sock")
    _record(evidence, "reached_docker_socket", socket.exists(), socket.exists())


def write_to_the_bundle(api, evidence):
    import pathlib

    target = pathlib.Path("/bundle/release/written_by_the_suite.txt")
    try:
        target.write_text("x")
        reached = True
    except OSError as exc:
        reached = False
        detail = type(exc).__name__
    else:
        detail = "wrote"
    _record(evidence, "reached_bundle_write", reached, detail)


def reach_internal_services(api, evidence):
    """Nothing of the candidate's own infrastructure may be on this wire."""
    import socket

    reached = []
    for host, port in (
        ("pg-fs", 5432),
        ("postgres", 5432),
        ("rabbitmq-fs", 5672),
        ("seaweedfs", 8333),
    ):
        try:
            with socket.create_connection((host, port), timeout=3):
                reached.append(f"{host}:{port}")
        except OSError:
            continue
    _record(evidence, "reached_internal_services", bool(reached), reached)


def reach_the_internet(api, evidence):
    """An `--internal` network has no route off the host, and that is the point."""
    import socket

    reached = []
    for host, port in (("1.1.1.1", 53), ("pypi.org", 443), ("github.com", 443)):
        try:
            with socket.create_connection((host, port), timeout=3):
                reached.append(f"{host}:{port}")
        except OSError:
            continue
    _record(evidence, "reached_the_internet", bool(reached), reached)


def reach_the_candidate(api, evidence):
    """The positive control: without it, a broken network passes every denial.

    Any HTTP answer proves the wire; the harness reports 0 when the connection
    itself failed. Readiness is the candidate's business, not this test's — a
    stand whose background jobs have not run yet answers 503 and is still there.
    """
    status, _ = api.request("GET", "/health/ready")
    _record(evidence, "reached_the_candidate", int(status or 0) > 0, f"HTTP {status}")


def delete_something_it_was_not_given(api, evidence):
    """The token must not be able to remove anything, including its own work."""
    attempts = {}
    for label, path in (
        ("project", "/api/projects/00000000-0000-0000-0000-000000000000"),
        ("dataset", "/api/datasets/release-suite-not-mine"),
        ("workflow", "/api/workflows/release_suite_not_mine"),
    ):
        status, _ = api.request("DELETE", path)
        attempts[label] = status
    # Refused, not "not found": a 404 would mean the route was open to it.
    _record(evidence, "was_allowed_to_delete", any(v != 403 for v in attempts.values()), attempts)


def report_runtime_limits(api, evidence):
    """What the container actually runs as, read from inside it."""
    import os
    import pathlib

    def _read(path):
        try:
            return pathlib.Path(path).read_text().strip()
        except OSError:
            return ""

    memory = _read("/sys/fs/cgroup/memory.max") or _read(
        "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    )
    pids = _read("/sys/fs/cgroup/pids.max") or _read("/sys/fs/cgroup/pids/pids.max")
    # Recorded as a passing row: the test reads the detail, not the verdict.
    evidence.add(
        "runtime_limits",
        True,
        json.dumps({"uid": os.getuid(), "gid": os.getgid(), "memory": memory, "pids": pids}),
    )


def read_host_filesystem(api, evidence):
    import pathlib

    # The repository the platform runs from, if it were mounted.
    hits = [str(p) for p in pathlib.Path("/").glob("app/scripts/smoke/*.py")]
    _record(evidence, "reached_platform_scripts", bool(hits), hits[:3])


RELEASE_CHECKS = {
    "hostile_bundle_write": write_to_the_bundle,
    "hostile_core_source": read_core_source,
    "hostile_docker_socket": read_docker_socket,
    "hostile_host_filesystem": read_host_filesystem,
    "hostile_internal_services": reach_internal_services,
    "hostile_internet": reach_the_internet,
    "hostile_candidate": reach_the_candidate,
    "hostile_delete": delete_something_it_was_not_given,
    "hostile_platform_secrets": read_platform_dsn,
    "hostile_runtime_limits": report_runtime_limits,
}
'''


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", DEFAULT_RUNNER_IMAGE],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="needs Docker and the bundle runner image"
)


@pytest.fixture
def hostile_bundle(tmp_path: Path):
    root = tmp_path / "extensions"
    bundle = root / "acme_hostile_probe"
    (bundle / "release").mkdir(parents=True)
    (bundle / "release" / "checks.py").write_text(HOSTILE_CHECKS, encoding="utf-8")
    suites = discover_bundle_suites(root)
    assert len(suites) == 1
    return suites[0]


def _rows_by_name(rows: list[dict]) -> dict[str, dict]:
    return {str(row.get("name")): row for row in rows}


def test_a_hostile_suite_reaches_none_of_what_it_tries(hostile_bundle) -> None:
    """Every reach is denied at run time, and the suite still reports normally.

    Run with no network at all, so this covers the host-side half: the filesystem,
    the socket, the environment and the read-only mount. The network half is
    proved separately, on the network a real release run builds.
    """
    rows, _created = run_bundle_suite(
        hostile_bundle,
        base_url="http://127.0.0.1:1",
        token="unused-in-this-test",
        account="",
        network="none",
        inputs={},
        timeout_seconds=180,
    )

    by_name = _rows_by_name(rows)
    assert by_name, f"the isolated run produced nothing: {rows}"
    for name in (
        "reached_core_source",
        "reached_docker_socket",
        "reached_platform_scripts",
        "reached_platform_secrets",
        "reached_bundle_write",
    ):
        assert name in by_name, f"{name} was not attempted: {sorted(by_name)}"
        assert by_name[name]["passed"] is False, f"{name}: {by_name[name]['detail']}"


def test_the_container_is_unprivileged_and_bounded(hostile_bundle) -> None:
    """Not root, and not free to consume the host.

    Root in the container owns root's view of every mount, and a read-only mount
    is then the only thing between a suite and what it was handed. The limits
    keep a suite that spins from taking the machine with it.
    """
    rows, _created = run_bundle_suite(
        hostile_bundle,
        base_url="http://127.0.0.1:1",
        token="unused-in-this-test",
        account="",
        network="none",
        inputs={},
        timeout_seconds=180,
    )

    limits = _rows_by_name(rows).get("runtime_limits")
    assert limits, f"the container did not report its limits: {rows}"
    reported = json.loads(limits["detail"])
    assert reported["uid"] != 0, "the suite ran as root"
    reported_user = f"{reported['uid']}:{reported['gid']}"
    assert reported_user == RUNNER_USER
    assert reported["pids"] == RUNNER_PIDS
    # 512m, as the runner declares it.
    assert int(reported["memory"]) == 512 * 1024 * 1024, reported["memory"]


@pytest.mark.skipif(
    not all(os.environ.get(name) for name in (APP_CONTAINER_ENV, APP_ALIAS_ENV)),
    reason=f"needs {APP_CONTAINER_ENV} and {APP_ALIAS_ENV} pointing at a live candidate",
)
def test_on_a_real_release_network_only_the_candidate_is_reachable(hostile_bundle) -> None:
    """The network a release run actually builds, not a stand-in.

    The candidate's own Compose network carries Postgres, RabbitMQ, the object
    store and a route off the host. This builds the network the dispatcher builds
    — `--internal`, holding the candidate and the declared services — and checks
    that the suite reaches the candidate and nothing else.
    """
    app_container = os.environ[APP_CONTAINER_ENV]
    app_alias = os.environ[APP_ALIAS_ENV]
    base_url = os.environ.get(INTERNAL_BASE_ENV) or f"http://{app_alias}:8099"

    with suite_network(app_container=app_container, app_alias=app_alias, services=[]) as network:
        rows, _created = run_bundle_suite(
            hostile_bundle,
            base_url=base_url,
            token="unused-in-this-test",
            account="",
            network=network,
            inputs={},
            timeout_seconds=300,
        )

    by_name = _rows_by_name(rows)
    assert by_name, f"the isolated run produced nothing: {rows}"
    # Positive control first: on a network where nothing is reachable, every
    # denial below would pass while proving nothing at all.
    candidate = by_name.get("reached_the_candidate")
    assert candidate, f"the candidate was not attempted: {sorted(by_name)}"
    assert candidate["passed"] is True, f"the candidate was unreachable: {candidate['detail']}"
    for name in ("reached_internal_services", "reached_the_internet"):
        assert name in by_name, f"{name} was not attempted: {sorted(by_name)}"
        assert by_name[name]["passed"] is False, f"{name}: {by_name[name]['detail']}"


@pytest.mark.skipif(
    not all(os.environ.get(name) for name in (APP_CONTAINER_ENV, APP_ALIAS_ENV, SUITE_TOKEN_ENV)),
    reason=f"needs {APP_CONTAINER_ENV}, {APP_ALIAS_ENV} and a narrow {SUITE_TOKEN_ENV}",
)
def test_a_suite_token_cannot_delete_anything(hostile_bundle) -> None:
    """The narrow scopes are the point, and 403 is what makes them real.

    Every DELETE on the candidate is admin-only, so a suite's token is refused
    even for resources it created — which is why the trusted harness declares,
    prepares and removes them instead.
    """
    app_container = os.environ[APP_CONTAINER_ENV]
    app_alias = os.environ[APP_ALIAS_ENV]
    base_url = os.environ.get(INTERNAL_BASE_ENV) or f"http://{app_alias}:8099"

    with suite_network(app_container=app_container, app_alias=app_alias, services=[]) as network:
        rows, _created = run_bundle_suite(
            hostile_bundle,
            base_url=base_url,
            token=os.environ[SUITE_TOKEN_ENV],
            account="",
            network=network,
            inputs={},
            timeout_seconds=300,
        )

    allowed = _rows_by_name(rows).get("was_allowed_to_delete")
    assert allowed, f"the deletions were not attempted: {sorted(_rows_by_name(rows))}"
    assert allowed["passed"] is False, f"a suite token deleted something: {allowed['detail']}"
    assert "403" in allowed["detail"], allowed["detail"]


def test_the_runner_image_is_pinned_by_digest() -> None:
    """A tag is republished; a digest is the image that was reviewed."""
    assert "@sha256:" in DEFAULT_RUNNER_IMAGE
    assert ":latest" not in DEFAULT_RUNNER_IMAGE
    assert DEFAULT_RUNNER_IMAGE.split("@", 1)[0] == "python"


def test_discovery_reads_the_declaration_without_executing_it(tmp_path: Path) -> None:
    """Even discovery must not run extension code.

    A `checks.py` that raises on import would take the whole smoke run down if
    discovery imported it; parsing it cannot.
    """
    root = tmp_path / "extensions"
    bundle = root / "acme_explodes_on_import"
    (bundle / "release").mkdir(parents=True)
    (bundle / "release" / "checks.py").write_text(
        'raise SystemExit("imported")\n'
        'RELEASE_SCOPES = ["admin:all"]\n'
        'RELEASE_CHECKS = {"never": None}\n',
        encoding="utf-8",
    )

    suites = discover_bundle_suites(root)

    assert [suite.name for suite in suites] == ["acme_explodes_on_import"]
    assert suites[0].checks == ["never"]
    assert suites[0].scopes == ["admin:all"]


def test_the_platform_runner_never_executes_extension_python() -> None:
    """Pinned, because this is the boundary and it is one line from being lost."""
    runner = Path(__file__).resolve().parents[2] / "scripts" / "smoke"
    dispatcher = (runner / "bundle_release_checks.py").read_text(encoding="utf-8")
    semantic = (runner / "candidate_semantic_checks.py").read_text(encoding="utf-8")

    for source in (dispatcher, semantic):
        assert "exec_module" not in source
        assert "spec_from_file_location" not in source
    # The harness does import the bundle — inside the container, never here.
    harness = (runner / "bundle_release_harness.py").read_text(encoding="utf-8")
    assert "exec_module" in harness
    # The harness starts nothing: it is started.
    assert "subprocess" not in harness
    assert "docker run" not in harness


def test_a_declared_service_this_host_lacks_is_named_not_raised(tmp_path: Path) -> None:
    """A stand without a service must say which one, and still run the rest.

    Before this, `docker network connect` raised out of the context manager and
    the whole suite reported a `CalledProcessError` — a stack trace about Docker
    where the useful sentence is "this host has no mailpit". The candidate is
    still not optional: without it there is nothing to test against.
    """
    root = tmp_path / "extensions"
    bundle = root / "acme_service_probe"
    (bundle / "release").mkdir(parents=True)
    (bundle / "release" / "checks.py").write_text(
        'RELEASE_SCOPES = ["read:*"]\n'
        'RELEASE_SERVICES = ["nowhere"]\n'
        "def _noop(api, evidence):\n"
        '    evidence.add("ran", True, "")\n'
        'RELEASE_CHECKS = {"noop": _noop}\n',
        encoding="utf-8",
    )
    suite = discover_bundle_suites(root)[0]
    assert suite.services == ["nowhere"]

    reported: list[tuple[str, str, str]] = []
    with suite_network(
        app_container=os.environ.get(APP_CONTAINER_ENV) or "",
        app_alias="fs-app",
        services=[("nowhere", "no-such-container-at-all")],
        on_missing_service=lambda *row: reported.append(row),
    ) as network:
        assert network.startswith("fs-release-suite-")

    assert [row[0] for row in reported] == ["nowhere"]
    assert "no-such-container-at-all" in reported[0][1]
