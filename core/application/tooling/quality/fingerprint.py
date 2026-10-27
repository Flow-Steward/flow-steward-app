"""Deterministic, input-scoped fingerprints for quality checks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from core.application.tooling.quality import pip_audit_acceptance
from core.application.tooling.quality.check_registry import CheckDefinition

FingerprintSource = Literal["worktree", "index", "commit"]
_GATE_INPUTS = (
    "infra/tooling/quality/check-registry.json",
    "core/application/tooling/quality/check_registry.py",
    "core/application/tooling/quality/fingerprint.py",
    "core/application/tooling/quality/evidence.py",
    "core/application/tooling/quality/verify.py",
    "core/entrypoints/cli/scripts/lint.sh",
    # The audit image pins the tool versions. Evidence reused across commits
    # (reuse_scope "inputs") must not outlive a ruff, mypy or semgrep upgrade.
    "infra/docker/Dockerfile",
    "requirements-audit.txt",
    "requirements-semgrep.txt",
    "requirements-cli.lock",
    # The audit image inherits the runtime packages too (runtime-dependencies stage).
    "requirements-runtime.lock",
    # pip installs this source package outside /app in the audit image.
    "core/infrastructure/extension_sdk/public/**",
)


def committed_candidate_sha(root: Path, expected_sha: str = "") -> str:
    """Bind executable checkout evidence to a clean, exact local HEAD."""
    observed = _run_git(root, "rev-parse", "HEAD").decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{40}", observed) is None:
        raise ValueError("candidate HEAD is unavailable or invalid")
    if expected_sha.strip() and expected_sha.strip() != observed:
        raise ValueError("candidate SHA does not match local HEAD")
    if _run_git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("candidate checkout has uncommitted changes")
    return observed


class _Hash(Protocol):
    def update(self, value: bytes) -> object: ...


@dataclass(frozen=True)
class FingerprintContext:
    """Repository view and non-sensitive runtime identity used for hashing."""

    root: Path
    source: FingerprintSource
    commit_sha: str = ""
    runtime_identity: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        root = self.root.resolve()
        if not (root / ".git").exists():
            raise ValueError(f"fingerprint root is not a Git repository: {root}")
        if self.source == "commit" and not self.commit_sha.strip():
            raise ValueError("commit_sha is required for commit fingerprints")
        object.__setattr__(self, "root", root)

    def iter_matching_content(
        self, patterns: tuple[str, ...]
    ) -> tuple[tuple[PurePosixPath, bytes], ...]:
        """Return sorted repository-relative content for the selected source."""
        expanded = _validated_patterns(patterns)
        if self.source == "worktree":
            return _worktree_content(self.root, expanded)
        if self.source == "index":
            return _git_content(self.root, expanded, source="index")
        return _git_content(self.root, expanded, source="commit", commit_sha=self.commit_sha)


def fingerprint_check(check: CheckDefinition, context: FingerprintContext) -> str:
    """Hash one check definition, its declared inputs, and runtime identity."""
    digest = hashlib.sha256()
    _feed(digest, "schema", b"1")
    definition = json.dumps(
        check.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    _feed(digest, "definition", definition)
    for path, content in context.iter_matching_content(_GATE_INPUTS):
        _feed(digest, f"gate:{path.as_posix()}", content)
    for path, content in context.iter_matching_content(check.inputs):
        _feed(digest, path.as_posix(), content if check.input_mode == "content" else b"")
    runtime_identity = dict(context.runtime_identity)
    if check.id == "pip-audit":
        # Expiring acceptances cannot reuse yesterday's green evidence. Own this
        # key here so both verify and release_gate agree, regardless of callers.
        runtime_identity["pip_audit_acceptance_utc_date"] = (
            pip_audit_acceptance.acceptance_evaluation_date().isoformat()
        )
    for key, value in sorted(runtime_identity.items()):
        _feed(digest, f"runtime:{key}", value.encode("utf-8"))
    return digest.hexdigest()


def _feed(hasher: _Hash, label: str, value: bytes) -> None:
    encoded_label = label.encode("utf-8")
    update = hasher.update
    update(len(encoded_label).to_bytes(8, "big"))
    update(encoded_label)
    update(len(value).to_bytes(8, "big"))
    update(value)


def _validated_patterns(patterns: tuple[str, ...]) -> tuple[str, ...]:
    expanded: list[str] = []
    for pattern in patterns:
        normalized = pattern.replace("\\", "/").strip()
        path = PurePosixPath(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"quality input must be repository-relative: {pattern!r}")
        expanded.extend(_expand_braces(normalized))
    return tuple(dict.fromkeys(expanded))


def _expand_braces(pattern: str) -> tuple[str, ...]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if match is None:
        return (pattern,)
    choices = tuple(choice.strip() for choice in match.group(1).split(","))
    if not all(choices):
        raise ValueError(f"invalid brace expansion in quality input: {pattern!r}")
    expanded: list[str] = []
    for choice in choices:
        replaced = pattern[: match.start()] + choice + pattern[match.end() :]
        expanded.extend(_expand_braces(replaced))
    return tuple(expanded)


def _worktree_content(
    root: Path, patterns: tuple[str, ...]
) -> tuple[tuple[PurePosixPath, bytes], ...]:
    matched: dict[str, bytes] = {}
    candidates = _run_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for raw_path in candidates.split(b"\0"):
        if not raw_path:
            continue
        normalized = raw_path.decode("utf-8", errors="surrogateescape")
        if not _matches_any_pattern(normalized, patterns):
            continue
        path = root / normalized
        if path.is_symlink():
            matched[normalized] = b"symlink\0" + os.readlink(path).encode("utf-8")
        elif path.is_file():
            matched[normalized] = path.read_bytes()
    return tuple((PurePosixPath(path), matched[path]) for path in sorted(matched))


def _git_content(
    root: Path,
    patterns: tuple[str, ...],
    *,
    source: Literal["index", "commit"],
    commit_sha: str = "",
) -> tuple[tuple[PurePosixPath, bytes], ...]:
    if source == "index":
        output = _run_git(root, "ls-files", "-s", "-z")
    else:
        output = _run_git(root, "ls-tree", "-r", "-z", commit_sha)

    entries: dict[str, tuple[str, str]] = {}
    for raw_entry in output.split(b"\0"):
        if not raw_entry:
            continue
        metadata, raw_path = raw_entry.split(b"\t", 1)
        parts = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if not _matches_any_pattern(path, patterns):
            continue
        if source == "index":
            mode, object_id, stage = parts
            if stage != "0":
                raise ValueError(f"cannot fingerprint unmerged index path: {path}")
        else:
            mode, _object_type, object_id = parts
        entries[path] = (mode, object_id)

    blob_ids = tuple(
        dict.fromkeys(object_id for mode, object_id in entries.values() if mode != "160000")
    )
    blobs = _read_git_blobs(root, blob_ids)
    content = []
    for path in sorted(entries):
        mode, object_id = entries[path]
        value = b"gitlink\0" + object_id.encode("ascii") if mode == "160000" else blobs[object_id]
        content.append((PurePosixPath(path), value))
    return tuple(content)


def _matches_any_pattern(path: str, patterns: tuple[str, ...]) -> bool:
    return any(_glob_regex(pattern).fullmatch(path) is not None for pattern in patterns)


def _glob_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    cursor = 0
    while cursor < len(pattern):
        character = pattern[cursor]
        if character == "*" and pattern[cursor : cursor + 3] == "**/":
            parts.append(r"(?:.*/)?")
            cursor += 3
        elif character == "*" and pattern[cursor : cursor + 2] == "**":
            parts.append(r".*")
            cursor += 2
        elif character == "*":
            parts.append(r"[^/]*")
            cursor += 1
        elif character == "?":
            parts.append(r"[^/]")
            cursor += 1
        else:
            parts.append(re.escape(character))
            cursor += 1
    return re.compile("".join(parts))


def _run_git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ("git", "-c", f"safe.directory={root}", *args),
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _read_git_blobs(root: Path, object_ids: tuple[str, ...]) -> dict[str, bytes]:
    if not object_ids:
        return {}
    result = subprocess.run(
        ("git", "-c", f"safe.directory={root}", "cat-file", "--batch"),
        cwd=root,
        input=("\n".join(object_ids) + "\n").encode("ascii"),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git cat-file --batch failed: {detail}")

    blobs: dict[str, bytes] = {}
    cursor = 0
    for requested_id in object_ids:
        header_end = result.stdout.find(b"\n", cursor)
        if header_end < 0:
            raise ValueError("git cat-file returned an incomplete header")
        header = result.stdout[cursor:header_end].decode("ascii").split()
        if len(header) != 3 or header[1] != "blob":
            raise ValueError(f"Git object is not a blob: {requested_id}")
        size = int(header[2])
        content_start = header_end + 1
        content_end = content_start + size
        blobs[requested_id] = result.stdout[content_start:content_end]
        if result.stdout[content_end : content_end + 1] != b"\n":
            raise ValueError("git cat-file returned an incomplete blob")
        cursor = content_end + 1
    return blobs
