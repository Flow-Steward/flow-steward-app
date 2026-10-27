"""An upgraded extension must be re-enableable.

Remediation epic 2026-07-29 — `AUDIT-075`.

Install 1.0.0, update to 1.0.1, disable, then enable, and enable fails with:

    Extension 'audit.lifecycle-fixture' must live in folder
    'audit_lifecycle_fixture', not '1.0.1-<digest>'

A file-installed extension's canonical path is a **symlink** into an immutable
release directory. `compile_extension_bundle` does `Path(bundle_dir).resolve()`,
so the folder check sees the release leaf rather than the logical folder. Install
happened to work only because `extension_state_service` passes
`logical_bundle_dir_name` explicitly; the enable path compiles the canonical path
and passed nothing.

The logical name is now derived at the resolve site, so every caller that hands
over a canonical path gets the right answer, while an explicit name from the
caller still wins.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.extensions.command_lifecycle_activation import (
    validate_extension_activation,
)
from core.application.extensions.extension_state_service import ExtensionStateService
from core.application.management.errors import ServiceValidationError
from core.infrastructure.extension_runtime.extension_runtime_compile import (
    _logical_bundle_dir_name,
    compile_extension_bundle,
)

EXTENSION_ID = "audit.lifecycle-fixture"
FOLDER = "audit_lifecycle_fixture"
RELEASE_LEAF = "1.0.1-8f2c1d9e"


def _write_bundle(root: Path, version: str = "1.0.1") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "extension.yaml").write_text(
        "\n".join(
            [
                "manifest_version: 2",
                f"extension_id: {EXTENSION_ID}",
                "kind: notification_provider",
                f"version: {version}",
                "display_name: Audit Lifecycle Fixture",
                "description: Fixture reproducing the AUDIT-075 enable failure.",
                "entrypoint: main:register",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root


class TestTheLogicalNameIsDerived:
    def test_a_symlinked_canonical_path_yields_the_logical_folder(self, tmp_path):
        release = _write_bundle(tmp_path / "releases" / RELEASE_LEAF)
        canonical = tmp_path / FOLDER
        canonical.symlink_to(release, target_is_directory=True)

        assert _logical_bundle_dir_name(canonical) == FOLDER

    def test_a_plain_directory_is_left_alone(self, tmp_path):
        plain = _write_bundle(tmp_path / FOLDER)

        assert _logical_bundle_dir_name(plain) == ""

    def test_an_explicit_name_always_wins(self, tmp_path):
        release = _write_bundle(tmp_path / "releases" / RELEASE_LEAF)
        canonical = tmp_path / FOLDER
        canonical.symlink_to(release, target_is_directory=True)

        assert _logical_bundle_dir_name(canonical, "something_else") == "something_else"

    def test_a_missing_path_does_not_raise(self, tmp_path):
        assert _logical_bundle_dir_name(tmp_path / "absent") == ""


class TestEnablingAnUpgradedRelease:
    @staticmethod
    def _folder_mismatch(compiled) -> list[str]:
        issues = list(compiled.get("validation_errors") or []) + list(
            compiled.get("compile_errors") or []
        )
        return [
            str(issue.get("message") or "")
            for issue in issues
            if str(issue.get("code") or "") == "extension_folder_mismatch"
        ]

    def test_the_canonical_symlink_compiles_without_a_folder_mismatch(self, tmp_path):
        """The exact enable-path shape that AUDIT-075 reported."""
        release = _write_bundle(tmp_path / "releases" / RELEASE_LEAF)
        canonical = tmp_path / FOLDER
        canonical.symlink_to(release, target_is_directory=True)

        compiled = compile_extension_bundle(canonical)

        assert self._folder_mismatch(compiled) == []

    def test_the_release_directory_itself_still_reports_the_mismatch(self, tmp_path):
        """Compiling the immutable release leaf directly is a genuine mismatch and
        must keep failing — the fix is about the canonical path, not about
        accepting any folder name."""
        release = _write_bundle(tmp_path / "releases" / RELEASE_LEAF)

        compiled = compile_extension_bundle(release)

        assert self._folder_mismatch(compiled), (
            "a bundle whose own folder is the release leaf is still misplaced"
        )

    def test_a_genuinely_wrong_folder_still_reports_the_mismatch(self, tmp_path):
        wrong = _write_bundle(tmp_path / "not_the_right_folder")

        compiled = compile_extension_bundle(wrong)

        assert self._folder_mismatch(compiled)

    @pytest.mark.parametrize("version", ["1.0.0", "1.0.1"])
    def test_it_holds_across_the_upgrade(self, tmp_path, version):
        """Install 1.0.0 then 1.0.1: both releases enable through the same symlink."""
        leaf = f"{version}-8f2c1d9e"
        release = _write_bundle(tmp_path / "releases" / leaf, version=version)
        canonical = tmp_path / FOLDER
        canonical.symlink_to(release, target_is_directory=True)

        compiled = compile_extension_bundle(canonical)

        assert self._folder_mismatch(compiled) == []
        assert str(compiled.get("version") or "") == version


class TestTheCacheDoesNotConflateThem:
    def test_the_two_paths_to_one_release_are_cached_separately(self, tmp_path):
        """The canonical path and the release dir validate against different names,
        so sharing a cache entry would make the result depend on call order."""
        release = _write_bundle(tmp_path / "releases" / RELEASE_LEAF)
        canonical = tmp_path / FOLDER
        canonical.symlink_to(release, target_is_directory=True)

        via_release = compile_extension_bundle(release)
        via_canonical = compile_extension_bundle(canonical)

        assert TestEnablingAnUpgradedRelease._folder_mismatch(via_release)
        assert TestEnablingAnUpgradedRelease._folder_mismatch(via_canonical) == []

    def test_order_does_not_change_the_answer(self, tmp_path):
        release = _write_bundle(tmp_path / "releases" / RELEASE_LEAF)
        canonical = tmp_path / FOLDER
        canonical.symlink_to(release, target_is_directory=True)

        via_canonical = compile_extension_bundle(canonical)
        via_release = compile_extension_bundle(release)

        assert TestEnablingAnUpgradedRelease._folder_mismatch(via_canonical) == []
        assert TestEnablingAnUpgradedRelease._folder_mismatch(via_release)


def test_enable_fails_closed_when_the_managed_release_target_is_missing(tmp_path):
    release = _write_bundle(tmp_path / ".releases" / FOLDER / RELEASE_LEAF)
    canonical = tmp_path / FOLDER
    canonical.symlink_to(release, target_is_directory=True)
    release.rename(release.with_name(f".{release.name}.missing"))

    extension_row = {
        "enabled": False,
        "compiled_manifest": {
            "bundle_root": str(release),
            "extension_id": EXTENSION_ID,
            "release_stage": "active",
        },
    }
    logger = MagicMock()
    logger.get_extension.return_value = extension_row
    managed_release = MagicMock()
    managed_release.managed_simple_extension_root.return_value = canonical
    service = ExtensionStateService(
        context=MagicMock(),
        projection=MagicMock(),
        cleanup=MagicMock(),
        managed_release=managed_release,
        get_logger=lambda: logger,
    )
    service.scan_extensions = MagicMock(return_value=MagicMock())

    _raises_input_197_1 = MagicMock()
    _raises_input_197_2 = MagicMock()
    with pytest.raises(ServiceValidationError, match="release artifact is not available"):
        service.set_enabled_state_locked(
            EXTENSION_ID,
            bl=logger,
            version="1.0.1",
            enabled=True,
            config={},
            _install_store=_raises_input_197_1,
            _runtime_store=_raises_input_197_2,
        )

    logger.sync_enabled_extension_record.assert_not_called()


def test_install_prepublication_transition_does_not_require_the_active_symlink(
    tmp_path, monkeypatch
):
    candidate = _write_bundle(tmp_path / ".releases" / FOLDER / RELEASE_LEAF)
    logger = MagicMock()
    logger.get_extension.return_value = {
        "enabled": False,
        "compiled_manifest": {
            "bundle_root": str(candidate),
            "extension_id": EXTENSION_ID,
            "release_stage": "active",
        },
    }
    logger.sync_enabled_extension_record.return_value = (EXTENSION_ID, [])
    managed_release = MagicMock()
    managed_release.managed_simple_extension_root.return_value = tmp_path / FOLDER
    context = MagicMock()
    context.command_service.return_value.validate_release_dependencies_for_install.return_value = (
        None
    )
    service = ExtensionStateService(
        context=context,
        projection=MagicMock(),
        cleanup=MagicMock(),
        managed_release=managed_release,
        get_logger=lambda: logger,
    )
    validate = MagicMock(side_effect=ServiceValidationError("active symlink is not published yet"))
    monkeypatch.setattr(
        "core.application.extensions.command_lifecycle_activation.validate_managed_release_artifact",
        validate,
    )

    service.set_enabled_state_locked(
        EXTENSION_ID,
        bl=logger,
        version="1.0.1",
        enabled=True,
        config={},
        _skip_rescan=True,
        _install_store=MagicMock(),
        _runtime_store=MagicMock(),
    )

    validate.assert_not_called()
    logger.sync_enabled_extension_record.assert_called_once()


def test_lifecycle_activation_fails_closed_when_managed_release_is_missing(tmp_path, monkeypatch):
    release = _write_bundle(tmp_path / ".releases" / FOLDER / RELEASE_LEAF)
    canonical = tmp_path / FOLDER
    canonical.symlink_to(release, target_is_directory=True)
    release.rename(release.with_name(f".{release.name}.missing"))
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
        lambda: tmp_path,
    )

    _raises_input_269_1 = SimpleNamespace(
        extension_id=EXTENSION_ID, runtime_mode="subprocess", remote_base_url="", config={}
    )
    _raises_input_269_2 = SimpleNamespace(
        descriptor={"bundle_root": str(release), "extension_id": EXTENSION_ID}
    )
    with pytest.raises(ServiceValidationError, match="release artifact is not available"):
        validate_extension_activation(_raises_input_269_1, _raises_input_269_2)
