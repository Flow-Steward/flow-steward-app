from __future__ import annotations

import ast
import importlib
import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).parents[2]
_EXTENSIONS = _ROOT / "core" / "application" / "extensions"


def _component(module_name: str, class_name: str):
    module = importlib.import_module(f"core.application.extensions.{module_name}")
    return getattr(module, class_name)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return payload.getvalue()


def test_archive_pipeline_has_focused_typed_components() -> None:
    expected = {
        "archive_validation_service.py": {
            "ExtensionArchiveValidationService",
            "ValidatedArchiveUpload",
            "ValidatedExtensionBundle",
        },
        "archive_extraction_service.py": {"ExtensionArchiveExtractionService"},
    }

    for module_name, class_names in expected.items():
        path = _EXTENSIONS / module_name
        assert path.exists(), module_name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined_classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
        assert class_names <= defined_classes, module_name


def test_archive_validator_rejects_unsafe_name_and_size_without_io() -> None:
    validator_type = _component("archive_validation_service", "ExtensionArchiveValidationService")
    validator = validator_type(context=MagicMock(), projection=MagicMock())

    with (
        patch.object(Path, "write_bytes", side_effect=AssertionError("must not write")),
        pytest.raises(Exception, match=r"plain '\.zip' name"),
    ):
        validator.validate_upload(
            filename="../escape.zip",
            content=b"zip",
            max_archive_bytes=16,
        )

    with (
        patch.object(Path, "write_bytes", side_effect=AssertionError("must not write")),
        pytest.raises(Exception, match="maximum 2 bytes"),
    ):
        validator.validate_upload(
            filename="large.zip",
            content=b"zip",
            max_archive_bytes=2,
        )


def test_archive_extractor_runs_guard_before_temp_write() -> None:
    validator_type = _component("archive_validation_service", "ExtensionArchiveValidationService")
    extractor_type = _component("archive_extraction_service", "ExtensionArchiveExtractionService")
    validator = validator_type(context=MagicMock(), projection=MagicMock())
    extractor = extractor_type(file_mode=lambda _member: 0o644)
    upload = validator.validate_upload(
        filename="bomb.zip",
        content=_zip_bytes({"payload.bin": b"\x00" * (5 * 1024 * 1024)}),
        max_archive_bytes=1024 * 1024,
    )

    with (
        patch.object(Path, "write_bytes", side_effect=AssertionError("must not write")),
        pytest.raises(Exception, match="Rejected archive"),
        extractor.extract(
            upload=upload,
            max_uncompressed_bytes=1024 * 1024,
        ),
    ):
        pass


def test_archive_extractor_rejects_insufficient_working_disk_before_write() -> None:
    validator_type = _component("archive_validation_service", "ExtensionArchiveValidationService")
    extractor_module = importlib.import_module(
        "core.application.extensions.archive_extraction_service"
    )
    validator = validator_type(context=MagicMock(), projection=MagicMock())
    extractor = extractor_module.ExtensionArchiveExtractionService(file_mode=lambda _member: 0o644)
    upload = validator.validate_upload(
        filename="extension.zip",
        content=_zip_bytes({"extension.yaml": b"id: example\n"}),
        max_archive_bytes=1024 * 1024,
    )

    with (
        patch.object(
            extractor_module.shutil,
            "disk_usage",
            return_value=MagicMock(free=1),
        ),
        patch.object(Path, "write_bytes", side_effect=AssertionError("must not write")),
        pytest.raises(Exception, match="temporary disk space"),
        extractor.extract(upload=upload, max_uncompressed_bytes=1024 * 1024),
    ):
        pass


def test_archive_workflow_methods_are_reviewable() -> None:
    limits = {
        ("archive_install_service.py", "install_extension_archive"): 80,
        ("archive_new_install_service.py", "_install_simple_extension_bundle"): 80,
    }

    for (module_name, method_name), limit in limits.items():
        tree = ast.parse((_EXTENSIONS / module_name).read_text(encoding="utf-8"))
        method = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == method_name
        )
        assert method.end_lineno is not None
        assert method.end_lineno - method.lineno + 1 <= limit, (
            module_name,
            method_name,
            method.end_lineno - method.lineno + 1,
        )


class TestBundleDependencyRefusals:
    """What a bundle is missing is the uploader's problem, not a server fault."""

    @staticmethod
    def _requirement(name: str = "numpy", version: str = "2.4.2") -> dict:
        return {
            "version": "1.0.0",
            "python_requirements": [
                {"name": name, "version": version, "hashes": ["sha256:" + "0" * 64]}
            ],
        }

    def test_a_missing_wheel_is_a_bad_request_that_names_the_requirement(self, tmp_path):
        from core.application.extensions.archive_validation_service import (
            materialized_python_requirements,
        )
        from core.application.management.errors import ServiceValidationError

        # Built outside the block so the only call that can raise inside it is
        # the one under test.
        compiled = self._requirement()

        with pytest.raises(ServiceValidationError) as raised:
            materialized_python_requirements(bundle_root=tmp_path, compiled_manifest=compiled)

        assert raised.value.status_code == 400
        # The reason reaches the caller instead of a correlation id and a log line.
        assert "numpy==2.4.2" in str(raised.value)

    def test_an_incompatible_wheel_reports_the_tags_it_needed_and_found(self, tmp_path):
        from core.application.extensions.archive_validation_service import (
            materialized_python_requirements,
        )
        from core.application.management.errors import ServiceValidationError

        compiled = self._requirement()

        with (
            patch(
                "core.infrastructure.extension_runtime.python_dependencies.select_bundled_wheels",
                side_effect=ValueError(
                    "No compatible wheel for python requirement 'numpy==2.4.2'. "
                    "Supported tags include: cp312-cp312-manylinux_2_41_aarch64. "
                    "Available wheel tags: cp312-cp312-manylinux_2_28_x86_64."
                ),
            ),
            pytest.raises(ServiceValidationError) as raised,
        ):
            materialized_python_requirements(bundle_root=tmp_path, compiled_manifest=compiled)

        message = str(raised.value)
        assert "Supported tags include" in message
        assert "Available wheel tags" in message

    def test_a_bundle_with_no_python_requirements_is_untouched(self, tmp_path):
        from core.application.extensions.archive_validation_service import (
            materialized_python_requirements,
        )

        assert materialized_python_requirements(
            bundle_root=tmp_path,
            compiled_manifest={"version": "1.0.0"},
        ) == {"mode": "none", "site_dir": "", "dependencies": []}

    def test_the_install_path_uses_the_reporting_wrapper(self):
        """A direct call to the resolver would go back to being a 500."""
        for module_name in ("archive_new_install_service", "archive_update_service"):
            source = (_EXTENSIONS / f"{module_name}.py").read_text(encoding="utf-8")
            assert "materialized_python_requirements" in source, module_name
            assert "import (\n            materialize_python_requirements," not in source
