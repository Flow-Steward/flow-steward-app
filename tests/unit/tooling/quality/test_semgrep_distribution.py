"""Downstream scanner packaging must preserve code and complete audit coverage."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest


def _source(tmp_path: Path) -> Path:
    metadata = "Metadata-Version: 2.1\nName: semgrep\nVersion: 1.183.0\n"
    for requirement in (
        "mcp==1.29.0",
        "opentelemetry-api~=1.37.0",
        "opentelemetry-sdk~=1.37.0",
        "opentelemetry-exporter-otlp-proto-http~=1.37.0",
        "opentelemetry-instrumentation-requests~=0.58b0",
        "opentelemetry-instrumentation-threading~=0.58b0",
    ):
        metadata += "Requires-Dist: " + requirement + "\n"
    path = tmp_path / "semgrep-1.183.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("semgrep-1.183.0.dist-info/METADATA", metadata)
        archive.writestr("semgrep-1.183.0.dist-info/RECORD", "")
        info = zipfile.ZipInfo("semgrep-1.183.0.data/purelib/semgrep/bin/semgrep-core")
        info.external_attr = 0o100755 << 16
        archive.writestr(info, b"unchanged-official-engine")
    return path


def test_rebuild_preserves_engine_permissions_and_explicit_local_identity(tmp_path: Path) -> None:
    from core.application.tooling.quality.semgrep_distribution import rebuild_wheel

    source = _source(tmp_path)
    output, receipt = rebuild_wheel(
        source, tmp_path / "out", expected_sha=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert "1.183.0+fs.1" in output.name
    with zipfile.ZipFile(output) as archive:
        engine = "semgrep-1.183.0+fs.1.data/purelib/semgrep/bin/semgrep-core"
        assert archive.read(engine) == b"unchanged-official-engine"
        assert archive.getinfo(engine).external_attr >> 16 == 0o100755
        metadata = archive.read("semgrep-1.183.0+fs.1.dist-info/METADATA").decode()
        assert "Version: 1.183.0+fs.1\n" in metadata
        assert "Requires-Dist: mcp==1.30.0\n" in metadata
        assert "Requires-Dist: opentelemetry-api==1.45.1\n" in metadata
        assert b"semgrep-core,sha256=" in archive.read("semgrep-1.183.0+fs.1.dist-info/RECORD")
    assert (
        receipt["files"]["semgrep/bin/semgrep-core"]
        == hashlib.sha256(b"unchanged-official-engine").hexdigest()
    )


def test_rebuild_rejects_source_digest_mismatch(tmp_path: Path) -> None:
    from core.application.tooling.quality.semgrep_distribution import rebuild_wheel

    with pytest.raises(ValueError, match="digest"):
        rebuild_wheel(_source(tmp_path), tmp_path / "out", expected_sha="0" * 64)


@pytest.mark.parametrize(
    "problem", ["skip", "missing", "version", "duplicate", "empty", "malformed"]
)
def test_audit_validation_fails_closed(tmp_path: Path, problem: str) -> None:
    from core.application.tooling.quality.semgrep_inventory import validate_report

    inventory = {"semgrep": "1.183.0", "mcp": "1.30.0"}
    dependencies = [
        {"name": name, "version": version, "vulns": []} for name, version in inventory.items()
    ]
    if problem == "skip":
        dependencies[0]["skip_reason"] = "not on PyPI"
    if problem == "missing":
        dependencies.pop()
    if problem == "version":
        dependencies[0]["version"] += "+fs.1"
    if problem == "duplicate":
        dependencies.append(dependencies[0])
    if problem == "empty":
        dependencies = []
    report = tmp_path / "report.json"
    report.write_text(json.dumps({} if problem == "malformed" else {"dependencies": dependencies}))
    with pytest.raises(ValueError):
        validate_report(report, inventory)


def test_audit_validation_retains_upstream_and_transitive_advisories(tmp_path: Path) -> None:
    from core.application.tooling.quality.pip_audit_acceptance import evaluate
    from core.application.tooling.quality.semgrep_inventory import validate_report

    inventory = {"semgrep": "1.183.0", "mcp": "1.30.0"}
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "dependencies": [
                    {"name": name, "version": version, "vulns": [{"id": "GHSA-fixture-unsafe"}]}
                    for name, version in inventory.items()
                ]
            }
        )
    )
    validate_report(report, inventory)
    accepted, unaccepted = evaluate(tmp_path / "no-acceptance.md", [f"semgrep:{report}"])
    assert not accepted
    assert {finding.package for finding in unaccepted} == set(inventory)


@pytest.mark.parametrize("status", [1, 2])
def test_audit_process_error_cannot_be_a_green_report(tmp_path: Path, status: int) -> None:
    from core.application.tooling.quality.semgrep_inventory import validate_report

    report = tmp_path / "report.json"
    report.write_text(
        json.dumps({"dependencies": [{"name": "mcp", "version": "1.30.0", "vulns": []}]})
    )
    with pytest.raises(ValueError, match="process status"):
        validate_report(report, {"mcp": "1.30.0"}, audit_status=status)


def test_installed_scanner_and_fixed_dependencies_are_qualified(tmp_path: Path) -> None:
    import subprocess

    inventory = tmp_path / "inventory.txt"
    result = subprocess.run(
        [
            "/opt/semgrep/bin/python",
            "-m",
            "core.application.tooling.quality.semgrep_inventory",
            "inventory",
            "/opt/semgrep/provenance.json",
            str(inventory),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "semgrep==1.183.0\n" in inventory.read_text()
    assert "mcp==1.30.0\n" in inventory.read_text()
    assert "opentelemetry-api==1.45.1\n" in inventory.read_text()
    result = subprocess.run(
        [
            "/opt/semgrep/bin/python",
            "-c",
            "import asyncio; from mcp.server.fastmcp import FastMCP; from semgrep.mcp.server import register; from opentelemetry.sdk.trace import TracerProvider; server=FastMCP('qualification'); register(server); assert asyncio.run(server.list_tools()); provider=TracerProvider(); span=provider.get_tracer('qualification').start_span('check'); assert span.is_recording(); span.end(); provider.shutdown()",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("code", "status", "findings"), [("eval(user_input)\n", 1, 1), ("print(42)\n", 0, 0)]
)
def test_patched_scanner_still_blocks_unsafe_code(
    tmp_path: Path, code: str, status: int, findings: int
) -> None:
    import subprocess

    rule = tmp_path / "rule.json"
    rule.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "id": "qualification-eval",
                        "languages": ["python"],
                        "message": "unsafe eval",
                        "severity": "ERROR",
                        "pattern": "eval(...)",
                    }
                ]
            }
        )
    )
    source = tmp_path / "source.py"
    source.write_text(code)
    result = subprocess.run(
        [
            "/opt/semgrep/bin/semgrep",
            "scan",
            "--metrics=off",
            "--disable-version-check",
            "--error",
            "--json",
            "--no-git-ignore",
            "--config",
            str(rule),
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == status, result.stderr
    assert len(json.loads(result.stdout)["results"]) == findings


@pytest.mark.parametrize("bad_member", ["../escape", "/absolute", "dir\\escape"])
def test_rebuild_rejects_unsafe_members(tmp_path: Path, bad_member: str) -> None:
    from core.application.tooling.quality.semgrep_distribution import rebuild_wheel

    source = _source(tmp_path)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(bad_member, b"untrusted")
    with pytest.raises(ValueError, match="unsafe"):
        rebuild_wheel(
            source, tmp_path / "out", expected_sha=hashlib.sha256(source.read_bytes()).hexdigest()
        )


def test_installed_code_tampering_fails_provenance_check(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from core.application.tooling.quality import semgrep_inventory as policy

    engine = tmp_path / "engine"
    engine.write_bytes(b"modified engine")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "source_sha256": next(iter(policy.UPSTREAM_DIGESTS)),
                "upstream_version": policy.UPSTREAM_VERSION,
                "local_version": policy.LOCAL_VERSION,
                "files": {"engine": hashlib.sha256(b"official engine").hexdigest()},
            }
        )
    )
    monkeypatch.setattr(
        policy.importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            version=policy.LOCAL_VERSION, locate_file=lambda relative: tmp_path / relative
        ),
    )
    with pytest.raises(ValueError, match="provenance mismatch"):
        policy.verified_inventory(receipt)
