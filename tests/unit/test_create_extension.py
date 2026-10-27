from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _forbidden_contract_files(bundle_root: Path) -> list[str]:
    forbidden: list[str] = []
    for path in bundle_root.rglob("*.json"):
        rel = path.relative_to(bundle_root)
        if (
            path.name in {"extension.json", "manifest.json"}
            or rel.match("schemas/*.schema.json")
            or rel.match("ui/ui_manifest.json")
            or rel.match("ui/actions/actions.json")
            or rel.match("ui/queries/queries.json")
            or rel.match("ui/pages/*.json")
            or rel.match("ui/components/*.json")
        ):
            forbidden.append(rel.as_posix())
    return sorted(forbidden)


def _repo_root() -> Path:
    return Path(__file__).parent.parent.parent


def _run_cli(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "core.entrypoints.cli", *args],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _validator_payload(stdout: str) -> dict[str, object]:
    payload = json.loads(stdout)
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict) and isinstance(result.get("payload"), dict):
        return result["payload"]
    if isinstance(payload, dict) and isinstance(payload.get("payload"), dict):
        return payload["payload"]
    output = payload.get("output") if isinstance(payload, dict) else None
    if isinstance(output, str) and output.strip().startswith("{"):
        return json.loads(output)
    return payload


def _create_extension(output_root: Path, extension_id: str, **kwargs: str):
    from core.application.extensions.scaffold import (
        ExtensionScaffoldCreateInput,
        ExtensionScaffoldService,
    )

    return ExtensionScaffoldService().create(
        ExtensionScaffoldCreateInput(
            extension_id=extension_id,
            output_root=output_root,
            **kwargs,
        )
    )


def test_service_generates_generic_yaml_only_bundle(tmp_path: Path) -> None:
    output_root = tmp_path / "extensions"
    result = _create_extension(output_root, "acme.demo-yaml-scaffold")
    assert result.status == "created"
    assert result.extension_id == "acme.demo-yaml-scaffold"
    assert result.files_created > 0

    bundle_root = output_root / "acme_demo_yaml_scaffold"
    expected_files = {
        "extension.yaml",
        "icon.png",
        "README.md",
        "requirements.in",
        "requirements-dev.txt",
        "main.py",
        "health.py",
        "scripts/smoke_check.py",
        "tests/test_acme_demo_yaml_scaffold.py",
        "handlers/__init__.py",
        "handlers/common.py",
        "handlers/queries.py",
        "handlers/actions.py",
        "handlers/router.py",
        "schemas/config.schema.yaml",
        "schemas/secrets.schema.yaml",
        "schemas/subscriptions.schema.yaml",
        "ui/ui_manifest.yaml",
        "ui/actions/actions.yaml",
        "ui/queries/queries.yaml",
        "ui/components/overview.yaml",
        "ui/components/sample_form.yaml",
        "ui/pages/overview.yaml",
        "ui/pages/form.yaml",
    }
    actual_files = {
        path.relative_to(bundle_root).as_posix()
        for path in bundle_root.rglob("*")
        if path.is_file()
    }
    assert actual_files == expected_files
    assert len(actual_files) == 24
    assert result.files_created == 24

    # The scaffold starts every extension with the default logo, a valid icon.
    from core.application.extensions.scaffold import DEFAULT_EXTENSION_ICON_PATH
    from core.infrastructure.extension_runtime.bundle_icon import extension_icon_issues

    assert (bundle_root / "icon.png").read_bytes() == DEFAULT_EXTENSION_ICON_PATH.read_bytes()
    assert extension_icon_issues(bundle_root) == []
    assert "replace that file with your own logo" in (bundle_root / "README.md").read_text(
        encoding="utf-8"
    )

    assert "runtime dependencies" in (bundle_root / "requirements.in").read_text(encoding="utf-8")
    assert "test dependencies" in (bundle_root / "requirements-dev.txt").read_text(encoding="utf-8")

    manifest_text = (bundle_root / "extension.yaml").read_text(encoding="utf-8")
    assert "Flow Steward extension scaffold TODO" in manifest_text
    assert "minimal valid placeholder" in manifest_text
    assert "replace placeholder" in manifest_text
    author_guidance = "\n".join(line for line in manifest_text.splitlines() if line.startswith("#"))
    assert "event_subscriptions" in author_guidance
    assert "supported_events" not in author_guidance
    manifest = yaml.safe_load(manifest_text)
    assert manifest["event_subscriptions"] == []
    assert manifest["manifest_version"] == 2
    assert manifest["runtime"] == {"contract_version": "extension_host_v2"}
    assert manifest["kind"] == "tool_provider"
    assert manifest["vendor"] == {"name": "Your Organization"}
    assert manifest["listing"]["category"] == "other"
    assert manifest["listing"]["summary"] == manifest["description"]
    assert "tool" in manifest["features"]
    assert "task_ingress" not in manifest["features"]
    assert not (bundle_root / "task_ingress").exists()
    assert _forbidden_contract_files(bundle_root) == []

    readme = (bundle_root / "README.md").read_text(encoding="utf-8")
    readme_inline = " ".join(readme.split())
    assert "## Scaffold Placeholders" in readme
    assert "`kind: tool_provider`" in readme
    assert "not a recommendation that every extension should be a tool provider" in readme_inline
    assert "Extension authoring contract files are YAML-only" in readme
    assert "Python code may still use JSON" in readme
    assert "flow-steward extensions validate acme.demo-yaml-scaffold --health" in readme
    assert "flow-steward extensions test acme.demo-yaml-scaffold" in readme
    assert "flow-steward extensions lock-deps acme.demo-yaml-scaffold" in readme
    assert "flow-steward extensions bundle acme.demo-yaml-scaffold" in readme
    assert (bundle_root / "requirements-dev.txt").read_text(encoding="utf-8").startswith("#")

    actions = yaml.safe_load((bundle_root / "ui/actions/actions.yaml").read_text(encoding="utf-8"))[
        "actions"
    ]
    save_settings = next(row for row in actions if row["action_id"] == "save_settings")
    assert save_settings["workflow_visible"] is False
    assert [row["name"] for row in save_settings["parameters"]] == [
        "name",
        "mode",
        "enabled",
        "notes",
    ]


def test_newly_scaffolded_extension_validates_and_smoke_checks(tmp_path: Path) -> None:
    output_root = tmp_path / "extensions"
    _create_extension(output_root, "acme.demo-valid-scaffold")

    validate_proc = _run_cli(
        [
            "extensions",
            "validate",
            "acme.demo-valid-scaffold",
            "--root",
            str(output_root),
            "--json",
        ]
    )
    assert validate_proc.returncode == 0, validate_proc.stderr or validate_proc.stdout
    payload = _validator_payload(validate_proc.stdout)
    assert payload["ok"] is True
    assert payload["extension"]["status"] == "valid"

    smoke_proc = subprocess.run(
        [sys.executable, "scripts/smoke_check.py"],
        cwd=output_root / "acme_demo_valid_scaffold",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert smoke_proc.returncode == 0, smoke_proc.stderr or smoke_proc.stdout
    assert json.loads(smoke_proc.stdout)["ok"] is True


@pytest.mark.parametrize(
    ("raw_request", "message"),
    [
        ("{", "Request payload must be valid JSON"),
        ("[]", "Request payload must be an object"),
    ],
)
def test_generated_main_returns_structured_failure_for_invalid_request_payloads(
    tmp_path: Path, raw_request: str, message: str
) -> None:
    output_root = tmp_path / "extensions"
    _create_extension(output_root, "acme.invalid-request")

    proc = subprocess.run(
        [sys.executable, "main.py"],
        cwd=output_root / "acme_invalid_request",
        input=raw_request,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_payload"
    assert payload["error"] == message
    assert payload["errors"] == [{"code": "invalid_payload", "message": message}]


_ROOT_STEP_SCOPE = {
    "group_id": None,
    "for_each_id": None,
    "step_id": None,
    "step_execution_id": None,
}


@pytest.mark.parametrize(
    ("contract_version", "runtime_context"),
    [
        ("extension_host_v1", dict(_ROOT_STEP_SCOPE)),
        (
            "extension_host_v2",
            {key: value for key, value in _ROOT_STEP_SCOPE.items() if key != "for_each_id"},
        ),
        ("extension_host_v2", {**_ROOT_STEP_SCOPE, "group_id": "g1", "for_each_id": "f1"}),
    ],
    ids=["foreign-contract", "missing-for-each-id", "two-owners"],
)
def test_generated_main_refuses_a_foreign_or_incomplete_host_envelope(
    tmp_path: Path, contract_version: str, runtime_context: dict
) -> None:
    output_root = tmp_path / "extensions"
    _create_extension(output_root, "acme.envelope-check")

    proc = subprocess.run(
        [sys.executable, "main.py"],
        cwd=output_root / "acme_envelope_check",
        input=json.dumps(
            {
                "contract_version": contract_version,
                "runtime_context": runtime_context,
                "mode": "action",
                "action": {"action_id": "save_settings", "values": {"name": "demo"}},
            }
        ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 2, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_host_envelope"
    assert payload["definitely_no_external_effect"] is True
    assert payload["result"] == {}


def test_generated_readme_states_the_host_contract(tmp_path: Path) -> None:
    output_root = tmp_path / "extensions"
    _create_extension(output_root, "acme.contract-readme")

    readme = (output_root / "acme_contract_readme" / "README.md").read_text(encoding="utf-8")

    assert "## Host contract" in readme
    assert "extension_host_v2" in readme
    assert "runtime_context_from_payload" in readme
    for key in ("group_id", "for_each_id", "step_id", "step_execution_id"):
        assert key in readme
    assert readme.index("## Logo") < readme.index("## Host contract")


def test_scaffold_extension_yaml_refs_are_yaml_only(tmp_path: Path) -> None:
    output_root = tmp_path / "extensions"
    _create_extension(output_root, "acme.demo-refs")

    bundle_root = output_root / "acme_demo_refs"
    manifest = yaml.safe_load((bundle_root / "extension.yaml").read_text(encoding="utf-8"))
    refs = [
        manifest["schemas"]["config"],
        manifest["schemas"]["secrets"],
        manifest["schemas"]["subscriptions"],
        manifest["ui_manifest"],
        manifest["query_manifest"],
        manifest["action_manifest"],
    ]
    assert refs
    assert all(str(ref).endswith(".yaml") for ref in refs)
    assert all(not str(ref).endswith(".json") for ref in refs)


def test_scaffold_accepts_catalog_publisher_and_category(tmp_path: Path) -> None:
    output_root = tmp_path / "extensions"
    _create_extension(
        output_root,
        "acme.crm-sync",
        vendor_name="Acme",
        category="sales-automation",
    )
    manifest = yaml.safe_load(
        (output_root / "acme_crm_sync" / "extension.yaml").read_text(encoding="utf-8")
    )
    assert manifest["vendor"]["name"] == "Acme"
    assert manifest["listing"]["category"] == "sales-automation"


def test_scaffold_forbidden_contract_json_fails_validation(tmp_path: Path) -> None:
    output_root = tmp_path / "extensions"
    _create_extension(output_root, "acme.bad-scaffold")

    bundle_root = output_root / "acme_bad_scaffold"
    (bundle_root / "extension.json").write_text(
        '{"extension_id":"acme.bad-scaffold"}', encoding="utf-8"
    )
    (bundle_root / "ui" / "actions" / "actions.json").write_text(
        '{"actions":[]}',
        encoding="utf-8",
    )

    stream = _run_cli(
        [
            "extensions",
            "validate",
            "acme.bad-scaffold",
            "--root",
            str(output_root),
            "--json",
        ]
    )
    assert stream.returncode == 3
    payload = json.loads(stream.stdout)
    assert payload["error"]["code"] == "validation_failed"
    assert "YAML-only" in payload["error"]["message"]
    assert "extension.json" in payload["error"]["message"]
    assert "ui/actions/actions.json" in payload["error"]["message"]


def test_create_extension_refuses_existing_target_without_deleting_files(tmp_path: Path) -> None:
    from core.application.extensions.scaffold import ExtensionScaffoldConflictError

    output_root = tmp_path / "extensions"
    _create_extension(output_root, "acme.demo-existing")
    marker = output_root / "acme_demo_existing" / "keep.txt"
    marker.write_text("preserve me", encoding="utf-8")

    with pytest.raises(ExtensionScaffoldConflictError):
        _create_extension(output_root, "acme.demo-existing")
    assert marker.read_text(encoding="utf-8") == "preserve me"


def test_scaffold_write_failure_never_exposes_partial_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.application.extensions.scaffold as scaffold
    from core.infrastructure.extension_runtime.extension_runtime import scan_extensions

    output_root = tmp_path / "extensions"
    target_dir = output_root / "acme_atomic_scaffold"
    real_write_file = scaffold._write_file
    writes = 0

    def fail_during_staging(path: Path, content: str | bytes) -> None:
        nonlocal writes
        writes += 1
        assert not target_dir.exists()
        real_write_file(path, content)
        assert scan_extensions(str(output_root)) == []
        if writes == 3:
            raise OSError("injected scaffold write failure")

    monkeypatch.setattr(scaffold, "_write_file", fail_during_staging)

    with pytest.raises(OSError, match="injected scaffold write failure"):
        _create_extension(output_root, "acme.atomic-scaffold")

    assert not target_dir.exists()
    assert list(output_root.iterdir()) == []


def test_scaffold_publish_failure_cleans_staged_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.application.extensions.scaffold as scaffold

    output_root = tmp_path / "extensions"
    target_dir = output_root / "acme_atomic_publish"

    def fail_publish(source: object, destination: object) -> None:
        staged_dir = Path(source)
        assert staged_dir.parent == output_root
        assert staged_dir.name.startswith(".acme_atomic_publish.scaffold-")
        assert (staged_dir / "extension.yaml").is_file()
        assert (staged_dir / "scripts" / "smoke_check.py").is_file()
        assert Path(destination) == target_dir
        raise OSError("injected scaffold publish failure")

    monkeypatch.setattr(scaffold, "_rename_no_replace", fail_publish, raising=False)

    with pytest.raises(OSError, match="injected scaffold publish failure"):
        _create_extension(output_root, "acme.atomic-publish")

    assert not target_dir.exists()
    assert list(output_root.iterdir()) == []


def test_scaffold_publish_never_replaces_target_created_after_last_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.application.extensions.scaffold import ExtensionScaffoldConflictError

    output_root = tmp_path / "extensions"
    target_dir = output_root / "acme_atomic_race"
    real_exists = Path.exists
    target_checks = 0

    def create_competing_target_after_last_check(path: Path) -> bool:
        nonlocal target_checks
        if path == target_dir:
            target_checks += 1
            if target_checks == 2:
                path.mkdir()
                return False
        return real_exists(path)

    monkeypatch.setattr(Path, "exists", create_competing_target_after_last_check)

    with pytest.raises(ExtensionScaffoldConflictError):
        _create_extension(output_root, "acme.atomic-race")

    assert target_checks == 2
    assert target_dir.is_dir()
    assert list(target_dir.iterdir()) == []
    assert list(output_root.iterdir()) == [target_dir]


@pytest.mark.parametrize("error_number", (errno.EEXIST, errno.ENOENT, errno.EIO))
def test_scaffold_ambiguous_completed_publish_is_verified_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    import core.application.extensions.scaffold as scaffold

    output_root = tmp_path / "extensions"
    target_dir = output_root / "acme_atomic_replay"
    real_rename = scaffold._rename_no_replace

    def publish_then_report_collision(source: Path, destination: Path) -> None:
        real_rename(source, destination)
        raise OSError(error_number, "simulated replayed rename response")

    monkeypatch.setattr(scaffold, "_rename_no_replace", publish_then_report_collision)

    result = _create_extension(output_root, "acme.atomic-replay")

    assert result.status == "created"
    assert (target_dir / "extension.yaml").is_file()
    assert list(output_root.glob(".acme_atomic_replay.scaffold-*")) == []


def test_scaffold_publish_uses_mkdir_style_directory_mode(tmp_path: Path) -> None:
    output_root = tmp_path / "extensions"
    previous_umask = os.umask(0o022)
    try:
        _create_extension(output_root, "acme.atomic-mode")
    finally:
        os.umask(previous_umask)

    target_dir = output_root / "acme_atomic_mode"
    assert stat.S_IMODE(target_dir.stat().st_mode) == 0o755


@pytest.mark.parametrize(
    "extension_id", ["1", "550e8400-e29b-41d4-a716-446655440000", "ACME.crm", "acme"]
)
def test_input_rejects_noncanonical_extension_ids_before_writing(
    tmp_path: Path, extension_id: str
) -> None:
    from core.application.extensions.scaffold import ExtensionScaffoldCreateInput

    output_root = tmp_path / "extensions"
    with pytest.raises(ValidationError):
        ExtensionScaffoldCreateInput(extension_id=extension_id, output_root=output_root)
    assert not output_root.exists()


def test_input_uses_canonical_normalizer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import core.application.extensions.scaffold as scaffold
    import core.application.extensions.scaffold_contracts as scaffold_contracts

    calls: list[str] = []
    original = scaffold_contracts.normalize_extension_id

    def tracking_normalizer(value: object, field_name: str = "extension_id") -> str:
        calls.append(str(value))
        return original(value, field_name)

    monkeypatch.setattr(scaffold_contracts, "normalize_extension_id", tracking_normalizer)
    command = scaffold.ExtensionScaffoldCreateInput(
        extension_id="acme.crm-sync", output_root=tmp_path / "extensions"
    )
    assert command.extension_id == "acme.crm-sync"
    assert calls == ["acme.crm-sync"]
