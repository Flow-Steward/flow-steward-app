"""The controller of `./flow-steward doctor`: which installation it reads, and what a
plain run reports (the doctor task: an id, a status, a cause and a command for each
finding; exit 4 when there is something to do)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.doctor.contracts import DoctorCheck, DoctorFinding
from core.application.doctor.service import DoctorService
from core.entrypoints.cli import doctor
from core.infrastructure.installer import installation_doctor as doctor_probe
from core.infrastructure.installer.state_store import InstallerStateStore


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> InstallerStateStore:
    (tmp_path / "infra/docker").mkdir(parents=True)
    monkeypatch.setattr(doctor, "_ROOT", tmp_path)
    # Another installation's name in the environment, as on a shared host.
    monkeypatch.setattr(doctor, "runtime_env", {"FS_COMPOSE_PROJECT_NAME": "flow-steward"})
    monkeypatch.setattr(doctor_probe, "runtime_env", {})
    return InstallerStateStore(tmp_path / ".flow-steward", lifecycle_state_root=tmp_path)


def test_the_gateway_is_this_installations_project_never_the_default(
    state: InstallerStateStore,
) -> None:
    gateway = doctor._installation_gateway(state, "fs-mine")

    assert gateway is not None
    assert gateway.project_identity() == "fs-mine"


def test_without_a_journal_identity_the_recorded_compose_env_names_the_project(
    state: InstallerStateStore,
) -> None:
    state.root.mkdir(parents=True)
    (state.root / "compose.env").write_text("FS_COMPOSE_PROJECT_NAME=fs-recorded\n", "utf-8")

    gateway = doctor._installation_gateway(state, "")

    assert gateway is not None
    assert gateway.project_identity() == "fs-recorded"


def test_an_installation_that_records_no_project_is_not_guessed(
    state: InstallerStateStore,
) -> None:
    assert doctor._installation_gateway(state, "") is None


def _service() -> DoctorService:
    return DoctorService(
        [
            DoctorCheck(
                "stack_stopped",
                "Services running",
                lambda _probe: DoctorFinding(
                    "stack_stopped",
                    "fail",
                    cause="Not running: fs-app.",
                    next_step="./flow-steward doctor --fix stack_stopped",
                    repairable=True,
                ),
                lambda _probe, _finding: "started",
            ),
            DoctorCheck(
                "schema_state",
                "Database schema",
                lambda _probe: DoctorFinding("schema_state", "ok", cause="Up to date."),
            ),
        ],
        profile="compact",
    )


def _probe(running: str = "") -> SimpleNamespace:
    return SimpleNamespace(profile="compact", operation_running=lambda: running)


def test_a_plain_json_report_names_each_finding_and_the_fix_and_exits_4(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "runtime_env", {})

    code = doctor._diagnose(_service(), _probe(), as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert code == 4
    assert payload["schema"] == "flow-steward.doctor.v1"
    assert payload["problems"] == ["stack_stopped"]
    assert payload["fix_command"] == "./flow-steward doctor --fix stack_stopped"
    (failing,) = [row for row in payload["installation"] if row["status"] == "fail"]
    assert failing == {
        "check_id": "stack_stopped",
        "status": "fail",
        "cause": "Not running: fs-app.",
        "next_step": "./flow-steward doctor --fix stack_stopped",
        "repairable": True,
    }
    assert payload["configuration"] is None  # not run: said, not hidden


def test_no_fix_is_offered_while_an_operation_runs(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "runtime_env", {})

    doctor._diagnose(_service(), _probe(running="lifecycle operation op-1"), as_json=False)

    out = capsys.readouterr().out
    assert "An operation is running: lifecycle operation op-1." in out
    assert "stack_stopped: Not running: fs-app." in out
    summary = out.strip().splitlines()[-1]
    assert summary == "1 problem: stack_stopped."  # and no fix offered while it runs


def test_a_clean_installation_exits_0(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "runtime_env", {})
    clean = DoctorService(
        [
            DoctorCheck(
                "schema_state", "Database schema", lambda _p: DoctorFinding("schema_state", "ok")
            )
        ],
        profile="compact",
    )

    code = doctor._diagnose(clean, _probe(), as_json=False)

    assert code == 0
    assert "No problems found." in capsys.readouterr().out


class _StoppedCompact:
    """A Compact installation after a Docker restart: nothing runs."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def stopped_services(self, _config, *, log) -> list[str]:
        return ["compact-runtime", "pg-fs", "rabbitmq-fs", "traefik"]

    def shared_tree_reports(self, _config, *, repair: bool = False):
        return []

    def installed_runtime_revision(self, _config) -> str:
        return "a" * 40

    def source_revision(self) -> str:
        return "a" * 40  # these sources installed it

    def missing_recorded_images(self, _config) -> list[tuple[str, str]]:
        return []

    def temporary_container_names(self, _config):
        return ()

    def interrupted_update(self, _config) -> str:
        return ""

    def read_compact_volume(self, _config, _script, *_arguments) -> str:
        self.reads.append("journals from the volume")
        return '{"held": false, "journals": []}'

    def stale_graphs(self, _config) -> list[str]:
        self.reads.append("stale_graphs")
        raise OSError('service "pg-fs" is not running')

    def read_compact_runtime_file(self, _config, _path: str):
        self.reads.append("maintenance")
        raise OSError('service "compact-runtime" is not running')


def test_a_stopped_stack_is_one_problem_and_what_needs_it_says_not_checked(
    state: InstallerStateStore,
) -> None:
    """Live on a stopped Compact stand (2026-09-25): lifecycle_interrupted,
    maintenance_left_enabled and graph_catalog each came out FAIL "could not run"."""
    from core.application.doctor.checks import p1_checks

    gateway = _StoppedCompact()
    probe = doctor_probe.InstallationDoctorProbe(
        root=state.root.parent,
        gateway=gateway,
        state=state,
        config=SimpleNamespace(deployment_profile="compact"),
        log=doctor_probe.TemporaryDoctorLog(),
    )

    report = DoctorService(p1_checks(), profile="compact").diagnose(probe)

    (stopped,) = report.failing
    assert stopped.check_id == "stack_stopped"
    assert stopped.cause == "Not running: compact-runtime, pg-fs, rabbitmq-fs, traefik."
    assert stopped.repairable
    blocked = {f.check_id: f for f in report.findings if f.status == "info"}
    # F1: unfinished operations are read from the volume, so they are checked, not
    # blocked, while the runtime is down.
    assert set(blocked) == {"maintenance_left_enabled", "graph_catalog"}
    assert next(f for f in report.findings if f.check_id == "lifecycle_interrupted").status == "ok"
    assert blocked["graph_catalog"].cause == "Not checked: pg-fs is not running."
    assert blocked["maintenance_left_enabled"].next_step == doctor_probe._START_FIRST
    # Nothing was asked of a service that is down; each check read the volume afresh.
    assert set(gateway.reads) == {"journals from the volume"}
    assert probe.operation_running() == ""  # a stopped runtime runs nothing


def test_what_was_not_checked_is_shown_and_not_counted_as_fine(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live: a stopped stack printed "5 of 6 checks are fine" with three not checked."""
    from core.application.doctor.contracts import CheckBlocked

    def blocked(_probe):
        raise CheckBlocked("pg-fs is not running", doctor_probe._START_FIRST)

    monkeypatch.setattr(doctor, "runtime_env", {})
    service = DoctorService(
        [
            DoctorCheck(
                "secrets_modes", "Secrets", lambda _p: DoctorFinding("secrets_modes", "ok")
            ),
            DoctorCheck("graph_catalog", "Graphs", blocked),
        ],
        profile="compact",
    )

    code = doctor._diagnose(service, _probe(), as_json=False)

    out = capsys.readouterr().out
    assert "Installation: 1 of 2 checks are fine." in out
    assert "INFO  graph_catalog: Not checked: pg-fs is not running." in out
    assert code == 0  # nothing failed; nothing to fix here


def test_the_doctor_reads_compact_state_where_the_compact_runtime_keeps_it() -> None:
    """Live on fs-acc (2026-09-25): maintenance was enabled -- `maintenance status` said so
    -- and doctor said "Maintenance mode is off", because it read /data/state/runtime.
    Derived from the runtime's own composition, so the two cannot part again."""
    from core.entrypoints.cli import compact_platform_runtime as compact
    from core.infrastructure.lifecycle.state_store import LifecycleStateStore

    maintenance = compact._STATE_ROOT / "runtime" / "maintenance-state.json"
    assert str(maintenance) == doctor_probe._COMPACT_MAINTENANCE
    operations = LifecycleStateStore(compact._STATE_PROJECT_ROOT).operations_dir
    assert str(operations) == doctor_probe._COMPACT_OPERATIONS


class _RuntimeStopped:
    """Compact with the runtime -- its workers -- down and RabbitMQ up: no consumers."""

    def stopped_services(self, _config, *, log) -> list[str]:
        return ["compact-runtime"]

    def queue_consumers(self, _config) -> int | None:
        return 0


@pytest.mark.parametrize(
    ("profile", "worker_service"), [("compact", "compact-runtime"), ("scalable", "fs-worker")]
)
def test_no_consumer_while_the_workers_are_down_is_not_checked(
    state: InstallerStateStore, profile: str, worker_service: str
) -> None:
    """F4 (live on fs-acc): a stopped runtime gave workers_consuming "fail: No worker
    consumes the job queue" while the other runtime checks said Not checked."""
    from core.application.doctor.hints import hint_checks

    gateway = _RuntimeStopped()
    gateway.stopped_services = lambda _config, *, log: [worker_service]
    probe = doctor_probe.InstallationDoctorProbe(
        root=state.root.parent,
        gateway=gateway,
        state=state,
        config=SimpleNamespace(deployment_profile=profile),
        log=doctor_probe.TemporaryDoctorLog(),
    )
    (check,) = [c for c in hint_checks() if c.check_id == "workers_consuming"]

    finding = DoctorService([check], profile=profile).diagnose(probe).findings[0]

    assert finding.status == "info"
    assert finding.cause == f"Not checked: {worker_service} is not running."
