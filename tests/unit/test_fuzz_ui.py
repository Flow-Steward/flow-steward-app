from __future__ import annotations

import json
import subprocess
from pathlib import Path

import requests

from core.application.tooling.testing import fuzz_ui


class _FakeCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


def test_lighthouse_accessibility_uses_extended_timeout_budget(monkeypatch):
    seen: dict[str, float] = {}

    def fake_run(cmd: list[str], **kwargs) -> _FakeCompleted:
        seen["timeout"] = float(kwargs["timeout"])
        report_path = Path(cmd[cmd.index("--output-path") + 1])
        report_path.write_text(
            json.dumps({"categories": {"accessibility": {"score": 1.0}}}),
            encoding="utf-8",
        )
        return _FakeCompleted()

    monkeypatch.setattr(fuzz_ui.shutil, "which", lambda _name: "/usr/bin/npx")
    monkeypatch.setattr(fuzz_ui, "_resolve_lighthouse_chrome_path", lambda: "/tmp/chrome")
    monkeypatch.setattr(fuzz_ui.subprocess, "run", fake_run)

    result = fuzz_ui._check_lighthouse_accessibility(
        session=requests.Session(),
        base_url="http://example.test",
        routes=["/login"],
        threshold=0.9,
        timeout=20.0,
        lighthouse_timeout=45.0,
    )

    assert result.status == "pass"
    assert seen["timeout"] == 60.0


def test_lighthouse_accessibility_reports_timeout_as_warning(monkeypatch):
    def fake_run(cmd: list[str], **kwargs) -> _FakeCompleted:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(fuzz_ui.shutil, "which", lambda _name: "/usr/bin/npx")
    monkeypatch.setattr(fuzz_ui, "_resolve_lighthouse_chrome_path", lambda: "/tmp/chrome")
    monkeypatch.setattr(fuzz_ui.subprocess, "run", fake_run)

    result = fuzz_ui._check_lighthouse_accessibility(
        session=requests.Session(),
        base_url="http://example.test",
        routes=["/login"],
        threshold=0.9,
        timeout=20.0,
        lighthouse_timeout=45.0,
    )

    assert result.status == "warn"
    assert "timed out after 60.0s" in result.detail
