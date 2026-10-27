"""Descriptor file and remote URL helpers for extension lifecycle operations."""

from __future__ import annotations

import ipaddress
import json
import socket
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_EXTENSION_DESCRIPTOR_ARCHIVE_EXTENSIONS = {
    ".zip",
    ".tar",
    ".tgz",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
}
_DEFAULT_EXTENSION_DESCRIPTOR_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_EXTENSION_ARTIFACT_MAX_BYTES = 2 * 1024 * 1024
_MAX_EXTENSION_ARTIFACT_DEPTH = 32
_MAX_EXTENSION_ARTIFACT_NODES = 100_000
_ARTIFACT_ID_FIELDS = (
    "workflow_id",
    "agent_id",
    "skill_id",
    "tool_id",
    "dataset_id",
    "query_template_id",
    "saved_query_id",
    "contract_id",
    "data_contract_id",
    "guardrail_id",
    "extension_id",
    "id",
)
_ARTIFACT_ID_FIELDS_BY_SECTION = {
    "workflows": ("workflow_id", "id"),
    "agents": ("agent_id", "id"),
    "skills": ("skill_id", "id"),
    "tools": ("tool_id", "id"),
    "datasets": ("dataset_id", "id"),
    "saved_queries": ("query_template_id", "saved_query_id", "id"),
    "data_contracts": ("contract_id", "data_contract_id", "id"),
    "guardrails": ("guardrail_id", "id"),
    "extensions": ("extension_id", "id"),
}


def _load_json_or_yaml_file(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    return _parse_json_or_yaml_text(raw)


def _descriptor_file_max_bytes() -> int:
    raw = str(_runtime_env.get("FS_EXTENSION_DESCRIPTOR_MAX_BYTES") or "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = _DEFAULT_EXTENSION_DESCRIPTOR_MAX_BYTES
        if parsed > 0:
            return parsed
    return _DEFAULT_EXTENSION_DESCRIPTOR_MAX_BYTES


def _detect_archive_kind(path: Path) -> str:
    suffixes = [str(suffix or "").lower() for suffix in path.suffixes]
    suffix_chain = "".join(suffixes)
    for ext in _EXTENSION_DESCRIPTOR_ARCHIVE_EXTENSIONS:
        if suffix_chain.endswith(ext):
            return ext.lstrip(".")

    header = b""
    try:
        with path.open("rb") as handle:
            header = handle.read(512)
    except Exception:
        return ""
    if header.startswith(b"PK\x03\x04"):
        return "zip"
    if header.startswith(b"\x1f\x8b"):
        return "gzip"
    if header.startswith(b"BZh"):
        return "bzip2"
    if header.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if header.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if len(header) >= 262 and header[257:262] == b"ustar":
        return "tar"
    return ""


def _descriptor_file_preflight_errors(path: Path) -> list[dict[str, str]]:
    archive_kind = _detect_archive_kind(path)
    if archive_kind:
        return [
            {
                "path": "descriptor",
                "code": "archive_not_supported",
                "message": (
                    "Descriptor import expects a JSON/YAML descriptor file and does not accept "
                    f"archive inputs ({archive_kind})"
                ),
            }
        ]

    try:
        size_bytes = int(path.stat().st_size)
    except OSError as exc:
        return [
            {
                "path": "descriptor",
                "code": "descriptor_stat_error",
                "message": f"Could not inspect descriptor file '{path}': {exc}",
            }
        ]

    max_bytes = _descriptor_file_max_bytes()
    if max_bytes > 0 and size_bytes > max_bytes:
        return [
            {
                "path": "descriptor",
                "code": "descriptor_file_too_large",
                "message": (
                    f"Descriptor file '{path}' exceeds the maximum allowed size of {max_bytes} bytes"
                ),
            }
        ]
    return []


def _parse_json_or_yaml_text(raw: str) -> dict[str, Any]:
    text = str(raw or "")
    try:
        parsed = json.loads(text)
    except Exception:
        try:
            import yaml
        except ImportError as exc:
            raise ValueError("YAML parsing requires PyYAML or valid JSON input") from exc
        parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ValueError("Descriptor must decode to an object")
    return parsed


def _bounded_artifact_structure(value: Any) -> None:
    node_count = 0
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        node_count += 1
        if node_count > _MAX_EXTENSION_ARTIFACT_NODES:
            raise ValueError("artifact exceeds the maximum structure size")
        if depth > _MAX_EXTENSION_ARTIFACT_DEPTH:
            raise ValueError("artifact exceeds the maximum nesting depth")
        if isinstance(current, dict):
            pending.extend((key, depth + 1) for key in current)
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)


def _artifact_document_id(document: dict[str, Any], *, section: str) -> str:
    for field in _ARTIFACT_ID_FIELDS_BY_SECTION.get(section, _ARTIFACT_ID_FIELDS):
        value = str(document.get(field) or "").strip()
        if value:
            return value
    return ""


def materialize_descriptor_artifacts(
    descriptor: dict[str, Any], *, bundle_root: Path
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Load immutable bundle-local artifact snapshots before persistence."""
    normalized = deepcopy(descriptor)
    artifacts = normalized.get("artifacts")
    if not isinstance(artifacts, dict):
        return normalized, []

    errors: list[dict[str, str]] = []
    root = bundle_root.resolve()
    for section, raw_entries in artifacts.items():
        if not isinstance(raw_entries, list):
            continue
        for index, raw_entry in enumerate(raw_entries):
            if not isinstance(raw_entry, dict):
                continue
            ref = str(raw_entry.get("ref") or "").strip()
            if not ref:
                continue
            error_path = f"artifacts.{section}[{index}].ref"
            candidate_ref = Path(ref)
            if urlparse(ref).scheme or candidate_ref.is_absolute():
                errors.append(
                    {
                        "path": error_path,
                        "code": "artifact_ref_not_bundle_local",
                        "message": "Artifact ref must be a relative bundle-local path",
                    }
                )
                continue
            if candidate_ref.suffix.lower() not in {".yaml", ".yml", ".json"}:
                errors.append(
                    {
                        "path": error_path,
                        "code": "artifact_ref_type_not_allowed",
                        "message": "Artifact ref must use .yaml, .yml, or .json",
                    }
                )
                continue
            try:
                artifact_path = (root / candidate_ref).resolve(strict=True)
                artifact_path.relative_to(root)
            except (OSError, ValueError):
                errors.append(
                    {
                        "path": error_path,
                        "code": "artifact_ref_missing_or_escaping",
                        "message": f"Artifact ref '{ref}' is missing or escapes the bundle",
                    }
                )
                continue
            try:
                size = artifact_path.stat().st_size
                if size > _DEFAULT_EXTENSION_ARTIFACT_MAX_BYTES:
                    raise ValueError(
                        f"artifact exceeds {_DEFAULT_EXTENSION_ARTIFACT_MAX_BYTES} bytes"
                    )
                document = _load_json_or_yaml_file(artifact_path)
                _bounded_artifact_structure(document)
            except Exception as exc:
                errors.append(
                    {
                        "path": error_path,
                        "code": "artifact_parse_error",
                        "message": f"Could not materialize artifact '{ref}': {exc}",
                    }
                )
                continue
            declared_id = ""
            for field in _ARTIFACT_ID_FIELDS_BY_SECTION.get(section, _ARTIFACT_ID_FIELDS):
                declared_id = str(raw_entry.get(field) or "").strip()
                if declared_id:
                    break
            snapshot_id = _artifact_document_id(document, section=section)
            if not snapshot_id or snapshot_id != declared_id:
                errors.append(
                    {
                        "path": error_path,
                        "code": "artifact_id_mismatch",
                        "message": (
                            f"Artifact '{ref}' declares id '{snapshot_id or '<missing>'}', "
                            f"expected '{declared_id}'"
                        ),
                    }
                )
                continue
            raw_entry["snapshot"] = document
    return normalized, errors


def _allow_private_remote_urls() -> bool:
    return str(_runtime_env.get("FS_ALLOW_PRIVATE_REMOTE_URLS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _resolve_hostname_ips(hostname: str, *, port: int) -> set[str]:
    resolved: set[str] = set()
    infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    for info in infos:
        sockaddr = info[4] if len(info) > 4 else ()
        if sockaddr:
            resolved.add(str(sockaddr[0] or "").strip())
    return resolved


def _is_blocked_ip(address: str) -> bool:
    ip = ipaddress.ip_address(str(address or "").split("%", 1)[0].strip())
    return not ip.is_global
