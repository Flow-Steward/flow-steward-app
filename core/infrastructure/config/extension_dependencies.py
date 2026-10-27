from __future__ import annotations

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.config.size_limits import clamp_size_limit_bytes

FS_EXTENSION_DEPS_FETCH_ENV = "FS_EXTENSION_DEPS_FETCH"
FS_EXTENSION_DEPS_SITE_MAX_BYTES_ENV = "FS_EXTENSION_DEPS_SITE_MAX_BYTES"
EXTENSION_DEPS_FETCH_OFF = "off"
EXTENSION_DEPS_FETCH_ALLOWLIST = "allowlist"
EXTENSION_DEPS_DEFAULT_INDEX_URL = "https://pypi.org/simple"
EXTENSION_DEPS_DEFAULT_SITE_MAX_BYTES = 500 * 1024 * 1024


def extension_deps_fetch_mode() -> str:
    return (
        str(
            _runtime_env.get(FS_EXTENSION_DEPS_FETCH_ENV, EXTENSION_DEPS_FETCH_OFF)
            or EXTENSION_DEPS_FETCH_OFF
        )
        .strip()
        .lower()
    )


def extension_deps_site_max_bytes() -> int:
    return clamp_size_limit_bytes(
        _runtime_env.get(FS_EXTENSION_DEPS_SITE_MAX_BYTES_ENV),
        default=EXTENSION_DEPS_DEFAULT_SITE_MAX_BYTES,
        minimum=1024 * 1024,
        maximum=10 * 1024 * 1024 * 1024,
    )
