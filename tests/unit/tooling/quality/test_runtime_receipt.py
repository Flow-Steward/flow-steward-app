from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from core.application.tooling.quality import runtime_receipt
from core.application.tooling.quality.runtime_receipt import main, runtime_digest


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "runtime"
    root.mkdir()
    for relative in ("usr/lib/native.so", "usr/lib/python/plugin.py", "usr/bin/tool"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"installed runtime bytes")
    return root, root / "receipts/backend-runtime.sha256"


def test_receipt_tracks_runtime_bytes_instead_of_source_commit(tmp_path: Path) -> None:
    root, output = _runtime(tmp_path)
    source = tmp_path / "app"
    source.mkdir()
    revision = source / "commit"
    revision.write_text("first source SHA")
    before = runtime_digest((root,), output=output)
    revision.write_text("second source SHA")
    assert runtime_digest((root,), output=output) == before
    assert re.fullmatch(r"[0-9a-f]{64}", before)


@pytest.mark.parametrize("relative", ["usr/lib/native.so", "usr/lib/python/plugin.py"])
def test_runtime_content_changes_invalidate_receipt(tmp_path: Path, relative: str) -> None:
    root, output = _runtime(tmp_path)
    before = runtime_digest((root,), output=output)
    (root / relative).write_bytes(b"different installed bytes")
    assert runtime_digest((root,), output=output) != before


def test_executable_bits_and_file_paths_are_measured(tmp_path: Path) -> None:
    root, output = _runtime(tmp_path)
    executable = root / "usr/bin/tool"
    executable.chmod(0o644)
    before = runtime_digest((root,), output=output)
    executable.chmod(0o755)
    executable_digest = runtime_digest((root,), output=output)
    assert executable_digest != before
    executable.rename(executable.with_name("renamed-tool"))
    assert runtime_digest((root,), output=output) != executable_digest


def test_symlink_target_is_measured_without_following_directory(tmp_path: Path) -> None:
    root, output = _runtime(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = outside / "ignored"
    payload.write_text("first")
    link = root / "linked-directory"
    link.symlink_to(outside, target_is_directory=True)
    before = runtime_digest((root,), output=output)
    payload.write_text("second")
    assert runtime_digest((root,), output=output) == before
    link.unlink()
    link.symlink_to("different-target", target_is_directory=True)
    assert runtime_digest((root,), output=output) != before


def test_root_order_and_excluded_receipt_and_caches_do_not_change_identity(tmp_path: Path) -> None:
    root, output = _runtime(tmp_path)
    roots = (root / "usr/bin", root / "usr/lib")
    before = runtime_digest(roots, output=output)
    assert runtime_digest(tuple(reversed(roots)), output=output) == before
    full_before = runtime_digest((root,), output=output)
    for relative in ("receipts/arbitrary", "usr/.cache/package", "usr/__pycache__/module.pyc"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not runtime input")
    assert runtime_digest((root,), output=output) == full_before
    assert main(["--root", str(root), "--output", str(output)]) == 0
    assert output.read_text() == full_before + "\n"
    assert main(["--root", str(root), "--output", str(output)]) == 0
    assert output.read_text() == full_before + "\n"


@pytest.mark.parametrize("root_kind", ["missing", "relative", "empty"])
def test_invalid_runtime_roots_fail(tmp_path: Path, root_kind: str) -> None:
    roots = (
        ()
        if root_kind == "empty"
        else (Path("relative") if root_kind == "relative" else tmp_path / "missing",)
    )
    with pytest.raises((ValueError, OSError)):
        runtime_digest(roots, output=tmp_path / "receipts/digest")


def test_default_absent_platform_root_is_measured_then_invalidates_when_created(
    tmp_path, monkeypatch
):
    root, output = _runtime(tmp_path)
    optional = root / "lib64"
    monkeypatch.setattr(runtime_receipt, "DEFAULT_ROOTS", (root / "usr", optional))
    before = runtime_digest(output=output)
    assert runtime_digest(output=output) == before
    optional.mkdir()
    assert runtime_digest(output=output) != before


def test_unreadable_runtime_fails_and_removes_old_receipt(tmp_path: Path, monkeypatch) -> None:
    root, output = _runtime(tmp_path)
    assert main(["--root", str(root), "--output", str(output)]) == 0
    original_open = Path.open

    def deny_native(path: Path, *args, **kwargs):
        if path.name == "native.so":
            raise PermissionError("unreadable runtime")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_native)
    with pytest.raises(SystemExit) as error:
        main(["--root", str(root), "--output", str(output)])
    assert error.value.code != 0
    assert not output.exists()


def test_cli_runs_as_standalone_file_without_application_imports(tmp_path: Path) -> None:
    root, output = _runtime(tmp_path)
    script = (
        Path(__file__).resolve().parents[4] / "core/application/tooling/quality/runtime_receipt.py"
    )
    result = subprocess.run(
        [sys.executable, "-I", str(script), "--root", str(root), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"[0-9a-f]{64}\n", output.read_text())
