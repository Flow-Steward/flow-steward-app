"""A file-backed ``EntitlementSource``.

A missing file entitles nothing. An unreadable or malformed file raises from
``features_for``/``limits_for`` until it is fixed, so Core sees a failed source
(fail closed for access, "unknown" for the journal retention pass) instead of a
definite "nothing is entitled".
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# fs_reference_dep is the bundled third-party dependency, loaded from site/.
from fs_reference_dep import normalize_feature_names, normalize_limits

from fs_reference_entitlements import ENTITLEMENTS_FILENAME

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Grant:
    features: frozenset[str] = frozenset()
    limits: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    accounts: Mapping[str, _Grant] = field(default_factory=dict)
    default: _Grant = field(default_factory=_Grant)

    def grant_for(self, account_id: str) -> _Grant:
        return self.accounts.get(account_id, self.default)


_EMPTY = _Snapshot()


class UnusableEntitlementsError(RuntimeError):
    """The entitlement file exists but cannot be read or parsed."""


class MalformedEntitlementsError(ValueError):
    """The entitlement file does not have the documented shape."""


def default_entitlements_path() -> Path:
    """``reference-entitlements.json`` in the platform-module root this role reads."""
    from core.infrastructure.platform_modules.loader import platform_modules_root

    return platform_modules_root() / ENTITLEMENTS_FILENAME


def _grant(raw: Any, *, where: str) -> _Grant:
    if not isinstance(raw, Mapping):
        raise MalformedEntitlementsError(f"{where} must be an object")
    unknown = set(raw) - {"features", "limits"}
    if unknown:
        raise MalformedEntitlementsError(f"{where} has unknown keys {sorted(unknown)}")
    try:
        return _Grant(
            features=normalize_feature_names(raw.get("features", [])),
            limits=normalize_limits(raw.get("limits", {})),
        )
    except ValueError as exc:
        raise MalformedEntitlementsError(f"{where}: {exc}") from exc


def parse_entitlements(payload: Any) -> _Snapshot:
    if not isinstance(payload, Mapping):
        raise MalformedEntitlementsError("the file must contain a JSON object")
    unknown = set(payload) - {"accounts", "default"}
    if unknown:
        raise MalformedEntitlementsError(f"unknown top-level keys {sorted(unknown)}")
    raw_accounts = payload.get("accounts", {})
    if not isinstance(raw_accounts, Mapping):
        raise MalformedEntitlementsError("'accounts' must be an object")
    accounts: dict[str, _Grant] = {}
    for account_id, raw in raw_accounts.items():
        key = str(account_id).strip()
        if not key:
            raise MalformedEntitlementsError("an account id is empty")
        accounts[key] = _grant(raw, where=f"accounts[{key!r}]")
    default = _grant(payload.get("default", {}), where="default")
    return _Snapshot(accounts=accounts, default=default)


class FileEntitlementSource:
    """Features and limits per account from one JSON file, re-read when it changes."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int, int] | None = None
        # ``None``: the file at ``_stamp`` is unusable.
        self._snapshot: _Snapshot | None = _EMPTY

    @property
    def path(self) -> Path:
        return self._path

    def features_for(self, account_id: str) -> frozenset[str]:
        return self._current().grant_for(str(account_id or "").strip()).features

    def limits_for(self, account_id: str) -> dict[str, int]:
        return dict(self._current().grant_for(str(account_id or "").strip()).limits)

    def _current(self) -> _Snapshot:
        try:
            info = self._path.stat()
        except OSError:
            with self._lock:
                self._stamp, self._snapshot = None, _EMPTY
            return _EMPTY
        stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
        with self._lock:
            if stamp != self._stamp:
                self._stamp, self._snapshot = stamp, self._load()
            snapshot = self._snapshot
        if snapshot is None:
            raise UnusableEntitlementsError(f"reference entitlements file {self._path} is unusable")
        return snapshot

    def _load(self) -> _Snapshot | None:
        try:
            return parse_entitlements(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            # json.JSONDecodeError and MalformedEntitlementsError are ValueErrors.
            _logger.warning(
                "reference entitlements file %s is unusable (%s); the source fails until it is fixed",
                self._path,
                type(exc).__name__,
            )
            return None
