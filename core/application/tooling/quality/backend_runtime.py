"""Conservative runtime identity for reusable backend test evidence."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path

from core.infrastructure.runtime_env import runtime_env

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_RUNTIME_RECEIPT = re.compile(r"[0-9a-f]{64}")
_RUNTIME_RECEIPT_PATH = Path("/opt/flow-steward/backend-runtime.sha256")
# These identify the gate invocation or its temporary resources, rather than
# test behavior. The test wrapper replaces project, source-root, hash-seed, and
# bytecode-cache settings. GitHub command files and run/workspace/SHA metadata
# identify the launcher; repository, event, branch, and GITHUB_ACTIONS stay bound.
# HOSTNAME is a disposable container ID. HOME differs for the final root reader;
# the baked receipt and distribution graph identify the installed runtime, while
# custom startup/plugin/configuration paths below disable cross-SHA reuse.
# PWD/OLDPWD/SHLVL/_ describe nested shell launches; every check sets its cwd.
_OPERATIONAL_NAMES = frozenset(
    {
        "FS_BUILD_SHA",
        "FS_HOST_ROOT_DIR",
        "FS_COMPOSE_PROJECT_NAME",
        "FS_QUALITY_CACHE_ROOT",
        "FS_QUALITY_EVIDENCE_MODE",
        "FS_BOUNDARY_TRACKED_FILES_MANIFEST",
        "FS_TEST_AUDIT_IMAGE_ID",
        "FS_TEST_BACKEND_SERVICES_ID",
        "FS_DOCKER_GID",
        "PYTEST_SOURCE_REPO_ROOT",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
        "PYTHONHASHSEED",
        "PYTHONPYCACHEPREFIX",
        "HOSTNAME",
        "HOME",
        "PWD",
        "OLDPWD",
        "SHLVL",
        "_",
        "GITHUB_SHA",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_RUN_NUMBER",
        "GITHUB_WORKSPACE",
        "GITHUB_ENV",
        "GITHUB_OUTPUT",
        "GITHUB_PATH",
        "GITHUB_STATE",
        "GITHUB_STEP_SUMMARY",
        "RUNNER_TEMP",
    }
)
_EXTERNAL_INPUT_NAMES = (
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "PYTHONHOME",
    "PYTHONUSERBASE",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "COVERAGE_RCFILE",
    "COVERAGE_PROCESS_START",
    "NODE_OPTIONS",
    "NODE_PATH",
    "VITE_FRONTEND_EXTENSION_MANIFEST",
    "npm_config_userconfig",
    "NPM_CONFIG_USERCONFIG",
    "npm_config_globalconfig",
    "NPM_CONFIG_GLOBALCONFIG",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "FS_PROJECT_CONFIG",
    "FS_ENV_FILE",
    "FS_AUTH_BOOTSTRAP_STATE_FILE",
    "FS_DB_SCHEMA_DIR",
    "FS_EXTENSIONS_ROOT",
    "FS_EXTENSIONS_DIR",
    "FS_TLS_CERTIFICATE_PATH",
    "FS_TLS_PRIVATE_KEY_PATH",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "BASH_ENV",
    "ENV",
)
_SYSTEM_EXECUTABLE_DIRS = frozenset(
    {
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
        "/usr/local/games",
        "/usr/games",
    }
)
_CONFIGURATION_PATH_NAME = re.compile(
    r"(?:^|_)(?:CONFIG|CONFIGURATION|FILE|FILES|PATH|DIR|DIRECTORY|ROOT)$"
)
_NON_PATH_CONFIGURATION_NAMES = frozenset({"FS_ATTACHMENT_MAX_FILES"})
# These exact resource paths are established by the gate, either as isolated
# write locations or as repository paths already covered by backend inputs.
# An unknown configuration path can refer to mutable files outside that graph.
_CANONICAL_CONFIGURATION_PATHS = {
    # Public quickstart marker: this directory is absent and is not mounted
    # into fs-audit. A real/custom target path remains candidate-bound.
    "TARGET_PROJECT_PATH": frozenset({"/absolute/path/to/your/project"}),
    "PLAYWRIGHT_BROWSERS_PATH": frozenset({"/ms-playwright"}),
    "PYTHONPATH": frozenset({"/app"}),
    "FS_STACK_ENV_FILE": frozenset({"/dev/null"}),
    "ARTIFACT_LOCAL_ROOT_DIR": frozenset({"/tmp/flow-steward-audit-artifacts"}),  # nosec B108 - isolated-audit comparison value; no filesystem creation
    "FS_ARTIFACT_ROOT": frozenset({"/tmp/flow-steward-audit-ops"}),  # nosec B108 - isolated-audit comparison value; no filesystem creation
    "FS_ROOT_DIR": frozenset({"/app"}),
    "FS_SCRIPTS_DIR": frozenset({"/app/core/entrypoints/cli/scripts"}),
    "FS_DOCKER_DIR": frozenset({"/app/infra/docker"}),
    "FS_TOOLING_DIR": frozenset({"/app/infra/tooling"}),
    "FS_PYTHON_CACHE_DIR": frozenset({"/app/logs/tool-cache/python/pycache"}),
}


def backend_runtime_identity(
    candidate_sha: str, *, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return secret-free identity, binding unknown/custom runtimes to one SHA.

    The host validates the audit image and hashes the named, actual PostgreSQL
    and RabbitMQ image IDs into FS_TEST_BACKEND_SERVICES_ID. The image carries a
    receipt of actual toolchain files, baked before application sources are
    copied in. That receipt covers native libraries and tool binaries while
    allowing source-only image rebuilds to reuse evidence. The full image ID
    is required as validation provenance, rather than part of that receipt.
    Every reader, including the final release gate, must receive those IDs.
    """
    environment = dict(runtime_env if environ is None else environ)
    image_id = environment.get("FS_TEST_AUDIT_IMAGE_ID", "")
    services_id = environment.get("FS_TEST_BACKEND_SERVICES_ID", "")
    identity = {
        "backend_audit_image": "validated" if _IMAGE_ID.fullmatch(image_id) else "unavailable",
        "backend_services": services_id if _IMAGE_ID.fullmatch(services_id) else "unavailable",
        "backend_runtime_receipt": _runtime_receipt_identity(),
        "backend_python": _digest(
            (platform.python_implementation(), sys.version, sys.implementation.cache_tag)
        ),
        "backend_platform": _digest((platform.system(), platform.machine(), platform.libc_ver())),
        "backend_environment": _digest(
            {key: value for key, value in environment.items() if key not in _OPERATIONAL_NAMES}
        ),
    }
    try:
        identity["backend_distributions"] = _distribution_identity()
    except (OSError, ValueError):
        # A shared 'unavailable' result must never permit cross-SHA reuse.
        identity["backend_distributions"] = "unavailable"
    if (
        "unavailable" in identity.values()
        or environment.get("FS_AUDIT_ISOLATED") != "1"
        or environment.get("FS_STACK_ENVIRONMENT") != "development"
        or _has_external_inputs(environment)
    ):
        identity["backend_candidate"] = candidate_sha
    return identity


def _runtime_receipt_identity() -> str:
    try:
        receipt = _RUNTIME_RECEIPT_PATH.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return "unavailable"
    return receipt if _RUNTIME_RECEIPT.fullmatch(receipt) else "unavailable"


def _has_external_inputs(environment: Mapping[str, str]) -> bool:
    target = environment.get("TARGET_PROJECT_PATH", "")
    if target == "/absolute/path/to/your/project" and Path(target).exists():
        return True
    if any(environment.get(name, "").strip() for name in _EXTERNAL_INPUT_NAMES):
        return True
    if environment.get("FS_STACK_ENV_FILE", "") not in {"", "/dev/null"}:
        return True
    # The canonical image uses /app; the test wrapper replaces it with its
    # isolated workspace. Any other inherited search root has an unknown graph.
    if environment.get("PYTHONPATH", "") not in {"", "/app"}:
        return True
    path = environment.get("PATH", "")
    if path and any(part not in _SYSTEM_EXECUTABLE_DIRS for part in path.split(":")):
        return True
    return any(
        value.strip()
        and key not in _OPERATIONAL_NAMES
        and key not in _NON_PATH_CONFIGURATION_NAMES
        and key != "PATH"
        and _CONFIGURATION_PATH_NAME.search(key.upper()) is not None
        and value not in _CANONICAL_CONFIGURATION_PATHS.get(key, ())
        for key, value in environment.items()
    )


def _distribution_identity() -> str:
    packages: list[tuple[str, str]] = []
    for distribution in metadata.distributions():
        try:
            name = distribution.metadata["Name"] or ""
        except KeyError:
            name = ""
        version = distribution.version
        if not name or not version:
            raise ValueError("installed distribution identity is incomplete")
        packages.append((name.lower().replace("_", "-"), version))
    if not packages:
        raise ValueError("installed distribution identity is unavailable")
    return _digest(sorted(packages))


def _digest(value: object) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
