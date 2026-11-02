"""Real Linux ownership acceptance, selected by the privileged external Tier 3 owner."""

from __future__ import annotations

import os
import pwd
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_cumulative_reader_can_read_both_container_writers() -> None:
    assert os.geteuid() == 0, "Run this acceptance through the root external Tier 3 verifier"
    fs = pwd.getpwnam("fs")
    assert fs.pw_uid != 0
    source = (ROOT / "core/entrypoints/cli/scripts/lint.sh").read_text()
    dispatch = source.split("python -m core.application.tooling.quality.release_gate", 1)[0]
    arguments = shlex.split(dispatch.rsplit("fs_compose run", 1)[1])
    reader_uid, reader_gid = fs.pw_uid, fs.pw_gid
    if "--user" in arguments:
        reader_uid, reader_gid = map(int, arguments[arguments.index("--user") + 1].split(":"))

    def read_as(uid: int, gid: int, *paths: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "import pathlib,sys; [pathlib.Path(p).read_bytes() for p in sys.argv[1:]]",
                *map(str, paths),
            ],
            user=uid,
            group=gid,
            capture_output=True,
            text=True,
            check=False,
        )

    with tempfile.TemporaryDirectory(prefix="fs-mixed-evidence-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        for name, uid, gid in (("root.json", 0, 0), ("fs.json", fs.pw_uid, fs.pw_gid)):
            record = root / name
            record.write_text('{"status":"passed"}\n')
            record.chmod(0o600)
            os.chown(record, uid, gid)
            metadata = record.stat()
            assert (metadata.st_uid, metadata.st_gid) == (uid, gid)
            assert stat.S_IMODE(metadata.st_mode) == 0o600

        # Prove that the fixture exercises actual access control, not a mocked
        # PermissionError or an inaccessible temporary parent directory.
        own = read_as(fs.pw_uid, fs.pw_gid, root / "fs.json")
        assert own.returncode == 0, own.stderr
        denied = read_as(fs.pw_uid, fs.pw_gid, root / "root.json")
        assert denied.returncode != 0
        assert "PermissionError" in denied.stderr
        result = read_as(reader_uid, reader_gid, root / "fs.json", root / "root.json")
        assert result.returncode == 0, result.stderr
