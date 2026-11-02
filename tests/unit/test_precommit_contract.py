from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PRE_COMMIT = ROOT / ".pre-commit-config.yaml"


def test_precommit_sync_precedes_local_tier_one_and_never_stages() -> None:
    source = PRE_COMMIT.read_text(encoding="utf-8")
    config = yaml.safe_load(source)
    hooks = config["repos"][0]["hooks"]

    assert [hook["id"] for hook in hooks[:3]] == [
        "fs-env-artifacts-sync",
        "fs-repository-boundary",
        "fs-quality-targeted",
    ]
    assert hooks[2]["entry"] == (
        "core/entrypoints/cli/scripts/lint.sh --checks targeted-tests --test-paths auto"
    )
    assert not any("--fast" in hook["entry"] for hook in hooks if "pre-commit" in hook["stages"])
    assert "git add" not in source
