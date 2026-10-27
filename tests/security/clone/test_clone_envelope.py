from __future__ import annotations

import json
from io import BytesIO

import pytest
from pydantic import ValidationError

from core.application.clone.contracts import CloneKdfHeader
from core.infrastructure.clone.envelope import (
    decrypt_clone_stream,
    encrypt_clone_stream,
)

_MAGIC = b"flow-steward-clone:v1\n"


def _header(payload: bytes) -> tuple[dict[str, object], bytes]:
    assert payload.startswith(_MAGIC)
    length_offset = len(_MAGIC)
    header_length = int.from_bytes(payload[length_offset : length_offset + 4], "big")
    header_offset = length_offset + 4
    header_bytes = payload[header_offset : header_offset + header_length]
    return json.loads(header_bytes), header_bytes


def test_clone_envelope_round_trips_with_scrypt_header_as_aad() -> None:
    encrypted = BytesIO()
    header = encrypt_clone_stream(
        BytesIO(b"manifest and durable product state"),
        encrypted,
        passphrase_source=lambda: b"correct horse battery staple",
    )

    decoded, header_bytes = _header(encrypted.getvalue())
    assert decoded == header.model_dump(mode="json")
    assert header_bytes == json.dumps(
        decoded,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    plaintext = BytesIO()
    returned = decrypt_clone_stream(
        BytesIO(encrypted.getvalue()),
        plaintext,
        passphrase_source=lambda: b"correct horse battery staple",
    )

    assert returned == header
    assert plaintext.getvalue() == b"manifest and durable product state"


def test_clone_envelope_uses_distinct_random_salts() -> None:
    first = BytesIO()
    second = BytesIO()

    first_header = encrypt_clone_stream(
        BytesIO(b"same payload"),
        first,
        passphrase_source=lambda: b"same passphrase",
    )
    second_header = encrypt_clone_stream(
        BytesIO(b"same payload"),
        second,
        passphrase_source=lambda: b"same passphrase",
    )

    assert first_header.kdf.salt != second_header.kdf.salt
    assert first.getvalue() != second.getvalue()


@pytest.mark.parametrize("tamper", ["passphrase", "header"])
def test_clone_envelope_rejects_wrong_passphrase_or_modified_header(tamper: str) -> None:
    encrypted = BytesIO()
    encrypt_clone_stream(
        BytesIO(b"authenticated plaintext"),
        encrypted,
        passphrase_source=lambda: b"right passphrase",
    )
    payload = encrypted.getvalue()
    passphrase = b"wrong passphrase"
    if tamper == "header":
        assert b'"p":1' in payload
        payload = payload.replace(b'"p":1', b'"p":2', 1)
        passphrase = b"right passphrase"
    destination = BytesIO(b"preexisting")
    source = BytesIO(payload)

    with pytest.raises(RuntimeError, match="authentication failed"):
        decrypt_clone_stream(
            source,
            destination,
            passphrase_source=lambda: passphrase,
        )

    assert destination.getvalue() == b""


def test_clone_envelope_rejects_unbounded_scrypt_parameters_before_derivation() -> None:
    payload = {
        "schema_version": 1,
        "name": "scrypt",
        "salt": "MDEyMzQ1Njc4OWFiY2RlZg==",
        "n": 2**20,
        "r": 8,
        "p": 1,
        "length": 32,
    }
    with pytest.raises(ValidationError):
        CloneKdfHeader.model_validate(payload)
