from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = ROOT / "core/entrypoints/cli/scripts/check-provider-neutral-core.py"
REGISTRY_PATH = ROOT / "infra/tooling/quality/check-registry.json"


def _scanner() -> ModuleType:
    assert SCANNER_PATH.is_file(), "provider-neutral core scanner is missing"
    spec = importlib.util.spec_from_file_location("provider_neutral_core_check", SCANNER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_policy(root: Path) -> Path:
    policy = root / "extensions/acme_cloud/tests/oauth-boundary-policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "patterns": [
                    {
                        "rule": "provider_identity",
                        "needles": ["acme"],
                        "regex": r"acme[\s._-]*cloud",
                    },
                    {
                        "rule": "provider_identity_field",
                        "needles": ["provider_id"],
                        "regex": (
                            r"""(?:(?:"provider_id"|'provider_id'|\bprovider_id\b)"""
                            r"""\s*(?::\s*[A-Za-z_][A-Za-z0-9_.\[\], |]*\s*=|[:=])\s*"""
                            r"""(?:\(\s*(?:[rRuUbBfF]{0,2}(?P<q1>["'])acme(?P=q1)|`acme`|acme)\s*\)"""
                            r"""|(?:[rRuUbBfF]{0,2}(?P<q2>["'])acme(?P=q2)|`acme`|acme)))"""
                            r"(?=$|[\s,;}])"
                        ),
                    },
                    {
                        "rule": "provider_endpoint",
                        "needles": ["acme"],
                        "regex": r"identity\.acme\.example",
                    },
                    {
                        "rule": "provider_token",
                        "needles": ["acme-token"],
                        "regex": r"\bacme-token-",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return policy


def test_current_repository_has_no_provider_specific_oauth_outside_owner_surfaces() -> None:
    assert _scanner().scan_repository(ROOT) == []


def test_only_frontend_template_data_and_its_contract_test_are_sanctioned(
    tmp_path: Path,
) -> None:
    scanner = _scanner()
    _fake_policy(tmp_path)
    template_root = tmp_path / "assets/frontend/src/pages/registry/mcp"
    template_root.mkdir(parents=True)
    (template_root / "mcp-templates.json").write_text(
        '{"base_url":"https://identity.acme.example/mcp"}\n', encoding="utf-8"
    )
    (template_root / "mcp-templates.test.ts").write_text(
        'expect("acme-token-canary").toBeTruthy();\n', encoding="utf-8"
    )
    loader = template_root / "mcp-templates.ts"
    loader.write_text('const provider = "acme_cloud";\n', encoding="utf-8")

    findings = scanner.scan_repository(tmp_path)

    assert all(finding.path == loader.relative_to(tmp_path) for finding in findings)


def test_the_retired_host_install_policy_path_is_not_sanctioned(tmp_path: Path) -> None:
    """No host policy file names extensions any more, so none is exempt from the scan."""
    scanner = _scanner()
    _fake_policy(tmp_path)
    host_policy = tmp_path / "infra/extension-runtime/extension-install-policy.yaml"
    host_policy.parent.mkdir(parents=True)
    host_policy.write_text("extensions:\n  acme_cloud: {}\n", encoding="utf-8")
    sibling = host_policy.with_name("provider-runtime.yaml")
    sibling.write_text("provider: acme_cloud\n", encoding="utf-8")

    findings = scanner.scan_repository(tmp_path)

    flagged = {finding.path for finding in findings}
    assert host_policy.relative_to(tmp_path) in flagged
    assert sibling.relative_to(tmp_path) in flagged


def test_scanner_discovers_extension_policy_and_rejects_core_path_content_and_test_root(
    tmp_path: Path,
) -> None:
    scanner = _scanner()
    _fake_policy(tmp_path)
    production = tmp_path / "core/application/extensions/acme_cloud.py"
    production.parent.mkdir(parents=True)
    production.write_text('endpoint = "https://identity.acme.example/token"\n', encoding="utf-8")
    frontend_test = tmp_path / "assets/frontend/tests/e2e/acme-cloud-oauth.spec.ts"
    frontend_test.parent.mkdir(parents=True)
    frontend_test.write_text("const token = 'acme-token-canary';\n", encoding="utf-8")

    findings = scanner.scan_repository(tmp_path)

    assert {(finding.kind, finding.path) for finding in findings} >= {
        ("path", production.relative_to(tmp_path)),
        ("content", production.relative_to(tmp_path)),
        ("path", frontend_test.relative_to(tmp_path)),
        ("content", frontend_test.relative_to(tmp_path)),
    }


def test_scanner_allows_provider_identity_only_inside_declaring_extension(tmp_path: Path) -> None:
    scanner = _scanner()
    policy = _fake_policy(tmp_path)
    owner_source = policy.parents[1] / "runtime/provider.py"
    owner_source.parent.mkdir(parents=True)
    owner_source.write_text('PROVIDER = "acme_cloud"\n', encoding="utf-8")

    assert scanner.scan_repository(tmp_path) == []


def test_scanner_rejects_provider_identity_in_a_different_extension(tmp_path: Path) -> None:
    scanner = _scanner()
    _fake_policy(tmp_path)
    foreign = tmp_path / "extensions/other_provider/runtime/client.py"
    foreign.parent.mkdir(parents=True)
    foreign.write_text('PROVIDER = "acme_cloud"\n', encoding="utf-8")

    findings = scanner.scan_repository(tmp_path)

    assert {(finding.kind, finding.path) for finding in findings} >= {
        ("content", foreign.relative_to(tmp_path)),
    }


def test_scanner_rejects_provider_identity_field_outside_owner(tmp_path: Path) -> None:
    scanner = _scanner()
    _fake_policy(tmp_path)
    core_source = tmp_path / "core/application/extensions/registry.py"
    core_source.parent.mkdir(parents=True)
    core_source.write_text(
        (
            'provider_id = "acme"\n'
            'grouped = provider_id = ("acme")\n'
            'provider_id: str = "acme"\n'
            'provider_id: str = r"acme"\n'
            'oauth_provider_id: str = r"acme"\n'
        ),
        encoding="utf-8",
    )
    frontend_source = tmp_path / "assets/frontend/src/provider-registry.ts"
    frontend_source.parent.mkdir(parents=True)
    frontend_source.write_text(
        ('const provider = { provider_id: `acme` };\nconst quoted = { "provider_id": "acme" };\n'),
        encoding="utf-8",
    )

    findings = scanner.scan_repository(tmp_path)

    assert {
        finding.line
        for finding in findings
        if finding.kind == "content" and finding.path == core_source.relative_to(tmp_path)
    } == {1, 2, 3, 4}
    assert {
        finding.line
        for finding in findings
        if finding.kind == "content" and finding.path == frontend_source.relative_to(tmp_path)
    } == {1, 2}


def test_scanner_never_dereferences_outside_repository_symlinks(tmp_path: Path) -> None:
    scanner = _scanner()
    _fake_policy(tmp_path)
    outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"
    outside.write_text("acme_cloud acme-token-secret", encoding="utf-8")
    safe_link = tmp_path / "core/safe-link.txt"
    safe_link.parent.mkdir(parents=True)
    safe_link.symlink_to(outside)
    unsafe_link = tmp_path / "tests/acme-cloud-oauth.spec.ts"
    unsafe_link.parent.mkdir(parents=True)
    unsafe_link.symlink_to(outside)

    findings = scanner.scan_repository(tmp_path)

    assert not any(finding.path == safe_link.relative_to(tmp_path) for finding in findings)
    assert {(finding.kind, finding.path) for finding in findings} >= {
        ("path", unsafe_link.relative_to(tmp_path)),
    }


def test_invalid_extension_policy_fails_closed(tmp_path: Path) -> None:
    scanner = _scanner()
    policy = _fake_policy(tmp_path)
    policy.write_text('{"schema_version":1,"patterns":[]}', encoding="utf-8")

    try:
        scanner.scan_repository(tmp_path)
    except ValueError as exc:
        assert "invalid OAuth boundary policy" in str(exc)
    else:
        raise AssertionError("empty boundary policy was accepted")


def test_provider_neutral_scanner_is_a_mandatory_tier_two_check() -> None:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    checks = {row["id"]: row for row in registry["checks"]}

    check = checks["provider-neutral-core"]
    assert check["owner_tier"] == 2
    # AC17-R97: the canonical command runs the scanner through the gate, so it
    # executes in the container like every other non-external check. The scanner
    # itself is still what runs -- lint.sh dispatches it as `provider-neutral-core`.
    assert check["command"] == [
        "bash",
        "core/entrypoints/cli/scripts/lint.sh",
        "--checks",
        "provider-neutral-core",
    ]
    assert SCANNER_PATH.is_file()
    assert set(check["inputs"]) >= {
        "assets/frontend/**",
        "core/**",
        "db/**",
        "docs/architecture/adr/0023-extension-owned-oauth-boundary.md",
        "extensions/*/tests/oauth-boundary-policy.json",
        "extensions/**",
        "infra/**",
        "tests/**",
    }
