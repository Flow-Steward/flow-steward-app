from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.application.lifecycle.contracts import DeploymentConfig
from core.infrastructure.lifecycle.storage_gateway import LifecycleStorageGateway


class _Client:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.calls = calls
        self.objects: dict[str, object] = {}

    def head_bucket(self, **kwargs: object) -> None:
        self.calls.append(("head_bucket", kwargs))

    def put_object(self, **kwargs: object) -> None:
        self.calls.append(("put_object", kwargs))
        self.objects[str(kwargs["Key"])] = kwargs["Body"]

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("get_object", kwargs))
        return {"Body": _Body(self.objects[str(kwargs["Key"])])}

    def delete_object(self, **kwargs: object) -> None:
        self.calls.append(("delete_object", kwargs))

    def get_paginator(self, name: str) -> object:
        self.calls.append(("get_paginator", name))

        class _Paginator:
            def paginate(self, **kwargs: object):
                self_calls = self
                del self_calls
                assert kwargs == {"Bucket": "artifacts"}
                return (
                    {
                        "Contents": [
                            {"Key": "one", "Size": 3},
                            {"Key": "two", "Size": 5},
                        ],
                        "IsTruncated": False,
                    },
                )

        return _Paginator()


class _Body:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def read(self) -> object:
        return self.payload


class _Session:
    def __init__(self, calls: list[tuple[str, object]], **kwargs: object) -> None:
        self.calls = calls
        calls.append(("session", kwargs))

    def client(self, service: str, **kwargs: object) -> _Client:
        self.calls.append((service, kwargs))
        return _Client(self.calls)


def _credentials(path: Path, *, mode: int = 0o600) -> None:
    path.write_text(
        json.dumps(
            {
                "aws_access_key_id": "candidate-access-value",
                "aws_secret_access_key": "candidate-secret-value",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(mode)


def _config(path: Path, **overrides: object) -> DeploymentConfig:
    values: dict[str, object] = {
        "environment": "production",
        "file_storage": "external_s3",
        "s3_endpoint": "https://objects.example.test",
        "s3_bucket": "artifacts",
        "s3_region": "eu-test-1",
        "s3_credentials_path": str(path),
    }
    values.update(overrides)
    return DeploymentConfig(**values)


def _bundled_credentials(project_root: Path) -> None:
    path = project_root / ".flow-steward/runtime/seaweedfs-s3.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "identities": [
                    {
                        "name": "flow-steward-core",
                        "credentials": [
                            {
                                "accessKey": "bundled-access-value",
                                "secretKey": "bundled-secret-value",
                            }
                        ],
                        "actions": ["Read", "Write", "List"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_candidate_credentials_are_owner_only_and_never_enter_identity(tmp_path: Path) -> None:
    credentials = tmp_path / "candidate.json"
    _credentials(credentials, mode=0o644)
    gateway = LifecycleStorageGateway(
        project_root=tmp_path,
        session_factory=lambda **kwargs: _Session([], **kwargs),
    )

    _raises_input_130_1 = _config(credentials)
    with pytest.raises(ValueError, match="owner-only"):
        gateway.candidate_environment(_raises_input_130_1, {})

    credentials.chmod(0o600)
    environment = gateway.candidate_environment(_config(credentials), {})
    identity = gateway.target_identity(_config(credentials))

    assert environment["ARTIFACT_S3_ACCESS_KEY"] == "candidate-access-value"
    assert environment["ARTIFACT_S3_SECRET_KEY"] == "candidate-secret-value"
    assert "candidate-access-value" not in identity
    assert "candidate-secret-value" not in identity


def test_bundled_identity_uses_the_installed_compose_project(tmp_path: Path) -> None:
    gateway = LifecycleStorageGateway(
        project_root=tmp_path,
        compose_project_provider=lambda _config: "installed-project",
    )

    assert (
        gateway.target_identity(DeploymentConfig(file_storage="bundled"))
        == "volume:installed-project_seaweedfs-data"
    )


def test_production_bundled_candidate_uses_dedicated_seaweed_credentials(
    tmp_path: Path,
) -> None:
    _bundled_credentials(tmp_path)
    gateway = LifecycleStorageGateway(project_root=tmp_path)

    environment = gateway.candidate_environment(
        DeploymentConfig(environment="production", file_storage="bundled"),
        {
            "ARTIFACT_S3_ENDPOINT_URL": "https://old-external.example",
            "ARTIFACT_S3_ACCESS_KEY": "old-external-access",
            "ARTIFACT_S3_SECRET_KEY": "old-external-secret",
        },
    )

    assert environment == {
        "ARTIFACT_S3_ENDPOINT_URL": "http://seaweedfs:8333",
        "ARTIFACT_S3_BUCKET": "artifacts",
        "ARTIFACT_S3_REGION": "us-east-1",
        "ARTIFACT_S3_USE_SSL": "false",
        "ARTIFACT_S3_ACCESS_KEY": "bundled-access-value",
        "ARTIFACT_S3_SECRET_KEY": "bundled-secret-value",
    }


def test_production_cli_joins_the_internal_bundled_storage_network() -> None:
    root = Path(__file__).resolve().parents[3]
    compose = (root / "infra/docker/docker-compose.prod.yaml").read_text(encoding="utf-8")
    match = re.search(
        r"^  fs-cli:\n(?P<body>.*?)(?=^  fs-worker:)", compose, re.MULTILINE | re.DOTALL
    )

    assert match is not None
    assert "- artifact-storage" in match.group("body")


def test_external_identity_normalizes_nonsecret_endpoint_bucket_region_and_tls(
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "candidate.json"
    _credentials(credentials)
    gateway = LifecycleStorageGateway(project_root=tmp_path)

    canonical = gateway.target_identity(
        _config(
            credentials,
            s3_endpoint="HTTPS://Objects.Example.Test:443/prefix///",
            s3_bucket=" artifacts ",
            s3_region=" EU-TEST-1 ",
        )
    )
    equivalent = gateway.target_identity(
        _config(
            credentials,
            s3_endpoint="https://objects.example.test/prefix",
            s3_bucket="artifacts",
            s3_region="eu-test-1",
        )
    )
    different_path = gateway.target_identity(
        _config(credentials, s3_endpoint="https://objects.example.test/other")
    )

    assert canonical == equivalent
    assert canonical == (
        's3:{"backend":"external_s3","bucket":"artifacts",'
        '"endpoint":"https://objects.example.test/prefix",'
        '"region":"eu-test-1","tls":"enabled"}'
    )
    assert canonical != different_path
    assert "candidate-access-value" not in canonical
    assert "candidate-secret-value" not in canonical


def test_external_inventory_reports_count_size_identity_and_completeness(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []
    credentials = tmp_path / "candidate.json"
    _credentials(credentials)
    gateway = LifecycleStorageGateway(
        project_root=tmp_path,
        session_factory=lambda **kwargs: _Session(calls, **kwargs),
    )

    result = gateway.inventory(_config(credentials))

    assert result.approximate_count == 2
    assert result.approximate_size_bytes == 8
    assert result.completeness == "complete"
    assert result.source_identity == (
        's3:{"backend":"external_s3","bucket":"artifacts",'
        '"endpoint":"https://objects.example.test/",'
        '"region":"eu-test-1","tls":"enabled"}'
    )
    assert ("head_bucket", {"Bucket": "artifacts"}) in calls
    assert not any(name in {"put_object", "delete_object"} for name, _value in calls)


def test_bundled_inventory_lists_actual_seaweed_bucket(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []
    _bundled_credentials(tmp_path)
    gateway = LifecycleStorageGateway(
        project_root=tmp_path,
        compose_project_provider=lambda _config: "installed-project",
        session_factory=lambda **kwargs: _Session(calls, **kwargs),
    )

    inventory = gateway.inventory(
        DeploymentConfig(environment="production", file_storage="bundled")
    )

    assert inventory.backend == "bundled"
    assert inventory.approximate_count == 2
    assert inventory.approximate_size_bytes == 8
    assert inventory.completeness == "complete"
    assert inventory.source_identity == "volume:installed-project_seaweedfs-data"
    assert [name for name, _value in calls if name in {"head_bucket", "get_paginator"}] == [
        "head_bucket",
        "get_paginator",
    ]


def test_inventory_failure_is_unknown_never_zero(tmp_path: Path) -> None:
    credentials = tmp_path / "candidate.json"
    _credentials(credentials)

    class _FailingClient(_Client):
        def head_bucket(self, **kwargs: object) -> None:
            del kwargs
            raise OSError("unavailable")

    class _FailingSession(_Session):
        def client(self, service: str, **kwargs: object) -> _Client:
            del service, kwargs
            return _FailingClient(self.calls)

    gateway = LifecycleStorageGateway(
        project_root=tmp_path,
        session_factory=lambda **kwargs: _FailingSession([], **kwargs),
    )

    result = gateway.inventory(_config(credentials))

    assert result.approximate_count is None
    assert result.approximate_size_bytes is None
    assert result.completeness == "unknown"


def test_write_probe_wraps_connectivity_failure_without_creating_an_object(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []
    credentials = tmp_path / "candidate.json"
    _credentials(credentials)

    class _FailingClient(_Client):
        def head_bucket(self, **kwargs: object) -> None:
            self.calls.append(("head_bucket", kwargs))
            raise OSError("unavailable")

    class _FailingSession(_Session):
        def client(self, service: str, **kwargs: object) -> _Client:
            del service, kwargs
            return _FailingClient(self.calls)

    gateway = LifecycleStorageGateway(
        project_root=tmp_path,
        session_factory=lambda **kwargs: _FailingSession(calls, **kwargs),
    )
    config = _config(credentials)

    _raises_input_325_1 = gateway.candidate_environment(config, {})
    with pytest.raises(ValueError, match="write validation failed"):
        gateway.probe_write(config, _raises_input_325_1)

    assert not any(name in {"put_object", "delete_object"} for name, _value in calls)


def test_write_probe_reads_back_unique_nonempty_payload_and_always_cleans_it(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []
    credentials = tmp_path / "candidate.json"
    _credentials(credentials)
    gateway = LifecycleStorageGateway(
        project_root=tmp_path,
        session_factory=lambda **kwargs: _Session(calls, **kwargs),
        probe_id_factory=lambda: "fixed-probe",
    )
    config = _config(credentials)
    environment = gateway.candidate_environment(config, {})
    environment["AWS_CA_BUNDLE"] = "/app/extensions/minio-ca.pem"

    gateway.probe_write(config, environment)

    assert dict(calls[1][1])["verify"] == "/app/extensions/minio-ca.pem"
    assert [name for name, _value in calls[-4:]] == [
        "head_bucket",
        "put_object",
        "get_object",
        "delete_object",
    ]
    put = calls[-3][1]
    assert put == {
        "Bucket": "artifacts",
        "Key": ".flow-steward/probes/fixed-probe",
        "Body": b"flow-steward-storage-probe:fixed-probe",
    }
    assert calls[-2] == (
        "get_object",
        {"Bucket": "artifacts", "Key": ".flow-steward/probes/fixed-probe"},
    )
    assert calls[-1] == (
        "delete_object",
        {"Bucket": "artifacts", "Key": ".flow-steward/probes/fixed-probe"},
    )


def test_write_probe_rejects_mismatched_read_and_still_cleans_up(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []
    credentials = tmp_path / "candidate.json"
    _credentials(credentials)

    class _MismatchedReadClient(_Client):
        def get_object(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(("get_object", kwargs))
            return {"Body": _Body(b"wrong-payload")}

    class _MismatchedReadSession(_Session):
        def client(self, service: str, **kwargs: object) -> _Client:
            del service, kwargs
            return _MismatchedReadClient(self.calls)

    gateway = LifecycleStorageGateway(
        project_root=tmp_path,
        session_factory=lambda **kwargs: _MismatchedReadSession(calls, **kwargs),
        probe_id_factory=lambda: "fixed-probe",
    )
    config = _config(credentials)

    _raises_input_393_1 = gateway.candidate_environment(config, {})
    with pytest.raises(ValueError, match="write validation failed"):
        gateway.probe_write(config, _raises_input_393_1)

    assert [name for name, _value in calls[-4:]] == [
        "head_bucket",
        "put_object",
        "get_object",
        "delete_object",
    ]


def test_write_probe_preserves_primary_failure_when_cleanup_also_fails(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []
    credentials = tmp_path / "candidate.json"
    _credentials(credentials)

    class _ReadAndCleanupFailingClient(_Client):
        def get_object(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(("get_object", kwargs))
            raise OSError("read unavailable")

        def delete_object(self, **kwargs: object) -> None:
            self.calls.append(("delete_object", kwargs))
            raise OSError("cleanup unavailable")

    class _ReadAndCleanupFailingSession(_Session):
        def client(self, service: str, **kwargs: object) -> _Client:
            del service, kwargs
            return _ReadAndCleanupFailingClient(self.calls)

    gateway = LifecycleStorageGateway(
        project_root=tmp_path,
        session_factory=lambda **kwargs: _ReadAndCleanupFailingSession(calls, **kwargs),
        probe_id_factory=lambda: "fixed-probe",
    )
    config = _config(credentials)

    _raises_input_430_1 = gateway.candidate_environment(config, {})
    with pytest.raises(ValueError, match="write validation failed"):
        gateway.probe_write(config, _raises_input_430_1)

    assert [name for name, _value in calls[-4:]] == [
        "head_bucket",
        "put_object",
        "get_object",
        "delete_object",
    ]


def test_bundled_write_probe_uses_unsigned_candidate_environment(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []
    gateway = LifecycleStorageGateway(
        project_root=tmp_path,
        session_factory=lambda **kwargs: _Session(calls, **kwargs),
        probe_id_factory=lambda: "bundled-probe",
    )

    gateway.probe_write(
        DeploymentConfig(file_storage="bundled"),
        {
            "ARTIFACT_S3_ENDPOINT_URL": "http://seaweedfs:8333",
            "ARTIFACT_S3_BUCKET": "artifacts",
            "ARTIFACT_S3_REGION": "us-east-1",
            "ARTIFACT_S3_USE_SSL": "false",
        },
    )

    session = dict(calls[0][1])
    assert session["aws_access_key_id"] is None
    assert session["aws_secret_access_key"] is None
    assert [name for name, _value in calls[-4:]] == [
        "head_bucket",
        "put_object",
        "get_object",
        "delete_object",
    ]


def test_install_credentials_preserves_unrelated_environment_and_uses_no_argv(
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "candidate.json"
    _credentials(credentials)
    target_env = tmp_path / ".env.prod"
    target_env.write_text("FS_SECRET_KEY=keep-me\n", encoding="utf-8")
    gateway = LifecycleStorageGateway(
        project_root=tmp_path,
        session_factory=lambda **kwargs: _Session([], **kwargs),
    )

    gateway.install_credentials(_config(credentials), target_env)

    text = target_env.read_text(encoding="utf-8")
    assert "FS_SECRET_KEY=keep-me" in text
    assert "ARTIFACT_S3_ACCESS_KEY=candidate-access-value" in text
    assert "ARTIFACT_S3_SECRET_KEY=candidate-secret-value" in text
    assert target_env.stat().st_mode & 0o777 == 0o600


def test_install_credentials_replaces_unmarked_source_storage_secrets(tmp_path: Path) -> None:
    credentials = tmp_path / "candidate.json"
    _credentials(credentials)
    target_env = tmp_path / ".env.prod"
    target_env.write_text(
        "ARTIFACT_S3_ACCESS_KEY=old-access\n"
        "export ARTIFACT_S3_SECRET_KEY='old-secret'\n"
        "AWS_SESSION_TOKEN=old-session\n"
        "FS_SECRET_KEY=keep-me\n",
        encoding="utf-8",
    )
    gateway = LifecycleStorageGateway(
        project_root=tmp_path,
        session_factory=lambda **kwargs: _Session([], **kwargs),
    )

    gateway.install_credentials(_config(credentials), target_env)

    text = target_env.read_text(encoding="utf-8")
    assert "old-access" not in text
    assert "old-secret" not in text
    assert "old-session" not in text
    assert text.count("ARTIFACT_S3_ACCESS_KEY=") == 1
    assert text.count("ARTIFACT_S3_SECRET_KEY=") == 1
    assert "FS_SECRET_KEY=keep-me" in text


def test_bundled_credential_install_has_exact_nonsecret_resume_evidence(tmp_path: Path) -> None:
    _bundled_credentials(tmp_path)
    target_env = tmp_path / ".env.prod"
    target_env.write_text(
        "ARTIFACT_S3_ACCESS_KEY=old-external-access\nARTIFACT_S3_SECRET_KEY=old-external-secret\n",
        encoding="utf-8",
    )
    config = DeploymentConfig(environment="production", file_storage="bundled")
    gateway = LifecycleStorageGateway(project_root=tmp_path)

    gateway.install_credentials(config, target_env)

    evidence = gateway.credential_evidence(config, target_env)
    assert gateway.credentials_match(config, target_env) is True
    assert evidence["present_keys"] == [
        "ARTIFACT_S3_ACCESS_KEY",
        "ARTIFACT_S3_SECRET_KEY",
    ]
    assert len(str(evidence["sha256"])) == 64
    assert "bundled-access-value" not in json.dumps(evidence)
    assert "bundled-secret-value" not in json.dumps(evidence)
    text = target_env.read_text(encoding="utf-8")
    assert "old-external" not in text
    assert "bundled-access-value" in text
