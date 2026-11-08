"""Host Compact configuration uses the installed one-shot management role."""

from pathlib import Path

import pytest

from tests.unit.cli_platform.test_launcher import (
    _arg_sequence,
    _count_action,
    _log,
    _run_launcher,
    _write_compact_installation,
)
from tests.unit.cli_platform.test_launcher import (
    fake_repo as fake_repo,
)


@pytest.mark.parametrize(
    "arguments",
    (
        ("config", "show", "--json"),
        ("config", "--help"),
        ("config", "validate", "--json"),
        (
            "config",
            "set",
            "--public-host",
            "fs.xenonled.eu",
            "--public-port",
            "443",
            "--proxy-kind",
            "apache",
            "--non-interactive",
        ),
        ("--plain", "config", "apply", "--confirm", "--non-interactive"),
    ),
    ids=("show", "group-help", "validate", "set-public-routing", "apply-confirmed"),
)
def test_compact_config_uses_installed_one_shot_management_image(
    fake_repo: tuple[Path, Path], arguments: tuple[str, ...]
) -> None:
    image = f"sha256:{'c' * 64}"
    _write_compact_installation(fake_repo, image=image)

    result = _run_launcher(
        fake_repo,
        *arguments,
        env={"FAKE_IMAGE_ID": image, "FAKE_IMAGE_REVISION": "test-revision"},
    )

    assert result.returncode == 0, result.stderr
    log = _log(fake_repo)
    assert f"ENV_FS_CLI_IMAGE={image}" in log
    assert _count_action(fake_repo, "run") == 1
    for action in ("exec", "start", "up", "build"):
        assert _count_action(fake_repo, action) == 0
    assert "CMD=container inspect" not in log
    # Original public arguments remain intact; canonical Click owns their parsing.
    assert _arg_sequence(*arguments) in log


def test_compact_stack_remains_unavailable(fake_repo: tuple[Path, Path]) -> None:
    _write_compact_installation(fake_repo, image=f"sha256:{'c' * 64}")

    result = _run_launcher(fake_repo, "stack", "recreate", "--confirm")

    assert result.returncode == 2
    assert "Unsupported Compact platform command" in result.stderr
    assert not _log(fake_repo)
