"""AC17-R142: the Compact preflight checks the credential the workers actually run with.

The auditor measured the sequence on a live Compact at every install and rerun: the
installer issued a token into .env.prod (revoking the running runtime's), the preflight
checked that token and passed, and some fifteen seconds later the new runtime issued its
own and revoked the installer's. The preflight was green only because it ran inside that
window. It now reads the runtime-owned file first, so the answer does not depend on who
issued last.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.entrypoints.cli.ops import candidate_runtime_preflight as preflight

_RUNTIME_TOKEN = "runtime-issued-worker-token"
_REVOKED_TOKEN = "installer-copy-the-runtime-revoked"


class _IdentityStore:
    conn = None

    def resolve_machine_token(self, token: str) -> dict[str, Any] | None:
        return {"token": token} if token == _RUNTIME_TOKEN else None


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    token_file = tmp_path / "worker-mcp-token"
    environment = {"FS_DB_DSN": "dbname=flow_steward"}
    monkeypatch.setattr(preflight, "_COMPACT_WORKER_TOKEN_PATH", token_file)
    monkeypatch.setattr(preflight, "_runtime_env", environment)
    monkeypatch.setattr(
        preflight, "connect_database", lambda _dsn: type("C", (), {"close": lambda self: None})()
    )
    monkeypatch.setattr(
        preflight, "inspect_migrations", lambda *_a, **_k: {"pending": [], "drifted": []}
    )
    monkeypatch.setattr(preflight, "create_identity_store", lambda _dsn: _IdentityStore())
    monkeypatch.setattr(
        preflight,
        "is_valid_worker_identity",
        lambda principal, **_kwargs: bool(principal),
    )
    # A fake clock for every case, so a refusal is decided at the deadline without
    # waiting for it; the cases about waiting install their own.
    clock = [0.0]

    def _advance(seconds: float) -> None:
        clock[0] += seconds

    fake_time = SimpleNamespace(sleep=_advance, monotonic=lambda: clock[0])
    monkeypatch.setattr(preflight, "time", fake_time, raising=False)
    return {"file": token_file, "env": environment}


def test_compact_checks_the_runtime_owned_credential_not_the_env_copy(
    harness: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    harness["file"].write_text(_RUNTIME_TOKEN + "\n", encoding="utf-8")
    harness["env"]["FS_WORKER_MCP_BEARER_TOKEN"] = _REVOKED_TOKEN

    assert preflight.main() == 0
    assert "preflight passed" in capsys.readouterr().out


def test_a_revoked_runtime_credential_fails_whatever_the_env_holds(
    harness: dict[str, Any],
) -> None:
    harness["file"].write_text(_REVOKED_TOKEN, encoding="utf-8")
    harness["env"]["FS_WORKER_MCP_BEARER_TOKEN"] = _RUNTIME_TOKEN

    assert preflight.main() == 2


def test_a_profile_without_the_runtime_file_still_reads_the_environment(
    harness: dict[str, Any],
) -> None:
    harness["env"]["FS_WORKER_MCP_BEARER_TOKEN"] = _RUNTIME_TOKEN

    assert not harness["file"].exists()
    assert preflight.main() == 0


def test_no_credential_anywhere_is_refused(harness: dict[str, Any]) -> None:
    assert preflight.main() == 2


def test_a_fresh_compact_waits_for_the_runtime_to_issue_its_credential(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured on a fresh local Compact: the runtime container started at 13:00:09.15
    and the preflight ran at 13:00:09.36, before bootstrap had written its credential.
    The file was absent, the environment held only the installer's opaque placeholder,
    and the install failed. Health had not implied bootstrap: verify_health only checks
    that the container is running.
    """
    harness["env"]["FS_WORKER_MCP_BEARER_TOKEN"] = "installer-opaque-placeholder"
    attempts: list[float] = []
    clock = [0.0]

    def sleep(seconds: float) -> None:
        attempts.append(clock[0])
        clock[0] += seconds
        if clock[0] >= 6:  # the runtime's bootstrap finishes a few seconds in
            harness["file"].write_text(_RUNTIME_TOKEN, encoding="utf-8")

    fake_time = SimpleNamespace(sleep=sleep, monotonic=lambda: clock[0])
    monkeypatch.setattr(preflight, "time", fake_time, raising=False)

    assert preflight.main() == 0
    assert attempts  # non-vacuity: it did have to wait


def test_a_credential_that_never_becomes_valid_still_fails_at_the_deadline(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    harness["file"].write_text(_REVOKED_TOKEN, encoding="utf-8")
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    fake_time = SimpleNamespace(sleep=sleep, monotonic=lambda: clock[0])
    monkeypatch.setattr(preflight, "time", fake_time, raising=False)

    assert preflight.main() == 2
    assert clock[0] >= getattr(preflight, "_IDENTITY_WAIT_SECONDS", 1)
