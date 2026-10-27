"""Authenticated codec for external-effect replay payloads."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.infrastructure.security.secret_codec import derive_secret_storage_key

_VERSION = "v1"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_KEY_CONTEXT = b"flow-steward.external-effect-replay.aesgcm.v1"
_AAD_CONTEXT = b"flow-steward.external-effect-replay.binding.v1\x00"
_BINDING_FIELDS = (
    "effect_id",
    "account_id",
    "project_id",
    "extension_id",
    "extension_install_id",
    "connection_id",
    "operation_id",
    "idempotency_scope_hash",
    "idempotency_key",
    "request_fingerprint",
)


class ExternalEffectReplayCodecError(ValueError):
    """Ciphertext is malformed or is not authentic for the supplied binding."""


def canonical_replay_binding(binding: Mapping[str, Any]) -> bytes:
    normalized = {field: str(binding.get(field) or "").strip() for field in _BINDING_FIELDS}
    if not normalized["effect_id"] or not normalized["account_id"]:
        raise ExternalEffectReplayCodecError("external effect replay binding is incomplete")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def replay_binding_hash(binding: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_replay_binding(binding)).hexdigest()


def _aad(binding: Mapping[str, Any]) -> bytes:
    return _AAD_CONTEXT + canonical_replay_binding(binding)


def _key(master_key: bytes | None) -> bytes:
    return derive_secret_storage_key(_KEY_CONTEXT, master_key=master_key)


def encrypt_external_effect_replay(
    plaintext: bytes,
    *,
    binding: Mapping[str, Any],
    master_key: bytes | None = None,
) -> str:
    if not isinstance(plaintext, bytes):
        raise TypeError("external effect replay plaintext must be bytes")
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(_key(master_key)).encrypt(nonce, plaintext, _aad(binding))
    return f"{_VERSION}:{base64.b64encode(nonce + ciphertext).decode('ascii')}"


def decrypt_external_effect_replay(
    encoded: str,
    *,
    binding: Mapping[str, Any],
    master_key: bytes | None = None,
) -> bytes:
    prefix, separator, payload = str(encoded or "").partition(":")
    if separator != ":" or prefix != _VERSION or not payload:
        raise ExternalEffectReplayCodecError("unsupported external effect replay ciphertext")
    try:
        raw = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise ExternalEffectReplayCodecError("malformed external effect replay ciphertext") from exc
    if len(raw) < _NONCE_BYTES + _TAG_BYTES:
        raise ExternalEffectReplayCodecError("truncated external effect replay ciphertext")
    try:
        return AESGCM(_key(master_key)).decrypt(
            raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _aad(binding)
        )
    except (InvalidTag, ValueError) as exc:
        raise ExternalEffectReplayCodecError(
            "external effect replay ciphertext authentication failed"
        ) from exc


__all__ = [
    "ExternalEffectReplayCodecError",
    "canonical_replay_binding",
    "decrypt_external_effect_replay",
    "encrypt_external_effect_replay",
    "replay_binding_hash",
]
