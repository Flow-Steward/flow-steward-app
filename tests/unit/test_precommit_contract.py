from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PRE_COMMIT = ROOT / ".pre-commit-config.yaml"


def test_precommit_sync_precedes_tier_two_reuse_and_never_stages() -> None:
    source = PRE_COMMIT.read_text(encoding="utf-8")
    config = yaml.safe_load(source)
    hooks = config["repos"][0]["hooks"]

    assert [hook["id"] for hook in hooks[:3]] == [
        "fs-env-artifacts-sync",
        "fs-repository-boundary",
        "fs-quality-fast",
    ]
    assert "--evidence-mode reuse" in hooks[2]["entry"]
    assert "--fingerprint-source index" in hooks[2]["entry"]
    assert "git add" not in source
