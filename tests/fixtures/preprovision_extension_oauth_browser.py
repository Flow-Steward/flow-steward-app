#!/usr/bin/env python3
"""Materialize the neutral OAuth fixture before read-only runtimes start."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import yaml

FIXTURE_EXTENSION_ID = "fixture.extension-oauth"


def preprovision_fixture(
    *,
    source: Path,
    extensions_root: Path,
    provider_base_url: str,
) -> Path:
    source = source.resolve(strict=True)
    manifest_path = source / "extension.yaml"
    if not manifest_path.is_file():
        raise ValueError(f"OAuth fixture manifest is missing: {manifest_path}")

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("extension_id") != FIXTURE_EXTENSION_ID:
        raise ValueError(f"OAuth fixture must declare extension_id {FIXTURE_EXTENSION_ID!r}")

    extensions_root.mkdir(parents=True, exist_ok=True)
    destination = extensions_root / source.name
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".fs-python", "__pycache__", "*.pyc"),
    )

    copied_manifest_path = destination / "extension.yaml"
    provider = manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]
    credential_fields = {field["field_id"]: field for field in provider["credential_fields"]}
    credential_fields["client_id"]["validation"] = {"min_length": 2, "max_length": 40}
    credential_fields["client_secret"]["validation"] = {"min_length": 4, "max_length": 80}
    for endpoint, suffix in {
        "authorization_endpoint": "authorize",
        "token_endpoint": "token",
        "revocation_endpoint": "revoke",
    }.items():
        provider[endpoint]["url"] = f"{provider_base_url.rstrip('/')}/{suffix}"
    copied_manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--extensions-root", type=Path, required=True)
    parser.add_argument("--provider-base-url", required=True)
    args = parser.parse_args()
    preprovision_fixture(
        source=args.source,
        extensions_root=args.extensions_root,
        provider_base_url=args.provider_base_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
