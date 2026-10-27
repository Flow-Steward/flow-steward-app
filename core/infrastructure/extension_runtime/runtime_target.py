"""Canonical extension package compatibility target."""

from __future__ import annotations

import platform

SUPPORTED_EXTENSION_RUNTIME_TARGETS = ("linux-amd64", "linux-arm64")


def normalize_extension_runtime_target(*, system: str = "", machine: str = "") -> str:
    normalized_system = str(system or platform.system()).strip().lower()
    normalized_machine = str(machine or platform.machine()).strip().lower()
    if normalized_system != "linux":
        raise ValueError(f"Unsupported extension runtime operating system '{normalized_system}'")
    if normalized_machine in {"x86_64", "amd64"}:
        return "linux-amd64"
    if normalized_machine in {"aarch64", "arm64"}:
        return "linux-arm64"
    raise ValueError(f"Unsupported extension runtime architecture '{normalized_machine}'")


def extension_runtime_target() -> str:
    return normalize_extension_runtime_target()


def require_extension_runtime_target(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in SUPPORTED_EXTENSION_RUNTIME_TARGETS:
        raise ValueError(
            f"Unsupported extension runtime target '{normalized or value}'; expected one of "
            f"{', '.join(SUPPORTED_EXTENSION_RUNTIME_TARGETS)}"
        )
    return normalized


__all__ = [
    "SUPPORTED_EXTENSION_RUNTIME_TARGETS",
    "extension_runtime_target",
    "normalize_extension_runtime_target",
    "require_extension_runtime_target",
]
