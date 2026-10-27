from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

from core.application.management.errors import ServicePreconditionError


def _manifest(bundle_root: Path) -> dict:
    return {
        "bundle_root": str(bundle_root),
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
                                "bridge_global": "Vendor.Picker",
                                "button_label": "Choose resource",
                                "options": {"mode": "documents"},
                                "scripts": [
                                    {"src": "https://sdk.example.test/picker.js"},
                                    {"asset_ref": "assets/picker-bridge.js"},
                                ],
                                "target_component_id": "resource_form",
                                "selection": {
                                    "maximum": 1,
                                    "fields": [
                                        {
                                            "source": "opaque_id",
                                            "target": "resource_id",
                                            "required": True,
                                            "max_length": 128,
                                        }
                                    ],
                                },
                            },
                        }
                    ],
                }
            ]
        },
    }


def _impact(tmp_path: Path, **overrides):
    from core.application.extensions.browser_authority_approval import (
        browser_authority_impact,
    )

    bundle_root = tmp_path / "bundle"
    (bundle_root / "assets").mkdir(parents=True, exist_ok=True)
    asset_path = bundle_root / "assets/picker-bridge.js"
    if not asset_path.exists():
        asset_path.write_bytes(b"window.Vendor = {};")
    values = {
        "account_id": "acc_a",
        "project_id": "",
        "extension_id": "acme.picker",
        "install_id": "pi_current",
        "old_release_id": "pr_old",
        "new_release_id": "pr_new",
        "descriptor_sha256": "a" * 64,
        "candidate_tree_sha256": "b" * 64,
        "manifest": _manifest(bundle_root),
    }
    values.update(overrides)
    return browser_authority_impact(**values)


def test_browser_authority_fingerprint_is_stable_and_full_declaration_bound(tmp_path: Path):
    first = _impact(tmp_path)
    second = _impact(tmp_path)

    assert first.approval_required is True
    assert first.impact_hash == second.impact_hash
    assert first.local_assets == (
        {
            "asset_ref": "assets/picker-bridge.js",
            "sha256": "40a2d9275a8aa7812453e7a7455ea22da1faa920c9af38f65d5c497859219055",
        },
    )
    assert first.remote_script_urls == ("https://sdk.example.test/picker.js",)
    assert first.remote_origins == ("https://sdk.example.test",)

    mutations = (
        lambda value: value["ui_manifest"]["pages"][0]["components"][0]["data"].update(
            {"bridge_global": "Vendor.OtherPicker"}
        ),
        lambda value: value["ui_manifest"]["pages"][0]["components"][0]["data"]["scripts"][
            0
        ].update({"src": "https://sdk.example.test/picker-v2.js"}),
        lambda value: value["ui_manifest"]["pages"][0]["components"][0]["data"].update(
            {"stylesheets": [{"src": "https://sdk.example.test/picker-v2.css"}]}
        ),
        lambda value: value["ui_manifest"]["pages"][0]["components"][0]["data"].update(
            {"allowed_connect_origins": ["https://api.example.test"]}
        ),
        lambda value: value["action_definitions"][0].update({"title": "Bootstrap v2"}),
        lambda value: value["ui_manifest"]["pages"][0]["components"][0]["data"].update(
            {"options": {"mode": "folders"}}
        ),
    )
    manifest = _manifest(Path(first.bundle_root))
    for mutate in mutations:
        candidate = copy.deepcopy(manifest)
        mutate(candidate)
        assert _impact(tmp_path, manifest=candidate).impact_hash != first.impact_hash


def test_current_authority_fingerprint_ignores_transition_only_tree_digest(tmp_path: Path):
    first = _impact(tmp_path)
    transitioned = _impact(tmp_path, candidate_tree_sha256="e" * 64)

    assert transitioned.impact_hash != first.impact_hash
    assert transitioned.authority_fingerprint == first.authority_fingerprint


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "acc_b"),
        ("project_id", "project_b"),
        ("extension_id", "other.picker"),
        ("install_id", "pi_other"),
        ("old_release_id", "pr_other"),
        ("new_release_id", "pr_candidate_other"),
        ("descriptor_sha256", "c" * 64),
        ("candidate_tree_sha256", "d" * 64),
    ],
)
def test_browser_authority_fingerprint_cannot_be_replayed(tmp_path: Path, field: str, value: str):
    first = _impact(tmp_path)

    assert _impact(tmp_path, **{field: value}).impact_hash != first.impact_hash


def test_browser_authority_fingerprint_binds_local_asset_bytes(tmp_path: Path):
    first = _impact(tmp_path)
    asset = Path(first.bundle_root) / "assets/picker-bridge.js"
    asset.write_bytes(b"window.Vendor = {version: 2};")

    assert _impact(tmp_path).impact_hash != first.impact_hash


def test_browser_authority_gate_returns_structured_retry_contract(tmp_path: Path):
    from core.application.extensions.browser_authority_approval import (
        require_browser_authority_approval,
    )

    impact = _impact(tmp_path)
    with pytest.raises(ServicePreconditionError) as exc_info:
        require_browser_authority_approval(impact, approved_impact_hash="")

    assert exc_info.value.payload == {
        "error": "browser_authority_approval_required",
        "impact_hash": impact.impact_hash,
        "authority_contract_version": impact.authority_contract_version,
        "extension_id": "acme.picker",
        "remote_script_urls": ["https://sdk.example.test/picker.js"],
        "remote_origins": ["https://sdk.example.test"],
        "local_asset_refs": ["assets/picker-bridge.js"],
    }
    assert (
        require_browser_authority_approval(
            impact,
            approved_impact_hash=impact.impact_hash,
        )
        == impact
    )


def test_release_without_external_browser_authority_needs_no_approval(tmp_path: Path):
    from core.application.extensions.browser_authority_approval import (
        browser_authority_impact,
        require_browser_authority_approval,
    )

    impact = browser_authority_impact(
        account_id="acc_a",
        project_id="",
        extension_id="acme.safe",
        install_id="",
        old_release_id="",
        new_release_id="pr_safe",
        descriptor_sha256="a" * 64,
        candidate_tree_sha256="b" * 64,
        manifest={"bundle_root": str(tmp_path), "action_definitions": []},
    )

    assert impact.approval_required is False
    assert impact.impact_hash == ""
    assert require_browser_authority_approval(impact, approved_impact_hash="") == impact


def test_canonical_standard_matches_browser_security_contract() -> None:
    from core.application.extensions.browser_authority_approval import (
        BROWSER_AUTHORITY_CONTRACT_VERSION,
    )

    standard = yaml.safe_load(
        Path("docs/standards/extension-manifest-standard.yaml").read_text(encoding="utf-8")
    )
    contract = standard["x-validator-contract"]["external_resource_picker_v1"]
    bootstrap = contract["bootstrap_result"]
    authority = contract["browser_authority_approval"]
    bridge_source = Path(
        "assets/frontend/src/components/extensions/external-resource-picker-bridge.ts"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"const FORBIDDEN_BOOTSTRAP_KEYS = new Set\(\[(.*?)\]\);",
        bridge_source,
        flags=re.DOTALL,
    )

    assert match is not None
    runtime_keys = set(re.findall(r'"([a-z]+)"', match.group(1)))
    assert set(bootstrap["normalized_exact_forbidden_keys"]) == runtime_keys
    assert set(bootstrap["allowed_non_equal_examples"]) == {
        "authorizationurl",
        "tokentype",
    }
    assert bootstrap["matching"] == "exact_after_removing_non_ascii_alphanumeric_characters"
    assert authority["contract_version"] == BROWSER_AUTHORITY_CONTRACT_VERSION
    assert authority["declaration_change_invalidates_approval"] is True
    assert authority["remote_url_or_origin_change_invalidates_approval"] is True
    assert authority["declared_local_asset_bytes_change_invalidates_approval"] is True
