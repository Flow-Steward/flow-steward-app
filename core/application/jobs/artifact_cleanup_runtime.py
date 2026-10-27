"""Helpers for artifact object cleanup tied to parent-entity lifecycle."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any

from core.application.extensions.artifact_plane_values import (
    replay_cleanup_completion_key,
    replay_owner_manifest_key,
    replay_owner_manifest_prefix,
)
from core.infrastructure.extension_runtime.object_storage import (
    APPLICATION_JSON_VALUE,
    ARTIFACT_INGRESS_GRANT_MAX_TTL_SECONDS,
    STORAGE_CLEANUP_PAGE_SIZE,
    iter_storage_keys,
    read_storage_key_page,
)

_MESSAGE_INVALID_CLEANUP_JOURNAL = "invalid cleanup journal"


def _valid_dataset_object_key(dataset_id: str, object_key: str) -> bool:
    prefix = f"datasets/{dataset_id}/"
    basename = object_key.removeprefix(prefix)
    return (
        object_key.startswith(prefix)
        and bool(basename)
        and "/" not in basename
        and basename != "meta.json"
    )


def extract_file_refs_from_meta(meta_value: Any) -> list[dict[str, Any]]:
    """Return normalized file refs from a job meta payload."""
    meta = meta_value
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            return []
    if not isinstance(meta, dict):
        return []
    raw_files = meta.get("files")
    refs = (
        [dict(item) for item in raw_files if isinstance(item, dict)]
        if isinstance(raw_files, list)
        else []
    )
    pending_refs = meta.get("attachment_persistence_artifacts")
    if isinstance(pending_refs, list):
        refs.extend(dict(item) for item in pending_refs if isinstance(item, dict))
    workflow_outputs = meta.get("workflow_outputs")
    workflow_artifacts = (
        workflow_outputs.get("artifacts") if isinstance(workflow_outputs, dict) else None
    )
    if isinstance(workflow_artifacts, dict):
        refs.extend(dict(item) for item in workflow_artifacts.values() if isinstance(item, dict))
    replay_pointer = meta.get("replay_snapshot")
    if isinstance(replay_pointer, dict):
        artifact_id = str(replay_pointer.get("artifact_id") or "").strip()
        if artifact_id:
            refs.append({"artifact_id": artifact_id})
    return refs


def collect_storage_keys_from_file_refs(file_refs: list[dict[str, Any]] | None) -> list[str]:
    """Collect object keys that should be deleted for file refs."""
    keys: list[str] = []
    for row in file_refs or []:
        object_key = str((row or {}).get("object_key") or "").strip()
        if object_key:
            keys.append(object_key)
        artifact_id = str((row or {}).get("artifact_id") or "").strip()
        if artifact_id:
            keys.append(f"_replay_owners/artifact/{artifact_id}.json")
            keys.append(f"artifacts/_metadata/{artifact_id}.json")
        dataset_id = str((row or {}).get("dataset_id") or "").strip()
        if dataset_id:
            representation_key = str((row or {}).get("representation_key") or "").strip()
            if representation_key:
                keys.extend([representation_key, f"{representation_key}.json"])
            keys.append(f"_replay_owners/dataset/{dataset_id}.json")
            keys.append(f"datasets/{dataset_id}/meta.json")
    # preserve order, drop duplicates
    return list(dict.fromkeys(keys))


def collect_storage_keys_from_job_row(job_row: Mapping[str, Any] | None) -> list[str]:
    """Collect storage keys from a job row's metadata."""
    if not isinstance(job_row, Mapping):
        return []
    refs = extract_file_refs_from_meta(job_row.get("meta"))
    return collect_storage_keys_from_file_refs(refs)


def purge_storage_keys(storage: Any, keys: list[str] | None) -> dict[str, int]:
    """Best-effort delete for object keys on a storage adapter."""
    delete_fn = getattr(storage, "delete", None)
    if not callable(delete_fn):
        return {"attempted": 0, "deleted": 0, "failed": 0}

    attempted = 0
    deleted = 0
    failed = 0
    for key in keys or []:
        safe_key = str(key or "").strip()
        if not safe_key:
            continue
        attempted += 1
        try:
            abort_fn = getattr(storage, "abort_incomplete_uploads", None)
            if callable(abort_fn):
                abort_fn(safe_key)
            if bool(delete_fn(safe_key)):
                deleted += 1
            else:
                stat_fn = getattr(storage, "stat", None)
                if callable(stat_fn) and stat_fn(safe_key) is None:
                    deleted += 1
                else:
                    failed += 1
        except Exception:
            failed += 1
    return {"attempted": attempted, "deleted": deleted, "failed": failed}


def _add_cleanup_stats(total: dict[str, int], result: Mapping[str, Any]) -> None:
    for key in ("attempted", "deleted", "failed"):
        total[key] += int(result.get(key) or 0)


def _purge_authority_batch(storage: Any, keys: list[str]) -> dict[str, int]:
    unique = list(dict.fromkeys(str(key or "").strip() for key in keys if str(key or "").strip()))
    manifests = [key for key in unique if key.startswith("_replay_job_manifests/")]
    sidecars = [
        key
        for key in unique
        if key.startswith("artifacts/_metadata/")
        or (key.startswith("datasets/") and key.endswith("/meta.json"))
    ]
    objects = [key for key in unique if key not in manifests and key not in sidecars]
    total = {"attempted": 0, "deleted": 0, "failed": 0}
    object_stats = purge_storage_keys(storage, objects)
    _add_cleanup_stats(total, object_stats)
    if object_stats["failed"]:
        return total
    sidecar_stats = purge_storage_keys(storage, sidecars)
    _add_cleanup_stats(total, sidecar_stats)
    if sidecar_stats["failed"]:
        return total
    _add_cleanup_stats(total, purge_storage_keys(storage, manifests))
    return total


JOB_DELETED_TRANSITION = "job_deleted"
PREPARING_TERMINALIZED_TRANSITION = "preparing_terminalized"
_CLEANUP_JOURNAL_VERSION = 2
_CLEANUP_TRANSITIONS = {JOB_DELETED_TRANSITION, PREPARING_TERMINALIZED_TRANSITION}


def _cleanup_completion_fingerprint(
    *,
    project_id: str,
    job_id: str,
    refs: list[dict[str, Any]],
) -> str | None:
    identities: list[dict[str, str]] = []
    for ref in refs:
        artifact_id = str(ref.get("artifact_id") or "").strip()
        dataset_id = str(ref.get("dataset_id") or "").strip()
        if bool(artifact_id) == bool(dataset_id):
            return None
        identities.append(
            {"kind": "artifact", "object_id": artifact_id}
            if artifact_id
            else {"kind": "dataset", "object_id": dataset_id}
        )
    payload = {
        "version": 1,
        "project_id": project_id,
        "job_id": job_id,
        "objects": sorted(identities, key=lambda item: (item["kind"], item["object_id"])),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cleanup_plan_fingerprint(objects: list[dict[str, str]]) -> str:
    encoded = json.dumps(
        sorted(objects, key=lambda item: (item["kind"], item["object_id"])),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_cleanup_journal(
    raw: bytes | None,
    *,
    project_id: str,
    job_id: str,
    fingerprint: str,
    db_transition: str,
) -> dict[str, Any] | None:
    try:
        marker = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(marker, dict)
        or str(marker.get("project_id") or "").strip() != project_id
        or str(marker.get("job_id") or "").strip() != job_id
        or str(marker.get("authority_fingerprint") or "").strip() != fingerprint
    ):
        return None
    try:
        version = int(marker.get("version") or 0)
    except (TypeError, ValueError):
        return None
    if version == 1:
        try:
            float(marker["completed_at"])
            float(marker["expires_at"])
        except (KeyError, TypeError, ValueError):
            return None
        return {**marker, "state": "complete", "db_transition": JOB_DELETED_TRANSITION}
    if (
        version != _CLEANUP_JOURNAL_VERSION
        or str(marker.get("state") or "").strip() not in {"preparing", "complete", "committed"}
        or str(marker.get("db_transition") or "").strip() != db_transition
        or not isinstance(marker.get("objects"), list)
    ):
        return None
    objects: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for raw_object in marker["objects"]:
        if not isinstance(raw_object, dict):
            return None
        item = {str(key): str(value or "").strip() for key, value in raw_object.items()}
        kind = item.get("kind", "")
        object_id = item.get("object_id", "")
        expected_prefix = (
            f"artifacts/{object_id}/" if kind == "artifact" else f"datasets/{object_id}/"
        )
        expected_metadata = (
            f"artifacts/_metadata/{object_id}.json"
            if kind == "artifact"
            else f"datasets/{object_id}/meta.json"
        )
        if (
            kind not in {"artifact", "dataset"}
            or not object_id
            or "/" in object_id
            or item.get("object_prefix") != expected_prefix
            or item.get("metadata_key") != expected_metadata
            or item.get("claim_key") != f"_replay_owners/{kind}/{object_id}.json"
            or item.get("manifest_key")
            != replay_owner_manifest_key(
                project_id=project_id,
                job_id=job_id,
                kind=kind,
                object_id=object_id,
            )
            or not item.get("object_key", "").startswith(expected_prefix)
        ):
            return None
        identity = (kind, object_id)
        if identity in identities:
            return None
        identities.add(identity)
        objects.append(item)
    if str(marker.get("plan_fingerprint") or "").strip() != _cleanup_plan_fingerprint(objects):
        return None
    if str(marker.get("state") or "").strip() == "committed":
        try:
            committed_at = float(marker["committed_at"])
            remove_after = float(marker["remove_after"])
        except (KeyError, TypeError, ValueError):
            return None
        if remove_after < committed_at + ARTIFACT_INGRESS_GRANT_MAX_TTL_SECONDS:
            return None
    return {**marker, "objects": objects}


def _valid_cleanup_completion_marker(
    raw: bytes | None,
    *,
    project_id: str,
    job_id: str,
    fingerprint: str,
    now: float | None = None,
    db_transition: str = JOB_DELETED_TRANSITION,
) -> bool:
    del now
    marker = _parse_cleanup_journal(
        raw,
        project_id=project_id,
        job_id=job_id,
        fingerprint=fingerprint,
        db_transition=db_transition,
    )
    return bool(marker and marker.get("state") == "complete")


def _journal_payload(
    *,
    project_id: str,
    job_id: str,
    fingerprint: str,
    db_transition: str,
    objects: list[dict[str, str]],
    state: str,
) -> dict[str, Any]:
    now = time.time()
    return {
        "version": _CLEANUP_JOURNAL_VERSION,
        "state": state,
        "project_id": project_id,
        "job_id": job_id,
        "db_transition": db_transition,
        "authority_fingerprint": fingerprint,
        "plan_fingerprint": _cleanup_plan_fingerprint(objects),
        "objects": sorted(objects, key=lambda item: (item["kind"], item["object_id"])),
        "prepared_at": now,
        **({"completed_at": now} if state == "complete" else {}),
    }


def _write_cleanup_journal(
    storage: Any,
    *,
    project_id: str,
    job_id: str,
    fingerprint: str,
    db_transition: str,
    objects: list[dict[str, str]],
    state: str,
    create_only: bool,
) -> bool:
    put_fn = getattr(storage, "put", None)
    put_if_absent_fn = getattr(storage, "put_if_absent", None)
    get_fn = getattr(storage, "get", None)
    if not callable(get_fn) or (not callable(put_fn) and not callable(put_if_absent_fn)):
        return False
    payload = _journal_payload(
        project_id=project_id,
        job_id=job_id,
        fingerprint=fingerprint,
        db_transition=db_transition,
        objects=objects,
        state=state,
    )
    marker_key = replay_cleanup_completion_key(project_id, job_id)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        if create_only and callable(put_if_absent_fn):
            created = put_if_absent_fn(
                marker_key,
                encoded,
                content_type=APPLICATION_JSON_VALUE,
            )
            if not created:
                existing = get_fn(marker_key)
                return (
                    _parse_cleanup_journal(
                        existing,
                        project_id=project_id,
                        job_id=job_id,
                        fingerprint=fingerprint,
                        db_transition=db_transition,
                    )
                    is not None
                )
        elif callable(put_fn):
            put_fn(marker_key, encoded, content_type=APPLICATION_JSON_VALUE)
        else:
            return False
        confirmed = _parse_cleanup_journal(
            get_fn(marker_key),
            project_id=project_id,
            job_id=job_id,
            fingerprint=fingerprint,
            db_transition=db_transition,
        )
    except Exception:
        return False
    return bool(confirmed and confirmed.get("state") == state)


def _cleanup_plan_object(
    *,
    project_id: str,
    job_id: str,
    kind: str,
    object_id: str,
    object_key: str,
) -> dict[str, str]:
    return {
        "kind": kind,
        "object_id": object_id,
        "object_key": object_key,
        "object_prefix": (
            f"artifacts/{object_id}/" if kind == "artifact" else f"datasets/{object_id}/"
        ),
        "metadata_key": (
            f"artifacts/_metadata/{object_id}.json"
            if kind == "artifact"
            else f"datasets/{object_id}/meta.json"
        ),
        "claim_key": f"_replay_owners/{kind}/{object_id}.json",
        "manifest_key": replay_owner_manifest_key(
            project_id=project_id,
            job_id=job_id,
            kind=kind,
            object_id=object_id,
        ),
    }


def _build_owned_cleanup_plan(
    storage: Any,
    *,
    project_id: str,
    job_id: str,
    refs: list[dict[str, Any]],
) -> list[dict[str, str]] | None:
    get_fn = getattr(storage, "get", None)
    if not callable(get_fn):
        return None
    objects: dict[tuple[str, str], dict[str, str]] = {}
    try:
        for manifest_key in iter_storage_keys(
            storage,
            replay_owner_manifest_prefix(project_id, job_id),
            page_size=STORAGE_CLEANUP_PAGE_SIZE,
        ):
            raw = get_fn(manifest_key)
            manifest = json.loads(raw) if raw else None
            if not isinstance(manifest, dict):
                return None
            kind = str(manifest.get("kind") or "").strip()
            object_id = str(manifest.get("object_id") or "").strip()
            object_key = str(manifest.get("object_key") or "").strip()
            metadata_key = str(manifest.get("metadata_key") or "").strip()
            item = _cleanup_plan_object(
                project_id=project_id,
                job_id=job_id,
                kind=kind,
                object_id=object_id,
                object_key=object_key,
            )
            if (
                kind not in {"artifact", "dataset"}
                or not object_id
                or str(manifest.get("project_id") or "").strip() != project_id
                or str(manifest.get("job_id") or "").strip() != job_id
                or manifest_key != item["manifest_key"]
                or metadata_key != item["metadata_key"]
                or not object_key.startswith(item["object_prefix"])
                or (
                    kind == "dataset"
                    and manifest.get("representation_prefix")
                    != f"datasets/{object_id}/representations/"
                )
            ):
                return None
            objects[(kind, object_id)] = item

        for ref in refs:
            artifact_id = str(ref.get("artifact_id") or "").strip()
            dataset_id = str(ref.get("dataset_id") or "").strip()
            if bool(artifact_id) == bool(dataset_id):
                return None
            kind = "artifact" if artifact_id else "dataset"
            object_id = artifact_id or dataset_id
            existing = objects.get((kind, object_id))
            metadata_key = (
                f"artifacts/_metadata/{object_id}.json"
                if kind == "artifact"
                else f"datasets/{object_id}/meta.json"
            )
            raw = get_fn(metadata_key)
            sidecar = json.loads(raw) if raw else None
            if not isinstance(sidecar, dict):
                return None
            owner = sidecar.get("owner_scope")
            owner = owner if isinstance(owner, dict) else {}
            object_key = str(sidecar.get("object_key") or "").strip()
            item = _cleanup_plan_object(
                project_id=project_id,
                job_id=job_id,
                kind=kind,
                object_id=object_id,
                object_key=object_key,
            )
            if (
                str(sidecar.get(f"{kind}_id") or "").strip() != object_id
                or str(owner.get("project_id") or "").strip() != project_id
                or str(owner.get("job_id") or "").strip() != job_id
                or not object_key.startswith(item["object_prefix"])
                or (existing is not None and existing != item)
            ):
                return None
            objects[(kind, object_id)] = item
    except Exception:
        return None
    return sorted(objects.values(), key=lambda item: (item["kind"], item["object_id"]))


def _legacy_plan_from_refs(
    storage: Any,
    *,
    project_id: str,
    job_id: str,
    refs: list[dict[str, Any]],
) -> list[dict[str, str]] | None:
    owned_objects = _build_owned_cleanup_plan(
        storage,
        project_id=project_id,
        job_id=job_id,
        refs=refs,
    )
    if owned_objects is None:
        return None
    objects_by_identity = {(item["kind"], item["object_id"]): item for item in owned_objects}
    objects: dict[tuple[str, str], dict[str, str]] = {}
    for ref in refs:
        artifact_id = str(ref.get("artifact_id") or "").strip()
        dataset_id = str(ref.get("dataset_id") or "").strip()
        if bool(artifact_id) == bool(dataset_id):
            return None
        kind = "artifact" if artifact_id else "dataset"
        object_id = artifact_id or dataset_id
        item = objects_by_identity.get((kind, object_id))
        requested_key = str(ref.get("object_key") or "").strip()
        if item is None or (requested_key and requested_key != item["object_key"]):
            return None
        objects[(kind, object_id)] = item
    return sorted(objects.values(), key=lambda item: (item["kind"], item["object_id"]))


def _purge_cleanup_plan(storage: Any, objects: list[dict[str, str]]) -> dict[str, int]:
    total = {"attempted": 0, "deleted": 0, "failed": 0}
    for item in objects:
        primary_seen = False
        try:
            for key in iter_storage_keys(
                storage,
                item["object_prefix"],
                page_size=STORAGE_CLEANUP_PAGE_SIZE,
            ):
                if key == item["metadata_key"]:
                    continue
                primary_seen = primary_seen or key == item["object_key"]
                result = purge_storage_keys(storage, [key])
                _add_cleanup_stats(total, result)
                if result["failed"]:
                    return total
        except Exception:
            total["failed"] += 1
            return total
        body_keys = [] if primary_seen else [item["object_key"]]
        for keys in (
            body_keys,
            [item["claim_key"]],
            [item["metadata_key"]],
            [item["manifest_key"]],
        ):
            result = purge_storage_keys(storage, keys)
            _add_cleanup_stats(total, result)
            if result["failed"]:
                return total
    return total


def _cleanup_plan_has_residuals(storage: Any, objects: list[dict[str, str]]) -> bool:
    try:
        stat_fn = getattr(storage, "stat", None)
        get_fn = getattr(storage, "get", None)
        for item in objects:
            abort_fn = getattr(storage, "abort_incomplete_uploads", None)
            if callable(abort_fn):
                abort_fn(item["object_key"])
            keys, token = read_storage_key_page(storage, item["object_prefix"], limit=1)
            if keys or token:
                return True
            for key in (item["metadata_key"], item["claim_key"], item["manifest_key"]):
                stored_object = None
                if callable(stat_fn):
                    stored_object = stat_fn(key)
                elif callable(get_fn):
                    stored_object = get_fn(key)
                if stored_object is not None:
                    return True
    except Exception:
        return True
    return False


def finalize_job_artifact_purge(
    job_row: Mapping[str, Any] | None,
    *,
    storage: Any,
    now: float | None = None,
) -> bool:
    """Persist a post-commit fence until every issued ingress grant has expired."""
    if not isinstance(job_row, Mapping):
        return True
    project_id = str(job_row.get("project_id") or "").strip()
    job_id = str(job_row.get("id") or "").strip()
    if not project_id or not job_id:
        return True
    marker_key = replay_cleanup_completion_key(project_id, job_id)
    get_fn = getattr(storage, "get", None)
    put_fn = getattr(storage, "put", None)
    if not callable(get_fn) or not callable(put_fn):
        return False
    try:
        raw = get_fn(marker_key)
        wire = json.loads(raw) if raw else None
        if not isinstance(wire, dict) or int(wire.get("version") or 0) != _CLEANUP_JOURNAL_VERSION:
            return False
        parsed = _parse_cleanup_journal(
            raw,
            project_id=project_id,
            job_id=job_id,
            fingerprint=str(wire.get("authority_fingerprint") or "").strip(),
            db_transition=str(wire.get("db_transition") or "").strip(),
        )
        if parsed is None or parsed.get("state") not in {"complete", "committed"}:
            return False
        if parsed.get("state") == "committed":
            return True
        committed_at = float(time.time() if now is None else now)
        payload = {
            **parsed,
            "state": "committed",
            "committed_at": committed_at,
            "remove_after": committed_at + ARTIFACT_INGRESS_GRANT_MAX_TTL_SECONDS,
        }
        put_fn(
            marker_key,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            content_type=APPLICATION_JSON_VALUE,
        )
        confirmed = _parse_cleanup_journal(
            get_fn(marker_key),
            project_id=project_id,
            job_id=job_id,
            fingerprint=str(parsed["authority_fingerprint"]),
            db_transition=str(parsed["db_transition"]),
        )
        return bool(confirmed and confirmed.get("state") == "committed")
    except Exception:
        return False


def purge_job_artifacts(
    job_row: Mapping[str, Any] | None,
    *,
    storage: Any,
    db_transition: str = JOB_DELETED_TRANSITION,
) -> dict[str, int]:
    """Delete storage objects referenced by a job row."""
    refs = extract_file_refs_from_meta(job_row.get("meta")) if isinstance(job_row, Mapping) else []
    job_id = str(job_row.get("id") or "").strip() if isinstance(job_row, Mapping) else ""
    project_id = (
        str(job_row.get("project_id") or "").strip() if isinstance(job_row, Mapping) else ""
    )
    if db_transition not in _CLEANUP_TRANSITIONS:
        return {"attempted": 0, "deleted": 0, "failed": 1}
    if refs and bool(job_id) != bool(project_id):
        return {"attempted": 0, "deleted": 0, "failed": 1}
    get_fn = getattr(storage, "get", None)
    if not (job_id and project_id):
        if callable(get_fn):
            for ref in refs:
                artifact_id = str(ref.get("artifact_id") or "").strip()
                dataset_id = str(ref.get("dataset_id") or "").strip()
                if not artifact_id and not dataset_id:
                    continue
                metadata_key = (
                    f"artifacts/_metadata/{artifact_id}.json"
                    if artifact_id
                    else f"datasets/{dataset_id}/meta.json"
                )
                try:
                    metadata = json.loads(get_fn(metadata_key) or b"null")
                except Exception:
                    metadata = None
                if isinstance(metadata, dict):
                    object_key = str(metadata.get("object_key") or "").strip()
                    if object_key:
                        ref["object_key"] = object_key
        return _purge_authority_batch(storage, collect_storage_keys_from_file_refs(refs))

    cleanup_fingerprint = _cleanup_completion_fingerprint(
        project_id=project_id,
        job_id=job_id,
        refs=refs,
    )
    if cleanup_fingerprint is None or not callable(get_fn):
        return {"attempted": 0, "deleted": 0, "failed": 1}
    marker_key = replay_cleanup_completion_key(project_id, job_id)
    try:
        marker_raw = get_fn(marker_key)
    except Exception:
        return {"attempted": 0, "deleted": 0, "failed": 1}

    marker = None
    objects: list[dict[str, str]] | None = None
    if marker_raw is not None:
        marker = _parse_cleanup_journal(
            marker_raw,
            project_id=project_id,
            job_id=job_id,
            fingerprint=cleanup_fingerprint,
            db_transition=db_transition,
        )
        if marker is None:
            return {"attempted": 0, "deleted": 0, "failed": 1}
        if int(marker.get("version") or 0) == 1:
            objects = _legacy_plan_from_refs(
                storage,
                project_id=project_id,
                job_id=job_id,
                refs=refs,
            )
        else:
            objects = marker["objects"]
        if objects is None:
            return {"attempted": 0, "deleted": 0, "failed": 1}
        try:
            expected_manifests = {item["manifest_key"] for item in objects}
            for manifest_key in iter_storage_keys(
                storage,
                replay_owner_manifest_prefix(project_id, job_id),
                page_size=STORAGE_CLEANUP_PAGE_SIZE,
            ):
                if manifest_key not in expected_manifests:
                    return {"attempted": 0, "deleted": 0, "failed": 1}
        except Exception:
            return {"attempted": 0, "deleted": 0, "failed": 1}
    else:
        objects = _build_owned_cleanup_plan(
            storage,
            project_id=project_id,
            job_id=job_id,
            refs=refs,
        )
        if objects is None:
            return {"attempted": 0, "deleted": 0, "failed": 1}
        # Empty owned plans also need a journal for the post-commit ingress fence.
        if not _write_cleanup_journal(
            storage,
            project_id=project_id,
            job_id=job_id,
            fingerprint=cleanup_fingerprint,
            db_transition=db_transition,
            objects=objects,
            state="preparing",
            create_only=True,
        ):
            return {"attempted": 0, "deleted": 0, "failed": 1}

    if (
        marker is not None
        and marker.get("state") == "complete"
        and not _cleanup_plan_has_residuals(storage, objects)
    ):
        return {"attempted": 0, "deleted": 0, "failed": 0}
    total = _purge_cleanup_plan(storage, objects)
    if total["failed"]:
        return total
    if _cleanup_plan_has_residuals(storage, objects):
        total["failed"] += 1
        return total
    if not _write_cleanup_journal(
        storage,
        project_id=project_id,
        job_id=job_id,
        fingerprint=cleanup_fingerprint,
        db_transition=db_transition,
        objects=objects,
        state="complete",
        create_only=False,
    ):
        total["failed"] += 1
    return total


def sweep_expired_artifact_storage(*, storage: Any, dry_run: bool = False) -> dict[str, int]:
    """Run the durable artifact/dataset expiry sweep used by production ops."""
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane

    return ArtifactDatasetPlane(storage=storage).cleanup_expired(dry_run=dry_run)


# Alias used by integration tests and cascade-cleanup contract tests.
extract_artifact_keys_from_job = collect_storage_keys_from_job_row
