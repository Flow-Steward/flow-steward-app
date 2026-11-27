"""What the running roles skipped of the platform-module set (ADR 0082 §5 item 7)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.application.lifecycle.stack_models import StackGatewayError
from core.application.release.platform_modules import LazyModuleActivator, NoModuleActivator
from core.entrypoints.cli.ops import platform_modules as manage
from core.infrastructure.platform_modules.role_status import (
    ROLE_STATUS_DIR_ENV,
    read_role_module_statuses,
    write_role_module_status,
)
from core.infrastructure.release.platform_module_activator import (
    COMPACT_MODULE_SERVICES,
    CompactModuleActivator,
    FilesystemModuleActivator,
    RoleModuleStatusReader,
)
from core.infrastructure.release.upgrade_gateway import (
    PLATFORM_MODULE_COMMAND,
    read_role_module_status,
)
from tests.unit.test_platform_module_activator import FakeRestarter

FAILURE = {
    "id": "acme.ledger",
    "version": "1.2.0",
    "status": "failed",
    "load_error": "ImportError: No module named 'acme_ledger_backend'",
}


# -- the role status files ---------------------------------------------------------


def test_a_role_status_round_trips(tmp_path):
    assert write_role_module_status(
        "worker",
        loaded=[{"id": "acme.notes", "version": "0.1.0"}],
        failures=[FAILURE],
        directory=tmp_path,
    )

    statuses = read_role_module_statuses(tmp_path)

    assert statuses == {
        "worker": {
            "loaded": [{"id": "acme.notes", "version": "0.1.0"}],
            "failures": [
                {"id": "acme.ledger", "version": "1.2.0", "load_error": FAILURE["load_error"]}
            ],
        }
    }
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(".")]


def test_a_new_start_replaces_the_previous_status(tmp_path):
    write_role_module_status("app", loaded=[], failures=[FAILURE], directory=tmp_path)
    write_role_module_status(
        "app", loaded=[{"id": "acme.ledger", "version": "1.2.1"}], failures=[], directory=tmp_path
    )

    assert read_role_module_statuses(tmp_path)["app"]["failures"] == []


def test_each_role_owns_one_file_and_ignores_unrelated_files(tmp_path):
    (tmp_path / "unrelated.json").write_text("{}", encoding="utf-8")

    write_role_module_status("control", loaded=[], failures=[], directory=tmp_path)

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "flow-steward-platform-module-status-control.json",
        "unrelated.json",
    ]
    assert read_role_module_statuses(tmp_path) == {"control": {"loaded": [], "failures": []}}


def test_an_unwritable_directory_never_stops_the_role(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")

    assert (
        write_role_module_status("worker", loaded=[], failures=[FAILURE], directory=blocker)
        is False
    )
    assert "could not be written" in caplog.text


def test_an_invalid_role_name_is_a_programming_error(tmp_path):
    with pytest.raises(ValueError):
        write_role_module_status("../app", loaded=[], failures=[], directory=tmp_path)


def test_unreadable_or_foreign_files_are_left_out(tmp_path):
    prefix = "flow-steward-platform-module-status-"
    (tmp_path / f"{prefix}app.json").write_text("not json", encoding="utf-8")
    (tmp_path / f"{prefix}worker.json").write_text(
        json.dumps({"schema_version": 1, "role": "control", "loaded": [], "failures": []}),
        encoding="utf-8",
    )
    (tmp_path / f"{prefix}control.json").write_text(
        json.dumps({"schema_version": 2, "role": "control", "loaded": [], "failures": []}),
        encoding="utf-8",
    )

    assert read_role_module_statuses(tmp_path) == {}
    assert read_role_module_statuses(tmp_path / "absent") == {}


# -- the in-image command ---------------------------------------------------------


def test_load_status_command_prints_the_roles_of_this_container(tmp_path, monkeypatch, capsys):
    from core.infrastructure.config.runtime_env import runtime_env

    monkeypatch.setitem(runtime_env, ROLE_STATUS_DIR_ENV, str(tmp_path))
    write_role_module_status("app", loaded=[], failures=[FAILURE], directory=tmp_path)

    code = manage.main(["load-status"], store=SimpleNamespace())

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["command"] == "load-status"
    assert payload["result"]["roles"]["app"]["failures"][0]["id"] == "acme.ledger"


# -- the gateway invocation ---------------------------------------------------------


def _target(profile="scalable"):
    return SimpleNamespace(deployment_profile=profile, runtime_services=("fs-app",))


def test_gateway_reads_the_status_inside_the_running_container():
    calls = []

    def run_compose(target, arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(
            stdout='{"ok": true, "command": "load-status", "result": {"roles": {}}}'
        )

    result = read_role_module_status(
        _target(),
        "fs-worker",
        timeout_seconds=30,
        run_compose=run_compose,
        environment={"FS_APP_IMAGE": "image"},
    )

    assert result == {"roles": {}}
    arguments, kwargs = calls[0]
    assert arguments == (
        "exec",
        "-T",
        "fs-worker",
        "python",
        "-m",
        PLATFORM_MODULE_COMMAND,
        "load-status",
    )
    assert kwargs["environment"] == {"FS_APP_IMAGE": "image"}
    assert kwargs["separate_stderr"] is True


@pytest.mark.parametrize("stdout", ["", "not json", '{"ok": false}', '{"ok": true}'])
def test_gateway_refuses_an_invalid_status_answer(stdout):
    with pytest.raises(StackGatewayError):
        read_role_module_status(
            _target(),
            "fs-app",
            timeout_seconds=1,
            run_compose=lambda *args, **kwargs: SimpleNamespace(stdout=stdout),
            environment={},
        )


def test_gateway_reads_only_services_that_load_modules():
    with pytest.raises(StackGatewayError):
        read_role_module_status(
            _target(),
            "fs-mcp",
            timeout_seconds=1,
            run_compose=lambda *args, **kwargs: pytest.fail("must not run"),
            environment={},
        )


# -- the reader ---------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _reader(answers, services=("fs-app", "fs-worker", "control-plane"), clock=None):
    clock = clock or Clock()
    calls: list[str] = []

    def read_service(service):
        calls.append(service)
        answer = answers[service]
        if callable(answer):
            return answer()
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer

    reader = RoleModuleStatusReader(
        read_service=read_service,
        services=lambda: services,
        wait_seconds=10,
        interval_seconds=2,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return reader, calls, clock


def _roles(**roles):
    return {
        "roles": {
            role: {"loaded": [], "failures": list(failures)} for role, failures in roles.items()
        }
    }


def test_reader_names_the_role_of_each_failure():
    reader, _, _ = _reader(
        {
            "fs-app": _roles(app=[FAILURE]),
            "fs-worker": _roles(worker=[]),
            "control-plane": _roles(control=[FAILURE]),
        }
    )

    failures = reader.failures(wait=True)

    assert [(item["role"], item["id"], item["version"]) for item in failures] == [
        ("app", "acme.ledger", "1.2.0"),
        ("control", "acme.ledger", "1.2.0"),
    ]
    assert failures[0]["load_error"] == FAILURE["load_error"]


def test_reader_waits_for_a_role_that_has_not_reported_yet():
    reader, calls, clock = _reader(
        {"fs-worker": [_roles(), _roles(), _roles(worker=[FAILURE])]}, services=("fs-worker",)
    )

    failures = reader.failures(wait=True)

    assert [item["role"] for item in failures] == ["worker"]
    assert calls == ["fs-worker"] * 3
    assert clock.sleeps == [2, 2]


def test_reader_stops_waiting_at_its_deadline_and_counts_only_reported_failures(caplog):
    reader, _calls, clock = _reader({"fs-worker": _roles()}, services=("fs-worker",))

    assert reader.failures(wait=True) == ()
    assert clock.now >= 10
    assert "not available" in caplog.text


def test_reader_without_wait_asks_once():
    reader, calls, clock = _reader({"fs-app": _roles()}, services=("fs-app",))

    assert reader.failures(wait=False) == ()
    assert calls == ["fs-app"]
    assert clock.sleeps == []


def test_compact_runtime_reports_every_role_of_its_container():
    reader, _, _ = _reader(
        {"compact-runtime": _roles(app=[], worker=[FAILURE], control=[])},
        services=COMPACT_MODULE_SERVICES,
    )

    assert [item["role"] for item in reader.failures(wait=True)] == ["worker"]


def test_a_service_that_cannot_be_asked_is_logged_and_skipped(caplog):
    def broken():
        raise StackGatewayError(action="platform_modules", detail="Docker exited with code 1.")

    reader, _, _ = _reader(
        {"fs-app": broken, "fs-mcp": _roles(app=[FAILURE])}, services=("fs-app", "fs-mcp")
    )

    assert reader.failures(wait=False) == ()
    assert "could not be read" in caplog.text


# -- the activators -----------------------------------------------------------------


def test_activators_report_the_reader_s_failures(tmp_path):
    reader, _, _ = _reader({"fs-app": _roles(app=[FAILURE])}, services=("fs-app",))
    filesystem = FilesystemModuleActivator(
        tmp_path / "platform-modules", restarter=FakeRestarter(), load_status=reader
    )
    compact = CompactModuleActivator(
        run_command=lambda arguments: {},
        restarter=FakeRestarter(roles=COMPACT_MODULE_SERVICES),
        target_provider=lambda: _target("compact"),
        load_status=reader,
    )

    for activator in (filesystem, compact, LazyModuleActivator(lambda: filesystem)):
        assert [item["id"] for item in activator.module_load_failures(wait=True)] == ["acme.ledger"]


def test_activators_without_a_reader_report_no_failures(tmp_path):
    filesystem = FilesystemModuleActivator(tmp_path / "platform-modules", restarter=FakeRestarter())

    assert filesystem.module_load_failures(wait=True) == ()
    assert NoModuleActivator().module_load_failures(wait=True) == ()


def test_the_cli_composes_activators_with_a_status_reader(monkeypatch, tmp_path):
    import core.infrastructure.platform_modules.loader as loader
    from core.infrastructure.config.runtime_env import runtime_env
    from tests.unit.test_platform_module_compact import _runtime

    root = tmp_path / "runtime" / "platform-modules"
    root.parent.mkdir(parents=True)
    monkeypatch.setattr(loader, "platform_modules_root", lambda: root)
    monkeypatch.setitem(
        runtime_env, "FS_CONFIGURATION_STATE_ROOT", "/run/flow-steward-install-state"
    )

    scalable = _runtime(monkeypatch, "scalable")._platform_module_activator("scalable")
    compact = _runtime(monkeypatch, "compact")._platform_module_activator("compact")

    assert isinstance(scalable.load_status, RoleModuleStatusReader)
    assert isinstance(compact.load_status, RoleModuleStatusReader)
