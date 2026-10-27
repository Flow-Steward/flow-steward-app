"""Guardrails for entrypoint boundary cleanup in iteration 1."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TARGET_ENTRYPOINTS = (
    ROOT / "core" / "entrypoints" / "api" / "workflow_entrypoints.py",
    ROOT / "core" / "entrypoints" / "api" / "security.py",
    ROOT / "core" / "entrypoints" / "api" / "auth_users.py",
)
LLM_PROVIDER_ENTRYPOINT = ROOT / "core" / "entrypoints" / "api" / "llm_provider_extensions.py"
LLM_PROVIDER_APPLICATION_REPOSITORY = (
    ROOT / "core" / "application" / "settings" / "llm_provider_extension_repository.py"
)
LLM_PROVIDER_OLD_REPOSITORY = (
    ROOT / "core" / "infrastructure" / "db" / "llm_provider_extension_store.py"
)

SQL_PATTERN = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b")
DIRECT_CONN_PATTERN = re.compile(r"\b(?:bl|conn)\.conn\b|\bconn\.cursor\s*\(|\bbl\.cursor\s*\(")
FOREIGN_PRIVATE_PATTERN = re.compile(
    r"\.\_[A-Za-z][A-Za-z0-9_]*\b|getattr\([^)\n]*[\"']_[A-Za-z][A-Za-z0-9_]*[\"']"
)


def _matching_lines(path: Path, pattern: re.Pattern[str]) -> list[str]:
    matches: list[str] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if pattern.search(line):
            matches.append(f"{path.relative_to(ROOT)}:{line_no}: {line.strip()}")
    return matches


def test_iteration_one_entrypoints_do_not_embed_sql():
    offenders = []
    for path in TARGET_ENTRYPOINTS:
        offenders.extend(_matching_lines(path, SQL_PATTERN))
    assert not offenders, (
        "Iteration 1 entrypoints must not embed SQL after boundary extraction. "
        f"Offending lines: {offenders}"
    )


def test_iteration_one_entrypoints_do_not_touch_connections_directly():
    offenders = []
    for path in TARGET_ENTRYPOINTS:
        offenders.extend(_matching_lines(path, DIRECT_CONN_PATTERN))
    assert not offenders, (
        "Iteration 1 entrypoints must not use direct connection access after boundary extraction. "
        f"Offending lines: {offenders}"
    )


def test_iteration_one_entrypoints_do_not_call_foreign_private_apis():
    offenders = []
    for path in TARGET_ENTRYPOINTS:
        offenders.extend(_matching_lines(path, FOREIGN_PRIVATE_PATTERN))
    assert not offenders, (
        f"Iteration 1 entrypoints must not call foreign private APIs. Offending lines: {offenders}"
    )


def test_llm_provider_entrypoint_is_transport_only():
    source = LLM_PROVIDER_ENTRYPOINT.read_text(encoding="utf-8")
    forbidden = (
        "transaction_connection",
        "encrypt_secret",
        "decrypt_secret",
        "run_capability_suite",
        "llm_provider_extension_repository",
    )
    assert not [name for name in forbidden if name in source]
    assert len(source.splitlines()) <= 250


def test_llm_provider_repository_is_application_owned_without_compatibility_shim():
    assert LLM_PROVIDER_APPLICATION_REPOSITORY.is_file()
    assert not LLM_PROVIDER_OLD_REPOSITORY.exists()
