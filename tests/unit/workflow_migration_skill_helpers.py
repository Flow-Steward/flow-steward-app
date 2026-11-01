"""Load the standalone skill without adding product entrypoints or dependencies."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

SKILL_ROOT = (
    Path(__file__).resolve().parents[2]
    / "docs/agent-skills/flow-steward-operator/skills/flow-steward-migrate-workflow"
)
_NAME = "flow_steward_migration_skill_test"
_SPEC = importlib.util.spec_from_file_location(
    _NAME,
    SKILL_ROOT / "scripts/workflow_migration/__init__.py",
    submodule_search_locations=[str(SKILL_ROOT / "scripts/workflow_migration")],
)
assert _SPEC is not None and _SPEC.loader is not None
_PACKAGE = importlib.util.module_from_spec(_SPEC)
sys.modules[_NAME] = _PACKAGE
_SPEC.loader.exec_module(_PACKAGE)
convert_n8n = importlib.import_module(f"{_NAME}.n8n").convert_n8n
