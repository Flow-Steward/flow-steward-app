"""Worker date operations need the same compiled translations as other adapters."""

import json
from pathlib import Path


def test_worker_packages_date_lexicons_and_locale_metadata():
    root = Path(__file__).resolve().parents[2]
    payload = json.loads((root / "infra/docker/source-slices/worker.json").read_text())
    paths = {item["path"] for item in payload["package_data_paths"]}
    assert {
        f"locales/{locale}/{name}"
        for locale in ("en", "lt")
        for name in ("LC_MESSAGES/ui.mo", "meta.json")
    } <= paths


def test_role_slices_package_every_standard_connector_module():
    root = Path(__file__).resolve().parents[2]
    for role in ("app", "worker", "compact"):
        payload = json.loads((root / f"infra/docker/source-slices/{role}.json").read_text())
        paths = set(payload["static_paths"]) | {item["path"] for item in payload["dynamic_paths"]}
        for category in ("array", "boolean", "date", "json", "money", "number", "text"):
            assert f"core/application/orchestration/tools/standard_{category}_actions.py" in paths
