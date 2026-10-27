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
