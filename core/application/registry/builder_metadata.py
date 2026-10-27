"""Builder-facing metadata helpers for registry primitives."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MINIMAL_BUILDER_METADATA_KEYS: tuple[str, ...] = (
    "purpose",
    "use_cases",
    "anti_use_cases",
    "composition_role",
    "determinism_level",
    "safe_for_auto_use",
)

DEFAULT_BUILDER_METADATA: dict[str, Any] = {
    "purpose": "",
    "use_cases": [],
    "anti_use_cases": [],
    "composition_role": "",
    "determinism_level": "hybrid",
    "safe_for_auto_use": False,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        text = _text(item)
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def normalize_builder_metadata(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, Mapping) else {}
    normalized = dict(DEFAULT_BUILDER_METADATA)
    normalized["purpose"] = _text(payload.get("purpose"))
    normalized["use_cases"] = _string_list(payload.get("use_cases"))
    normalized["anti_use_cases"] = _string_list(payload.get("anti_use_cases"))
    normalized["composition_role"] = _text(payload.get("composition_role"))
    normalized["determinism_level"] = _text(payload.get("determinism_level")) or "hybrid"
    normalized["safe_for_auto_use"] = bool(payload.get("safe_for_auto_use", False))
    return normalized


def builder_metadata_summary(value: Any) -> str:
    metadata = normalize_builder_metadata(value)
    parts: list[str] = []
    purpose = _text(metadata.get("purpose"))
    if purpose:
        parts.append(purpose)
    composition_role = _text(metadata.get("composition_role"))
    if composition_role:
        parts.append(f"Role: {composition_role}.")
    use_cases = _string_list(metadata.get("use_cases"))
    if use_cases:
        parts.append(f"Use cases: {', '.join(use_cases[:4])}.")
    anti_use_cases = _string_list(metadata.get("anti_use_cases"))
    if anti_use_cases:
        parts.append(f"Avoid for: {', '.join(anti_use_cases[:3])}.")
    determinism_level = _text(metadata.get("determinism_level"))
    if determinism_level:
        parts.append(f"Determinism: {determinism_level}.")
    if metadata.get("safe_for_auto_use"):
        parts.append("Safe for automatic use.")
    return " ".join(part for part in parts if part).strip()
