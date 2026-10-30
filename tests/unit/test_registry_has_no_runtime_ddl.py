"""The registry store must not run schema DDL while the app is running.

``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` takes an ACCESS EXCLUSIVE lock even
when the column exists. Behind one idle-in-transaction session it queues, and every
later query on the table queues behind it, which freezes the whole application.
The schema lives in ``db/schema`` and changes only through ``db/migrations``.
"""

from __future__ import annotations

import re
from pathlib import Path

_REGISTRY = Path(__file__).resolve().parents[2] / "core" / "application" / "registry"
_DDL = re.compile(r"\b(ALTER\s+TABLE|CREATE\s+TABLE|DROP\s+TABLE|CREATE\s+INDEX)\b", re.IGNORECASE)


def test_registry_modules_contain_no_schema_ddl() -> None:
    offenders = [
        f"{path.relative_to(_REGISTRY.parents[2])}:{number}"
        for path in sorted(_REGISTRY.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _DDL.search(line)
    ]
    assert not offenders, f"runtime DDL in the registry package: {offenders}"
