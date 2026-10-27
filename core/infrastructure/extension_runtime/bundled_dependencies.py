"""Build-time materialization for dependencies of shipped extension bundles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from core.infrastructure.extension_runtime.extension_runtime import scan_extensions
from core.infrastructure.extension_runtime.python_dependencies import (
    materialize_python_requirements,
)


def materialize_bundled_extension_dependencies(
    extensions_root: str | Path,
) -> list[dict[str, Any]]:
    """Materialize every shipped manifest's pinned wheels into its own bundle."""
    root = Path(extensions_root).resolve()
    evidence: list[dict[str, Any]] = []
    for extension in scan_extensions(str(root)):
        requirements = list(extension.get("python_requirements") or [])
        if not requirements:
            continue
        extension_id = str(extension.get("extension_key") or "").strip()
        errors = [
            *list(extension.get("compile_errors") or []),
            *list(extension.get("validation_errors") or []),
        ]
        scan_error = str(extension.get("scan_error") or "").strip()
        if scan_error:
            errors.append(scan_error)
        if errors:
            raise RuntimeError(
                f"Cannot materialize invalid shipped extension '{extension_id}': {errors!r}"
            )
        compiled_manifest = dict(extension.get("compiled_manifest") or {})
        result = materialize_python_requirements(
            bundle_root=str(extension.get("bundle_root") or ""),
            compiled_manifest=compiled_manifest,
        )
        if result.get("mode") != "bundled_wheels":
            raise RuntimeError(
                f"Shipped extension '{extension_id}' did not materialize bundled wheels"
            )
        evidence.append(
            {
                "extension_id": extension_id,
                "version": str(extension.get("version") or "").strip(),
                "dependency_count": len(result.get("dependencies") or []),
            }
        )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("extensions_root", nargs="?", default="/app/extensions")
    args = parser.parse_args()
    evidence = materialize_bundled_extension_dependencies(args.extensions_root)
    print(json.dumps({"materialized": evidence}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["materialize_bundled_extension_dependencies"]
