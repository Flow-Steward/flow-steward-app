"""doctor inside the Compact container, where there is no Docker (the doctor task, D4).

A platform such as Railway runs the Compact image itself: there is no ./install.sh, no
journal, and the image is built without the Docker CLI. Every check and repair reads or
changes the installation through Docker, so doctor names each one as unavailable, and
why -- never "not installed, run ./install.sh", and never "no problems found"."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application.doctor.checks import p1_checks
from core.application.doctor.hints import hint_checks
from core.application.doctor.repairs import repair_checks
from core.entrypoints.cli import doctor
from core.infrastructure.installer import host_diagnostics

_COMPACT_IDS = [
    check.check_id
    for check in (*p1_checks(), *hint_checks(), *repair_checks())
    if "compact" in check.profiles
]
_COMPACT_REPAIRS = [
    check.check_id
    for check in (*p1_checks(), *hint_checks(), *repair_checks())
    if "compact" in check.profiles and check.repair is not None
]


@pytest.fixture
def in_the_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(doctor, "_ROOT", tmp_path)  # no journal: nothing was installed here
    monkeypatch.setattr(doctor, "runtime_env", {"FS_DEPLOYMENT_PROFILE": "compact"})
    monkeypatch.setattr(host_diagnostics, "docker_cli_available", lambda: False)
    return tmp_path


def test_every_check_is_named_as_not_checked_and_why(
    in_the_container: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = doctor.main([])

    out = capsys.readouterr().out
    assert code == doctor._EXIT_NOT_CHECKED
    assert "Docker is not available here" in out
    for check_id in _COMPACT_IDS:
        assert f"NOT CHECKED {check_id}:" in out
    assert "Nothing was checked." in out
    assert "No problems found" not in out
    assert "Run ./install.sh" not in out
    assert not (in_the_container / ".flow-steward").exists()  # it wrote nothing


def test_every_repair_is_named_as_unavailable_and_nothing_runs(
    in_the_container: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = doctor.main(["--fix", "--confirm"])

    out = capsys.readouterr().out
    assert code == doctor._EXIT_NOT_CHECKED
    for check_id in _COMPACT_REPAIRS:
        assert f"UNAVAILABLE {check_id}:" in out
    assert "Nothing was changed." in out


def test_the_named_repairs_only_and_an_unknown_one_is_a_usage_error(
    in_the_container: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert doctor.main(["--fix", "stack_stopped"]) == doctor._EXIT_NOT_CHECKED
    out = capsys.readouterr().out
    assert "UNAVAILABLE stack_stopped:" in out
    assert "container_names" not in out

    assert doctor.main(["--fix", "no_such_check"]) == doctor._EXIT_USAGE
    assert "Unknown check id: no_such_check" in capsys.readouterr().err


def test_the_json_report_says_the_same(
    in_the_container: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doctor.main(["--json"])

    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "no_docker"
    assert {item["check_id"] for item in report["installation"]} == set(_COMPACT_IDS)
    assert {item["status"] for item in report["installation"]} == {"not_checked"}
    assert report["exit_code"] == doctor._EXIT_NOT_CHECKED


def test_where_docker_is_there_doctor_reads_the_installation_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The green direction: the management runtime of ./install.sh has the Docker CLI."""
    monkeypatch.setattr(doctor, "_ROOT", tmp_path)
    monkeypatch.setattr(doctor, "runtime_env", {"FS_DEPLOYMENT_PROFILE": "compact"})
    monkeypatch.setattr(host_diagnostics, "docker_cli_available", lambda: True)

    code = doctor.main([])

    assert code == doctor._EXIT_FINDINGS
    assert "Flow Steward is not installed here" in capsys.readouterr().err


@pytest.mark.parametrize("present", [False, True])
def test_infrastructure_docker_availability_only_reads_executable_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, present: bool
) -> None:
    bin_root = tmp_path / "bin"
    bin_root.mkdir()
    if present:
        executable = bin_root / "docker"
        executable.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_root))

    def unexpected_probe(*_args):
        pytest.fail("availability detection executed a Docker probe")

    monkeypatch.setattr(host_diagnostics, "_run_docker", unexpected_probe)

    assert host_diagnostics.docker_cli_available() is present
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bin"]


@pytest.mark.parametrize("profile", ["", "scalable"])
def test_absent_docker_outside_compact_does_not_claim_embedded_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: str,
) -> None:
    monkeypatch.setattr(doctor, "_ROOT", tmp_path)
    monkeypatch.setattr(doctor, "runtime_env", {"FS_DEPLOYMENT_PROFILE": profile})
    monkeypatch.setattr(host_diagnostics, "docker_cli_available", lambda: False)

    assert doctor.main([]) == doctor._EXIT_FINDINGS
    assert "Flow Steward is not installed here" in capsys.readouterr().err
    assert not (tmp_path / ".flow-steward").exists()


# -- the provider console: `platform_runtime doctor` ------------------------------------------


def _console(report: dict[str, object]):
    """The configuration doctor of the Compact platform runtime, as click runs it."""

    class _App:
        def main(self, *, args, **_kwargs):
            assert args == ["doctor", "--json"]
            print(json.dumps(report))
            return 0

    class _Click:
        click_app = _App()

    return _Click()


_CLEAN = {"ok": True, "data": {"findings": [{"check_id": "public_url", "status": "passed"}]}}
_PROBLEM = {
    "ok": False,
    "data": {
        "findings": [
            {
                "check_id": "public_url",
                "status": "failed",
                "title": "Public URL",
                "detail": "FS_PUBLIC_BASE_URL is not set.",
                "remediation": "Set FS_PUBLIC_BASE_URL on the service.",
            }
        ]
    },
}


def _platform_doctor(monkeypatch: pytest.MonkeyPatch, report, *argv: str) -> int:
    from core.entrypoints.cli import platform_runtime

    env: dict[str, str] = {}
    monkeypatch.setattr(doctor, "runtime_env", env)
    monkeypatch.setattr("core.infrastructure.config.runtime_env.runtime_env", env)
    monkeypatch.setattr(host_diagnostics, "docker_cli_available", lambda: False)
    return platform_runtime._doctor(
        _console(report), platform_runtime._parser().parse_args(["doctor", *argv])
    )


def test_the_console_doctor_shows_the_configuration_and_names_the_rest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _platform_doctor(monkeypatch, _PROBLEM)

    out = capsys.readouterr().out
    assert code == doctor._EXIT_FINDINGS  # a configuration problem is a problem
    assert "FAILED Public URL: FS_PUBLIC_BASE_URL is not set." in out
    assert "-> Set FS_PUBLIC_BASE_URL on the service." in out
    assert "NOT CHECKED stack_stopped:" in out
    assert "1 problem: public_url." in out


def test_a_clean_configuration_still_says_the_installation_was_not_checked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _platform_doctor(monkeypatch, _CLEAN, "--json") == doctor._EXIT_NOT_CHECKED

    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "no_docker"
    assert report["configuration"] == _CLEAN
    assert report["problems"] == []


@pytest.mark.parametrize("exit_code", (0, 4))
def test_configuration_section_returns_only_canonical_report_and_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exit_code: int
) -> None:
    from types import SimpleNamespace

    import click

    from core.entrypoints.cli import platform_runtime

    report = _CLEAN if exit_code == 0 else _PROBLEM

    @click.command(name="doctor")
    @click.option("--json", "as_json", is_flag=True)
    def configuration_doctor(as_json: bool) -> None:
        assert as_json
        click.echo(json.dumps(report))
        raise click.exceptions.Exit(exit_code)

    # Keep real Click exit handling; only replace installation diagnosis, whose
    # Docker probes must never run in the launcher's configuration section.
    @click.group()
    def configuration_cli() -> None:
        pass

    configuration_cli.add_command(configuration_doctor)
    environment = {"FS_DOCTOR_SECTION": "configuration"}
    monkeypatch.setattr("core.infrastructure.config.runtime_env.runtime_env", environment)
    monkeypatch.setattr(host_diagnostics, "docker_cli_available", lambda: True)

    def unexpected_installation_doctor(_argv):
        pytest.fail("configuration section invoked installation diagnosis")

    monkeypatch.setattr(doctor, "main", unexpected_installation_doctor)
    monkeypatch.setattr(doctor, "without_docker", unexpected_installation_doctor)

    actual = platform_runtime._doctor(
        SimpleNamespace(click_app=configuration_cli),
        platform_runtime._parser().parse_args(["doctor", "--json"]),
    )

    assert actual == exit_code
    assert capsys.readouterr().out == json.dumps(report) + "\n"
    assert "FS_DOCTOR_CONFIGURATION_REPORT" not in environment


def test_the_console_names_the_repairs_it_cannot_make(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _platform_doctor(monkeypatch, _CLEAN, "--fix", "container_names") == (
        doctor._EXIT_NOT_CHECKED
    )

    out = capsys.readouterr().out
    assert "UNAVAILABLE container_names:" in out
    assert "Nothing was changed." in out


def test_management_doctor_with_docker_runs_full_installation_diagnosis(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from core.entrypoints.cli import platform_runtime

    environment: dict[str, str] = {}
    monkeypatch.setattr("core.infrastructure.config.runtime_env.runtime_env", environment)
    monkeypatch.setattr(host_diagnostics, "docker_cli_available", lambda: True)

    def installation_diagnosis(argv):
        assert argv == ["--json"]
        print('{"mode":"installation"}')
        return 4

    monkeypatch.setattr(doctor, "main", installation_diagnosis)
    code = platform_runtime._doctor(
        _console(_CLEAN), platform_runtime._parser().parse_args(["doctor", "--json"])
    )

    assert code == 4
    assert json.loads(capsys.readouterr().out) == {"mode": "installation"}
    assert json.loads(environment["FS_DOCTOR_CONFIGURATION_REPORT"]) == _CLEAN


def test_ids_and_fix_are_refused_for_every_other_platform_operation() -> None:
    from core.entrypoints.cli import platform_runtime

    with pytest.raises(SystemExit):
        platform_runtime.main(["backup-list", "--fix"])
