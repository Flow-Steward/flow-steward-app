"""A scan that cannot run must not hand back the text it was asked to clean.

Found while fixing AC17-R155: every scanner fell through to `except Exception: return
the input unchanged` on anything but a policy-parse error -- in enforce mode too, which
is the production default. (The policy loaders already fall back to the default policy
when the database is unavailable; this path is any other failure inside the scan --
the engine, a detector, a policy merge that raises something unexpected.) Each such
failure turned redaction off for display, persistence and delivery. The
failure is injected here at the policy load, the first thing every scan does.

The rule now: a surface that redacts what leaves (outbound, tool output) withholds
the text on any failure -- blocking in enforce mode, redacting to a placeholder in
monitor mode, which never blocks but always redacts. A surface that only detects
(inbound, tool input) treats an unexpected failure as it already treated a policy
error: blocked in enforce mode, passed in monitor mode unless the fail-closed flag is on.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.infrastructure.security import integration
from core.infrastructure.security.prompt_safety import redact_text_for_persistence

_SECRET = "AKIAIOSFODNN7EXAMPLE"
_TEXT = f"deploy key {_SECRET} for the release"

_REDACTING = {
    "scan_outbound": "outbound",
    "scan_tool_output": "tool output",
}
_DETECTING = {"scan_inbound": "inbound", "scan_tool_input": "tool input"}


def _scan_breaks(*_args, **_kwargs):
    raise RuntimeError("detector crashed")


@pytest.fixture
def failing_policy_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration, "security_enabled", lambda: True)
    monkeypatch.setattr(integration, "load_global_policy", _scan_breaks)


def test_every_scanner_is_classified() -> None:
    """Derived from the module: a new scanner cannot join without a failure rule."""
    scanners = {name for name in dir(integration) if name.startswith("scan_")}
    assert scanners == set(_REDACTING) | set(_DETECTING)


@pytest.mark.parametrize("enforce", [True, False], ids=["enforce", "monitor"])
@pytest.mark.parametrize("scanner", sorted(_REDACTING))
def test_a_redacting_scan_that_fails_withholds_the_text(
    failing_policy_store: None, scanner: str, enforce: bool
) -> None:
    with (
        patch.object(integration, "enforcement_active", return_value=enforce),
        patch.object(integration, "policy_error_fail_closed", return_value=False),
    ):
        decision = getattr(integration, scanner)(_TEXT)

    assert _SECRET not in str(decision.redacted_text)
    assert decision.action == ("block" if enforce else "redact")


@pytest.mark.parametrize("scanner", sorted(_DETECTING))
def test_a_detecting_scan_that_fails_blocks_in_enforce_mode(
    failing_policy_store: None, scanner: str
) -> None:
    with patch.object(integration, "enforcement_active", return_value=True):
        decision = getattr(integration, scanner)(_TEXT)

    assert decision.action == "block"
    assert _SECRET not in str(decision.redacted_text)


@pytest.mark.parametrize("scanner", sorted(_DETECTING))
def test_a_detecting_scan_that_fails_still_passes_in_monitor_mode(
    failing_policy_store: None, scanner: str
) -> None:
    """The green direction: monitor mode keeps its contract for detection."""
    with (
        patch.object(integration, "enforcement_active", return_value=False),
        patch.object(integration, "policy_error_fail_closed", return_value=False),
    ):
        decision = getattr(integration, scanner)(_TEXT)

    assert decision.action == "allow"
    assert decision.redacted_text == _TEXT


def test_a_scan_that_works_still_redacts_rather_than_withholding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-vacuity: with a policy the secret is redacted and the rest of the text kept."""
    monkeypatch.setattr(integration, "security_enabled", lambda: True)
    with patch.object(integration, "enforcement_active", return_value=False):
        decision = integration.scan_outbound(_TEXT)

    assert _SECRET not in str(decision.redacted_text)
    assert "for the release" in str(decision.redacted_text)


def test_persistence_redaction_withholds_the_text_when_the_scan_raises() -> None:
    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        side_effect=RuntimeError("scan failed"),
    ):
        value = redact_text_for_persistence(f"{_TEXT} db=postgres://admin:secret@db/app")

    assert _SECRET not in value
    assert "admin:secret" not in value
