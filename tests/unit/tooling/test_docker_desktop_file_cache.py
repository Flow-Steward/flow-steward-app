"""Containers started by the gate and the installer see the host's current files.

Docker Desktop's VM caches what it has looked up in a mounted host folder and relies
on the host to invalidate it. Under Docker VMM that notifier was found blocked for
six days, and every folder mounted before read stale: new files missing, renamed-away
files read as NUL bytes, files replaced by rename cut to their old size.
``docker_desktop_file_cache.sh`` drops the VM's cache and proves the view with a
probe before a container reads the tree. These tests pin when it runs, what it
runs, which failures stop the caller and which never do.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "core/entrypoints/cli/scripts/docker_desktop_file_cache.sh"
LINT_SCRIPT = ROOT / "core/entrypoints/cli/scripts/lint.sh"
INSTALL_SCRIPT = ROOT / "install.sh"
DROP = "sync && echo 3 > /proc/sys/vm/drop_caches"
# install.sh pins a prepared installer image to its immutable local ID before use.
INSTALLER_IMAGE_ID = "sha256:" + "b" * 64


def _fake_docker(
    tmp_path: Path,
    *,
    operating_system: str,
    drop_exit: int = 0,
    probe: str = "faithful",
) -> Path:
    """A docker CLI that logs its calls and plays the probe container.

    ``probe``: ``faithful`` runs the probe script against the real directory,
    ``stale`` answers the way the wedged VM did (old size and content, no new
    file), ``fail`` cannot start the container.
    """
    log_path = tmp_path / "docker.log"
    shim = tmp_path / "docker"
    shim.write_text(
        f"""#!/usr/bin/env bash
{{ printf '%s' "${{1:-}}"; for arg in "${{@:2}}"; do printf '\\t%s' "${{arg}}"; done; printf '\\n'; }} >> "{log_path}"
command="${{1:-}}"
[[ "${{command}}" == "--host" ]] && command="${{3:-}}"
if [[ "${{1:-}}" == "context" && "${{2:-}}" == "inspect" ]]; then
  printf '%s\\n' "unix://{tmp_path / "docker.sock"}"
fi
if [[ "${{command}}" == "info" ]]; then
  printf '%s\\n' "{operating_system}"
  exit 0
fi
if [[ "${{command}}" == "image" && " $* " == *" --format "* ]]; then
  printf '%s\\n' "{INSTALLER_IMAGE_ID}"
  exit 0
fi
if [[ "${{command}}" == "run" ]]; then
  if [[ " $* " == *" --privileged "* ]]; then
    exit {drop_exit}
  fi
  mount=""
  for arg in "$@"; do
    [[ "${{arg}}" == *":/probe:ro" ]] && mount="${{arg%:/probe:ro}}"
  done
  if [[ -n "${{mount}}" ]]; then
    case "{probe}" in
      fail) exit 125 ;;
      stale) printf '%s\\n' 9 'old-content' . .. .docker-desktop-mount-probe; exit 0 ;;
    esac
    script="${{@: -1}}"
    exec bash -c "${{script//\\/probe/${{mount}}}}"
  fi
fi
exit 0
""",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return log_path


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    return env


def _refresh(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HELPER), *args],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(log_path: Path) -> list[list[str]]:
    if not log_path.exists():
        return []
    return [line.split("\t") for line in log_path.read_text(encoding="utf-8").splitlines()]


def _probe_dir(tmp_path: Path) -> Path:
    probe_dir = tmp_path / "checkout" / "logs" / "quality"
    probe_dir.mkdir(parents=True)
    return probe_dir


def test_docker_desktop_drops_the_vm_file_cache_with_the_given_local_image(
    tmp_path: Path,
) -> None:
    log_path = _fake_docker(tmp_path, operating_system="Docker Desktop")

    completed = _refresh(tmp_path, "refresh", "sha256:" + "a" * 64)

    assert completed.returncode == 0, completed.stderr
    calls = _calls(log_path)
    assert calls[0] == ["info", "--format", "{{.OperatingSystem}}"]
    drop = calls[1]
    assert drop[0] == "run"
    # Never pulled: the caller names an image it has just built or verified.
    assert drop[drop.index("--pull") + 1] == "never"
    assert "--privileged" in drop
    assert drop[drop.index("--network") + 1] == "none"
    assert drop[drop.index("--user") + 1] == "0:0"
    assert "sha256:" + "a" * 64 in drop
    assert drop[-1] == DROP
    # Without a probe directory nothing is written and nothing else runs.
    assert len(calls) == 2
    assert completed.stderr == ""


def test_a_current_view_passes_the_probe_and_leaves_only_the_kept_probe_file(
    tmp_path: Path,
) -> None:
    log_path = _fake_docker(tmp_path, operating_system="Docker Desktop")
    probe_dir = _probe_dir(tmp_path)

    first = _refresh(tmp_path, "refresh", "fs-audit:test", str(probe_dir))
    second = _refresh(tmp_path, "refresh", "fs-audit:test", str(probe_dir))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    probe = _calls(log_path)[2]
    assert probe[0] == "run"
    assert "--privileged" not in probe
    assert probe[probe.index("--pull") + 1] == "never"
    assert probe[probe.index("--network") + 1] == "none"
    assert f"{probe_dir.resolve()}:/probe:ro" in probe
    assert [path.name for path in probe_dir.iterdir()] == [".docker-desktop-mount-probe"]


def test_a_stale_view_stops_the_caller_with_what_to_do(tmp_path: Path) -> None:
    _fake_docker(tmp_path, operating_system="Docker Desktop", probe="stale")
    probe_dir = _probe_dir(tmp_path)

    completed = _refresh(tmp_path, "refresh", "fs-audit:test", str(probe_dir))

    assert completed.returncode == 3
    assert "stale copy" in completed.stderr
    assert "missing in the container's listing" in completed.stderr
    assert "Restart" in completed.stderr
    assert "Virtual Machine Manager" in completed.stderr
    assert [path.name for path in probe_dir.iterdir()] == [".docker-desktop-mount-probe"]


@pytest.mark.parametrize("operating_system", ["Ubuntu 24.04.5 LTS", ""])
def test_any_other_engine_starts_no_container_and_writes_nothing(
    tmp_path: Path, operating_system: str
) -> None:
    log_path = _fake_docker(tmp_path, operating_system=operating_system)
    probe_dir = _probe_dir(tmp_path)

    completed = _refresh(tmp_path, "refresh", "flow-steward-installer:test", str(probe_dir))

    assert completed.returncode == 0, completed.stderr
    assert [call[0] for call in _calls(log_path)] == ["info"]
    assert list(probe_dir.iterdir()) == []
    assert completed.stderr == ""


def test_a_refused_cache_drop_warns_and_still_probes(tmp_path: Path) -> None:
    log_path = _fake_docker(tmp_path, operating_system="Docker Desktop", drop_exit=125)
    probe_dir = _probe_dir(tmp_path)

    completed = _refresh(tmp_path, "refresh", "fs-audit:test", str(probe_dir))

    assert completed.returncode == 0
    assert "Could not drop" in completed.stderr
    assert len([call for call in _calls(log_path) if call[0] == "run"]) == 2


def test_a_probe_container_that_cannot_start_warns_and_never_fails_the_caller(
    tmp_path: Path,
) -> None:
    _fake_docker(tmp_path, operating_system="Docker Desktop", probe="fail")
    probe_dir = _probe_dir(tmp_path)

    completed = _refresh(tmp_path, "refresh", "fs-audit:test", str(probe_dir))

    assert completed.returncode == 0
    assert "Could not start a container" in completed.stderr
    assert [path.name for path in probe_dir.iterdir()] == [".docker-desktop-mount-probe"]


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("refresh",),
        ("refresh", ""),
        ("drop", "image"),
        ("refresh", "image", ""),
        ("refresh", "image", "/definitely/not/a/directory"),
        ("refresh", "image", "/tmp", "extra"),
    ],
)
def test_malformed_invocation_is_a_usage_error(tmp_path: Path, args: tuple[str, ...]) -> None:
    log_path = _fake_docker(tmp_path, operating_system="Docker Desktop")

    completed = _refresh(tmp_path, *args)

    assert completed.returncode == 2
    assert _calls(log_path) == []


def test_helper_is_executable_for_lint() -> None:
    assert HELPER.stat().st_mode & stat.S_IXUSR


def test_the_gate_refreshes_after_its_last_host_write_and_before_fs_audit_reads_the_tree() -> None:
    source = LINT_SCRIPT.read_text(encoding="utf-8")
    refresh = (
        '"${SCRIPT_DIR}/docker_desktop_file_cache.sh" refresh "${FS_TEST_AUDIT_IMAGE_ID}" \\\n'
        '      "${ROOT_DIR}/logs/quality" || exit $?'
    )

    assert source.count("docker_desktop_file_cache.sh") == 1
    assert source.count(refresh) == 1
    refresh_at = source.index(refresh)
    # The image it runs is the validated fs-audit image, resolved before.
    assert source.index("resolve_audit_test_image || exit $?") < refresh_at
    # The tracked-files manifest is the last file the host writes into the tree.
    assert source.index('git -C "${ROOT_DIR}" ls-files > "${boundary_manifest_host}"') < refresh_at
    # Every fs-audit run that reads the checkout comes after it.
    for reader in (
        'bash /app/core/entrypoints/cli/scripts/lint.sh "${final_tier_two_args[@]}"',
        "python -m core.application.tooling.db.init_db",
        'fs-audit bash /app/core/entrypoints/cli/scripts/lint.sh "${local_inside_args[@]}"',
        '"${SCRIPT_DIR}/run-external-tier-three.sh"',
        "python -m core.application.tooling.quality.release_gate",
    ):
        assert refresh_at < source.index(reader), reader


def _install(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = _env(tmp_path)
    env.update(
        {
            "FS_INSTALLER_IMAGE": "flow-steward-installer:test",
            "FS_INSTALLER_STATE_DIR": str(tmp_path / "state"),
            "FS_INSTALLER_HEARTBEAT_SECONDS": "0.05",
            "PYTHON_BIN": "/definitely/not/python",
        }
    )
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--non-interactive", "--confirm"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _runs(log_path: Path) -> list[list[str]]:
    return [call for call in _calls(log_path) if call[0] == "run"]


def test_installer_drops_and_probes_with_its_own_image_before_the_installer_container(
    tmp_path: Path,
) -> None:
    log_path = _fake_docker(tmp_path, operating_system="Docker Desktop")

    completed = _install(tmp_path)

    assert completed.returncode == 0, completed.stderr
    drop, probe, installer = _runs(log_path)
    assert drop[-1] == DROP
    lifecycle_dir = (ROOT / ".flow-steward").resolve()
    assert f"{lifecycle_dir}:/probe:ro" in probe
    # The same immutable image the installer itself then runs.
    for run in (drop, probe, installer):
        assert INSTALLER_IMAGE_ID in run
    assert "--privileged" not in installer
    assert "core.entrypoints.cli.installer" in installer


def test_installer_on_a_stale_view_stops_before_the_installer_container(
    tmp_path: Path,
) -> None:
    log_path = _fake_docker(tmp_path, operating_system="Docker Desktop", probe="stale")

    completed = _install(tmp_path)

    assert completed.returncode == 7
    assert "stale copy" in completed.stderr
    assert not any("core.entrypoints.cli.installer" in call for call in _runs(log_path))


def test_installer_on_a_linux_engine_starts_only_the_installer_container(
    tmp_path: Path,
) -> None:
    log_path = _fake_docker(tmp_path, operating_system="Ubuntu 24.04.5 LTS")

    completed = _install(tmp_path)

    assert completed.returncode == 0, completed.stderr
    runs = _runs(log_path)
    assert len(runs) == 1
    assert "--privileged" not in runs[0]
