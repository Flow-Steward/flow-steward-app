"""Discover exact production releases from public GitHub Releases."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import total_ordering
from urllib.parse import quote

DEFAULT_RELEASE_REPOSITORY = "Flow-Steward/flow-steward-app"
GITHUB_API_ROOT = "https://api.github.com"
GITHUB_WEB_ROOT = "https://github.com"
GITHUB_RELEASE_PAGE_SIZE = 100
DEFAULT_MAX_RELEASE_PAGES = 10
FLOATING_TAGS = frozenset({"latest", "pg17", "stable", "edge", "main", "master"})
SHA256_IMAGE_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$", re.ASCII)
_TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$", re.ASCII)
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$"
)


class ReleaseError(RuntimeError):
    """Release metadata is unavailable, malformed, or unsafe."""


class ReleaseDiscoveryError(ReleaseError):
    """Typed, secret-free failure while reading an external release catalog."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "release_discovery_failed",
        remediation: str = "Correct the release discovery configuration and retry.",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.remediation = remediation
        self.retryable = retryable


class ReleaseNotFoundError(ReleaseDiscoveryError):
    """No published stable release matches the requested version."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="release_not_found",
            remediation="Verify the requested release exists and the credential can read it.",
            retryable=False,
        )


def _release_contract_error(message: str) -> ReleaseDiscoveryError:
    return ReleaseDiscoveryError(
        message,
        code="release_github_contract_invalid",
        remediation="Correct the published GitHub release metadata before retrying.",
        retryable=False,
    )


@total_ordering
@dataclass(frozen=True)
class ReleaseVersion:
    """Canonical public production version with numeric ordering."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> ReleaseVersion:
        text = str(value or "")
        match = _VERSION_RE.fullmatch(text)
        if match is None:
            raise ReleaseError("Release version must match canonical X.Y.Z")
        return cls(*(int(part) for part in match.groups()))

    @classmethod
    def from_tag(cls, tag: str) -> ReleaseVersion:
        text = str(tag or "")
        match = _TAG_RE.fullmatch(text)
        if match is None:
            raise ReleaseError("Production release tag must match exactly vX.Y.Z")
        return cls(*(int(part) for part in match.groups()))

    @property
    def tag(self) -> str:
        return f"v{self}"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ReleaseVersion):
            return NotImplemented
        return (self.major, self.minor, self.patch) < (
            other.major,
            other.minor,
            other.patch,
        )


@dataclass(frozen=True)
class ReleaseCoordinates:
    """Exact source identity shared by local install, remote update, and Clone."""

    version: ReleaseVersion
    tag: str
    commit_sha: str
    source: str
    repository: str

    def __post_init__(self) -> None:
        if not isinstance(self.version, ReleaseVersion):
            raise ReleaseError("Release coordinates require a canonical version")
        if self.tag != self.version.tag:
            raise ReleaseError("Release coordinate tag must match its canonical version")
        if _FULL_GIT_SHA_RE.fullmatch(self.commit_sha) is None:
            raise ReleaseError("Release coordinates require a full lowercase Git SHA")
        validated_release_repository(self.repository)
        if not self.source.strip() or len(self.source) > 512:
            raise ReleaseError("Release coordinates require bounded source provenance")


@dataclass(frozen=True)
class PublishedRelease:
    """Stable GitHub Release identity before platform image resolution."""

    version: ReleaseVersion
    tag: str
    commit_sha: str
    published_at: str
    source: str
    repository: str

    @property
    def coordinates(self) -> ReleaseCoordinates:
        return ReleaseCoordinates(
            version=self.version,
            tag=self.tag,
            commit_sha=self.commit_sha,
            source=self.source,
            repository=self.repository,
        )


JsonFetcher = Callable[[str], object]
TagCommitResolver = Callable[[str, str], str]


class GitHubReleaseDiscovery:
    """Select stable GitHub releases through an injected HTTP transport."""

    def __init__(
        self,
        *,
        fetch_json: JsonFetcher,
        repository: str = DEFAULT_RELEASE_REPOSITORY,
        resolve_tag_commit: TagCommitResolver | None = None,
        max_pages: int = DEFAULT_MAX_RELEASE_PAGES,
    ) -> None:
        self.repository = validated_release_repository(repository)
        self.fetch_json = fetch_json
        self.resolve_tag_commit = resolve_tag_commit or (
            lambda selected_repository, tag: _resolve_github_tag_commit(
                selected_repository, tag, fetch_json=self.fetch_json
            )
        )
        if not 1 <= int(max_pages) <= 100:
            raise ReleaseError("GitHub Release pagination limit must be between 1 and 100")
        self.max_pages = int(max_pages)

    def latest(self) -> PublishedRelease:
        releases = self._published_releases()
        if not releases:
            raise ReleaseNotFoundError(
                f"Repository {self.repository} has no published stable vX.Y.Z release"
            )
        return self._resolve(max(releases, key=lambda row: row[0]))

    def for_version(self, version: str) -> PublishedRelease:
        requested = ReleaseVersion.parse(version)
        for candidate in self._published_releases():
            if candidate[0] == requested:
                return self._resolve(candidate)
        raise ReleaseNotFoundError(
            f"Published stable release {requested} is unavailable from {self.repository}"
        )

    def _published_releases(self) -> tuple[tuple[ReleaseVersion, dict[str, object]], ...]:
        selected: dict[ReleaseVersion, dict[str, object]] = {}
        for page in range(1, self.max_pages + 1):
            payload = self.fetch_json(
                f"{GITHUB_API_ROOT}/repos/{self.repository}/releases"
                f"?per_page={GITHUB_RELEASE_PAGE_SIZE}&page={page}"
            )
            if not isinstance(payload, list):
                raise _release_contract_error("GitHub Releases endpoint must return a JSON list")
            for raw in payload:
                if not isinstance(raw, dict):
                    raise _release_contract_error(
                        "GitHub Releases endpoint contains a non-object release"
                    )
                candidate = _published_candidate(raw)
                if candidate is None:
                    continue
                version, release = candidate
                if version in selected:
                    raise _release_contract_error(
                        f"GitHub returned duplicate stable release {version}"
                    )
                selected[version] = release
            if len(payload) < GITHUB_RELEASE_PAGE_SIZE:
                return tuple(selected.items())
        raise _release_contract_error(
            f"GitHub Release pagination limit ({self.max_pages} pages) was exceeded"
        )

    def _resolve(self, candidate: tuple[ReleaseVersion, dict[str, object]]) -> PublishedRelease:
        version, raw = candidate
        tag = version.tag
        commit_sha = str(self.resolve_tag_commit(self.repository, tag) or "").lower()
        if _FULL_GIT_SHA_RE.fullmatch(commit_sha) is None:
            raise _release_contract_error("Release tag must resolve to a full 40-character Git SHA")
        published_at = str(raw.get("published_at") or "").strip()
        if not published_at:
            raise _release_contract_error(f"Published release {tag} has no publication timestamp")
        return PublishedRelease(
            version=version,
            tag=tag,
            commit_sha=commit_sha,
            published_at=published_at,
            source=f"{GITHUB_WEB_ROOT}/{self.repository}/releases/tag/{tag}",
            repository=self.repository,
        )


def _published_candidate(raw: object) -> tuple[ReleaseVersion, dict[str, object]] | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("draft") is not False or raw.get("prerelease") is not False:
        return None
    tag = str(raw.get("tag_name") or "")
    if _TAG_RE.fullmatch(tag) is None:
        return None
    return ReleaseVersion.from_tag(tag), raw


def validated_release_repository(value: str) -> str:
    repository = str(value or "").strip()
    if _REPOSITORY_RE.fullmatch(repository) is None:
        raise ReleaseError("Release repository must match owner/name")
    return repository


def _resolve_github_tag_commit(repository: str, tag: str, *, fetch_json: JsonFetcher) -> str:
    payload = fetch_json(f"{GITHUB_API_ROOT}/repos/{repository}/git/ref/tags/{quote(tag, safe='')}")
    if not isinstance(payload, dict):
        raise _release_contract_error("GitHub tag reference must be a JSON object")
    target = payload.get("object")
    for _depth in range(2):
        if not isinstance(target, dict):
            raise _release_contract_error("GitHub tag reference has no target object")
        target_type = str(target.get("type") or "")
        target_sha = str(target.get("sha") or "").lower()
        if _FULL_GIT_SHA_RE.fullmatch(target_sha) is None:
            raise _release_contract_error("GitHub tag target is not a full 40-character Git SHA")
        if target_type == "commit":
            return target_sha
        if target_type != "tag":
            raise _release_contract_error("GitHub release tag does not resolve to a commit")
        annotated = fetch_json(f"{GITHUB_API_ROOT}/repos/{repository}/git/tags/{target_sha}")
        if not isinstance(annotated, dict):
            raise _release_contract_error("GitHub annotated tag must be a JSON object")
        target = annotated.get("object")
    raise _release_contract_error("GitHub release tag indirection exceeds the supported bound")


def _normalize_version(version: str) -> str:
    text = str(version or "").strip()
    return text[1:] if text.startswith("v") else text


def _versions_equal(left: str, right: str) -> bool:
    try:
        return ReleaseVersion.parse(_normalize_version(left)) == ReleaseVersion.parse(
            _normalize_version(right)
        )
    except ReleaseError:
        return False


def _image_version(image: str) -> str:
    text = str(image or "").strip()
    if not text or "@sha256:" in text:
        return ""
    leaf = text.rsplit("/", 1)[-1]
    return _normalize_version(leaf.rsplit(":", 1)[1]) if ":" in leaf else ""


def _is_floating_image_ref(image: str) -> bool:
    text = str(image or "").strip()
    if not text:
        return True
    if "@sha256:" in text:
        return False
    leaf = text.rsplit("/", 1)[-1]
    if ":" not in leaf:
        return True
    return leaf.rsplit(":", 1)[1].strip().lower() in FLOATING_TAGS


def _is_digest_qualified_image_ref(image: str) -> bool:
    return bool(SHA256_IMAGE_RE.fullmatch(str(image or "").strip().lower()))


__all__ = [
    "DEFAULT_RELEASE_REPOSITORY",
    "GitHubReleaseDiscovery",
    "PublishedRelease",
    "ReleaseCoordinates",
    "ReleaseDiscoveryError",
    "ReleaseError",
    "ReleaseNotFoundError",
    "ReleaseVersion",
    "_image_version",
    "_is_digest_qualified_image_ref",
    "_is_floating_image_ref",
    "_normalize_version",
    "_versions_equal",
    "validated_release_repository",
]
