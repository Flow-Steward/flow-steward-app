"""Env-backed config for curated OpenAPI/docs exposure."""

from __future__ import annotations

from dataclasses import dataclass

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

PUBLIC_OPENAPI_PATH = "/openapi-public.json"
PUBLIC_DOCS_PATH = "/docs/api"
INTERNAL_OPENAPI_PATH = "/openapi-internal.json"
INTERNAL_DOCS_PATH = "/docs/internal"


def _env_bool(name: str, default: bool) -> bool:
    raw = _runtime_env.get(name)
    if raw is None:
        return bool(default)
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return bool(default)


@dataclass(frozen=True)
class OpenAPIDocsConfig:
    public_openapi_enabled: bool
    public_docs_enabled: bool
    internal_openapi_enabled: bool
    internal_docs_enabled: bool
    public_openapi_require_auth: bool
    internal_openapi_require_auth: bool

    @property
    def public_docs_available(self) -> bool:
        return self.public_openapi_enabled and self.public_docs_enabled

    @property
    def internal_docs_available(self) -> bool:
        return self.internal_openapi_enabled and self.internal_docs_enabled


def load_openapi_docs_config() -> OpenAPIDocsConfig:
    return OpenAPIDocsConfig(
        public_openapi_enabled=_env_bool("FS_OPENAPI_PUBLIC_ENABLED", False),
        public_docs_enabled=_env_bool("FS_DOCS_PUBLIC_ENABLED", False),
        internal_openapi_enabled=_env_bool("FS_OPENAPI_INTERNAL_ENABLED", False),
        internal_docs_enabled=_env_bool("FS_DOCS_INTERNAL_ENABLED", False),
        public_openapi_require_auth=_env_bool("FS_OPENAPI_PUBLIC_REQUIRE_AUTH", True),
        internal_openapi_require_auth=_env_bool("FS_OPENAPI_INTERNAL_REQUIRE_AUTH", True),
    )


def docs_public_auth_paths() -> set[str]:
    config = load_openapi_docs_config()
    public_paths: set[str] = set()
    if config.public_openapi_enabled and not config.public_openapi_require_auth:
        public_paths.add(PUBLIC_OPENAPI_PATH)
    if config.public_docs_available and not config.public_openapi_require_auth:
        public_paths.add(PUBLIC_DOCS_PATH)
    if config.internal_openapi_enabled and not config.internal_openapi_require_auth:
        public_paths.add(INTERNAL_OPENAPI_PATH)
    if config.internal_docs_available and not config.internal_openapi_require_auth:
        public_paths.add(INTERNAL_DOCS_PATH)
    return public_paths
