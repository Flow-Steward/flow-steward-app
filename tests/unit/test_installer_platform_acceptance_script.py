from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "core/entrypoints/cli/scripts/installer_platform_acceptance.sh"
REVISION = "0123456789abcdef0123456789abcdef01234567"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_host(
    tmp_path: Path,
    *,
    system: str,
    revision: str = REVISION,
) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    _write_executable(
        fake_bin / "uname",
        f"""#!/bin/sh
case "$1" in
  -s) printf '%s\\n' {system!r} ;;
  -m) printf '%s\\n' x86_64 ;;
  *) printf '%s\\n' {system!r} ;;
esac
""",
    )
    _write_executable(
        fake_bin / "git",
        f"""#!/bin/sh
printf '%s\\n' {revision!r}
""",
    )
    _write_executable(
        fake_bin / "docker",
        f"""#!/bin/sh
printf '%s\\n' "$*" >> {str(command_log)!r}
if [ "$1 $2" = "compose version" ]; then
  printf '%s\\n' 'v2.40.3'
elif [ "$1" = "version" ]; then
  printf '%s\\n' '28.5.2/29.6.1'
else
  exit 64
fi
""",
    )
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment.get('PATH', '')}"
    return environment, command_log


def _fake_pytest_module(tmp_path: Path, source: str) -> Path:
    module_root = tmp_path / "modules"
    package = module_root / "pytest"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(source, encoding="utf-8")
    return module_root


def _fake_pytest_run(
    manifest: dict[str, object],
    *,
    exit_code: int = 0,
    invocation_log: Path | None = None,
    manifest_count: int | None = None,
    skipped_index: int | None = None,
) -> str:
    log_statement = ""
    manifest_limit = "len(nodes)" if manifest_count is None else str(manifest_count)
    if invocation_log is not None:
        log_statement = (
            f"Path({str(invocation_log)!r}).write_text("
            '" ".join(sys.argv[1:]) + "\\n", encoding="utf-8")'
        )
    return f"""import sys
from pathlib import Path
from xml.etree import ElementTree

{log_statement}
base = next(Path(value.removeprefix("--basetemp=")) for value in sys.argv if value.startswith("--basetemp="))
junit = next(Path(value.removeprefix("--junitxml=")) for value in sys.argv if value.startswith("--junitxml="))
nodes = [value for value in sys.argv if value.startswith("tests/")]
for index in range({manifest_limit}):
    resource_manifest = base / f"case-{{index}}/disposable-installer-resources.json"
    resource_manifest.parent.mkdir(parents=True)
    resource_manifest.write_text({json.dumps(manifest)!r}, encoding="utf-8")
suite = ElementTree.Element("testsuite")
for index, node in enumerate(nodes):
    module, name = node.split("::", 1)
    case = ElementTree.SubElement(
        suite,
        "testcase",
        classname=module.removesuffix(".py").replace("/", "."),
        name=name,
    )
    if index == {skipped_index!r}:
        ElementTree.SubElement(case, "skipped")
    elif {exit_code} and index == 0:
        ElementTree.SubElement(case, "failure")
ElementTree.ElementTree(suite).write(junit, encoding="unicode")
raise SystemExit({exit_code})
"""


def test_require_linux_rejects_darwin_without_running_or_cleaning(tmp_path: Path) -> None:
    assert SCRIPT.is_file(), f"missing acceptance runner: {SCRIPT}"
    environment, command_log = _fake_host(tmp_path, system="Darwin")
    pytest_marker = tmp_path / "pytest-called"
    module_root = _fake_pytest_module(
        tmp_path,
        f"from pathlib import Path\nPath({str(pytest_marker)!r}).touch()\n",
    )
    environment["PYTHONPATH"] = str(module_root)
    output_dir = tmp_path / "evidence"

    result = subprocess.run(
        [str(SCRIPT), "--require-linux", "--scenario", "all", "--output-dir", str(output_dir)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 4
    evidence = json.loads((output_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["platform"] == {"os": "darwin", "architecture": "amd64"}
    assert evidence["revision"] == REVISION
    assert evidence["scenario"] == "all"
    assert evidence["result"]["status"] == "blocked"
    assert evidence["result"]["cleanup_verified"] is False
    assert not pytest_marker.exists()
    commands = command_log.read_text(encoding="utf-8")
    assert "prune" not in commands
    assert "down -v" not in commands


def test_linux_runner_records_exact_scenario_and_cleanup_evidence(tmp_path: Path) -> None:
    assert SCRIPT.is_file(), f"missing acceptance runner: {SCRIPT}"
    environment, command_log = _fake_host(tmp_path, system="Linux")
    pytest_log = tmp_path / "pytest-command.log"
    module_root = _fake_pytest_module(
        tmp_path,
        _fake_pytest_run(
            {
                "source_revision": REVISION,
                "project_name": "fs-inst-contract",
                "external_s3": {"access_key_id": "must-not-be-published"},
                "active_resources": {
                    "containers": [],
                    "images": [],
                    "networks": [],
                    "paths": [],
                    "volumes": [],
                },
            },
            invocation_log=pytest_log,
        ),
    )
    environment["PYTHONPATH"] = str(module_root)
    output_dir = tmp_path / "evidence"

    result = subprocess.run(
        [
            str(SCRIPT),
            "--require-linux",
            "--scenario",
            "production",
            "--output-dir",
            str(output_dir),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads((output_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["platform"] == {"os": "linux", "architecture": "amd64"}
    assert evidence["docker"] == {"engine": "28.5.2/29.6.1", "compose": "v2.40.3"}
    assert evidence["revision"] == REVISION
    assert evidence["scenario"] == "production"
    assert evidence["result"] == {
        "status": "passed",
        "exit_code": 0,
        "manifest_count": 6,
        "cleanup_verified": True,
        "expected_scenario_count": 6,
        "scenario_count": 6,
        "scenarios_verified": True,
    }
    assert len(evidence["scenario_results"]) == 6
    assert {item["outcome"] for item in evidence["scenario_results"]} == {"passed"}
    assert sorted(path.name for path in output_dir.iterdir()) == ["evidence.json"]
    assert "must-not-be-published" not in json.dumps(evidence)
    assert len(evidence["cleanup_proofs"]) == 6
    assert all(
        proof
        == {
            "project_name": "fs-inst-contract",
            "source_revision": REVISION,
            "active_resource_counts": {
                "containers": 0,
                "images": 0,
                "networks": 0,
                "paths": 0,
                "volumes": 0,
            },
        }
        for proof in evidence["cleanup_proofs"]
    )
    invocation = pytest_log.read_text(encoding="utf-8")
    assert "production-textual-bundled" in invocation
    assert "production-textual-external_s3" in invocation
    assert "development-textual-bundled" not in invocation
    assert "--basetemp=" in invocation
    basetemp = next(
        Path(value.removeprefix("--basetemp="))
        for value in invocation.split()
        if value.startswith("--basetemp=")
    )
    assert not basetemp.exists()
    commands = command_log.read_text(encoding="utf-8")
    assert "prune" not in commands
    assert "down -v" not in commands


def test_linux_runner_rejects_unknown_revision_before_pytest(tmp_path: Path) -> None:
    environment, _command_log = _fake_host(tmp_path, system="Linux", revision="unknown")
    pytest_marker = tmp_path / "pytest-called"
    module_root = _fake_pytest_module(
        tmp_path,
        f"from pathlib import Path\nPath({str(pytest_marker)!r}).touch()\n",
    )
    environment["PYTHONPATH"] = str(module_root)
    output_dir = tmp_path / "evidence"

    result = subprocess.run(
        [str(SCRIPT), "--require-linux", "--scenario", "all", "--output-dir", str(output_dir)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 5
    evidence = json.loads((output_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["revision"] == "unknown"
    assert evidence["result"]["status"] == "blocked"
    assert "exact Git revision" in evidence["blocked_reason"]
    assert not pytest_marker.exists()
    assert sorted(path.name for path in output_dir.iterdir()) == ["evidence.json"]


def test_failed_pytest_publishes_only_sanitized_incomplete_cleanup_proof(tmp_path: Path) -> None:
    environment, _command_log = _fake_host(tmp_path, system="Linux")
    module_root = _fake_pytest_module(
        tmp_path,
        _fake_pytest_run(
            {
                "source_revision": REVISION,
                "project_name": "fs-inst-failed",
                "external_s3": {
                    "access_key_id": "must-not-leak",
                    "credentials_path": "/tmp/external-s3-credentials.json",
                },
                "active_resources": {
                    "containers": ["fs-inst-failed-app-1"],
                    "images": [],
                    "networks": [],
                    "paths": [],
                    "volumes": [],
                },
            },
            exit_code=1,
        ),
    )
    environment["PYTHONPATH"] = str(module_root)
    output_dir = tmp_path / "evidence"

    result = subprocess.run(
        [
            str(SCRIPT),
            "--require-linux",
            "--scenario",
            "production",
            "--output-dir",
            str(output_dir),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    evidence = json.loads((output_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["result"] == {
        "status": "failed",
        "exit_code": 1,
        "manifest_count": 6,
        "cleanup_verified": False,
        "expected_scenario_count": 6,
        "scenario_count": 6,
        "scenarios_verified": False,
    }
    assert evidence["cleanup_proofs"][0]["active_resource_counts"]["containers"] == 1
    assert "must-not-leak" not in json.dumps(evidence)
    assert "external-s3-credentials" not in json.dumps(evidence)
    assert sorted(path.name for path in output_dir.iterdir()) == ["evidence.json"]


def test_success_exit_rejects_skipped_scenario_evidence(tmp_path: Path) -> None:
    environment, _command_log = _fake_host(tmp_path, system="Linux")
    module_root = _fake_pytest_module(
        tmp_path,
        _fake_pytest_run(
            {
                "source_revision": REVISION,
                "project_name": "fs-inst-skipped",
                "active_resources": {
                    "containers": [],
                    "images": [],
                    "networks": [],
                    "paths": [],
                    "volumes": [],
                },
            },
            skipped_index=2,
        ),
    )
    environment["PYTHONPATH"] = str(module_root)
    output_dir = tmp_path / "evidence"

    result = subprocess.run(
        [
            str(SCRIPT),
            "--require-linux",
            "--scenario",
            "production",
            "--output-dir",
            str(output_dir),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    evidence = json.loads((output_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["result"]["status"] == "failed"
    assert evidence["result"]["scenarios_verified"] is False
    assert [item["outcome"] for item in evidence["scenario_results"]].count("skipped") == 1


def test_cleanup_proof_rejects_non_list_active_resources(tmp_path: Path) -> None:
    environment, _command_log = _fake_host(tmp_path, system="Linux")
    module_root = _fake_pytest_module(
        tmp_path,
        _fake_pytest_run(
            {
                "source_revision": REVISION,
                "project_name": "fs-inst-invalid-cleanup",
                "active_resources": {
                    "containers": "",
                    "images": [],
                    "networks": [],
                    "paths": [],
                    "volumes": [],
                },
            }
        ),
    )
    environment["PYTHONPATH"] = str(module_root)
    output_dir = tmp_path / "evidence"

    result = subprocess.run(
        [
            str(SCRIPT),
            "--require-linux",
            "--scenario",
            "production",
            "--output-dir",
            str(output_dir),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    evidence = json.loads((output_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["result"]["cleanup_verified"] is False
    assert evidence["cleanup_proofs"][0]["active_resource_counts"]["containers"] == -1


def test_success_exit_rejects_partial_cleanup_manifest_set(tmp_path: Path) -> None:
    environment, _command_log = _fake_host(tmp_path, system="Linux")
    module_root = _fake_pytest_module(
        tmp_path,
        _fake_pytest_run(
            {
                "source_revision": REVISION,
                "project_name": "fs-inst-partial-cleanup",
                "active_resources": {
                    "containers": [],
                    "images": [],
                    "networks": [],
                    "paths": [],
                    "volumes": [],
                },
            },
            manifest_count=1,
        ),
    )
    environment["PYTHONPATH"] = str(module_root)
    output_dir = tmp_path / "evidence"

    result = subprocess.run(
        [
            str(SCRIPT),
            "--require-linux",
            "--scenario",
            "production",
            "--output-dir",
            str(output_dir),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    evidence = json.loads((output_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["result"]["manifest_count"] == 1
    assert evidence["result"]["cleanup_verified"] is False
