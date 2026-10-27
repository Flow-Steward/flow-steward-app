from __future__ import annotations

import ast
import json
import unicodedata
from pathlib import Path

from core.infrastructure.config.env_audit import build_env_audit

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "core" / "infrastructure" / "config" / "flow_steward_env_manifest.json"
UNREGISTERED_FS_VARIABLE_BASELINE = 0
DIRECT_ENV_READ_BASELINE = 0
DIRECT_ENV_READ_EXCLUDED_ROOTS = (ROOT / "core" / "infrastructure" / "extension_sdk" / "public",)
# A direct read names os.getenv or os.environ, so source without either identifier
# has none. Non-ASCII source is NFKC-normalised first, as the parser does for names.
DIRECT_ENV_READ_TOKENS = ("getenv", "environ")


def _unregistered_fs_variables() -> list[str]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    registered = {str(row["name"]) for row in manifest["variables"]}
    used = {
        str(row["variable_name"])
        for row in build_env_audit(repo_root=ROOT)
        if str(row["variable_name"]).startswith("FS_") and row.get("code_read_locations")
    }
    return sorted(used - registered)


def _direct_env_reads() -> list[str]:
    reads: list[str] = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        if any(path.is_relative_to(root) for root in DIRECT_ENV_READ_EXCLUDED_ROOTS):
            # The distributable extension SDK is intentionally self-contained and
            # cannot import the host application's runtime-env adapter.
            continue
        text = path.read_text(encoding="utf-8")
        identifiers = text if text.isascii() else unicodedata.normalize("NFKC", text)
        if not any(token in identifiers for token in DIRECT_ENV_READ_TOKENS):
            continue
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            is_read = False
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                is_read = (
                    isinstance(receiver, ast.Name)
                    and receiver.id == "os"
                    and node.func.attr == "getenv"
                ) or (
                    isinstance(receiver, ast.Attribute)
                    and isinstance(receiver.value, ast.Name)
                    and receiver.value.id == "os"
                    and receiver.attr == "environ"
                    and node.func.attr in {"get", "__getitem__"}
                )
            elif isinstance(node, ast.Subscript):
                receiver = node.value
                is_read = (
                    isinstance(receiver, ast.Attribute)
                    and isinstance(receiver.value, ast.Name)
                    and receiver.value.id == "os"
                    and receiver.attr == "environ"
                )
            if is_read:
                reads.append(f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)}")
    return reads


def test_runtime_environment_reads_are_registered_and_centralized() -> None:
    unregistered = _unregistered_fs_variables()
    direct_reads = _direct_env_reads()

    assert len(unregistered) <= UNREGISTERED_FS_VARIABLE_BASELINE, unregistered
    assert len(direct_reads) <= DIRECT_ENV_READ_BASELINE, direct_reads
