from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
import yaml

from tests.support.extension_icon import write_extension_icon


def _bundle(
    extension_id: str,
    *,
    root: Path,
    output_dir: Path,
    runtime_target: str = "",
    version: str = "",
    with_icon: bool = True,
):
    from core.application.extensions.identity import extension_folder_name
    from core.application.tooling.extensions.bundle_extension import (
        ExtensionBundleRequest,
        bundle_extension,
    )

    bundle_dir = root / extension_folder_name(extension_id)
    if with_icon and bundle_dir.is_dir() and not (bundle_dir / "icon.png").exists():
        write_extension_icon(bundle_dir)
    return bundle_extension(
        ExtensionBundleRequest(
            extension_id=extension_id,
            root=root,
            out_dir=output_dir,
            runtime_target=runtime_target,
            version=version,
        )
    )


def _elf64(machine: int) -> bytes:
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    header[16:18] = (2).to_bytes(2, "little")
    header[18:20] = machine.to_bytes(2, "little")
    header[20:24] = (1).to_bytes(4, "little")
    return bytes(header)


def test_bundle_version_override_rewrites_the_archived_manifest_without_mutating_source(
    tmp_path: Path,
) -> None:
    from core.application.extensions.identity import extension_folder_name

    extension_id = "acme.versioned"
    bundle_dir = tmp_path / "extensions" / extension_folder_name(extension_id)
    bundle_dir.mkdir(parents=True)
    source_manifest = (
        "manifest_version: 2\n"
        "extension_id: acme.versioned\n"
        "version: 1.0.0\n"
        "display_name: Versioned\n"
    )
    source_descriptor = (
        "schema_version: extension_descriptor_v1\n"
        "extension_id: acme.versioned\n"
        "release_version: 1.0.0\n"
    )
    (bundle_dir / "extension.yaml").write_text(source_manifest, encoding="utf-8")
    (bundle_dir / "extension-descriptor.yaml").write_text(source_descriptor, encoding="utf-8")
    (bundle_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")

    baseline = _bundle(
        extension_id,
        root=tmp_path / "extensions",
        output_dir=tmp_path / "baseline",
    )
    upgraded = _bundle(
        extension_id,
        root=tmp_path / "extensions",
        output_dir=tmp_path / "upgraded",
        version="1.0.1",
    )

    baseline_path = tmp_path / "baseline" / "acme.versioned-1.0.0.zip"
    upgraded_path = tmp_path / "upgraded" / "acme.versioned-1.0.1.zip"
    with zipfile.ZipFile(upgraded_path) as archive:
        archived_manifest = yaml.safe_load(
            archive.read(f"{extension_folder_name(extension_id)}/extension.yaml")
        )
        archived_descriptor = yaml.safe_load(
            archive.read(f"{extension_folder_name(extension_id)}/extension-descriptor.yaml")
        )

    assert baseline.exit_code == upgraded.exit_code == 0
    assert archived_manifest["version"] == "1.0.1"
    assert archived_descriptor["release_version"] == "1.0.1"
    assert (bundle_dir / "extension.yaml").read_text(encoding="utf-8") == source_manifest
    assert (bundle_dir / "extension-descriptor.yaml").read_text(
        encoding="utf-8"
    ) == source_descriptor
    assert (
        hashlib.sha256(baseline_path.read_bytes()).hexdigest()
        != hashlib.sha256(upgraded_path.read_bytes()).hexdigest()
    )
    assert hashlib.sha256(upgraded_path.read_bytes()).hexdigest() in upgraded.output


def test_bundle_excludes_authoring_dependencies_and_cache_directories(
    tmp_path: Path,
) -> None:
    from core.application.extensions.identity import extension_folder_name

    extension_id = "acme.clean-archive"
    bundle_name = extension_folder_name(extension_id)
    bundle_dir = tmp_path / "extensions" / bundle_name
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "extension.yaml").write_text(
        "manifest_version: 2\nextension_id: acme.clean-archive\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (bundle_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
    included_wheel = bundle_dir / "wheels" / "runtime_dependency-1.0-py3-none-any.whl"
    included_wheel.parent.mkdir()
    included_wheel.write_bytes(b"runtime wheel")
    excluded_files = (
        bundle_dir / "dev-wheels" / "pytest-1.0-py3-none-any.whl",
        bundle_dir / ".pytest_cache" / "state",
        bundle_dir / ".mypy_cache" / "state",
        bundle_dir / ".ruff_cache" / "state",
        bundle_dir / ".hypothesis" / "examples" / "state",
        bundle_dir / "package" / "__pycache__" / "main.pyc",
    )
    for path in excluded_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"authoring only")

    result = _bundle(
        extension_id,
        root=tmp_path / "extensions",
        output_dir=tmp_path / "dist",
    )

    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(tmp_path / "dist" / "acme.clean-archive-1.0.0.zip") as archive:
        names = set(archive.namelist())
    assert f"{bundle_name}/main.py" in names
    assert f"{bundle_name}/wheels/{included_wheel.name}" in names
    assert all(
        f"{bundle_name}/{path.relative_to(bundle_dir)}" not in names for path in excluded_files
    )


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", "linux-amd64"),
        ("AMD64", "linux-amd64"),
        ("aarch64", "linux-arm64"),
        ("ARM64", "linux-arm64"),
    ],
)
def test_normalize_extension_runtime_target_uses_canonical_linux_values(
    machine: str, expected: str
) -> None:
    from core.infrastructure.extension_runtime.runtime_target import (
        normalize_extension_runtime_target,
    )

    assert normalize_extension_runtime_target(system="Linux", machine=machine) == expected


@pytest.mark.parametrize(
    ("system", "machine", "message"),
    [
        ("Darwin", "arm64", "operating system"),
        ("Linux", "riscv64", "architecture"),
    ],
)
def test_normalize_extension_runtime_target_rejects_unsupported_hosts(
    system: str, machine: str, message: str
) -> None:
    from core.infrastructure.extension_runtime.runtime_target import (
        normalize_extension_runtime_target,
    )

    with pytest.raises(ValueError, match=message):
        normalize_extension_runtime_target(system=system, machine=machine)


@pytest.mark.parametrize("output_flag", ["--output-dir", "--out-dir"])
def test_bundle_runtime_target_supports_output_flags_and_filters_binaries(
    tmp_path: Path, output_flag: str
) -> None:
    from core.application.extensions.identity import extension_folder_name

    extension_id = "acme.runtime-demo"
    bundle_dir = tmp_path / "extensions" / extension_folder_name(extension_id)
    (bundle_dir / "bin" / "linux-amd64").mkdir(parents=True)
    (bundle_dir / "bin" / "linux-arm64").mkdir(parents=True)
    (bundle_dir / "bin" / "darwin-arm64").mkdir(parents=True)
    (bundle_dir / "extension.yaml").write_text(
        "manifest_version: 2\nextension_id: acme.runtime-demo\nversion: 1.2.3\n",
        encoding="utf-8",
    )
    (bundle_dir / "bin" / "linux-amd64" / "runtime").write_bytes(_elf64(62))
    arm64_binary = _elf64(183)
    arm64_path = bundle_dir / "bin" / "linux-arm64" / "runtime"
    arm64_path.write_bytes(arm64_binary)
    arm64_path.chmod(0o755)
    (bundle_dir / "bin" / "darwin-arm64" / "runtime").write_bytes(b"darwin")
    (bundle_dir / "bin" / "shared-helper").write_bytes(b"shared")
    (bundle_dir / ".fs-package.yaml").write_text("runtime_target: linux-amd64\n", encoding="utf-8")
    (bundle_dir / "README.md").write_text("runtime demo\n", encoding="utf-8")
    output_dir = tmp_path / output_flag.removeprefix("--")

    result = _bundle(
        extension_id,
        root=tmp_path / "extensions",
        runtime_target="linux-arm64",
        output_dir=output_dir,
    )

    assert result.exit_code == 0
    archive_path = output_dir / "acme.runtime-demo-1.2.3-linux-arm64.zip"
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        top_folder = extension_folder_name(extension_id)
        assert f"{top_folder}/bin/linux-arm64/runtime" in names
        assert f"{top_folder}/bin/linux-amd64/runtime" not in names
        assert f"{top_folder}/bin/darwin-arm64/runtime" not in names
        assert f"{top_folder}/bin/shared-helper" not in names
        package_metadata = yaml.safe_load(
            archive.read(f"{top_folder}/.fs-package.yaml").decode("utf-8")
        )
        assert archive.namelist().count(f"{top_folder}/.fs-package.yaml") == 1
    assert package_metadata == {
        "native_executable": {
            "path": "bin/linux-arm64/runtime",
            "sha256": hashlib.sha256(arm64_binary).hexdigest(),
        },
        "runtime_target": "linux-arm64",
    }


def test_bundle_native_payload_requires_runtime_target(
    tmp_path: Path,
) -> None:
    from core.application.extensions.identity import extension_folder_name

    extension_id = "acme.runtime-demo"
    bundle_dir = tmp_path / "extensions" / extension_folder_name(extension_id)
    target_dir = bundle_dir / "bin" / "linux-amd64"
    target_dir.mkdir(parents=True)
    (bundle_dir / "extension.yaml").write_text(
        "manifest_version: 2\nextension_id: acme.runtime-demo\nversion: 1.2.3\n",
        encoding="utf-8",
    )
    executable = target_dir / "runtime"
    executable.write_bytes(_elf64(62))
    executable.chmod(0o755)

    result = _bundle(
        extension_id,
        root=tmp_path / "extensions",
        output_dir=tmp_path / "dist",
    )

    assert result.exit_code == 2
    assert "require --runtime-target" in result.output
    output_dir = tmp_path / "dist"
    assert not output_dir.exists() or not list(output_dir.glob("*.zip"))


@pytest.mark.parametrize("executable_count", [2])
def test_bundle_runtime_target_requires_exactly_one_target_executable(
    tmp_path: Path,
    executable_count: int,
) -> None:
    from core.application.extensions.identity import extension_folder_name

    extension_id = "acme.runtime-demo"
    bundle_dir = tmp_path / "extensions" / extension_folder_name(extension_id)
    target_dir = bundle_dir / "bin" / "linux-amd64"
    target_dir.mkdir(parents=True)
    (bundle_dir / "extension.yaml").write_text(
        "manifest_version: 2\nextension_id: acme.runtime-demo\nversion: 1.2.3\n",
        encoding="utf-8",
    )
    for index in range(executable_count):
        executable = target_dir / f"runtime-{index}"
        executable.write_bytes(_elf64(62))
        executable.chmod(0o755)

    result = _bundle(
        extension_id,
        root=tmp_path / "extensions",
        runtime_target="linux-amd64",
        output_dir=tmp_path / "dist",
    )

    assert result.exit_code == 2
    assert "exactly one native executable" in result.output


# ---------------------------------------------------------------------------
# A target package without a native executable (per-architecture Python wheels)
# ---------------------------------------------------------------------------

_AMD64_WHEEL = "numpy-2.4.2-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
_ARM64_WHEEL = "numpy-2.4.2-cp312-cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl"
_PURE_WHEEL = "packaging-26.3-py3-none-any.whl"
_MAC_WHEEL = "pyyaml-6.0.3-cp312-cp312-macosx_11_0_arm64.whl"


@pytest.mark.parametrize(
    ("wheel", "target", "expected"),
    [
        (_AMD64_WHEEL, "linux-amd64", True),
        (_AMD64_WHEEL, "linux-arm64", False),
        (_ARM64_WHEEL, "linux-arm64", True),
        (_PURE_WHEEL, "linux-arm64", True),
        (_MAC_WHEEL, "linux-arm64", False),
        ("protobuf-7.36.1-cp310-abi3-manylinux2014_aarch64.whl", "linux-arm64", True),
        ("README.md", "linux-amd64", True),
    ],
)
def test_a_wheel_ships_only_to_the_targets_it_installs_on(
    wheel: str, target: str, expected: bool
) -> None:
    from core.infrastructure.extension_runtime.native_package import wheel_supports_runtime_target

    assert wheel_supports_runtime_target(wheel, target) is expected


def test_a_target_package_without_a_native_executable_carries_only_its_wheels(
    tmp_path: Path,
) -> None:
    from core.application.extensions.identity import extension_folder_name

    extension_id = "acme.images"
    bundle_dir = tmp_path / "extensions" / extension_folder_name(extension_id)
    (bundle_dir / "wheels").mkdir(parents=True)
    (bundle_dir / "extension.yaml").write_text(
        "manifest_version: 2\nextension_id: acme.images\nversion: 1.0.0\n", encoding="utf-8"
    )
    for wheel in (_AMD64_WHEEL, _ARM64_WHEEL, _PURE_WHEEL, _MAC_WHEEL):
        (bundle_dir / "wheels" / wheel).write_bytes(b"wheel")

    result = _bundle(
        extension_id,
        root=tmp_path / "extensions",
        runtime_target="linux-arm64",
        output_dir=tmp_path / "dist",
    )

    assert result.exit_code == 0, result.output
    top = extension_folder_name(extension_id)
    with zipfile.ZipFile(tmp_path / "dist" / "acme.images-1.0.0-linux-arm64.zip") as archive:
        wheels = sorted(
            name.rsplit("/", 1)[-1] for name in archive.namelist() if "/wheels/" in name
        )
        metadata = yaml.safe_load(archive.read(f"{top}/.fs-package.yaml").decode("utf-8"))
    assert wheels == sorted([_ARM64_WHEEL, _PURE_WHEEL])
    assert metadata == {"runtime_target": "linux-arm64"}


def test_the_installer_accepts_a_target_package_without_a_native_executable(
    tmp_path: Path,
) -> None:
    from core.infrastructure.extension_runtime.native_package import (
        validate_native_target_package,
    )

    (tmp_path / "wheels").mkdir()
    (tmp_path / "wheels" / _ARM64_WHEEL).write_bytes(b"wheel")

    validate_native_target_package(
        tmp_path, package_metadata={"runtime_target": "linux-arm64"}, runtime_target="linux-arm64"
    )


def test_a_target_package_cannot_hide_a_native_executable_by_declaring_none(
    tmp_path: Path,
) -> None:
    from core.infrastructure.extension_runtime.native_package import (
        validate_native_target_package,
    )

    (tmp_path / "bin" / "linux-arm64").mkdir(parents=True)
    (tmp_path / "bin" / "linux-arm64" / "runtime").write_bytes(_elf64(183))

    with pytest.raises(ValueError, match="declares none"):
        validate_native_target_package(
            tmp_path,
            package_metadata={"runtime_target": "linux-arm64"},
            runtime_target="linux-arm64",
        )


def test_bundle_refuses_a_bundle_without_its_icon(tmp_path: Path) -> None:
    from core.application.extensions.identity import extension_folder_name

    extension_id = "acme.no-logo"
    bundle_dir = tmp_path / "extensions" / extension_folder_name(extension_id)
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "extension.yaml").write_text(
        "manifest_version: 2\nextension_id: acme.no-logo\nversion: 1.0.0\n", encoding="utf-8"
    )

    result = _bundle(
        extension_id,
        root=tmp_path / "extensions",
        output_dir=tmp_path / "dist",
        with_icon=False,
    )

    assert result.exit_code == 2
    assert "[FAIL] icon.png: icon.png is missing" in result.output
    assert not (tmp_path / "dist").exists() or not list((tmp_path / "dist").glob("*.zip"))
