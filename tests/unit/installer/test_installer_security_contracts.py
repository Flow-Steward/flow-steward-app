from __future__ import annotations

from pathlib import Path

from core.application.installer.redaction import redact_installer_text


def test_installer_redaction_covers_headers_sessions_and_bare_bearer() -> None:
    text = (
        "Cookie: session=abc; Authorization: Basic xyz; "
        "session_token=session-secret; Bearer bearer-secret; credential=cred-secret"
    )

    redacted = redact_installer_text(text)

    for secret in ("abc", "xyz", "session-secret", "bearer-secret", "cred-secret"):
        assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_launcher_does_not_persist_raw_docker_info() -> None:
    launcher = Path("install.sh").read_text(encoding="utf-8")

    assert 'docker info >"${BOOTSTRAP_LOG}"' not in launcher
    assert "docker info --format" in launcher
