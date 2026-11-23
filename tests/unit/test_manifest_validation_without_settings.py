"""Manifest validation runs where no application settings exist.

The `extension-dependencies` service compiles every installed extension before the
app starts, in a container with no secrets. A validation path that imported the
security configuration there crashed on the missing PII salt and stopped the stack.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "extensions" / "fixture_extension_oauth"

_SCRIPT = """
import sys
from core.infrastructure.extension_runtime.extension_runtime import scan_extensions

extensions = scan_extensions(sys.argv[1])
assert extensions, "the fixture extension was not compiled"
assert "core.infrastructure.security.config" not in sys.modules
"""


def test_extension_scan_needs_no_security_settings(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    shutil.copytree(FIXTURE, root / FIXTURE.name)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"SECURITY_PII_SALT", "FS_STACK_ENVIRONMENT", "ENVIRONMENT", "APP_ENV"}
    }
    env["FS_STACK_ENVIRONMENT"] = "production"
    env["PYTHONPATH"] = str(REPO_ROOT)

    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(root)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-4000:]
