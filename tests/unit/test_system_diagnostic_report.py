"""The installation diagnostic report: what it carries and what it must never carry."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from core.application.reports.system_diagnostic_report import (
    SYSTEM_DIAGNOSTIC_REPORT_SCHEMA,
    build_system_diagnostic_report,
    collect_system_diagnostic_report,
    configuration_section,
    extensions_section,
    product_section,
)
from core.infrastructure.monitoring.system_diagnostics import hosting_platform_facts

_DB_PASSWORD = "pg-Very-Secret-Value-42"  # pragma: allowlist secret
_SECRET_KEY = "app-signing-key-0123456789abcdef"  # pragma: allowlist secret
_BROKER_URL = "amqp://flow:hunter2pass@rabbit:5672/"  # pragma: allowlist secret


def _env(**extra: str) -> dict[str, str]:
    return {
        "ENVIRONMENT": "production",
        "FS_DEPLOYMENT_PROFILE": "compact",
        "POSTGRES_PASSWORD": _DB_PASSWORD,
        "FS_SECRET_KEY": _SECRET_KEY,
        "FS_PGVECTOR_DSN": f"postgresql://flow:{_DB_PASSWORD}@db.internal:5432/flow",
        "FS_BUILD_SHA": "abc1234",
        "FS_COMPACT_IMAGE": "ghcr.io/flow-steward/compact:1.0.0",
        "FS_COMPACT_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "FS_COMPACT_IMAGE_PLATFORM": "linux/arm64",
        **extra,
    }


def test_a_failing_section_is_reported_and_the_others_still_run() -> None:
    def broken() -> object:
        raise ConnectionError("broker unreachable")

    report = build_system_diagnostic_report(
        source="api",
        sections={"broker": broken, "product": lambda: {"version": "1.0.0"}},
        env=_env(),
    )

    assert report["schema_version"] == SYSTEM_DIAGNOSTIC_REPORT_SCHEMA
    assert report["source"] == "api"
    assert report["broker"] == {"error": "ConnectionError: broker unreachable"}
    assert report["product"] == {"version": "1.0.0"}


def test_a_configured_secret_is_removed_wherever_it_appears() -> None:
    report = build_system_diagnostic_report(
        source="cli",
        sections={
            "database": lambda: {"error": f"could not connect with {_DB_PASSWORD}"},
            "host": lambda: {"note": f"key={_SECRET_KEY}", "nested": [_SECRET_KEY]},
            "broker": lambda: {"url": _BROKER_URL},
        },
        env=_env(),
    )

    encoded = json.dumps(report)
    assert _DB_PASSWORD not in encoded
    assert _SECRET_KEY not in encoded
    assert "hunter2pass" not in encoded


def test_configuration_names_settings_but_never_carries_their_values() -> None:
    section = configuration_section(_env(FS_PUBLIC_HOST="customer.example.com"))

    encoded = json.dumps(section)
    assert section["topology"]["FS_DEPLOYMENT_PROFILE"] == "compact"
    assert "POSTGRES_PASSWORD" in section["protected_settings_in_this_process"]["set"]
    assert "FS_PGVECTOR_DSN" in section["customised_settings"]
    assert _DB_PASSWORD not in encoded
    assert _SECRET_KEY not in encoded
    assert "db.internal" not in encoded
    assert "customer.example.com" not in encoded


def test_the_report_keeps_its_own_section_names_through_redaction() -> None:
    env = _env()
    report = build_system_diagnostic_report(
        source="api",
        sections={"configuration": lambda: configuration_section(env)},
        env=env,
    )

    protected = report["configuration"]["protected_settings_in_this_process"]
    assert "POSTGRES_PASSWORD" in protected["set"]


def test_product_names_the_build_and_the_images_it_runs() -> None:
    section = product_section(
        _env(FS_EMBEDDED_IMAGE_ROLE="compact-runtime", FS_EMBEDDED_IMAGE_PLATFORM="linux/arm64")
    )

    assert section["build_sha"] == "abc1234"
    assert section["running_image"] == {"role": "compact-runtime", "platform": "linux/arm64"}
    assert section["images"]["compact"] == {
        "image": "ghcr.io/flow-steward/compact:1.0.0",
        "digest": "sha256:" + "a" * 64,
        "revision": "",
        "platform": "linux/arm64",
    }


def test_extensions_are_listed_by_id_with_their_state_only() -> None:
    listing = {
        "extensions": [
            {
                "extension_id": "zeta",
                "version": "2.0.0",
                "state": "active",
                "health_status": "healthy",
                "remote_base_url": "https://private.example.com",
                "description": "long text",
            },
            {"extension_id": "alpha", "version": "1.1.0", "state": "disabled", "broken": True},
        ],
        "total": 2,
    }

    section = extensions_section(listing)

    assert section["total"] == 2
    assert [row["extension_id"] for row in section["extensions"]] == ["alpha", "zeta"]
    assert section["extensions"][1] == {
        "extension_id": "zeta",
        "version": "2.0.0",
        "state": "active",
        "health_status": "healthy",
    }


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        (
            {"RAILWAY_PROJECT_ID": "p-1", "RAILWAY_REPLICA_REGION": "europe-west4"},
            {"platform": "railway", "region": "europe-west4"},
        ),
        ({"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, {"platform": "kubernetes", "region": ""}),
        ({"FLY_APP_NAME": "fs", "FLY_REGION": "ams"}, {"platform": "fly", "region": "ams"}),
    ],
)
def test_the_hosting_platform_is_named_without_its_identifiers(environ, expected) -> None:
    assert hosting_platform_facts(environ) == expected


def test_collect_reports_every_standard_section_even_when_the_backends_are_down() -> None:
    class _Store:
        @property
        def conn(self):
            raise ConnectionError("database down")

    with (
        patch(
            "core.application.monitoring.system.query_runtime_components",
            return_value=None,
        ),
        patch(
            "core.infrastructure.runtime.worker.queue_broker.get_broker_facts",
            side_effect=ConnectionError("broker down"),
        ),
    ):
        report = collect_system_diagnostic_report(
            source="cli",
            store=_Store(),
            list_extensions=lambda: {"extensions": []},
            host=lambda: {"collected": False},
            env=_env(),
        )

    for name in (
        "product",
        "deployment",
        "runtime",
        "configuration",
        "database",
        "broker",
        "services",
        "activity",
        "extensions",
        "host",
    ):
        assert name in report, name
    assert report["database"] == {"error": "ConnectionError: database down"}
    assert report["broker"] == {"error": "ConnectionError: broker down"}
    assert report["deployment"]["profile"] == "compact"
    assert report["runtime"]["architecture"]


def _diagnostics_client(*, role: str):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.entrypoints.api.system_monitoring import create_api_system_monitoring_router

    def authorized(request, *, required_roles):
        if request.headers.get("x-test-role") != "admin":
            raise PermissionError
        return {"roles": ["admin"]}

    app = FastAPI()
    app.include_router(
        create_api_system_monitoring_router(
            local_auth_enabled=lambda: True,
            external_auth_enabled=False,
            require_authenticated_user=authorized,
            auth_admin_roles={"admin"},
            list_extensions=lambda _request: {"extensions": []},
        )
    )
    return TestClient(app, raise_server_exceptions=False), {"x-test-role": role}


def test_the_report_download_is_for_administrators_only() -> None:
    client, headers = _diagnostics_client(role="operator")

    response = client.get("/api/system/diagnostics/report", headers=headers)

    assert response.status_code == 403


def test_the_report_downloads_as_a_dated_json_attachment() -> None:
    client, headers = _diagnostics_client(role="admin")
    collected = {}

    def fake_collect(**kwargs):
        collected.update(kwargs)
        return {"schema_version": SYSTEM_DIAGNOSTIC_REPORT_SCHEMA, "source": kwargs["source"]}

    with (
        patch("core.entrypoints.api.system_monitoring.deps.get_logger_only", return_value=object()),
        patch(
            "core.entrypoints.api.system_monitoring.collect_system_diagnostic_report",
            side_effect=fake_collect,
        ),
    ):
        response = client.get(
            "/api/system/diagnostics/report",
            headers=headers,
            params={"download_token": "t" * 24},
        )

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="flow-steward-diagnostics-')
    assert f"fs_job_report_download_{'t' * 24}=1" in response.headers["set-cookie"]
    assert json.loads(response.text)["source"] == "api"
    assert collected["list_extensions"]() == {"extensions": []}


def test_the_cli_command_folds_the_host_facts_into_the_report() -> None:
    from core.entrypoints.cli.platform.service_bindings.reports import reports_diagnostics

    calls = {}

    def fake_collect(**kwargs):
        calls.update(kwargs)
        return {"source": kwargs["source"], "host": kwargs["host"]()}

    runtime = SimpleNamespace(
        get_worker_store=lambda: "store",
        extension_query_service=SimpleNamespace(list_extensions=lambda **_kw: {"extensions": []}),
    )
    with patch(
        "core.application.reports.system_diagnostic_report.collect_system_diagnostic_report",
        side_effect=fake_collect,
    ):
        result = asyncio.run(
            reports_diagnostics(
                runtime,
                {"host": json.dumps({"platform": {"arch": "arm64"}}), "doctor": {"exit_code": 0}},
            )
        )

    assert calls["store"] == "store"
    assert result["report"]["host"] == {
        "collected": True,
        "facts": {"platform": {"arch": "arm64"}},
        "doctor": {"exit_code": 0},
    }


def test_the_cli_command_says_when_it_ran_without_the_host() -> None:
    from core.entrypoints.cli.platform.service_bindings.reports import reports_diagnostics

    runtime = SimpleNamespace(
        get_worker_store=lambda: "store",
        extension_query_service=SimpleNamespace(list_extensions=lambda **_kw: {}),
    )
    with patch(
        "core.application.reports.system_diagnostic_report.collect_system_diagnostic_report",
        side_effect=lambda **kwargs: {"host": kwargs["host"]()},
    ):
        result = asyncio.run(reports_diagnostics(runtime, {}))

    assert result["report"]["host"]["collected"] is False
    assert "installation host" in result["report"]["host"]["reason"]


def test_an_undeclared_environment_is_named_as_read_as_production() -> None:
    from core.application.reports.system_diagnostic_report import deployment_section

    section = deployment_section({}, runtime_target=lambda: "linux-amd64")

    assert section == {
        "profile": "scalable",
        "environment": "unset (read as production)",
        "extension_runtime_target": "linux-amd64",
    }


def _report_components(now):
    """Two app starts (one replaced), a worker that went silent, and a failed broker."""
    from core.application.monitoring.contracts import (
        RuntimeInstallationContext,
        StatusRecord,
        build_observation_reports,
    )
    from core.application.monitoring.queries import RuntimeStatusQueryService
    from core.infrastructure.runtime.monitoring.observations import SourceObservation

    context = RuntimeInstallationContext(
        installation_id="report-test",
        deployment_profile="scalable",
        expectations=(("app", None), ("worker", 1), ("mcp", 1)),
        dependencies=("postgres", "rabbitmq"),
    )

    def stored(age_seconds, **values):
        observed = now - timedelta(seconds=age_seconds)
        base = {
            "service_type": "app",
            "source": "self",
            "original_process_id": str(uuid4()),
            "observer_id": uuid4(),
            "observation_kind": "process",
            "source_sequence": 1,
            "checked_at": observed,
            "source_age_seconds": 0,
            "source_stale_seconds": 180,
            "availability": "observed",
            "lifecycle": "running",
            "started_at": observed - timedelta(minutes=5),
        }
        base.update(values)
        (report,) = build_observation_reports(
            (SourceObservation(**base),), context=context, published_at=observed
        )
        return StatusRecord(**report.model_dump(), id=uuid4(), received_at=observed)

    rows = [
        stored(86400, started_at=now - timedelta(days=2)),
        stored(5),
        stored(900, service_type="worker", consumer_state="active"),
        stored(
            5,
            service_type="rabbitmq",
            source="control.rabbitmq",
            original_process_id=None,
            observation_kind="dependency",
            readiness=False,
        ),
    ]

    class _Rows:
        def load(self, _context):
            return rows

    return RuntimeStatusQueryService(repository=_Rows()).get_components(context, now=now)


def test_the_services_section_leads_with_verdicts_and_caps_history() -> None:
    from datetime import UTC, datetime

    from core.application.reports.system_diagnostic_report import services_section

    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    section = services_section(_report_components(now))

    verdicts = {(item["kind"], item["name"]): item for item in section["summary"]}
    assert section["summary"][0]["severity"] == "problem"
    assert verdicts[("dependency", "rabbitmq")]["verdict"] == "unreachable"
    assert verdicts[("role", "app")]["verdict"] == "healthy"
    assert verdicts[("role", "app")]["health_evidence"] == "self_report"
    assert verdicts[("role", "app")]["running_instances"] == 1
    assert verdicts[("role", "worker")]["verdict"] == "not_reporting"
    assert verdicts[("role", "mcp")]["verdict"] == "not_reporting"
    assert verdicts[("dependency", "postgres")]["verdict"] == "not_reporting"
    assert section["history"]["total"] == 1
    assert all(not row.get("superseded") for row in section["components"]["rows"])


def test_an_unchecked_extension_says_why_its_status_is_unknown() -> None:
    section = extensions_section(
        {
            "extensions": [
                {"extension_id": "local", "runtime_mode": "subprocess", "health_status": "unknown"},
                {"extension_id": "remote", "runtime_mode": "remote_https", "health_status": ""},
                {
                    "extension_id": "foreign",
                    "entitlement_status": "unknown",
                    "status_group": "unknown",
                },
                {
                    "extension_id": "fine",
                    "runtime_mode": "remote_https",
                    "health_status": "healthy",
                },
            ]
        }
    )

    notes = {row["extension_id"]: row.get("status_note") for row in section["extensions"]}
    assert "Local extension: not probed" in notes["local"]
    assert "handshake" in notes["remote"]
    assert "another account" in notes["foreign"]
    assert notes["fine"] is None
