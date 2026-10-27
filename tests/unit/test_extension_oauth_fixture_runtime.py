from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _invoke(payload: dict) -> subprocess.CompletedProcess[str]:
    entrypoint = Path("tests/fixtures/extensions/fixture_extension_oauth/main.py")
    return subprocess.run(
        [sys.executable, str(entrypoint)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


def test_neutral_fixture_verifies_identity_from_access_token_only_without_echoing_it() -> None:
    token = "neutral-access-token-canary"
    result = _invoke(
        {
            "mode": "action",
            "action": {
                "action_id": "verify_identity",
                "target": {
                    "connection_id": "connection-neutral",
                    "connection": {
                        "connection_id": "connection-neutral",
                        "secrets": {"access_token": token},
                    },
                },
                "context": {"project_id": "project-neutral"},
                "input": {},
            },
        }
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "result": {
            "subject": "neutral-subject",
            "verified_display_name": "Neutral Verified Account",
            "verified_email": "verified@example.test",
            "safe_metadata": {"fixture": "neutral"},
            "health": "ok",
        },
    }
    assert token not in result.stdout
    assert result.stderr == ""


def test_neutral_fixture_rejects_additional_runtime_secrets() -> None:
    result = _invoke(
        {
            "mode": "action",
            "action": {
                "action_id": "verify_identity",
                "target": {
                    "connection": {
                        "secrets": {
                            "access_token": "allowed-token",
                            "refresh_token": "forbidden-refresh-canary",
                        }
                    }
                },
            },
        }
    )

    assert result.returncode != 0
    assert "forbidden-refresh-canary" not in result.stdout
    assert json.loads(result.stdout) == {
        "ok": False,
        "error_code": "invalid_runtime_target",
        "result": {},
    }
