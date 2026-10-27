"""Shared host-side artifact/dataset grant preparation for extension runtimes."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from core.application.auth.principal import coerce_optional_project_id
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

_TOKEN_APPLICATION_OCTET_STREAM = "application/octet-stream"
_TOKEN_APPLICATION_JSON = "application/json"

DEFAULT_GRANT_OUTPUT_MAX_SIZE_BYTES = 100 * 1024 * 1024
FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV = "FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS"
SUPPORTED_EXTENSION_GRANT_URL_SCHEMES = {"file", "http", "https"}
SUPPORTED_GRANT_RETENTION_CLASSES = {"ephemeral", "run_window", "audit", "persistent"}
# The path the platform serves an object on when storage cannot hand out a URL.
PLATFORM_SERVED_ARTIFACT_PATH = "/api/extension-artifacts"

_KINDS = {"artifact", "dataset"}


@dataclass(slots=True)
class PreparedExtensionGrants:
    artifact_inputs: list[dict[str, Any]] = field(default_factory=list)
    artifact_outputs: list[dict[str, Any]] = field(default_factory=list)
    dataset_inputs: list[dict[str, Any]] = field(default_factory=list)
    dataset_outputs: list[dict[str, Any]] = field(default_factory=list)
    artifact_output_ids: list[str] = field(default_factory=list)
    dataset_output_ids: list[str] = field(default_factory=list)
    minted_artifact_output_ids: list[str] = field(default_factory=list)
    minted_dataset_output_ids: list[str] = field(default_factory=list)
    artifact_output_max_size_by_id: dict[str, int] = field(default_factory=dict)
    dataset_output_max_size_by_id: dict[str, int] = field(default_factory=dict)
    produced_artifacts: list[dict[str, Any]] = field(default_factory=list)
    produced_datasets: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class FinalizedExtensionGrants:
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    datasets: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    produced_artifacts: list[dict[str, Any]] = field(default_factory=list)
    produced_datasets: list[dict[str, Any]] = field(default_factory=list)


def _normalize_kind(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized not in _KINDS:
        raise ValueError(f"Unsupported extension grant kind '{kind}'")
    return normalized


def _id_field(kind: str) -> str:
    return f"{_normalize_kind(kind)}_id"


def _handle_field(kind: str) -> str:
    return f"{_normalize_kind(kind)}_handle"


def _content_type_for_kind(kind: str, meta: dict[str, Any]) -> str:
    if _normalize_kind(kind) == "dataset":
        return (
            str(meta.get("content_type") or _TOKEN_APPLICATION_JSON).strip()
            or _TOKEN_APPLICATION_JSON
        )
    return (
        str(meta.get("content_type") or _TOKEN_APPLICATION_OCTET_STREAM).strip()
        or _TOKEN_APPLICATION_OCTET_STREAM
    )


def normalize_grant_id(value: Any, *, kind: str) -> str:
    normalized = str(value or "").strip()
    prefix = f"{_normalize_kind(kind)}:"
    if normalized.startswith(prefix):
        return normalized.split(":", 1)[1].strip()
    return normalized


def positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def output_max_size(value: Any = None) -> int:
    parsed = positive_int(value)
    return parsed if parsed is not None else DEFAULT_GRANT_OUTPUT_MAX_SIZE_BYTES


def output_max_size_from_spec(spec: dict[str, Any]) -> int:
    for key in ("max_size_bytes", "max_bytes", "size_limit_bytes"):
        parsed = positive_int(spec.get(key))
        if parsed is not None:
            return parsed
    return DEFAULT_GRANT_OUTPUT_MAX_SIZE_BYTES


def retention_class_from_spec(spec: dict[str, Any]) -> str:
    retention = str(spec.get("retention_class") or spec.get("retention") or "").strip().lower()
    if not retention:
        return "run_window"
    if retention not in SUPPORTED_GRANT_RETENTION_CLASSES:
        raise ServiceValidationError(f"Unsupported retention_class '{retention}'")
    return retention


def trusted_local_fs_artifact_grants_enabled() -> bool:
    value = (
        str(_runtime_env.get(FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV) or "")
        .strip()
        .lower()
    )
    return value in {"1", "true", "yes", "on"}


def require_safe_grant_url(url: str) -> None:
    scheme = str(urlparse(str(url or "")).scheme or "").strip().lower()
    if scheme == "file" and not trusted_local_fs_artifact_grants_enabled():
        raise ServiceUnavailableError(
            "Local filesystem artifact grants require "
            f"{FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV}=1 and are trusted "
            "local/dev only"
        )
    if scheme not in SUPPORTED_EXTENSION_GRANT_URL_SCHEMES:
        raise ServiceUnavailableError(
            f"Artifact grant URL scheme '{scheme or 'none'}' is not supported for "
            "extension subprocesses"
        )


def _getter_name(kind: str) -> str:
    return f"get_{_normalize_kind(kind)}"


def _presign_name(kind: str) -> str:
    return f"{_normalize_kind(kind)}_presigned_url"


def _upload_stat_name(kind: str) -> str:
    return f"{_normalize_kind(kind)}_upload_stat"


def _finalize_name(kind: str) -> str:
    return f"finalize_{_normalize_kind(kind)}_upload"


def _create_from_spec_name(kind: str) -> str:
    return "create_artifact" if kind == "artifact" else "create_dataset_grant"


def meta_for_grant(plane: Any, object_id: str, *, kind: str) -> dict[str, Any]:
    getter = getattr(plane, _getter_name(kind), None)
    meta = getter(object_id) if callable(getter) else None
    if not isinstance(meta, dict):
        raise ServiceForbiddenError(f"{_normalize_kind(kind).title()} access denied")
    return dict(meta)


def require_grant_ownership(
    plane: Any,
    object_ids: list[str],
    *,
    kind: str,
    scope: Any,
) -> None:
    caller_account_id = str(getattr(scope, "account_id", "") or "").strip()
    allowed_project_ids = getattr(scope, "allowed_project_ids", None) or frozenset()
    for raw_id in object_ids:
        object_id = normalize_grant_id(raw_id, kind=kind)
        if not object_id:
            raise ServiceForbiddenError(f"{_normalize_kind(kind).title()} access denied")
        meta = meta_for_grant(plane, object_id, kind=kind)
        owner_scope = meta.get("owner_scope") if isinstance(meta, dict) else None
        owner_scope = owner_scope if isinstance(owner_scope, dict) else {}
        owner_account_id = str(owner_scope.get("account_id") or "").strip()
        if not caller_account_id or owner_account_id != caller_account_id:
            raise ServiceForbiddenError(f"{_normalize_kind(kind).title()} access denied")
        if allowed_project_ids:
            owner_project_id = coerce_optional_project_id(owner_scope.get("project_id"))
            if owner_project_id is not None and owner_project_id not in allowed_project_ids:
                raise ServiceForbiddenError(f"{_normalize_kind(kind).title()} access denied")


def require_disjoint_grants(
    input_ids: list[str],
    output_ids: list[str],
    *,
    kind: str,
) -> None:
    normalized_kind = _normalize_kind(kind)
    inputs = {
        normalize_grant_id(item, kind=normalized_kind)
        for item in input_ids
        if normalize_grant_id(item, kind=normalized_kind)
    }
    outputs = {
        normalize_grant_id(item, kind=normalized_kind)
        for item in output_ids
        if normalize_grant_id(item, kind=normalized_kind)
    }
    overlap = sorted(inputs & outputs)
    if overlap:
        raise ServiceValidationError(
            f"Output {normalized_kind}_id must differ from input {normalized_kind}_id "
            f"({normalized_kind} bodies are immutable; create a new output {normalized_kind})"
        )


def require_empty_output_target(
    plane: Any,
    object_id: str,
    *,
    kind: str,
    meta: dict[str, Any] | None = None,
) -> None:
    normalized_kind = _normalize_kind(kind)
    current = dict(meta) if isinstance(meta, dict) else meta_for_grant(plane, object_id, kind=kind)
    if (
        str(current.get("upload_status") or "").strip().lower() == "rejected"
        or str(current.get("upload_error") or "").strip()
        or str(current.get("sha256") or "").strip()
    ):
        raise ServiceValidationError(
            f"Output {normalized_kind}_id must reference an empty unfinalized "
            f"{normalized_kind} ({normalized_kind} bodies are immutable; create a new output "
            f"{normalized_kind})"
        )
    stat_fn = getattr(plane, _upload_stat_name(kind), None)
    if not callable(stat_fn):
        return
    try:
        stat = stat_fn(object_id)
    except Exception as exc:
        raise ServiceUnavailableError(f"{normalized_kind.title()} access is not available") from exc
    if isinstance(stat, dict):
        raise ServiceValidationError(
            f"Output {normalized_kind}_id must reference an empty unfinalized "
            f"{normalized_kind} ({normalized_kind} bodies are immutable; create a new output "
            f"{normalized_kind})"
        )


@dataclass(frozen=True, slots=True)
class GrantTarget:
    """Where an extension fetches an object, and what it must send to be let in."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)


def _storage_offered_url(plane: Any, object_id: str, *, kind: str, method: str, meta: Any) -> str:
    """The URL this deployment's storage can hand out, or "" when it has none.

    A URL the extension could not use is the same answer as no URL. That includes
    `file://` unless the operator has deliberately trusted it, which is the one case
    where a path is accepted as a capability and stays available here.
    """
    presigner = getattr(plane, _presign_name(kind), None)
    candidates = []
    if callable(presigner):
        candidates.append(str(presigner(object_id, method=method) or "").strip())
    object_key = str(meta.get("object_key") or "").strip()
    storage = getattr(plane, "storage", None)
    if object_key and storage is not None and hasattr(storage, "presign"):
        candidates.append(str(storage.presign(object_key, method=method) or "").strip())
    for candidate in candidates:
        if not candidate:
            continue
        scheme = str(urlparse(candidate).scheme or "").strip().lower()
        if scheme in {"http", "https"}:
            return candidate
        if scheme == "file" and trusted_local_fs_artifact_grants_enabled():
            return candidate
    return ""


def grant_target(plane: Any, object_id: str, *, kind: str, method: str) -> GrantTarget:
    """Resolve how this caller reaches the object, serving it ourselves if we must.

    AC17-R123. The question is never which storage backend is configured, it is
    whether a URL came back that *this* caller can fetch. An extension subprocess runs
    inside the deployment, so an internal address is perfectly usable to it; what it
    cannot use is a filesystem path. When nothing usable comes back the platform
    serves the object itself, and the token that authorizes that request travels in
    the grant's required headers -- a channel the SDK already verifies and pins to the
    URL, so it cannot be replayed against another one.
    """
    normalized_kind = _normalize_kind(kind)
    safe_method = str(method or "GET").strip().upper()
    meta = meta_for_grant(plane, object_id, kind=normalized_kind)
    if safe_method == "GET":
        if (
            str(meta.get("upload_status") or "").strip().lower() == "rejected"
            or str(meta.get("upload_error") or "").strip()
            or not str(meta.get("sha256") or "").strip()
        ):
            raise ServiceUnavailableError(f"{normalized_kind.title()} content is not available")
    elif safe_method == "PUT":
        require_empty_output_target(plane, object_id, kind=normalized_kind, meta=meta)

    offered = _storage_offered_url(
        plane,
        object_id,
        kind=normalized_kind,
        method=safe_method,
        meta=meta,
    )
    if offered:
        require_safe_grant_url(offered)
        return GrantTarget(url=offered)
    return _platform_served_target(object_id, kind=normalized_kind, method=safe_method)


def _platform_served_target(object_id: str, *, kind: str, method: str) -> GrantTarget:
    from core.infrastructure.security.artifact_access_tokens import (
        HEADER_NAME,
        issue_artifact_access_token,
    )

    base = str(_runtime_env.get("FS_APP_INTERNAL_URL") or "").strip().rstrip("/")
    if not base:
        # Naming the setting rather than minting an address to somewhere that does not
        # answer: a grant pointing nowhere fails as a network error, which reads as an
        # infrastructure fault instead of a deployment that was never told its own
        # address. There is no default worth guessing -- the one compiled in elsewhere
        # is a Scalable service name and is wrong on every other profile.
        raise ServiceUnavailableError(
            f"{kind.title()} access needs FS_APP_INTERNAL_URL: this deployment's storage "
            "cannot hand an extension a URL, so the platform has to serve the object "
            "itself, and it has not been told the address to serve it from"
        )
    token = issue_artifact_access_token(object_id=object_id, kind=kind, method=method)
    if not token:
        raise ServiceUnavailableError(
            f"{kind.title()} access needs FS_SECRET_KEY: the platform has to serve this "
            "object itself and cannot authorize the request without it"
        )
    url = f"{base}{PLATFORM_SERVED_ARTIFACT_PATH}/{kind}/{object_id}"
    require_safe_grant_url(url)
    return GrantTarget(url=url, headers={HEADER_NAME: token})


def presigned_url(plane: Any, object_id: str, *, kind: str, method: str) -> str:
    return grant_target(plane, object_id, kind=kind, method=method).url


def build_descriptor_for_invocation(
    plane: Any,
    object_id: str,
    *,
    kind: str,
    role: str,
    binding_key: str,
    audit: dict[str, Any],
    max_size_bytes: int | None = None,
    meta_override: dict[str, Any] | None = None,
    url_override: str = "",
) -> dict[str, Any]:
    from core.application.extensions.remote_runtime_protocol_v2 import (
        build_artifact_access_descriptor,
    )

    normalized_kind = _normalize_kind(kind)
    normalized_role = "input" if str(role or "").strip().lower() == "input" else "output"
    meta = (
        dict(meta_override)
        if isinstance(meta_override, dict)
        else meta_for_grant(plane, object_id, kind=normalized_kind)
    )
    mode = "read" if normalized_role == "input" else "write"
    method = "GET" if normalized_role == "input" else "PUT"
    override = str(url_override or "").strip()
    target = (
        GrantTarget(url=override)
        if override
        else grant_target(plane, object_id, kind=normalized_kind, method=method)
    )
    url = target.url
    size_bytes = meta.get("size_bytes") if normalized_role == "input" else None
    grant_max_size = size_bytes if normalized_role == "input" else output_max_size(max_size_bytes)
    id_field = _id_field(normalized_kind)
    object_key = str(meta.get("object_key") or "").strip()
    descriptor = build_artifact_access_descriptor(
        binding_key=binding_key,
        role=normalized_role,
        artifact_id=object_id,
        artifact_handle=f"{normalized_kind}:{object_id}",
        artifact_kind=normalized_kind,
        filename=str(meta.get("filename") or ("rows.json" if normalized_kind == "dataset" else "")),
        content_type=_content_type_for_kind(normalized_kind, meta),
        size_bytes=size_bytes,
        checksum_sha256=str(meta.get("sha256") or "") if normalized_role == "input" else "",
        retention_class=str(meta.get("retention_class") or "run_window"),
        transport="presigned_url",
        mode=mode,
        download_url=url if normalized_role == "input" else "",
        upload_url=url if normalized_role == "output" else "",
        # The platform's own access token rides here when it is serving the object
        # itself. required_headers is signed into the grant's claims and the SDK
        # refuses a request whose headers do not match, so the token is pinned to this
        # URL and this method rather than being a bearer credential in its own right.
        required_headers={
            **({"If-None-Match": "*"} if normalized_role == "output" else {}),
            **target.headers,
        },
        single_use=normalized_role == "output",
        max_size_bytes=grant_max_size,
        scope=audit,
    )
    if urlparse(url).scheme in {"http", "https"}:
        from core.infrastructure.security.artifact_grant_signing import (
            sign_platform_artifact_grant,
        )

        descriptor = sign_platform_artifact_grant(descriptor)
    descriptor[id_field] = object_id
    descriptor[_handle_field(normalized_kind)] = f"{normalized_kind}:{object_id}"
    descriptor["grant_kind"] = normalized_kind
    if object_key:
        descriptor["object_key"] = object_key
    if normalized_kind == "dataset":
        descriptor["schema"] = dict(meta.get("schema") or {})
        if "row_count" in meta:
            descriptor["row_count"] = meta.get("row_count")
    return descriptor


def create_output_from_spec(
    plane: Any,
    spec: dict[str, Any],
    *,
    kind: str,
    audit: dict[str, Any],
    extension_id: str,
    object_id: str | None = None,
) -> tuple[str, dict[str, Any], int]:
    normalized_kind = _normalize_kind(kind)
    creator = getattr(plane, _create_from_spec_name(normalized_kind), None)
    if not callable(creator):
        raise ServiceUnavailableError(f"{normalized_kind.title()} output grants are not available")
    common_kwargs = {
        "owner_scope": audit,
        "run_id": str(spec.get("run_id") or audit.get("job_id") or "").strip(),
        "step_id": str(spec.get("step_id") or extension_id).strip(),
        "retention_class": retention_class_from_spec(spec),
    }
    if normalized_kind == "artifact":
        handle, meta = creator(
            **common_kwargs,
            artifact_id=object_id,
            filename=str(spec.get("filename") or "output").strip() or "output",
            content_type=str(spec.get("content_type") or _TOKEN_APPLICATION_OCTET_STREAM).strip(),
        )
        object_id = str(getattr(handle, "artifact_id", "") or meta.get("artifact_id") or "").strip()
    else:
        handle, meta = creator(
            **common_kwargs,
            dataset_id=object_id,
            schema=dict(spec.get("schema") or {}),
            content_type=str(spec.get("content_type") or _TOKEN_APPLICATION_JSON).strip()
            or _TOKEN_APPLICATION_JSON,
        )
        object_id = str(getattr(handle, "dataset_id", "") or meta.get("dataset_id") or "").strip()
    if not object_id:
        raise ServiceUnavailableError(f"{normalized_kind.title()} output registration failed")
    return object_id, dict(meta), output_max_size_from_spec(spec)


def _kind_collection_name(kind: str) -> str:
    return "artifacts" if _normalize_kind(kind) == "artifact" else "datasets"


def cleanup_output_grant_placeholder(
    plane: Any,
    object_id: str,
    *,
    kind: str,
    allowed_object_ids: set[str] | frozenset[str] | None = None,
) -> bool:
    return (
        cleanup_output_grant_placeholder_status(
            plane,
            object_id,
            kind=kind,
            allowed_object_ids=allowed_object_ids,
        )
        == "removed"
    )


def cleanup_output_grant_placeholder_status(
    plane: Any,
    object_id: str,
    *,
    kind: str,
    allowed_object_ids: set[str] | frozenset[str] | None = None,
) -> str:
    normalized_kind = _normalize_kind(kind)
    normalized_id = normalize_grant_id(object_id, kind=normalized_kind)
    if not normalized_id:
        return "unknown"
    if allowed_object_ids is not None and normalized_id not in allowed_object_ids:
        return "unknown"
    cleanup_status = getattr(plane, "cleanup_output_grant_placeholder_status", None)
    if callable(cleanup_status):
        try:
            status = str(cleanup_status(normalized_id, kind=normalized_kind) or "")
        except Exception:
            return "unknown"
        return status if status in {"removed", "verified_absent", "unknown"} else "unknown"
    deleter = getattr(plane, f"delete_{normalized_kind}", None)
    if callable(deleter):
        try:
            if deleter(normalized_id):
                return "removed"
        except Exception:
            return "unknown"
    getter = getattr(plane, _getter_name(normalized_kind), None)
    if not callable(getter):
        return "unknown"
    try:
        meta = getter(normalized_id)
    except Exception:
        return "unknown"
    if meta is None:
        return "verified_absent"
    meta = dict(meta) if isinstance(meta, dict) else {}
    storage = getattr(plane, "_storage", None)
    storage_delete = getattr(storage, "delete", None)
    removed = False
    if callable(storage_delete):
        object_key = str(meta.get("object_key") or "").strip()
        if object_key:
            with contextlib.suppress(Exception):
                removed = bool(storage_delete(object_key)) or removed
        meta_key = f"{_kind_collection_name(normalized_kind)}/{normalized_id}/meta.json"
        with contextlib.suppress(Exception):
            removed = bool(storage_delete(meta_key)) or removed
    cache = getattr(plane, f"_{_kind_collection_name(normalized_kind)}", None)
    if isinstance(cache, dict):
        removed = cache.pop(normalized_id, None) is not None or removed
    return "removed" if removed else "unknown"


def cleanup_prepared_extension_grants(
    plane: Any,
    prepared: PreparedExtensionGrants | None,
) -> dict[str, int]:
    if prepared is None:
        return {"artifacts": 0, "datasets": 0}
    artifact_count = 0
    dataset_count = 0
    minted_artifact_ids = frozenset(getattr(prepared, "minted_artifact_output_ids", None) or [])
    minted_dataset_ids = frozenset(getattr(prepared, "minted_dataset_output_ids", None) or [])
    for object_id in minted_artifact_ids:
        if cleanup_output_grant_placeholder(
            plane,
            object_id,
            kind="artifact",
            allowed_object_ids=minted_artifact_ids,
        ):
            artifact_count += 1
    for object_id in minted_dataset_ids:
        if cleanup_output_grant_placeholder(
            plane,
            object_id,
            kind="dataset",
            allowed_object_ids=minted_dataset_ids,
        ):
            dataset_count += 1
    return {"artifacts": artifact_count, "datasets": dataset_count}


def _grant_request_rows(values: list[Any] | None, *, kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    normalized_kind = _normalize_kind(kind)
    id_key = _id_field(normalized_kind)
    handle_key = _handle_field(normalized_kind)
    for item in values or []:
        binding_key = ""
        if isinstance(item, dict):
            object_id = (
                item.get(id_key)
                or item.get(handle_key)
                or item.get("id")
                or item.get("handle")
                or item.get("value")
            )
            binding_key = str(item.get("binding_key") or item.get("name") or "").strip()
            max_size_bytes = positive_int(item.get("max_size_bytes"))
            content_type = str(item.get("content_type") or "").strip()
        else:
            object_id = item
            max_size_bytes = None
            content_type = ""
        normalized_id = normalize_grant_id(object_id, kind=normalized_kind)
        if not normalized_id:
            continue
        row: dict[str, Any] = {
            "id": normalized_id,
            "binding_key": binding_key,
        }
        if max_size_bytes is not None:
            row["max_size_bytes"] = max_size_bytes
        if content_type:
            row["content_type"] = content_type
        rows.append(row)
    return rows


def prepare_extension_grants(
    *,
    plane: Any,
    scope: Any,
    audit: dict[str, Any],
    extension_id: str,
    artifact_inputs: list[str] | None = None,
    artifact_outputs: list[str] | None = None,
    artifact_output_specs: list[dict[str, Any]] | None = None,
    dataset_inputs: list[str] | None = None,
    dataset_outputs: list[str] | None = None,
    dataset_output_specs: list[dict[str, Any]] | None = None,
    before_materialize_outputs: Callable[[PreparedExtensionGrants], None] | None = None,
) -> PreparedExtensionGrants:
    prepared = PreparedExtensionGrants()
    artifact_input_rows = _grant_request_rows(artifact_inputs, kind="artifact")
    artifact_output_rows = _grant_request_rows(artifact_outputs, kind="artifact")
    dataset_input_rows = _grant_request_rows(dataset_inputs, kind="dataset")
    dataset_output_rows = _grant_request_rows(dataset_outputs, kind="dataset")
    kind_inputs = {
        "artifact": [row["id"] for row in artifact_input_rows],
        "dataset": [row["id"] for row in dataset_input_rows],
    }
    kind_outputs = {
        "artifact": [row["id"] for row in artifact_output_rows],
        "dataset": [row["id"] for row in dataset_output_rows],
    }
    input_rows_by_kind = {"artifact": artifact_input_rows, "dataset": dataset_input_rows}
    output_rows_by_kind = {"artifact": artifact_output_rows, "dataset": dataset_output_rows}
    specs_by_kind = {
        "artifact": [item for item in artifact_output_specs or [] if isinstance(item, dict)],
        "dataset": [item for item in dataset_output_specs or [] if isinstance(item, dict)],
    }
    planned_specs_by_kind: dict[str, list[tuple[dict[str, Any], str]]] = {
        "artifact": [],
        "dataset": [],
    }

    for kind in ("artifact", "dataset"):
        require_disjoint_grants(kind_inputs[kind], kind_outputs[kind], kind=kind)
        require_grant_ownership(plane, kind_inputs[kind], kind=kind, scope=scope)
        require_grant_ownership(plane, kind_outputs[kind], kind=kind, scope=scope)
        max_sizes = (
            prepared.artifact_output_max_size_by_id
            if kind == "artifact"
            else prepared.dataset_output_max_size_by_id
        )
        output_ids = (
            prepared.artifact_output_ids if kind == "artifact" else prepared.dataset_output_ids
        )
        minted_output_ids = (
            prepared.minted_artifact_output_ids
            if kind == "artifact"
            else prepared.minted_dataset_output_ids
        )
        for object_id in kind_outputs[kind]:
            normalized_id = normalize_grant_id(object_id, kind=kind)
            if normalized_id:
                output_ids.append(normalized_id)
                max_sizes[normalized_id] = DEFAULT_GRANT_OUTPUT_MAX_SIZE_BYTES
        for spec in specs_by_kind[kind]:
            prefix = "art" if kind == "artifact" else "dst"
            object_id = f"{prefix}_{uuid.uuid4().hex[:24]}"
            max_size = output_max_size_from_spec(spec)
            output_ids.append(object_id)
            minted_output_ids.append(object_id)
            max_sizes[object_id] = max_size
            planned_specs_by_kind[kind].append((spec, object_id))

    if callable(before_materialize_outputs) and (
        prepared.minted_artifact_output_ids or prepared.minted_dataset_output_ids
    ):
        before_materialize_outputs(prepared)

    for kind in ("artifact", "dataset"):
        output_ids = (
            prepared.artifact_output_ids if kind == "artifact" else prepared.dataset_output_ids
        )
        max_sizes = (
            prepared.artifact_output_max_size_by_id
            if kind == "artifact"
            else prepared.dataset_output_max_size_by_id
        )
        produced = prepared.produced_artifacts if kind == "artifact" else prepared.produced_datasets
        for spec, object_id in planned_specs_by_kind[kind]:
            created_id, meta, _max_size = create_output_from_spec(
                plane,
                spec,
                kind=kind,
                audit=audit,
                extension_id=extension_id,
                object_id=object_id,
            )
            if created_id != object_id:
                raise ServiceUnavailableError(
                    f"{kind.title()} output registration changed its preallocated identity"
                )
            produced.append(meta)

        input_descriptors = (
            prepared.artifact_inputs if kind == "artifact" else prepared.dataset_inputs
        )
        output_descriptors = (
            prepared.artifact_outputs if kind == "artifact" else prepared.dataset_outputs
        )
        for index, row in enumerate(input_rows_by_kind[kind]):
            object_id = row["id"]
            max_size = positive_int(row.get("max_size_bytes"))
            meta = meta_for_grant(plane, object_id, kind=kind)
            representation_url = ""
            if max_size is not None:
                size_bytes = meta.get("size_bytes")
                if (
                    isinstance(size_bytes, bool)
                    or not isinstance(size_bytes, int)
                    or size_bytes < 0
                    or size_bytes > max_size
                ):
                    raise ServiceValidationError(f"Input {kind} exceeds max_size_bytes={max_size}")
            desired_content_type = str(row.get("content_type") or "").strip().lower()
            current_content_type = _content_type_for_kind(kind, meta).lower()
            if (
                kind == "dataset"
                and desired_content_type
                and desired_content_type != current_content_type
            ):
                if max_size is None:
                    raise ServiceValidationError(
                        "Dataset representation requires a max_size_bytes policy"
                    )
                materializer = getattr(plane, "materialize_dataset_representation", None)
                representation_presigner = getattr(
                    plane,
                    "dataset_representation_presigned_url",
                    None,
                )
                if not callable(materializer) or not callable(representation_presigner):
                    raise ServiceUnavailableError("Dataset representation is not available")
                try:
                    meta = materializer(
                        object_id,
                        content_type=desired_content_type,
                        max_size_bytes=max_size,
                    )
                except ValueError as exc:
                    raise ServiceValidationError(str(exc)) from exc
                representation_url = str(
                    representation_presigner(
                        object_id,
                        object_key=str(meta.get("object_key") or ""),
                        method="GET",
                    )
                    or ""
                ).strip()
                if not representation_url:
                    raise ServiceUnavailableError("Dataset representation access is not available")
                require_safe_grant_url(representation_url)
            input_descriptors.append(
                build_descriptor_for_invocation(
                    plane,
                    object_id,
                    kind=kind,
                    role="input",
                    binding_key=row.get("binding_key") or f"{kind}_{index + 1}",
                    audit=audit,
                    meta_override=meta,
                    url_override=representation_url,
                )
            )
        output_bindings = [row.get("binding_key") or "" for row in output_rows_by_kind[kind]]
        output_bindings.extend(
            str(spec.get("binding_key") or spec.get("name") or "").strip()
            for spec in specs_by_kind[kind]
        )
        for index, object_id in enumerate(output_ids):
            output_descriptors.append(
                build_descriptor_for_invocation(
                    plane,
                    object_id,
                    kind=kind,
                    role="output",
                    binding_key=output_bindings[index] or f"{kind}_{index + 1}",
                    audit=audit,
                    max_size_bytes=max_sizes.get(object_id),
                )
            )
    return prepared


def grant_upload_error(
    code: str,
    message: str,
    *,
    object_id: str,
    kind: str,
) -> dict[str, Any]:
    normalized_kind = _normalize_kind(kind)
    if normalized_kind == "artifact":
        from core.application.extensions.remote_runtime_protocol_v2 import (
            canonical_artifact_error,
        )

        return canonical_artifact_error(
            code,
            message,
            artifact_id=object_id,
            transport="presigned_url",
        )
    return {
        "code": code.replace("artifact_", "dataset_", 1) if code.startswith("artifact_") else code,
        "message": message,
        "dataset_id": object_id,
        "transport": "presigned_url",
    }


def finalize_uploads(
    plane: Any,
    object_ids: list[str],
    *,
    kind: str,
    max_size_by_id: dict[str, int] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    normalized_kind = _normalize_kind(kind)
    finalized: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    max_size_by_id = dict(max_size_by_id or {})
    finalizer = getattr(plane, _finalize_name(normalized_kind), None)
    getter = getattr(plane, _getter_name(normalized_kind), None)
    for raw_id in object_ids:
        object_id = normalize_grant_id(raw_id, kind=normalized_kind)
        if not object_id:
            continue
        meta: dict[str, Any] | None = None
        max_size = max_size_by_id.get(object_id)
        if callable(finalizer):
            refreshed = finalizer(object_id, max_size_bytes=max_size)
            if isinstance(refreshed, dict):
                meta = refreshed
        if meta is None and callable(getter):
            current = getter(object_id)
            if isinstance(current, dict):
                meta = current
        size_bytes = positive_int(meta.get("size_bytes")) if meta else None
        if size_bytes is not None and max_size is not None and size_bytes > max_size:
            errors.append(
                grant_upload_error(
                    "artifact_size_limit_exceeded",
                    f"Output {normalized_kind} exceeds max_size_bytes={max_size}",
                    object_id=object_id,
                    kind=normalized_kind,
                )
            )
            continue
        if not meta or not str(meta.get("sha256") or "").strip():
            errors.append(
                grant_upload_error(
                    "artifact_upload_missing",
                    f"Output {normalized_kind} upload is missing",
                    object_id=object_id,
                    kind=normalized_kind,
                )
            )
            continue
        finalized[object_id] = dict(meta)
    return finalized, errors


def finalize_prepared_extension_grants(
    plane: Any,
    prepared: PreparedExtensionGrants,
    *,
    already_finalized: dict[str, dict[str, Any]] | None = None,
) -> FinalizedExtensionGrants:
    """Finalize every prepared output; outputs finalized while the run went on are reused.

    An output announced ready during the run was finalized then, which reads the
    whole object; reading a large export a second time at the end bought nothing.
    """
    early = dict(already_finalized or {})
    artifacts, artifact_errors = finalize_uploads(
        plane,
        [object_id for object_id in prepared.artifact_output_ids if object_id not in early],
        kind="artifact",
        max_size_by_id=prepared.artifact_output_max_size_by_id,
    )
    datasets, dataset_errors = finalize_uploads(
        plane,
        [object_id for object_id in prepared.dataset_output_ids if object_id not in early],
        kind="dataset",
        max_size_by_id=prepared.dataset_output_max_size_by_id,
    )
    # Keyed as finalize_uploads keys its own results: by the bare object id.
    artifacts.update(
        {
            normalize_grant_id(key, kind="artifact"): value
            for key, value in early.items()
            if key in prepared.artifact_output_ids
        }
    )
    datasets.update(
        {
            normalize_grant_id(key, kind="dataset"): value
            for key, value in early.items()
            if key in prepared.dataset_output_ids
        }
    )
    produced_artifacts = [
        artifacts.get(str(meta.get("artifact_id") or ""), meta)
        for meta in prepared.produced_artifacts
    ]
    produced_datasets = [
        datasets.get(str(meta.get("dataset_id") or ""), meta) for meta in prepared.produced_datasets
    ]
    return FinalizedExtensionGrants(
        artifacts=artifacts,
        datasets=datasets,
        errors=[*artifact_errors, *dataset_errors],
        produced_artifacts=produced_artifacts,
        produced_datasets=produced_datasets,
    )


__all__ = [
    "DEFAULT_GRANT_OUTPUT_MAX_SIZE_BYTES",
    "FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV",
    "SUPPORTED_GRANT_RETENTION_CLASSES",
    "FinalizedExtensionGrants",
    "PreparedExtensionGrants",
    "build_descriptor_for_invocation",
    "cleanup_output_grant_placeholder",
    "cleanup_output_grant_placeholder_status",
    "cleanup_prepared_extension_grants",
    "finalize_prepared_extension_grants",
    "finalize_uploads",
    "grant_upload_error",
    "meta_for_grant",
    "normalize_grant_id",
    "output_max_size",
    "output_max_size_from_spec",
    "positive_int",
    "prepare_extension_grants",
    "presigned_url",
    "require_disjoint_grants",
    "require_empty_output_target",
    "require_grant_ownership",
    "require_safe_grant_url",
    "retention_class_from_spec",
    "trusted_local_fs_artifact_grants_enabled",
]
