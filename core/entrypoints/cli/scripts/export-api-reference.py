#!/usr/bin/env python3
"""Offline CI adapter: use released application imports, never tooling's core tree."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

# This file and the pure contract module are mounted read-only into the released
# image. Do NOT put the tooling checkout on sys.path: /app owns product imports.
ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location(
    "api_reference_contract", ROOT / "core/application/tooling/api_reference.py"
)
if spec is None or spec.loader is None:
    raise ImportError("api_reference contract module could not be loaded")
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


def export(directory: Path, release: str, revision: str) -> None:
    """Assemble real routes without lifespan, requests, database or HTTP server."""
    from core.infrastructure.config.version import APP_VERSION
    from core.infrastructure.runtime_env import runtime_env

    if str(APP_VERSION).removeprefix("v") != release.removeprefix("v"):
        raise ValueError("released application version differs from requested release")
    if runtime_env.get("FS_EMBEDDED_IMAGE_REVISION", runtime_env.get("FS_BUILD_SHA")) != revision:
        raise ValueError("released image revision differs from requested revision")
    from core.entrypoints.api import dependencies
    from core.entrypoints.api.app_factory import create_app
    from core.entrypoints.api.public_openapi_policy import PUBLIC_OPENAPI_POLICY as policy
    from core.infrastructure.runtime import http_app_lifecycle as canonical
    from core.infrastructure.runtime.http_app_constants import _INTERNAL_OPENAPI_DESCRIPTION

    # Offline construction fixture only: this eager runtime guard resolves live DB
    # stores before route registration. Schema generation uses none of those stores.
    # All route/dependency/schema registration remains real; patch restores on exit.
    with patch(
        "core.infrastructure.runtime.app_factory.validate_runtime_service_ports", return_value={}
    ):
        app = create_app(initialize_api_dependencies=dependencies.init)
    public = canonical._build_curated_openapi_schema(
        current_app=app,
        title="Flow Steward Public API",
        version="v1",
        description=policy.description,
        routes=policy.select_routes(app),
        tags=policy.tags,
        security_scheme_name="BearerAuth",
        schema_aliases=policy.schema_aliases,
        customize=policy.apply,
    )
    application = canonical._build_curated_openapi_schema(
        current_app=app,
        title="Flow Steward Internal API",
        version=str(app.version or ""),
        description=_INTERNAL_OPENAPI_DESCRIPTION,
        routes=canonical._internal_openapi_routes(app),
    )
    selected = {
        (route.path, method.lower())
        for route in policy.select_routes(app)
        for method in route.methods
    }
    actual = {
        (path, method)
        for path, item in public["paths"].items()
        for method in item
        if method in contract.METHODS
    }
    if actual != selected or not actual:
        raise ValueError("export differs from the canonical public allowlist")
    if (
        len(application["paths"]) <= len(public["paths"])
        or "/api/projects" not in application["paths"]
        or not application.get("components", {}).get("schemas")
    ):
        raise ValueError("application reference is missing real application routes/models")
    if app.openapi_url is not None or app.docs_url is not None:
        raise ValueError("offline export must not enable live docs endpoints")
    public = contract.normalize_release_metadata(public)
    application = contract.normalize_release_metadata(application)
    contract.write_assets(directory, release, revision, public, application)
    contract.validate_assets(directory, release, revision)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "validate", "select-image"))
    parser.add_argument("--release", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "export":
        export(args.directory, args.release, args.revision)
    elif args.action == "validate":
        contract.validate_assets(args.directory, args.release, args.revision)
    else:
        # On the runner, validate the complete canonical image-role/platform set
        # using publication tooling before selecting any individual export image.
        import sys

        sys.path.insert(0, str(ROOT))
        from core.application.tooling.release_automation import require_complete_release

        paths = sorted(args.directory.glob("registry-verification-*.json"))
        if not paths:
            raise ValueError("release has no registry verification receipts")
        documents = []
        for path in paths:
            document = json.loads(path.read_text())
            namespaces = tuple(row["namespace"] for row in document["registries"])
            require_complete_release(
                path, version=args.release[1:], revision=args.revision, namespaces=namespaces
            )
            documents.append(document)
        selected = contract.select_verified_image(documents, args.release, args.revision)
        if args.output is None:
            parser.error("select-image requires --output")
        args.output.write_bytes(contract.encode_json(selected))


if __name__ == "__main__":
    main()
