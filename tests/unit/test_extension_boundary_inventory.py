"""Shipped source must not name a concrete extension.

Core knows the abstract extension protocol — manifest, install/enable lifecycle,
sandbox, routing, and a limited public SDK. It does not know which extensions
exist, what they are called, what their actions are named, or what their domain
fields mean. The same holds for the platform's own tests, its product data, its
frontend and its release tooling: an identity written into any of them is a
dependency on a bundle that is meant to be able to leave.

The identities are literals here on purpose. Reading `extensions/` to discover
them would make this guard pass vacuously the moment that directory is empty —
which is exactly the state the extraction is heading for, and exactly when the
guard matters most. It therefore reads no manifest and requires no bundle.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Vendor prefix every first-party extension identity uses, in both spellings:
#: the dotted extension id and the bundle directory name.
_VENDOR_PREFIXES = ("flowsteward.", "flowsteward_")

#: What is still outstanding, named one by one so it can only shrink.
#:
#: One group is left: the candidate smoke script and the gate test that asserts its
#: contents. Splitting it means a generic platform acceptance against a synthetic
#: installed extension plus per-bundle release suites, and the script runs against a
#: live stand — so it is a release-verification change with its own review, not a
#: rename. Why, in docs/tasks/extension-boundary-core-leaks-2026-08-31.md.
#:
#: A guard that skipped these files would stop noticing a *new* identity in them.
#: Each entry is a file plus the exact identity it still writes down.
#: Empty, and it must stay that way. Every concrete identity that used to live in
#: Core moved to the bundle that owns it — see the release suites under
#: `extensions/<bundle>/release/checks.py` — or onto the synthetic `acme.*` probe
#: the platform's own checks drive.
KNOWN_REMAINING: frozenset[str] = frozenset()

# This is not a shipped bundle identity: it is a runtime lock namespace.
NON_BUNDLE_IDENTITIES = frozenset(
    {
        "core/infrastructure/extension_runtime/extension_operation_lock.py: "
        "flowsteward.extension-operation",
        # The fs-audit image's OCI label (org.flowsteward.audit-toolchain), not a bundle.
        "core/entrypoints/cli/scripts/lint.sh: flowsteward.audit-toolchain",
        "tests/unit/test_lint_script.py: flowsteward.audit-toolchain",
    }
)

#: Importing a bundle's code is a coupling no rename can fix, and unlike a name in
#: a docstring it is unambiguous. Reading a *named* bundle's files is the same
#: coupling and is already caught by the identity check below — spelling it a
#: second time by path only re-found Python module paths like
#: `core/application/extensions/binding_context.py` and `tmp_path` fixture
#: directories, which is why the first attempt at this was noise.
BUNDLE_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+extensions[.\s]|sys\.path\.(?:insert|append)\([^)]*/extensions/",
    re.MULTILINE,
)

#: Where a concrete identity must not appear. `extensions/` is deliberately absent:
#: a bundle naming itself is the one place it belongs.
SCANNED_ROOTS = ("core", "assets", "db", "scripts", "tests")
SCANNED_SUFFIXES = {
    ".py",
    ".sh",
    ".yaml",
    ".yml",
    ".json",
    ".ts",
    ".tsx",
    ".sql",
}
#: This file names the prefixes in order to forbid them.
_SELF = Path(__file__).relative_to(REPO_ROOT).as_posix()

#: A dotted id is unambiguous: `flowsteward.<slug>` is only ever an extension.
_DOTTED_IDENTITY = re.compile(r"flowsteward\.[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE)
#: The underscore spelling is a bundle *directory*, so it only counts inside a
#: path. Matching it as a bare word would flag ordinary names like
#: `flowsteward_upload_` (a tempfile prefix) and the SDK distribution.
_BUNDLE_PATH_IDENTITY = re.compile(
    r"extensions/(flowsteward_[a-z0-9]+(?:_[a-z0-9]+)*)", re.IGNORECASE
)


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        files.extend(
            path
            for path in sorted(base.rglob("*"))
            if path.is_file()
            and path.suffix in SCANNED_SUFFIXES
            and "node_modules" not in path.parts
        )
    return files


def _offending_identities(text: str) -> set[str]:
    candidates = {match.lower() for match in _DOTTED_IDENTITY.findall(text)}
    candidates |= {match.lower() for match in _BUNDLE_PATH_IDENTITY.findall(text)}
    return {
        identity
        for identity in candidates
        # The SDK distribution is not an extension.
        if not identity.startswith("flowsteward_extension_sdk")
        # Nor is the product's own domain (docs.flowsteward.org).
        and identity != "flowsteward.org"
    }


def test_shipped_source_does_not_import_a_bundle() -> None:
    """The platform must be buildable with `extensions/` empty.

    An import of bundle code is a hard dependency on that bundle shipping; a
    product name in a docstring is not, and treating the two the same would have
    turned a rename into a rewrite of HMAC verification and LLM supplier
    resolution on no evidence.
    """
    offenders: list[str] = []
    for path in _scanned_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative == _SELF:
            continue
        if BUNDLE_IMPORT.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(relative)
    assert not offenders, "\n".join(offenders)


def test_no_concrete_extension_identity_is_written_into_shipped_source() -> None:
    """One test over source, product data, frontend, tooling and the platform suite."""
    offenders: list[str] = []
    for path in _scanned_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative == _SELF:
            continue
        for identity in sorted(
            _offending_identities(path.read_text(encoding="utf-8", errors="ignore"))
        ):
            entry = f"{relative}: {identity}"
            if entry not in KNOWN_REMAINING and entry not in NON_BUNDLE_IDENTITIES:
                offenders.append(entry)
    assert not offenders, "\n".join(offenders)


def test_the_outstanding_list_only_shrinks() -> None:
    """An entry that no longer applies must be deleted, not left to rot.

    A stale allowance is indistinguishable from an unnoticed one, and it makes the
    remaining work look larger than it is.
    """
    stale: list[str] = []
    for entry in sorted(KNOWN_REMAINING):
        relative, _, identity = entry.partition(": ")
        path = REPO_ROOT / relative
        if not path.is_file() or identity not in path.read_text(encoding="utf-8", errors="ignore"):
            stale.append(entry)
    assert not stale, "no longer present; delete from KNOWN_REMAINING:\n" + "\n".join(stale)


def test_the_guard_does_not_read_or_need_any_extension() -> None:
    """It must still hold when `extensions/` is gone — that is the point of it.

    The identities are literals above; nothing here discovers them from a bundle,
    so an empty or absent `extensions/` weakens nothing.
    """
    assert "extensions" not in SCANNED_ROOTS
    scanned = _scanned_files()
    assert scanned, "the scan found no files at all"
    assert not [path for path in scanned if path.relative_to(REPO_ROOT).parts[0] == "extensions"]


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        ("flowsteward.sample-mailbox", {"flowsteward.sample-mailbox"}),
        (
            "extensions/flowsteward_sample_connector/tests",
            {"flowsteward_sample_connector"},
        ),
        # A bare word that merely starts with the vendor prefix is not an identity.
        ("flowsteward_upload_", set()),
        ("flowsteward_did_not_prepare_a_draft_to_validate_message", set()),
        ("extensions/flowsteward_extension_sdk", set()),
        ("acme.showcase", set()),
        ("Flow Steward", set()),
    ],
)
def test_the_identity_pattern_catches_what_it_should(sample: str, expected: set[str]) -> None:
    assert _offending_identities(sample) == expected


def test_core_does_not_know_one_extensions_action_vocabulary() -> None:
    """The audit policy is declared by the action, so Core needs none of these."""
    source = "\n".join(
        (REPO_ROOT / "core" / "application" / "extensions" / filename).read_text(encoding="utf-8")
        for filename in (
            "operation_service.py",
            "operation_projection.py",
            "operation_effects.py",
            "operation_audit.py",
        )
    )
    for token in ("imap", "mailbox", "get_message", "search_messages", "bodyHtml", "rawHeaders"):
        assert token.lower() not in source.lower(), f"operation_service.py mentions {token}"


def test_the_audit_policy_is_read_from_the_declaration() -> None:
    """The mechanism, not just the absence of names: Core applies a declaration."""
    from core.application.extensions.operation_projection import action_audit_policy

    declared = {
        "action_definitions": [
            {
                "action_id": "fetch_record",
                "audit": {
                    "redact_result_fields": ["Payload", "note"],
                    "failure_projection": {"keep_row_fields": ["item_id"]},
                },
            }
        ]
    }
    policy = action_audit_policy(declared, "fetch_record")
    assert policy["redact_result_fields"] == frozenset({"payload", "note"})
    assert policy["failure_projection"] == frozenset({"item_id"})

    assert action_audit_policy(declared, "other_action") == {}
    assert action_audit_policy({"action_definitions": [{"action_id": "fetch_record"}]}, "x") == {}
    assert action_audit_policy({}, "fetch_record") == {}
