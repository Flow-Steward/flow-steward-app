from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.support.extension_icon import (
    EXTENSION_ICON_PNG,
    png_bytes,
    write_extension_icon,
)


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


_HOST_CONTRACT = "extension_host_v2"


def _write_minimal_yaml_extension(
    root: Path,
    extension_id: str = "acme.demo-notifier",
    bundle_name: str = "acme_demo_notifier",
    *,
    contract_version: str | None = _HOST_CONTRACT,
) -> Path:
    bundle = root / bundle_name
    runtime: dict = {
        "extension_contract_v2": {"operation_manifest": "contracts/operation_manifest.yaml"}
    }
    if contract_version is not None:
        runtime["contract_version"] = contract_version
    _write_yaml(
        bundle / "extension.yaml",
        {
            "manifest_version": 2,
            "extension_id": extension_id,
            "kind": "notification_provider",
            "version": "1.0.0",
            "display_name": "Demo Notifier",
            "description": "Test-only notification provider.",
            "vendor": {"name": "Acme Test"},
            "features": ["notify"],
            "required_scopes": ["extension:invoke"],
            "entrypoint": {"command": ["python3", "main.py"]},
            "health": {"command": ["python3", "health.py"]},
            "ui_manifest": "ui/ui_manifest.yaml",
            "action_manifest": "ui/actions.yaml",
            "runtime": runtime,
            "external_effects": [
                {
                    "operation_id": "deliver_notification",
                    "effect_kind": "message_delivery",
                    "channel": "test",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        },
    )
    _write_yaml(
        bundle / "ui" / "actions.yaml",
        {
            "actions": [
                {
                    "action_id": "deliver_notification",
                    "title": "Deliver Notification",
                    "handler": {"mode": "extension"},
                    "workflow_visible": False,
                    "parameters": [
                        {"name": "event", "type": "object", "required": True},
                        {"name": "route", "type": "object", "required": True},
                    ],
                }
            ]
        },
    )
    _write_yaml(
        bundle / "contracts" / "operation_manifest.yaml",
        {
            "operations": [
                {
                    "operation_id": "deliver_notification",
                    "operation_kind": "action",
                    "workflow_visible": False,
                    "inputs": [
                        {"name": "event", "value_type": "object", "required": True},
                        {"name": "route", "value_type": "object", "required": True},
                    ],
                    "outputs": [
                        {"name": "delivered", "value_type": "boolean"},
                        {"name": "provider_message_id", "value_type": "text"},
                    ],
                }
            ]
        },
    )
    _write_yaml(
        bundle / "ui" / "ui_manifest.yaml",
        {"version": "1", "pages": [{"ref": "pages/home.yaml"}]},
    )
    (bundle / "ui" / "pages").mkdir(parents=True, exist_ok=True)
    _write_yaml(
        bundle / "ui" / "pages" / "home.yaml",
        {
            "page_id": "home",
            "title": "Home",
            "description": "",
            "components": [],
        },
    )
    (bundle / "main.py").write_text("print('{\"ok\": true}')\n", encoding="utf-8")
    (bundle / "health.py").write_text("print('ok')\n", encoding="utf-8")
    write_extension_icon(bundle)
    return bundle


def _validate(root: Path, extension_id: str, *, health: bool = False):
    from core.application.tooling.extensions.validate_extensions import (
        ExtensionValidationRequest,
        validate_extensions,
    )

    return validate_extensions(
        ExtensionValidationRequest(extension_id=extension_id, root=root, health=health)
    )


def test_validate_explicit_extension_id_json_mode_returns_ok(tmp_path: Path):
    root = tmp_path / "extensions"
    _write_minimal_yaml_extension(root)
    result = _validate(root, "acme.demo-notifier", health=True)
    payload = result.payload
    assert result.exit_code == 0, payload
    assert payload["ok"] is True
    assert payload["extension"]["extension_key"] == "acme.demo-notifier"
    assert str(payload["extension"]["manifest_path"]).endswith("extension.yaml")
    assert payload["health"]["status"] == "healthy"


def test_validate_synthetic_extension_has_ui_pages_structure(tmp_path: Path):
    root = tmp_path / "extensions"
    bundle = _write_minimal_yaml_extension(root, "acme.demo-notifier", "acme_demo_notifier")
    extension_ids = ["acme.demo-notifier"]

    for extension_id in extension_ids:
        result = _validate(root, extension_id)
        payload = result.payload

        assert result.exit_code == 0, extension_id
        assert payload["ok"] is True, extension_id
        assert payload["ui_pages"]["ok"] is True, extension_id
        assert payload["ui_pages"]["errors"] == [], extension_id
    assert bundle.is_dir()


@pytest.mark.parametrize(
    ("icon", "code", "message"),
    [
        (None, "extension_icon_missing", "icon.png is missing"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "extension_icon_invalid", "is not a PNG"),
        (png_bytes(128, 96), "extension_icon_invalid", "must be square"),
        (png_bytes(16, 16), "extension_icon_invalid", "(is 16x16)"),
        (png_bytes(1024, 1024), "extension_icon_invalid", "(is 1024x1024)"),
        (png_bytes(128, 128, padding=100 * 1024), "extension_icon_invalid", "the limit is 102400"),
    ],
    ids=["missing", "not-png", "not-square", "too-small", "too-large", "too-many-bytes"],
)
def test_validate_refuses_a_bundle_without_a_valid_icon(
    tmp_path: Path, icon: bytes | None, code: str, message: str
) -> None:
    root = tmp_path / "extensions"
    bundle = _write_minimal_yaml_extension(root)
    if icon is None:
        (bundle / "icon.png").unlink()
    else:
        (bundle / "icon.png").write_bytes(icon)

    result = _validate(root, "acme.demo-notifier")

    assert result.exit_code == 1
    errors = result.payload["icon"]["errors"]
    assert [error["code"] for error in errors] == [code]
    assert errors[0]["field_path"] == "icon.png"
    assert message in errors[0]["message"]
    assert "icon invalid" in result.output


def test_validate_accepts_a_square_png_icon(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    bundle = _write_minimal_yaml_extension(root)
    (bundle / "icon.png").write_bytes(png_bytes(32, 32))

    result = _validate(root, "acme.demo-notifier")

    assert result.payload["icon"] == {"errors": [], "ok": True}
    assert "icon.png valid" in result.output
    assert EXTENSION_ICON_PNG.startswith(b"\x89PNG")


def test_validate_requires_explicit_extension_id():
    root = Path(__file__).parent.parent.parent
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "core.entrypoints.cli",
            "extensions",
            "validate",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 3
    assert "extension_id" in proc.stderr
    assert "scanned_count" not in proc.stdout


def test_validate_yaml_only_bundle_passes(tmp_path: Path):
    root = tmp_path / "extensions"
    _write_minimal_yaml_extension(root)

    result = _validate(root, "acme.demo-notifier")
    payload = result.payload
    assert result.exit_code == 0
    assert payload["ok"] is True


def test_validate_fails_a_release_without_host_contract(tmp_path: Path):
    # The host never invokes such a release, so it must not pass as valid and
    # then fail every action with extension_contract_incompatible.
    root = tmp_path / "extensions"
    _write_minimal_yaml_extension(root, contract_version=None)

    result = _validate(root, "acme.demo-notifier")

    payload = result.payload
    assert result.exit_code == 1
    assert payload["ok"] is False
    errors = payload["extension"]["validation_errors"]
    [error] = [err for err in errors if err["field_path"] == "runtime.contract_version"]
    assert error["code"] == "missing_host_contract"
    assert error["severity"] == "error"
    assert _HOST_CONTRACT in error["message"]
    assert "runtime.contract_version is missing" in result.output


def test_validate_fails_a_release_built_for_another_host_contract(tmp_path: Path):
    root = tmp_path / "extensions"
    _write_minimal_yaml_extension(root, contract_version="extension_host_v1")

    result = _validate(root, "acme.demo-notifier")

    assert result.exit_code == 1
    errors = result.payload["extension"]["validation_errors"]
    [error] = [err for err in errors if err["field_path"] == "runtime.contract_version"]
    assert error["code"] == "unsupported_host_contract"
    assert "'extension_host_v1'" in error["message"]
    assert _HOST_CONTRACT in error["message"]


def test_validate_names_no_contract_error_for_the_host_contract(tmp_path: Path):
    root = tmp_path / "extensions"
    _write_minimal_yaml_extension(root)

    result = _validate(root, "acme.demo-notifier")

    errors = result.payload["extension"]["validation_errors"]
    assert result.exit_code == 0, errors
    assert not [err for err in errors if err["field_path"] == "runtime.contract_version"]


def test_validate_rejects_private_core_module_in_artifact(tmp_path: Path):
    root = tmp_path / "extensions"
    bundle = _write_minimal_yaml_extension(root)
    _write_yaml(
        bundle / "extension-descriptor.yaml",
        {"artifacts": {"tools": [{"tool_id": "demo", "ref": "artifacts/tools/demo.yaml"}]}},
    )
    _write_yaml(
        bundle / "artifacts/tools/demo.yaml",
        {"implementation": {"module": "core.application.private_tools"}},
    )

    result = _validate(root, "acme.demo-notifier")
    payload = result.payload
    assert result.exit_code == 1
    assert payload["artifact_modules"]["ok"] is False
    assert "must not reference private core" in payload["artifact_modules"]["errors"][0]


def test_validate_extension_json_fails_yaml_only(tmp_path: Path):
    root = tmp_path / "extensions"
    bundle = root / "json_manifest"
    bundle.mkdir(parents=True)
    (bundle / "extension.json").write_text('{"extension_id":"json_manifest"}', encoding="utf-8")

    result = _validate(root, "json_manifest")
    payload = result.payload
    errors = payload["extension"]["compile_errors"]
    assert result.exit_code == 1
    assert payload["ok"] is False
    assert any(err["code"] == "extension.yaml_only_violation" for err in errors)
    assert any("extension.json" in err["message"] for err in errors)


def test_validate_actions_json_fails_yaml_only(tmp_path: Path):
    root = tmp_path / "extensions"
    bundle = _write_minimal_yaml_extension(root, "acme.bad-actions", "acme_bad_actions")
    (bundle / "ui" / "actions").mkdir(parents=True)
    (bundle / "ui" / "actions" / "actions.json").write_text('{"actions":[]}', encoding="utf-8")

    result = _validate(root, "acme.bad-actions")
    payload = result.payload
    errors = payload["extension"]["compile_errors"]
    assert result.exit_code == 1
    assert any(err["code"] == "extension.yaml_only_violation" for err in errors)
    assert any("ui/actions/actions.json" in err["message"] for err in errors)


def test_validate_schema_json_fails_yaml_only(tmp_path: Path):
    root = tmp_path / "extensions"
    bundle = _write_minimal_yaml_extension(root, "acme.bad-schema", "acme_bad_schema")
    (bundle / "schemas").mkdir(parents=True)
    (bundle / "schemas" / "config.schema.json").write_text('{"type":"object"}', encoding="utf-8")

    result = _validate(root, "acme.bad-schema")
    payload = result.payload
    errors = payload["extension"]["compile_errors"]
    assert result.exit_code == 1
    assert any(err["code"] == "extension.yaml_only_violation" for err in errors)
    assert any("schemas/config.schema.json" in err["message"] for err in errors)


def test_extension_manifest_standard_yaml_documents_callbacks():
    import yaml

    from core.infrastructure.extension_runtime import extension_runtime

    standard_path = (
        Path(__file__).parent.parent.parent
        / "docs"
        / "standards"
        / "extension-manifest-standard.yaml"
    )
    payload = yaml.safe_load(standard_path.read_text(encoding="utf-8"))

    assert payload["manifest_version"] == 2
    assert payload["callbacks"][0]["callback_id"] == "updates"
    assert payload["callbacks"][0]["auth"]["type"] == "shared_secret_header"
    assert payload["schemas"]["config"] == "schemas/config.schema.yaml"
    assert payload["x-validator-contract"]["authoring_format"] == "yaml_only"
    assert (
        payload["x-validator-contract"]["yaml_only_error_code"] == "extension.yaml_only_violation"
    )
    assert "extension.json" in payload["x-validator-contract"]["forbidden_contract_files"]
    assert "signed_state" in payload["x-planned-callback-auth"]
    assert set(payload["x-validator-contract"]["callback_methods"]) == (
        extension_runtime.ALLOWED_CALLBACK_METHODS
    )
    assert set(payload["x-validator-contract"]["callback_auth_types"]) == (
        extension_runtime.ALLOWED_CALLBACK_AUTH_TYPES
    )


def test_prod_env_example_exists():
    root = Path(__file__).parent.parent.parent
    assert (root / ".env.example.prod").is_file()
