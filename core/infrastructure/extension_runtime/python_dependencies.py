from __future__ import annotations

import csv
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.infrastructure.config.extension_dependencies import (
    EXTENSION_DEPS_DEFAULT_INDEX_URL,
    EXTENSION_DEPS_FETCH_ALLOWLIST,
    EXTENSION_DEPS_FETCH_OFF,
    FS_EXTENSION_DEPS_FETCH_ENV,
    extension_deps_fetch_mode,
    extension_deps_site_max_bytes,
)
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.extension_runtime.python_requirement_contract import (
    PYTHON_REQUIREMENTS_FIELD,
    _issue,
)

try:  # pragma: no cover - exercised in deployed/runtime images
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.tags import Tag, sys_tags
    from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
except ImportError:  # pragma: no cover - local dev fallback when only pip is installed
    from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
    from pip._vendor.packaging.tags import Tag, sys_tags
    from pip._vendor.packaging.utils import (
        InvalidWheelFilename,
        canonicalize_name,
        parse_wheel_filename,
    )


PYTHON_DEPS_ROOT_DIRNAME = ".fs-python"
PYTHON_DEPS_SITE_DIRNAME = "site"
PYTHON_DEPS_WHEELS_DIRNAME = "wheels"
DEFAULT_WHEEL_PLATFORMS = ("manylinux2014_x86_64", "manylinux2014_aarch64")


@dataclass(frozen=True)
class WheelCandidate:
    path: Path
    name: str
    version: str
    tags: frozenset[Tag]
    sha256: str


@dataclass(frozen=True)
class SelectedWheel:
    requirement: dict[str, Any]
    wheel: WheelCandidate
    selected_tag: str


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 64)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def _site_size_limit_bytes() -> int:
    return extension_deps_site_max_bytes()


def extension_wheels_dir(bundle_root: str | Path) -> Path:
    return Path(bundle_root).resolve() / PYTHON_DEPS_WHEELS_DIRNAME


def extension_python_deps_version_dir(
    bundle_root: str | Path,
    *,
    version: str,
) -> Path:
    normalized_version = str(version or "").strip() or "0.0.0"
    return Path(bundle_root).resolve() / PYTHON_DEPS_ROOT_DIRNAME / normalized_version


def extension_python_site_dir_from_parts(
    bundle_root: str | Path,
    *,
    version: str,
) -> Path:
    return (
        extension_python_deps_version_dir(bundle_root, version=version) / PYTHON_DEPS_SITE_DIRNAME
    )


def extension_python_site_dir(extension: dict[str, Any] | None) -> Path | None:
    payload = dict(extension or {})
    bundle_root = str(payload.get("bundle_root") or "").strip()
    if not bundle_root:
        return None
    compiled_manifest = payload.get("compiled_manifest")
    if not isinstance(compiled_manifest, dict):
        compiled_manifest = {}
    # `release_version` is how a release imported from an extension descriptor names
    # it: such a release resolved no site, and ran without its bundle's dependencies.
    version = str(
        payload.get("version")
        or payload.get("latest_version")
        or compiled_manifest.get("version")
        or payload.get("release_version")
        or ""
    ).strip()
    if not version:
        return None
    return extension_python_site_dir_from_parts(bundle_root, version=version)


def extension_sdk_pythonpath_root() -> Path:
    return Path(__file__).resolve().parents[1] / "extension_sdk" / "public"


def extension_runtime_pythonpath(extension: dict[str, Any] | None) -> str:
    roots: list[str] = []
    site_dir = extension_python_site_dir(extension)
    if site_dir is not None:
        roots.append(str(site_dir))
    roots.append(str(extension_sdk_pythonpath_root()))
    return os.pathsep.join(roots)


def _parse_wheel(path: Path) -> WheelCandidate:
    name, version, _build, tags = parse_wheel_filename(path.name)
    return WheelCandidate(
        path=path,
        name=str(name),
        version=str(version),
        tags=frozenset(tags),
        sha256=_sha256_file(path),
    )


def bundled_wheel_candidates(bundle_root: str | Path) -> tuple[list[WheelCandidate], list[str]]:
    return wheel_candidates_from_dir(extension_wheels_dir(bundle_root))


def _remove_local_wheel_direct_url_metadata(site_dir: Path) -> None:
    """Remove pip metadata that embeds the random staging directory.

    The wheel hash and filename remain in Flow Steward's install metadata. PEP 610
    ``direct_url.json`` adds only the temporary local wheel URL here, which makes
    otherwise identical managed release trees differ across retries.
    """
    for direct_url_path in sorted(site_dir.glob("*.dist-info/direct_url.json")):
        record_path = direct_url_path.parent / "RECORD"
        if not record_path.is_file():
            raise ValueError(
                f"Installed wheel metadata is missing RECORD: {record_path.parent.name}"
            )
        relative_direct_url = direct_url_path.relative_to(site_dir).as_posix()
        with record_path.open("r", encoding="utf-8", newline="") as source:
            rows = list(csv.reader(source))
        retained = [row for row in rows if not row or row[0] != relative_direct_url]
        if len(retained) == len(rows):
            raise ValueError(f"Installed wheel RECORD does not reference {relative_direct_url}")
        direct_url_path.unlink()
        with record_path.open("w", encoding="utf-8", newline="") as destination:
            csv.writer(destination, lineterminator="\n").writerows(retained)


def wheel_candidates_from_dir(wheels_dir: str | Path) -> tuple[list[WheelCandidate], list[str]]:
    wheels_dir = Path(wheels_dir)
    if not wheels_dir.exists():
        return [], []
    candidates: list[WheelCandidate] = []
    errors: list[str] = []
    for path in sorted(wheels_dir.iterdir()):
        if path.is_dir():
            continue
        if path.suffix != ".whl":
            errors.append(
                f"Unsupported dependency artifact '{path.name}'; only .whl files are allowed"
            )
            continue
        try:
            candidates.append(_parse_wheel(path))
        except InvalidWheelFilename as exc:
            errors.append(f"Invalid wheel filename '{path.name}': {exc}")
    return candidates, errors


def validate_bundled_wheels(
    *,
    bundle_root: str | Path,
    requirements: list[dict[str, Any]],
    manifest_path: str = "",
) -> list[dict[str, str]]:
    if not requirements:
        return []
    issues: list[dict[str, str]] = []
    requirements_by_name_version = {
        (str(item.get("name") or "").strip(), str(item.get("version") or "").strip()): dict(item)
        for item in requirements
    }
    hash_matched_keys: set[tuple[str, str]] = set()
    candidates, wheel_errors = bundled_wheel_candidates(bundle_root)
    for error in wheel_errors:
        issues.append(
            _issue(
                severity="error",
                code="invalid_python_requirement_wheel",
                message=error,
                file_path=str(extension_wheels_dir(bundle_root)),
                field_path=PYTHON_REQUIREMENTS_FIELD,
            )
        )
    declared_requirements_by_name = {
        str(item.get("name") or "").strip(): dict(item) for item in requirements
    }
    for candidate in candidates:
        key = (candidate.name, candidate.version)
        req = requirements_by_name_version.get(key)
        if req is None:
            issues.append(
                _issue(
                    severity="error",
                    code="undeclared_python_requirement_wheel",
                    message=(
                        f"Bundled wheel '{candidate.path.name}' is not declared in "
                        "python_requirements"
                    ),
                    file_path=str(candidate.path),
                    field_path=PYTHON_REQUIREMENTS_FIELD,
                )
            )
            continue
        if f"sha256:{candidate.sha256}" not in set(req.get("hashes") or []):
            issues.append(
                _issue(
                    severity="error",
                    code="python_requirement_wheel_hash_missing",
                    message=(
                        f"Bundled wheel '{candidate.path.name}' sha256 is not listed "
                        "in python_requirements"
                    ),
                    file_path=str(candidate.path),
                    field_path=PYTHON_REQUIREMENTS_FIELD,
                )
            )
        else:
            hash_matched_keys.add(key)
        issues.extend(
            _closure_issues_for_wheel(
                candidate,
                declared_requirements_by_name=declared_requirements_by_name,
                manifest_path=manifest_path,
            )
        )
    if extension_deps_fetch_mode() == EXTENSION_DEPS_FETCH_OFF:
        for item in requirements:
            key = (
                str(item.get("name") or "").strip(),
                str(item.get("version") or "").strip(),
            )
            if key not in hash_matched_keys:
                issues.append(
                    _issue(
                        severity="error",
                        code="missing_python_requirement_wheel",
                        message=(
                            f"Missing bundled wheel for python requirement "
                            f"'{key[0]}=={key[1]}' with a declared sha256 hash"
                        ),
                        file_path=str(extension_wheels_dir(bundle_root)),
                        field_path=PYTHON_REQUIREMENTS_FIELD,
                        hint=(
                            "Bundle wheels under wheels/ or set FS_EXTENSION_DEPS_FETCH=allowlist "
                            "for install-time allowlisted downloads."
                        ),
                    )
                )
    return issues


def _closure_issues_for_wheel(
    candidate: WheelCandidate,
    *,
    declared_requirements_by_name: dict[str, dict[str, Any]],
    manifest_path: str,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    metadata_text = _wheel_metadata_text(candidate.path)
    if not metadata_text:
        return []
    for requirement_text in _requires_dist_values(metadata_text):
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement:
            continue
        if requirement.marker is not None:
            try:
                if not requirement.marker.evaluate({"extra": ""}):
                    continue
            except Exception:
                continue
        dep_name = str(canonicalize_name(requirement.name))
        declared_requirement = declared_requirements_by_name.get(dep_name)
        if dep_name and declared_requirement is None:
            issues.append(
                _issue(
                    severity="error",
                    code="python_requirement_closure_incomplete",
                    message=(
                        f"Python requirement '{candidate.name}' declares transitive "
                        f"dependency '{dep_name}', but it is missing from python_requirements"
                    ),
                    file_path=manifest_path,
                    field_path=PYTHON_REQUIREMENTS_FIELD,
                    hint="Run tooling extensions deps lock to regenerate the full transitive closure.",
                )
            )
            continue
        if declared_requirement is None:
            continue
        declared_version = str(declared_requirement.get("version") or "").strip()
        if requirement.specifier and not requirement.specifier.contains(
            declared_version,
            prereleases=True,
        ):
            issues.append(
                _issue(
                    severity="error",
                    code="python_requirement_closure_version_mismatch",
                    message=(
                        f"Python requirement '{candidate.name}' declares transitive "
                        f"dependency '{dep_name}{requirement.specifier}', but "
                        f"python_requirements pins '{dep_name}=={declared_version}'"
                    ),
                    file_path=manifest_path,
                    field_path=PYTHON_REQUIREMENTS_FIELD,
                    hint="Run tooling extensions deps lock to regenerate compatible pins.",
                )
            )
    return issues


def _wheel_metadata_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith(".dist-info/METADATA"):
                    with archive.open(name) as handle:
                        return handle.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    return ""


def _requires_dist_values(metadata_text: str) -> list[str]:
    values: list[str] = []
    current_name = ""
    current_value = ""
    for row in metadata_text.splitlines():
        if row.startswith((" ", "\t")) and current_name.lower() == "requires-dist":
            current_value += " " + row.strip()
            continue
        if current_name.lower() == "requires-dist" and current_value:
            values.append(current_value)
        current_name = ""
        current_value = ""
        if ":" not in row:
            continue
        current_name, current_value = row.split(":", 1)
        current_value = current_value.strip()
    if current_name.lower() == "requires-dist" and current_value:
        values.append(current_value)
    return values


def select_bundled_wheels(
    *,
    bundle_root: str | Path,
    requirements: list[dict[str, Any]],
) -> list[SelectedWheel]:
    candidates, wheel_errors = bundled_wheel_candidates(bundle_root)
    if wheel_errors:
        raise ValueError("; ".join(wheel_errors))
    supported_tags = list(sys_tags())
    supported_rank = {tag: idx for idx, tag in enumerate(supported_tags)}
    selected: list[SelectedWheel] = []
    for requirement in requirements:
        req_name = str(requirement.get("name") or "").strip()
        req_version = str(requirement.get("version") or "").strip()
        allowed_hashes = set(requirement.get("hashes") or [])
        matches = [
            candidate
            for candidate in candidates
            if candidate.name == req_name
            and candidate.version == req_version
            and f"sha256:{candidate.sha256}" in allowed_hashes
        ]
        if not matches:
            mode = extension_deps_fetch_mode()
            if mode == EXTENSION_DEPS_FETCH_ALLOWLIST:
                cache_dir = Path(bundle_root).resolve() / PYTHON_DEPS_ROOT_DIRNAME / "cache"
                download_wheels_for_requirements(
                    requirements=[dict(requirement)],
                    destination=cache_dir,
                    index_url=EXTENSION_DEPS_DEFAULT_INDEX_URL,
                )
                cached_candidates, cached_errors = wheel_candidates_from_dir(cache_dir)
                if cached_errors:
                    raise ValueError("; ".join(cached_errors))
                candidates.extend(cached_candidates)
                matches = [
                    candidate
                    for candidate in candidates
                    if candidate.name == req_name
                    and candidate.version == req_version
                    and f"sha256:{candidate.sha256}" in allowed_hashes
                ]
            elif mode != EXTENSION_DEPS_FETCH_OFF:
                raise ValueError(
                    f"Unsupported {FS_EXTENSION_DEPS_FETCH_ENV} value '{mode}'; "
                    f"use {EXTENSION_DEPS_FETCH_OFF} or {EXTENSION_DEPS_FETCH_ALLOWLIST}"
                )
        if not matches:
            raise ValueError(
                f"Missing bundled wheel for python requirement '{req_name}=={req_version}' "
                "with a declared sha256 hash"
            )
        ranked: list[tuple[int, str, WheelCandidate]] = []
        available_tags: set[str] = set()
        for candidate in matches:
            available_tags.update(str(tag) for tag in candidate.tags)
            best_tag = None
            best_rank = len(supported_tags) + 1
            for tag in candidate.tags:
                rank = supported_rank.get(tag)
                if rank is not None and rank < best_rank:
                    best_rank = rank
                    best_tag = tag
            if best_tag is not None:
                ranked.append((best_rank, str(best_tag), candidate))
        if not ranked:
            supported_preview = ", ".join(str(tag) for tag in supported_tags[:8])
            available_preview = ", ".join(sorted(available_tags)[:16])
            raise ValueError(
                f"No compatible wheel for python requirement '{req_name}=={req_version}'. "
                f"Supported tags include: {supported_preview}. "
                f"Available wheel tags: {available_preview or 'none'}."
            )
        ranked.sort(key=lambda item: item[0])
        _rank, tag_text, wheel = ranked[0]
        selected.append(
            SelectedWheel(
                requirement=dict(requirement),
                wheel=wheel,
                selected_tag=tag_text,
            )
        )
    return selected


def materialize_python_requirements(
    *,
    bundle_root: str | Path,
    compiled_manifest: dict[str, Any],
) -> dict[str, Any]:
    requirements = _as_list(compiled_manifest.get(PYTHON_REQUIREMENTS_FIELD))
    if not requirements:
        return {
            "mode": "none",
            "site_dir": "",
            "dependencies": [],
        }
    version = str(compiled_manifest.get("version") or "").strip() or "0.0.0"
    selected = select_bundled_wheels(bundle_root=bundle_root, requirements=requirements)
    version_dir = extension_python_deps_version_dir(bundle_root, version=version)
    final_site = version_dir / PYTHON_DEPS_SITE_DIRNAME
    deps_root = version_dir.parent
    deps_root.mkdir(parents=True, exist_ok=True)
    stage_dir = deps_root / f".stage-{version}-{uuid.uuid4().hex[:8]}"
    backup_dir = deps_root / f".backup-{version}-{uuid.uuid4().hex[:8]}"
    stage_site = stage_dir / PYTHON_DEPS_SITE_DIRNAME
    try:
        stage_site.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "pip",
            "--isolated",
            "--no-input",
            "install",
            "--no-compile",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            "--no-deps",
            "--no-index",
            "--target",
            str(stage_site),
            *[str(item.wheel.path) for item in selected],
        ]
        proc = subprocess.run(  # nosec B603 - fixed interpreter and verified wheel paths.
            command,
            cwd=str(Path(bundle_root).resolve()),
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
            env=_pip_download_env(),
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise ValueError(f"Python dependency wheel install failed: {detail[:2000]}")
        _remove_local_wheel_direct_url_metadata(stage_site)
        site_size = _dir_size_bytes(stage_site)
        max_size = _site_size_limit_bytes()
        if site_size > max_size:
            raise ValueError(
                f"Python dependency site dir is too large ({site_size} bytes > {max_size} bytes)"
            )
        if version_dir.exists():
            shutil.move(str(version_dir), str(backup_dir))
        shutil.move(str(stage_dir), str(version_dir))
        shutil.rmtree(backup_dir, ignore_errors=True)
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        if backup_dir.exists() and not version_dir.exists():
            shutil.move(str(backup_dir), str(version_dir))
        elif backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        raise

    return {
        "mode": "bundled_wheels",
        "site_dir": str(final_site),
        "max_site_bytes": _site_size_limit_bytes(),
        "site_bytes": _dir_size_bytes(final_site),
        "dependencies": [
            {
                "name": item.requirement["name"],
                "version": item.requirement["version"],
                "hash": f"sha256:{item.wheel.sha256}",
                "wheel": item.wheel.path.name,
                "selected_tag": item.selected_tag,
            }
            for item in selected
        ],
    }


def remove_obsolete_python_dependency_dirs(
    *,
    bundle_root: str | Path,
    keep_version: str,
) -> None:
    deps_root = Path(bundle_root).resolve() / PYTHON_DEPS_ROOT_DIRNAME
    if not deps_root.exists():
        return
    normalized_keep = str(keep_version or "").strip()
    for child in deps_root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name != normalized_keep:
            shutil.rmtree(child, ignore_errors=True)


def requirement_lines(requirements: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in requirements:
        hashes = " ".join(f"--hash={digest}" for digest in item.get("hashes") or [])
        lines.append(f"{item['name']}=={item['version']} {hashes}".strip())
    return lines


def resolve_wheel_platforms(raw_value: str, *, default: tuple[str, ...] | None = None) -> list[str]:
    raw = str(raw_value or "").strip()
    default_platforms = default if default is not None else DEFAULT_WHEEL_PLATFORMS
    if not raw:
        return list(default_platforms)
    values = [item.strip() for item in raw.replace(" ", ",").split(",") if item.strip()]
    if any(item.lower() in {"active", "current", "host"} for item in values):
        return [""]
    return values


def _pip_download_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in _runtime_env.items()
        if key in {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}
    }
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    return env


def _pip_target_options(
    *,
    platform: str,
    python_version: str = "",
    implementation: str = "",
    abi_tags: list[str] | None = None,
) -> list[str]:
    if not platform:
        return []
    major = sys.version_info.major
    minor = sys.version_info.minor
    impl = implementation or ("cp" if sys.implementation.name == "cpython" else "py")
    py_version = python_version or f"{major}.{minor}"
    target_options = [
        "--platform",
        platform,
        "--implementation",
        impl,
        "--python-version",
        py_version,
    ]
    resolved_abi_tags = abi_tags or (
        [f"cp{major}{minor}", "abi3", "none"] if impl == "cp" else ["none"]
    )
    for abi_tag in resolved_abi_tags:
        target_options.extend(["--abi", abi_tag])
    return target_options


def download_wheels_for_requirements(
    *,
    requirements: list[dict[str, Any]],
    destination: str | Path,
    index_url: str = EXTENSION_DEPS_DEFAULT_INDEX_URL,
    platform: str = "",
    python_version: str = "",
    implementation: str = "",
    abi_tags: list[str] | None = None,
) -> None:
    Path(destination).mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
        requirements_path = Path(handle.name)
        handle.write("\n".join(requirement_lines(requirements)) + "\n")
    try:
        target_options = _pip_target_options(
            platform=platform,
            python_version=python_version,
            implementation=implementation,
            abi_tags=abi_tags,
        )
        command = [
            sys.executable,
            "-m",
            "pip",
            "--isolated",
            "--no-input",
            "download",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "--no-deps",
            "--require-hashes",
            "--index-url",
            index_url,
            *target_options,
            "--dest",
            str(Path(destination).resolve()),
            "-r",
            str(requirements_path),
        ]
        proc = subprocess.run(  # nosec B603 - fixed interpreter and static pip args.
            command,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
            env=_pip_download_env(),
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise ValueError(f"Wheel download failed: {detail[:2000]}")
    finally:
        requirements_path.unlink(missing_ok=True)


def lock_direct_requirements(
    *,
    input_path: str | Path,
    index_url: str = EXTENSION_DEPS_DEFAULT_INDEX_URL,
    platforms: list[str] | None = None,
) -> list[dict[str, Any]]:
    resolved_platforms = list(platforms if platforms is not None else DEFAULT_WHEEL_PLATFORMS)
    if not resolved_platforms:
        resolved_platforms = [""]
    with tempfile.TemporaryDirectory(prefix="fs-extension-deps-lock-") as temp_dir:
        dest = Path(temp_dir) / "wheels"
        for platform in resolved_platforms:
            command = [
                sys.executable,
                "-m",
                "pip",
                "--isolated",
                "--no-input",
                "download",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--index-url",
                index_url,
                *_pip_target_options(platform=str(platform or "")),
                "--dest",
                str(dest),
                "-r",
                str(Path(input_path).resolve()),
            ]
            proc = subprocess.run(  # nosec B603 - fixed interpreter and static pip args.
                command,
                text=True,
                capture_output=True,
                timeout=300,
                check=False,
                env=_pip_download_env(),
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                platform_label = str(platform or "active").strip()
                raise ValueError(
                    f"Dependency lock failed for platform '{platform_label}': {detail[:2000]}"
                )
        locked_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for wheel in sorted(dest.glob("*.whl")):
            candidate = _parse_wheel(wheel)
            key = (str(canonicalize_name(candidate.name)), candidate.version)
            row = locked_by_key.setdefault(
                key,
                {
                    "name": candidate.name,
                    "version": candidate.version,
                    "hashes": [],
                },
            )
            digest = f"sha256:{candidate.sha256}"
            if digest not in row["hashes"]:
                row["hashes"].append(digest)
        locked = list(locked_by_key.values())
        for row in locked:
            row["hashes"] = sorted(row["hashes"])
        return sorted(locked, key=lambda row: (str(row["name"]), str(row["version"])))


def read_dist_info_version(site_dir: str | Path, distribution_name: str) -> str:
    normalized_name = str(canonicalize_name(distribution_name))
    for metadata_path in Path(site_dir).glob("*.dist-info/METADATA"):
        metadata = metadata_path.read_text(encoding="utf-8", errors="replace")
        name = ""
        version = ""
        for row in metadata.splitlines():
            if row.lower().startswith("name:"):
                name = str(canonicalize_name(row.split(":", 1)[1].strip()))
            elif row.lower().startswith("version:"):
                version = row.split(":", 1)[1].strip()
        if name == normalized_name:
            return version
    return ""
