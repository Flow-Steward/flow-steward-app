"""Load bundle-local or bounded remote extension contract references."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from core.application.extensions.extension_contract_v2_common import (
    _error,
    _normalize_lower,
)


def _extract_contract_root(descriptor: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(descriptor, dict):
        return None
    root = descriptor.get("extension_contract_v2")
    if isinstance(root, dict):
        return copy.deepcopy(root)
    runtime = descriptor.get("runtime")
    if isinstance(runtime, dict):
        runtime_root = runtime.get("extension_contract_v2")
        if isinstance(runtime_root, dict):
            return copy.deepcopy(runtime_root)
    return None


def _load_section_payload(
    section: str,
    raw_value: Any,
    *,
    contract_root: dict[str, Any],
    bundle_root: Path | None,
    descriptor_url: str,
    url_fetcher: Callable[[str, float], str] | None,
    timeout_seconds: float,
    errors: list[dict[str, str]],
) -> Any:
    if raw_value in (None, ""):
        return []
    if isinstance(raw_value, dict) and "ref" in raw_value:
        resolved = _resolve_ref(
            str(raw_value.get("ref") or ""),
            contract_root=contract_root,
            bundle_root=bundle_root,
            descriptor_url=descriptor_url,
            url_fetcher=url_fetcher,
            timeout_seconds=timeout_seconds,
            errors=errors,
            path=f"extension_contract_v2.{section}.ref",
        )
        if resolved is None:
            return []
        return resolved
    if isinstance(raw_value, (dict, list)):
        return copy.deepcopy(raw_value)
    if not isinstance(raw_value, str):
        _error(
            errors,
            path=f"extension_contract_v2.{section}",
            message=f"{section} must be an object, list, or ref string",
            code="invalid_section_type",
        )
        return []

    resolved = _resolve_ref(
        raw_value,
        contract_root=contract_root,
        bundle_root=bundle_root,
        descriptor_url=descriptor_url,
        url_fetcher=url_fetcher,
        timeout_seconds=timeout_seconds,
        errors=errors,
        path=f"extension_contract_v2.{section}",
    )
    if resolved is None:
        return []
    return resolved


def _resolve_ref(
    ref: str,
    *,
    contract_root: dict[str, Any],
    bundle_root: Path | None,
    descriptor_url: str,
    url_fetcher: Callable[[str, float], str] | None,
    timeout_seconds: float,
    errors: list[dict[str, str]],
    path: str,
) -> Any | None:
    normalized_ref = str(ref or "").strip()
    if not normalized_ref:
        _error(errors, path=path, message="ref cannot be empty", code="empty_ref")
        return None

    if normalized_ref.startswith("snapshot://"):
        snapshot_key = normalized_ref[len("snapshot://") :].strip()
        snapshot_docs = contract_root.get("snapshot_documents")
        if isinstance(snapshot_docs, dict) and snapshot_key in snapshot_docs:
            return copy.deepcopy(snapshot_docs[snapshot_key])
        _error(
            errors,
            path=path,
            message=f"Snapshot ref '{normalized_ref}' is missing in snapshot_documents",
            code="snapshot_ref_missing",
        )
        return None

    if descriptor_url:
        resolved_from_url = _resolve_url_ref(
            normalized_ref,
            descriptor_url=descriptor_url,
            url_fetcher=url_fetcher,
            timeout_seconds=timeout_seconds,
            errors=errors,
            path=path,
        )
        if resolved_from_url is not None:
            return resolved_from_url
        return None

    if bundle_root is None:
        _error(
            errors,
            path=path,
            message=(
                f"Ref '{normalized_ref}' cannot be resolved without bundle_root or descriptor_url"
            ),
            code="ref_resolution_unavailable",
        )
        return None

    target = (bundle_root / normalized_ref).resolve()
    if not _path_is_inside(target, bundle_root):
        _error(
            errors,
            path=path,
            message=f"Ref '{normalized_ref}' points outside bundle root",
            code="ref_outside_bundle",
        )
        return None
    if not target.is_file():
        _error(
            errors,
            path=path,
            message=f"Ref '{normalized_ref}' could not be read",
            code="ref_unreadable",
        )
        return None
    try:
        return _parse_json_or_yaml_text(target.read_text(encoding="utf-8"), ref_hint=str(target))
    except Exception as exc:
        _error(
            errors,
            path=path,
            message=f"Ref '{normalized_ref}' parse failed: {exc}",
            code="ref_parse_failed",
        )
        return None


def _resolve_url_ref(
    ref: str,
    *,
    descriptor_url: str,
    url_fetcher: Callable[[str, float], str] | None,
    timeout_seconds: float,
    errors: list[dict[str, str]],
    path: str,
) -> Any | None:
    descriptor_parsed = urlparse(descriptor_url)
    if descriptor_parsed.scheme.lower() not in {"http", "https"}:
        _error(
            errors,
            path=path,
            message=f"Descriptor URL scheme '{descriptor_parsed.scheme}' is not supported",
            code="descriptor_url_scheme_invalid",
        )
        return None

    base_dir = descriptor_url.rsplit("/", 1)[0] + "/"
    candidate_url = urljoin(base_dir, ref)
    candidate_parsed = urlparse(candidate_url)

    same_origin = (
        candidate_parsed.scheme.lower() == descriptor_parsed.scheme.lower()
        and candidate_parsed.netloc == descriptor_parsed.netloc
    )
    if not same_origin:
        _error(
            errors,
            path=path,
            message=f"Ref '{ref}' points outside descriptor origin",
            code="ref_outside_bundle",
        )
        return None

    base_path = _normalized_url_path(base_dir)
    candidate_path = _normalized_url_path(candidate_url)
    if not candidate_path.startswith(base_path):
        _error(
            errors,
            path=path,
            message=f"Ref '{ref}' points outside descriptor bundle path",
            code="ref_outside_bundle",
        )
        return None

    try:
        if url_fetcher is None:
            _error(
                errors,
                path=path,
                message=f"Ref '{ref}' cannot be fetched without a safe URL fetcher",
                code="ref_unreadable",
            )
            return None
        raw = url_fetcher(candidate_url, timeout_seconds)
        return _parse_json_or_yaml_text(raw, ref_hint=candidate_url)
    except Exception as exc:
        _error(
            errors,
            path=path,
            message=f"Ref '{ref}' could not be fetched: {exc}",
            code="ref_unreadable",
        )
        return None


def _normalized_url_path(url_value: str) -> str:
    parsed = urlparse(url_value)
    path = parsed.path or "/"
    normalized = "/" + "/".join(part for part in path.split("/") if part and part != ".")
    if not normalized.endswith("/"):
        normalized = normalized.rsplit("/", 1)[0] + "/"
    return normalized


def _path_is_inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _parse_json_or_yaml_text(raw: str, *, ref_hint: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        import yaml
    except Exception as exc:
        raise ValueError(
            f"Expected JSON at '{ref_hint}' and PyYAML is unavailable for YAML parsing"
        ) from exc
    parsed = yaml.safe_load(text)
    if parsed is None:
        return {}
    return parsed


def _normalize_section(section: str, payload: Any) -> list[dict[str, Any]]:
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(row)
        if section == "dataset_connectors":
            runtime_kind = _normalize_lower(normalized.get("runtime_kind")) or "local_extension"
            normalized["runtime_kind"] = runtime_kind
        return normalized

    if isinstance(payload, list):
        return [_normalize_row(dict(item)) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    candidates = {
        "oauth_providers": ("oauth_providers", "providers", "items"),
        "connection_types": ("connection_types", "items"),
        "dataset_connectors": ("dataset_connectors", "connectors", "items"),
        "operation_manifest": ("operations", "operation_manifest", "items"),
        "step_ui_manifest": ("steps", "forms", "step_ui_manifest", "items"),
        "resource_query_manifest": ("queries", "resource_query_manifest", "items"),
        "event_bindings": ("bindings", "event_bindings", "items"),
        "artifact_policies": ("policies", "artifact_policies", "items"),
    }.get(section, ("items",))
    for key in candidates:
        value = payload.get(key)
        if isinstance(value, list):
            return [_normalize_row(dict(item)) for item in value if isinstance(item, dict)]
    return [_normalize_row(dict(payload))] if payload else []
