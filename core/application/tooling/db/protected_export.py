#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkstemp
from typing import BinaryIO

if __package__ in {None, ""}:
    _repo_root = str(Path(__file__).resolve().parents[4])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

from core.infrastructure.runtime_env import runtime_env as _runtime_env

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception as exc:  # pragma: no cover - exercised via CLI tests
    AESGCM = None
    Cipher = None
    algorithms = None
    modes = None
    InvalidTag = Exception
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


_TRUNCATED_PAYLOAD_ERROR = "Protected export payload is truncated"
_AUTHENTICATION_FAILED_ERROR = (
    "Protected export authentication failed; the backup key is wrong or the file is damaged"
)
_MAGIC_AESGCM = b"protected-export:aesgcm-v1\n"
_MAGIC_AESGCM_STREAM = b"protected-export:aesgcm-stream-v2\n"
_MAGIC_OPENSSL = b"protected-export:openssl-aes256cbc-v1\n"
_AESGCM_NONCE_BYTES = 12
_AESGCM_TAG_BYTES = 16
_STREAM_CHUNK_BYTES = 1024 * 1024

KeySource = Callable[[], str | bytes]


def backup_encryption_key_from_environment() -> str:
    """Return only the dedicated durable backup key."""
    raw = str(_runtime_env.get("FS_BACKUP_ENCRYPTION_KEY", "") or "").strip()
    if not raw:
        raise RuntimeError("FS_BACKUP_ENCRYPTION_KEY is required for protected backups")
    return raw


def _key_bytes(key_source: KeySource) -> bytes:
    raw_value = key_source()
    raw = raw_value if isinstance(raw_value, bytes) else str(raw_value).encode()
    raw = raw.strip()
    if not raw:
        raise RuntimeError("FS_BACKUP_ENCRYPTION_KEY is required for protected backups")
    return hashlib.sha256(b"backup-export:" + raw).digest()


def _openssl_path() -> str:
    binary = shutil.which("openssl")
    if not binary:
        raise RuntimeError("openssl is required when cryptography/AESGCM is unavailable")
    return binary


def _openssl_passphrase(key_source: KeySource) -> str:
    return _key_bytes(key_source).hex()


def encrypt_payload(payload: bytes, *, key_source: KeySource) -> bytes:
    if AESGCM is None:
        raise RuntimeError(
            f"cryptography/AESGCM is required for protected exports: {_IMPORT_ERROR}"
        )
    key = _key_bytes(key_source)
    nonce = os.urandom(_AESGCM_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, payload, None)
    return (
        _MAGIC_AESGCM
        + base64.urlsafe_b64encode(nonce)
        + b"\n"
        + base64.urlsafe_b64encode(ciphertext)
        + b"\n"
    )


def encrypt_stream(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    key_source: KeySource | None = None,
    key: bytes | None = None,
    associated_data: bytes = b"",
    chunk_bytes: int = _STREAM_CHUNK_BYTES,
) -> None:
    """Encrypt a stream with one artifact-wide AES-GCM authentication tag."""
    if Cipher is None or algorithms is None or modes is None:
        raise RuntimeError(
            f"cryptography streaming AES-GCM is required for protected exports: {_IMPORT_ERROR}"
        )
    stream_key = _stream_key(key_source=key_source, key=key)
    nonce = os.urandom(_AESGCM_NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(stream_key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(_MAGIC_AESGCM_STREAM + associated_data)
    destination.write(_MAGIC_AESGCM_STREAM)
    destination.write(nonce)
    while chunk := source.read(chunk_bytes):
        destination.write(encryptor.update(chunk))
    destination.write(encryptor.finalize())
    destination.write(encryptor.tag)


def decrypt_stream(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    key_source: KeySource | None = None,
    key: bytes | None = None,
    associated_data: bytes = b"",
    envelope_offset: int = 0,
    chunk_bytes: int = _STREAM_CHUNK_BYTES,
) -> None:
    """Decrypt into a private staging stream and fail closed on authentication."""
    if Cipher is None or algorithms is None or modes is None:
        raise RuntimeError(
            f"cryptography streaming AES-GCM is required for protected exports: {_IMPORT_ERROR}"
        )
    nonce, ciphertext_bytes, tag = _stream_envelope_parts(
        source,
        envelope_offset=envelope_offset,
    )
    stream_key = _stream_key(key_source=key_source, key=key)
    decryptor = Cipher(algorithms.AES(stream_key), modes.GCM(nonce, tag)).decryptor()
    decryptor.authenticate_additional_data(_MAGIC_AESGCM_STREAM + associated_data)
    remaining = ciphertext_bytes
    try:
        while remaining:
            chunk = source.read(min(chunk_bytes, remaining))
            if not chunk:
                raise RuntimeError(_TRUNCATED_PAYLOAD_ERROR)
            remaining -= len(chunk)
            destination.write(decryptor.update(chunk))
        destination.write(decryptor.finalize())
    except InvalidTag as exc:
        _discard_untrusted_plaintext(destination)
        raise RuntimeError(_AUTHENTICATION_FAILED_ERROR) from exc
    except Exception:
        _discard_untrusted_plaintext(destination)
        raise


def _stream_key(*, key_source: KeySource | None, key: bytes | None) -> bytes:
    if (key_source is None) == (key is None):
        raise RuntimeError("exactly one protected export key source is required")
    if key is not None:
        if len(key) != 32:
            raise RuntimeError("protected export explicit key must be exactly 32 bytes")
        return key
    assert key_source is not None
    return _key_bytes(key_source)


def encrypt_file(source: Path, destination: Path, *, key_source: KeySource) -> None:
    with source.open("rb") as source_file, destination.open("wb") as destination_file:
        encrypt_stream(source_file, destination_file, key_source=key_source)


def decrypt_file(source: Path, destination: Path, *, key_source: KeySource) -> None:
    with (
        source.open("rb") as source_file,
        _anonymous_owner_only_temp(destination.parent) as authenticated_plaintext,
    ):
        decrypt_stream(source_file, authenticated_plaintext, key_source=key_source)
        authenticated_plaintext.seek(0)
        with destination.open("wb") as destination_file:
            try:
                shutil.copyfileobj(
                    authenticated_plaintext,
                    destination_file,
                    _STREAM_CHUNK_BYTES,
                )
            except Exception:
                _discard_untrusted_plaintext(destination_file)
                raise


def validate_authenticated_file(source: Path) -> None:
    with source.open("rb") as source_file:
        _stream_envelope_parts(source_file)


def _stream_envelope_parts(
    source: BinaryIO,
    *,
    envelope_offset: int = 0,
) -> tuple[bytes, int, bytes]:
    if envelope_offset < 0:
        raise RuntimeError("Protected export envelope offset must not be negative")
    source.seek(0, os.SEEK_END)
    total_bytes = source.tell()
    minimum_bytes = len(_MAGIC_AESGCM_STREAM) + _AESGCM_NONCE_BYTES + _AESGCM_TAG_BYTES
    if total_bytes - envelope_offset <= minimum_bytes:
        raise RuntimeError(_TRUNCATED_PAYLOAD_ERROR)
    source.seek(envelope_offset)
    if source.read(len(_MAGIC_AESGCM_STREAM)) != _MAGIC_AESGCM_STREAM:
        raise RuntimeError("Input is not an authenticated streaming protected export")
    nonce = source.read(_AESGCM_NONCE_BYTES)
    if len(nonce) != _AESGCM_NONCE_BYTES:
        raise RuntimeError(_TRUNCATED_PAYLOAD_ERROR)
    ciphertext_bytes = total_bytes - envelope_offset - minimum_bytes
    source.seek(total_bytes - _AESGCM_TAG_BYTES)
    tag = source.read(_AESGCM_TAG_BYTES)
    if len(tag) != _AESGCM_TAG_BYTES:
        raise RuntimeError(_TRUNCATED_PAYLOAD_ERROR)
    source.seek(envelope_offset + len(_MAGIC_AESGCM_STREAM) + _AESGCM_NONCE_BYTES)
    return nonce, ciphertext_bytes, tag


def _discard_untrusted_plaintext(destination: BinaryIO) -> None:
    try:
        destination.seek(0)
        destination.truncate(0)
    except (AttributeError, OSError):
        return


@contextmanager
def _anonymous_owner_only_temp(directory: Path) -> Iterator[BinaryIO]:
    descriptor, raw_path = mkstemp(dir=directory, prefix=".protected-export-")
    path = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        path.unlink()
        with os.fdopen(descriptor, "w+b") as temporary:
            descriptor = -1
            yield temporary
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)


def _canonical_backups_directory() -> Path:
    default_root = Path(__file__).resolve().parents[4]
    artifact_root = Path(_runtime_env.get("FS_ARTIFACT_ROOT") or default_root)
    backups = artifact_root / "backups"
    if backups.is_symlink():
        raise RuntimeError("Canonical backups directory must not be a symbolic link")
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    backups.chmod(0o700)
    return backups.resolve(strict=True)


def validate_envelope(payload: bytes) -> None:
    """Validate envelope structure without decrypting its protected content."""
    if payload.startswith(_MAGIC_AESGCM):
        lines = payload.splitlines()
        if len(lines) != 3:
            raise RuntimeError(_TRUNCATED_PAYLOAD_ERROR)
        nonce = _decode_base64(lines[1])
        ciphertext = _decode_base64(lines[2])
        if len(nonce) != _AESGCM_NONCE_BYTES or len(ciphertext) < _AESGCM_TAG_BYTES:
            raise RuntimeError(_TRUNCATED_PAYLOAD_ERROR)
        return

    if payload.startswith(_MAGIC_OPENSSL):
        lines = payload.splitlines()
        if len(lines) != 2 or not _decode_base64(lines[1]):
            raise RuntimeError(_TRUNCATED_PAYLOAD_ERROR)
        return
    raise RuntimeError("Input is not a protected export payload")


def decrypt_payload(payload: bytes, *, key_source: KeySource) -> bytes:
    validate_envelope(payload)
    if payload.startswith(_MAGIC_AESGCM):
        lines = payload.splitlines()
        nonce = _decode_base64(lines[1])
        ciphertext = _decode_base64(lines[2])
        key = _key_bytes(key_source)
        if AESGCM is None:
            raise RuntimeError(
                f"cryptography/AESGCM is required for AES-GCM protected exports: {_IMPORT_ERROR}"
            )
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            raise RuntimeError(_AUTHENTICATION_FAILED_ERROR) from exc

    if payload.startswith(_MAGIC_OPENSSL):
        lines = payload.splitlines()
        encrypted = _decode_base64(lines[1])
        passphrase_environment = os.environ.copy()
        passphrase_environment["FS_BACKUP_OPENSSL_PASSPHRASE"] = _openssl_passphrase(key_source)
        try:
            return subprocess.run(
                [
                    _openssl_path(),
                    "enc",
                    "-d",
                    "-aes-256-cbc",
                    "-pbkdf2",
                    "-pass",
                    "env:FS_BACKUP_OPENSSL_PASSPHRASE",
                ],
                input=encrypted,
                capture_output=True,
                check=True,
                env=passphrase_environment,
            ).stdout
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(_AUTHENTICATION_FAILED_ERROR) from exc
    raise RuntimeError("Input is not a protected export payload")  # pragma: no cover


def _decode_base64(payload: bytes) -> bytes:
    try:
        return base64.b64decode(payload, altchars=b"-_", validate=True)
    except ValueError as exc:
        raise RuntimeError("Protected export payload is malformed or truncated") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Encrypt or decrypt protected export payloads.")
    parser.add_argument("mode", choices=("encrypt", "decrypt", "decrypt-legacy"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        if (args.input is None) != (args.output is None):
            raise RuntimeError("--input and --output must be provided together")
        if args.input is not None and args.output is not None:
            _run_file_mode(args.mode, args.input, args.output)
        else:
            _run_stdio_mode(args.mode)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _run_file_mode(mode: str, source: Path, destination: Path) -> None:
    if mode == "encrypt":
        encrypt_file(source, destination, key_source=backup_encryption_key_from_environment)
    elif mode == "decrypt":
        decrypt_file(source, destination, key_source=backup_encryption_key_from_environment)
    else:
        destination.write_bytes(
            decrypt_payload(
                source.read_bytes(),
                key_source=backup_encryption_key_from_environment,
            )
        )


def _run_stdio_mode(mode: str) -> None:
    if mode == "encrypt":
        encrypt_stream(
            sys.stdin.buffer,
            sys.stdout.buffer,
            key_source=backup_encryption_key_from_environment,
        )
    elif mode == "decrypt":
        backups = _canonical_backups_directory()
        with (
            _anonymous_owner_only_temp(backups) as protected_input,
            _anonymous_owner_only_temp(backups) as authenticated_plaintext,
        ):
            shutil.copyfileobj(sys.stdin.buffer, protected_input, _STREAM_CHUNK_BYTES)
            protected_input.seek(0)
            decrypt_stream(
                protected_input,
                authenticated_plaintext,
                key_source=backup_encryption_key_from_environment,
            )
            authenticated_plaintext.seek(0)
            shutil.copyfileobj(authenticated_plaintext, sys.stdout.buffer, _STREAM_CHUNK_BYTES)
    else:
        sys.stdout.buffer.write(
            decrypt_payload(
                sys.stdin.buffer.read(),
                key_source=backup_encryption_key_from_environment,
            )
        )


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
