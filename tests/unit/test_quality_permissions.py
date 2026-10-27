from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PERMISSION_SCRIPT = ROOT / "core" / "entrypoints" / "cli" / "scripts" / "quality_permissions.sh"
ALLOWLIST = (
    Path("logs/coverage"),
    Path("logs/coverage/unit"),
    Path("logs/quality"),
    Path("logs/quality/evidence"),
)


def _run(
    mode: str, repo_root: Path, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(PERMISSION_SCRIPT), mode, "--repo-root", str(repo_root)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_repair_creates_only_allowlisted_directories(tmp_path: Path) -> None:
    unrelated_directory = tmp_path / "unrelated"
    unrelated_directory.mkdir(mode=0o750)
    unrelated_file = tmp_path / "unchanged.txt"
    unrelated_file.write_text("unchanged\n", encoding="utf-8")
    unrelated_file.chmod(0o640)

    completed = _run("repair", tmp_path)

    assert completed.returncode == 0, completed.stderr
    for relative_path in ALLOWLIST:
        path = tmp_path / relative_path
        assert path.is_dir()
        assert _mode(path) == 0o777
    assert _mode(unrelated_directory) == 0o750
    assert _mode(unrelated_file) == 0o640
    assert unrelated_file.read_text(encoding="utf-8") == "unchanged\n"


def test_check_rejects_restrictive_mode_without_repairing_it(tmp_path: Path) -> None:
    repair = _run("repair", tmp_path)
    assert repair.returncode == 0, repair.stderr
    restricted = tmp_path / "logs/quality/evidence"
    restricted.chmod(0o700)

    completed = _run("check", tmp_path)

    assert completed.returncode != 0
    assert str(restricted) in completed.stderr
    assert "0777" in completed.stderr
    assert _mode(restricted) == 0o700


def test_check_rejects_symlink_without_modifying_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o750)
    coverage = tmp_path / "logs/coverage"
    coverage.parent.mkdir()
    coverage.symlink_to(target, target_is_directory=True)

    completed = _run("check", tmp_path)

    assert completed.returncode != 0
    assert "symbolic link" in completed.stderr
    assert _mode(target) == 0o750


def test_repair_rejects_symlink_without_modifying_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o750)
    quality = tmp_path / "logs/quality"
    quality.parent.mkdir()
    quality.symlink_to(target, target_is_directory=True)

    completed = _run("repair", tmp_path)

    assert completed.returncode != 0
    assert "symbolic link" in completed.stderr
    assert _mode(target) == 0o750


def test_repair_uses_bsd_compatible_chmod_arguments(tmp_path: Path) -> None:
    shim_directory = tmp_path / "bin"
    shim_directory.mkdir()
    chmod_shim = shim_directory / "chmod"
    chmod_shim.write_text(
        "#!/bin/sh\n"
        'for argument in "$@"; do\n'
        '  if [ "$argument" = "--" ]; then exit 64; fi\n'
        "done\n"
        'exec /bin/chmod "$@"\n',
        encoding="utf-8",
    )
    chmod_shim.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{shim_directory}:{env['PATH']}"

    completed = _run("repair", tmp_path, env=env)

    assert completed.returncode == 0, completed.stderr


def test_repair_does_not_mutate_existing_compliant_directories(tmp_path: Path) -> None:
    for relative_path in ALLOWLIST:
        path = tmp_path / relative_path
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o777)

    shim_directory = tmp_path / "bin"
    shim_directory.mkdir()
    for command in ("chmod", "mkdir"):
        shim = shim_directory / command
        shim.write_text("#!/bin/sh\nexit 65\n", encoding="utf-8")
        shim.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{shim_directory}:{env['PATH']}"

    completed = _run("repair", tmp_path, env=env)

    assert completed.returncode == 0, completed.stderr
