"""Canonical owner-only SeaweedFS S3 identity provisioning."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path

from core.infrastructure.installer.secret_values import is_missing_or_placeholder_secret
from core.infrastructure.installer.state_store import _atomic_write_text

_IDENTITY_NAME = "flow-steward-core"


class BundledStorageIdentityService:
    """Read or idempotently provision the installer-owned Seaweed identity."""

    def __init__(
        self,
        path: Path,
        *,
        secret_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self._secret_factory = secret_factory or (lambda: secrets.token_urlsafe(32))

    def read(self) -> dict[str, str]:
        try:
            file_stat = self.path.lstat()
            if (
                self.path.is_symlink()
                or not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.getuid()
                or file_stat.st_mode & 0o077
            ):
                raise ValueError("Bundled storage identity must be an owner-only regular file")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Bundled storage identity is unavailable") from exc
        identities = payload.get("identities") if isinstance(payload, dict) else None
        identity = next(
            (
                item
                for item in identities or ()
                if isinstance(item, dict) and item.get("name") == _IDENTITY_NAME
            ),
            None,
        )
        records = identity.get("credentials") if isinstance(identity, dict) else None
        record = records[0] if isinstance(records, list) and records else None
        access_key = str(record.get("accessKey") or "") if isinstance(record, dict) else ""
        secret_key = str(record.get("secretKey") or "") if isinstance(record, dict) else ""
        if is_missing_or_placeholder_secret(access_key) or is_missing_or_placeholder_secret(
            secret_key
        ):
            raise ValueError("Bundled storage identity credentials are incomplete")
        return {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "aws_session_token": "",
        }

    def ensure(
        self,
        *,
        existing_access_key: str = "",
        existing_secret_key: str = "",
    ) -> dict[str, object]:
        if self.path.exists() or self.path.is_symlink():
            credentials = self.read()
        else:
            access_key = (
                existing_access_key
                if not is_missing_or_placeholder_secret(existing_access_key)
                else self._secret_factory()
            )
            secret_key = (
                existing_secret_key
                if not is_missing_or_placeholder_secret(existing_secret_key)
                else self._secret_factory()
            )
            if is_missing_or_placeholder_secret(access_key) or is_missing_or_placeholder_secret(
                secret_key
            ):
                raise ValueError("Generated bundled storage credentials are invalid")
            self._write(access_key=access_key, secret_key=secret_key)
            credentials = self.read()
        proof = json.dumps(credentials, sort_keys=True, separators=(",", ":")).encode()
        return {
            "identity": _IDENTITY_NAME,
            "present": True,
            "sha256": hashlib.sha256(proof).hexdigest(),
        }

    def replace(self, *, access_key: str, secret_key: str) -> dict[str, object]:
        """Atomically publish source-owned credentials for a restored bundled volume."""
        if is_missing_or_placeholder_secret(access_key) or is_missing_or_placeholder_secret(
            secret_key
        ):
            raise ValueError("Bundled storage identity credentials are incomplete")
        self._write(access_key=access_key, secret_key=secret_key)
        credentials = self.read()
        proof = json.dumps(credentials, sort_keys=True, separators=(",", ":")).encode()
        return {
            "identity": _IDENTITY_NAME,
            "present": True,
            "sha256": hashlib.sha256(proof).hexdigest(),
        }

    def _write(self, *, access_key: str, secret_key: str) -> None:
        payload = {
            "identities": [
                {
                    "name": _IDENTITY_NAME,
                    "credentials": [{"accessKey": access_key, "secretKey": secret_key}],
                    "actions": ["Admin", "Read", "Write", "List", "Tagging"],
                }
            ]
        }
        _atomic_write_text(
            self.path,
            json.dumps(payload, separators=(",", ":")) + "\n",
            mode=0o600,
        )


__all__ = ["BundledStorageIdentityService"]
