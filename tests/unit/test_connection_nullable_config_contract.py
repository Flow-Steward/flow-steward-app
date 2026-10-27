"""Contract oracle for known optional null/empty connection values."""

import pytest
from fastapi import HTTPException

from core.entrypoints.admin.datasets_helpers import _validate_connection_config

SCHEMA = {
    "type": "object",
    "required": ["port"],
    "additionalProperties": True,
    "properties": {
        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
        "optional_note": {"type": ["string", "null"]},
        "empty_label": {"type": "string"},
    },
}


def test_connection_schema_keeps_legal_optional_null_and_empty_string():
    config = {"port": 5432, "optional_note": None, "empty_label": ""}
    assert _validate_connection_config(config, schema=SCHEMA) == config


def test_nullable_optional_field_does_not_make_required_integer_nullable():
    with pytest.raises(HTTPException) as exc:
        _validate_connection_config({"port": None}, schema=SCHEMA)
    assert exc.value.status_code == 400
