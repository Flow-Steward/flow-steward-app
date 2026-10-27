"""A synthetic bundle that vendors a hashed wheel, built under `tmp_path`.

Installer and release tests need *a* bundle with vendored wheels to prove the
platform installs them offline and verifies their hashes. Discovering one from
`extensions/` made those tests depend on a bundle shipping: with that directory
extracted or empty they would skip, and a skipped test reads as a passing one.

The bundle is written here instead, so the checks run whatever ships.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import Any

import yaml

BUNDLE_DIR_NAME = "acme_wheel_consumer"
EXTENSION_ID = "acme.wheel-consumer"
DEPENDENCY_NAME = "acme-dependency"
#: The wheel filename uses the distribution name with underscores, as wheels do.
DEPENDENCY_WHEEL_NAME = "acme_dependency"
DEPENDENCY_VERSION = "1.0.0"


def _write_wheel(wheels_dir: Path) -> tuple[Path, str]:
    """Write a minimal importable wheel and return it with its sha256 digest."""
    wheels_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = wheels_dir / f"{DEPENDENCY_WHEEL_NAME}-{DEPENDENCY_VERSION}-py3-none-any.whl"
    dist_info = f"{DEPENDENCY_WHEEL_NAME}-{DEPENDENCY_VERSION}.dist-info"
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{DEPENDENCY_WHEEL_NAME}/__init__.py", "VALUE = 'from-wheel'\n")
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {DEPENDENCY_NAME}\nVersion: {DEPENDENCY_VERSION}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: flow-steward-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    return wheel_path, f"sha256:{digest}"


def write_vendored_wheel_bundle(
    extensions_root: Path,
) -> Path:
    """Write a bundle that pins one wheel by hash, and return its directory."""
    bundle = Path(extensions_root) / BUNDLE_DIR_NAME
    (bundle / "ui").mkdir(parents=True, exist_ok=True)
    _wheel_path, digest = _write_wheel(bundle / "wheels")
    manifest: dict[str, Any] = {
        "manifest_version": 2,
        "runtime": {"contract_version": "extension_host_v2"},
        "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        "extension_id": EXTENSION_ID,
        "kind": "tool_provider",
        "version": "1.0.0",
        "display_name": "Acme Wheel Consumer",
        "description": "Test-only bundle that vendors one hashed wheel.",
        "vendor": {"name": "Acme Test"},
        "features": ["tool"],
        "required_scopes": ["extension:invoke"],
        "entrypoint": {"command": ["python3", "main.py"]},
        "health": {"command": ["python3", "health.py"]},
        "ui_manifest": "ui/ui_manifest.yaml",
        # The shape the runtime accepts: a PEP 503-normalized name, a version and
        # the hashes of the wheels that satisfy it. The wheels are found in
        # `wheels/`, not listed.
        "python_requirements": [
            {
                "name": DEPENDENCY_NAME,
                "version": DEPENDENCY_VERSION,
                "hashes": [digest],
            }
        ],
    }
    (bundle / "extension.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    (bundle / "ui" / "ui_manifest.yaml").write_text("version: '1'\npages: []\n", encoding="utf-8")
    (bundle / "main.py").write_text("print('{\"ok\": true}')\n", encoding="utf-8")
    # Health imports the vendored dependency, so a passing health check is the
    # proof that the wheel was materialized and is importable in the image — not
    # merely that a script exited zero.
    (bundle / "health.py").write_text(
        f"import {DEPENDENCY_WHEEL_NAME} as dependency\n"
        f"print(f'{DEPENDENCY_WHEEL_NAME} {{dependency.VALUE}}')\n",
        encoding="utf-8",
    )
    return bundle
