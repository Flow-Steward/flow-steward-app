"""Recording and carrying the per-module test impact map (ADR 0068)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.application.tooling.quality.impact_map import ImpactMap, RecordedRun, read_recorded_run
from core.application.tooling.quality.impact_plan_filter import planned_partition
from core.application.tooling.quality.impact_vitest_result import failed_test_files

ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = Path(os.environ.get("PYTEST_SOURCE_REPO_ROOT", str(ROOT)))


def _run(modules: dict[str, frozenset[str]], shared: frozenset[str] = frozenset()) -> RecordedRun:
    return RecordedRun(complete=True, exitstatus=0, failed=(), modules=modules, shared=shared)


def test_map_round_trips_and_answers_which_tests_used_a_file(tmp_path: Path) -> None:
    impact = ImpactMap(
        commit="c" * 40,
        modules={
            "tests/unit/test_a.py": frozenset({"core/a.py", "docs"}),
            "tests/unit/test_b.py": frozenset({"core/b.py"}),
        },
        shared=frozenset({"VERSION"}),
    )
    impact.dump(tmp_path / "map.json")
    loaded = ImpactMap.load(tmp_path / "map.json")

    assert loaded == impact
    assert loaded.tests_using("core/a.py") == {"tests/unit/test_a.py"}
    # A listed directory stands for every file directly inside it.
    assert loaded.tests_using("docs/new.md") == {"tests/unit/test_a.py"}
    assert loaded.is_shared("VERSION") and not loaded.used("core/c.py")


def test_a_partial_run_replaces_only_the_modules_it_ran() -> None:
    impact = ImpactMap(
        modules={
            "tests/unit/test_a.py": frozenset({"core/a.py"}),
            "tests/unit/test_b.py": frozenset({"core/b.py"}),
        },
        shared=frozenset({"VERSION"}),
    )
    run = _run({"tests/unit/test_a.py": frozenset({"core/c.py"})}, frozenset({"pyproject.toml"}))

    partial = impact.updated(run, commit="d" * 40, full=False)
    full = impact.updated(run, commit="d" * 40, full=True)

    assert partial.modules["tests/unit/test_a.py"] == {"core/c.py"}
    assert partial.modules["tests/unit/test_b.py"] == {"core/b.py"}
    assert partial.shared == {"VERSION", "pyproject.toml"}
    assert set(full.modules) == {"tests/unit/test_a.py"} and full.shared == {"pyproject.toml"}


def test_an_unreadable_map_is_no_map(tmp_path: Path) -> None:
    (tmp_path / "map.json").write_text('{"schema": 1, "files": []}', encoding="utf-8")
    assert ImpactMap.load(tmp_path / "map.json") is None
    assert ImpactMap.load(tmp_path / "missing.json") is None


@pytest.mark.parametrize("workers", [0, 2])
def test_recorder_maps_each_module_to_the_files_it_used(tmp_path: Path, workers: int) -> None:
    project = tmp_path / "project"
    (project / "pkg").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "pkg/__init__.py").write_text("")
    (project / "pkg/used.py").write_text("def value():\n    return 1\n")
    (project / "pkg/unused.py").write_text("def value():\n    return 2\n")
    (project / "data.txt").write_text("data\n")
    (project / "tests/conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture(scope='session')\ndef shared_data():\n"
        "    return open('data.txt').read()\n"
    )
    (project / "tests/test_used.py").write_text(
        "from pkg import unused\nfrom pkg.used import value\n\n\n"
        "def test_value(shared_data):\n    assert value() == 1\n"
    )
    (project / "tests/test_failing.py").write_text("def test_fails():\n    assert False\n")
    output = tmp_path / "impact"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COV_", "COVERAGE_"))
    }
    environment["PYTHONPATH"] = f"{project}{os.pathsep}{SOURCE_ROOT}"
    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "core.application.tooling.quality.impact_recorder",
            "--fs-test-impact-output",
            str(output),
            "--fs-test-impact-map",
            *(("-n", str(workers)) if workers else ("-p", "no:xdist")),
            "tests",
        ),
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr

    run = read_recorded_run(output)

    assert run.complete and run.usable_failures
    assert run.failed == ("tests/test_failing.py",)
    assert "pkg/used.py" in run.modules["tests/test_used.py"]
    # Imported but never called: import-time code is not a test's use.
    assert "pkg/unused.py" not in run.modules["tests/test_used.py"]
    # The session fixture ran before the module's own tests: shared by all.
    assert "data.txt" in run.shared
    assert "data.txt" not in run.modules["tests/test_used.py"]


def test_a_run_without_its_result_is_incomplete(tmp_path: Path) -> None:
    (tmp_path / "map-gw0.json").write_text(
        json.dumps({"complete": True, "modules": [], "contexts": {}}), encoding="utf-8"
    )
    assert not read_recorded_run(tmp_path).complete
    assert not read_recorded_run(tmp_path / "missing").complete


def test_plan_filter_keeps_the_planned_modules_of_one_partition() -> None:
    plan = {
        "full": False,
        "modules": [
            "tests/unit/a/test_x.py",
            "tests/unit/stress/test_y.py",
            "tests/unit/test_z.py",
        ],
    }

    assert planned_partition(plan, ["tests/unit", "--ignore=tests/unit/stress"]) == [
        "tests/unit/a/test_x.py",
        "tests/unit/test_z.py",
    ]
    assert planned_partition(plan, ["tests/unit/test_z.py"]) == ["tests/unit/test_z.py"]
    assert planned_partition({"full": True, "modules": []}, ["tests/unit"]) is None


def test_vitest_failures_are_named_relative_to_the_workspace(tmp_path: Path) -> None:
    report = {
        "testResults": [
            {"name": str(tmp_path / "src/a.test.tsx"), "status": "failed"},
            {"name": str(tmp_path / "src/b.test.tsx"), "status": "passed"},
        ]
    }

    assert failed_test_files(report, tmp_path) == ["src/a.test.tsx"]
    assert (
        failed_test_files(
            {"testResults": [{"name": "/elsewhere/x.test.tsx", "status": "failed"}]}, tmp_path
        )
        is None
    )
    assert failed_test_files({}, tmp_path) is None


def test_without_a_map_request_the_run_reports_failures_only(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "tests/test_failing.py").write_text("def test_fails():\n    assert False\n")
    output = tmp_path / "impact"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COV_", "COVERAGE_"))
    }
    environment["PYTHONPATH"] = f"{project}{os.pathsep}{SOURCE_ROOT}"
    subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            "-p",
            "core.application.tooling.quality.impact_recorder",
            "--fs-test-impact-output",
            str(output),
            "tests",
        ),
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    run = read_recorded_run(output)

    assert run.reported and run.usable_failures and not run.complete
    assert run.failed == ("tests/test_failing.py",) and not run.modules
