from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from core.application.clone.contracts import CloneEnvelopeHeader, CloneKdfHeader


def test_clone_contracts_reject_unknown_fields_and_secret_output() -> None:
    salt = base64.urlsafe_b64encode(b"0123456789abcdef").decode("ascii")
    kdf = CloneKdfHeader(salt=salt)
    header = CloneEnvelopeHeader(kdf=kdf)
    payload = {**kdf.model_dump(), "passphrase": "do-not-store"}

    with pytest.raises(ValidationError):
        CloneKdfHeader.model_validate(payload)

    serialized = header.model_dump_json()
    assert "passphrase" not in serialized
    assert "do-not-store" not in serialized


@pytest.mark.parametrize(
    ("field", "value"),
    (("n", 2**20), ("r", 33), ("p", 17), ("length", 64)),
)
def test_clone_contracts_reject_unbounded_scrypt_parameters(
    field: str,
    value: int,
) -> None:
    payload = {
        "schema_version": 1,
        "name": "scrypt",
        "salt": base64.urlsafe_b64encode(b"0123456789abcdef").decode("ascii"),
        "n": 2**14,
        "r": 8,
        "p": 1,
        "length": 32,
        field: value,
    }

    with pytest.raises(ValidationError):
        CloneKdfHeader.model_validate(payload)
