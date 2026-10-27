"""Versioned scrypt framing over the shared streaming AES-GCM primitive."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Callable
from typing import BinaryIO

from core.application.clone.contracts import CloneEnvelopeHeader, CloneKdfHeader
from core.application.tooling.db.protected_export import decrypt_stream, encrypt_stream

CLONE_ENVELOPE_MAGIC = b"flow-steward-clone:v1\n"
_HEADER_LENGTH_BYTES = 4
_MAX_HEADER_BYTES = 4096
_SALT_BYTES = 16
_MAX_PASSPHRASE_BYTES = 4096
_SCRYPT_MAX_MEMORY_BYTES = 256 * 1024 * 1024

PassphraseSource = Callable[[], str | bytes]


def derive_clone_key(passphrase: bytes, header: CloneKdfHeader) -> bytes:
    """Derive the exact 256-bit key permitted by the validated header."""
    if not passphrase:
        raise RuntimeError("Clone passphrase must not be empty")
    if len(passphrase) > _MAX_PASSPHRASE_BYTES:
        raise RuntimeError("Clone passphrase exceeds the supported size")
    return hashlib.scrypt(
        passphrase,
        salt=header.salt_bytes(),
        n=header.n,
        r=header.r,
        p=header.p,
        maxmem=_SCRYPT_MAX_MEMORY_BYTES,
        dklen=header.length,
    )


def encrypt_clone_stream(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    passphrase_source: PassphraseSource,
) -> CloneEnvelopeHeader:
    """Encrypt one Clone payload and return its non-secret authenticated header."""
    header = CloneEnvelopeHeader(
        kdf=CloneKdfHeader(salt=base64.urlsafe_b64encode(os.urandom(_SALT_BYTES)).decode("ascii"))
    )
    header_bytes = _canonical_header(header)
    prefix = _header_prefix(header_bytes)
    destination.write(prefix)
    encrypt_stream(
        source,
        destination,
        key=_derived_key(passphrase_source, header.kdf),
        associated_data=prefix,
    )
    return header


def decrypt_clone_stream(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    passphrase_source: PassphraseSource,
) -> CloneEnvelopeHeader:
    """Authenticate one Clone payload before returning its plaintext stream."""
    magic = source.read(len(CLONE_ENVELOPE_MAGIC))
    if magic != CLONE_ENVELOPE_MAGIC:
        raise RuntimeError("Input is not a versioned Flow Steward Clone archive")
    raw_length = source.read(_HEADER_LENGTH_BYTES)
    if len(raw_length) != _HEADER_LENGTH_BYTES:
        raise RuntimeError("Clone envelope header is truncated")
    header_length = int.from_bytes(raw_length, "big")
    if not 1 <= header_length <= _MAX_HEADER_BYTES:
        raise RuntimeError("Clone envelope header size is outside the supported bounds")
    header_bytes = source.read(header_length)
    if len(header_bytes) != header_length:
        raise RuntimeError("Clone envelope header is truncated")
    try:
        raw_header = json.loads(header_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Clone envelope header is not valid canonical JSON") from exc
    header = CloneEnvelopeHeader.model_validate(raw_header)
    if _canonical_header(header) != header_bytes:
        raise RuntimeError("Clone envelope header is not canonical")
    prefix = magic + raw_length + header_bytes
    decrypt_stream(
        source,
        destination,
        key=_derived_key(passphrase_source, header.kdf),
        associated_data=prefix,
        envelope_offset=len(prefix),
    )
    return header


def _derived_key(source: PassphraseSource, header: CloneKdfHeader) -> bytes:
    raw = source()
    passphrase = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
    return derive_clone_key(passphrase, header)


def _canonical_header(header: CloneEnvelopeHeader) -> bytes:
    return json.dumps(
        header.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _header_prefix(header_bytes: bytes) -> bytes:
    if not 1 <= len(header_bytes) <= _MAX_HEADER_BYTES:
        raise RuntimeError("Clone envelope header size is outside the supported bounds")
    return (
        CLONE_ENVELOPE_MAGIC
        + len(header_bytes).to_bytes(_HEADER_LENGTH_BYTES, "big")
        + header_bytes
    )


__all__ = [
    "CLONE_ENVELOPE_MAGIC",
    "decrypt_clone_stream",
    "derive_clone_key",
    "encrypt_clone_stream",
]
