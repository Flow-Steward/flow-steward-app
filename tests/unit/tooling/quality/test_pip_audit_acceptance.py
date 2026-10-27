from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from core.application.tooling.quality import pip_audit_acceptance
from core.application.tooling.quality.pip_audit_acceptance import (
    AcceptanceFormatError,
    evaluate,
    load_acceptances,
    main,
)


def _write_acceptance(path: Path, *, expires: str = "2026-10-14") -> None:
    path.write_text(
        "\n".join(
            (
                "# pip-audit Risk Acceptance",
                "",
                "## Accepted Vulnerabilities",
                "",
                "### pyjwt / GHSA-example-1234",
                "",
                "- Audit label: semgrep",
                f"- Accepted until: {expires}",
                "- Scope: isolated Semgrep audit-tool environment only",
            )
        ),
        encoding="utf-8",
    )


def _write_report(path: Path, advisory: str = "GHSA-example-1234") -> None:
    path.write_text(
        json.dumps(
            {
                "dependencies": [
                    {"name": "PyJWT", "vulns": [{"id": advisory}]},
                ]
            }
        ),
        encoding="utf-8",
    )


def test_acceptance_matches_exact_label_package_and_advisory(tmp_path: Path) -> None:
    acceptance = tmp_path / "acceptance.md"
    report = tmp_path / "report.json"
    _write_acceptance(acceptance)
    _write_report(report)

    accepted, unaccepted = evaluate(
        acceptance,
        [f"semgrep:{report}"],
        today=date(2026, 10, 14),
    )

    assert [(item.label, item.package, item.advisory) for item in accepted] == [
        ("semgrep", "PyJWT", "GHSA-example-1234")
    ]
    assert unaccepted == []


def test_acceptance_does_not_apply_to_another_audit_label(tmp_path: Path) -> None:
    acceptance = tmp_path / "acceptance.md"
    report = tmp_path / "report.json"
    _write_acceptance(acceptance)
    _write_report(report)

    accepted, unaccepted = evaluate(
        acceptance,
        [f"runtime+audit:{report}"],
        today=date(2026, 10, 1),
    )

    assert accepted == []
    assert [item.label for item in unaccepted] == ["runtime+audit"]


def test_acceptance_is_rejected_after_expiry(tmp_path: Path) -> None:
    acceptance = tmp_path / "acceptance.md"
    report = tmp_path / "report.json"
    _write_acceptance(acceptance)
    _write_report(report)

    accepted, unaccepted = evaluate(
        acceptance,
        [f"semgrep:{report}"],
        today=date(2026, 10, 15),
    )

    assert accepted == []
    assert [item.advisory for item in unaccepted] == ["GHSA-example-1234"]


@pytest.mark.parametrize(
    ("instant", "accepted_count"),
    [
        (datetime(2026, 10, 14, 23, 59, 59, tzinfo=UTC), 1),
        (datetime(2026, 10, 15, 0, 0, 0, tzinfo=UTC), 0),
    ],
)
def test_default_evaluation_uses_utc_midnight(
    tmp_path: Path, monkeypatch, instant: datetime, accepted_count: int
) -> None:
    class ControlledClock:
        @staticmethod
        def now(tz):
            assert tz is UTC
            return instant

    monkeypatch.setattr(pip_audit_acceptance, "datetime", ControlledClock)
    acceptance = tmp_path / "acceptance.md"
    report = tmp_path / "report.json"
    _write_acceptance(acceptance)
    _write_report(report)

    assert pip_audit_acceptance.acceptance_evaluation_date() == instant.date()
    accepted, unaccepted = evaluate(acceptance, [f"semgrep:{report}"])
    assert len(accepted) == accepted_count
    assert len(unaccepted) == 1 - accepted_count
    # Explicit date injection remains independent of the default clock.
    accepted, unaccepted = evaluate(acceptance, [f"semgrep:{report}"], today=date(2026, 10, 14))
    assert len(accepted) == 1
    assert unaccepted == []


@pytest.mark.parametrize(
    "metadata",
    [
        "- Accepted until: 2026-10-14",
        "- Audit label: semgrep",
        "- Audit label: semgrep\n- Accepted until: someday",
    ],
)
def test_malformed_acceptance_fails_closed(tmp_path: Path, metadata: str) -> None:
    acceptance = tmp_path / "acceptance.md"
    acceptance.write_text(
        f"## Accepted Vulnerabilities\n\n### pyjwt / GHSA-example-1234\n\n{metadata}\n",
        encoding="utf-8",
    )

    current_date = date(2026, 10, 1)
    with pytest.raises(AcceptanceFormatError):
        load_acceptances(acceptance, today=current_date)


def test_acceptance_does_not_combine_package_and_advisory_across_sections(
    tmp_path: Path,
) -> None:
    acceptance = tmp_path / "acceptance.md"
    report = tmp_path / "report.json"
    acceptance.write_text(
        """## Accepted Vulnerabilities

### pyjwt / GHSA-other-1234

- Audit label: semgrep
- Accepted until: 2026-10-14

### another-package / GHSA-example-1234

- Audit label: semgrep
- Accepted until: 2026-10-14
""",
        encoding="utf-8",
    )
    _write_report(report)

    accepted, unaccepted = evaluate(
        acceptance,
        [f"semgrep:{report}"],
        today=date(2026, 10, 1),
    )

    assert accepted == []
    assert len(unaccepted) == 1


def test_main_reports_accepted_finding(tmp_path: Path, capsys) -> None:
    acceptance = tmp_path / "acceptance.md"
    report = tmp_path / "report.json"
    _write_acceptance(acceptance, expires="2099-10-14")
    _write_report(report)

    assert main([str(acceptance), f"semgrep:{report}"]) == 0
    assert "all vulnerabilities are covered" in capsys.readouterr().out


def test_main_rejects_finding_when_acceptance_file_is_missing(tmp_path: Path, capsys) -> None:
    acceptance = tmp_path / "missing.md"
    report = tmp_path / "report.json"
    _write_report(report)

    assert main([str(acceptance), f"semgrep:{report}"]) == 1
    output = capsys.readouterr().out
    assert "unaccepted vulnerabilities" in output
    assert "Acceptance file not found" in output


def test_main_accepts_empty_report(tmp_path: Path, capsys) -> None:
    report = tmp_path / "report.json"
    report.write_text('{"dependencies": []}', encoding="utf-8")

    assert main([str(tmp_path / "missing.md"), f"semgrep:{report}"]) == 0
    assert "no known vulnerabilities" in capsys.readouterr().out


def test_main_fails_closed_for_invalid_report(tmp_path: Path, capsys) -> None:
    report = tmp_path / "report.json"
    report.write_text("not-json", encoding="utf-8")

    assert main([str(tmp_path / "acceptance.md"), f"semgrep:{report}"]) == 1
    assert "failed to parse JSON output" in capsys.readouterr().err


def test_main_requires_acceptance_and_report_arguments(capsys) -> None:
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err


def test_acceptance_section_requires_supported_advisory(tmp_path: Path) -> None:
    acceptance = tmp_path / "acceptance.md"
    acceptance.write_text(
        """### pyjwt / internal-ticket-123

- Audit label: semgrep
- Accepted until: 2099-10-14
""",
        encoding="utf-8",
    )

    with pytest.raises(AcceptanceFormatError, match="no supported advisory"):
        load_acceptances(acceptance)
