"""P1-2: two Compact runtimes could run on one data volume.

ADR 0029: "A local volume lock prevents two Compact processes sharing that volume".
There was no such lock: main() went straight into bootstrap, so a second container on
the same volume -- an overlapping replacement, a duplicated service -- ran schema,
seed and worker identity in parallel with the first and then started a second set of
consumers against the same database and broker.

Exercised through the real main(), in two processes sharing one data root. Bootstrap is
replaced by a marker, because the question is only who gets past the start.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_RUNTIME = textwrap.dedent(
    """
    import os, pathlib, sys, time
    from core.entrypoints.cli import compact_runtime

    data_root = pathlib.Path(sys.argv[1])
    marker = pathlib.Path(sys.argv[2])
    hold = float(sys.argv[3])

    def bootstrap(_root):
        marker.write_text(str(os.getpid()))
        time.sleep(hold)  # still running while the other one starts
        raise SystemExit(0)

    compact_runtime._bootstrap = bootstrap
    compact_runtime.verify_compact_environment_identity = lambda _env: None
    os.environ["FS_COMPACT_DATA_ROOT"] = str(data_root)
    os.environ["FS_DEPLOYMENT_PROFILE"] = "compact"
    sys.exit(compact_runtime.main())
    """
)


def _start(data_root: Path, marker: Path, hold: float) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _RUNTIME, str(data_root), str(marker), str(hold)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_a_second_runtime_on_the_same_volume_is_refused(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    (data_root / "state").mkdir(parents=True)
    first_marker, second_marker = tmp_path / "first", tmp_path / "second"

    first = _start(data_root, first_marker, hold=6)
    deadline = time.monotonic() + 20
    while not first_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert first_marker.exists(), first.communicate(timeout=5)
    second = _start(data_root, second_marker, hold=0)
    second_out, second_err = second.communicate(timeout=30)
    first.communicate(timeout=30)

    assert not second_marker.exists(), "the second runtime started next to the first"
    assert second.returncode == 75, second_out + second_err
    assert "Another Compact runtime is using this data volume" in second_err


def test_a_runtime_whose_predecessor_died_is_not_locked_out(tmp_path: Path) -> None:
    """The lock follows its holder's life: a crashed container must not wedge the volume."""
    data_root = tmp_path / "data"
    (data_root / "state").mkdir(parents=True)
    first_marker, second_marker = tmp_path / "first", tmp_path / "second"

    first = _start(data_root, first_marker, hold=30)
    deadline = time.monotonic() + 20
    while not first_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    first.kill()  # no cleanup, no unlock: what a crash leaves
    first.communicate(timeout=10)

    second = _start(data_root, second_marker, hold=0)
    second_out, second_err = second.communicate(timeout=30)

    assert second_marker.exists(), second_out + second_err
    assert second.returncode == 0, second_out + second_err
