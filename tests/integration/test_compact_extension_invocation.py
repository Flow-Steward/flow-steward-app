from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from core.application.extensions.invocation_release import InvocationReleaseSelection
from core.infrastructure.extension_runtime.extension_runtime import run_extension_invocation


def _release(root: Path, version: str, barrier: Path, resume: Path) -> Path:
    release = root / ".releases" / version
    release.mkdir(parents=True)
    (release / "version.txt").write_text(version, encoding="utf-8")
    (release / "dependency.txt").write_text(f"dependency-{version}", encoding="utf-8")
    (release / "main.py").write_text(
        "import json, pathlib, time\n"
        "root = pathlib.Path.cwd()\n"
        "version = (root / 'version.txt').read_text().strip()\n"
        f"barrier = pathlib.Path({str(barrier)!r})\n"
        f"resume = pathlib.Path({str(resume)!r})\n"
        "if version == 'v1':\n"
        "    barrier.write_text('ready')\n"
        "    deadline = time.monotonic() + 10\n"
        "    while not resume.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        "dependency = (root / 'dependency.txt').read_text().strip()\n"
        "print(json.dumps({'ok': True, 'version': version, 'dependency': dependency}))\n",
        encoding="utf-8",
    )
    return release


def _extension(active: Path, version: str) -> dict[str, object]:
    return {
        "extension_id": "acme.barrier",
        "release_id": version,
        "version": version,
        "bundle_root": str(active),
        "runtime": {"contract_version": "extension_host_v2"},
        "entrypoint": {"command": [sys.executable, "main.py"]},
    }


def test_v1_reader_survives_v2_publish(tmp_path: Path) -> None:
    barrier = tmp_path / "barrier"
    resume = tmp_path / "resume"
    v1 = _release(tmp_path, "v1", barrier, resume)
    v2 = _release(tmp_path, "v2", barrier, resume)
    active = tmp_path / "active"
    active.symlink_to(v1)
    selected = InvocationReleaseSelection.capture(_extension(active, "v1"))
    assert selected.bundle_root == v1

    result: dict[str, object] = {}

    def invoke_v1() -> None:
        result.update(run_extension_invocation(_extension(active, "v1"), timeout_seconds=15))

    thread = threading.Thread(target=invoke_v1)
    thread.start()
    deadline = time.monotonic() + 10
    while not barrier.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert barrier.exists(), result

    replacement = tmp_path / ".active-v2"
    replacement.symlink_to(v2)
    os.replace(replacement, active)
    next_result = run_extension_invocation(_extension(active, "v2"), timeout_seconds=5)
    resume.write_text("continue", encoding="utf-8")
    thread.join(timeout=10)

    assert result["response"] == {
        "ok": True,
        "version": "v1",
        "dependency": "dependency-v1",
    }
    assert next_result["response"] == {
        "ok": True,
        "version": "v2",
        "dependency": "dependency-v2",
    }
    assert v1.is_dir()
