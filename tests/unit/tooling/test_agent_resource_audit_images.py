from __future__ import annotations

import pytest

from core.infrastructure.agent_resources.docker import DockerResources


@pytest.mark.parametrize("matching", [True, False])
def test_selects_only_an_immutable_image_matching_canonical_toolchain(
    tmp_path, monkeypatch, matching
):
    from core.infrastructure.agent_resources.audit_images import select

    wanted = "a" * 40
    identity = "sha256:" + "b" * 64
    script = tmp_path / "lint.sh"
    script.write_text("#!/bin/bash\nprintf '%s' " + wanted + "\n")
    docker = DockerResources()
    calls = []

    def command(*args):
        calls.append(args)
        return identity

    monkeypatch.setattr(docker, "command", command)
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda *args: {
            "Id": identity,
            "Config": {
                "Labels": {"org.flowsteward.audit-toolchain": wanted if matching else "stale"}
            },
        },
    )
    assert select(script, docker) == (identity if matching else None)
    assert calls == [
        (
            "image",
            "ls",
            "-q",
            "--no-trunc",
            "--filter",
            "label=org.flowsteward.audit-toolchain=" + wanted,
        )
    ]


def test_selection_failure_never_silently_authorizes_a_cold_build(tmp_path, monkeypatch):
    from core.infrastructure.agent_resources.audit_images import select

    script = tmp_path / "lint.sh"
    script.write_text("#!/bin/bash\nprintf invalid\n")
    docker = DockerResources()
    monkeypatch.setattr(
        docker, "command", lambda *args: pytest.fail("invalid toolchain must stop before Docker")
    )
    with pytest.raises(ValueError, match="toolchain"):
        select(script, docker)
