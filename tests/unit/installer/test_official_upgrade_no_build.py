"""Official upgrade management images remain immutable across CLI repair."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application.installer.errors import InstallerFailure
from core.infrastructure.installer.process import ProcessOutcome
from tests.unit.installer.test_installer_gateway import (
    _CLI_IMAGE_ID,
    _CLI_REVISION,
    _configure_runtime_contract,
    _runtime_inspection,
)


@pytest.mark.parametrize("image_available", (True, False))
def test_official_digest_cli_repair_never_builds_from_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image_available: bool
) -> None:
    selected = f"registry.example/flow-steward-cli@sha256:{'d' * 64}"
    gateway, process, config, _helper, ps, inspect = _configure_runtime_contract(
        tmp_path, monkeypatch, environment="production", cli_image=selected
    )
    gateway.state_store.update_env_values(
        tmp_path / ".env.prod", {"FS_CLI_IMAGE_REVISION": _CLI_REVISION}
    )
    revision_command = (
        "docker",
        "image",
        "inspect",
        selected,
        "--format",
        '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
    )
    process.outcomes[revision_command] = ProcessOutcome(
        revision_command,
        0 if image_available else 1,
        stdout=_CLI_REVISION + "\n" if image_available else "",
    )
    id_command = ("docker", "image", "inspect", selected, "--format", "{{.Id}}")
    process.outcomes[id_command] = ProcessOutcome(id_command, 0, stdout=_CLI_IMAGE_ID + "\n")
    process.queued_outcomes[ps] = [ProcessOutcome(ps, 0, stdout=""), process.outcomes[ps]]
    process.outcomes[inspect] = ProcessOutcome(
        inspect,
        0,
        stdout=_runtime_inspection(
            gateway,
            image_id=_CLI_IMAGE_ID,
            revision="a" * 64,
            state_runtime_root=gateway.state_store.root / "runtime",
            candidate_state_dir=gateway.state_store.root / "candidate-state",
            candidate_prod_env=tmp_path / ".env.prod",
        ),
    )
    log = gateway.state_store.open_log("official-digest-cli-repair")
    try:
        if image_available:
            gateway.ensure_cli_runtime(config, log=log)
        else:
            with pytest.raises(InstallerFailure) as refused:
                gateway.ensure_cli_runtime(config, log=log)
            assert refused.value.code == "cli_runtime_unavailable"
    finally:
        log.close()

    assert not any("build" in command for command in process.commands)
    assert not any(command[0] == "git" for command in process.commands)
    starts = [command for command in process.commands if "up" in command]
    assert bool(starts) is image_available
    for command in starts:
        assert "--no-build" in command
        assert command[command.index("--pull") + 1] == "never"


def test_upgrade_handoff_passes_every_official_image_without_shell_bootstrap(
    tmp_path: Path,
) -> None:
    from core.application.release.images import all_release_roles
    from tests.unit.installer.test_release_handoff import (
        FakeGit,
        _handoff,
        _release,
        _successful_child,
    )

    target = _release()
    observed = {}

    def child(command, environment, _timeout):
        observed["command"] = tuple(command)
        observed["environment"] = dict(environment)
        state.publish_installed_release(target)
        return _successful_child(command)

    handoff, state = _handoff(tmp_path, git=FakeGit(target), installer=child)
    result = handoff.activate(target, timeout_seconds=60)

    assert result.status == "upgraded"
    assert Path(observed["command"][1]).name == "release-handoff-bootstrap.py"
    assert "install.sh" not in " ".join(observed["command"])
    role_variables = {role.role: role.environment_variable for role in all_release_roles()}
    for image in target.images:
        assert observed["environment"][role_variables[image.role]] == image.registry_ref


@pytest.mark.parametrize("durable_state", ("pending", "installed", "handoff"))
def test_official_upgrade_refuses_mutable_management_image_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durable_state: str
) -> None:
    from core.application.release.activation import ReleaseHandoffJournal
    from tests.unit.installer.test_release_handoff import _release

    gateway, process, config, *_ = _configure_runtime_contract(
        tmp_path, monkeypatch, environment="production", cli_image="flow-steward-cli:mutable"
    )
    target = _release()
    if durable_state == "pending":
        gateway.state_store.stage_pending_release(target)
    elif durable_state == "installed":
        gateway.state_store.publish_installed_release(target)
    else:
        gateway.state_store.save_release_handoff(
            ReleaseHandoffJournal(target=target, phase="checkout_applied")
        )
    log = gateway.state_store.open_log("official-mutable-cli-refused")
    try:
        with pytest.raises(InstallerFailure) as refused:
            gateway.ensure_cli_runtime(config, log=log)
        assert refused.value.code == "cli_runtime_unavailable"
        assert "immutable" in refused.value.cause.lower()
    finally:
        log.close()
    assert not any("build" in command or "up" in command for command in process.commands)
