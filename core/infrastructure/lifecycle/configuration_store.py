"""Owner-only active, pending, snapshot, and generated configuration storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.application.installer.contracts import InstallConfig
from core.application.lifecycle.contracts import (
    ConfigurationPatch,
    DeploymentConfig,
)
from core.infrastructure.config.env_registry import (
    StackEnvSelection,
    build_lifecycle_env,
    image_pin_keys,
)
from core.infrastructure.installer.state_store import _assert_secret_free, _atomic_write_text
from core.infrastructure.installer.storage_identity import BundledStorageIdentityService
from core.infrastructure.lifecycle.configuration_files import _PATH_FLOW_STEWARD, _SAFE_OPERATION_ID

MANAGED_BEGIN = "# BEGIN FLOW STEWARD INSTALLER MANAGED"
MANAGED_END = "# END FLOW STEWARD INSTALLER MANAGED"
_SAFE_COMPOSE_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_COMPOSE_IDENTITY_KEYS = frozenset({"FS_COMPOSE_PROJECT_NAME", "COMPOSE_PROJECT_NAME"})
# AC17-R152: the uid:gid every service runs as (`${FS_HOST_UID:-1000}:...`). The
# installer records the operator's in its managed block; this store rewrote that block
# without them, so on any host whose operator is not uid 1000 `config apply` started its
# candidates as 1000, they could not open their own logs or runtime state, and every
# apply rolled back. They are the installation's, not the configuration's to change.
_HOST_IDENTITY_KEYS = frozenset({"FS_HOST_UID", "FS_HOST_GID"})
# AC17-R153: configuration fields added after the shape the previous release wrote. A
# file from that release lacks them, and must read with their defaults: without this,
# adding worker_replicas made every existing stack.toml look non-canonical and every
# installer journal look truncated, so each installation's update and every `config`
# command failed. Anything else missing is still a truncated file, and an unknown key
# is still refused. A field added from now on must be listed here, with a default --
# tests/unit/lifecycle/test_previous_release_configuration.py fails until it is.
_FIELDS_ADDED_SINCE_PREVIOUS_SHAPE = frozenset(
    {"worker_replicas", "data_placement", "operator_settings"}
)
_PRESERVED_ENVIRONMENT_KEYS = frozenset(
    {
        *_HOST_IDENTITY_KEYS,
        "FS_SECRET_KEY",
        "ENCRYPTION_KEY",
        "FS_BACKUP_ENCRYPTION_KEY",
        "POSTGRES_PASSWORD",
        "RABBITMQ_PASS",
        "FS_METRICS_TOKEN",
        "SECURITY_PII_SALT",
        "ARTIFACT_S3_ACCESS_KEY",
        "ARTIFACT_S3_SECRET_KEY",
        *_COMPOSE_IDENTITY_KEYS,
    }
)
_REPORTED_SECRET_KEYS = tuple(
    sorted(_PRESERVED_ENVIRONMENT_KEYS - _COMPOSE_IDENTITY_KEYS - _HOST_IDENTITY_KEYS)
)
_TRANSITION_PROTECTED_ENVIRONMENT_KEYS = frozenset(
    (*_PRESERVED_ENVIRONMENT_KEYS, *image_pin_keys())
)
_STORAGE_SECRET_ENVIRONMENT_KEYS = frozenset(
    {"ARTIFACT_S3_ACCESS_KEY", "ARTIFACT_S3_SECRET_KEY", "AWS_SESSION_TOKEN"}
)
_STORAGE_CREDENTIALS_BEGIN = "# BEGIN FLOW STEWARD STORAGE CREDENTIALS"
_STORAGE_CREDENTIALS_END = "# END FLOW STEWARD STORAGE CREDENTIALS"


class LifecycleConfigurationStore:
    """Persist the single installed configuration and a secret-free pending patch."""

    def __init__(self, project_root: Path, *, state_root: Path | None = None) -> None:
        root = Path(project_root).resolve()
        self.project_root = root.parent if root.name == _PATH_FLOW_STEWARD else root
        if state_root is None:
            self.state_root = root if root.name == _PATH_FLOW_STEWARD else root / _PATH_FLOW_STEWARD
        else:
            self.state_root = Path(state_root).resolve()
        self.active_path = self.state_root / "stack.toml"
        self.pending_path = self.state_root / "pending-config.json"
        self.compose_env_path = self.state_root / "compose.env"
        self.active_publication_pending_path = self.state_root / "active-publication.pending.json"
        self.active_revision_path = self.state_root / "active-revision.json"
        self.operations_dir = self.state_root / "operations"
        self.install_journal_path = self.state_root / "install-state.json"

    def read_active(self) -> DeploymentConfig | None:
        pending = self.read_active_publication_pending()
        if pending is not None:
            return DeploymentConfig.model_validate(pending["previous"])
        active = self._read_active_file()
        if (
            active is not None
            and self.active_revision_path.exists()
            and not self.active_publication_matches(active)
        ):
            raise ValueError("Active publication revision does not match canonical files")
        return active

    def _read_active_file(self) -> DeploymentConfig | None:
        if not self.active_path.exists():
            return None
        raw = tomllib.loads(self.active_path.read_text(encoding="utf-8"))
        legacy = {} if _is_complete_canonical_stack(raw) else self._installer_journal_config()
        values = {**legacy, **raw}
        values["environment"] = _canonical_environment(values.get("environment"))
        values["file_storage"] = _canonical_file_storage(values.get("file_storage"))
        if "development_tools" not in raw:
            values["development_tools"] = tuple(
                name
                for name, enabled in (
                    ("test_runner", raw.get("test_runner", False)),
                    ("audit_tools", raw.get("audit_tools", False)),
                    ("sonarqube", raw.get("quality_tools", False)),
                )
                if bool(enabled)
            )
        allowed = {
            name: value for name, value in values.items() if name in DeploymentConfig.model_fields
        }
        return DeploymentConfig.model_validate(allowed)

    def read_active_publication_pending(self) -> dict[str, Any] | None:
        if not self.active_publication_pending_path.exists():
            return None
        payload = json.loads(self.active_publication_pending_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Active publication pending snapshot has an invalid shape")
        state = payload.get("state")
        expected: set[str] = set()
        if state == "prepared":
            expected = {
                "schema_version",
                "state",
                "pending_revision",
                "previous",
                "candidate",
                "target_env_path",
                "target_env_sha256",
                "compose_values",
            }
        elif state == "committed":
            expected = {
                "schema_version",
                "state",
                "pending_revision",
                "revision",
                "previous",
                "candidate",
                "stack_sha256",
                "target_env_path",
                "target_env_sha256",
                "compose_env_sha256",
            }
        if set(payload) != expected:
            raise ValueError("Active publication pending snapshot has an invalid shape")
        if payload.get("schema_version") != 2:
            raise ValueError("Active publication pending snapshot has an invalid version")
        DeploymentConfig.model_validate(payload["previous"])
        DeploymentConfig.model_validate(payload["candidate"])
        if state == "prepared" and not isinstance(payload.get("compose_values"), dict):
            raise ValueError("Active publication pending snapshot has invalid Compose values")
        return payload

    def begin_active_publication(
        self,
        previous: DeploymentConfig,
        candidate: DeploymentConfig,
        *,
        target_env_path: Path,
        compose_values: Mapping[str, str],
    ) -> str:
        target_path = Path(target_env_path).resolve()
        try:
            relative_target = target_path.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError("Active publication target environment escapes the project") from exc
        if not target_path.is_file() or target_path.is_symlink():
            raise ValueError("Active publication target environment is unavailable")
        descriptor = {
            "previous": previous.model_dump(mode="json"),
            "candidate": candidate.model_dump(mode="json"),
            "target_env_path": relative_target.as_posix(),
            "target_env_sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
            "compose_values": {str(key): str(value) for key, value in compose_values.items()},
        }
        _assert_secret_free(descriptor)
        pending_revision = hashlib.sha256(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        pending = {
            "schema_version": 2,
            "state": "prepared",
            "pending_revision": pending_revision,
            **descriptor,
        }
        existing = self.read_active_publication_pending()
        if existing is not None:
            stable_fields = {
                "schema_version",
                "previous",
                "candidate",
                "target_env_path",
            }
            if existing.get("state") == "prepared":
                stable_fields.add("compose_values")
            if any(existing.get(field) != pending.get(field) for field in stable_fields):
                raise ValueError("A different active publication is already pending")
            return str(existing["pending_revision"])
        _atomic_write_text(
            self.active_publication_pending_path,
            json.dumps(pending, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )
        return pending_revision

    def commit_active_publication(
        self,
        candidate: DeploymentConfig,
        *,
        revision: str,
        target_env_path: Path,
    ) -> None:
        target_path = Path(target_env_path).resolve()
        pending = self.read_active_publication_pending()
        if pending is None or pending.get("pending_revision") != revision:
            raise ValueError("Active publication pending revision is unavailable")
        previous = DeploymentConfig.model_validate(pending["previous"])
        pending_candidate = DeploymentConfig.model_validate(pending["candidate"])
        if pending_candidate != candidate:
            raise ValueError("Active publication candidate changed before commit")
        marker_core = {
            "schema_version": 2,
            "state": "committed",
            "pending_revision": revision,
            "previous": previous.model_dump(mode="json"),
            "candidate": candidate.model_dump(mode="json"),
            "stack_sha256": hashlib.sha256(self.active_path.read_bytes()).hexdigest(),
            "target_env_path": target_path.relative_to(self.project_root).as_posix(),
            "target_env_sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
            "compose_env_sha256": hashlib.sha256(self.compose_env_path.read_bytes()).hexdigest(),
        }
        marker = {
            **marker_core,
            "revision": hashlib.sha256(
                json.dumps(marker_core, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        _assert_secret_free(marker)
        _atomic_write_text(
            self.active_publication_pending_path,
            json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )
        os.replace(self.active_publication_pending_path, self.active_revision_path)
        directory_fd = os.open(self.state_root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def refresh_active_environment(self, config: DeploymentConfig, *, env_path: Path) -> None:
        """Refresh the committed publication after an owner changed only bootstrap env keys.

        The caller holds installation/lifecycle locks and snapshots this marker. Never
        publish pending configuration or remove the integrity marker to hide a mismatch.
        """
        if self.active_publication_pending_path.exists():
            raise ValueError("Active configuration publication is unresolved")
        if not self.active_revision_path.exists():
            return
        config = DeploymentConfig.model_validate(config.model_dump(mode="json"))
        if self._read_active_file() != config:
            raise ValueError("Active configuration changed during bootstrap publication")
        marker = json.loads(self.active_revision_path.read_text())
        target = (self.project_root / marker["target_env_path"]).resolve()
        if target != env_path.resolve():
            raise ValueError("Bootstrap environment differs from the active publication")
        marker["target_env_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        core = {key: value for key, value in marker.items() if key != "revision"}
        marker["revision"] = hashlib.sha256(
            json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        _atomic_write_text(
            self.active_revision_path, json.dumps(marker, sort_keys=True) + "\n", mode=0o600
        )
        if not self.active_publication_matches(config):
            raise ValueError("Bootstrap publication failed canonical verification")

    def active_publication_matches(self, candidate: DeploymentConfig) -> bool:
        try:
            if self.active_publication_pending_path.exists():
                return False
            marker = json.loads(self.active_revision_path.read_text(encoding="utf-8"))
            if not isinstance(marker, dict) or set(marker) != {
                "schema_version",
                "state",
                "revision",
                "pending_revision",
                "previous",
                "candidate",
                "stack_sha256",
                "target_env_path",
                "target_env_sha256",
                "compose_env_sha256",
            }:
                return False
            if (
                marker.get("schema_version") != 2
                or marker.get("state") != "committed"
                or DeploymentConfig.model_validate(marker.get("candidate")) != candidate
            ):
                return False
            marker_core = {key: value for key, value in marker.items() if key != "revision"}
            if (
                hashlib.sha256(
                    json.dumps(marker_core, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                != marker["revision"]
            ):
                return False
            target_path = (self.project_root / str(marker["target_env_path"])).resolve()
            target_path.relative_to(self.project_root)
            return bool(
                self._read_active_file() == candidate
                and hashlib.sha256(self.active_path.read_bytes()).hexdigest()
                == marker["stack_sha256"]
                and target_path.is_file()
                and not target_path.is_symlink()
                and hashlib.sha256(target_path.read_bytes()).hexdigest()
                == marker["target_env_sha256"]
                and hashlib.sha256(self.compose_env_path.read_bytes()).hexdigest()
                == marker["compose_env_sha256"]
            )
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def write_active(self, config: DeploymentConfig) -> None:
        canonical = DeploymentConfig.model_validate(config.model_dump(mode="json"))
        _assert_secret_free(canonical.model_dump(mode="json"))
        # What the model serializes: a field it leaves out (data_placement where it is the
        # profile's own) is not written for the previous release to refuse.
        serialized = canonical.model_dump(mode="json")
        lines = ["# Flow Steward canonical installed configuration."]
        for name in DeploymentConfig.model_fields:
            if name not in serialized:
                continue
            value = getattr(canonical, name)
            lines.append(f"{name} = {_toml_value(value)}")
        _atomic_write_text(self.active_path, "\n".join(lines) + "\n", mode=0o600)

    def read_pending(self) -> ConfigurationPatch | None:
        if not self.pending_path.exists():
            return None
        return ConfigurationPatch.model_validate_json(self.pending_path.read_text(encoding="utf-8"))

    def read_pending_with_revision(self) -> tuple[ConfigurationPatch, str] | None:
        if not self.pending_path.exists():
            return None
        payload = self.pending_path.read_bytes()
        patch = ConfigurationPatch.model_validate_json(payload)
        return patch, hashlib.sha256(payload).hexdigest()

    def pending_revision_matches(self, revision: str) -> bool:
        if not self.pending_path.exists():
            return False
        return hashlib.sha256(self.pending_path.read_bytes()).hexdigest() == revision

    def clear_pending_if_revision(self, revision: str) -> bool:
        if not self.pending_revision_matches(revision):
            return False
        self.pending_path.unlink()
        return True

    def write_pending(self, patch: ConfigurationPatch) -> None:
        payload = patch.model_dump(mode="json", exclude_none=True)
        _assert_secret_free(payload)
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_write_text(self.pending_path, text, mode=0o600)

    def clear_pending(self) -> None:
        self.pending_path.unlink(missing_ok=True)

    def snapshot_path(self, operation_id: str) -> Path:
        if not _SAFE_OPERATION_ID.fullmatch(str(operation_id)):
            raise ValueError(
                "Configuration operation id must contain only safe identifier characters"
            )
        return self.operations_dir / f"{operation_id}.previous-config.json"

    def save_snapshot(self, operation_id: str, config: DeploymentConfig) -> Path:
        path = self.snapshot_path(operation_id)
        payload = config.model_dump(mode="json")
        _assert_secret_free(payload)
        _atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )
        return path

    def env_path(self, config: DeploymentConfig) -> Path:
        return self.project_root / (".env" if config.environment == "development" else ".env.prod")

    def read_env_values(self, path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        if not path.exists():
            return values
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw = stripped.split("=", 1)
            values[key.strip()] = raw.strip().strip('"').strip("'")
        return values

    def read_durable_environment(self, config: DeploymentConfig) -> dict[str, str]:
        """Hydrate runtime env plus the installer-owned storage identity."""
        values = self.read_env_values(self.env_path(config))
        compose_values = self.read_env_values(self.compose_env_path)
        for key in _STORAGE_SECRET_ENVIRONMENT_KEYS:
            durable_value = str(compose_values.get(key) or "").strip()
            if durable_value:
                values[key] = durable_value
        if config.file_storage == "bundled":
            identity_path = self.state_root / "runtime/seaweedfs-s3.json"
            try:
                credentials = BundledStorageIdentityService(identity_path).read()
            except ValueError:
                credentials = {}
            access_key = str(credentials.get("aws_access_key_id") or "").strip()
            secret_key = str(credentials.get("aws_secret_access_key") or "").strip()
            if access_key and secret_key:
                values["ARTIFACT_S3_ACCESS_KEY"] = access_key
                values["ARTIFACT_S3_SECRET_KEY"] = secret_key
                session_token = str(credentials.get("aws_session_token") or "").strip()
                if session_token:
                    values["AWS_SESSION_TOKEN"] = session_token
        return values

    def build_environment(
        self,
        config: DeploymentConfig,
        existing: Mapping[str, str],
    ) -> dict[str, str]:
        return build_lifecycle_env(
            selection=StackEnvSelection(
                environment=config.environment,
                graph_storage="postgres_age",
                vector_search="pgvector",
                file_storage=config.file_storage,
                test_runner="test_runner" in config.development_tools,
                audit_tools="audit_tools" in config.development_tools,
                quality_tools="sonarqube" in config.development_tools,
            ),
            existing=existing,
            deployment_profile=config.deployment_profile,
        )

    def write_runtime_environment(self, path: Path, managed: Mapping[str, str]) -> None:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        managed_text = _env_lines(managed)
        rendered = _replace_managed_block(
            current,
            managed_text,
            preserve_keys=_PRESERVED_ENVIRONMENT_KEYS,
        )
        _atomic_write_text(path, rendered, mode=0o600)

    def write_transition_runtime_environment(
        self,
        path: Path,
        managed: Mapping[str, str],
        *,
        source_path: Path,
    ) -> None:
        """Stage a target from its active source without trusting stale target secrets."""
        rendered = self._render_transition_runtime_environment(source_path, managed)
        _atomic_write_text(path, rendered, mode=0o600)

    def transition_runtime_environment_matches(
        self,
        path: Path,
        managed: Mapping[str, str],
        *,
        source_path: Path,
        allow_installed_storage_credentials: bool = False,
    ) -> bool:
        """Return whether the target is exactly the deterministic transition render."""
        if not path.exists():
            return False
        expected = self._render_transition_runtime_environment(source_path, managed)
        actual = path.read_text(encoding="utf-8")
        if allow_installed_storage_credentials:
            expected = _without_storage_credentials(expected)
            actual = _without_storage_credentials(actual)
        return actual == expected

    @staticmethod
    def _render_transition_runtime_environment(
        source_path: Path,
        managed: Mapping[str, str],
    ) -> str:
        source = source_path.read_text(encoding="utf-8") if source_path.exists() else ""
        return _replace_managed_block(
            source,
            _env_lines(managed),
            preserve_keys=_TRANSITION_PROTECTED_ENVIRONMENT_KEYS,
        )

    def write_compose_environment(self, values: Mapping[str, str]) -> None:
        _assert_secret_free(values)
        current = (
            self.compose_env_path.read_text(encoding="utf-8")
            if self.compose_env_path.exists()
            else ""
        )
        preserved_identity_lines = _environment_lines_for_keys(current, _COMPOSE_IDENTITY_KEYS)
        existing = self.read_env_values(self.compose_env_path)
        merged = {
            name: value for name, value in existing.items() if name not in _COMPOSE_IDENTITY_KEYS
        }
        merged.update(
            {name: value for name, value in values.items() if name not in _COMPOSE_IDENTITY_KEYS}
        )
        text = "\n".join((*preserved_identity_lines, _env_lines(merged).rstrip(), ""))
        _atomic_write_text(self.compose_env_path, text, mode=0o600)

    def project_name(self, config: DeploymentConfig) -> str:
        return self._project_name_from_paths(
            (
                self.env_path(config),
                self.compose_env_path,
            )
        )

    def transition_project_name(self, active: DeploymentConfig) -> str:
        """Resolve identity from the committed source, never the opposite target env."""
        return self._project_name_from_paths((self.env_path(active), self.compose_env_path))

    @staticmethod
    def _project_name_from_paths(paths: tuple[Path, ...]) -> str:
        identities: set[str] = set()
        for path in paths:
            for identity in _compose_identity_values(path):
                if not identity:
                    raise ValueError("Installed Compose project identity is blank")
                if not _SAFE_COMPOSE_IDENTITY.fullmatch(identity):
                    raise ValueError("Installed Compose project identity has unsafe syntax")
                identities.add(identity)
        if not identities:
            raise ValueError("Installed Compose project identity is unavailable")
        if len(identities) > 1:
            raise ValueError(
                "Configured Compose project identities disagree; lifecycle configuration "
                "will not change the active deployment."
            )
        return next(iter(identities))

    def resolve_project_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        return path if path.is_absolute() else self.project_root / path

    def secret_statuses(self, config: DeploymentConfig) -> dict[str, bool]:
        """Report only whether each durable secret is non-empty."""
        path = self.env_path(config)
        statuses = dict.fromkeys(_REPORTED_SECRET_KEYS, False)
        if not path.exists():
            return statuses
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw = stripped.split("=", 1)
            name = key.strip()
            if name in statuses:
                statuses[name] = bool(raw.strip().strip('"').strip("'"))
        return statuses

    def _installer_journal_config(self) -> dict[str, Any]:
        if not self.install_journal_path.exists():
            return {}
        payload = json.loads(self.install_journal_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Legacy installer state must be a JSON object")
        config = payload.get("config") if isinstance(payload, dict) else None
        if not isinstance(config, dict):
            raise ValueError("Legacy installer state is missing its configuration object")
        known_fields = set(InstallConfig.model_fields)
        required_fields = known_fields - _FIELDS_ADDED_SINCE_PREVIOUS_SHAPE
        actual_fields = set(config)
        if not required_fields <= actual_fields or not actual_fields <= known_fields:
            missing = ", ".join(sorted(required_fields - actual_fields)) or "none"
            unexpected = ", ".join(sorted(actual_fields - known_fields)) or "none"
            raise ValueError(
                "Legacy installer configuration does not match the complete serialized shape "
                f"(missing: {missing}; unexpected: {unexpected})"
            )
        normalized = dict(config)
        normalized["environment"] = _canonical_environment(normalized.get("environment"))
        normalized["file_storage"] = _canonical_file_storage(normalized.get("file_storage"))
        return InstallConfig.model_validate(normalized).model_dump(mode="json")


ConfigurationStore = LifecycleConfigurationStore


def _is_complete_canonical_stack(raw: Mapping[str, object]) -> bool:
    return (set(DeploymentConfig.model_fields) - _FIELDS_ADDED_SINCE_PREVIOUS_SHAPE).issubset(raw)


def _canonical_environment(value: object) -> str:
    normalized = str(value or "development").strip().lower().replace("-", "_")
    if normalized in {"development", "local_development"}:
        return "development"
    if normalized == "production":
        return "production"
    return normalized


def _canonical_file_storage(value: object) -> str:
    normalized = str(value or "bundled").strip().lower().replace("-", "_")
    if normalized in {"bundled", "bundled_seaweedfs"}:
        return "bundled"
    if normalized == "external_s3":
        return "external_s3"
    return normalized


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(json.dumps(str(item)) for item in value) + "]"
    if isinstance(value, dict):
        # An inline table: host settings, NAME = "value".
        return (
            "{ "
            + ", ".join(f"{name} = {json.dumps(str(item))}" for name, item in sorted(value.items()))
            + " }"
        )
    return json.dumps(str(value))


def _env_lines(values: Mapping[str, str]) -> str:
    return (
        "\n".join(f"{name}={shlex.quote(str(value))}" for name, value in sorted(values.items()))
        + "\n"
    )


def _replace_managed_block(
    text: str,
    managed: str,
    *,
    preserve_keys: frozenset[str],
) -> str:
    lines = text.splitlines()
    managed_source_lines = managed.splitlines()
    managed_keys = {_environment_key(line) for line in managed_source_lines}
    retained: list[str] = []
    preserved: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == MANAGED_BEGIN:
            inside = True
            continue
        if line.strip() == MANAGED_END:
            inside = False
            continue
        if inside and _environment_key(line) in preserve_keys:
            preserved.append(line)
            continue
        if not inside:
            key = _environment_key(line)
            if key in managed_keys and key not in preserve_keys:
                continue
            retained.append(line)
    while retained and not retained[-1].strip():
        retained.pop()
    if retained:
        retained.append("")
    managed_lines = [
        line for line in managed_source_lines if _environment_key(line) not in preserve_keys
    ]
    retained.extend((MANAGED_BEGIN, *preserved, *managed_lines, MANAGED_END, ""))
    return "\n".join(retained)


def _environment_lines_for_keys(text: str, keys: frozenset[str]) -> tuple[str, ...]:
    return tuple(line for line in text.splitlines() if _environment_key(line) in keys)


def _environment_key(line: str) -> str:
    stripped = line.strip()
    return stripped.split("=", 1)[0].strip() if "=" in stripped else ""


def _without_storage_credentials(text: str) -> str:
    """Remove only storage-owned secret records before a transition comparison."""
    retained: list[str] = []
    inside_storage_credentials = False
    for line in text.splitlines():
        if line.strip() == _STORAGE_CREDENTIALS_BEGIN:
            inside_storage_credentials = True
            continue
        if line.strip() == _STORAGE_CREDENTIALS_END:
            inside_storage_credentials = False
            continue
        if inside_storage_credentials or _environment_key(line) in _STORAGE_SECRET_ENVIRONMENT_KEYS:
            continue
        retained.append(line)
    while retained and not retained[-1].strip():
        retained.pop()
    return "\n".join((*retained, ""))


def _compose_identity_values(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    values: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        if key.strip() in _COMPOSE_IDENTITY_KEYS:
            values.append(raw.strip().strip('"').strip("'"))
    return tuple(values)
