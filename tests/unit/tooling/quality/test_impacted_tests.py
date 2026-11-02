from __future__ import annotations

import subprocess
from pathlib import Path

from core.application.tooling.quality.impacted_tests import changed_files, select_unit_tests


def _write(root: Path, relative: str, text: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(root: Path) -> Path:
    _write(root, "core/__init__.py")
    _write(root, "core/store.py", "VALUE = 1\n")
    _write(root, "core/service.py", "from core.store import VALUE\n")
    _write(root, "core/api.py", "from core import service\n")
    _write(root, "core/unrelated.py", "OTHER = 1\n")
    _write(root, "tests/unit/test_store.py", "from core.store import VALUE\n")
    _write(root, "tests/unit/test_service.py", "from core.service import VALUE\n")
    _write(root, "tests/unit/test_api.py", "import core.api\n")
    _write(root, "tests/unit/test_unrelated.py", "from core.unrelated import OTHER\n")
    _write(root, "tests/unit/test_compose_pin.py", 'COMPOSE = "infra/docker/compose.yaml"\n')
    _write(root, "tests/unit/workflows/conftest.py")
    _write(root, "tests/unit/workflows/test_runs.py")
    _write(root, "infra/docker/compose.yaml", "services: {}\n")
    return root


def test_a_changed_module_selects_its_tests_and_its_direct_importers_tests(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path)

    assert select_unit_tests(root, ["core/store.py"]) == [
        "tests/unit/test_service.py",
        "tests/unit/test_store.py",
    ]


def test_from_package_import_module_counts_as_importing_that_module(tmp_path: Path) -> None:
    root = _tree(tmp_path)

    assert select_unit_tests(root, ["core/service.py"]) == [
        "tests/unit/test_api.py",
        "tests/unit/test_service.py",
    ]


def test_a_changed_non_python_file_selects_tests_that_name_it(tmp_path: Path) -> None:
    root = _tree(tmp_path)

    assert select_unit_tests(root, ["infra/docker/compose.yaml"]) == [
        "tests/unit/test_compose_pin.py"
    ]


def test_a_changed_conftest_selects_every_test_below_it(tmp_path: Path) -> None:
    root = _tree(tmp_path)

    assert select_unit_tests(root, ["tests/unit/workflows/conftest.py"]) == [
        "tests/unit/workflows/test_runs.py"
    ]


def test_deleted_files_and_docs_select_nothing(tmp_path: Path) -> None:
    root = _tree(tmp_path)

    assert select_unit_tests(root, ["core/removed.py", "docs/notes.md"]) == []


def test_head_selects_incoming_merge_while_main_keeps_prior_feature(tmp_path: Path) -> None:
    root = _tree(tmp_path)

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.name", "Quality test")
    git("config", "user.email", "quality@example.invalid")
    git("add", ".")
    git("commit", "-m", "Initial main")
    git("checkout", "-b", "feature")
    _write(root, "core/store.py", "VALUE = 2\n")
    git("commit", "-am", "Earlier feature")
    git("checkout", "-b", "incoming", "main")
    _write(root, "core/unrelated.py", "OTHER = 2\n")
    git("commit", "-am", "Incoming change")
    git("checkout", "feature")
    git("merge", "--no-commit", "--no-ff", "incoming")

    assert select_unit_tests(root, changed_files(root, "HEAD")) == ["tests/unit/test_unrelated.py"]
    branch_tests = [
        "tests/unit/test_service.py",
        "tests/unit/test_store.py",
        "tests/unit/test_unrelated.py",
    ]
    assert select_unit_tests(root, changed_files(root, "main")) == branch_tests

    git("commit", "-m", "Merge incoming")
    assert select_unit_tests(root, changed_files(root, "HEAD")) == []
    assert select_unit_tests(root, changed_files(root, "main")) == branch_tests
