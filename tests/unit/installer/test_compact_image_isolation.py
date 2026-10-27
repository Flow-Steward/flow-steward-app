from __future__ import annotations

import pytest

from core.infrastructure.installer.process import ProcessOutcome
from tests.unit.installer.test_compact_management_image import _source_compact
from tests.unit.installer.test_installer_gateway import (
    _CLI_IMAGE_ID,
    _FULL_REVISION,
    _R141_RUNNING,
    FakeProcessExecutor,
    _compact,
    _currency,
    _gateway,
    _RuntimeExecutor,
    _write_env_values,
)


@pytest.mark.parametrize("project", ("stand-a", "stand-b"))
@pytest.mark.parametrize("role", ("runtime", "cli"))
def test_compact_source_build_tags_are_installation_scoped(tmp_path, monkeypatch, project, role):
    if role == "cli":
        gateway, process = _source_compact(tmp_path, monkeypatch)
        tag = f"flow-steward-cli:compact-{project}-{_FULL_REVISION[:12]}"
        process.outcomes[("docker", "image", "inspect", tag)] = process.outcomes[
            ("docker", "image", "inspect", _CLI_IMAGE_ID)
        ]
    else:
        process = FakeProcessExecutor()
        gateway = _gateway(tmp_path, process, environment={"FS_INSTALL_MODE": "source"})
        old_tag = f"flow-steward-compact:{_FULL_REVISION[:12]}"
        tag = f"flow-steward-compact:{project}-{_FULL_REVISION[:12]}"
        for reference in (old_tag, tag):
            command = ("docker", "image", "inspect", "--format", "{{.Id}}", reference)
            process.outcomes[command] = ProcessOutcome(
                command=command, returncode=0, stdout=_R141_RUNNING + "\n"
            )
    monkeypatch.setattr(gateway, "_project_identity", lambda: project)
    log = gateway.state_store.open_log("installation-scoped-build")
    try:
        if role == "cli":
            gateway._build_compact_management_image(_compact(), log=log)
        else:
            gateway._build_compact_runtime_image(_compact(), log=log)
    finally:
        log.close()

    build = next(c for c in process.commands if c[:3] == ("docker", "buildx", "build"))
    assert build[build.index("--tag") + 1] == tag
    assert f"com.flow-steward.installation={project}" in build


@pytest.mark.parametrize(
    "mode,explicit_pin", (("source", False), ("source", True), ("official", False))
)
def test_missing_same_revision_compact_image_enters_prepare_only_for_source_owner(
    tmp_path, mode, explicit_pin
):
    _write_env_values(
        tmp_path / ".env.prod",
        {
            "FS_INSTALL_MODE": mode,
            "FS_COMPACT_IMAGE": _R141_RUNNING,
            "FS_COMPACT_IMAGE_REVISION": _FULL_REVISION,
        },
    )
    process = _RuntimeExecutor(
        running={"compact-runtime": _R141_RUNNING}, labels={_R141_RUNNING: _FULL_REVISION}
    )
    currency = _currency(
        tmp_path,
        process,
        _compact(),
        environment={"FS_COMPACT_IMAGE": _R141_RUNNING} if explicit_pin else {},
    )

    assert currency.update_required is (mode == "source" and not explicit_pin)
    assert currency.running_revision == _FULL_REVISION
    assert currency.resumes_prepared_update is False
