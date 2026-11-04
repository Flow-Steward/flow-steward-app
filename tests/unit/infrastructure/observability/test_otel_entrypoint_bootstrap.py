"""Every real entrypoint applies the OTEL_* policy before a library reads it.

Each module is imported in its own interpreter, as its process starts it, with
an unsupported setting in the environment. A meta-path hook notes the setting
at the first import of each library that reads ``OTEL_*``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
# Module -> whether importing it is known to load a library that reads OTEL_*.
ENTRYPOINTS = {
    "core.entrypoints.mcp.server": True,
    "core.entrypoints.mcp.server_main": True,
    "core.entrypoints.cli.__main__": False,
    "core.entrypoints.cli.worker_process": False,
    "core.entrypoints.cli.control_process": False,
}
_PROBE = """
import importlib.abc, json, os, sys
seen = {}
class Probe(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name in {"opentelemetry", "mcp"} and name not in seen:
            seen[name] = os.environ.get("OTEL_PROPAGATORS")
        return None
sys.meta_path.insert(0, Probe())
import importlib
importlib.import_module(sys.argv[1])
print(json.dumps(seen))
"""


@pytest.mark.parametrize(("module", "loads_library"), sorted(ENTRYPOINTS.items()))
def test_the_entrypoint_removes_unsupported_settings_before_libraries_load(
    module: str, loads_library: bool
) -> None:
    env = {**os.environ, "OTEL_PROPAGATORS": "b3", "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, module],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    if loads_library:
        # The probe reached a library import, so the check below means something.
        assert seen, seen
    assert all(value is None for value in seen.values()), seen
