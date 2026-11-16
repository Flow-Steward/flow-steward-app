"""Deterministic, self-contained release API reference artifact contract."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
FILES = {"public": "openapi-public.json", "application": "openapi-application.json"}


def encode_json(value: Any) -> bytes:
    """Serialize exact checksum-covered UTF-8 bytes deterministically."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _identity(release: str, revision: str) -> None:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", release) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("expected a stable release tag and full Git revision")


def _schema_counts(schema: dict[str, Any]) -> tuple[int, int]:
    if not isinstance(schema, dict) or not re.fullmatch(
        r"3\.[01]\.\d+", str(schema.get("openapi", ""))
    ):
        raise ValueError("expected an OpenAPI 3 document")
    info = schema.get("info")
    if not isinstance(info, dict) or not info.get("title") or not info.get("version"):
        raise ValueError("schema info requires title and version")
    paths = schema.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise ValueError("schema paths must be a nonempty object")
    if schema.get("servers"):
        raise ValueError("release schemas must not inject instance servers")
    operations = 0
    for path, item in paths.items():
        if not path.startswith("/") or not isinstance(item, dict):
            raise ValueError("invalid OpenAPI path item")
        if item.get("servers"):
            raise ValueError("release paths must not inject instance servers")
        for method, operation in item.items():
            if method not in METHODS:
                continue
            if (
                not isinstance(operation, dict)
                or not isinstance(operation.get("responses"), dict)
                or not operation["responses"]
            ):
                raise ValueError("operation requires responses")
            if operation.get("servers"):
                raise ValueError("release operations must not inject instance servers")
            operations += 1
    if not operations:
        raise ValueError("schema contains no operations")

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                if not isinstance(ref, str) or not ref.startswith("#/components/"):
                    raise ValueError("only local component references are allowed")
                target: Any = schema
                try:
                    for segment in ref[2:].split("/"):
                        target = target[segment.replace("~1", "/").replace("~0", "~")]
                except (KeyError, TypeError) as exc:
                    raise ValueError(f"unresolved component reference: {ref}") from exc
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)
    return len(paths), operations


def normalize_release_metadata(schema: dict[str, Any]) -> dict[str, Any]:
    """Repair misplaced v0.9.18 request prose without changing API contracts.

    Media Type Objects have no description field. The canonical approval route
    placed request documentation there; retain it on its owning Request Body.
    Keep this export-only so published product behavior remains unchanged.
    """
    normalized = deepcopy(schema)
    for item in normalized.get("paths", {}).values():
        for method, operation in item.items():
            if method not in METHODS:
                continue
            body = operation.get("requestBody", {})
            for media in body.get("content", {}).values():
                description = media.pop("description", None)
                if description:
                    existing = body.get("description", "")
                    body["description"] = (
                        existing + "\n\n" + description if existing else description
                    )
    return normalized


def write_assets(
    directory: Path,
    release: str,
    revision: str,
    public: dict[str, Any],
    application: dict[str, Any],
) -> None:
    """Validate and write the two canonical schemas and checksum/count manifest."""
    _identity(release, revision)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "release": release,
        "revision": revision,
        "schemas": {},
    }
    payloads = {}
    for key, schema in (("public", public), ("application", application)):
        paths, operations = _schema_counts(schema)
        payload = encode_json(schema)
        payloads[FILES[key]] = payload
        manifest["schemas"][key] = {
            "file": FILES[key],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "paths": paths,
            "operations": operations,
        }
    directory.mkdir(parents=True, exist_ok=True)
    for filename, payload in payloads.items():
        (directory / filename).write_bytes(payload)
    (directory / "openapi-manifest.json").write_bytes(encode_json(manifest))


def validate_assets(directory: Path, release: str, revision: str) -> dict[str, Any]:
    """Fail closed on missing, malformed, misidentified or changed snapshots."""
    _identity(release, revision)
    manifest = json.loads((directory / "openapi-manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("schema_version"), manifest.get("release"), manifest.get("revision")) != (
        1,
        release,
        revision,
    ) or set(manifest.get("schemas", {})) != set(FILES):
        raise ValueError("manifest has the wrong format or release identity")
    for key, filename in FILES.items():
        entry = manifest["schemas"][key]
        if entry.get("file") != filename:
            raise ValueError("manifest contains an unexpected asset name")
        payload = (directory / filename).read_bytes()
        if hashlib.sha256(payload).hexdigest() != entry.get("sha256"):
            raise ValueError("schema checksum mismatch")
        paths, operations = _schema_counts(json.loads(payload))
        if (entry.get("paths"), entry.get("operations")) != (paths, operations):
            raise ValueError("schema counts mismatch")
    return manifest


def select_verified_image(
    documents: list[dict[str, Any]], release: str, revision: str, platform: str = "linux/amd64"
) -> dict[str, str]:
    """Select a verified immutable child image; reject conflicting retry receipts."""
    _identity(release, revision)
    candidates = []
    for document in documents:
        if (document.get("version"), document.get("revision")) != (release[1:], revision):
            raise ValueError("registry evidence has the wrong release identity")
        for registry in document.get("registries", []):
            if registry.get("status") != "verified":
                continue
            for image in registry.get("images", []):
                if image.get("role") != "fs-app":
                    continue
                reference = image.get("reference", "")
                namespace = registry.get("namespace", "")
                if (
                    not re.fullmatch(r"[a-z0-9./-]+", namespace)
                    or reference != f"{namespace}/flow-steward-app:{release[1:]}"
                ):
                    raise ValueError("unexpected application image reference")
                identity = image.get("platforms", {}).get(platform, {})
                manifest = identity.get("manifest_digest", "")
                config = identity.get("config_digest", "")
                if not all(
                    re.fullmatch(r"sha256:[a-f0-9]{64}", digest) for digest in (manifest, config)
                ):
                    raise ValueError("missing verified platform image identity")
                candidates.append(
                    {
                        "image": reference.rsplit(":", 1)[0] + "@" + manifest,
                        "config_digest": config,
                        "manifest_digest": manifest,
                        "platform": platform,
                    }
                )
    if not candidates:
        raise ValueError("no verified application image")
    if len({(item["manifest_digest"], item["config_digest"]) for item in candidates}) != 1:
        raise ValueError("conflicting verified application image receipts")
    return candidates[0]
