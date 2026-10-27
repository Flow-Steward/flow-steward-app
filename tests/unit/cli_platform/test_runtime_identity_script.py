"""Closed-contract tests for the non-secret CLI runtime identity helper."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "core/infrastructure/cli_runtime/runtime_identity.sh"
EXPECTED_DIGEST_WITHOUT_ENV_FILE = (
    "45cc8b65b68d13f99c1d29aaf9e352d293a09363f397ec13d5730a9da2f4ed14"  # pragma: allowlist secret
)
FIELDS = {
    "compose-project": "flow-steward-cli",
    "stack-environment": "production",
    "active-revision": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "compose-files": (
        "/workspace/infra/docker/docker-compose.yaml:"
        "/workspace/infra/docker/docker-compose.prod.yaml"
    ),
    "env-file": "/workspace/.env.prod",
    "host-project-root": "/workspace",
    "host-uid": "501",
    "host-gid": "20",
    "docker-gid": "20",
}


def _run_identity(
    fields: Mapping[str, str] = FIELDS,
    *extra_args: str,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    assert SCRIPT.is_file(), "runtime identity helper is missing"
    arguments = ["sh", str(SCRIPT)]
    for name, value in fields.items():
        arguments.extend((f"--{name}", value))
    arguments.extend(extra_args)
    return subprocess.run(
        arguments,
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, **(environment or {})},
    )


@pytest.fixture
def fake_secret_env_file(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.env"
    path.write_text(
        "FS_SECRET_KEY=fake-fixture-secret\nDATABASE_URL=postgresql://fake:secret@db/test\n",
        encoding="utf-8",
    )
    return path


def test_runtime_identity_hashes_the_fixed_non_secret_descriptor() -> None:
    """Changing any descriptor field must change the reusable-container identity."""
    completed = _run_identity({**FIELDS, "env-file": "absent"})

    assert completed.returncode == 0
    assert completed.stdout == f"{EXPECTED_DIGEST_WITHOUT_ENV_FILE}\n"
    assert completed.stderr == ""


def test_runtime_identity_hashes_env_contents_without_printing_fake_secrets(
    fake_secret_env_file: Path,
) -> None:
    """Secret values in the selected env file must not influence or leak into the digest."""
    fields = {**FIELDS, "env-file": str(fake_secret_env_file)}
    first = _run_identity(fields)
    fake_secret_env_file.write_text(
        "FS_SECRET_KEY=changed-fake-fixture-secret\nDATABASE_URL=postgresql://other:secret@db/test\n",
        encoding="utf-8",
    )
    second = _run_identity(fields)

    assert first.returncode == second.returncode == 0
    assert first.stdout != second.stdout
    assert "fake-fixture-secret" not in first.stdout + first.stderr + second.stdout + second.stderr
    assert "postgresql://" not in first.stdout + first.stderr + second.stdout + second.stderr


def test_runtime_identity_accepts_validated_env_digest_without_reading_owner_file(
    fake_secret_env_file: Path,
) -> None:
    fields = {**FIELDS, "env-file": str(fake_secret_env_file)}
    expected = _run_identity(fields)
    digest = hashlib.sha256(fake_secret_env_file.read_bytes()).hexdigest()
    fake_secret_env_file.chmod(0)

    overridden = _run_identity(
        fields,
        "--env-file-digest-from-environment",
        environment={"FS_CLI_ENV_FILE_DIGEST": digest},
    )

    assert overridden.returncode == 0, overridden.stderr
    assert overridden.stdout == expected.stdout
    assert digest not in overridden.stdout + overridden.stderr


@pytest.mark.parametrize("digest", ("", "not-a-digest", "A" * 64, "a" * 63))
def test_runtime_identity_rejects_invalid_environment_digest(
    fake_secret_env_file: Path,
    digest: str,
) -> None:
    completed = _run_identity(
        {**FIELDS, "env-file": str(fake_secret_env_file)},
        "--env-file-digest-from-environment",
        environment={"FS_CLI_ENV_FILE_DIGEST": digest},
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    if digest:
        assert digest not in completed.stderr


@pytest.mark.parametrize(
    ("fields", "extra_args", "rejected_value"),
    [
        ({key: value for key, value in FIELDS.items() if key != "docker-gid"}, (), ""),
        (FIELDS, ("--unknown", "value"), "value"),
        ({**FIELDS, "compose-project": "flow-steward\ninjected"}, (), "flow-steward\ninjected"),
        ({**FIELDS, "host-uid": "not-a-number"}, (), "not-a-number"),
        ({**FIELDS, "host-gid": "not-a-number"}, (), "not-a-number"),
        ({**FIELDS, "compose-project": "flow/steward"}, (), "flow/steward"),
        ({**FIELDS, "active-revision": "not-a-sha256"}, (), "not-a-sha256"),
        (
            {**FIELDS, "compose-project": ""},
            ("--compose-project", "flow-steward-cli"),
            "flow-steward-cli",
        ),
    ],
    ids=(
        "missing-field",
        "unknown-flag",
        "newline-injection",
        "non-numeric-uid",
        "non-numeric-gid",
        "unsafe-compose-project",
        "malformed-active-revision",
        "empty-then-duplicate-required-option",
    ),
)
def test_runtime_identity_rejects_invalid_descriptor_inputs(
    fields: Mapping[str, str],
    extra_args: tuple[str, ...],
    rejected_value: str,
) -> None:
    """Malformed identity input must fail without reflecting untrusted values."""
    completed = _run_identity(fields, *extra_args)

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr
    if rejected_value:
        assert rejected_value not in completed.stderr
