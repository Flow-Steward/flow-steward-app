"""Owner-accepted npm advisories pass only when named, unexpired and dev-only."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from core.application.tooling.quality import npm_audit_exceptions as audit

ROOT = Path(__file__).resolve().parents[4]
TODAY = date(2026, 11, 17)
PICOMATCH = {
    "advisory": "GHSA-fc5q-39xc-vjpf",
    "package": "picomatch",
    "dev_only": True,
    "expires": "2026-12-31",
}


def _report(*, advisory: str = "GHSA-fc5q-39xc-vjpf", severity: str = "high") -> dict:
    return {
        "vulnerabilities": {
            "picomatch": {
                "name": "picomatch",
                "severity": severity,
                "via": [
                    {
                        "name": "picomatch",
                        "severity": severity,
                        "url": f"https://github.com/advisories/{advisory}",
                    }
                ],
                "nodes": ["node_modules/picomatch"],
            },
            "vite": {"name": "vite", "severity": severity, "via": ["picomatch"], "nodes": []},
        }
    }


def _lock(*, dev: bool = True) -> dict:
    return {"packages": {"node_modules/picomatch": {"version": "4.0.7", "dev": dev}}}


def test_accepted_dev_only_advisory_passes() -> None:
    assert audit.unaccepted_findings(_report(), _lock(), [PICOMATCH], TODAY) == []


def test_another_advisory_still_fails() -> None:
    findings = audit.unaccepted_findings(
        _report(advisory="GHSA-xxxx-yyyy-zzzz"), _lock(), [PICOMATCH], TODAY
    )
    assert findings == ["picomatch: GHSA-xxxx-yyyy-zzzz is not accepted"]


def test_expired_exception_fails() -> None:
    assert audit.unaccepted_findings(_report(), _lock(), [PICOMATCH], date(2027, 1, 1)) == [
        "picomatch: GHSA-fc5q-39xc-vjpf is not accepted"
    ]


def test_production_install_fails_even_when_accepted() -> None:
    assert audit.unaccepted_findings(_report(), _lock(dev=False), [PICOMATCH], TODAY) == [
        "picomatch: GHSA-fc5q-39xc-vjpf is accepted only for dev-only installs"
    ]


def test_exception_must_declare_dev_only() -> None:
    entry = {**PICOMATCH, "dev_only": False}
    assert audit.unaccepted_findings(_report(), _lock(), [entry], TODAY)


def test_unreadable_report_fails() -> None:
    assert audit.unaccepted_findings({}, _lock(), [PICOMATCH], TODAY)


def test_repository_exceptions_are_dev_only_and_expire(tmp_path: Path) -> None:
    entries = json.loads(
        (ROOT / "infra/tooling/quality/npm-audit-exceptions.json").read_text(encoding="utf-8")
    )["exceptions"]
    lock = json.loads((ROOT / "assets/frontend/package-lock.json").read_text(encoding="utf-8"))
    for entry in entries:
        assert entry["dev_only"] is True
        assert date.fromisoformat(entry["expires"]) <= date.fromisoformat(
            entry["accepted_on"]
        ).replace(year=date.fromisoformat(entry["accepted_on"]).year + 1)
        installs = [
            meta
            for path, meta in lock["packages"].items()
            if path.rsplit("node_modules/", 1)[-1] == entry["package"]
        ]
        assert installs and all(meta.get("dev") is True for meta in installs)
