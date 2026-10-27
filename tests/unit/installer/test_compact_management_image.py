"""Source Compact must retain a separate Docker-enabled management image."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application.installer import InstallConfig
from core.application.installer.errors import InstallerFailure
from core.infrastructure.installer import gateway_image_provenance
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.installer.registry_image_resolver import LocalImageContentIdentity
from tests.unit.installer.test_installer_gateway import (
    _CLI_IMAGE_ID,
    _FULL_REVISION,
    FakeProcessExecutor,
    _gateway,
    _image_inspection,
    _write_env_values,
)

_COMPACT_IMAGE = "sha256:" + "a" * 64
_CONFIG = InstallConfig(environment="production", deployment_profile="compact")


def _source_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    process = FakeProcessExecutor()
    gateway = _gateway(tmp_path, process, environment={"FS_INSTALL_MODE": "source"})
    _write_env_values(
        tmp_path / ".env.prod",
        {
            "FS_INSTALL_MODE": "source",
            "FS_COMPACT_IMAGE": _COMPACT_IMAGE,
            "FS_COMPACT_IMAGE_REVISION": _FULL_REVISION,
            "FS_BUILD_SHA": _FULL_REVISION,
        },
    )
    compact_inspect = ("docker", "image", "inspect", _COMPACT_IMAGE)
    process.outcomes[compact_inspect] = ProcessOutcome(
        command=compact_inspect,
        returncode=0,
        stdout=_image_inspection(
            _COMPACT_IMAGE,
            service="compact-runtime",
            entrypoint=("python", "-m", "core.entrypoints.cli.compact_provider_runtime"),
        ),
    )
    monkeypatch.setattr(
        gateway_image_provenance,
        "resolve_local_image_archive",
        lambda **_kwargs: LocalImageContentIdentity(
            index_digest=_COMPACT_IMAGE,
            manifest_digest=_COMPACT_IMAGE,
            config_digest=_COMPACT_IMAGE,
        ),
    )
    tag = f"flow-steward-cli:compact-flow-steward-{_FULL_REVISION[:12]}"
    for image in (_COMPACT_IMAGE, tag, _CLI_IMAGE_ID):
        command = ("docker", "image", "inspect", "--format", "{{.Id}}", image)
        process.outcomes[command] = ProcessOutcome(
            command=command,
            returncode=0,
            stdout=(_COMPACT_IMAGE if image == _COMPACT_IMAGE else _CLI_IMAGE_ID) + "\n",
        )
    cli_inspect = ("docker", "image", "inspect", _CLI_IMAGE_ID)
    process.outcomes[cli_inspect] = ProcessOutcome(
        command=cli_inspect,
        returncode=0,
        stdout=_image_inspection(
            _CLI_IMAGE_ID,
            entrypoint=("python", "-m", "core.entrypoints.cli.lifecycle_click_app"),
        ),
    )
    process.outcomes[("docker", "image", "inspect", tag)] = process.outcomes[cli_inspect]
    return gateway, process


def _prepare(gateway, operation="compact-management"):
    log = gateway.state_store.open_log(operation)
    try:
        gateway.prepare_core(_CONFIG, log=log)
    finally:
        log.close()


def test_source_compact_prepares_separate_immutable_management_image(tmp_path, monkeypatch):
    gateway, process = _source_compact(tmp_path, monkeypatch)

    _prepare(gateway)

    values = gateway.state_store.read_env_values(tmp_path / ".env.prod")
    assert values["FS_CLI_IMAGE"] == _CLI_IMAGE_ID
    assert values["FS_CLI_IMAGE_REVISION"] == _FULL_REVISION
    assert values["FS_CLI_IMAGE_DIGEST"] == _CLI_IMAGE_ID
    build = next(
        command for command in process.commands if command[:3] == ("docker", "buildx", "build")
    )
    assert build[build.index("--target") + 1] == "cli"
    assert f"FS_BUILD_REVISION={_FULL_REVISION}" in build
    assert "--load" in build
    assert values["FS_COMPACT_IMAGE"] == _COMPACT_IMAGE


def test_source_compact_without_management_image_does_not_reuse_prepare(tmp_path, monkeypatch):
    gateway, _process = _source_compact(tmp_path, monkeypatch)
    log = gateway.state_store.open_log("existing-compact-provenance")
    try:
        gateway._prepare_exact_prebuilt_provenance(_CONFIG, log=log)
    finally:
        log.close()

    assert gateway.postcondition_holds("prepare", _CONFIG) is False


def test_source_compact_reuses_its_available_management_image(tmp_path, monkeypatch):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    _prepare(gateway)
    process.commands.clear()

    _prepare(gateway, "compact-management-reuse")

    assert not any(command[:3] == ("docker", "buildx", "build") for command in process.commands)
    assert gateway.postcondition_holds("prepare", _CONFIG) is True


def test_source_compact_rebuilds_its_lost_management_image(tmp_path, monkeypatch):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    _prepare(gateway)
    process.commands.clear()
    command = ("docker", "image", "inspect", _CLI_IMAGE_ID)
    # The first inventory read sees the missing image; rebuilding restores it.
    process.queued_outcomes[command] = [
        ProcessOutcome(command=command, returncode=1),
        process.outcomes[command],
    ]

    _prepare(gateway, "compact-management-recover")

    assert any(command[:3] == ("docker", "buildx", "build") for command in process.commands)
    assert (
        gateway.state_store.read_env_values(tmp_path / ".env.prod")["FS_CLI_IMAGE"] == _CLI_IMAGE_ID
    )


def test_source_compact_rebuilds_owned_management_image_after_checkout_changes(
    tmp_path, monkeypatch
):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    _prepare(gateway)
    process.commands.clear()
    previous = "9" * 40
    record = gateway.state_store.root / "compact-cli-image.json"
    owned = json.loads(record.read_text(encoding="utf-8"))
    owned["revision"] = previous
    record.write_text(json.dumps(owned), encoding="utf-8")
    gateway.state_store.update_env_values(
        tmp_path / ".env.prod",
        {"FS_CLI_IMAGE_REVISION": previous, "FS_CLI_IMAGE_DIGEST": "sha256:" + "9" * 64},
    )

    _prepare(gateway, "compact-management-update")

    values = gateway.state_store.read_env_values(tmp_path / ".env.prod")
    assert values["FS_CLI_IMAGE_REVISION"] == _FULL_REVISION
    assert values["FS_CLI_IMAGE_DIGEST"] == _CLI_IMAGE_ID
    assert any(command[:3] == ("docker", "buildx", "build") for command in process.commands)


@pytest.mark.parametrize(
    "record_state", ["absent", "corrupt", "wrong-image", "wrong-project", "wrong-revision"]
)
def test_source_compact_preserves_selected_management_image_without_owned_record(
    tmp_path, monkeypatch, record_state
):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    gateway.state_store.update_env_values(
        tmp_path / ".env.prod",
        {"FS_CLI_IMAGE": _CLI_IMAGE_ID, "FS_CLI_IMAGE_REVISION": _FULL_REVISION},
    )
    record = gateway.state_store.root / "compact-cli-image.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    if record_state == "corrupt":
        record.write_text("{", encoding="utf-8")
    elif record_state != "absent":
        record.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "image_id": "sha256:" + "8" * 64
                    if record_state == "wrong-image"
                    else _CLI_IMAGE_ID,
                    "revision": "9" * 40 if record_state == "wrong-revision" else _FULL_REVISION,
                    "project_identity": "foreign-project"
                    if record_state == "wrong-project"
                    else gateway._project_identity(),
                }
            ),
            encoding="utf-8",
        )

    _prepare(gateway)

    assert (
        gateway.state_store.read_env_values(tmp_path / ".env.prod")["FS_CLI_IMAGE"] == _CLI_IMAGE_ID
    )
    assert not any(command[:3] == ("docker", "buildx", "build") for command in process.commands)


def test_source_compact_with_stale_management_revision_does_not_reuse_prepare(
    tmp_path, monkeypatch
):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    _prepare(gateway)
    previous = "9" * 40
    gateway.state_store.update_env_values(
        tmp_path / ".env.prod", {"FS_CLI_IMAGE_REVISION": previous}
    )
    command = ("docker", "image", "inspect", _CLI_IMAGE_ID)
    process.outcomes[command] = ProcessOutcome(
        command=command,
        returncode=0,
        stdout=_image_inspection(
            _CLI_IMAGE_ID,
            previous,
            entrypoint=("python", "-m", "core.entrypoints.cli.lifecycle_click_app"),
        ),
    )

    assert gateway.postcondition_holds("prepare", _CONFIG) is False


def test_source_compact_explicit_selection_overrides_matching_owned_record(tmp_path, monkeypatch):
    gateway, process = _source_compact(tmp_path, monkeypatch)
    _prepare(gateway)
    process.commands.clear()
    gateway._environment["FS_CLI_IMAGE"] = _CLI_IMAGE_ID
    command = ("docker", "image", "inspect", "--format", "{{.Id}}", _CLI_IMAGE_ID)
    process.outcomes[command] = ProcessOutcome(command=command, returncode=1)

    with pytest.raises(InstallerFailure, match="not present locally"):
        _prepare(gateway, "compact-management-explicit")

    assert not any(command[:3] == ("docker", "buildx", "build") for command in process.commands)


@pytest.mark.parametrize("replacement", [False, True])
def test_management_selection_is_not_published_when_ownership_write_fails(
    tmp_path, monkeypatch, replacement
):
    gateway, _process = _source_compact(tmp_path, monkeypatch)
    if replacement:
        _prepare(gateway)
        previous = "9" * 40
        record_path = gateway.state_store.root / "compact-cli-image.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["revision"] = previous
        record_path.write_text(json.dumps(record), encoding="utf-8")
        gateway.state_store.update_env_values(
            tmp_path / ".env.prod", {"FS_CLI_IMAGE_REVISION": previous}
        )
    before = gateway.state_store.read_env_values(tmp_path / ".env.prod")

    def fail_ownership_write(_path, _content):
        raise OSError("simulated atomic ownership write failure")

    monkeypatch.setattr(gateway.state_store, "write_owner_only_text", fail_ownership_write)
    with pytest.raises(OSError, match="simulated atomic ownership"):
        _prepare(gateway, "interrupted-management-selection")

    after = gateway.state_store.read_env_values(tmp_path / ".env.prod")
    assert after.get("FS_CLI_IMAGE") == before.get("FS_CLI_IMAGE")
    assert after.get("FS_CLI_IMAGE_REVISION") == before.get("FS_CLI_IMAGE_REVISION")


@pytest.mark.parametrize("replacement", [False, True])
def test_management_publication_resumes_after_environment_write_failure(
    tmp_path, monkeypatch, replacement
):
    gateway, _process = _source_compact(tmp_path, monkeypatch)
    previous = "9" * 40
    if replacement:
        _prepare(gateway)
        record_path = gateway.state_store.root / "compact-cli-image.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["revision"] = previous
        record_path.write_text(json.dumps(record), encoding="utf-8")
        gateway.state_store.update_env_values(
            tmp_path / ".env.prod", {"FS_CLI_IMAGE_REVISION": previous}
        )
    update = gateway.state_store.update_env_values

    def fail_selection_write(path, values):
        if "FS_CLI_IMAGE" in values:
            raise OSError("simulated selection write failure")
        return update(path, values)

    monkeypatch.setattr(gateway.state_store, "update_env_values", fail_selection_write)
    with pytest.raises(OSError, match="simulated selection"):
        _prepare(gateway, "interrupted-management-environment")
    if replacement:
        values = gateway.state_store.read_env_values(tmp_path / ".env.prod")
        assert gateway._compact_cli_owned_revision(values) == previous

    monkeypatch.setattr(gateway.state_store, "update_env_values", update)
    _prepare(gateway, "resume-management-environment")
    values = gateway.state_store.read_env_values(tmp_path / ".env.prod")
    assert values["FS_CLI_IMAGE"] == _CLI_IMAGE_ID
    assert values["FS_CLI_IMAGE_REVISION"] == _FULL_REVISION
    assert gateway._compact_cli_owned_revision(values) == _FULL_REVISION
