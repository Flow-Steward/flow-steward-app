#!/usr/bin/env python3
"""Build, sync, and verify the one canonical public extension SDK wheel."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import tomllib
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = "flow-steward-extension-sdk-wheel-v2"
SDK_RELATIVE_ROOT = Path("core/infrastructure/extension_sdk/public")
ATTESTATION_NAME = "sdk-wheel-attestation.json"


class SdkWheelContractError(RuntimeError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_files(root: Path) -> list[Path]:
    sdk_root = root / SDK_RELATIVE_ROOT
    files = [sdk_root / "pyproject.toml", sdk_root / "README.md"]
    files.extend(sorted((sdk_root / "flowsteward_extension_sdk").glob("*.py")))
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise SdkWheelContractError(f"SDK source file is missing: {missing[0]}")
    return files


def _source_identity(root: Path) -> tuple[str, list[Path]]:
    sdk_root = root / SDK_RELATIVE_ROOT
    files = _source_files(root)
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(sdk_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest(), files


def _metadata(root: Path) -> tuple[str, str]:
    payload = tomllib.loads((root / SDK_RELATIVE_ROOT / "pyproject.toml").read_text("utf-8"))
    project = payload.get("project") if isinstance(payload, dict) else {}
    project = project if isinstance(project, dict) else {}
    name = str(project.get("name") or "").strip()
    version = str(project.get("version") or "").strip()
    if name != "flowsteward-extension-sdk" or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SdkWheelContractError("SDK project name/version is invalid")
    return name, version


def _wheel_timestamp(source_sha256: str) -> tuple[int, int, int, int, int, int]:
    base = datetime(2000, 1, 1, tzinfo=UTC)
    instant = base + timedelta(seconds=int(source_sha256[:8], 16) % (50 * 365 * 24 * 3600))
    return (instant.year, instant.month, instant.day, instant.hour, instant.minute, instant.second)


def _record_row(path: str, content: bytes) -> list[str]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
    return [path, f"sha256={digest}", str(len(content))]


def build_wheel_bytes(root: Path) -> tuple[str, bytes, dict[str, Any]]:
    root = root.resolve()
    source_sha256, source_files = _source_identity(root)
    project_name, version = _metadata(root)
    distribution = project_name.replace("-", "_")
    wheel_name = f"{distribution}-{version}-py3-none-any.whl"
    dist_info = f"{distribution}-{version}.dist-info"
    sdk_root = root / SDK_RELATIVE_ROOT
    entries: dict[str, bytes] = {}
    for path in source_files:
        if path.suffix == ".py":
            entries[path.relative_to(sdk_root).as_posix()] = path.read_bytes()
    # Published on PyPI as flowsteward-extension-sdk, so this is what an extension
    # author reads there; the README is the long description.
    readme = (sdk_root / "README.md").read_text(encoding="utf-8")
    entries[f"{dist_info}/METADATA"] = (
        "Metadata-Version: 2.3\n"
        f"Name: {project_name}\n"
        f"Version: {version}\n"
        "Summary: Public SDK for Flow Steward extensions: artifact grants, SSRF-safe HTTP, "
        "runtime context and test-kit helpers.\n"
        "Author: Flow Steward\n"
        "License: Apache-2.0\n"
        "Project-URL: Homepage, https://github.com/Flow-Steward/flow-steward-app\n"
        "Project-URL: Extension catalog, https://github.com/Flow-Steward/extensions\n"
        "Classifier: License :: OSI Approved :: Apache Software License\n"
        "Classifier: Programming Language :: Python :: 3\n"
        "Classifier: Programming Language :: Python :: 3.12\n"
        "Requires-Python: >=3.11\n"
        # Imported lazily by the SDK (signed artifact checks; S3 artifact storage).
        "Requires-Dist: cryptography>=42\n"
        "Provides-Extra: s3\n"
        'Requires-Dist: boto3>=1.34; extra == "s3"\n'
        "Description-Content-Type: text/markdown\n\n"
        f"{readme}"
    ).encode()
    entries[f"{dist_info}/WHEEL"] = (
        b"Wheel-Version: 1.0\n"
        b"Generator: flow-steward-sdk-wheel-contract\n"
        b"Root-Is-Purelib: true\n"
        b"Tag: py3-none-any\n"
    )
    entries[f"{dist_info}/top_level.txt"] = b"flowsteward_extension_sdk\n"
    record_path = f"{dist_info}/RECORD"
    rows = [_record_row(path, entries[path]) for path in sorted(entries)]
    rows.append([record_path, "", ""])
    record_buffer = io.StringIO(newline="")
    csv.writer(record_buffer, lineterminator="\n").writerows(rows)
    entries[record_path] = record_buffer.getvalue().encode("utf-8")

    output = io.BytesIO()
    timestamp = _wheel_timestamp(source_sha256)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(entries):
            info = zipfile.ZipInfo(str(path), date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(
                info, entries[path], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
            )
    wheel = output.getvalue()
    return (
        wheel_name,
        wheel,
        {
            "schema": SCHEMA,
            "project": project_name,
            "version": version,
            "source_sha256": source_sha256,
            "wheel_filename": wheel_name,
            "wheel_sha256": _sha256_bytes(wheel),
            "python_requires": ">=3.11",
            "wheel_tags": ["py3-none-any"],
            "architectures": ["any"],
        },
    )


def _extension_consumers(root: Path) -> list[tuple[str, Path]]:
    consumers: list[tuple[str, Path]] = []
    for manifest in sorted((root / "extensions").glob("*/extension.yaml")):
        extension_id = ""
        for line in manifest.read_text("utf-8").splitlines():
            match = re.match(r"^extension_id:\s*['\"]?([^'\"#\s]+)", line)
            if match:
                extension_id = match.group(1)
                break
        if not extension_id:
            raise SdkWheelContractError(f"extension_id is missing: {manifest}")
        requirements = manifest.parent / "requirements-dev.txt"
        if not requirements.is_file():
            raise SdkWheelContractError(f"standalone requirements are missing: {requirements}")
        consumers.append((extension_id, manifest.parent))
    # Core ships no extensions, so normally there are none: extensions install the
    # SDK from PyPI. A checkout that still vendors the wheel is kept in step.
    return consumers


def _expected_attestation(
    base: dict[str, Any], consumers: list[tuple[str, Path]]
) -> dict[str, Any]:
    """The attestation states what the wheel *is*, not who happens to consume it.

    Which bundles vendor the wheel is still verified below, every run — but it is
    not written into the platform, because a list of concrete extensions inside
    `core/` is knowledge Core must not hold and would go stale the moment a bundle
    moves to its own repository.
    """
    _ = consumers
    return dict(base)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync(root: Path) -> dict[str, Any]:
    root = root.resolve()
    wheel_name, wheel, base = build_wheel_bytes(root)
    consumers = _extension_consumers(root)
    expected_requirement = f"./dev-wheels/{wheel_name}"
    for _extension_id, extension_root in consumers:
        wheel_dir = extension_root / "dev-wheels"
        for old_wheel in wheel_dir.glob("flowsteward_extension_sdk-*.whl"):
            if old_wheel.name != wheel_name:
                old_wheel.unlink()
        _atomic_write(wheel_dir / wheel_name, wheel)
        requirements = extension_root / "requirements-dev.txt"
        lines = requirements.read_text("utf-8").splitlines()
        replaced = False
        for index, line in enumerate(lines):
            if "flowsteward_extension_sdk-" in line:
                lines[index] = expected_requirement
                replaced = True
        if not replaced:
            lines.insert(0, expected_requirement)
        _atomic_write(requirements, ("\n".join(lines) + "\n").encode("utf-8"))
    attestation = _expected_attestation(base, consumers)
    _atomic_write(
        root / SDK_RELATIVE_ROOT / ATTESTATION_NAME,
        (json.dumps(attestation, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return attestation


def check(root: Path) -> dict[str, Any]:
    root = root.resolve()
    wheel_name, wheel, base = build_wheel_bytes(root)
    consumers = _extension_consumers(root)
    errors: list[str] = []
    expected_requirement = f"./dev-wheels/{wheel_name}"
    for extension_id, extension_root in consumers:
        wheels = sorted((extension_root / "dev-wheels").glob("flowsteward_extension_sdk-*.whl"))
        if [path.name for path in wheels] != [wheel_name]:
            errors.append(f"{extension_id}: expected exactly {wheel_name}")
            continue
        actual = wheels[0].read_bytes()
        if actual != wheel:
            errors.append(
                f"{extension_id}: stale SDK wheel "
                f"source={base['source_sha256']} expected={base['wheel_sha256']} "
                f"actual={_sha256_bytes(actual)}"
            )
        requirements = (extension_root / "requirements-dev.txt").read_text("utf-8").splitlines()
        sdk_lines = [line.strip() for line in requirements if "flowsteward_extension_sdk-" in line]
        if sdk_lines != [expected_requirement]:
            errors.append(f"{extension_id}: requirements must reference {expected_requirement}")
    attestation_path = root / SDK_RELATIVE_ROOT / ATTESTATION_NAME
    expected_attestation = _expected_attestation(base, consumers)
    try:
        actual_attestation = json.loads(attestation_path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"SDK attestation is missing or invalid: {exc}")
    else:
        if actual_attestation != expected_attestation:
            errors.append("SDK attestation does not match canonical source/wheel/consumers")
    if errors:
        raise SdkWheelContractError("\n".join(errors))
    return expected_attestation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--sync", action="store_true")
    mode.add_argument(
        "--out", type=Path, help="write the wheel into this directory, e.g. for a PyPI upload"
    )
    args = parser.parse_args()
    try:
        if args.out:
            result = check(args.root)
            wheel_name, wheel, _ = build_wheel_bytes(args.root.resolve())
            _atomic_write(args.out / wheel_name, wheel)
            result = {**result, "written": str(args.out / wheel_name)}
        else:
            result = check(args.root) if args.check else sync(args.root)
    except SdkWheelContractError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
