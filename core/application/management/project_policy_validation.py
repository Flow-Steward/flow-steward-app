"""Shared numeric constraints at project-policy write boundaries."""

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field, TypeAdapter, ValidationError

ProjectPolicyConfidence = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
ProjectConcurrencyCap = Annotated[int, Field(ge=1)]

_CONFIDENCE_ADAPTER = TypeAdapter[float](ProjectPolicyConfidence)
_CONCURRENCY_ADAPTER = TypeAdapter[int](ProjectConcurrencyCap)


def project_policy_numbers_from_form(form_data: Mapping[str, Any]) -> dict[str, float | int]:
    """Preserve blank form defaults, but reject supplied invalid numbers."""
    fields: tuple[tuple[str, TypeAdapter[Any], float | int], ...] = (
        ("autonomy_confidence_threshold", _CONFIDENCE_ADAPTER, 0.8),
        ("concurrency_cap", _CONCURRENCY_ADAPTER, 1),
    )
    result: dict[str, float | int] = {}
    for name, adapter, default in fields:
        field_name = f"policy_{name}"
        value = form_data.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            value = default
        try:
            result[name] = adapter.validate_python(value)
        except ValidationError as exc:
            message = exc.errors(include_input=False, include_context=False, include_url=False)[0][
                "msg"
            ]
            raise ValueError(f"{field_name}: {message}") from exc
    return result
