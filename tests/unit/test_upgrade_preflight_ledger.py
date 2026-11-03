"""The applied migrations of a supported source are a prefix of the ordered files (ADR 0056)."""

from __future__ import annotations

import pytest

from core.application.tooling.db.upgrade_preflight import ledger_gaps

ORDERED = [
    "20261101153533_inbound_oauth_tables.sql",
    "20261103090000_retired_agent_type.sql",
    "20261103090000_secret_catalog.sql",
]


@pytest.mark.parametrize(
    ("applied", "gaps"),
    [
        (set(), []),
        ({ORDERED[0]}, []),
        (set(ORDERED[:2]), []),
        (set(ORDERED), []),
        # The stand that ran the catalog file before main's agent-type file existed.
        ({ORDERED[0], ORDERED[2]}, [ORDERED[1]]),
        ({ORDERED[2]}, ORDERED[:2]),
        ({ORDERED[1]}, [ORDERED[0]]),
        # A name this release does not know is reported as newer or pre-release instead.
        ({ORDERED[0], "20991231235959_from_a_newer_release.sql"}, []),
    ],
)
def test_ledger_gaps(applied: set[str], gaps: list[str]) -> None:
    assert ledger_gaps(ORDERED, applied) == gaps


def test_the_full_filename_orders_two_files_with_one_timestamp() -> None:
    assert ledger_gaps(ORDERED, {ORDERED[0], ORDERED[2]}) == [
        "20261103090000_retired_agent_type.sql"
    ]
