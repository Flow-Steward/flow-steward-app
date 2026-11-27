"""Compact CLI maintenance can always write its operation log, or runs without it.

On Compact, `maintenance` runs in the runtime container as the operator user
(10007) and logs under /app/.flow-steward/logs, a link to the volume's
state/.flow-steward/logs. A command that ran there as root first (a one-shot
`database upgrade`, an operator's `docker exec`) created that directory as root
with mode 0700, and every later `maintenance` stopped with operation_log_unavailable.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from core.entrypoints.cli.platform import operation_log as operation_log_module
from core.entrypoints.cli.platform.operation_log import OperationLog
from core.infrastructure.runtime.compact import volume as volume_module
from core.infrastructure.runtime.compact.volume import CompactVolumeLayout, prepare_compact_volume


def test_a_root_process_gives_the_log_directories_it_creates_to_their_parents_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    chowned: list[tuple[str, int, int, bool]] = []
    monkeypatch.setattr(operation_log_module.os, "geteuid", lambda: 0)
    real_stat = os.stat

    def stat_as_operator(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if Path(path) == state:
            fields = list(result)
            fields[stat.ST_UID] = 10007
            fields[stat.ST_GID] = 10102
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(operation_log_module.os, "stat", stat_as_operator)
    monkeypatch.setattr(
        operation_log_module.os,
        "chown",
        lambda path, uid, gid, *, follow_symlinks=True: chowned.append(
            (str(path), uid, gid, follow_symlinks)
        ),
    )
    monkeypatch.setattr(operation_log_module.os, "fchown", lambda *_args: None)

    log = OperationLog("maintenance.enable", root=state / ".flow-steward" / "logs")
    log.close()

    assert chowned == [
        (str(state / ".flow-steward"), 10007, 10102, False),
        (str(state / ".flow-steward" / "logs"), 10007, 10102, False),
    ]


def test_a_non_root_process_changes_no_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(operation_log_module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        operation_log_module.os,
        "chown",
        lambda *_args, **_kwargs: pytest.fail("a non-root process must not chown"),
    )

    log = OperationLog("maintenance.enable", root=tmp_path / "logs")
    log.write("command=maintenance.enable")
    log.close()

    assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o700
    assert "command=maintenance.enable" in log.path.read_text(encoding="utf-8")


def test_a_write_that_fails_is_reported_once_and_never_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = OperationLog("database.upgrade", root=tmp_path)
    os.close(log._file)

    log.write("first")
    log.write("second")
    log.close()

    warning = capsys.readouterr().err
    assert warning.count(f"Warning: cannot write the operation log in '{tmp_path}'") == 1


def _state_tree(tmp_path: Path) -> Path:
    logs = tmp_path / "state" / ".flow-steward" / "logs"
    logs.parent.mkdir(parents=True)
    return logs


def _layout(*, root_uid: int, operator_uid: int) -> CompactVolumeLayout:
    return CompactVolumeLayout(
        root_uid=root_uid, root_gid=os.getgid(), operator_uid=operator_uid, runtime_gid=10102
    )


def _repair(tmp_path: Path, layout: CompactVolumeLayout) -> None:
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        volume_module._return_operation_log_root_to_operator(root_fd, layout)
    finally:
        os.close(root_fd)


def test_volume_preparation_gives_a_root_owned_log_root_to_the_operator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    logs = _state_tree(tmp_path)
    logs.mkdir(mode=0o700)
    # The volume as the root supervisor sees it: the log root belongs to "root".
    layout = _layout(root_uid=os.getuid(), operator_uid=os.getuid() + 1)
    changed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        volume_module.os, "fchown", lambda _fd, uid, gid: changed.append((uid, gid))
    )

    _repair(tmp_path, layout)

    assert changed == [(layout.operator_uid, 10102)]
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700


def test_volume_preparation_leaves_an_operator_owned_or_absent_log_root_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    logs = _state_tree(tmp_path)
    monkeypatch.setattr(volume_module.os, "fchown", lambda *_args: pytest.fail("nothing to repair"))
    operator_owned = _layout(root_uid=os.getuid() + 1, operator_uid=os.getuid())

    _repair(tmp_path, operator_owned)  # absent
    logs.mkdir(mode=0o700)
    _repair(tmp_path, operator_owned)  # owned by the operator, not by root
    # One user for root and operator (a provider volume): nothing to give back.
    _repair(tmp_path, _layout(root_uid=os.getuid(), operator_uid=os.getuid()))


def test_volume_preparation_runs_the_log_root_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[CompactVolumeLayout] = []
    monkeypatch.setattr(
        volume_module,
        "_return_operation_log_root_to_operator",
        lambda _fd, layout: calls.append(layout),
    )
    tmp_path.chmod(0o755)
    layout = CompactVolumeLayout.for_current_user()

    prepare_compact_volume(tmp_path, layout=layout)

    assert calls == [layout]
