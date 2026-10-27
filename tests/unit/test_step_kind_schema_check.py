"""The database accepts every step kind the code supports.

A new step kind that is missing from the canonical CHECK constraint passes
every unit test and fails only when an operator saves such a step.
"""

from __future__ import annotations

import re

from core.application.workflows.workflow_definition_validation import DEFINITION_STEP_KINDS
from core.infrastructure.db.schema_state import read_canonical_schema_sql


def test_the_schema_check_lists_every_supported_step_kind() -> None:
    schema = read_canonical_schema_sql()
    match = re.search(
        r"registry_workflow_steps_step_kind_check CHECK \(\(step_kind = ANY \(ARRAY\[(.*?)\]\)\)\)",
        schema,
    )
    assert match, "registry_workflow_steps_step_kind_check is missing from db/schema"
    allowed = set(re.findall(r"'([a-z0-9_]+)'::text", match.group(1)))

    assert set(DEFINITION_STEP_KINDS) <= allowed, sorted(set(DEFINITION_STEP_KINDS) - allowed)
