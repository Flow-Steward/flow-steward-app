"""Credential-safe inventory and write probes for lifecycle storage transitions."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from core.application.lifecycle.contracts import DeploymentConfig
from core.application.lifecycle.environment_service import StorageInventory
from core.infrastructure.installer.state_store import _atomic_write_text
from core.infrastructure.installer.storage_identity import BundledStorageIdentityService

_MESSAGE_EXPORT = "export "

_CREDENTIAL_KEYS = frozenset(
    {
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
    }
)
_PROBE_PREFIX = ".flow-steward/probes"
_MAX_INVENTORY_OBJECTS = 1_000_000
_STORAGE_CREDENTIALS_BEGIN = "# BEGIN FLOW STEWARD STORAGE CREDENTIALS"
_STORAGE_CREDENTIALS_END = "# END FLOW STEWARD STORAGE CREDENTIALS"
_STORAGE_SECRET_VARIABLES = frozenset(
    {
        "ARTIFACT_S3_ACCESS_KEY",
        "ARTIFACT_S3_SECRET_KEY",
        "AWS_SESSION_TOKEN",
    }
)
_STORAGE_ENVIRONMENT_VARIABLES = frozenset(
    {
        "ARTIFACT_S3_ENDPOINT_URL",
        "ARTIFACT_S3_BUCKET",
        "ARTIFACT_S3_REGION",
        "ARTIFACT_S3_USE_SSL",
        *_STORAGE_SECRET_VARIABLES,
    }
)
_BUNDLED_STORAGE_ENVIRONMENT = {
    "ARTIFACT_S3_ENDPOINT_URL": "http://seaweedfs:8333",
    "ARTIFACT_S3_BUCKET": "artifacts",
    "ARTIFACT_S3_REGION": "us-east-1",
    "ARTIFACT_S3_USE_SSL": "false",
}
_DEFERRED_BUNDLED_ACCESS = "flow_steward_deferred_bundled_access"
_DEFERRED_BUNDLED_SECRET = "flow_steward_deferred_bundled_secret"


class LifecycleStorageGateway:
    """Keep candidate credentials in memory and expose only non-secret identities."""

    def __init__(
        self,
        *,
        project_root: Path,
        session_factory: Callable[..., Any] | None = None,
        bundled_inventory: Callable[[DeploymentConfig], StorageInventory] | None = None,
        bundled_write_probe: Callable[[DeploymentConfig], None] | None = None,
        bundled_probe_prepare: Callable[[DeploymentConfig], None] | None = None,
        compose_project_provider: Callable[[DeploymentConfig], str] | None = None,
        probe_id_factory: Callable[[], str] | None = None,
        secret_factory: Callable[[], str] | None = None,
    ) -> None:
        self._project_root = Path(project_root).resolve()
        self._bundled_credentials_path = (
            self._project_root / ".flow-steward/runtime/seaweedfs-s3.json"
        )
        self._bundled_identity = BundledStorageIdentityService(
            self._bundled_credentials_path,
            secret_factory=secret_factory,
        )
        self._session_factory = session_factory
        self._bundled_inventory = bundled_inventory
        self._bundled_write_probe = bundled_write_probe
        self._bundled_probe_prepare = bundled_probe_prepare
        self._compose_project_provider = compose_project_provider or (
            lambda _config: "flow-steward"
        )
        self._probe_id_factory = probe_id_factory or (lambda: uuid4().hex)

    def target_identity(self, config: DeploymentConfig) -> str:
        if config.file_storage == "bundled":
            project = str(self._compose_project_provider(config)).strip()
            if not project:
                raise ValueError("Compose project identity is unavailable")
            return f"volume:{project}_seaweedfs-data"
        return "s3:" + json.dumps(
            self._external_identity_fields(config),
            sort_keys=True,
            separators=(",", ":"),
        )

    def candidate_environment(
        self,
        config: DeploymentConfig,
        environment: Mapping[str, str],
    ) -> dict[str, str]:
        candidate = {str(key): str(value) for key, value in environment.items()}
        if config.file_storage == "bundled":
            candidate = {
                key: value
                for key, value in candidate.items()
                if key not in _STORAGE_ENVIRONMENT_VARIABLES
            }
            candidate.update(_BUNDLED_STORAGE_ENVIRONMENT)
            try:
                credentials = self._bundled_credentials(config)
            except ValueError:
                credentials = (
                    {
                        "aws_access_key_id": _DEFERRED_BUNDLED_ACCESS,
                        "aws_secret_access_key": _DEFERRED_BUNDLED_SECRET,
                    }
                    if config.environment == "production"
                    else {}
                )
            if credentials:
                candidate.update(
                    {
                        "ARTIFACT_S3_ACCESS_KEY": credentials["aws_access_key_id"],
                        "ARTIFACT_S3_SECRET_KEY": credentials["aws_secret_access_key"],
                    }
                )
            return candidate
        credentials = self._storage_credentials(config)
        candidate.update(
            {
                "ARTIFACT_S3_ENDPOINT_URL": config.s3_endpoint,
                "ARTIFACT_S3_BUCKET": config.s3_bucket,
                "ARTIFACT_S3_REGION": config.s3_region,
                "ARTIFACT_S3_USE_SSL": (
                    "true" if config.s3_endpoint.lower().startswith("https://") else "false"
                ),
                "ARTIFACT_S3_ACCESS_KEY": credentials["aws_access_key_id"],
                "ARTIFACT_S3_SECRET_KEY": credentials["aws_secret_access_key"],
            }
        )
        session_token = credentials.get("aws_session_token", "")
        if session_token:
            candidate["AWS_SESSION_TOKEN"] = session_token
        return candidate

    def provision_credentials(self, config: DeploymentConfig) -> dict[str, object]:
        """Provision a missing bundled identity under the lifecycle lock."""
        if config.file_storage == "bundled":
            return self._bundled_identity.ensure()
        return {"identity": "flow-steward-core", "present": False, "sha256": ""}

    def provisioning_matches(self, config: DeploymentConfig) -> bool:
        if config.file_storage != "bundled":
            return True
        try:
            self._bundled_identity.read()
        except ValueError:
            return False
        return True

    def inventory(self, config: DeploymentConfig) -> StorageInventory:
        identity = self.target_identity(config)
        if config.file_storage == "bundled" and self._bundled_inventory is not None:
            try:
                return self._bundled_inventory(config)
            except (OSError, RuntimeError, ValueError):
                return StorageInventory(
                    backend="bundled",
                    source_identity=identity,
                    completeness="unknown",
                )
        try:
            environment = self.candidate_environment(config, {})
            client = self._client(config, environment)
            bucket = str(environment.get("ARTIFACT_S3_BUCKET") or config.s3_bucket).strip()
            if not bucket:
                raise ValueError("Storage inventory bucket is unavailable")
            client.head_bucket(Bucket=bucket)
            count = 0
            size = 0
            complete = True
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket):
                contents = page.get("Contents", ())
                if not isinstance(contents, (list, tuple)):
                    raise ValueError("S3 inventory page has invalid contents")
                for item in contents:
                    if not isinstance(item, Mapping):
                        raise ValueError("S3 inventory object has an invalid shape")
                    count += 1
                    size += max(0, int(item.get("Size") or 0))
                    if count >= _MAX_INVENTORY_OBJECTS:
                        complete = False
                        break
                if not complete:
                    break
            return StorageInventory(
                backend=config.file_storage,
                approximate_count=count,
                approximate_size_bytes=size,
                source_identity=identity,
                completeness="complete" if complete else "partial",
            )
        except (*self._client_errors(), OSError, RuntimeError, ValueError):
            return StorageInventory(
                backend=config.file_storage,
                source_identity=identity,
                completeness="unknown",
            )

    def probe_write(
        self,
        config: DeploymentConfig,
        environment: Mapping[str, str],
    ) -> None:
        if config.file_storage == "bundled":
            if self._bundled_probe_prepare is not None:
                self._bundled_probe_prepare(config)
            if self._bundled_write_probe is not None:
                self._bundled_write_probe(config)
                return
            environment = self.candidate_environment(config, environment)
        client = self._client(config, environment)
        bucket = str(environment.get("ARTIFACT_S3_BUCKET") or config.s3_bucket).strip()
        if not bucket:
            raise ValueError("Candidate storage bucket is unavailable")
        probe_id = self._probe_id_factory()
        key = f"{_PROBE_PREFIX}/{probe_id}"
        payload = f"flow-steward-storage-probe:{probe_id}".encode()
        put_attempted = False
        primary_failure = False
        try:
            client.head_bucket(Bucket=bucket)
            put_attempted = True
            client.put_object(Bucket=bucket, Key=key, Body=payload)
            response = client.get_object(Bucket=bucket, Key=key)
            body = response["Body"]
            try:
                if body.read() != payload:
                    raise ValueError("Candidate storage probe payload did not match")
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except Exception as exc:
            primary_failure = True
            raise ValueError("Candidate storage write validation failed") from exc
        except BaseException:
            primary_failure = True
            raise
        finally:
            if put_attempted:
                try:
                    client.delete_object(Bucket=bucket, Key=key)
                except Exception as exc:
                    if not primary_failure:
                        raise ValueError("Candidate storage probe cleanup failed") from exc

    def install_credentials(self, config: DeploymentConfig, environment_path: Path) -> None:
        credentials = self._storage_credentials(config)
        destination = Path(environment_path)
        if not destination.is_absolute():
            destination = self._project_root / destination
        lexical = Path(os.path.abspath(destination))
        try:
            lexical.relative_to(self._project_root)
        except ValueError as exc:
            raise ValueError("Target environment path escapes the project root") from exc
        if lexical.is_symlink():
            raise ValueError("Target environment path must not be a symlink")
        if lexical.parent.resolve(strict=True) != lexical.parent:
            raise ValueError("Target environment parent must not traverse a symlink")
        current = lexical.read_text(encoding="utf-8") if lexical.exists() else ""
        retained: list[str] = []
        inside = False
        for line in current.splitlines():
            if line.strip() == _STORAGE_CREDENTIALS_BEGIN:
                inside = True
                continue
            if line.strip() == _STORAGE_CREDENTIALS_END:
                inside = False
                continue
            assignment = line.lstrip()
            if assignment.startswith(_MESSAGE_EXPORT):
                assignment = assignment.removeprefix(_MESSAGE_EXPORT).lstrip()
            key = assignment.partition("=")[0].strip() if "=" in assignment else ""
            if not inside and key not in _STORAGE_SECRET_VARIABLES:
                retained.append(line)
        while retained and not retained[-1].strip():
            retained.pop()
        if retained:
            retained.append("")
        if credentials:
            retained.extend(
                (
                    _STORAGE_CREDENTIALS_BEGIN,
                    f"ARTIFACT_S3_ACCESS_KEY={shlex.quote(credentials['aws_access_key_id'])}",
                    f"ARTIFACT_S3_SECRET_KEY={shlex.quote(credentials['aws_secret_access_key'])}",
                )
            )
            if credentials.get("aws_session_token"):
                retained.append(
                    f"AWS_SESSION_TOKEN={shlex.quote(credentials['aws_session_token'])}"
                )
            retained.append(_STORAGE_CREDENTIALS_END)
        retained.append("")
        _atomic_write_text(lexical, "\n".join(retained), mode=0o600)

    def credentials_match(self, config: DeploymentConfig, environment_path: Path) -> bool:
        try:
            expected = self._storage_credentials(config)
            actual, counts = _storage_secret_assignments(Path(environment_path))
        except (OSError, ValueError):
            return False
        expected_values = {
            "ARTIFACT_S3_ACCESS_KEY": expected.get("aws_access_key_id", ""),
            "ARTIFACT_S3_SECRET_KEY": expected.get("aws_secret_access_key", ""),
            "AWS_SESSION_TOKEN": expected.get("aws_session_token", ""),
        }
        return all(
            actual.get(key, "") == value and counts.get(key, 0) == (1 if value else 0)
            for key, value in expected_values.items()
        )

    def credential_evidence(
        self,
        config: DeploymentConfig,
        environment_path: Path,
    ) -> dict[str, object]:
        if not self.credentials_match(config, environment_path):
            raise ValueError("Installed storage credentials do not match the candidate")
        expected = self._storage_credentials(config)
        values = {
            "ARTIFACT_S3_ACCESS_KEY": expected.get("aws_access_key_id", ""),
            "ARTIFACT_S3_SECRET_KEY": expected.get("aws_secret_access_key", ""),
            "AWS_SESSION_TOKEN": expected.get("aws_session_token", ""),
        }
        present = sorted(key for key, value in values.items() if value)
        payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return {"present_keys": present, "sha256": hashlib.sha256(payload).hexdigest()}

    def _storage_credentials(self, config: DeploymentConfig) -> dict[str, str]:
        if config.file_storage == "external_s3":
            return self._read_credentials(config.s3_credentials_path)
        return self._bundled_credentials(config)

    def _bundled_credentials(self, config: DeploymentConfig) -> dict[str, str]:
        del config
        return self._bundled_identity.read()

    def _read_credentials(self, raw_path: str) -> dict[str, str]:
        requested = Path(str(raw_path or "")).expanduser()
        path = requested if requested.is_absolute() else self._project_root / requested
        if not str(raw_path or "").strip() or path.is_symlink():
            raise ValueError("S3 credentials file must be a regular owner-only file")
        try:
            file_stat = path.stat()
        except OSError as exc:
            raise ValueError("S3 credentials file is unavailable") from exc
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.getuid()
            or file_stat.st_mode & 0o077
        ):
            raise ValueError("S3 credentials file must be a regular owner-only file")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("S3 credentials file must contain valid JSON") from exc
        if not isinstance(payload, dict) or not set(payload).issubset(_CREDENTIAL_KEYS):
            raise ValueError("S3 credentials file has an unsupported shape")
        access_key = str(payload.get("aws_access_key_id") or "")
        secret_key = str(payload.get("aws_secret_access_key") or "")
        session_token = str(payload.get("aws_session_token") or "")
        if not access_key or not secret_key:
            raise ValueError("S3 credentials file requires an access key and secret key")
        return {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "aws_session_token": session_token,
        }

    @staticmethod
    def _external_identity_fields(config: DeploymentConfig) -> dict[str, str]:
        endpoint = urlsplit(str(config.s3_endpoint).strip())
        scheme = endpoint.scheme.lower()
        host = str(endpoint.hostname or "").lower()
        if not scheme or not host:
            raise ValueError("External S3 identity requires an endpoint")
        try:
            port = endpoint.port
        except ValueError as exc:
            raise ValueError("External S3 identity has an invalid endpoint port") from exc
        default_port = {"http": 80, "https": 443}.get(scheme)
        normalized_host = f"[{host}]" if ":" in host else host
        authority = normalized_host
        if port is not None and port != default_port:
            authority = f"{authority}:{port}"
        path = endpoint.path.rstrip("/") or "/"
        bucket = str(config.s3_bucket).strip()
        region = str(config.s3_region).strip().lower()
        if not bucket or not region:
            raise ValueError("External S3 identity requires a bucket and region")
        return {
            "backend": "external_s3",
            "endpoint": urlunsplit((scheme, authority, path, "", "")),
            "bucket": bucket,
            "region": region,
            "tls": "enabled" if scheme == "https" else "disabled",
        }

    def _client(
        self,
        config: DeploymentConfig,
        environment: Mapping[str, str],
    ) -> Any:
        try:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config as BotoConfig
        except ImportError as exc:
            raise ValueError("Boto3 is required for S3 storage validation") from exc
        access_key = str(environment.get("ARTIFACT_S3_ACCESS_KEY") or "")
        secret_key = str(environment.get("ARTIFACT_S3_SECRET_KEY") or "")
        session_token = str(environment.get("AWS_SESSION_TOKEN") or "")
        if bool(access_key) != bool(secret_key):
            raise ValueError("Candidate S3 credentials are incomplete")
        if config.file_storage == "external_s3" and not access_key:
            raise ValueError("Candidate S3 credentials are unavailable")
        endpoint = str(environment.get("ARTIFACT_S3_ENDPOINT_URL") or config.s3_endpoint).strip()
        region = str(
            environment.get("ARTIFACT_S3_REGION") or config.s3_region or "us-east-1"
        ).strip()
        if not endpoint or not region:
            raise ValueError("Candidate S3 endpoint or region is unavailable")
        session_factory = self._session_factory or boto3.Session
        session = session_factory(
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            aws_session_token=session_token or None,
            region_name=region,
        )
        use_ssl = (
            str(
                environment.get("ARTIFACT_S3_USE_SSL")
                or ("true" if endpoint.lower().startswith("https://") else "false")
            ).lower()
            == "true"
        )
        config_options: dict[str, Any] = {
            "connect_timeout": 3,
            "read_timeout": 5,
            "retries": {"max_attempts": 0, "mode": "standard"},
            "s3": {"addressing_style": "path"},
        }
        if not access_key:
            config_options["signature_version"] = UNSIGNED
        ca_bundle = str(environment.get("AWS_CA_BUNDLE") or "").strip()
        return session.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            use_ssl=use_ssl,
            verify=(ca_bundle if use_ssl and ca_bundle else use_ssl),
            config=BotoConfig(**config_options),
        )

    @staticmethod
    def _client_errors() -> tuple[type[BaseException], ...]:
        errors: list[type[BaseException]] = [
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            KeyError,
            OverflowError,
        ]
        try:
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError:
            pass
        else:
            errors.extend((BotoCoreError, ClientError))
        return tuple(errors)


def _storage_secret_assignments(path: Path) -> tuple[dict[str, str], dict[str, int]]:
    values: dict[str, str] = {}
    counts: dict[str, int] = {}
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("Target environment is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("Target environment must be a regular file")
    for line in path.read_text(encoding="utf-8").splitlines():
        assignment = line.strip()
        if assignment.startswith(_MESSAGE_EXPORT):
            assignment = assignment.removeprefix(_MESSAGE_EXPORT).lstrip()
        if not assignment or assignment.startswith("#") or "=" not in assignment:
            continue
        key, raw = assignment.split("=", 1)
        key = key.strip()
        if key not in _STORAGE_SECRET_VARIABLES:
            continue
        parsed = shlex.split(raw, comments=False, posix=True)
        value = parsed[0] if len(parsed) == 1 else raw
        counts[key] = counts.get(key, 0) + 1
        values[key] = value
    return values, counts


__all__ = ["LifecycleStorageGateway"]
