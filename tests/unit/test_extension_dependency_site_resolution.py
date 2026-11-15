"""Which dependency site an extension subprocess gets on its PYTHONPATH.

Live on fs-ext (2026-09-24): enabling an extension failed its health check with
"No module named ..." although its .fs-python site held that module. A bundle with
an extension-descriptor.yaml is re-imported as a descriptor release on enable, and
that release names its version `release_version` -- the site resolver read
`version`, `latest_version` and the compiled manifest only, so it resolved no site,
and the health ran without the bundle's dependencies. A bundle without a descriptor
file passed only because it keeps the scanned release.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from core.infrastructure.extension_runtime.python_dependencies import (
    extension_python_site_dir,
    extension_runtime_pythonpath,
)


def test_a_descriptor_release_resolves_its_bundles_site(tmp_path: Path) -> None:
    release = {"bundle_root": str(tmp_path), "release_version": "0.1.0"}

    assert extension_python_site_dir(release) == tmp_path / ".fs-python" / "0.1.0" / "site"


def test_a_descriptor_release_gets_its_site_first_on_the_path(tmp_path: Path) -> None:
    """The release shape a descriptor import stores: `release_version`, no `version`."""
    release = {"bundle_root": str(tmp_path), "release_version": "0.1.0"}

    site = str(tmp_path / ".fs-python" / "0.1.0" / "site")
    assert extension_runtime_pythonpath(release).split(os.pathsep)[0] == site


def test_an_explicit_version_still_wins(tmp_path: Path) -> None:
    """The green direction: the fields read before are read first, as before."""
    release = {
        "bundle_root": str(tmp_path),
        "version": "2.0.0",
        "latest_version": "1.5.0",
        "compiled_manifest": {"version": "1.0.0"},
        "release_version": "0.1.0",
    }

    assert extension_python_site_dir(release) == tmp_path / ".fs-python" / "2.0.0" / "site"
    assert extension_python_site_dir({"bundle_root": str(tmp_path)}) is None


def test_an_installed_descriptor_resolves_its_nested_release_version(tmp_path: Path) -> None:
    """Operator/runtime install rows carry the descriptor under compiled_manifest."""
    install = {
        "bundle_root": str(tmp_path),
        "compiled_manifest": {"release_version": "1.0.0"},
    }
    site = tmp_path / ".fs-python" / "1.0.0" / "site"
    site.mkdir(parents=True)
    (site / "installed_dependency.py").write_text("VALUE = 'installed wheel site'\n")

    env = {**os.environ, "PYTHONPATH": extension_runtime_pythonpath(install)}
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import installed_dependency; print(installed_dependency.VALUE)",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "installed wheel site"
    assert extension_python_site_dir(install) == site


def test_nested_descriptor_does_not_override_existing_release_version(tmp_path: Path) -> None:
    release = {
        "bundle_root": str(tmp_path),
        "release_version": "2.0.0",
        "compiled_manifest": {"release_version": "1.0.0"},
    }
    assert extension_python_site_dir(release) == tmp_path / ".fs-python" / "2.0.0" / "site"
