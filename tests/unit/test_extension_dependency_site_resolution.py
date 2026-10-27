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
