from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application.tooling.db.restore_parity_check import (
    capture_restore_parity_metadata,
    verify_restore_parity_metadata,
)
from tests._disposable_db import DisposableDatabase


@pytest.mark.integration
def test_capture_then_verify_works_against_clean_canonical_schema(
    disposable_db: DisposableDatabase,
    tmp_path: Path,
) -> None:
    dsn = disposable_db.dsn
    baseline_path = tmp_path / "restore-parity-baseline.json"

    captured = capture_restore_parity_metadata(
        dsn=dsn,
        label="clean-1.0-baseline",
        output=baseline_path,
    )
    verified = verify_restore_parity_metadata(
        dsn=dsn,
        label="clean-1.0-restored",
        baseline=baseline_path,
    )

    assert baseline_path.exists()
    assert json.loads(baseline_path.read_text(encoding="utf-8")) == captured["snapshot"]
    assert verified["passed"] is True
    assert verified["mismatches"] == []
