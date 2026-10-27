"""C1 (External review, P1-1 remainder): a child whose command name has a space.

/proc/<pid>/stat puts the command name in parentheses, and the name may contain spaces
("extension job") and parentheses. Two of the supervisor's three readers split the
whole line, so such a child's parent pid came from the wrong column and the child was
not found: cleanup counted as done and a replacement started beside the old one.
Derived: every reader is driven over the same fake /proc.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.infrastructure.runtime.compact import supervisor

_SUPERVISOR = 100
_NAMES = ("python", "extension job", "odd) (name", "a b c d e")


def _stat(pid: int, name: str, ppid: int, session: int) -> str:
    return f"{pid} ({name}) S {ppid} {pid} {session} 0 -1 4194560 0 0 0 0\n"


@pytest.fixture
def proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[int, str]:
    children = {}
    for offset, name in enumerate(_NAMES):
        pid = 200 + offset
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(_stat(pid, name, _SUPERVISOR, 900 + offset))
        children[pid] = name
    (tmp_path / "300").mkdir()
    (tmp_path / "300" / "stat").write_text(_stat(300, "grand child", 203, 903))
    (tmp_path / "self").mkdir()  # not a pid: ignored
    monkeypatch.setattr(supervisor, "_PATH_PROC", str(tmp_path))
    return children


def test_every_child_is_found_whatever_its_name(proc: dict[int, str]) -> None:
    assert supervisor._linux_child_pids(_SUPERVISOR) == set(proc)


def test_the_process_table_reads_parent_and_session_past_the_name(proc: dict[int, str]) -> None:
    table = supervisor._process_table()

    assert table[201] == (_SUPERVISOR, 901)  # "extension job"
    assert table[202] == (_SUPERVISOR, 902)  # a name with its own parentheses
    assert supervisor._descendants(_SUPERVISOR, table) == {*proc, 300}


def test_the_whole_tree_is_signalled(proc: dict[int, str], monkeypatch: pytest.MonkeyPatch) -> None:
    signalled: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, _signum: signalled.append(pid))
    monkeypatch.setattr(os, "killpg", lambda _pid, _signum: None)

    class _Process:
        pid = _SUPERVISOR

    supervisor._signal_process_tree(_Process(), 15)

    assert set(signalled) == {*proc, 300}
