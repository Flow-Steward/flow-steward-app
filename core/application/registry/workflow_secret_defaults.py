"""Write-only configuration of a workflow input's secret default.

The value is accepted once, encrypted immediately, and referred to by key from
then on. Nothing downstream — the contract, an export, a job payload, a
checkpoint, a browser response — ever holds it again; they hold the key.
"""

from __future__ import annotations

import copy
import hashlib
import secrets
from collections.abc import Callable
from typing import Any


class SecretDefaultRejected(ValueError):
    """The supplied secret cannot be stored as configured."""


def secret_default_ref_key(workflow_id: str, field_name: str, *, version_token: str = "") -> str:
    """The store key for one workflow input's configured secret.

    Scoped to both the workflow and the field: two workflows that each configure
    an input called `login` must not end up sharing one stored value, which is
    the kind of collision that only shows up as the wrong credential in
    production.
    """
    workflow = str(workflow_id or "").strip()
    field = str(field_name or "").strip()
    if not workflow or not field:
        raise SecretDefaultRejected("a secret default needs both a workflow and a field name")
    version = str(version_token or "").strip() or secrets.token_hex(16)
    version_digest = hashlib.sha256(version.encode("utf-8")).hexdigest()[:24]
    return f"{secret_default_ref_owner_prefix(workflow, field)}{version_digest}"


def secret_default_ref_owner_prefix(workflow_id: str, field_name: str) -> str:
    """Canonical prefix shared only by versions of one exact workflow field."""
    workflow = str(workflow_id or "").strip()
    field = str(field_name or "").strip()
    if not workflow or not field:
        raise SecretDefaultRejected("a secret default needs both a workflow and a field name")
    field_owner = f"{len(field)}:{field}".encode()
    field_digest = hashlib.sha256(field_owner).hexdigest()
    return f"{secret_default_workflow_owner_prefix(workflow)}{field_digest}:"


def secret_default_workflow_owner_prefix(workflow_id: str) -> str:
    """Canonical opaque prefix shared by every secret owned by one workflow."""
    workflow = str(workflow_id or "").strip()
    if not workflow:
        raise SecretDefaultRejected("a secret default needs a workflow")
    workflow_owner = f"{len(workflow)}:{workflow}".encode()
    workflow_digest = hashlib.sha256(workflow_owner).hexdigest()
    return f"workflow-input:v2:{workflow_digest}:"


def secret_default_ref_belongs_to(
    ref_key: str,
    *,
    workflow_id: str,
    field_name: str,
) -> bool:
    """Return whether a canonical ref is owned by this exact workflow field."""
    workflow = str(workflow_id or "").strip()
    field = str(field_name or "").strip()
    value = str(ref_key or "").strip()
    if not workflow or not field or not value:
        return False
    prefix = secret_default_ref_owner_prefix(workflow, field)
    version_digest = value.removeprefix(prefix)
    return (
        value.startswith(prefix)
        and len(version_digest) == 24
        and all(char in "0123456789abcdef" for char in version_digest)
    )


def secret_ref_fingerprint(ref_key: str) -> str:
    value = str(ref_key or "").strip()
    return hashlib.sha256(f"workflow-secret-ref\0{value}".encode()).hexdigest()


def workflow_secret_launch_snapshot(interface: Any, *, inputs: Any = None) -> list[str]:
    """Return the immutable, non-revealing secret authority for one launch."""
    if hasattr(interface, "expected_inputs"):
        rows = getattr(interface, "expected_inputs", [])
    elif isinstance(interface, dict):
        rows = interface.get("expected_inputs") or []
    else:
        rows = []
    refs: set[str] = set()
    for row in rows if isinstance(rows, (list, tuple)) else []:
        if isinstance(row, dict):
            field_type = str(row.get("type") or "").strip().lower()
            ref = str(row.get("default_secret_ref") or "").strip()
        else:
            field_type = str(getattr(row, "type", "") or "").strip().lower()
            ref = str(getattr(row, "default_secret_ref", "") or "").strip()
        if field_type == "secret" and ref:
            refs.add(secret_ref_fingerprint(ref))
    refs.update(_presented_secret_ref_fingerprints(inputs))
    return sorted(refs)


def _presented_secret_ref_fingerprints(value: Any) -> set[str]:
    from core.infrastructure.security.secret_reference import (
        as_secret_reference,
        inline_secret_ref_fingerprint,
    )

    reference = as_secret_reference(value)
    if reference is not None:
        if reference.ref_key:
            return {secret_ref_fingerprint(reference.ref_key)}
        fingerprint = inline_secret_ref_fingerprint(reference)
        return {fingerprint} if fingerprint else set()
    if isinstance(value, dict):
        fingerprints: set[str] = set()
        for item in value.values():
            fingerprints.update(_presented_secret_ref_fingerprints(item))
        return fingerprints
    if isinstance(value, (list, tuple)):
        fingerprints = set()
        for item in value:
            fingerprints.update(_presented_secret_ref_fingerprints(item))
        return fingerprints
    return set()


def delegated_workflow_secret_launch_snapshot(
    *, parent_state: Any, child_interface: Any, child_inputs: Any
) -> list[str]:
    """Add only parent-authorized capabilities actually passed to the child."""
    parent_snapshot = {
        str(item or "").strip()
        for item in (
            parent_state.get("_workflow_secret_ref_fingerprints")
            if isinstance(parent_state, dict)
            else []
        )
        or []
        if str(item or "").strip()
    }
    delegated = parent_snapshot.intersection(_presented_secret_ref_fingerprints(child_inputs))
    return sorted(set(workflow_secret_launch_snapshot(child_interface)).union(delegated))


def strip_workflow_secret_authority(value: Any) -> Any:
    """Project a workflow payload without exposing encrypted-store authority."""
    if isinstance(value, dict):
        is_secret_field = str(value.get("type") or "").strip().lower() == "secret"
        configured = bool(str(value.get("default_secret_ref") or "").strip())
        projected = {
            key: strip_workflow_secret_authority(item)
            for key, item in value.items()
            if key != "default_secret_ref"
        }
        if is_secret_field:
            projected.pop("default", None)
            projected["default_configured"] = configured
        return projected
    if isinstance(value, list):
        return [strip_workflow_secret_authority(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_workflow_secret_authority(item) for item in value)
    return copy.deepcopy(value)


def set_secret_default(
    *,
    workflow_id: str,
    field_name: str,
    plaintext: str,
    account_id: str,
    encrypt: Callable[[str], str],
    write_ciphertext: Callable[[str, str, str], None],
    version_token: str = "",
) -> str:
    """Store one secret and return the key the contract should record.

    Returns the key rather than any part of the value, so a caller that logs the
    result of configuring a secret logs a key.
    """
    value = str(plaintext or "")
    if not value.strip():
        # An empty credential is not a credential. Storing it would leave the
        # field reading as configured while every run using it fails at the
        # connector, which is the least explainable place for it to fail.
        raise SecretDefaultRejected("a secret default cannot be empty")
    ref_key = secret_default_ref_key(
        workflow_id,
        field_name,
        version_token=version_token,
    )
    write_ciphertext(str(account_id or ""), ref_key, encrypt(value))
    return ref_key


__all__ = [
    "SecretDefaultRejected",
    "delegated_workflow_secret_launch_snapshot",
    "preserve_secret_references",
    "secret_default_ref_belongs_to",
    "secret_default_ref_key",
    "secret_default_ref_owner_prefix",
    "secret_default_workflow_owner_prefix",
    "secret_ref_fingerprint",
    "set_secret_default",
    "strip_workflow_secret_authority",
    "workflow_secret_launch_snapshot",
]


def preserve_secret_references(incoming_settings: Any, stored_settings: Any) -> dict[str, Any]:
    """Carry a configured secret reference across an ordinary settings save.

    The browser is never given the storage key — knowing where a credential is
    kept is not part of editing a workflow — so an ordinary save cannot echo it
    back. Reading its absence as "clear it" would silently unconfigure a
    credential every time someone edited an unrelated field on the same form.

    Clearing stays explicit, through the endpoint that exists for it. A field
    that is no longer a secret keeps nothing: a reference on a string field is
    not reachable and would only be a way to smuggle one back later.

    A file input's test-run file is carried the same way. It is set only by its
    upload endpoint, so a payload cannot point a field at another file.
    """
    incoming = dict(incoming_settings) if isinstance(incoming_settings, dict) else {}
    stored = stored_settings if isinstance(stored_settings, dict) else {}
    incoming_callable = incoming.get("callable")
    stored_callable = stored.get("callable")
    if not isinstance(incoming_callable, dict):
        return incoming
    rows = incoming_callable.get("expected_inputs")
    if not isinstance(rows, list):
        return incoming

    stored_refs: dict[str, str] = {}
    stored_samples: dict[str, Any] = {}
    if isinstance(stored_callable, dict):
        for row in stored_callable.get("expected_inputs") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            ref = str(row.get("default_secret_ref") or "").strip()
            field_type = str(row.get("type") or "").strip().lower()
            if name and ref and field_type == "secret":
                stored_refs[name] = ref
            if name and field_type == "file" and isinstance(row.get("sample_file"), dict):
                stored_samples[name] = copy.deepcopy(row["sample_file"])

    merged_rows: list[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            merged_rows.append(row)
            continue
        merged = dict(row)
        name = str(merged.get("name") or "").strip()
        field_type = str(merged.get("type") or "").strip().lower()
        # Public payloads never carry authority. Always remove whatever the
        # caller supplied, then restore only this workflow's stored same-field
        # reference when the field still has that type.
        merged.pop("default_secret_ref", None)
        merged.pop("default_configured", None)
        merged.pop("sample_file", None)
        if field_type == "secret" and name in stored_refs:
            merged["default_secret_ref"] = stored_refs[name]
        if field_type == "file" and name in stored_samples:
            merged["sample_file"] = stored_samples[name]
        merged_rows.append(merged)

    return {
        **incoming,
        "callable": {**incoming_callable, "expected_inputs": merged_rows},
    }
