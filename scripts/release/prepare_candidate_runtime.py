#!/usr/bin/env python3
"""Materialize one candidate-owned production env and secret-free CLI state."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

from core.infrastructure.release_candidate.env_source import read_last_wins


def _write_owner_only_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def prepare(
    *,
    source_env: Path,
    output_env: Path,
    state_dir: Path,
    candidate_commit: str,
    public_port: int,
    runtime_overrides: Path | None = None,
    self_hosted_evidence: bool = False,
) -> None:
    short = candidate_commit[:12]
    project = f"fs-candidate-{short}"
    values = read_last_wins(source_env).values
    if not values.get("FS_BACKUP_ENCRYPTION_KEY"):
        values["FS_BACKUP_ENCRYPTION_KEY"] = secrets.token_urlsafe(48)
    storage_hint = values.get("FS_FILE_STORAGE", "").lower()
    endpoint = values.get("ARTIFACT_S3_ENDPOINT_URL", "").lower()
    bundled = (
        (not storage_hint and not endpoint)
        or "bundled" in storage_hint
        or "seaweed" in storage_hint
        or "seaweed" in endpoint
    )
    file_storage = "bundled" if bundled else "external_s3"
    if bundled:
        values["ARTIFACT_STORAGE_BACKEND"] = "s3"
        values["ARTIFACT_S3_ENDPOINT_URL"] = "http://seaweedfs:8333"
        values["ARTIFACT_S3_BUCKET"] = values.get("ARTIFACT_S3_BUCKET") or "flow-steward-artifacts"
        values["ARTIFACT_S3_REGION"] = values.get("ARTIFACT_S3_REGION") or "us-east-1"
        values["ARTIFACT_S3_USE_SSL"] = "false"
        if not values.get("ARTIFACT_S3_ACCESS_KEY"):
            values["ARTIFACT_S3_ACCESS_KEY"] = secrets.token_hex(32)
        if not values.get("ARTIFACT_S3_SECRET_KEY"):
            values["ARTIFACT_S3_SECRET_KEY"] = secrets.token_urlsafe(48)
    values["FS_STACK_ENVIRONMENT"] = "production"
    values["FS_AUTH_COOKIE_SECURE"] = "1"
    values["FS_MCP_AUTH_DISABLED"] = "0"
    values["FS_FILE_STORAGE"] = file_storage
    values["FS_COMPOSE_PROJECT_NAME"] = project
    values["COMPOSE_PROJECT_NAME"] = project
    values["APP_PORT"] = str(public_port)
    values["FS_PUBLIC_BASE_URL"] = f"https://localhost:{public_port}"
    values["FS_DEPLOYMENT_DOCTOR_BASE_URL"] = "http://fs-app:8099"
    if self_hosted_evidence:
        # The release harness owns Mailpit on the candidate's private Compose
        # network. Keep this exception explicit here; production and general
        # self-hosted environments retain the secure private-network denial.
        values["FS_ALLOW_PRIVATE_REMOTE_URLS"] = "1"
    values["FS_CANDIDATE_STATE_DIR"] = "/run/flow-steward-candidate-state"
    values["FS_CANDIDATE_PROD_ENV"] = "/app/.env.prod"
    if runtime_overrides is not None:
        values.update(read_last_wins(runtime_overrides).values)
    # Never inherit a stale or half-configured first-admin token from the
    # operator input. A production candidate must satisfy the installed auth
    # defaults; audit users are seeded through the in-process bootstrap path.
    values["FS_AUTH_BOOTSTRAP_TOKEN"] = ""
    values["FS_AUTH_BOOTSTRAP_TOKEN_EXPIRES_AT"] = ""
    values["FS_AUTH_BOOTSTRAP_STATE_FILE"] = ""
    values["FS_AUTH_BOOTSTRAP_ENABLED"] = "0"
    values["FS_AUTH_BOOTSTRAP_LOCALHOST_ONLY"] = "1"
    values["FS_AUTH_BOOTSTRAP_ALLOW_TRUSTED_PROXY"] = "1"

    output_env.parent.mkdir(parents=True, exist_ok=True)
    output_env.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    os.chmod(output_env, 0o600)

    if bundled:
        _write_owner_only_json(
            output_env.parent / "runtime" / "seaweedfs-s3.json",
            {
                "identities": [
                    {
                        "name": "flow-steward-core",
                        "credentials": [
                            {
                                "accessKey": values["ARTIFACT_S3_ACCESS_KEY"],
                                "secretKey": values["ARTIFACT_S3_SECRET_KEY"],
                            }
                        ],
                        "actions": ["Admin", "Read", "Write", "List", "Tagging"],
                    }
                ]
            },
        )

    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    profiles = "storage-seaweedfs" if bundled else ""
    services = "traefik rabbitmq-fs pg-fs fs-mcp fs-app fs-worker control-plane mailpit"
    if bundled:
        services += " seaweedfs"
    (state_dir / "stack.toml").write_text(
        "\n".join(
            (
                "# Immutable release-candidate runtime selection.",
                'environment = "production"',
                f'file_storage = "{file_storage}"',
                'tls_mode = "local"',
                'public_host = "localhost"',
                f"public_port = {public_port}",
                "",
            )
        ),
        encoding="utf-8",
    )
    (state_dir / "compose.env").write_text(
        "\n".join(
            (
                "FS_STACK_ENVIRONMENT=production",
                f"FS_COMPOSE_PROJECT_NAME={project}",
                f"FS_FILE_STORAGE={file_storage}",
                "FS_ENV_FILE=.env.prod",
                "FS_COMPOSE_FILES=infra/docker/docker-compose.prod.yaml",
                f"FS_COMPOSE_PROFILES={profiles}",
                f"FS_COMPOSE_SERVICES={services}",
                "FS_REBUILD_SERVICES=pg-fs",
                "",
            )
        ),
        encoding="utf-8",
    )
    os.chmod(state_dir / "stack.toml", 0o600)
    os.chmod(state_dir / "compose.env", 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-env", required=True, type=Path)
    parser.add_argument("--output-env", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--public-port", required=True, type=int)
    parser.add_argument("--runtime-overrides", type=Path)
    parser.add_argument("--self-hosted-evidence", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_commit):
        raise SystemExit("candidate commit must be a full lowercase Git SHA")
    if not args.source_env.is_file():
        raise SystemExit("candidate production source env is missing")
    prepare(
        source_env=args.source_env.resolve(),
        output_env=args.output_env.resolve(),
        state_dir=args.state_dir.resolve(),
        candidate_commit=args.candidate_commit,
        public_port=args.public_port,
        runtime_overrides=(args.runtime_overrides.resolve() if args.runtime_overrides else None),
        self_hosted_evidence=args.self_hosted_evidence,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
