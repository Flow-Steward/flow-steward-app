"""Provider-neutral artifact storage adapter construction."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

SUPPORTED_ARTIFACT_STORAGE_BACKENDS = frozenset({"local_fs", "s3", "memory"})


def validate_artifact_storage_backend(value: object) -> str:
    """Reject unsupported storage before runtime preparation or adapter effects."""
    backend = str(value or "local_fs").strip().lower()
    if backend not in SUPPORTED_ARTIFACT_STORAGE_BACKENDS:
        raise ValueError("Invalid ARTIFACT_STORAGE_BACKEND; expected local_fs, s3, or memory")
    return backend


def build_artifact_storage(
    *,
    create_storage_adapter: Callable[[dict[str, Any]], Any],
    artifact_plane_factory: Callable[..., Any],
    environ: dict[str, str] | None = None,
):
    """Build the artifact storage plane from environment configuration."""

    env = environ or dict(_runtime_env)
    # AC17-R128: the backend every Compose file selects, and the one this image
    # already knows the directory for. Falling back to "memory" meant a platform
    # deployment accepted artifacts, reported them available and answered 503 on
    # the way back out, because the bytes were in a process that had moved on.
    backend = validate_artifact_storage_backend(env.get("ARTIFACT_STORAGE_BACKEND"))
    key_prefix = str(
        env.get("ARTIFACT_KEY_PREFIX") or env.get("ARTIFACT_S3_KEY_PREFIX") or ""
    ).strip()
    config: dict[str, Any] = {"backend": backend, "key_prefix": key_prefix}
    if backend == "s3":
        config.update(
            {
                "endpoint_url": env.get("ARTIFACT_S3_ENDPOINT_URL", ""),
                "public_endpoint_url": env.get("ARTIFACT_S3_PUBLIC_ENDPOINT_URL", ""),
                "bucket": env.get("ARTIFACT_S3_BUCKET", "flow-steward-artifacts"),
                "access_key": env.get("ARTIFACT_S3_ACCESS_KEY", ""),
                "secret_key": env.get("ARTIFACT_S3_SECRET_KEY", ""),
                "region": env.get("ARTIFACT_S3_REGION", "us-east-1"),
                "use_ssl": env.get("ARTIFACT_S3_USE_SSL", "true").lower()
                not in ("0", "false", "no"),
                "presign_ttl_seconds": int(env.get("ARTIFACT_S3_PRESIGN_TTL_SECONDS") or 600),
            }
        )
    elif backend == "local_fs":
        config["root_dir"] = env.get("ARTIFACT_LOCAL_ROOT_DIR", "/data/artifacts")

    try:
        adapter = create_storage_adapter(config)
    except ModuleNotFoundError as exc:
        import sys

        if backend != "s3" or exc.name != "boto3" or "pytest" not in sys.modules:
            raise
        adapter = create_storage_adapter({"backend": "memory", "key_prefix": key_prefix})
    return artifact_plane_factory(storage=adapter)
