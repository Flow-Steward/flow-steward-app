"""The assertion-rewrite warm-up splits the selected test files across workers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.application.tooling.quality import assert_cache_warmup


def _tree(root: Path, count: int) -> None:
    for index in range(count):
        path = root / "tests/unit" / f"test_{index:02d}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    stress = root / "tests/unit/stress/test_heavy.py"
    stress.parent.mkdir(parents=True, exist_ok=True)
    stress.write_text("\n", encoding="utf-8")
    (root / "tests/unit/helper.py").write_text("\n", encoding="utf-8")


def test_directories_expand_to_test_modules_without_ignored_paths(tmp_path: Path) -> None:
    _tree(tmp_path, 3)

    files = assert_cache_warmup.selected_test_files(
        tmp_path, ["tests/unit", "tests/unit/test_01.py::test_ok"], ["tests/unit/stress"]
    )

    assert files == ["tests/unit/test_00.py", "tests/unit/test_01.py", "tests/unit/test_02.py"]


def test_each_worker_gets_one_collect_only_process(tmp_path: Path, monkeypatch) -> None:
    _tree(tmp_path, 40)
    launched: list[tuple[str, ...]] = []

    class FakeProcess:
        def __init__(self, command, **kwargs) -> None:
            assert kwargs["cwd"] == tmp_path
            launched.append(tuple(command))

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(assert_cache_warmup.subprocess, "Popen", FakeProcess)
    config = SimpleNamespace(
        invocation_params=SimpleNamespace(dir=tmp_path),
        args=["tests/unit"],
        getoption=lambda name: ["tests/unit/stress"],
    )

    assert_cache_warmup.pytest_xdist_setupnodes(config, specs=[object()] * 4)

    assert len(launched) == 4
    assert all("--collect-only" in command and "no:xdist" in command for command in launched)
    collected = [item for command in launched for item in command if item.endswith(".py")]
    assert sorted(collected) == [f"tests/unit/test_{index:02d}.py" for index in range(40)]


def test_a_small_selection_is_not_warmed(tmp_path: Path, monkeypatch) -> None:
    _tree(tmp_path, 5)
    monkeypatch.setattr(
        assert_cache_warmup.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError),
    )
    config = SimpleNamespace(
        invocation_params=SimpleNamespace(dir=tmp_path),
        args=["tests/unit"],
        getoption=lambda name: [],
    )

    assert_cache_warmup.pytest_xdist_setupnodes(config, specs=[object()] * 4)
