"""Run the host startup bridge against a controlled external Docker boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/host/systemd/autostart.sh"
PG = "a" * 64
APP = "b" * 64


@pytest.fixture
def engine(tmp_path: Path):
    """Only Docker is substituted; execute the actual host shell program."""
    state = tmp_path / "engine.json"
    events = tmp_path / "events.jsonl"
    bins = tmp_path / "bin"
    bins.mkdir()
    docker = bins / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['TEST_EVENTS'], 'a') as out:\n"
        "    out.write(json.dumps(args) + '\\n')\n"
        "state = json.loads(Path(os.environ['TEST_ENGINE']).read_text())\n"
        "if args[0] == 'inspect':\n"
        "    item = state.get(args[-1])\n"
        "    if item is None: sys.exit(1)\n"
        "    if 'Health' in args[2]: print(item.get('health', 'none'))\n"
        "    else: print(item['project'] + '|' + item['service'])\n"
        "elif args[0] in ('start', 'stop'): pass\n"
        "else: sys.exit(91)\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    manifest = tmp_path / "containers"
    # Deliberately application-first: boot must still start healthy infra first.
    manifest.write_text(f"fs-test\n{APP} compact-runtime\n{PG} pg-fs\n")
    state.write_text(
        json.dumps(
            {
                PG: {"project": "fs-test", "service": "pg-fs", "health": "healthy"},
                APP: {"project": "fs-test", "service": "compact-runtime"},
            }
        )
    )
    env = {
        **os.environ,
        "PATH": f"{bins}:{os.environ['PATH']}",
        "TEST_ENGINE": str(state),
        "TEST_EVENTS": str(events),
    }

    def run(action: str = "start"):
        result = subprocess.run(
            ["bash", str(SCRIPT), action, str(manifest)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        calls = (
            [json.loads(line) for line in events.read_text().splitlines()]
            if events.exists()
            else []
        )
        return result, calls

    return state, manifest, run


def test_boot_starts_infrastructure_before_runtime_without_build_or_pull(engine):
    _, _, run = engine
    result, calls = run()
    assert result.returncode == 0, result.stderr
    starts = [call for call in calls if call[0] == "start"]
    assert starts == [["start", PG], ["start", APP]]
    health_index = next(i for i, call in enumerate(calls) if "Health" in " ".join(call))
    assert calls.index(starts[0]) < health_index < calls.index(starts[1])
    assert {call[0] for call in calls} == {"inspect", "start"}


@pytest.mark.parametrize("failure", ["missing", "foreign-project", "different-service"])
def test_invalid_ownership_refuses_before_any_container_is_started(engine, failure):
    state, _, run = engine
    items = json.loads(state.read_text())
    if failure == "missing":
        del items[APP]
    elif failure == "foreign-project":
        items[APP]["project"] = "another-installation"
    else:
        items[APP]["service"] = "fs-audit"
    state.write_text(json.dumps(items))
    result, calls = run()
    assert result.returncode == 4
    assert not any(call[0] == "start" for call in calls)
    assert "install.sh" in result.stderr


def test_unhealthy_database_does_not_start_runtime(engine):
    state, _, run = engine
    items = json.loads(state.read_text())
    items[PG]["health"] = "unhealthy"
    state.write_text(json.dumps(items))
    result, calls = run()
    assert result.returncode == 4
    assert ["start", APP] not in calls


def test_stop_drains_runtime_before_stopping_its_database(engine):
    _, _, run = engine
    result, calls = run("stop")
    assert result.returncode == 0, result.stderr
    assert [call for call in calls if call[0] == "stop"] == [
        ["stop", "--time", "190", APP],
        ["stop", "--time", "190", PG],
    ]


def test_malformed_manifest_is_rejected_without_docker_effects(engine):
    _, manifest, run = engine
    manifest.write_text(f"fs-test\n{APP} compact-runtime unexpected\n")
    result, calls = run()
    assert result.returncode == 4
    assert calls == []


@pytest.mark.parametrize("ownership", ["owned", "whitespace", "foreign", "symlink", "drop-in"])
def test_explicit_legacy_retirement_never_runs_the_units_container_stop(tmp_path, ownership):
    """Retiring the old host owner must preserve running Docker-owned containers."""
    system_root = tmp_path / "system"
    directory = system_root / "etc/flow-steward/autostart/fs-test"
    directory.mkdir(parents=True)
    unit_file = system_root / "etc/systemd/system/flow-steward-fs-test.service"
    unit_file.parent.mkdir(parents=True)
    original = (
        "# Flow Steward owned auto-start unit: fs-test\n[Service]\n"
        "ExecStart=/old/runner.sh start /old/containers\n"
        "ExecStop=/old/runner.sh stop /old/containers\n"
        "ExecStopPost=/old/runner.sh stop /old/containers\n"
    )
    if ownership == "whitespace":
        original = original.replace("ExecStop=", "  ExecStop =").replace(
            "ExecStopPost=", "  ExecStopPost ="
        )
    unit_file.write_text(original)
    (directory / "checkout").write_text(
        "/another-checkout" if ownership == "foreign" else str(ROOT)
    )
    if ownership == "symlink":
        target = tmp_path / "external-unit"
        unit_file.rename(target)
        unit_file.symlink_to(target)
    if ownership == "drop-in":
        drop_ins = unit_file.parent / f"{unit_file.name}.d"
        drop_ins.mkdir()
        (drop_ins / "custom.conf").write_text("[Service]\nExecStopPost=/custom/container-stop\n")
    for name in ("runner.sh", "containers"):
        (directory / name).write_text("legacy")
    state = tmp_path / "state"
    state.mkdir()
    (state / "compose.env").write_text("FS_COMPOSE_PROJECT_NAME=fs-test\n")
    bins = tmp_path / "bin"
    bins.mkdir()
    events = tmp_path / "events"
    systemctl = bins / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = disable ] || [ "$1" = stop ]; then\n'
        '  if grep -Eq "^[[:space:]]*ExecStop(Post)?[[:space:]]*=" "$TEST_UNIT"; then exit 90; fi\n'
        "fi\n"
        'printf "%s\\n" "$*" >> "$TEST_EVENTS"\n'
    )
    systemctl.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; retire_unit "$2" "$3" "$4"',
            "retire-test",
            str(SCRIPT),
            str(ROOT),
            str(state),
            str(system_root),
        ],
        env={
            **os.environ,
            "PATH": f"{bins}:{os.environ['PATH']}",
            "TEST_UNIT": str(unit_file),
            "TEST_EVENTS": str(events),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if ownership not in ("owned", "whitespace"):
        assert result.returncode == 4
        assert unit_file.read_text() == original
        assert not events.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert not unit_file.exists()
        assert not directory.exists()
        assert events.read_text().splitlines() == [
            "daemon-reload",
            "disable flow-steward-fs-test.service",
            "stop flow-steward-fs-test.service",
            "daemon-reload",
        ]


@pytest.mark.parametrize("installation", ["absent", "owned", "drop-in"])
def test_legacy_check_is_read_only_and_reports_required_explicit_retirement(tmp_path, installation):
    system_root = tmp_path / "system"
    directory = system_root / "etc/flow-steward/autostart/fs-test"
    directory.mkdir(parents=True)
    unit_file = system_root / "etc/systemd/system/flow-steward-fs-test.service"
    unit_file.parent.mkdir(parents=True)
    original = "# Flow Steward owned auto-start unit: fs-test\n[Service]\nExecStopPost=/old/stop\n"
    if installation != "absent":
        unit_file.write_text(original)
        (directory / "checkout").write_text(str(ROOT))
    if installation == "drop-in":
        drop_ins = unit_file.parent / f"{unit_file.name}.d"
        drop_ins.mkdir()
        (drop_ins / "custom.conf").write_text("[Service]\nExecStopPost=/custom/container-stop\n")
    state = tmp_path / "state"
    state.mkdir()
    (state / "compose.env").write_text("FS_COMPOSE_PROJECT_NAME=fs-test\n")
    bins = tmp_path / "bin"
    bins.mkdir()
    events = tmp_path / "events"
    # A preflight must not invoke any host service manager or privilege prompt.
    for command in ("sudo", "systemctl", "docker"):
        program = bins / command
        program.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$TEST_EVENTS"\nexit 91\n')
        program.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; check_legacy_unit "$2" "$3" "$4"',
            "check-test",
            str(SCRIPT),
            str(ROOT),
            str(state),
            str(system_root),
        ],
        env={**os.environ, "PATH": f"{bins}:{os.environ['PATH']}", "TEST_EVENTS": str(events)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert not events.exists()
    if installation != "absent":
        assert result.returncode == 4
        assert unit_file.read_text() == original
        if installation == "drop-in":
            assert "manual" in result.stderr.lower() and "drop-in" in result.stderr.lower()
        else:
            assert "sudo" in result.stderr and " retire " in result.stderr
    else:
        assert result.returncode == 0, result.stderr


def test_fresh_install_legacy_check_does_not_create_its_state_directory(tmp_path):
    state = tmp_path / "not-installed"
    result = subprocess.run(
        ["bash", str(SCRIPT), "check", str(ROOT), str(state)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not state.exists()
