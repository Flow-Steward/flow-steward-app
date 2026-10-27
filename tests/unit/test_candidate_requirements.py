from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application.release.candidate_requirements import (
    ConsumerRequirements,
    GroupRequirementDeclaration,
    RequirementContractError,
    RequirementDriftError,
    aggregate_requirements,
    load_group_declarations,
    validate_declaration_coverage,
)

ROOT = Path(__file__).resolve().parents[2]


def _write_manifest(
    path: Path,
    checks: list[tuple[str, str, str]],
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-release-smoke-v1",
                "checks": [
                    {
                        "id": check_id,
                        "group": group,
                        "profile": profile,
                        "required": True,
                        "description": check_id,
                    }
                    for check_id, group, profile in checks
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _group(
    group: str,
    *,
    common: ConsumerRequirements,
    checks: dict[str, ConsumerRequirements],
    sources: tuple[Path, ...] = (),
) -> GroupRequirementDeclaration:
    return GroupRequirementDeclaration(
        group=group,
        sources=sources,
        common=common,
        checks=checks,
    )


def test_selected_check_ids_aggregate_only_their_consumer_inputs(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path / "checks.json",
        [("A01", "A", "oss"), ("C04", "C", "tenant")],
    )
    declarations = {
        "A": _group(
            "A",
            common=ConsumerRequirements(required_env=("FS_SMOKE_BASE",)),
            checks={
                "A01": ConsumerRequirements(required_env=("FS_SMOKE_MODEL",)),
            },
        ),
        "C": _group(
            "C",
            common=ConsumerRequirements(required_env=("FS_SMOKE_BASE",)),
            checks={
                "C04": ConsumerRequirements(required_env=("FS_SMOKE_EMAIL",)),
            },
        ),
    }

    result = aggregate_requirements(
        manifest,
        declarations,
        profiles={"oss", "tenant"},
        check_ids={"A01"},
    )

    assert result.check_ids == ("A01",)
    assert result.groups == ("A",)
    assert result.required_env == ("FS_SMOKE_BASE", "FS_SMOKE_MODEL")


def test_selected_check_includes_only_its_shared_consumer_requirements(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path / "checks.json",
        [("A01", "A", "oss"), ("C04", "C", "tenant")],
    )
    declarations = {
        "shared": _group(
            "shared",
            common=ConsumerRequirements(optional_env=("FS_SMOKE_CANDIDATE_COMMIT",)),
            checks={
                "A01": ConsumerRequirements(required_env=("FS_SMOKE_MODEL",)),
                "C04": ConsumerRequirements(required_env=("FS_SMOKE_EMAIL",)),
            },
        ),
        "A": _group(
            "A",
            common=ConsumerRequirements(required_env=("FS_SMOKE_BASE",)),
            checks={"A01": ConsumerRequirements()},
        ),
        "C": _group(
            "C",
            common=ConsumerRequirements(required_env=("FS_SMOKE_BASE",)),
            checks={"C04": ConsumerRequirements()},
        ),
    }

    result = aggregate_requirements(
        manifest,
        declarations,
        profiles={"oss", "tenant"},
        check_ids={"A01"},
    )

    assert result.required_env == ("FS_SMOKE_BASE", "FS_SMOKE_MODEL")
    assert result.optional_env == ("FS_SMOKE_CANDIDATE_COMMIT",)


def test_source_env_read_missing_from_declaration_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "smoke.sh"
    source.write_text('printf "%s\\n" "$FS_SMOKE_MODEL"\n', encoding="utf-8")
    declaration = _group(
        "A",
        sources=(source,),
        common=ConsumerRequirements(),
        checks={"A01": ConsumerRequirements()},
    )

    with pytest.raises(RequirementDriftError, match="FS_SMOKE_MODEL"):
        validate_declaration_coverage(tmp_path, declaration)


def test_group_declaration_loader_resolves_owned_sources(tmp_path: Path) -> None:
    group_dir = tmp_path / "scripts" / "smoke" / "groupA"
    group_dir.mkdir(parents=True)
    source = group_dir / "smoke_A.sh"
    source.write_text('printf "%s\\n" "$FS_SMOKE_BASE"\n', encoding="utf-8")
    (group_dir / "requirements.json").write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-smoke-requirements-v1",
                "group": "A",
                "sources": ["smoke_A.sh"],
                "common": {
                    "required_env": ["FS_SMOKE_BASE"],
                    "optional_env": [],
                    "services": ["fs-app"],
                    "scopes": [],
                    "mutable_resources": [],
                    "cleanup_obligations": [],
                },
                "checks": {"A01": {}},
            }
        ),
        encoding="utf-8",
    )

    result = load_group_declarations(tmp_path)

    assert tuple(result) == ("A",)
    assert result["A"].sources == (source.resolve(),)
    assert result["A"].checks["A01"] == ConsumerRequirements()


def test_group_declaration_loader_rejects_unknown_fields(tmp_path: Path) -> None:
    group_dir = tmp_path / "scripts" / "smoke" / "groupA"
    group_dir.mkdir(parents=True)
    (group_dir / "requirements.json").write_text(
        json.dumps(
            {
                "schema_version": "flow-steward-smoke-requirements-v1",
                "group": "A",
                "sources": [],
                "common": {},
                "checks": {"A01": {"undeclared_contract": True}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RequirementContractError, match="unknown fields"):
        load_group_declarations(tmp_path)


def test_repository_declarations_cover_manifest_without_overstating_focused_check() -> None:
    declarations = load_group_declarations(ROOT)
    manifest = ROOT / "scripts" / "smoke" / "checks.json"

    for declaration in declarations.values():
        validate_declaration_coverage(ROOT, declaration)
    all_checks = aggregate_requirements(
        manifest,
        declarations,
        profiles={"oss", "tenant", "deploy"},
    )
    cheap_probe = aggregate_requirements(
        manifest,
        declarations,
        profiles={"oss"},
        check_ids={"A05"},
    )
    public_mcp = aggregate_requirements(
        manifest,
        declarations,
        profiles={"oss"},
        check_ids={"A24"},
    )

    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert all_checks.check_ids == tuple(
        sorted(check["id"] for check in manifest_payload["checks"])
    )
    assert "FS_SMOKE_EMAIL" not in cheap_probe.required_env
    assert {"FS_SMOKE_EMAIL", "FS_SMOKE_PASSWORD", "FS_SMOKE_MCP_CA_FILE"}.issubset(
        public_mcp.required_env
    )
