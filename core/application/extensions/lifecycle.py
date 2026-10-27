"""
Descriptor validation and remote artifact safety helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import (  # noqa: F401  (urlopen kept as a stable patch target for SSRF security tests)
    HTTPRedirectHandler,
    Request,
    urlopen,
)

from . import lifecycle_descriptor_io as _descriptor_io
from . import url_safety as _url_safety
from .extension_contract_v2 import validate_extension_contract_v2
from .extension_descriptor import (
    DEFAULT_FS_RUNTIME_VERSION,
    validate_extension_descriptor,
)
from .extension_projection_v2 import validate_canonical_event_bindings

_load_json_or_yaml_file = _descriptor_io._load_json_or_yaml_file
_descriptor_file_preflight_errors = _descriptor_io._descriptor_file_preflight_errors
_parse_json_or_yaml_text = _descriptor_io._parse_json_or_yaml_text
materialize_descriptor_artifacts = _descriptor_io.materialize_descriptor_artifacts
_allow_private_remote_urls = _descriptor_io._allow_private_remote_urls
_resolve_hostname_ips = _descriptor_io._resolve_hostname_ips
_is_blocked_ip = _descriptor_io._is_blocked_ip


def _resolve_bundle_path(
    bundle_root: Path,
    raw_path: str | Path,
    *,
    path_label: str,
    must_exist: bool = True,
    require_file: bool = False,
    require_dir: bool = False,
) -> Path:
    raw_text = str(raw_path or "").strip()
    if not raw_text:
        raise ValueError(f"{path_label} is required")
    candidate = Path(raw_text)
    if candidate.is_absolute():
        raise ValueError(f"{path_label} must be relative to the extension bundle")
    resolved_root = bundle_root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"{path_label} escapes the extension bundle")
    if must_exist and not resolved.exists():
        raise ValueError(f"{path_label} does not exist")
    if require_file and not resolved.is_file():
        raise ValueError(f"{path_label} must be a file")
    if require_dir and not resolved.is_dir():
        raise ValueError(f"{path_label} must be a directory")
    return resolved


# -- Artifact type <-> registry entity mapping --------------------------------

_ARTIFACT_TO_ENTITY: dict[str, str] = {
    "workflow": "workflow",
    "tool": "tool",
    "skill": "skill",
    "agent": "agent",
    "data_contract": "data_contract",
}

_ENTITY_ID_FIELDS: dict[str, str] = {
    "workflow": "workflow_id",
    "tool": "tool_id",
    "skill": "skill_id",
    "agent": "agent_id",
    "data_contract": "contract_id",
}


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Descriptor validate (extension_descriptor_v1)
# ---------------------------------------------------------------------------


def validate_extension_descriptor_file(
    descriptor_file: str | Path,
    *,
    platform_version: str = DEFAULT_FS_RUNTIME_VERSION,
    allow_insecure_remote: bool = False,
) -> dict[str, Any]:
    """Validate a ``extension_descriptor_v1`` document loaded from a local file."""
    descriptor_path = Path(descriptor_file)
    if not descriptor_path.is_file():
        return {
            "valid": False,
            "descriptor": None,
            "normalized": None,
            "errors": [
                {
                    "path": "descriptor",
                    "message": f"Descriptor file '{descriptor_path}' does not exist",
                }
            ],
            "warnings": [],
        }
    preflight_errors = _descriptor_file_preflight_errors(descriptor_path)
    if preflight_errors:
        return {
            "valid": False,
            "descriptor": None,
            "normalized": None,
            "errors": preflight_errors,
            "warnings": [],
        }
    try:
        descriptor_data = _load_json_or_yaml_file(descriptor_path)
    except Exception as exc:
        return {
            "valid": False,
            "descriptor": None,
            "normalized": None,
            "errors": [
                {
                    "path": "descriptor",
                    "code": "descriptor_parse_error",
                    "message": f"Could not parse descriptor file '{descriptor_path}': {exc}",
                }
            ],
            "warnings": [],
        }

    descriptor_data, materialization_errors = materialize_descriptor_artifacts(
        descriptor_data, bundle_root=descriptor_path.parent
    )
    if materialization_errors:
        return {
            "valid": False,
            "descriptor": None,
            "normalized": None,
            "errors": materialization_errors,
            "warnings": [],
        }
    result = validate_extension_descriptor(
        descriptor_data,
        platform_version=platform_version,
        allow_insecure_remote=allow_insecure_remote,
        artifact_resolver=lambda _ref, _section, _idx, entry: bool(entry.snapshot),
    )
    _apply_extension_contract_v2_validation(
        result,
        bundle_root=descriptor_path.parent,
    )
    result["warnings"] = []
    return result


def validate_extension_descriptor_url(
    descriptor_url: str,
    *,
    platform_version: str = DEFAULT_FS_RUNTIME_VERSION,
    allow_insecure_remote: bool = False,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Validate a ``extension_descriptor_v1`` document loaded from URL."""
    normalized_url = str(descriptor_url or "").strip()
    if not normalized_url:
        return {
            "valid": False,
            "descriptor": None,
            "normalized": None,
            "errors": [{"path": "descriptor_url", "message": "descriptor_url is required"}],
            "warnings": [],
        }

    try:
        raw_text = _fetch_url_text(normalized_url, timeout_seconds=timeout_seconds)
        descriptor_data = _parse_json_or_yaml_text(raw_text)
    except Exception as exc:
        return {
            "valid": False,
            "descriptor": None,
            "normalized": None,
            "errors": [
                {
                    "path": "descriptor_url",
                    "message": f"Could not load descriptor from '{normalized_url}': {exc}",
                }
            ],
            "warnings": [],
        }

    remote_artifact_errors: list[dict[str, str]] = []
    artifact_sections = descriptor_data.get("artifacts")
    if isinstance(artifact_sections, dict):
        for section, entries in artifact_sections.items():
            if not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                if isinstance(entry, dict) and str(entry.get("ref") or "").strip():
                    remote_artifact_errors.append(
                        {
                            "path": f"artifacts.{section}[{index}].ref",
                            "code": "artifact_ref_not_bundle_local",
                            "message": (
                                "Artifact refs require a bundle-local descriptor; "
                                "remote descriptors must include immutable snapshots"
                            ),
                        }
                    )
    if remote_artifact_errors:
        return {
            "valid": False,
            "descriptor": None,
            "normalized": None,
            "errors": remote_artifact_errors,
            "warnings": [],
        }

    result = validate_extension_descriptor(
        descriptor_data,
        platform_version=platform_version,
        allow_insecure_remote=allow_insecure_remote,
        artifact_resolver=lambda _ref, _section, _idx, _entry: False,
    )
    _apply_extension_contract_v2_validation(
        result,
        descriptor_url=normalized_url,
        timeout_seconds=timeout_seconds,
    )
    result["warnings"] = []
    return result


def validate_extension_descriptor_builtin(
    descriptor_source: dict[str, Any] | str | Path,
    *,
    source_root: str | Path | None = None,
    platform_version: str = DEFAULT_FS_RUNTIME_VERSION,
    allow_insecure_remote: bool = False,
) -> dict[str, Any]:
    """Validate a built-in/system ``extension_descriptor_v1`` source.

    ``descriptor_source`` may be an already-loaded dict or a local descriptor file
    path. Artifact refs are resolved from ``source_root`` when provided.
    """
    descriptor_data: dict[str, Any]
    resolved_root: Path | None = Path(source_root) if source_root else None

    if isinstance(descriptor_source, dict):
        descriptor_data = descriptor_source
    else:
        descriptor_path = Path(descriptor_source)
        if not descriptor_path.is_file():
            return {
                "valid": False,
                "descriptor": None,
                "normalized": None,
                "errors": [
                    {
                        "path": "descriptor",
                        "message": f"Built-in descriptor '{descriptor_path}' does not exist",
                    }
                ],
                "warnings": [],
            }
        try:
            descriptor_data = _load_json_or_yaml_file(descriptor_path)
        except Exception as exc:
            return {
                "valid": False,
                "descriptor": None,
                "normalized": None,
                "errors": [
                    {
                        "path": "descriptor",
                        "message": f"Could not parse built-in descriptor '{descriptor_path}': {exc}",
                    }
                ],
                "warnings": [],
            }
        if resolved_root is None:
            resolved_root = descriptor_path.parent

    if resolved_root is not None:
        descriptor_data, materialization_errors = materialize_descriptor_artifacts(
            descriptor_data, bundle_root=resolved_root
        )
        if materialization_errors:
            return {
                "valid": False,
                "descriptor": None,
                "normalized": None,
                "errors": materialization_errors,
                "warnings": [],
            }
    result = validate_extension_descriptor(
        descriptor_data,
        platform_version=platform_version,
        allow_insecure_remote=allow_insecure_remote,
        artifact_resolver=lambda _ref, _section, _idx, entry: bool(entry.snapshot),
    )
    _apply_extension_contract_v2_validation(
        result,
        bundle_root=resolved_root,
    )
    result["warnings"] = []
    return result


def _apply_extension_contract_v2_validation(
    result: dict[str, Any],
    *,
    bundle_root: str | Path | None = None,
    descriptor_url: str = "",
    timeout_seconds: float = 15.0,
) -> None:
    normalized = result.get("normalized")
    if not isinstance(normalized, dict):
        return

    validation = validate_extension_contract_v2(
        normalized,
        bundle_root=bundle_root,
        descriptor_url=descriptor_url,
        url_fetcher=lambda url, timeout: _fetch_url_text(url, timeout_seconds=timeout),
        timeout_seconds=timeout_seconds,
    )
    extension_contract_status = {
        "present": bool(validation.get("present")),
        "valid": bool(validation.get("valid")),
        "preview": validation.get("preview", {}),
    }
    result["extension_contract_v2"] = extension_contract_status

    canonical_errors: list[dict[str, str]] = []
    # Dictionaries belong to the release with or without a contract.
    locale_bundles = validation.get("locale_bundles")
    if locale_bundles:
        normalized["locale_bundles"] = locale_bundles
    normalized_contract = validation.get("normalized_contract")
    if validation.get("present") and isinstance(normalized_contract, dict):
        normalized["extension_contract_v2_normalized"] = normalized_contract
        canonical_errors = validate_canonical_event_bindings(normalized_contract)
        if canonical_errors:
            extension_contract_status["valid"] = False
            preview = extension_contract_status.get("preview")
            if not isinstance(preview, dict):
                preview = {}
                extension_contract_status["preview"] = preview
            preview_errors = list(preview.get("errors") or [])
            for issue in canonical_errors:
                event_path = str(issue.get("path") or "").strip()
                prefixed_path = (
                    f"extension_contract_v2.{event_path}"
                    if event_path
                    else "extension_contract_v2.event_bindings"
                )
                message = str(issue.get("message") or "Unknown canonical event").strip()
                canonical_issue = {
                    "path": prefixed_path,
                    "message": message,
                    "code": "unknown_canonical_event",
                }
                result.setdefault("errors", []).append(canonical_issue)
                preview_errors.append(canonical_issue)
            preview["errors"] = sorted(
                preview_errors,
                key=lambda row: (
                    str(row.get("path") or ""),
                    str(row.get("code") or ""),
                    str(row.get("message") or ""),
                ),
            )

    errors = validation.get("errors", [])
    if errors:
        result.setdefault("errors", []).extend(errors)
        result["valid"] = False
    if canonical_errors:
        result["valid"] = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _ = (req, fp, code, msg, headers, newurl)


def _open_url_no_redirect(request: Request, *, timeout_seconds: float):
    """Open *request* with no redirects, pinning the socket to a pre-validated IP.

    Resolving + validating the host once and forcing the connection to one of
    those exact IPs (while preserving Host header + TLS SNI) defeats DNS
    rebinding between preflight validation and the actual connect.
    """
    return _url_safety.open_pinned_url(
        request, timeout_seconds=timeout_seconds, purpose="remote URL"
    )


def _fetch_url_text(url: str, *, timeout_seconds: float = 20.0) -> str:
    safe_url = _url_safety.assert_safe_remote_http_url(url, purpose="descriptor URL")
    request = Request(
        safe_url,
        headers={"Accept": "application/json, application/yaml, text/yaml, text/plain"},
    )
    with _open_url_no_redirect(  # nosec B310
        request, timeout_seconds=timeout_seconds
    ) as response:
        body = response.read()
    return body.decode("utf-8")


def _probe_url_exists(url: str, *, timeout_seconds: float = 10.0) -> bool:
    safe_url = _url_safety.assert_safe_remote_http_url(url, purpose="artifact URL")
    try:
        request = Request(safe_url, method="HEAD")
        with _open_url_no_redirect(  # nosec B310
            request, timeout_seconds=timeout_seconds
        ):
            return True
    except HTTPError as exc:
        if 300 <= int(exc.code) < 400:
            return False
        if exc.code not in (405, 501):
            return 200 <= int(exc.code) < 400
    except URLError:
        return False

    try:
        request = Request(safe_url, method="GET")
        with _open_url_no_redirect(  # nosec B310
            request, timeout_seconds=timeout_seconds
        ):
            return True
    except Exception:
        return False


def _resolve_artifact_ref_from_file(ref: str, *, base_dir: Path) -> bool:
    parsed = urlparse(ref)
    if parsed.scheme in {"http", "https"}:
        return _probe_url_exists(ref)
    if parsed.scheme:
        return False
    candidate = Path(ref)
    if candidate.is_absolute():
        return False
    try:
        return _resolve_bundle_path(
            base_dir,
            candidate,
            path_label=f"Artifact ref '{ref}'",
        ).exists()
    except ValueError:
        return False


def _resolve_artifact_ref_from_url(
    ref: str,
    *,
    descriptor_url: str,
    timeout_seconds: float,
) -> bool:
    parsed = urlparse(ref)
    if parsed.scheme in {"http", "https"}:
        target_url = ref
    elif parsed.scheme:
        return False
    else:
        target_url = urljoin(descriptor_url, ref)
    return _probe_url_exists(target_url, timeout_seconds=timeout_seconds)


def _resolve_artifact_ref_from_builtin(ref: str, *, source_root: Path | None) -> bool:
    parsed = urlparse(ref)
    if parsed.scheme in {"http", "https"}:
        return _probe_url_exists(ref)
    if parsed.scheme:
        return False
    if source_root is None:
        return False
    candidate = Path(ref)
    if candidate.is_absolute():
        return False
    try:
        return _resolve_bundle_path(
            source_root,
            candidate,
            path_label=f"Artifact ref '{ref}'",
        ).exists()
    except ValueError:
        return False
