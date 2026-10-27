"""Runtime boundaries for deterministic workflow authoring."""

from __future__ import annotations

import json
import subprocess
import sys

from core.entrypoints.admin import workflows


def test_authoring_import_does_not_load_retired_builder_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys; from core.application.workflows.authoring.service import WorkflowAuthoringService; print(json.dumps([name for name in sys.modules if name.startswith('core.application.workflows.ai_builder')]))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == []


def test_workflow_router_has_no_retired_builder_endpoints():
    paths = {route.path for route in workflows.router.routes}
    assert not {path for path in paths if "/ai-builder/" in path}
    assert "/api/workflows/{workflow_id}/canvas-graph" in paths
    assert "/api/workflows/{workflow_id}/validate" in paths
