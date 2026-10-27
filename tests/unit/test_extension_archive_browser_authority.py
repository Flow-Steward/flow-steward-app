from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.application.extensions.archive_install_service import ExtensionArchiveInstallService
from core.application.extensions.archive_validation_service import ValidatedExtensionBundle
from core.application.management.errors import ServicePreconditionError
from core.application.management.scope import ScopeContext
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID


def _bundle(tmp_path: Path) -> ValidatedExtensionBundle:
    root = tmp_path / "candidate"
    (root / "assets").mkdir(parents=True)
    (root / "assets/picker.js").write_text("window.Acme = {};", encoding="utf-8")
    compiled = {
        "bundle_root": str(root),
        "extension_key": "acme.browser-picker",
        "version": "2.0.0",
        "action_definitions": [
            {
                "action_id": "picker_bootstrap",
                "handler": {"mode": "extension"},
                "mutates_platform": False,
                "parameters": [],
                "workflow_visible": False,
            }
        ],
        "ui_manifest": {
            "pages": [
                {
                    "components": [
                        {
                            "type": "external_resource_picker",
                            "data": {
                                "allowed_script_origins": ["https://sdk.example.test"],
                                "bootstrap_action_id": "picker_bootstrap",
                                "bridge_global": "Acme.Picker",
                                "button_label": "Choose",
                                "scripts": [
                                    {"src": "https://sdk.example.test/picker.js"},
                                    {"asset_ref": "assets/picker.js"},
                                ],
                                "target_component_id": "form",
                                "selection": {"maximum": 1, "fields": []},
                            },
                        }
                    ]
                }
            ]
        },
    }
    return ValidatedExtensionBundle(
        bundle_dir=root,
        descriptor_path=None,
        compiled=compiled,
        extension_id="acme.browser-picker",
        version="2.0.0",
    )


def _service(current_install=None):
    context = MagicMock()
    context.update_lock.return_value = contextlib.nullcontext()
    install_store = MagicMock()
    runtime_store = MagicMock()
    context.install_store.return_value = install_store
    context.runtime_store.return_value = runtime_store
    state = MagicMock()
    state.current_extension_install_record.return_value = current_install
    managed_release = MagicMock()
    managed_release.managed_simple_extension_root.return_value = Path(
        "/__flow_steward_browser_authority_test_missing__"
    )
    return ExtensionArchiveInstallService(
        context=context,
        projection=MagicMock(),
        managed_release=managed_release,
        state=state,
        new_install=MagicMock(),
        update=MagicMock(),
        get_logger=lambda: MagicMock(),
    )


def _scope() -> ScopeContext:
    return ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="admin-a",
        principal_type="admin",
    )


def test_archive_install_gates_before_publication_and_reuses_exact_approval(tmp_path: Path):
    service = _service()
    bundle = _bundle(tmp_path)

    _raises_input_103_1 = MagicMock()
    _raises_input_103_2 = _scope()
    _raises_input_103_3 = _scope()
    with pytest.raises(ServicePreconditionError) as preview:
        service._install_validated_bundle(
            bl=_raises_input_103_1,
            bundle=bundle,
            extensions_root=tmp_path / "extensions",
            metadata_filename=".flow-steward-install.yaml",
            resolved_scope=_raises_input_103_2,
            scope=_raises_input_103_3,
        )
    service._new_install.install_simple_extension_bundle.assert_not_called()

    expected = SimpleNamespace(extension={"extension_id": bundle.extension_id})
    service._new_install.install_simple_extension_bundle.return_value = expected
    result = service._install_validated_bundle(
        bl=MagicMock(),
        bundle=bundle,
        extensions_root=tmp_path / "extensions",
        metadata_filename=".flow-steward-install.yaml",
        resolved_scope=_scope(),
        scope=_scope(),
        approved_browser_authority_impact_hash=preview.value.payload["impact_hash"],
    )

    assert result is expected
    kwargs = service._new_install.install_simple_extension_bundle.call_args.kwargs
    assert kwargs["approved_browser_authority_impact_hash"] == preview.value.payload["impact_hash"]
    assert (
        kwargs["browser_authority_handoff"].approval["approved_impact_hash"]
        == (preview.value.payload["impact_hash"])
    )


def test_archive_update_rejection_leaves_old_release_active(tmp_path: Path):
    current = SimpleNamespace(
        install_id="pi_current",
        release_id="pr_old",
        version="1.0.0",
        state="active",
    )
    service = _service(current)

    _raises_input_144_1 = MagicMock()
    _raises_input_144_2 = _bundle(tmp_path)
    _raises_input_144_3 = _scope()
    _raises_input_144_4 = _scope()
    with pytest.raises(ServicePreconditionError):
        service._install_validated_bundle(
            bl=_raises_input_144_1,
            bundle=_raises_input_144_2,
            extensions_root=tmp_path / "extensions",
            metadata_filename=".flow-steward-install.yaml",
            resolved_scope=_raises_input_144_3,
            scope=_raises_input_144_4,
        )

    assert current.state == "active"
    service._update.update_simple_extension_bundle.assert_not_called()
