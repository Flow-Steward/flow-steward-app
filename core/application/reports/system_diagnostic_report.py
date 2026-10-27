"""On-demand, redacted diagnostic report for the whole installation.

An operator downloads it from the Monitoring page or writes it with
``flow-steward reports diagnostics`` and attaches it to a bug report. It says
what is installed and how it runs: version and images, Compact or Scalable,
CPU architecture and resources, database and broker versions, the services
and their health, the installed extensions, and which settings are customised.

It carries no logs, no job content and no configuration value outside a short
allowlist of topology settings; every value that is a configured secret is
removed wherever it appears.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from core.application.security.credential_redaction import RedactionContext, redact
from core.infrastructure.config.env_registry import env_specs
from core.infrastructure.config.version import APP_VERSION

SYSTEM_DIAGNOSTIC_REPORT_SCHEMA = "flow-steward.system-diagnostic-report.v1"

# Settings that describe the shape of the deployment rather than its data.
# Anything else is reported only as "customised", never with its value: a
# non-secret setting can still hold a hostname, an address or a DSN.
_TOPOLOGY_SETTINGS = (
    "ENVIRONMENT",
    "FS_STACK_ENVIRONMENT",
    "FS_DEPLOYMENT_PROFILE",
    "FS_INSTALL_MODE",
    "FS_INGRESS_MODE",
    "FS_COMPOSE_PROFILES",
    "FS_FILE_STORAGE",
    "ARTIFACT_STORAGE_BACKEND",
    "FS_CONTEXT_GRAPH_BACKEND",
    "FS_MEMORY_PROFILE",
    "FS_MEMORY_KNOWLEDGE_BACKEND",
    "FS_MEMORY_REPOSITORY_BACKEND",
    "FS_MEMORY_TEXT_SEARCH_BACKEND",
    "FS_MEMORY_VECTOR_BACKEND",
    "FS_GRAPH_RESUME_EXECUTOR",
    "FS_LOG_LEVEL",
    "FS_WORKER_PROCESSES",
    "FS_WORKER_REPLICAS",
    "FS_DEFAULT_PROVIDER",
    "FS_DEFAULT_MODEL",
    "SECURITY_SCAN_MODE",
)
_IMAGE_ROLES = ("APP", "WORKER", "CONTROL", "CLI", "FRONTEND", "COMPACT")
_EXTENSION_FIELDS = (
    "extension_id",
    "version",
    "extension_type",
    "state",
    "runtime_mode",
    "runtime_status",
    "health_status",
    "handshake_status",
    "status_group",
    "descriptor_origin",
    "entitlement_status",
    "connected",
    "broken",
    "broken_reason",
    "files_missing",
    "installed_at",
)

Section = Callable[[], Any]


def _image_identity(env: Mapping[str, str]) -> dict[str, dict[str, str]]:
    images: dict[str, dict[str, str]] = {}
    for role in _IMAGE_ROLES:
        prefix = f"FS_{role}_IMAGE"
        row = {
            key: str(env.get(f"{prefix}{suffix}") or "").strip()
            for key, suffix in (("image", ""), ("digest", "_DIGEST"), ("revision", "_REVISION"))
        }
        if any(row.values()):
            images[role.lower()] = row
    platform_value = str(env.get("FS_COMPACT_IMAGE_PLATFORM") or "").strip()
    if platform_value and "compact" in images:
        images["compact"]["platform"] = platform_value
    return images


def _running_image(env: Mapping[str, str]) -> dict[str, str]:
    # Baked into the image itself, and the only image identity the Compact
    # supervisor passes on to its processes.
    return {
        key: value
        for key, value in (
            ("role", str(env.get("FS_EMBEDDED_IMAGE_ROLE") or "").strip()),
            ("revision", str(env.get("FS_EMBEDDED_IMAGE_REVISION") or "").strip()),
            ("platform", str(env.get("FS_EMBEDDED_IMAGE_PLATFORM") or "").strip()),
        )
        if value
    }


def product_section(env: Mapping[str, str]) -> dict[str, Any]:
    return {
        "version": APP_VERSION,
        "build_sha": str(env.get("FS_BUILD_SHA") or "").strip(),
        "running_image": _running_image(env),
        "images": _image_identity(env),
    }


def deployment_section(env: Mapping[str, str], *, runtime_target: Section) -> dict[str, Any]:
    try:
        target = runtime_target()
    except ValueError as exc:
        target = f"unsupported: {exc}"
    return {
        "profile": str(env.get("FS_DEPLOYMENT_PROFILE") or "").strip() or "scalable",
        # AC17-R129: with no marker the runtime reads silence as production.
        "environment": str(env.get("FS_STACK_ENVIRONMENT") or env.get("ENVIRONMENT") or "").strip()
        or "unset (read as production)",
        "extension_runtime_target": target,
    }


def configuration_section(env: Mapping[str, str]) -> dict[str, Any]:
    """Name what is customised and which protected settings are present, never their values."""
    specs = env_specs()
    customised: list[str] = []
    protected_present: list[str] = []
    protected_absent: list[str] = []
    for spec in specs:
        value = str(env.get(spec.name) or "").strip()
        if spec.secret or spec.doctor_secret:
            if spec.doctor_required:
                (protected_present if value else protected_absent).append(spec.name)
            continue
        default = spec.default_for_environment(
            str(env.get("ENVIRONMENT") or "production").strip() or "production"
        )
        if value and value != default and spec.name not in _TOPOLOGY_SETTINGS:
            customised.append(spec.name)
    known = {spec.name for spec in specs}
    return {
        "topology": {
            name: str(env.get(name) or "").strip()
            for name in _TOPOLOGY_SETTINGS
            if str(env.get(name) or "").strip()
        },
        "customised_settings": sorted(customised),
        # Only what reached this process: a runtime that is handed a DSN, for
        # instance, never sees the database password on its own.
        "protected_settings_in_this_process": {
            "set": sorted(protected_present),
            "not_set": sorted(protected_absent),
        },
        "unregistered_fs_settings": sorted(
            name for name in env if name.startswith("FS_") and name not in known
        ),
    }


#: Past process starts kept in the report: enough to see a restart pattern
#: without burying the current state under a week of history.
_SERVICE_HISTORY_LIMIT = 20

_UNCHECKED = {"", "unknown", None}


def services_section(components: Any) -> dict[str, Any]:
    """Verdicts first, then the current observations, then a capped history."""
    from core.application.monitoring.verdicts import service_verdicts

    if components is None:
        return {"summary": [], "components": None}
    dumped = components.model_dump(mode="json")
    rows = dumped.pop("rows", []) or []
    current = [row for row in rows if not row.get("superseded")]
    history = sorted(
        (row for row in rows if row.get("superseded")),
        key=lambda row: str(row.get("observed_at") or ""),
        reverse=True,
    )
    return {
        "summary": service_verdicts(components),
        "components": {**dumped, "rows": current},
        "history": {
            "total": len(history),
            "shown": min(len(history), _SERVICE_HISTORY_LIMIT),
            "rows": history[:_SERVICE_HISTORY_LIMIT],
        },
    }


def _extension_status_note(row: Mapping[str, Any]) -> str | None:
    """Why an extension's status reads "unknown", instead of leaving it a mystery.

    Only remote extensions are probed (at install, or by a handshake on
    demand). A local extension is never probed, so "unknown" there means "not
    checked", not "broken"; an install another account owns is not visible to
    this one.
    """
    if row.get("entitlement_status") == "unknown" and row.get("status_group") == "unknown":
        return "Installed by another account or present on disk without an install record"
    if row.get("health_status") in _UNCHECKED and row.get("handshake_status") in _UNCHECKED:
        if str(row.get("runtime_mode") or "") == "remote_https":
            return "Remote extension not probed since install; run a handshake to check it"
        return "Local extension: not probed; it is checked when a step uses it"
    return None


def extensions_section(listing: Any) -> dict[str, Any]:
    payload = listing.model_dump(mode="json") if hasattr(listing, "model_dump") else listing
    rows = [row for row in (dict(payload or {}).get("extensions") or []) if isinstance(row, dict)]
    extensions = []
    for row in sorted(rows, key=lambda item: str(item.get("extension_id") or "")):
        item = {key: row.get(key) for key in _EXTENSION_FIELDS if key in row}
        note = _extension_status_note(row)
        if note:
            item["status_note"] = note
        extensions.append(item)
    return {"total": len(rows), "extensions": extensions}


def _known_secret_values(env: Mapping[str, str]) -> tuple[str, ...]:
    values = []
    for spec in env_specs():
        if not (spec.secret or spec.doctor_secret):
            continue
        value = str(env.get(spec.name) or "").strip()
        # A short value ("1", "true") would erase unrelated text everywhere.
        if len(value) >= 8:
            values.append(value)
    return tuple(values)


def _run_section(section: Section) -> Any:
    try:
        return section()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def build_system_diagnostic_report(
    *,
    source: str,
    sections: Mapping[str, Section],
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Run every section independently and return one redacted report.

    A section that fails is reported with its error and does not stop the
    others: the report is most needed exactly when something is broken.
    """
    report: dict[str, Any] = {
        "schema_version": SYSTEM_DIAGNOSTIC_REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source,
    }
    for name, section in sections.items():
        report[name] = _run_section(section)
    # Only credential shapes, not the generic secret scanner: its provider-token
    # patterns read dotted names such as this report's own schema as tokens.
    redacted = redact(report, context=RedactionContext(known_secrets=_known_secret_values(env)))
    return json.loads(json.dumps(redacted, ensure_ascii=False, default=str))


def collect_system_diagnostic_report(
    *,
    source: str,
    store: Any,
    list_extensions: Section,
    readiness: Section | None = None,
    host: Section | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the standard sections from what this process can reach."""
    from core.application.monitoring.system import query_runtime_components
    from core.infrastructure.config.connection_strings import resolve_rabbitmq_url
    from core.infrastructure.extension_runtime.runtime_target import extension_runtime_target
    from core.infrastructure.monitoring.system_diagnostics import (
        load_activity_facts,
        load_database_facts,
        runtime_process_facts,
    )
    from core.infrastructure.runtime.worker.queue_broker import get_broker_facts
    from core.infrastructure.runtime_env import runtime_env

    values: Mapping[str, str] = env if env is not None else runtime_env.copy()

    def _on_database(read: Callable[[Any], Any]) -> Section:
        def _section() -> Any:
            conn = store.conn
            try:
                return read(conn)
            except Exception:
                # A failed statement aborts the transaction; the next section
                # that reads the same connection must not inherit that.
                if not getattr(conn, "autocommit", False):
                    conn.rollback()
                raise

        return _section

    def _services() -> dict[str, Any]:
        components = query_runtime_components(store)
        payload: dict[str, Any] = services_section(components)
        if readiness is not None:
            payload["readiness"] = readiness()
        return payload

    sections: dict[str, Section] = {
        "product": lambda: product_section(values),
        "deployment": lambda: deployment_section(values, runtime_target=extension_runtime_target),
        "runtime": runtime_process_facts,
        "configuration": lambda: configuration_section(values),
        "database": _on_database(load_database_facts),
        "broker": lambda: get_broker_facts(resolve_rabbitmq_url(values) or None),
        "services": _services,
        "activity": _on_database(load_activity_facts),
        "extensions": lambda: extensions_section(list_extensions()),
    }
    if host is not None:
        sections["host"] = host
    return build_system_diagnostic_report(source=source, sections=sections, env=values)


__all__ = [
    "SYSTEM_DIAGNOSTIC_REPORT_SCHEMA",
    "build_system_diagnostic_report",
    "collect_system_diagnostic_report",
    "configuration_section",
    "extensions_section",
    "product_section",
    "services_section",
]
