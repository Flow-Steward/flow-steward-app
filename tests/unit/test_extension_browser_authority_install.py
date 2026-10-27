from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from core.application.extensions.command_context import ExtensionCommandContext
from core.application.extensions.command_install_release import ExtensionReleaseInstallWorkflow
from core.application.extensions.extension_runtime import ExtensionReleaseRecord
from core.application.extensions.install import ExtensionInstallStore
from core.application.management.command_models import ExtensionReleaseInstallInput
from core.application.management.command_specs.extensions import ExtensionToggleCommandInput
from core.application.management.errors import (
    ServiceNotFoundError,
    ServicePreconditionError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from tests.support.source_index import parse_file


def _release(tmp_path: Path) -> ExtensionReleaseRecord:
    bundle = tmp_path / "bundle"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "assets/picker.js").write_text("window.Acme = {};", encoding="utf-8")
    return ExtensionReleaseRecord(
        release_id="pr_candidate",
        extension_id="acme.picker",
        release_version="1.0.0",
        account_id=DEFAULT_ACCOUNT_ID,
        source_kind="file",
        descriptor={
            "bundle_root": str(bundle),
            "distribution": {"execution_modes": ["python_subprocess"]},
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
                        "page_id": "resources",
                        "components": [
                            {
                                "component_id": "picker",
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
                        ],
                    }
                ]
            },
        },
    )


def _workflow(tmp_path: Path):
    release = _release(tmp_path)
    runtime_store = MagicMock()
    runtime_store.get_release.return_value = release
    install_store = ExtensionInstallStore()
    context = ExtensionCommandContext(
        get_logger=lambda: None,
        get_install_store=lambda: install_store,
        get_runtime_store=lambda: runtime_store,
    )
    lifecycle = MagicMock()
    lifecycle.install_record_payload.return_value = None
    workflow = ExtensionReleaseInstallWorkflow(
        context=context,
        lifecycle=lambda: lifecycle,
        validate_dependencies=lambda **_kwargs: [],
        run_handshake=lambda **_kwargs: None,
        project_release=lambda **_kwargs: None,
        materialize_bridges=lambda **_kwargs: None,
        validate_remote_url=lambda value, **_kwargs: value,
    )
    return workflow, install_store, runtime_store


def _scope(account_id: str = DEFAULT_ACCOUNT_ID) -> ScopeContext:
    return ScopeContext(
        account_id=account_id,
        actor_id="admin-a",
        principal_type="admin",
    )


def test_public_install_input_rejects_prevalidated_browser_authority_payload() -> None:
    with pytest.raises(ValidationError):
        ExtensionReleaseInstallInput.model_validate(
            {
                "release_id": "pr_candidate",
                "browser_authority_prevalidated_approval": {
                    "approved_impact_hash": "a" * 64,
                },
            }
        )


def test_direct_release_install_requires_exact_browser_authority_retry(tmp_path: Path):
    workflow, install_store, runtime_store = _workflow(tmp_path)

    _raises_input_125_1 = ExtensionReleaseInstallInput(release_id="pr_candidate")
    _raises_input_125_2 = _scope()
    with pytest.raises(ServicePreconditionError) as preview:
        workflow.install(_raises_input_125_1, scope=_raises_input_125_2)
    assert install_store.get(extension_id="acme.picker", account_id="acc_a") is None

    installed = workflow.install(
        ExtensionReleaseInstallInput(
            release_id="pr_candidate",
            approved_browser_authority_impact_hash=preview.value.payload["impact_hash"],
        ),
        scope=_scope(),
    )

    record = install_store.get(extension_id="acme.picker", account_id=DEFAULT_ACCOUNT_ID)
    assert installed.install_id == record.install_id
    approval = record.provenance["browser_authority_approval"]
    assert approval["authority_contract_version"] == "extension_browser_authority_v1"
    assert approval["approved_impact_hash"] == preview.value.payload["impact_hash"]
    assert approval["actor_id"] == "admin-a"
    assert approval["approved_at"] == pytest.approx(approval["approved_at"])
    assert approval["account_id"] == DEFAULT_ACCOUNT_ID
    assert approval["authority_fingerprint"]
    from core.application.extensions.query_detail_projection import ExtensionDetailProjector

    release = runtime_store.get_release.return_value
    authority_status = ExtensionDetailProjector._browser_authority_status(
        record=record,
        release=release,
    )
    assert authority_status["approval_required"] is True
    assert authority_status["approved"] is True
    assert authority_status["matches"] is True
    assert (
        authority_status["current_authority_fingerprint"]
        == (authority_status["approved_authority_fingerprint"])
    )
    assert "externally supplied browser code" in authority_status["warning"]

    from core.application.extensions.command_lifecycle_activation import (
        ExtensionLifecycleActivationOperations,
    )

    install_store.disable(extension_id="acme.picker", account_id=DEFAULT_ACCOUNT_ID)
    context = ExtensionCommandContext(
        get_logger=lambda: None,
        get_install_store=lambda: install_store,
        get_runtime_store=lambda: runtime_store,
    )
    activation = ExtensionLifecycleActivationOperations(
        context=context,
        core=MagicMock(),
        install=MagicMock(),
        recovery=MagicMock(),
    )
    assert activation.enable_extension("acme.picker", scope=_scope()).state == "active"
    install_store.disable(extension_id="acme.picker", account_id=DEFAULT_ACCOUNT_ID)
    (Path(release.descriptor["bundle_root"]) / "assets/picker.js").write_text(
        "window.Acme = {version: 2};",
        encoding="utf-8",
    )
    _raises_input_187_1 = _scope()
    with pytest.raises(ServicePreconditionError) as changed:
        activation.enable_extension("acme.picker", scope=_raises_input_187_1)
    assert changed.value.payload["impact_hash"] != preview.value.payload["impact_hash"]
    assert (
        ExtensionDetailProjector._browser_authority_status(
            record=record,
            release=release,
        )["matches"]
        is False
    )


def test_reenable_requires_persisted_matching_browser_authority(tmp_path: Path):
    from core.application.extensions.command_lifecycle_activation import (
        ExtensionLifecycleActivationOperations,
    )

    release = _release(tmp_path)
    runtime_store = MagicMock()
    runtime_store.get_release.return_value = release
    install_store = ExtensionInstallStore()
    record = install_store.install(
        extension_id=release.extension_id,
        version=release.release_version,
        account_id=DEFAULT_ACCOUNT_ID,
        release_id=release.release_id,
        provenance={},
    )
    install_store.disable(extension_id=release.extension_id, account_id=DEFAULT_ACCOUNT_ID)
    context = ExtensionCommandContext(
        get_logger=lambda: None,
        get_install_store=lambda: install_store,
        get_runtime_store=lambda: runtime_store,
    )
    install = MagicMock()
    activation = ExtensionLifecycleActivationOperations(
        context=context,
        core=MagicMock(),
        install=install,
        recovery=MagicMock(),
    )

    _raises_input_229_1 = _scope()
    with pytest.raises(ServicePreconditionError) as preview:
        activation.enable_extension(release.extension_id, scope=_raises_input_229_1)
    assert record.state.value == "disabled"
    install.project_release_for_install.assert_not_called()

    result = activation.enable_extension(
        release.extension_id,
        scope=_scope(),
        approved_browser_authority_impact_hash=preview.value.payload["impact_hash"],
    )

    assert result.state == "active"
    refreshed = install_store.get(
        extension_id=release.extension_id,
        account_id=DEFAULT_ACCOUNT_ID,
    )
    assert (
        refreshed.provenance["browser_authority_approval"]["approved_impact_hash"]
        == (preview.value.payload["impact_hash"])
    )


def test_real_cli_enable_binding_cannot_bypass_browser_authority_gate(tmp_path: Path) -> None:
    from core.application.extensions.browser_authority_approval import (
        browser_authority_impact,
        candidate_browser_tree_sha256,
    )
    from core.entrypoints.cli.platform.service_bindings.extensions import extensions_enable

    extension_id = "acme.picker"
    extensions_root = tmp_path / "extensions"
    bundle_root = extensions_root / "acme_picker"
    (bundle_root / "assets").mkdir(parents=True)
    (bundle_root / "assets" / "picker.js").write_text("window.Acme = {};", encoding="utf-8")
    picker_ui = _release(tmp_path).descriptor["ui_manifest"]
    picker_ui["pages"][0]["components"].insert(
        0,
        {
            "component_id": "form",
            "type": "form",
            "ui_schema": {"fields": [{"field_id": "resource_id", "widget": "text"}]},
        },
    )
    picker_ui["pages"][0]["components"][1]["data"]["selection"]["fields"] = [
        {
            "source": "opaque_id",
            "target": "resource_id",
            "required": True,
            "max_length": 128,
        }
    ]
    descriptor = {
        "schema_version": "extension_descriptor_v1",
        "extension_id": extension_id,
        "release_version": "1.0.0",
        "extension_type": "service_extension",
        "identity": {"display_name": "Picker", "description": "desc"},
        "vendor": {"vendor_name": "Acme"},
        "distribution": {
            "channel": "private_byo",
            "execution_modes": ["local"],
            "allows_self_hosted": True,
            "requires_connected_control_plane": False,
            "import_sources": ["file"],
        },
        "runtime": {
            "contract_version": "extension_host_v2",
            "compatibility": {"platform_min": "0.1.0"},
        },
        "artifacts": {
            "extensions": [{"extension_id": extension_id, "ref": "extension.yaml", "publish": True}]
        },
        "provenance": {"origin": "imported"},
    }
    (bundle_root / "extension-descriptor.yaml").write_text(json.dumps(descriptor), encoding="utf-8")
    (bundle_root / "extension.yaml").write_text(
        json.dumps(
            {
                "manifest_version": 2,
                "runtime": {"contract_version": "extension_host_v2"},
                "lifecycle": {"data_schema_version": 1, "data_migrations": []},
                "extension_id": extension_id,
                "kind": "tool_provider",
                "version": "1.0.0",
                "display_name": "Picker",
                "description": "Picker scan fixture.",
                "vendor": {"name": "Acme"},
                "features": ["tool"],
                "required_scopes": ["extension:invoke"],
                "entrypoint": {"mode": "subprocess", "command": ["python3", "main.py"]},
                "health": {"command": ["python3", "health.py"], "timeout_seconds": 5},
                "ui_manifest": "ui/ui_manifest.yaml",
                "action_manifest": "ui/actions.yaml",
            }
        ),
        encoding="utf-8",
    )
    (bundle_root / "ui").mkdir()
    (bundle_root / "ui" / "ui_manifest.yaml").write_text(
        json.dumps({"version": "1", **picker_ui}),
        encoding="utf-8",
    )
    (bundle_root / "ui" / "actions.yaml").write_text(
        json.dumps(
            {
                "actions": [
                    {
                        "action_id": "picker_bootstrap",
                        "title": "Picker bootstrap",
                        "description": "Prepare the external picker.",
                        "required_scopes": ["extension:invoke"],
                        "handler": {"mode": "extension"},
                        "mutates_platform": False,
                        "parameters": [],
                        "workflow_visible": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (bundle_root / "main.py").write_text("print('{}')\n", encoding="utf-8")
    (bundle_root / "health.py").write_text("print('ok')\n", encoding="utf-8")
    from core.infrastructure.extension_runtime.extension_runtime import scan_extensions

    scanned = scan_extensions(str(extensions_root))
    assert len(scanned) == 1
    assert scanned[0]["status"] == "valid", json.dumps(scanned[0], indent=2, default=str)

    from core.application.extensions.runtime_store import ExtensionRuntimeStore

    runtime_store = ExtensionRuntimeStore()
    observed_releases: list[ExtensionReleaseRecord] = []
    create_release = runtime_store.create_release

    def observe_release(record: ExtensionReleaseRecord | dict) -> ExtensionReleaseRecord:
        saved = create_release(record)
        observed_releases.append(saved)
        return saved

    runtime_store.create_release = observe_release
    install_store = ExtensionInstallStore()
    bl = MagicMock()
    bl.conn = None
    catalog: dict[str, dict] = {}
    provider_rows: dict[str, dict] = {}
    bl.save_provider.side_effect = lambda row: provider_rows.update({row["provider_key"]: row})

    def sync_scan(_connection: object, scanned: list[dict]) -> tuple[list[dict], list]:
        catalog.clear()
        catalog.update(
            {
                str(row.get("extension_key") or ""): dict(row)
                for row in scanned
                if str(row.get("extension_key") or "")
            }
        )
        return scanned, []

    bl.sync_extension_scan_records.side_effect = sync_scan
    bl.get_extension.side_effect = lambda key: catalog.get(str(key))
    bl.list_extensions.side_effect = lambda **_kwargs: list(catalog.values())
    selected: dict[str, object] = {}

    def sync_enabled(
        _connection: object,
        extension_id: str,
        *,
        version: str | None,
        enabled: bool,
        config: dict,
    ) -> tuple[dict, list]:
        selected.update(version=version, enabled=enabled, config=config)
        catalog[extension_id]["enabled"] = enabled
        return extension_id, []

    bl.sync_enabled_extension_record.side_effect = sync_enabled
    from core.application.extensions.binding_service import ExtensionBindingService

    binding = ExtensionBindingService(
        get_logger=lambda: bl,
        get_operation_store=lambda: bl,
        resolve_extension_runtime_record=lambda _extension_id: (None, None),
        extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
        get_extension_install_store=lambda: install_store,
        get_extension_runtime_store=lambda: runtime_store,
    )

    runtime = MagicMock(extension_binding_service=binding)

    with (
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
            return_value=str(extensions_root),
        ),
        patch(
            "core.application.extensions.command_lifecycle_activation.validate_managed_release_artifact"
        ),
        # Keep provider lifecycle validation real, using the same selected stores
        # rather than querying the logger's unconfigured PostgreSQL mock.
        patch(
            "core.application.extensions.extension_scan_lifecycle.DBExtensionInstallStore",
            return_value=install_store,
        ),
        patch(
            "core.application.extensions.extension_scan_lifecycle.DBExtensionRuntimeStore",
            return_value=runtime_store,
        ),
    ):
        _raises_input_427_1 = extensions_enable(
            runtime, ExtensionToggleCommandInput(extension_id=extension_id)
        )
        with pytest.raises(ServicePreconditionError) as preview:
            asyncio.run(_raises_input_427_1)
        assert provider_rows == {}
        assert install_store.get(extension_id=extension_id, account_id=DEFAULT_ACCOUNT_ID) is None
        assert observed_releases
        from core.infrastructure.extension_runtime.extension_runtime import (
            compile_extension_bundle,
        )

        release = observed_releases[-1]
        compiled = compile_extension_bundle(bundle_root)
        cross_account_hash = browser_authority_impact(
            account_id="acc_other",
            project_id="",
            extension_id=extension_id,
            install_id="",
            old_release_id="",
            new_release_id=release.release_id,
            descriptor_sha256=release.descriptor_sha256,
            candidate_tree_sha256=candidate_browser_tree_sha256(release.descriptor),
            manifest=compiled,
        ).impact_hash
        _raises_input_453_1 = extensions_enable(
            runtime,
            ExtensionToggleCommandInput(
                extension_id=extension_id, approved_browser_authority_impact_hash=cross_account_hash
            ),
        )
        with pytest.raises(ServicePreconditionError):
            asyncio.run(_raises_input_453_1)
        assert provider_rows == {}
        assert install_store.get(extension_id=extension_id, account_id=DEFAULT_ACCOUNT_ID) is None

        enabled = asyncio.run(
            extensions_enable(
                runtime,
                ExtensionToggleCommandInput(
                    extension_id=extension_id,
                    version="1.0.0",
                    config={"region": "eu"},
                    approved_browser_authority_impact_hash=(preview.value.payload["impact_hash"]),
                ),
            )
        )
        assert enabled.extension
        assert provider_rows[extension_id]["provider_type_key"] == "tool_provider"
        assert provider_rows[extension_id]["enabled"] is True
        assert selected == {
            "version": "1.0.0",
            "enabled": True,
            "config": {"region": "eu"},
        }
        record = install_store.get(
            extension_id=extension_id,
            account_id=DEFAULT_ACCOUNT_ID,
        )
        assert record is not None
        release = runtime_store.get_release(record.release_id)
        assert release is not None
        assert release.descriptor["kind"] == "tool_provider"
        assert release.descriptor["features"] == ["tool"]
        assert provider_rows[extension_id]["display_name"] == "Picker"
        assert provider_rows[extension_id]["description"] == "Picker scan fixture."
        assert provider_rows[extension_id]["features"] == ["tool"]
        from core.application.extensions.query_detail_projection import (
            ExtensionDetailProjector,
        )

        status = ExtensionDetailProjector._browser_authority_status(
            record=record,
            release=release,
        )
        assert status["approval_required"] is True
        assert status["approved"] is True
        assert status["matches"] is True

        from core.application.extensions.browser_assets import (
            resolve_authorized_browser_asset,
        )
        from core.application.extensions.query_service import ExtensionQueryService

        query = ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=lambda: install_store,
            get_runtime_store=lambda: runtime_store,
            list_extension_projects=lambda *, account_id: [
                {"id": "project-current", "account_id": account_id}
            ],
        )
        project_scope = _scope().model_copy(
            update={
                "project_id": "project-current",
                "allowed_project_ids": frozenset({"project-current"}),
            }
        )

        page = query.get_extension_page_context(
            extension_id=extension_id,
            selected_project_id="project-current",
            scope=project_scope,
        )
        assert page.compiled_manifest["ui_manifest"]["pages"]
        asset_manifest = query.get_browser_asset_manifest(
            extension_id,
            install_id=record.install_id,
            release_id=record.release_id,
            descriptor_sha256=release.descriptor_sha256,
            project_id="project-current",
            scope=project_scope,
        )
        asset_path = resolve_authorized_browser_asset(
            asset_manifest,
            asset_ref="assets/picker.js",
            extension_id=extension_id,
            install_id=record.install_id,
            release_id=record.release_id,
            descriptor_sha256=release.descriptor_sha256,
            project_id="project-current",
            scope=project_scope,
        )
        assert asset_path.read_text(encoding="utf-8") == "window.Acme = {};"

        (bundle_root / "assets" / "picker.js").write_text(
            "window.Acme = { changed: true };", encoding="utf-8"
        )
        mutated_status = ExtensionDetailProjector._browser_authority_status(
            record=record,
            release=release,
        )
        assert mutated_status["approval_required"] is True
        assert mutated_status["matches"] is False
        with pytest.raises(ServicePreconditionError):
            query.get_browser_asset_manifest(
                extension_id,
                install_id=record.install_id,
                release_id=record.release_id,
                descriptor_sha256=release.descriptor_sha256,
                project_id="project-current",
                scope=project_scope,
            )
        with pytest.raises(ServicePreconditionError):
            query.get_extension_page_context(
                extension_id=extension_id,
                selected_project_id="project-current",
                scope=project_scope,
            )

        _raises_input_572_1 = extensions_enable(
            runtime, ExtensionToggleCommandInput(extension_id=extension_id)
        )
        with pytest.raises(ServicePreconditionError) as changed_preview:
            asyncio.run(_raises_input_572_1)
        asyncio.run(
            extensions_enable(
                runtime,
                ExtensionToggleCommandInput(
                    extension_id=extension_id,
                    approved_browser_authority_impact_hash=(
                        changed_preview.value.payload["impact_hash"]
                    ),
                ),
            )
        )
        query.get_extension_page_context(
            extension_id=extension_id,
            selected_project_id="project-current",
            scope=project_scope,
        )

        refreshed = install_store.get(
            extension_id=extension_id,
            account_id=DEFAULT_ACCOUNT_ID,
        )
        assert refreshed is not None
        approved_provenance = dict(refreshed.provenance)
        install_store.set_provenance(
            extension_id=extension_id,
            account_id=DEFAULT_ACCOUNT_ID,
            provenance={},
        )
        with pytest.raises(ServicePreconditionError):
            query.get_extension_page_context(
                extension_id=extension_id,
                selected_project_id="project-current",
                scope=project_scope,
            )
        install_store.set_provenance(
            extension_id=extension_id,
            account_id=DEFAULT_ACCOUNT_ID,
            provenance=approved_provenance,
        )

        ui_payload = json.loads(
            (bundle_root / "ui" / "ui_manifest.yaml").read_text(encoding="utf-8")
        )
        ui_payload["pages"][0]["components"][1]["data"]["scripts"][0]["src"] = (
            "https://changed.example.test/picker.js"
        )
        ui_payload["pages"][0]["components"][1]["data"]["allowed_script_origins"] = [
            "https://changed.example.test"
        ]
        (bundle_root / "ui" / "ui_manifest.yaml").write_text(
            json.dumps(ui_payload), encoding="utf-8"
        )
        with pytest.raises(ServicePreconditionError):
            query.get_browser_asset_manifest(
                extension_id,
                install_id=record.install_id,
                release_id=record.release_id,
                descriptor_sha256=release.descriptor_sha256,
                project_id="project-current",
                scope=project_scope,
            )
        with pytest.raises(ServicePreconditionError) as remote_preview:
            query.get_extension_page_context(
                extension_id=extension_id,
                selected_project_id="project-current",
                scope=project_scope,
            )
        asyncio.run(
            extensions_enable(
                runtime,
                ExtensionToggleCommandInput(
                    extension_id=extension_id,
                    approved_browser_authority_impact_hash=(
                        remote_preview.value.payload["impact_hash"]
                    ),
                ),
            )
        )

        ui_payload["pages"][0]["components"] = [
            component
            for component in ui_payload["pages"][0]["components"]
            if component.get("type") != "external_resource_picker"
        ]
        (bundle_root / "ui" / "ui_manifest.yaml").write_text(
            json.dumps(ui_payload), encoding="utf-8"
        )
        (bundle_root / "assets" / "picker.js").write_text(
            "window.Acme = { bypass: true };", encoding="utf-8"
        )
        safe_page = query.get_extension_page_context(
            extension_id=extension_id,
            selected_project_id="project-current",
            scope=project_scope,
        )
        assert "external_resource_picker" not in json.dumps(safe_page.compiled_manifest)
        safe_asset_manifest = query.get_browser_asset_manifest(
            extension_id,
            install_id=record.install_id,
            release_id=record.release_id,
            descriptor_sha256=release.descriptor_sha256,
            project_id="project-current",
            scope=project_scope,
        )
        with pytest.raises(ServiceNotFoundError):
            resolve_authorized_browser_asset(
                safe_asset_manifest,
                asset_ref="assets/picker.js",
                extension_id=extension_id,
                install_id=record.install_id,
                release_id=record.release_id,
                descriptor_sha256=release.descriptor_sha256,
                project_id="project-current",
                scope=project_scope,
            )

        (bundle_root / "extension.yaml").unlink()
        with pytest.raises(ServiceValidationError):
            query.get_extension_page_context(
                extension_id=extension_id,
                selected_project_id="project-current",
                scope=project_scope,
            )
        with pytest.raises(ServiceValidationError):
            query.get_browser_asset_manifest(
                extension_id,
                install_id=record.install_id,
                release_id=record.release_id,
                descriptor_sha256=release.descriptor_sha256,
                project_id="project-current",
                scope=project_scope,
            )


def test_no_production_caller_directly_enables_through_legacy_state_binding() -> None:
    direct_enables: list[tuple[Path, ast.Call]] = []
    for path in Path("core").rglob("*.py"):
        for node in parse_file(path).nodes_of(ast.Call):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "set_enabled_state":
                continue
            enabled = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "enabled"),
                None,
            )
            if isinstance(enabled, ast.Constant) and enabled.value is True:
                direct_enables.append((path, node))

    assert len(direct_enables) == 1
    path, call = direct_enables[0]
    assert path.as_posix().endswith("core/entrypoints/cli/platform/service_bindings/extensions.py")
    assert any(keyword.arg == "approved_browser_authority_impact_hash" for keyword in call.keywords)
    state_source = Path("core/application/extensions/extension_state_service.py").read_text(
        encoding="utf-8"
    )
    private_body = state_source.split("def _set_enabled_state_locked", 1)[1]
    assert private_body.index("authorize_enable_extension(") < private_body.index(
        "sync_enabled_extension_with_llm_lifecycle("
    )


def test_browser_authority_gate_bypass_is_private_to_post_gate_archive_publication() -> None:
    bypass_callers: list[Path] = []
    for path in Path("core").rglob("*.py"):
        for node in parse_file(path).nodes_of(ast.Call):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "set_enabled_state_locked":
                continue
            bypass = next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "_skip_browser_authority_gate"
                ),
                None,
            )
            if isinstance(bypass, ast.Constant) and bypass.value is True:
                bypass_callers.append(path)

    assert {path.as_posix() for path in bypass_callers} == {
        "core/application/extensions/archive_new_install_service.py",
        "core/application/extensions/archive_update_models.py",
        "core/application/extensions/extension_update_persistence.py",
    }
    state_source = Path("core/application/extensions/extension_state_service.py").read_text(
        encoding="utf-8"
    )
    public_signature = state_source.split("def set_enabled_state(", 1)[1].split(") ->", 1)[0]
    assert "_skip_browser_authority_gate" not in public_signature
