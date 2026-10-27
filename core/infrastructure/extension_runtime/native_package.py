"""Host-owned contract for target-specific native extension packages."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

NATIVE_EXECUTABLE_FIELD = "native_executable"

_ELF_MACHINE_BY_TARGET = {
    "linux-amd64": 62,
    "linux-arm64": 183,
}
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


_WHEEL_ARCHITECTURE_BY_TARGET = {"linux-amd64": "x86_64", "linux-arm64": "aarch64"}


def wheel_supports_runtime_target(filename: str, runtime_target: str) -> bool:
    """Whether a wheel installs on a runtime target: pure, or built for its Linux architecture."""
    name = PurePosixPath(filename).name
    if not name.endswith(".whl"):
        return True
    platforms = name[: -len(".whl")].rsplit("-", 1)[-1].split(".")
    architecture = _WHEEL_ARCHITECTURE_BY_TARGET[runtime_target]
    return any(
        platform == "any"
        or (
            platform.startswith(("manylinux", "musllinux", "linux"))
            and platform.endswith(f"_{architecture}")
        )
        for platform in platforms
    )


def native_target_payload_paths(bundle_root: Path) -> list[Path]:
    """Return files stored below a canonical ``bin/<runtime-target>`` directory."""
    payloads: list[Path] = []
    for path in bundle_root.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(bundle_root).parts
        if any(
            part == "bin"
            and index + 1 < len(relative_parts)
            and relative_parts[index + 1] in _ELF_MACHINE_BY_TARGET
            for index, part in enumerate(relative_parts)
        ):
            payloads.append(path)
    return sorted(payloads)


def _elf_machine(path: Path) -> int:
    header = path.read_bytes()[:20]
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2:
        raise ValueError(f"Native executable '{path.name}' must be an ELF64 binary")
    if header[5] == 1:
        return int.from_bytes(header[18:20], "little")
    if header[5] == 2:
        return int.from_bytes(header[18:20], "big")
    raise ValueError(f"Native executable '{path.name}' has invalid ELF byte order")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _native_binaries(bundle_root: Path) -> list[Path]:
    bin_root = bundle_root / "bin"
    if not bin_root.is_dir():
        return []
    binaries: list[Path] = []
    for path in sorted(bin_root.rglob("*")):
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            if handle.read(4) == b"\x7fELF":
                binaries.append(path)
    return binaries


def build_native_package_metadata(bundle_root: Path, *, runtime_target: str) -> dict[str, str]:
    target_root = bundle_root / "bin" / runtime_target
    candidates = [path for path in _native_binaries(bundle_root) if target_root in path.parents]
    if len(candidates) != 1:
        raise ValueError(
            f"Target package '{runtime_target}' must contain exactly one native executable; "
            f"found {len(candidates)}"
        )
    executable = candidates[0]
    if not os.access(executable, os.X_OK):
        raise ValueError(f"Native executable '{executable.name}' must have executable permission")
    _require_elf_machine(executable, runtime_target=runtime_target)
    return {
        "path": executable.relative_to(bundle_root).as_posix(),
        "sha256": _sha256(executable),
    }


def validate_native_target_package(
    bundle_root: Path,
    *,
    package_metadata: dict[str, Any],
    runtime_target: str,
) -> None:
    declaration = package_metadata.get(NATIVE_EXECUTABLE_FIELD)
    if declaration is None:
        # A target package without a native executable: one built for this
        # architecture because its Python wheels are, say. It may then carry no
        # native binary at all, so nothing undeclared can run.
        binaries = _native_binaries(bundle_root)
        if binaries:
            raise ValueError(
                "Extension package contains a native executable but declares none: "
                + ", ".join(path.relative_to(bundle_root).as_posix() for path in binaries)
            )
        return
    if not isinstance(declaration, dict):
        raise ValueError("Extension package metadata must declare one native_executable")
    declared_path = str(declaration.get("path") or "").strip()
    declared_digest = str(declaration.get("sha256") or "").strip().lower()
    relative = PurePosixPath(declared_path)
    if (
        not declared_path
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[:2] != ("bin", runtime_target)
        or len(relative.parts) < 3
    ):
        raise ValueError(f"Native executable path must be inside 'bin/{runtime_target}'")
    if _SHA256_PATTERN.fullmatch(declared_digest) is None:
        raise ValueError("Native executable SHA-256 must be 64 lowercase hexadecimal characters")

    executable = bundle_root.joinpath(*relative.parts)
    if not executable.is_file():
        raise ValueError(f"Declared native executable '{declared_path}' is missing")
    binaries = _native_binaries(bundle_root)
    if binaries != [executable]:
        undeclared = [
            path.relative_to(bundle_root).as_posix() for path in binaries if path != executable
        ]
        if undeclared:
            raise ValueError(
                "Extension package contains undeclared target binary: " + ", ".join(undeclared)
            )
        raise ValueError("Extension package must contain exactly one declared native executable")
    if _sha256(executable) != declared_digest:
        raise ValueError(f"Native executable SHA-256 mismatch for '{declared_path}'")
    _require_elf_machine(executable, runtime_target=runtime_target)
    if not os.access(executable, os.X_OK):
        raise ValueError(f"Native executable '{declared_path}' is not executable")


def _require_elf_machine(path: Path, *, runtime_target: str) -> None:
    expected = _ELF_MACHINE_BY_TARGET[runtime_target]
    actual = _elf_machine(path)
    if actual != expected:
        expected_name = "x86_64" if expected == 62 else "AArch64"
        raise ValueError(
            f"Native executable ELF machine {actual} does not match {expected_name} "
            f"for '{runtime_target}'"
        )
