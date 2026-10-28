"""
Pre-0.1.0 cleanup guardrail tests.

These tests prevent regressions introduced by the pre-0.1.0 cleanup
and environment-hardening cleanup. Each test enforces one invariant that
must hold forever after the cleanup is applied.

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_pre_010_cleanup_guardrails.py -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from core.infrastructure.db.schema_state import read_canonical_schema_sql
from tests.support.source_index import whole_words_in

ROOT = Path(__file__).parent.parent.parent
REGISTRY_SEED = ROOT / "db" / "seeds" / "registry_seed.json"
LOOKUP_SEED = ROOT / "db" / "seeds" / "lookup_seed.json"
_RETIRED_BRAND_UPPER = "RETIREDCODENAME"
_RETIRED_BRAND_LOWER = _RETIRED_BRAND_UPPER.lower()
_ALLOWED_HISTORICAL_WORKSPACE_PATH_LINES = {
    "docs/superpowers/plans/2026-09-29-source-release-transition.md": {15},
}


def _legacy_env(name: str) -> str:
    return f"{_RETIRED_BRAND_UPPER}_{name}"


def _legacy_hyphenated(*parts: str) -> str:
    return "-".join((_RETIRED_BRAND_LOWER, *parts))


def _source_paths_for_absolute_path_guard(root: Path) -> list[str]:
    """Return tracked paths, falling back when mounted worktree metadata is unusable."""
    excluded_parts = {
        ".git",
        ".pytest_cache",
        ".superpowers",
        "logs",
        "node_modules",
    }
    tracked_manifest = Path(str(os.environ.get("FS_BOUNDARY_TRACKED_FILES_MANIFEST") or ""))
    resolved_root = root.resolve()
    manifest_scoped_to_root = tracked_manifest.is_absolute() and tracked_manifest.is_relative_to(
        resolved_root
    )
    if (resolved_root == ROOT.resolve() or manifest_scoped_to_root) and tracked_manifest.is_file():
        return [
            path
            for path in tracked_manifest.read_text(encoding="utf-8").splitlines()
            if path and not excluded_parts.intersection(Path(path).parts)
        ]
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=False,
            capture_output=True,
            text=False,
        )
        if result.returncode == 0:
            tracked_paths = [
                raw_name.decode("utf-8", errors="ignore")
                for raw_name in result.stdout.split(b"\0")
                if raw_name
            ]
            return [
                path for path in tracked_paths if not excluded_parts.intersection(Path(path).parts)
            ]

    return [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not excluded_parts.intersection(path.parts)
    ]


def test_absolute_path_guard_falls_back_when_git_metadata_is_unusable(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text(
        "gitdir: /missing/flow-steward/worktrees/unavailable\n",
        encoding="utf-8",
    )
    (tmp_path / "example.py").write_text("print('ok')\n", encoding="utf-8")

    assert "example.py" in _source_paths_for_absolute_path_guard(tmp_path)


def test_absolute_path_guard_prefers_explicit_tracked_manifest(tmp_path: Path, monkeypatch) -> None:
    tracked = tmp_path / "tracked.py"
    tracked.write_text("print('tracked')\n", encoding="utf-8")
    untracked = tmp_path / "untracked.py"
    untracked.write_text("print('untracked')\n", encoding="utf-8")
    manifest = tmp_path / "tracked-files.txt"
    manifest.write_text("tracked.py\n", encoding="utf-8")
    monkeypatch.setenv("FS_BOUNDARY_TRACKED_FILES_MANIFEST", str(manifest))

    assert _source_paths_for_absolute_path_guard(tmp_path) == ["tracked.py"]


def test_absolute_path_guard_excludes_internal_agent_artifacts_in_git_repo(
    tmp_path: Path,
) -> None:
    """Git-backed discovery must use the same internal-artifact exclusions as fallback."""
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "example.py").write_text("value = 1\n", encoding="utf-8")
    report = tmp_path / ".superpowers" / "sdd" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "developer workspace: /Us" + "ers/example/project\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)

    paths = _source_paths_for_absolute_path_guard(tmp_path)

    assert "core/example.py" in paths
    assert ".superpowers/sdd/report.md" not in paths


# ---------------------------------------------------------------------------
# 1. State schema — retired fields must not reappear
# ---------------------------------------------------------------------------

_REMOVED_STATE_FIELDS = frozenset(
    {
        "attachment_context",
        "ui_test_verdict",
        "ui_test_flows_run",
        "ui_test_screenshots",
    }
)
_REMOVED_WORKFLOW_ROUTING_KEY = "workflow" + "_family"
_REMOVED_PARENT_ROUTING_KEY = "parent_" + "workflow" + "_family"
_REMOVED_TRACKER_KEY = "click" + "up"
_REMOVED_PROVIDER_SHIM_PATH = ROOT / "integrations" / (_REMOVED_TRACKER_KEY + "_client.py")


def test_removed_state_fields_absent_from_typeddict(monkeypatch):
    """ui_test_* fields removed in pre-0.1.0 must never reappear in OrchestratorState."""
    monkeypatch.syspath_prepend(str(ROOT))
    from core.application.orchestration.state import OrchestratorState

    annotations = set(OrchestratorState.__annotations__.keys())
    reappeared = _REMOVED_STATE_FIELDS & annotations
    assert not reappeared, (
        f"Removed retired state fields reappeared in OrchestratorState: {sorted(reappeared)}. "
        "These were intentionally deleted in pre-0.1.0 cleanup."
    )


def test_removed_state_fields_absent_from_pydantic_model():
    """ui_test_* fields removed in pre-0.1.0 must never reappear in OrchestratorStateModel."""
    from core.application.orchestration.state import OrchestratorStateModel

    model_fields = set(OrchestratorStateModel.model_fields.keys())
    reappeared = _REMOVED_STATE_FIELDS & model_fields
    assert not reappeared, (
        f"Removed retired state fields reappeared in OrchestratorStateModel: {sorted(reappeared)}."
    )


def test_core_does_not_define_a_platform_postgres_dataset_connector():
    source_files = (ROOT / "core" / "application").rglob("*.py")
    source_text = "\n".join(path.read_text() for path in source_files)

    assert "platform.postgres" not in source_text


def test_registry_seed_does_not_include_removed_alias_workflows():
    """Alias/family workflows removed for first release must stay gone."""
    payload = json.loads(REGISTRY_SEED.read_text())
    workflow_ids = {
        str((row or {}).get("workflow_id") or "").strip()
        for row in (payload.get("workflows") or [])
        if isinstance(row, dict)
    }
    assert "family_quick_fix" not in workflow_ids
    assert all(not workflow_id.startswith("family_") for workflow_id in workflow_ids if workflow_id)


def test_registry_seed_does_not_store_removed_routing_metadata():
    """The removed routing field must not linger in workflow seed metadata."""
    payload = json.loads((ROOT / "db" / "seeds" / "registry_seed.json").read_text())

    offenders: list[str] = []

    def _walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                if key == _REMOVED_WORKFLOW_ROUTING_KEY:
                    offenders.append(child_path)
                _walk(value, child_path)
            return
        if isinstance(node, list):
            for index, item in enumerate(node):
                _walk(item, f"{path}[{index}]")

    _walk(payload.get("workflows") or [], "workflows")
    assert not offenders, (
        f"registry_seed.json still stores removed routing metadata in workflows: {offenders}"
    )


def _seed_workflow_ids() -> set[str]:
    workflow_ids: set[str] = set()
    for path in (REGISTRY_SEED,):
        payload = json.loads(path.read_text())
        for row in payload.get("workflows") or []:
            if not isinstance(row, dict):
                continue
            workflow_id = str((row or {}).get("workflow_id") or "").strip()
            if workflow_id:
                workflow_ids.add(workflow_id)
    return workflow_ids


def _behavioral_module_paths() -> list[Path]:
    return [
        ROOT / "core/application/orchestration/validation_node.py",
        ROOT / "core/application/orchestration/step_hub.py",
        ROOT / "core/application/orchestration/agent_messages.py",
        ROOT / "core/application/orchestration/agents/prompt_agent.py",
        ROOT / "core/application/orchestration/agents/prompt_builder.py",
        ROOT / "core/application/orchestration/graph/builder.py",
        ROOT / "core/application/workflows/serve_workflow_helpers.py",
        ROOT / "core/application/jobs/task_enqueue_runtime.py",
        ROOT / "core/application/management/query_services.py",
        ROOT / "core/application/management/command_services.py",
        ROOT / "core/entrypoints/api/jobs_control.py",
        ROOT / "assets/frontend/src/pages/workflows/detail/index.tsx",
    ]


def test_behavioral_modules_do_not_embed_seeded_workflow_ids():
    """Production behavioral modules must not hardcode authored workflow ids."""
    workflow_ids = sorted(
        workflow_id
        for workflow_id in _seed_workflow_ids() | set(_REMOVED_SEED_IDS["workflows"])
        if workflow_id not in {"maintenance"}
    )
    offenders: list[str] = []
    for path in _behavioral_module_paths():
        text = path.read_text(encoding="utf-8")
        for workflow_id in workflow_ids:
            if workflow_id in text:
                offenders.append(f"{path.relative_to(ROOT)} -> {workflow_id}")
    assert offenders == [], (
        "Behavioral modules hardcode authored workflow ids. "
        f"Remove these literals from runtime/UI logic: {offenders}"
    )


def test_forbidden_runtime_patterns_absent_from_production_sources():
    """Removed routing and alias patterns must not reappear in production code."""
    forbidden_patterns = (
        "if skill ==",
        "if agent_id ==",
        "resolve_workflow_for_task_class",
        "normalize_workflow_id_alias",
        "__auto__",
        "routing_rule",
        "project_default_workflow_id(",
        "include_compatibility_aliases",
        "workflow_task_classes",
        _legacy_env("STRICT_INPUT_SELECTORS"),
        "allow_legacy_fallback",
    )
    offenders: list[str] = []
    for path in _behavioral_module_paths():
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            if pattern in text:
                offenders.append(f"{path.relative_to(ROOT)} -> {pattern}")
    assert offenders == [], (
        f"Forbidden workflow-selection/runtime patterns reappeared in production code: {offenders}"
    )


def test_lookup_seed_does_not_ship_task_classes_catalog():
    payload = json.loads(LOOKUP_SEED.read_text())
    assert "task_classes" not in payload


def test_retired_root_core_shims_are_removed():
    assert not (ROOT / "core" / "state.py").exists()
    assert not (ROOT / "core" / "models.py").exists()


def test_core_identity_schema_uses_uuid_scope_columns():
    """Fresh-install schema must use UUID identity columns before first release."""
    schema = read_canonical_schema_sql()

    table_matches = re.findall(
        r"CREATE TABLE public\.(?P<table>\w+) \((?P<body>.*?)\n\);",
        schema,
        re.DOTALL,
    )
    allowed_text_account_scope_tables = {
        "platform_workflow_knowledge_nodes",
        "platform_workflow_knowledge_edges",
        "platform_workflow_knowledge_documents",
    }
    offending_account_tables = sorted(
        table
        for table, body in table_matches
        if "account_id text" in body and table not in allowed_text_account_scope_tables
    )
    offending_tenant_tables = sorted(
        table
        for table, body in table_matches
        if "tenant_id text" in body and table not in allowed_text_account_scope_tables
    )
    assert offending_account_tables == []
    assert offending_tenant_tables == []
    assert "project_id integer" not in schema
    assert "job_id integer" not in schema
    assert "user_id integer" not in schema
    assert "service_account_id integer" not in schema
    assert "CREATE TABLE public.workspaces" in schema
    assert "DEFAULT '00000000-0000-0000-0000-000000000001'::uuid" in schema
    assert "DEFAULT '00000000-0000-0000-0000-000000000002'::uuid" in schema
    assert "DEFAULT '00000000-0000-0000-0000-000000000003'::uuid" in schema
    accounts_match = re.search(
        r"CREATE TABLE public\.accounts \((?P<body>.*?)\n\);",
        schema,
        re.DOTALL,
    )
    assert accounts_match, "accounts table definition missing"
    assert "id uuid DEFAULT gen_random_uuid() NOT NULL" in accounts_match.group("body")


def test_memory_pgvector_column_lives_in_canonical_schema():
    """Fresh-install schema must not depend on a one-shot job for memory vector columns."""
    schema = read_canonical_schema_sql()
    memory_match = re.search(
        r"CREATE TABLE public\.memory_records \((?P<body>.*?)\n\);",
        schema,
        re.DOTALL,
    )

    assert memory_match, "memory_records table definition missing"
    assert "CREATE EXTENSION IF NOT EXISTS vector;" in schema
    assert "embedding public.vector(1536)" in memory_match.group("body")
    assert "CREATE INDEX idx_memory_records_embedding_cosine" in schema


def test_canonical_schema_foreign_key_column_types_match():
    """Fresh-install schema must not define FK constraints across incompatible types."""
    schema = read_canonical_schema_sql()
    columns: dict[str, dict[str, str]] = {}
    current_table = ""
    for line in schema.splitlines():
        table_match = re.match(r"CREATE TABLE public\.([\w_]+) \(", line)
        if table_match:
            current_table = table_match.group(1)
            columns[current_table] = {}
            continue
        if current_table and line.startswith(");"):
            current_table = ""
            continue
        if not current_table:
            continue
        column_match = re.match(r"\s+([\w_]+)\s+([^,]+)", line)
        if not column_match or column_match.group(1).upper() in {
            "CONSTRAINT",
            "PRIMARY",
            "UNIQUE",
            "FOREIGN",
            "CHECK",
        }:
            continue
        raw_type = column_match.group(2).strip()
        normalized_type = re.split(
            r"\s+(GENERATED|DEFAULT|NOT|NULL|COLLATE|CONSTRAINT|PRIMARY|UNIQUE|REFERENCES|CHECK)\b",
            raw_type,
            flags=re.IGNORECASE,
        )[0].strip()
        columns[current_table][column_match.group(1)] = normalized_type

    fk_pattern = re.compile(
        r"ALTER TABLE ONLY public\.([\w_]+)\n"
        r"\s+ADD CONSTRAINT [^\n]+ FOREIGN KEY \(([^)]+)\) "
        r"REFERENCES public\.([\w_]+)\(([^)]+)\)",
        re.MULTILINE,
    )
    mismatches: list[str] = []
    for table, local_columns, ref_table, ref_columns in fk_pattern.findall(schema):
        for local_column, ref_column in zip(
            [item.strip() for item in local_columns.split(",")],
            [item.strip() for item in ref_columns.split(",")],
            strict=False,
        ):
            local_type = columns.get(table, {}).get(local_column)
            ref_type = columns.get(ref_table, {}).get(ref_column)
            if local_type != ref_type:
                mismatches.append(
                    f"{table}.{local_column} {local_type} -> {ref_table}.{ref_column} {ref_type}"
                )

    assert mismatches == []


def test_projects_require_workspace_in_canonical_schema():
    """No project may exist without a workspace in the canonical SQL artifact."""
    schema = read_canonical_schema_sql()
    projects_match = re.search(
        r"CREATE TABLE public\.projects \((?P<body>.*?)\n\);",
        schema,
        re.DOTALL,
    )
    assert projects_match, "projects table definition missing"
    body = projects_match.group("body")
    assert "id uuid DEFAULT gen_random_uuid() NOT NULL" in body
    assert "workspace_id uuid" in body
    assert re.search(r"workspace_id uuid .*NOT NULL", body)
    assert "projects_workspace_id_fkey" in schema


def test_lookup_seed_uses_default_uuid_identity_constants():
    """Default tenant/account/workspace seed must use deterministic UUIDs as IDs."""
    payload = json.loads((ROOT / "db" / "seeds" / "lookup_seed.json").read_text())
    account = (payload.get("accounts") or [{}])[0]
    workspace = (payload.get("workspaces") or [{}])[0]

    assert account["id"] == "00000000-0000-0000-0000-000000000002"
    assert account["tenant_id"] == "00000000-0000-0000-0000-000000000001"
    assert account["account_key"] == "default"
    assert workspace["id"] == "00000000-0000-0000-0000-000000000003"
    assert workspace["tenant_id"] == "00000000-0000-0000-0000-000000000001"
    assert workspace["account_id"] == account["id"]
    assert workspace["workspace_key"] == "default"


def test_the_lookup_seed_registers_no_extension_backed_provider() -> None:
    """A clean platform must not ship knowing which extensions exist.

    An extension that declares itself a provider registers one when it is enabled,
    from its own typed `kind`, identity and `features`.
    """
    payload = json.loads(LOOKUP_SEED.read_text(encoding="utf-8"))
    extension_backed = [
        row["provider_key"]
        for row in payload["providers"]
        if row.get("implementation_kind") == "extension"
    ]

    assert extension_backed == [], extension_backed


def test_registry_seed_does_not_ship_workflow_task_class_mappings():
    """Workflow selection must not depend on retired task-class mappings."""
    payload = json.loads((ROOT / "db" / "seeds" / "registry_seed.json").read_text())
    assert "workflow_task_classes" not in payload


def test_registry_seed_does_not_ship_placeholder_duplicate_workflow_steps():
    payload = json.loads((ROOT / "db" / "seeds" / "registry_seed.json").read_text())

    def _norm(value):
        normalized = str(value or "").strip()
        return "" if normalized.lower() == "none" else normalized

    offenders = []
    for workflow in payload.get("workflows") or []:
        workflow_id = str(workflow.get("workflow_id") or "").strip()
        for phase_key in ("_phases", "phases"):
            for phase in workflow.get(phase_key) or []:
                phase_id = str(phase.get("phase_id") or "").strip()
                for steps_key in ("_steps", "steps"):
                    steps = phase.get(steps_key) or []
                    if not steps:
                        continue
                    seen = set()
                    for step in steps:
                        key = (
                            _norm(step.get("step_kind")) or "agent",
                            _norm(step.get("agent_id")),
                            _norm(step.get("skill_id")),
                            _norm(step.get("connector_id")),
                            _norm(step.get("workflow_id")),
                            _norm(step.get("participant_key")),
                            _norm(step.get("block_key")),
                        )
                        label = str(step.get("display_name") or step.get("step_id") or "").strip()
                        placeholder = not label or label.lower().replace("_", " ").startswith(
                            "step "
                        )
                        if key in seen and placeholder:
                            offenders.append(
                                {
                                    "workflow_id": workflow_id,
                                    "phase_id": phase_id,
                                    "phase_key": phase_key,
                                    "steps_key": steps_key,
                                    "step_id": step.get("id"),
                                    "label": label,
                                }
                            )
                        seen.add(key)

    assert offenders == []


def test_deprecated_tracker_core_client_shim_removed():
    """Extension-backed providers must not keep retired direct tracker clients in core integrations."""
    assert not _REMOVED_PROVIDER_SHIM_PATH.exists(), (
        "A removed direct-provider client shim has reappeared under integrations/. "
        "Task providers must be supplied only through extensions."
    )


def test_retired_tracker_key_is_not_in_core_release_surface():
    """The removed tracker integration must not reappear outside future extension work."""
    forbidden = _REMOVED_TRACKER_KEY
    scan_roots = [
        ROOT / "core",
        ROOT / "db",
        ROOT / "docs",
        ROOT / "infra",
        ROOT / "assets",
        ROOT / "locales",
        ROOT / "tests",
        ROOT / "README.md",
        ROOT / ".env.example",
        ROOT / ".env.example.prod",
        ROOT / ".env.example.ops",
        ROOT / "install.sh",
    ]
    ignored = {
        Path("core/infrastructure/extension_sdk/public/sdk-wheel-attestation.json"),
        Path("docs/configuration/flow-steward-env-audit.json"),
    }
    # Historical audit, task, and design records describe retired integrations;
    # they are not executable release surfaces and must remain truthful records.
    ignored_roots = (
        Path("docs/audits"),
        Path("docs/tasks"),
        Path("docs/superpowers/specs"),
    )
    suffixes = {
        "",
        ".cfg",
        ".css",
        ".html",
        ".json",
        ".md",
        ".po",
        ".py",
        ".sh",
        ".sql",
        ".toml",
        ".tsx",
        ".ts",
        ".txt",
        ".yaml",
        ".yml",
        ".xml",
    }
    bad: list[str] = []
    for root in scan_roots:
        paths = [root] if root.is_file() else list(root.rglob("*"))
        for path in paths:
            if not path.is_file() or path.suffix not in suffixes:
                continue
            rel = path.relative_to(ROOT)
            if rel in ignored or any(rel.is_relative_to(prefix) for prefix in ignored_roots):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            if forbidden in text:
                bad.append(str(rel))

    assert not bad, (
        "Removed task-tracker provider key reappeared in core release surfaces: "
        f"{sorted(set(bad))}. Keep service-specific integrations out of Core."
    )


# ---------------------------------------------------------------------------
# 2. System email — sent through an email extension, never configured from env
# ---------------------------------------------------------------------------


def test_core_reads_no_system_email_env():
    """System email goes through a connected email extension; no env var configures it."""
    offenders = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "core").rglob("*.py")
        if "SYSTEM_EMAIL_" in path.read_text(encoding="utf-8")
    )
    assert not offenders, f"SYSTEM_EMAIL_* is read in: {offenders}"
    assert not (ROOT / "core" / "infrastructure" / "mail").exists()


# ---------------------------------------------------------------------------
# 3. Version — hardcoded "2.0.0" must not appear in the launcher or concrete services
# ---------------------------------------------------------------------------


def test_serve_py_does_not_hardcode_old_version():
    """The retired root launcher cannot reintroduce a hardcoded version."""
    assert not (ROOT / "serve.py").exists(), (
        "The retired root serve.py launcher must stay removed; use the canonical "
        "core/entrypoints server modules and core.version."
    )


def test_api_composition_does_not_hardcode_old_version():
    """The canonical API composition must not hardcode the retired version."""
    source = (ROOT / "core" / "entrypoints" / "api" / "app.py").read_text()
    assert "2.0.0" not in source, (
        "Hardcoded version '2.0.0' found in the canonical API composition. "
        "Use APP_VERSION from core.version (reads VERSION file)."
    )


def test_extension_services_does_not_hardcode_old_version():
    """Concrete extension services must not contain the retired version."""
    extensions_root = ROOT / "core" / "application" / "extensions"
    for module_name in ("command_service.py", "query_service.py", "service_support.py"):
        assert "2.0.0" not in (extensions_root / module_name).read_text()


def test_version_file_exists_and_is_semver():
    """VERSION file at repo root must exist and contain a valid semver string."""
    version_file = ROOT / "VERSION"
    assert version_file.exists(), "VERSION file missing from repo root."
    version = version_file.read_text().strip()
    assert re.match(r"^\d+\.\d+\.\d+", version), (
        f"VERSION file does not start with a semver string: {version!r}"
    )


def test_core_version_reads_version_file():
    """core/version.py must export APP_VERSION matching the VERSION file."""
    from core.infrastructure.config.version import APP_VERSION

    expected = (ROOT / "VERSION").read_text().strip()
    assert expected == APP_VERSION, (
        f"core.version.APP_VERSION={APP_VERSION!r} does not match VERSION file={expected!r}"
    )


# ---------------------------------------------------------------------------
# 4. Static assets — renamed files must exist; old names must not
# ---------------------------------------------------------------------------

_ASSETS = ROOT / "assets"

_RENAMED_ASSETS_NEW = [
    "platform-renderer.js",
    "platform-components.css",
    "platform-kit/index.js",
    "platform-kit/index-" + "ap" + "p.js",
]

_RENAMED_ASSETS_OLD = [
    _legacy_hyphenated("renderer.js"),
    _legacy_hyphenated("components.css"),
    f"{_RETIRED_BRAND_LOWER}-kit/index.js",
    f"{_RETIRED_BRAND_LOWER}-kit/index-" + "ap" + "p.js",
]


def test_new_assets_exist():
    """Renamed platform assets must exist in the canonical assets/ root."""
    for rel in _RENAMED_ASSETS_NEW:
        path = _ASSETS / rel
        assert path.exists(), (
            f"Expected renamed platform asset missing: assets/{rel}. "
            "Do not revert the platform-renderer / platform-kit rename."
        )


def test_old_static_assets_absent():
    """Retired renderer/kit asset names must not reappear in assets/."""
    for rel in _RENAMED_ASSETS_OLD:
        path = _ASSETS / rel
        assert not path.exists(), (
            f"Old static asset name has reappeared: assets/{rel}. "
            "These were renamed to platform-* in pre-0.1.0 cleanup."
        )


def test_retired_static_root_removed():
    """Retired root static/ must be removed after assets migration."""
    assert not (ROOT / "static").exists(), (
        "Retired root static/ must not exist after asset migration to assets/."
    )


# ---------------------------------------------------------------------------
# 5. Env var naming — auth/runtime modules must not read retired-prefix env aliases
# ---------------------------------------------------------------------------

_RENAMED_ENV_PATTERNS = [
    _legacy_env("AUTH_COOKIE_SECURE"),
    _legacy_env("AUTH_COOKIE_NAME"),
    _legacy_env("AUTH_SESSION_TTL"),
    _legacy_env("AUTH_PASSWORD_MIN"),
    _legacy_env("AUTH_LOGIN_RATE"),
    _legacy_env("AUTH_LOGIN_MAX"),
    _legacy_env("AUTH_LOGIN_LOCKOUT"),
    _legacy_env("AUTH_PASSWORD_RESET"),
    _legacy_env("LOCAL_AUTH_ENABLED"),
    _legacy_env("ATTACHMENT"),
    _legacy_env("EXTENSION"),
    _legacy_env("TRACE_REDACT"),
    _legacy_env("DEFAULT_PROVIDER"),
    _legacy_env("DEFAULT_MODEL"),
    _legacy_env("DEFAULT_TEMPERATURE"),
    _legacy_env("CHUNK_SIZE"),
    _legacy_env("CHUNK_OVERLAP"),
]

_CONTROL_ENV_FORBIDDEN = [
    _legacy_env("PROJECT_LEASE"),
    _legacy_env("WATCHDOG"),
    _legacy_env("SCHEDULED_JOBS"),
    _legacy_env("DASHBOARD_ROLLUP"),
    _legacy_env("SECURITY_POSTURE"),
]

_WEBHOOK_ENV_FORBIDDEN = [
    _legacy_env("CLICK" + "UP_" + "WEBHOOK"),
    _legacy_env("SENTRY_WEBHOOK"),
]

_PROVIDER_WEBHOOK_ENV_PREFIX = "CLICK" + "UP_" + "WEBHOOK_"
_PLATFORM_WEBHOOK_ENV_FORBIDDEN = [
    rf"\b{_PROVIDER_WEBHOOK_ENV_PREFIX}\w+\b",
]

_HARNESS_ENV_FORBIDDEN = [
    _legacy_env("APP_PORT"),
    _legacy_env("BASE_URL"),
    _legacy_env("E2E"),
    _legacy_env("TEST_PROJECT"),
    _legacy_env("NIGHTFIRE"),
    _legacy_env("REGISTRY_MODE"),
    _legacy_env("PROJECT_PATH"),
    r"FS_MCP_BEARER_TOKEN",
]

_BASE_ENV_OVERLAY_ONLY = [
    r"^APP_BASE_URL$",
    r"^TEST_RUNNER_BASE_URL$",
    r"^E2E_\w+",
    r"^TEST_PROJECT_\w+",
    r"^NIGHTFIRE_\w+",
    r"^SONAR_\w+",
    r"^APP_WORKERS$",
    r"^CONTROL_LOOPS_ENABLED$",
    r"^FS_PROJECT_LEASE_\w+",
    r"^FS_WATCHDOG_\w+",
    r"^FS_SCHEDULED_JOBS_\w+",
    r"^FS_DASHBOARD_ROLLUP_\w+",
    r"^POSTGRES_\w+",
    r"^NEO4J_HEAP_\w+",
    r"^SECURITY_\w+",
    r"^ARTIFACT_\w+",
    r"^SENTRY_\w+",
    r"^FS_AUTH_COOKIE_NAME$",
    r"^FS_AUTH_CSRF_COOKIE_NAME$",
    r"^FS_AUTH_CSRF_HEADER_NAME$",
    r"^FS_AUTH_SESSION_TTL_HOURS$",
    r"^FS_AUTH_REMEMBER_SESSION_TTL_HOURS$",
    r"^FS_AUTH_LOGIN_\w+",
    r"^FS_AUTH_PASSWORD_ITERATIONS$",
    r"^FS_AUTH_PASSWORD_MIN_LENGTH$",
    r"^FS_AUTH_PASSWORD_RESET_\w+",
    r"^FS_DEFAULT_ACCOUNT_ID$",
    r"^FS_METRICS_TOKEN$",
]

_PROD_ENV_OVERLAY_ONLY = [
    r"^APP_BASE_URL$",
    r"^TEST_RUNNER_BASE_URL$",
    r"^E2E_\w+",
    r"^TEST_PROJECT_\w+",
    r"^NIGHTFIRE_\w+",
    r"^SONAR_\w+",
    r"^APP_WORKERS$",
    r"^CONTROL_LOOPS_ENABLED$",
    r"^FS_PROJECT_LEASE_\w+",
    r"^FS_WATCHDOG_\w+",
    r"^FS_SCHEDULED_JOBS_\w+",
    r"^FS_DASHBOARD_ROLLUP_\w+",
    r"^POSTGRES_COMPUTE_QUERY_ID$",
    r"^POSTGRES_SHARED_PRELOAD_LIBRARIES$",
    r"^POSTGRES_SESSION_PRELOAD_LIBRARIES$",
    r"^POSTGRES_LOG_\w+",
    r"^POSTGRES_DEADLOCK_TIMEOUT$",
    r"^POSTGRES_TRACK_IO_TIMING$",
    r"^POSTGRES_SHARED_BUFFERS$",
    r"^POSTGRES_EFFECTIVE_CACHE_SIZE$",
    r"^POSTGRES_WORK_MEM$",
    r"^POSTGRES_MAINTENANCE_WORK_MEM$",
    r"^POSTGRES_RANDOM_PAGE_COST$",
    r"^POSTGRES_EFFECTIVE_IO_CONCURRENCY$",
    r"^POSTGRES_CHECKPOINT_COMPLETION_TARGET$",
    r"^POSTGRES_WAL_COMPRESSION$",
    r"^POSTGRES_MAX_WAL_SIZE$",
    r"^POSTGRES_MIN_WAL_SIZE$",
    r"^POSTGRES_DEFAULT_STATISTICS_TARGET$",
    r"^POSTGRES_JIT$",
    r"^POSTGRES_PG_STAT_STATEMENTS_\w+",
    r"^POSTGRES_AUTO_EXPLAIN_\w+",
    r"^NEO4J_HEAP_\w+",
    # SECURITY_PII_SALT is deliberately NOT overlay-only: production refuses
    # structured-output agent steps without it, so a production template that
    # omits it ships a deployment where no agent workflow can run. The rest of
    # the SECURITY_* knobs stay in .env.example.ops.
    r"^SECURITY_(?!PII_SALT)\w+",
    r"^ARTIFACT_\w+",
    r"^SENTRY_\w+",
    r"^FS_AUTH_COOKIE_NAME$",
    r"^FS_AUTH_CSRF_COOKIE_NAME$",
    r"^FS_AUTH_CSRF_HEADER_NAME$",
    r"^FS_AUTH_SESSION_TTL_HOURS$",
    r"^FS_AUTH_REMEMBER_SESSION_TTL_HOURS$",
    r"^FS_AUTH_LOGIN_\w+",
    r"^FS_AUTH_PASSWORD_ITERATIONS$",
    r"^FS_AUTH_PASSWORD_MIN_LENGTH$",
    r"^FS_AUTH_PASSWORD_RESET_\w+",
    r"^FS_DEFAULT_ACCOUNT_ID$",
    r"^FS_METRICS_TOKEN$",
]


def _env_name_hits(path: Path, patterns: list[str]) -> list[str]:
    source = path.read_text()
    hits: set[str] = set()
    for pattern in patterns:
        hits.update(re.findall(pattern, source, re.MULTILINE))
    return sorted(hits)


def test_auth_config_does_not_read_old_brand_auth_env_vars():
    """core/application/auth/config.py must not read retired-prefix auth vars."""
    source = (ROOT / "core" / "application" / "auth" / "config.py").read_text()
    hits = [p for p in _RENAMED_ENV_PATTERNS if re.search(p, source)]
    assert not hits, (
        f"core/application/auth/config.py still reads retired-prefix env vars: {hits}. "
        "These were renamed to FS_* in pre-0.1.0 cleanup."
    )


def test_core_config_does_not_read_old_brand_env_vars():
    """runtime_config.py must not read retired-prefix attachment/extension/trace vars."""
    source = (ROOT / "core" / "infrastructure" / "config" / "runtime_config.py").read_text()
    hits = [p for p in _RENAMED_ENV_PATTERNS if re.search(p, source)]
    assert not hits, (
        f"core/infrastructure/config/runtime_config.py still reads retired-prefix env vars: {hits}. "
        "These were renamed to FS_* in pre-0.1.0 cleanup."
    )


def test_serve_py_does_not_read_old_brand_auth_env_vars():
    """The retired root launcher cannot reintroduce old auth environment names."""
    assert not (ROOT / "serve.py").exists(), (
        "The retired root serve.py launcher must stay removed; canonical entrypoints "
        "must use FS_* environment names."
    )


def test_api_composition_does_not_read_old_brand_auth_env_vars():
    """The canonical API composition must not read retired-prefix auth vars."""
    source = (ROOT / "core" / "entrypoints" / "api" / "app.py").read_text()
    hits = [p for p in _RENAMED_ENV_PATTERNS if re.search(p, source)]
    assert not hits, (
        f"Canonical API composition still reads retired-prefix env vars: {hits}. "
        "These were renamed to FS_* in pre-0.1.0 cleanup."
    )


def test_control_plane_modules_do_not_read_old_brand_control_env_vars():
    """control/ and monitoring modules must not read pre-cleanup retired control-loop env vars."""
    files = [
        ROOT / "core" / "infrastructure" / "runtime" / "control" / "config.py",
        ROOT / "core" / "infrastructure" / "runtime" / "control" / "loops.py",
        ROOT / "core" / "entrypoints" / "cli" / "control_process.py",
        ROOT / "core" / "entrypoints" / "api" / "health.py",
        ROOT / "core" / "entrypoints" / "api" / "system_monitoring.py",
        ROOT / "core" / "application" / "management" / "command_specs" / "jobs.py",
        ROOT / "core" / "entrypoints" / "cli" / "platform" / "service_bindings" / "jobs.py",
        ROOT / "core" / "application" / "jobs" / "ops_health_snapshot_runtime.py",
        ROOT / "core" / "infrastructure" / "runtime" / "worker" / "queue.py",
    ]
    bad: dict[str, list[str]] = {}
    for path in files:
        source = path.read_text()
        hits = [p for p in _CONTROL_ENV_FORBIDDEN if re.search(p, source)]
        if hits:
            bad[str(path.relative_to(ROOT))] = hits
    assert not bad, (
        f"Control-plane modules still reference old retired control env vars: {bad}. "
        "Use FS_* for worker leases and control loops."
    )


def test_provider_webhook_modules_do_not_read_old_brand_webhook_env_vars():
    """Provider webhook runtime must use provider-specific env names without the old retired prefix."""
    files = [
        ROOT / "core" / "entrypoints" / "api" / "provider_webhooks.py",
        ROOT / "core" / "application" / "extensions" / "provider_webhook_security.py",
    ]
    bad: dict[str, list[str]] = {}
    for path in files:
        source = path.read_text()
        hits = [p for p in _WEBHOOK_ENV_FORBIDDEN if re.search(p, source)]
        if hits:
            bad[str(path.relative_to(ROOT))] = hits
    assert not bad, (
        f"Webhook runtime still references old retired webhook env vars: {bad}. "
        "Use extension-owned provider secrets instead of platform webhook env names."
    )


def test_env_examples_do_not_reintroduce_old_harness_or_control_env_names():
    """.env.example files must not advertise retired-prefix control/test env names anymore."""
    files = [
        ROOT / ".env.example",
        ROOT / ".env.example.prod",
    ]
    bad: dict[str, list[str]] = {}
    patterns = (
        _CONTROL_ENV_FORBIDDEN
        + _WEBHOOK_ENV_FORBIDDEN
        + _PLATFORM_WEBHOOK_ENV_FORBIDDEN
        + _HARNESS_ENV_FORBIDDEN
    )
    for path in files:
        source = path.read_text()
        hits = [p for p in patterns if re.search(p, source)]
        if hits:
            bad[path.name] = hits
    assert not bad, (
        f"Example env files still advertise removed old-brand names: {bad}. "
        "Keep the public env contract on FS_/APP_/E2E_/MCP_ namespaces."
    )


def test_retired_tracker_webhook_env_is_not_platform_configuration():
    """Removed tracker webhook secrets must not be platform env configuration."""
    files = [
        ROOT / ".env.example",
        ROOT / ".env.example.prod",
        ROOT / ".env.example.ops",
        ROOT / "docs" / "GUIDE.md",
        ROOT / "infra" / "docker" / "docker-compose.yaml",
        ROOT / "infra" / "docker" / "docker-compose.prod.yaml",
        ROOT / "core" / "entrypoints" / "api" / "provider_webhooks.py",
        ROOT / "core" / "application" / "extensions" / "provider_webhook_security.py",
    ]
    bad: dict[str, list[str]] = {}
    for path in files:
        source = path.read_text()
        hits = [p for p in _PLATFORM_WEBHOOK_ENV_FORBIDDEN if re.search(p, source)]
        if hits:
            bad[str(path.relative_to(ROOT))] = hits
    assert not bad, (
        f"Removed tracker webhook env vars leaked back into platform configuration: {bad}. "
        "Use extension-owned provider secrets instead."
    )


def test_overlay_env_examples_exist():
    """Layered env templates must exist so quickstart files stay small and focused."""
    for rel in (".env.example.test", ".env.example.quality", ".env.example.ops"):
        path = ROOT / rel
        assert path.exists(), f"Missing overlay env template: {rel}"


def test_dev_env_example_stays_minimal():
    """.env.example must not absorb test, quality, or advanced ops knobs."""
    path = ROOT / ".env.example"
    hits = _env_name_hits(path, _BASE_ENV_OVERLAY_ONLY)
    assert not hits, (
        f".env.example should stay minimal, but found overlay-only vars: {hits}. "
        "Move test/quality/ops knobs into their dedicated overlay files."
    )


def test_prod_env_example_stays_minimal():
    """.env.example.prod must not absorb test, quality, or advanced ops knobs."""
    path = ROOT / ".env.example.prod"
    hits = _env_name_hits(path, _PROD_ENV_OVERLAY_ONLY)
    assert not hits, (
        f".env.example.prod should stay minimal, but found overlay-only vars: {hits}. "
        "Move non-core production knobs into .env.example.ops."
    )


def test_overlay_env_examples_advertise_expected_namespaces():
    """Overlay templates should contain the namespaces they were created for."""
    test_overlay = (ROOT / ".env.example.test").read_text()
    quality_overlay = (ROOT / ".env.example.quality").read_text()
    ops_overlay = (ROOT / ".env.example.ops").read_text()
    assert "E2E_ADMIN_USERNAME=" in test_overlay
    assert "NIGHTFIRE_LANE=" in test_overlay
    assert "SONAR_HOST_URL=" in quality_overlay
    assert "SONAR_TOKEN=" in quality_overlay
    assert "APP_WORKERS=" in ops_overlay
    assert "FS_WATCHDOG_ENABLED=" in ops_overlay


def test_test_overlay_does_not_reintroduce_generic_test_aliases():
    """.env.example.test must use explicit UI/backend naming, not generic alias names."""
    source = (ROOT / ".env.example.test").read_text()
    forbidden = re.findall(
        r"^(TEST_PROJECT_ID|TEST_PROJECT_NAME|E2E_AUTH_USERNAME|E2E_AUTH_PASSWORD|E2E_AUTH_DISPLAY_NAME)$",
        source,
        re.MULTILINE,
    )
    assert not forbidden, (
        f".env.example.test still advertises generic alias names: {forbidden}. "
        "Use explicit TEST_PROJECT_UI_*/TEST_PROJECT_BACKEND_* and role-specific E2E_* names only."
    )


def test_runtime_env_helper_has_no_retired_lane_or_generic_test_aliases():
    """runtime_env.py must not preserve retired lanes or generic test-env aliases."""
    source = (ROOT / "core" / "application" / "tooling" / "testing" / "runtime_env.py").read_text()
    retired_lane_marker = "retired"
    forbidden = re.findall(
        rf"{retired_lane_marker}|TEST_PROJECT_ID|TEST_PROJECT_NAME|E2E_AUTH_USERNAME|E2E_AUTH_PASSWORD|"
        rf"E2E_AUTH_DISPLAY_NAME|{re.escape(_legacy_env('ACTIVE_TEST_ROLE'))}|"
        rf"{re.escape(_legacy_env('ACTIVE_TEST_LANE'))}|E2E_TEST_ROLE|"
        rf"{re.escape(_legacy_env('UI_ENFORCE_AUTH'))}",
        source,
    )
    assert not forbidden, (
        f"runtime_env.py still contains retired/generic test-env aliases: {sorted(set(forbidden))}. "
        "Pre-1.0 test harness must use explicit UI/backend env names only."
    )


# ---------------------------------------------------------------------------
# 6. Version — health.py and system_monitoring.py must not hardcode "2.0.0"
# ---------------------------------------------------------------------------


def test_health_route_does_not_hardcode_version():
    """core/entrypoints/api/health.py must not contain the hardcoded retired version '2.0.0'."""
    source = (ROOT / "core" / "entrypoints" / "api" / "health.py").read_text()
    assert "2.0.0" not in source, (
        "Hardcoded version '2.0.0' found in core/entrypoints/api/health.py. "
        "Use APP_VERSION from core.version (reads VERSION file)."
    )


def test_system_monitoring_does_not_hardcode_version():
    """core/entrypoints/api/system_monitoring.py must not contain the hardcoded retired version."""
    source = (ROOT / "core" / "entrypoints" / "api" / "system_monitoring.py").read_text()
    assert "2.0.0" not in source, (
        "Hardcoded version '2.0.0' found in core/entrypoints/api/system_monitoring.py. "
        "Use APP_VERSION from core.version."
    )


# ---------------------------------------------------------------------------
# 7. Auth modules — serve_auth_runtime.py must not read retired-prefix auth/base-url aliases
# ---------------------------------------------------------------------------

_AUTH_RUNTIME_FORBIDDEN = [
    _legacy_env("PUBLIC_BASE_URL"),
    _legacy_env("AUTH_TRUSTED_ORIGINS"),
    _legacy_env("AUTH_BOOTSTRAP_ENABLED"),
    _legacy_env("AUTH_BOOTSTRAP_TOKEN"),
    _legacy_env("AUTH_BOOTSTRAP_LOCALHOST_ONLY"),
    _legacy_env("LOCAL_AUTH_ENABLED"),
]


def test_serve_auth_runtime_does_not_read_old_brand_env_vars():
    """core/application/auth/serve_auth_runtime.py must not read any retired-prefix auth vars."""
    source = (ROOT / "core" / "application" / "auth" / "serve_auth_runtime.py").read_text()
    hits = [p for p in _AUTH_RUNTIME_FORBIDDEN if re.search(p, source)]
    assert not hits, (
        f"core/application/auth/serve_auth_runtime.py still references retired-prefix vars: {hits}. "
        "These were renamed to FS_* in pre-0.1.0 cleanup."
    )


def test_auth_modules_do_not_read_old_brand_auth_env_vars():
    """All core/application/auth/*.py files must not read retired-prefix auth vars."""
    auth_dir = ROOT / "core" / "application" / "auth"
    sources = {p.name: p.read_text() for p in auth_dir.glob("*.py")}
    bad: dict[str, list[str]] = {}
    for fname, source in sources.items():
        hits = [p for p in _AUTH_RUNTIME_FORBIDDEN if re.search(p, source)]
        if hits:
            bad[fname] = hits
    assert not bad, (
        f"core/application/auth modules still reference retired-prefix vars: {bad}. "
        "These were renamed to FS_* in pre-0.1.0 cleanup."
    )


# ---------------------------------------------------------------------------
# 8. Frontend toolchain — eslint, tailwind, sonar must reference platform-kit, not retired names
# ---------------------------------------------------------------------------


def test_eslint_config_references_platform_kit():
    """eslint.config.mjs must reference platform-kit and platform-renderer, not retired names."""
    source = (ROOT / "eslint.config.mjs").read_text()
    legacy_kit = f"{_RETIRED_BRAND_LOWER}-kit"
    legacy_renderer = _legacy_hyphenated("renderer")
    assert legacy_kit not in source, (
        "eslint.config.mjs still references the retired kit path. Update to 'platform-kit'."
    )
    assert legacy_renderer not in source, (
        "eslint.config.mjs still references the retired renderer path. Update to 'platform-renderer'."
    )
    assert "platform-kit" in source, (
        "eslint.config.mjs does not reference 'platform-kit'. Was the glob updated?"
    )


def test_tailwind_config_references_platform_kit():
    """tailwind.config.js must reference platform-kit, not retired kit paths or web UI templates."""
    source = (ROOT / "tailwind.config.js").read_text()
    assert f"{_RETIRED_BRAND_LOWER}-kit" not in source, (
        "tailwind.config.js still references the retired kit path. Update to 'platform-kit'."
    )
    assert "platform-kit" in source, (
        "tailwind.config.js does not reference 'platform-kit'. Was the content glob updated?"
    )
    assert "templates" not in source, (
        "tailwind.config.js still scans web UI templates. React owns the frontend source now."
    )
    assert ".html" not in source, (
        "tailwind.config.js still scans web UI templates. React owns the frontend source now."
    )


def test_babel_config_does_not_scan_removed_web_ui_templates():
    """babel.cfg must not retain the removed Jinja web UI template extractor."""
    source = (ROOT / "babel.cfg").read_text()
    assert "jinja2" not in source.lower(), (
        "babel.cfg still configures a Jinja extractor after the React cutover."
    )
    assert "templates" not in source, (
        "babel.cfg still scans removed web UI templates after the React cutover."
    )
    assert ".html" not in source, (
        "babel.cfg still scans removed web UI templates after the React cutover."
    )


def test_root_templates_directory_stays_removed():
    """Root templates/ must not return as a placeholder for removed web UI templates."""
    assert not (ROOT / "templates").exists(), (
        "Root templates/ exists. Product/admin UI is React-only; non-UI templates must live "
        "inside their owning infrastructure package."
    )


def test_removed_tailwind_and_root_npm_artifacts_stay_out_of_build_context():
    """Ignored local artifacts must not become live static files through Docker COPY . ."""
    forbidden_paths = (
        ROOT / "assets" / "tailwind.css",
        ROOT / "package.json",
        ROOT / "package-lock.json",
    )
    present = [str(path.relative_to(ROOT)) for path in forbidden_paths if path.exists()]
    assert not present, (
        f"Release build context contains root/template-era generated artifacts: {present}"
    )

    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "/assets/tailwind.css" in dockerignore
    assert "/package.json" in dockerignore
    assert "/package-lock.json" in dockerignore


def test_tracked_source_has_no_developer_machine_absolute_paths():
    """Tracked source/config/tests must not bake in local workstation paths."""
    forbidden = (
        "/Us" + "ers/",
        "file:///Us" + "ers",
        "/private/" + "var",
        "/var/" + "folders",
        "/Vol" + "umes/",
    )
    text_suffixes = {
        ".cfg",
        ".css",
        ".csv",
        ".env",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsx",
        ".log",
        ".md",
        ".mjs",
        ".py",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
    relative_paths = _source_paths_for_absolute_path_guard(ROOT)
    offenders: dict[str, list[str]] = {}
    for rel in relative_paths:
        path = ROOT / rel
        if not path.exists() or path.suffix.lower() not in text_suffixes:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        allowed_lines = _ALLOWED_HISTORICAL_WORKSPACE_PATH_LINES.get(rel, set())
        hits = sorted(
            {
                marker
                for line_number, line in enumerate(source.splitlines(), start=1)
                if line_number not in allowed_lines
                for marker in forbidden
                if marker in line
            }
        )
        if hits:
            offenders[rel] = hits

    assert not offenders, (
        "Tracked source/config/tests contain developer-machine absolute paths: "
        f"{offenders}. Use repo-relative paths, tmp_path fixtures, or env overrides."
    )


def test_sonar_properties_references_platform_renderer():
    """infra/tooling/sonar-project.properties must not exclude the retired renderer path."""
    source = (ROOT / "infra" / "tooling" / "sonar-project.properties").read_text()
    legacy_renderer = _legacy_hyphenated("renderer.js")
    assert legacy_renderer not in source, (
        "infra/tooling/sonar-project.properties still excludes the retired renderer path. "
        "Update to 'platform-renderer.js'."
    )
    assert "platform-renderer.js" in source, (
        "infra/tooling/sonar-project.properties does not reference 'platform-renderer.js'. "
        "Was the sonar.exclusions line updated?"
    )


# ---------------------------------------------------------------------------
# 9. Prod compose boundary — fs-audit service must not appear in prod compose
# ---------------------------------------------------------------------------


def test_prod_compose_has_no_audit_service():
    """infra/docker/docker-compose.prod.yaml must not define a fs-audit service block."""
    source = (ROOT / "infra" / "docker" / "docker-compose.prod.yaml").read_text()
    assert "fs-audit:" not in source, (
        "infra/docker/docker-compose.prod.yaml still defines a 'fs-audit' service. "
        "Audit tooling must not be part of the production compose file."
    )


def test_prod_compose_has_no_sonar_service():
    """infra/docker/docker-compose.prod.yaml must not define sonarqube or sonar-scanner services."""
    source = (ROOT / "infra" / "docker" / "docker-compose.prod.yaml").read_text()
    assert "sonarqube:" not in source, (
        "infra/docker/docker-compose.prod.yaml still defines a 'sonarqube' service."
    )
    assert "sonar-scanner:" not in source, (
        "infra/docker/docker-compose.prod.yaml still defines a 'sonar-scanner' service."
    )


def test_prod_compose_has_no_development_mail_or_frontend_services():
    """Production must not define Development-only mail, frontend, or test services."""
    import yaml

    payload = yaml.safe_load(
        (ROOT / "infra" / "docker" / "docker-compose.prod.yaml").read_text(encoding="utf-8")
    )
    assert {
        "mailpit",
        "fs-frontend",
        "fs-test-runner",
    }.isdisjoint(payload["services"])


def test_root_flow_steward_launcher_stays_on_new_cli_platform():
    """The first-release root launcher must never regress to old parser entrypoints."""
    files = [
        ROOT / "flow-steward",
        ROOT / "core" / "entrypoints" / "cli" / "scripts" / "flow_steward_launcher.sh",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in files)

    for forbidden in (
        "core.entrypoints.cli.main",
        "manage.py",
        "docker compose down -v",
        "docker volume rm",
        "eval ",
    ):
        assert forbidden not in source

    assert re.search(r"(?m)^\s*(?:source|\.)\s+", source) is None
    assert re.search(r"python\s+-m\s+core\.entrypoints\.cli(?:\s|$)", source) is None


def test_enforcement_matrix_points_extension_scaffold_to_public_flow_steward_cli():
    """The standards matrix must not send maintainers to the removed extension entrypoint."""
    source = (ROOT / "docs" / "standards" / "enforcement-matrix.md").read_text()
    row = next(
        line
        for line in source.splitlines()
        if line.startswith("| Extension scaffold generates YAML-only authoring bundles |")
    )

    assert "flow-steward extensions create <extension_id>" in row
    assert "core/application/extensions/scaffold.py" in row
    assert "tooling extensions create" not in row
    assert "create_extension.py" not in row


def test_fs_cli_compose_service_has_no_durable_service_side_effects():
    """The retained CLI may write lifecycle state but must not start stack services."""
    import yaml

    for compose_path in (
        ROOT / "infra" / "docker" / "docker-compose.yaml",
        ROOT / "infra" / "docker" / "docker-compose.prod.yaml",
    ):
        payload = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        service = payload["services"]["fs-cli"]
        assert "depends_on" not in service
        assert service["restart"] == "always"
        assert "ports" not in service
        assert "expose" not in service
        assert "tty" not in service
        assert "stdin_open" not in service
        assert "privileged" not in service
        assert "cap_add" not in service
        assert "healthcheck" not in service
        assert service["profiles"] == ["cli"]
        assert service["entrypoint"] == [
            "python",
            "-m",
            "core.entrypoints.cli.warm_runtime",
        ]
        assert "core.entrypoints.cli.click_app" not in service["entrypoint"]
        assert service["labels"] == {
            "com.flow-steward.cli-runtime": "true",
            "com.flow-steward.cli-runtime-contract": "1",
            "com.flow-steward.cli-runtime-revision": "${FS_CLI_RUNTIME_REVISION:-unconfigured}",
        }
        expected_volumes = [
            "${FS_DOCKER_HOST_PROJECT_ROOT:-../../}:${FS_DOCKER_HOST_PROJECT_ROOT:-/run/flow-steward-host-project}:rw",
            "/var/run/docker.sock:/var/run/docker.sock",
            "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}/runtime:/run/flow-steward-runtime:rw",
        ]
        if compose_path.name == "docker-compose.yaml":
            expected_volumes = [
                "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}:/run/flow-steward-install-state:rw",
                "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}:${FS_INSTALLER_STATE_DIR:-${FS_DOCKER_HOST_PROJECT_ROOT:-/run/flow-steward-host-project}/.flow-steward}:rw",
                *expected_volumes,
            ]
        if compose_path.name == "docker-compose.prod.yaml":
            expected_volumes = [
                "${FS_CANDIDATE_STATE_DIR:?candidate state directory is required}:/run/flow-steward-candidate-state:rw",
                "${FS_CANDIDATE_PROD_ENV:?candidate production env is required}:/app/.env.prod:rw",
                *expected_volumes,
            ]
        assert service["volumes"] == expected_volumes


def test_prod_env_example_has_no_sonar_blocks():
    """.env.example.prod must not contain SONAR_* or retired-prefix SONAR_* variable blocks."""
    source = (ROOT / ".env.example.prod").read_text()
    hits = re.findall(
        rf"^(SONAR_\w+|{re.escape(_legacy_env('SONAR'))}_\w+)",
        source,
        re.MULTILINE,
    )
    assert not hits, (
        f".env.example.prod still contains Sonar/quality variables: {hits}. "
        "Strip the Sonar block — it belongs in dev/.env.example only."
    )


# ---------------------------------------------------------------------------
# 10. Repository structure migration phase 0 — scaffolding and guardrails
# ---------------------------------------------------------------------------

_PHASE0_TARGET_DIRS = [
    ROOT / "assets",
    ROOT / "backups",
    ROOT / "infra",
    ROOT / "core" / "entrypoints" / "admin",
    ROOT / "core" / "entrypoints" / "api",
    ROOT / "core" / "entrypoints" / "web",
    ROOT / "core" / "entrypoints" / "ws",
    ROOT / "core" / "entrypoints" / "cli",
    ROOT / "core" / "entrypoints" / "mcp",
    ROOT / "core" / "application" / "auth",
    ROOT / "core" / "application" / "jobs",
    ROOT / "core" / "application" / "registry",
    ROOT / "core" / "application" / "workflows",
    ROOT / "core" / "application" / "orchestration",
    ROOT / "core" / "application" / "external_agents",
    ROOT / "core" / "application" / "search",
    ROOT / "core" / "application" / "reports",
    ROOT / "core" / "application" / "extensions",
    ROOT / "core" / "application" / "settings",
    ROOT / "core" / "infrastructure" / "config",
    ROOT / "core" / "infrastructure" / "db",
    ROOT / "core" / "infrastructure" / "observability",
    ROOT / "core" / "infrastructure" / "security",
    ROOT / "core" / "infrastructure" / "extension_runtime",
    ROOT / "core" / "infrastructure" / "runtime",
    ROOT / "db" / "schema",
    ROOT / "db" / "seeds",
]


def test_tool_specific_rule_files_are_removed():
    """AGENTS.md plus docs/standards/* must remain the only manual rule surfaces."""
    retired_rules_file = "CLAU" + "DE.md"
    assert not (ROOT / retired_rules_file).exists(), (
        "Tool-specific rules files must stay deleted. Manual repository rules belong only in AGENTS.md "
        "and docs/standards/*."
    )


def test_gitignore_explicitly_ignores_dot_venv():
    """The repo must ignore local .venv directories during the migration."""
    source = (ROOT / ".gitignore").read_text()
    assert re.search(r"(?m)^\.venv/$", source), (
        ".gitignore must contain an explicit .venv/ entry so local virtualenvs "
        "never pollute the worktree."
    )


def test_phase0_target_directory_skeleton_exists():
    """Phase 0 must establish the canonical target directory skeleton."""
    missing = [str(path.relative_to(ROOT)) for path in _PHASE0_TARGET_DIRS if not path.is_dir()]
    assert not missing, (
        f"Phase 0 target directories are missing: {missing}. "
        "Keep the clean-slate target skeleton checked in during the migration."
    )


def test_retired_ops_root_removed():
    """Generated artifacts and runtime scripts must not use the retired root ops/ bucket."""
    assert not (ROOT / "ops").exists(), (
        "Retired root ops/ directory must not exist; use infra/ for checked-in operations "
        "and logs/* for generated artifacts."
    )


def test_retired_projects_root_removed():
    """Phase 1 removes the root projects/ bucket as a repository concept."""
    assert not (ROOT / "projects").exists(), (
        "Retired root projects/ directory must not exist after Phase 1 root cleanup."
    )


def test_retired_reports_root_removed():
    """Generated artifacts must not use a root-level reports/ bucket."""
    assert not (ROOT / "reports").exists(), (
        "Retired root reports/ directory must not exist; use logs/* subfolders instead."
    )


def test_retired_routes_api_package_removed():
    """Phase 2 removes the retired routes/api Python package after entrypoint migration."""
    retired_api = ROOT / "routes" / "api"
    retired_py_files = sorted(path.name for path in retired_api.glob("*.py"))
    assert not retired_py_files, (
        "Retired routes/api package files must not exist after Phase 2 transport migration."
    )
    assert (ROOT / "core" / "entrypoints" / "api" / "reports.py").exists(), (
        "Canonical API transport modules must live under core/entrypoints/api/."
    )


def test_retired_routes_websockets_package_removed():
    """Phase 2 removes the retired routes/websockets Python package after migration."""
    retired_ws = ROOT / "routes" / "websockets"
    retired_py_files = sorted(path.name for path in retired_ws.glob("*.py"))
    assert not retired_py_files, (
        "Retired routes/websockets package files must not exist after Phase 2 migration."
    )
    assert (ROOT / "core" / "entrypoints" / "ws" / "jobs.py").exists(), (
        "Canonical WebSocket transport modules must live under core/entrypoints/ws/."
    )


def test_retired_routes_root_removed():
    """Phase 2 removes the retired root routes/ package after transport migration."""
    assert not (ROOT / "routes").exists(), (
        "Retired root routes/ directory must not exist after Phase 2 transport migration."
    )


def test_retired_admin_routes_package_removed():
    """Phase 2 removes retired admin/routes Python modules after transport migration."""
    retired_admin_routes = ROOT / "admin" / "routes"
    retired_py_files = sorted(path.name for path in retired_admin_routes.glob("*.py"))
    assert not retired_py_files, (
        "Retired admin/routes package files must not exist after Phase 2 migration."
    )
    assert (ROOT / "core" / "entrypoints" / "admin" / "api.py").exists(), (
        "Canonical admin transport modules must live under core/entrypoints/admin/."
    )


def test_retired_admin_root_removed():
    """Phase 2 removes the retired root admin/ package after transport migration."""
    assert not (ROOT / "admin").exists(), (
        "Retired root admin/ directory must not exist after Phase 2 transport migration."
    )


def test_retired_cli_python_package_removed():
    """Phase 2 removes the retired root cli Python package after transport migration."""
    retired_cli = ROOT / "cli"
    retired_py_files = sorted(path.name for path in retired_cli.glob("*.py"))
    assert not retired_py_files, (
        "Retired cli/ package files must not exist after Phase 2 transport migration."
    )
    assert (ROOT / "core" / "entrypoints" / "cli" / "__main__.py").exists(), (
        "Canonical CLI transport modules must live under core/entrypoints/cli/."
    )
    assert (ROOT / "core" / "entrypoints" / "cli" / "platform" / "click_adapter.py").exists(), (
        "Canonical CLI command tree must be built by the schema-driven platform adapter."
    )


def test_retired_cli_root_removed():
    """Phase 2 removes the retired root cli/ package after transport migration."""
    assert not (ROOT / "cli").exists(), (
        "Retired root cli/ directory must not exist after Phase 2 transport migration."
    )


def test_retired_mcp_server_python_package_removed():
    """Phase 2 removes the retired mcp_server Python package after transport migration."""
    retired_mcp = ROOT / "mcp_server"
    retired_py_files = sorted(path.name for path in retired_mcp.glob("*.py"))
    assert not retired_py_files, (
        "Retired mcp_server/*.py modules must not exist after Phase 2 transport migration."
    )
    assert (ROOT / "core" / "entrypoints" / "mcp" / "server.py").exists(), (
        "Canonical MCP transport modules must live under core/entrypoints/mcp/."
    )


def test_mcp_dockerfile_moved_under_infra():
    """MCP container Dockerfile must live under infra/, not retired mcp_server/."""
    assert not (ROOT / "mcp_server" / "Dockerfile").exists(), (
        "Retired mcp_server/Dockerfile must be removed after infra split."
    )
    assert (ROOT / "infra" / "mcp" / "Dockerfile").exists(), (
        "Canonical MCP container Dockerfile must live under infra/mcp/Dockerfile."
    )


def test_retired_mcp_server_root_removed():
    """The retired root mcp_server/ directory must be fully removed after migration."""
    assert not (ROOT / "mcp_server").exists(), (
        "Retired root mcp_server/ directory must not exist after Phase 2 migration."
    )


def test_serve_transport_imports_use_core_entrypoints():
    """Phase 2 runtime composition should resolve transport modules from core/entrypoints/*."""
    assert not (ROOT / "serve.py").exists(), (
        "The retired root serve.py launcher must stay removed after transport migration."
    )
    app_source = (ROOT / "core" / "entrypoints" / "api" / "app.py").read_text()
    assert "from core.entrypoints.api.app_factory import create_app" in app_source, (
        "The ASGI composition root must delegate to the canonical API app factory."
    )
    api_entrypoint_dir = ROOT / "core" / "entrypoints" / "api"
    composition_sources = [
        api_entrypoint_dir / "app.py",
        api_entrypoint_dir / "app_factory.py",
        api_entrypoint_dir / "runtime_wiring.py",
        api_entrypoint_dir / "route_registration.py",
        api_entrypoint_dir / "route_bindings.py",
    ]
    assert all(path.is_file() for path in composition_sources)
    source = "\n".join(path.read_text() for path in composition_sources)
    required_imports = [
        "from core.entrypoints.api.events_stream import router as _events_stream_router",
        "from core.entrypoints.api.health import router as _health_router",
        "from core.entrypoints.api.security import router as _security_router",
        "from core.entrypoints.api.workload import router as _workload_router",
        "from core.entrypoints.api.job_webhook_bindings import (",
        "from core.entrypoints.api.provider_webhooks import (",
        "from core.entrypoints.api.dashboard_metrics import (",
    ]
    for import_line in required_imports:
        assert import_line in source, (
            "The API composition root must use transport imports from core/entrypoints/* "
            f"after Phase 2 migration (missing: {import_line!r})."
        )

    forbidden_imports = [
        "from routes.events_stream import router as _events_stream_router",
        "from routes.health import router as _health_router",
        "from routes.security import router as _security_router",
        "from routes.workload import router as _workload_router",
        "from routes.job_logs import (",
        "from routes.job_webhook_bindings import (",
        "from routes.provider_webhooks import (",
        "from routes.project_kb_helpers import (",
        "from routes.dashboard_metrics import (",
    ]
    for import_line in forbidden_imports:
        assert import_line not in source, (
            "The API composition root still imports transport modules from retired routes/* "
            f"(found: {import_line!r})."
        )


def test_legacy_routes_python_modules_removed():
    """Phase 2 removes root-level routes/*.py modules after transport migration."""
    legacy_routes_root = ROOT / "routes"
    legacy_py_files = sorted(path.name for path in legacy_routes_root.glob("*.py"))
    assert not legacy_py_files, (
        "Retired routes/*.py modules must not exist after Phase 2 transport migration."
    )


def test_reporting_services_moved_under_core_application_reports():
    """Phase 3 report-domain services live in core/application/reports, not root services/."""
    assert not (ROOT / "services" / "analytics_service.py").exists(), (
        "services/analytics_service.py must be removed after Phase 3 report-domain migration."
    )
    assert not (ROOT / "services" / "reporting_service.py").exists(), (
        "services/reporting_service.py must be removed after Phase 3 report-domain migration."
    )
    assert (ROOT / "core" / "application" / "reports" / "analytics_service.py").exists(), (
        "Canonical analytics service must live under core/application/reports/."
    )
    assert (ROOT / "core" / "application" / "reports" / "reporting_service.py").exists(), (
        "Canonical reporting service must live under core/application/reports/."
    )


def test_search_services_moved_under_core_application_search():
    """Phase 3 search-domain services live in core/application/search, not services/."""
    for legacy_name in (
        "global_search_service.py",
        "global_search_categories.py",
        "search_neo4j.py",
        "search_postgres.py",
    ):
        assert not (ROOT / "services" / legacy_name).exists(), (
            f"services/{legacy_name} must be removed after Phase 3 search-domain migration."
        )

    for canonical_name in (
        "global_search_service.py",
        "global_search_categories.py",
        "search_postgres.py",
    ):
        assert (ROOT / "core" / "application" / "search" / canonical_name).exists(), (
            f"Canonical search module must live under core/application/search/{canonical_name}."
        )


def test_workload_service_moved_under_core_application_jobs():
    """Phase 3 jobs-domain workload service lives in core/application/jobs, not services/."""
    assert not (ROOT / "services" / "workload_service.py").exists(), (
        "services/workload_service.py must be removed after Phase 3 jobs-domain migration."
    )
    assert (ROOT / "core" / "application" / "jobs" / "workload_service.py").exists(), (
        "Canonical workload service must live under core/application/jobs/."
    )


def test_workflow_availability_services_split_by_owner():
    """Workflow availability stays in workflows; install availability is owned by extensions."""
    for legacy_name in ("workflow_availability_service.py", "install_availability_service.py"):
        assert not (ROOT / "services" / legacy_name).exists(), (
            f"services/{legacy_name} must be removed after Phase 3 workflow-domain migration."
        )
    assert (
        ROOT / "core" / "application" / "workflows" / "workflow_availability_service.py"
    ).exists(), "Workflow availability aggregation must live under core/application/workflows/."
    assert (
        ROOT / "core" / "application" / "extensions" / "install_availability_service.py"
    ).exists(), "Install availability ownership must live under core/application/extensions/."


def test_workflow_identity_helpers_moved_under_core_application_workflows():
    """Phase 3 workflow identity helpers live in core/application/workflows, not services/."""
    for legacy_name in ("workflow_catalog_identity.py", "workflow_step_kinds.py"):
        assert not (ROOT / "services" / legacy_name).exists(), (
            f"services/{legacy_name} must be removed after Phase 3 workflow-domain migration."
        )
    for canonical_name in ("workflow_catalog_identity.py", "workflow_step_kinds.py"):
        assert (ROOT / "core" / "application" / "workflows" / canonical_name).exists(), (
            f"Canonical workflow helper must live under core/application/workflows/{canonical_name}."
        )


def test_root_core_modules_are_tiny_facades_when_present():
    """Any remaining root-level core facades must stay tiny re-export files."""
    shim_files = (
        "agent_aliases.py",
        "agent_messages.py",
        "app_factory.py",
        "chat_control_graph.py",
        "chat_control_state.py",
        "config.py",
        "display_text.py",
        "enterprise_layout.py",
        "graph.py",
        "message_bus.py",
        "model_provider.py",
        "prompt_templates.py",
        "safe_eval.py",
        "safe_exec.py",
        "scope.py",
        "test_mode.py",
        "tool_registry.py",
        "version.py",
    )
    for filename in shim_files:
        path = ROOT / "core" / filename
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        assert any(marker in content for marker in ("re-export", "launcher", "Canonical")), (
            f"core/{filename} must stay a thin entrypoint file, not a live implementation bucket."
        )
        assert len(content.splitlines()) <= 80, (
            f"core/{filename} grew beyond shim size; move implementation back to canonical packages."
        )


def test_root_admin_ui_shim_removed():
    """The deleted registry UI must not reintroduce removed alias entrypoints."""
    assert not (ROOT / "admin_ui.py").exists(), (
        "Root admin_ui.py alias entrypoint must stay removed; "
        "registry pages are served by the React shell."
    )
    assert not (ROOT / "core/entrypoints/admin/registry_ui.py").exists()


def test_workflow_runtime_packages_moved_under_core_application_workflows():
    """Retired workflows package modules live in core/application/workflows."""
    for legacy_name in ("executor.py", "gate_handler.py"):
        assert not (ROOT / "workflows" / legacy_name).exists(), (
            f"workflows/{legacy_name} must be removed after workflow-runtime migration."
        )
    # Phase gates were retired with phases: conditions are If/Switch steps and a
    # human decision is an Approval step, so no gate handler exists anywhere.
    assert not (ROOT / "core" / "application" / "workflows" / "gate_handler.py").exists()


def test_legacy_workflows_package_removed():
    """The retired root workflows/ package must be fully removed after migration."""
    assert not (ROOT / "workflows").exists(), (
        "Retired root workflows/ package must not exist after workflow-runtime migration."
    )


def test_workflow_runtime_helpers_moved_under_core_application_workflows():
    """Phase 3 workflow runtime helpers live in core/application/workflows, not services/."""
    for legacy_name in (
        "workflow_start_runtime.py",
        "workflow_override_runtime.py",
        "workflow_reaction_sources.py",
    ):
        assert not (ROOT / "services" / legacy_name).exists(), (
            f"services/{legacy_name} must be removed after Phase 3 workflow-domain migration."
        )
    for canonical_name in (
        "workflow_start_runtime.py",
        "workflow_override_runtime.py",
        "workflow_reaction_sources.py",
    ):
        assert (ROOT / "core" / "application" / "workflows" / canonical_name).exists(), (
            f"Canonical workflow runtime helper must live under core/application/workflows/{canonical_name}."
        )


def test_domain_event_runtime_moved_under_core_application_workflows():
    """Domain-event helpers live in core/application/workflows."""
    assert not (ROOT / "services" / "domain_event_runtime.py").exists(), (
        "services/domain_event_runtime.py must be removed after workflow-domain migration."
    )
    assert (ROOT / "core" / "application" / "workflows" / "domain_event_runtime.py").exists(), (
        "Canonical domain-event helpers must live under core/application/workflows/."
    )


def test_auth_http_runtime_moved_under_core_application_auth():
    """HTTP auth middleware runtime helpers live in core/application/auth."""
    assert not (ROOT / "services" / "auth_http_runtime.py").exists(), (
        "services/auth_http_runtime.py must be removed after auth-domain migration."
    )
    assert (ROOT / "core" / "application" / "auth" / "auth_http_runtime.py").exists(), (
        "Canonical auth HTTP runtime helpers must live under core/application/auth/."
    )


def test_auth_modules_moved_under_core_application_auth():
    """Auth-domain modules must live under core/application/auth."""
    moved_files = (
        "__init__.py",
        "account_memberships.py",
        "audit.py",
        "config.py",
        "csrf.py",
        "email.py",
        "enforce.py",
        "password.py",
        "password_reset_rate_limit.py",
        "principal.py",
        "rate_limit.py",
        "resolve.py",
        "secrets.py",
    )
    for filename in moved_files:
        assert not (ROOT / "auth" / filename).exists(), (
            f"auth/{filename} must be removed after auth-domain migration."
        )
        assert (ROOT / "core" / "application" / "auth" / filename).exists(), (
            f"Canonical auth-domain module must live under core/application/auth/{filename}."
        )


def test_legacy_auth_root_removed():
    """The retired root auth/ package must be fully removed after migration."""
    assert not (ROOT / "auth").exists(), (
        "Retired root auth/ package must not exist after auth-domain migration."
    )


def test_reaction_runtime_helpers_moved_under_core_application_workflows():
    """Workflow reaction-runtime helpers live in core/application/workflows."""
    assert not (ROOT / "services" / "reaction_runtime.py").exists(), (
        "services/reaction_runtime.py must be removed after workflow-domain migration."
    )
    assert (ROOT / "core" / "application" / "workflows" / "reaction_runtime.py").exists(), (
        "Canonical workflow reaction-runtime helpers must live under core/application/workflows/."
    )


def test_reaction_scope_helpers_moved_under_core_application_workflows():
    """Workflow reaction-scope helpers live in core/application/workflows."""
    assert not (ROOT / "services" / "reaction_scopes.py").exists(), (
        "services/reaction_scopes.py must be removed after workflow-domain migration."
    )
    assert (ROOT / "core" / "application" / "workflows" / "reaction_scopes.py").exists(), (
        "Canonical workflow reaction-scope helpers must live under core/application/workflows/."
    )


def test_timezone_service_moved_under_core_application_settings():
    """Phase 3 timezone service lives in core/application/settings, not services/."""
    assert not (ROOT / "services" / "timezone_service.py").exists(), (
        "services/timezone_service.py must be removed after Phase 3 settings migration."
    )
    assert (ROOT / "core" / "application" / "settings" / "timezone_service.py").exists(), (
        "Canonical timezone service must live under core/application/settings/."
    )


def test_model_catalog_moved_under_core_application_settings():
    """Phase 3 model catalog lives in core/application/settings, not services/."""
    assert not (ROOT / "services" / "model_catalog.py").exists(), (
        "services/model_catalog.py must be removed after Phase 3 settings migration."
    )
    assert (ROOT / "core" / "application" / "settings" / "model_catalog.py").exists(), (
        "Canonical model catalog must live under core/application/settings/."
    )


def test_legacy_indexer_root_removed():
    """The retired root indexer/ package must be fully removed after migration."""
    assert not (ROOT / "indexer").exists(), (
        "Retired root indexer/ package must not exist after indexing-domain migration."
    )


def test_platform_contracts_moved_under_core_application_settings():
    """Platform seam contracts live in core/application/settings, not services/."""
    assert not (ROOT / "services" / "platform_contracts.py").exists(), (
        "services/platform_contracts.py must be removed after Phase 3 settings migration."
    )
    assert (ROOT / "core" / "application" / "settings" / "platform_contracts.py").exists(), (
        "Canonical platform contracts must live under core/application/settings/."
    )


def test_extension_services_live_under_core_application_extensions():
    """Phase 3 extension-domain services live in core/application/extensions, not services/."""
    assert not (ROOT / "services" / "extension_services.py").exists(), (
        "services/extension_services.py must be removed after Phase 3 extensions migration."
    )
    extensions_root = ROOT / "core" / "application" / "extensions"
    assert (extensions_root / "command_service.py").exists()
    assert (extensions_root / "query_service.py").exists()
    assert not (extensions_root / "extension_services.py").exists()


def test_ui_shell_preferences_removed_with_shell_mode():
    """Shell-mode preference helpers stay removed from the community runtime."""
    assert not (ROOT / "services" / "ui_shell_preferences.py").exists(), (
        "services/ui_shell_preferences.py must be removed after Phase 3 settings migration."
    )
    assert not (ROOT / "core" / "application" / "settings" / "ui_shell_preferences.py").exists(), (
        "core/application/settings/ui_shell_preferences.py must stay removed with shell mode."
    )


def test_step_replay_runtime_moved_under_core_application_orchestration():
    """Step replay runtime helpers live in core/application/orchestration."""
    assert not (ROOT / "services" / "step_replay_runtime.py").exists(), (
        "services/step_replay_runtime.py must be removed after orchestration migration."
    )
    assert (ROOT / "core" / "application" / "orchestration" / "step_replay_runtime.py").exists(), (
        "Canonical step replay runtime helpers must live under core/application/orchestration/."
    )


def test_interrupted_step_runtime_moved_under_core_application_orchestration():
    """Interrupted-step runtime helpers live in core/application/orchestration."""
    assert not (ROOT / "services" / "interrupted_step_runtime.py").exists(), (
        "services/interrupted_step_runtime.py must be removed after orchestration migration."
    )
    assert (
        ROOT / "core" / "application" / "orchestration" / "interrupted_step_runtime.py"
    ).exists(), (
        "Canonical interrupted-step runtime helpers must live under core/application/orchestration/."
    )


def test_connector_step_runtime_moved_under_core_application_orchestration():
    """Connector-step runtime helpers live in core/application/orchestration."""
    assert not (ROOT / "services" / "connector_step_runtime.py").exists(), (
        "services/connector_step_runtime.py must be removed after orchestration migration."
    )
    assert (
        ROOT / "core" / "application" / "orchestration" / "connector_step_runtime.py"
    ).exists(), (
        "Canonical connector-step runtime helpers must live under core/application/orchestration/."
    )


def test_http_request_step_runtime_moved_under_core_application_orchestration():
    """HTTP-request step runtime helpers live in core/application/orchestration."""
    assert not (ROOT / "services" / "http_request_step_runtime.py").exists(), (
        "services/http_request_step_runtime.py must be removed after orchestration migration."
    )
    assert (
        ROOT / "core" / "application" / "orchestration" / "http_request_step_runtime.py"
    ).exists(), (
        "Canonical HTTP-request step runtime helpers must live under core/application/orchestration/."
    )


def test_scheduled_job_runtime_moved_under_core_application_jobs():
    """Scheduled-job runtime helpers live in core/application/jobs."""
    assert not (ROOT / "services" / "scheduled_job_runtime.py").exists(), (
        "services/scheduled_job_runtime.py must be removed after jobs-domain migration."
    )
    assert (ROOT / "core" / "application" / "jobs" / "scheduled_job_runtime.py").exists(), (
        "Canonical scheduled-job runtime helpers must live under core/application/jobs/."
    )


def test_scheduled_job_concurrency_moved_under_core_application_jobs():
    """Scheduled-job concurrency helpers live in core/application/jobs."""
    assert not (ROOT / "services" / "scheduled_job_concurrency.py").exists(), (
        "services/scheduled_job_concurrency.py must be removed after jobs-domain migration."
    )
    assert (ROOT / "core" / "application" / "jobs" / "scheduled_job_concurrency.py").exists(), (
        "Canonical scheduled-job concurrency helpers must live under core/application/jobs/."
    )


def test_task_enqueue_runtime_moved_under_core_application_jobs():
    """Task enqueue runtime helpers live in core/application/jobs."""
    assert not (ROOT / "services" / "task_enqueue_runtime.py").exists(), (
        "services/task_enqueue_runtime.py must be removed after jobs-domain migration."
    )
    assert (ROOT / "core" / "application" / "jobs" / "task_enqueue_runtime.py").exists(), (
        "Canonical task enqueue runtime helpers must live under core/application/jobs/."
    )


def test_artifact_cleanup_runtime_moved_under_core_application_jobs():
    """Artifact cleanup helpers live in core/application/jobs."""
    assert not (ROOT / "services" / "artifact_cleanup_runtime.py").exists(), (
        "services/artifact_cleanup_runtime.py must be removed after jobs-domain migration."
    )
    assert (ROOT / "core" / "application" / "jobs" / "artifact_cleanup_runtime.py").exists(), (
        "Canonical artifact cleanup helpers must live under core/application/jobs/."
    )


def test_job_runtime_payload_moved_under_core_application_jobs():
    """Job runtime payload builders live in core/application/jobs."""
    assert not (ROOT / "services" / "job_runtime_payload.py").exists(), (
        "services/job_runtime_payload.py must be removed after jobs-domain migration."
    )
    assert (ROOT / "core" / "application" / "jobs" / "job_runtime_payload.py").exists(), (
        "Canonical job runtime payload builders must live under core/application/jobs/."
    )


def test_job_trace_runtime_moved_under_core_application_jobs():
    """Job trace and replay helpers live in core/application/jobs."""
    assert not (ROOT / "services" / "job_trace_runtime.py").exists(), (
        "services/job_trace_runtime.py must be removed after jobs-domain migration."
    )
    assert (ROOT / "core" / "application" / "jobs" / "job_trace_runtime.py").exists(), (
        "Canonical job trace/replay helpers must live under core/application/jobs/."
    )


def test_app_lifecycle_runtime_moved_under_core_infrastructure_runtime():
    """Runtime lifecycle bootstrap helpers live in core/infrastructure/runtime."""
    assert not (ROOT / "services" / "app_lifecycle_runtime.py").exists(), (
        "services/app_lifecycle_runtime.py must be removed after runtime-infrastructure migration."
    )
    assert (ROOT / "core" / "infrastructure" / "runtime" / "app_lifecycle_runtime.py").exists(), (
        "Canonical app lifecycle runtime helpers must live under core/infrastructure/runtime/."
    )


def test_process_runtime_moved_under_core_infrastructure_runtime():
    """Process control helpers live in core/infrastructure/runtime."""
    assert not (ROOT / "services" / "process_runtime.py").exists(), (
        "services/process_runtime.py must be removed after runtime-infrastructure migration."
    )
    assert (ROOT / "core" / "infrastructure" / "runtime" / "process_runtime.py").exists(), (
        "Canonical process runtime helpers must live under core/infrastructure/runtime/."
    )


def test_logger_runtime_moved_under_core_infrastructure_observability():
    """Logger runtime helpers live in core/infrastructure/observability."""
    assert not (ROOT / "services" / "logger_runtime.py").exists(), (
        "services/logger_runtime.py must be removed after observability migration."
    )
    assert (ROOT / "core" / "infrastructure" / "observability" / "logger_runtime.py").exists(), (
        "Canonical logger runtime helpers must live under core/infrastructure/observability/."
    )


def test_runtime_logging_moved_under_core_infrastructure_observability():
    """Runtime logging bootstrap helpers live in core/infrastructure/observability."""
    assert not (ROOT / "services" / "runtime_logging.py").exists(), (
        "services/runtime_logging.py must be removed after observability migration."
    )
    assert (ROOT / "core" / "infrastructure" / "observability" / "runtime_logging.py").exists(), (
        "Canonical runtime logging helpers must live under core/infrastructure/observability/."
    )


def test_observability_metrics_moved_under_core_infrastructure_observability():
    """Prometheus observability helpers live in core/infrastructure/observability."""
    assert not (ROOT / "services" / "observability_metrics.py").exists(), (
        "services/observability_metrics.py must be removed after observability migration."
    )
    assert (
        ROOT / "core" / "infrastructure" / "observability" / "observability_metrics.py"
    ).exists(), (
        "Canonical observability metrics helpers must live under core/infrastructure/observability/."
    )


def test_process_metrics_runtime_moved_under_core_infrastructure_observability():
    """Process-level metrics exporters live in core/infrastructure/observability."""
    assert not (ROOT / "services" / "process_metrics_runtime.py").exists(), (
        "services/process_metrics_runtime.py must be removed after observability migration."
    )
    assert (
        ROOT / "core" / "infrastructure" / "observability" / "process_metrics_runtime.py"
    ).exists(), (
        "Canonical process-metrics runtime helpers must live under core/infrastructure/observability/."
    )


def test_extension_runtime_helpers_moved_under_core_infrastructure_extension_runtime():
    """Extension runtime helpers live in core/infrastructure/extension_runtime."""
    assert not (ROOT / "services" / "extension_runtime_helpers.py").exists(), (
        "services/extension_runtime_helpers.py must be removed after extension-runtime migration."
    )
    assert (
        ROOT / "core" / "infrastructure" / "extension_runtime" / "extension_runtime_helpers.py"
    ).exists(), (
        "Canonical extension runtime helpers must live under core/infrastructure/extension_runtime/."
    )


def test_security_modules_moved_under_core_infrastructure_security():
    """Security modules must live under core/infrastructure/security."""
    moved_files = (
        "__init__.py",
        "config.py",
        "default_policy.yaml",
        "engine.py",
        "event_logger.py",
        "integration.py",
        "models.py",
        "policy.py",
        "policy_loader.py",
        "posture.py",
        "prompt_safety.py",
        "zip_guard.py",
    )
    for filename in moved_files:
        assert not (ROOT / "security" / filename).exists(), (
            f"security/{filename} must be removed after infrastructure-security migration."
        )
        assert (ROOT / "core" / "infrastructure" / "security" / filename).exists(), (
            f"Canonical security module must live under core/infrastructure/security/{filename}."
        )


def test_security_scanner_modules_moved_under_core_infrastructure_security():
    """Security scanner modules must live under core/infrastructure/security/scanners."""
    moved_files = ("__init__.py", "base.py", "injection.py", "pii.py", "secrets.py")
    for filename in moved_files:
        assert not (ROOT / "security" / "scanners" / filename).exists(), (
            f"security/scanners/{filename} must be removed after infrastructure-security migration."
        )
        assert (ROOT / "core" / "infrastructure" / "security" / "scanners" / filename).exists(), (
            "Canonical security scanner must live under "
            f"core/infrastructure/security/scanners/{filename}."
        )


def test_integrations_modules_moved_under_core_infrastructure_db():
    """Retired integrations modules must live under core/infrastructure/db."""
    moved_files = (
        "__init__.py",
        "db_pool.py",
        "platform_logger.py",
        "remote_extension_runtime.py",
        "event_store.py",
        "project_settings_store.py",
        "job_queries_store.py",
        "report_run_store.py",
    )
    for filename in moved_files:
        assert not (ROOT / "integrations" / filename).exists(), (
            f"integrations/{filename} must be removed after infrastructure migration."
        )
        assert (ROOT / "core" / "infrastructure" / "db" / filename).exists(), (
            f"Canonical integration module must live under core/infrastructure/db/{filename}."
        )

    assert not (ROOT / "integrations" / "provider_runtime.py").exists()
    assert not (ROOT / "core" / "infrastructure" / "db" / "provider_runtime.py").exists()
    for module in (
        "provider_runtime",
        "provider_attachments",
        "provider_webhook_security",
        "provider_completion",
    ):
        assert (ROOT / "core" / "application" / "extensions" / (module + ".py")).exists()

    assert not (ROOT / "integrations" / "notification_runtime.py").exists()
    assert not (ROOT / "core" / "infrastructure" / "db" / "notification_runtime.py").exists()
    for module in ("contracts.py", "delivery.py", "dispatch.py"):
        assert (ROOT / "core" / "application" / "notifications" / module).exists()

    assert not (ROOT / "integrations" / "system_email.py").exists(), (
        "integrations/system_email.py must be removed after infrastructure migration."
    )

    assert not (ROOT / "integrations" / "email" / "service.py").exists(), (
        "integrations/email/service.py must be removed after infrastructure migration."
    )
    assert (ROOT / "core" / "infrastructure" / "observability" / "sentry.py").exists(), (
        "Canonical sentry adapter must live under core/infrastructure/observability/sentry.py."
    )


def test_core_db_module_filenames_do_not_use_old_brand_token():
    """Canonical DB module filenames must not reintroduce retired old-brand naming."""
    db_root = ROOT / "core" / "infrastructure" / "db"
    offenders = sorted(path.name for path in db_root.glob(f"*{_RETIRED_BRAND_LOWER}*.py"))
    assert not offenders, (
        "core/infrastructure/db contains forbidden old-brand-named modules: "
        f"{offenders}. Rename to neutral platform identifiers."
    )


def test_legacy_integrations_root_removed():
    """The retired root integrations/ package must be fully removed after migration."""
    assert not (ROOT / "integrations").exists(), (
        "Retired root integrations/ package must not exist after infrastructure migration."
    )


def test_mcp_only_client_stubs_do_not_live_in_oss_core():
    """Provider-specific Context7/Figma stubs must not be first-party core adapters."""
    assert not (ROOT / "core" / "infrastructure" / "db" / "context7_client.py").exists(), (
        "core/infrastructure/db/context7_client.py must not exist. "
        "MCP-only adapters belong in a dedicated non-DB seam."
    )
    assert not (ROOT / "core" / "infrastructure" / "db" / "figma_client.py").exists(), (
        "core/infrastructure/db/figma_client.py must not exist. "
        "MCP-only adapters belong in a dedicated non-DB seam."
    )
    assert not (ROOT / "core" / "infrastructure" / "mcp_clients" / "context7_client.py").exists(), (
        "core must not ship provider-specific Context7 client stubs; use generic MCP connectors."
    )
    assert not (ROOT / "core" / "infrastructure" / "mcp_clients" / "figma_client.py").exists(), (
        "core must not ship provider-specific Figma client stubs; use generic MCP connectors."
    )
    assert not (ROOT / "core" / "infrastructure" / "mcp_clients").exists(), (
        "core/infrastructure/mcp_clients must not exist as a placeholder seam. "
        "OSS core should expose only generic MCP connector runtime surfaces."
    )


def test_branded_mcp_provider_modules_do_not_exist_anywhere_under_core():
    """Provider-branded Context7/Figma module files must never appear under core/."""
    branded_tokens = ("context7", "figma")
    offenders = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "core").rglob("*.py")
        if any(token in path.name.lower() for token in branded_tokens)
    )
    assert not offenders, (
        "core/ contains provider-branded Context7/Figma modules: "
        f"{offenders}. OSS core must keep MCP integrations provider-agnostic."
    )


def test_integration_tools_stays_generic_without_provider_specific_branches():
    """integration_tools.py must stay provider-agnostic for product-facing MCP calls."""
    module_path = ROOT / "core" / "application" / "orchestration" / "tools" / "integration_tools.py"
    source = module_path.read_text(encoding="utf-8")

    assert "def mcp_connector_invoke(" in source, (
        "integration_tools.py must keep the generic mcp_connector_invoke seam."
    )

    provider_mentions = sorted(
        {
            match.group(0).lower()
            for match in re.finditer(r"\b(context7|figma)\b", source, re.IGNORECASE)
        }
    )
    assert not provider_mentions, (
        "integration_tools.py reintroduced provider-specific MCP branding: "
        f"{provider_mentions}. Keep this module generic."
    )

    provider_env_reads = re.findall(
        r"os\.getenv\(\s*[\"'](?:CONTEXT7|FIGMA)_[^\"']*[\"']\s*[,)]",
        source,
        flags=re.IGNORECASE,
    )
    assert not provider_env_reads, (
        "integration_tools.py must not parse provider-specific env keys. "
        "Use canonical generic connector runtime configuration instead."
    )


def test_gitlab_sample_tasks_kb_backfill_experiment_is_not_public_release_surface():
    """Personal GitLab/Sample Tasks KB backfill must not ship in the public CLI surface."""
    assert not (ROOT / "core" / "infrastructure" / "db" / "gitlab_client.py").exists(), (
        "core/infrastructure/db/gitlab_client.py must not exist. "
        "The personal GitLab/Sample Tasks KB backfill experiment must not move into runtime infra."
    )
    assert not (ROOT / "core" / "application" / "tooling" / "lib" / "gitlab_client.py").exists(), (
        "GitLab backfill helper must not ship in the public release tree."
    )
    assert not (ROOT / "core" / "application" / "tooling" / "ops" / "backfill_kb.py").exists(), (
        "Sample Tasks/GitLab KB backfill command must not ship in the public release tree."
    )


def test_http_transport_entrypoint_surfaces_match_canonical_taxonomy():
    """Canonical transport taxonomy should expose API/admin seams and React shell web UI."""
    assert (ROOT / "core" / "entrypoints" / "control" / "api.py").exists(), (
        "Control-plane aggregator must live under core/entrypoints/control/api.py."
    )
    assert (ROOT / "core" / "entrypoints" / "api" / "auth_route_bindings.py").exists(), (
        "Auth API bindings must live under core/entrypoints/api/."
    )
    assert (ROOT / "core" / "entrypoints" / "admin" / "ui_route_bindings.py").exists(), (
        "Authenticated UI bindings must live under core/entrypoints/admin/."
    )
    assert (ROOT / "core" / "entrypoints" / "web" / "frontend_shell.py").exists(), (
        "Public web UI must be served by the React frontend shell."
    )
    assert not (ROOT / "core" / "entrypoints" / "web" / "ui_route_bindings.py").exists(), (
        "Retired public web UI bindings must not be restored after the React cutover."
    )
    assert not (ROOT / "core" / "entrypoints" / "operator").exists(), (
        "Operator entrypoint facade must be removed from the canonical transport taxonomy."
    )


def test_legacy_security_root_removed():
    """The retired root security/ package must be fully removed after migration."""
    assert not (ROOT / "security").exists(), (
        "Retired root security/ package must not exist after infrastructure-security migration."
    )


def test_serve_interrupt_helpers_moved_under_core_infrastructure_runtime():
    """Serve interrupt helpers live in core/infrastructure/runtime."""
    assert not (ROOT / "services" / "serve_interrupt_helpers.py").exists(), (
        "services/serve_interrupt_helpers.py must be removed after runtime-infrastructure migration."
    )
    assert (ROOT / "core" / "infrastructure" / "runtime" / "serve_interrupt_helpers.py").exists(), (
        "Canonical serve interrupt helpers must live under core/infrastructure/runtime/."
    )


def test_serve_request_runtime_moved_under_core_infrastructure_runtime():
    """Serve request helpers live in core/infrastructure/runtime."""
    assert not (ROOT / "services" / "serve_request_runtime.py").exists(), (
        "services/serve_request_runtime.py must be removed after runtime-infrastructure migration."
    )
    assert (ROOT / "core" / "infrastructure" / "runtime" / "serve_request_runtime.py").exists(), (
        "Canonical serve request helpers must live under core/infrastructure/runtime/."
    )


def test_snapshot_serializers_have_application_management_owner():
    """Management read-model policy belongs to the application layer."""
    assert not (ROOT / "services" / "serve_snapshot_serializers.py").exists()
    assert not (
        ROOT / "core" / "infrastructure" / "runtime" / "serve_snapshot_serializers.py"
    ).exists()
    assert (ROOT / "core" / "application" / "management" / "snapshot_serializers.py").exists()


def test_serve_workflow_helpers_moved_under_core_application_workflows():
    """Serve workflow helpers live in core/application/workflows."""
    assert not (ROOT / "services" / "serve_workflow_helpers.py").exists(), (
        "services/serve_workflow_helpers.py must be removed after workflows-domain migration."
    )
    assert (ROOT / "core" / "application" / "workflows" / "serve_workflow_helpers.py").exists(), (
        "Canonical serve workflow helpers must live under core/application/workflows/."
    )


def test_serve_service_wiring_moved_under_core_infrastructure_runtime():
    """Serve transport wiring helpers live in core/infrastructure/runtime."""
    assert not (ROOT / "services" / "serve_service_wiring.py").exists(), (
        "services/serve_service_wiring.py must be removed after runtime-infrastructure migration."
    )
    assert (ROOT / "core" / "infrastructure" / "runtime" / "serve_service_wiring.py").exists(), (
        "Canonical serve service-wiring helpers must live under core/infrastructure/runtime/."
    )


def test_control_runtime_modules_moved_under_core_infrastructure_runtime_control():
    """Control-plane runtime modules must live under core/infrastructure/runtime/control."""
    moved_files = (
        "__init__.py",
        "_cron.py",
        "config.py",
        "loops.py",
        "scans.py",
        "scans_rollups.py",
        "scans_scheduled_jobs.py",
        "state_reader.py",
    )
    for filename in moved_files:
        assert not (ROOT / "control" / filename).exists(), (
            f"control/{filename} must be removed after control-runtime migration."
        )
        assert (ROOT / "core" / "infrastructure" / "runtime" / "control" / filename).exists(), (
            "Canonical control runtime module must live under "
            f"core/infrastructure/runtime/control/{filename}."
        )
    assert not (ROOT / "core" / "infrastructure" / "runtime" / "control" / "main.py").exists()
    assert (ROOT / "core" / "entrypoints" / "cli" / "control_process.py").exists()


def test_legacy_control_root_removed():
    """The retired root control/ package must be fully removed after migration."""
    assert not (ROOT / "control").exists(), (
        "Retired root control/ package must not exist after control-runtime migration."
    )


def test_worker_runtime_modules_moved_under_core_infrastructure_runtime_worker():
    """Worker runtime modules must live under core/infrastructure/runtime/worker."""
    moved_files = ("__init__.py", "manager.py", "queue.py", "worker.py")
    for filename in moved_files:
        assert not (ROOT / "worker" / filename).exists(), (
            f"worker/{filename} must be removed after worker-runtime migration."
        )
        assert (ROOT / "core" / "infrastructure" / "runtime" / "worker" / filename).exists(), (
            "Canonical worker runtime module must live under "
            f"core/infrastructure/runtime/worker/{filename}."
        )


def test_legacy_worker_root_removed():
    """The retired root worker/ package must be fully removed after migration."""
    assert not (ROOT / "worker").exists(), (
        "Retired root worker/ package must not exist after worker-runtime migration."
    )


def test_serve_auth_runtime_moved_under_core_application_auth():
    """Serve auth runtime helpers live in core/application/auth."""
    assert not (ROOT / "services" / "serve_auth_runtime.py").exists(), (
        "services/serve_auth_runtime.py must be removed after auth-domain migration."
    )
    assert (ROOT / "core" / "application" / "auth" / "serve_auth_runtime.py").exists(), (
        "Canonical serve auth runtime helpers must live under core/application/auth/."
    )


def test_ui_i18n_moved_under_core_application_settings():
    """UI i18n helpers live in core/application/settings."""
    assert not (ROOT / "services" / "ui_i18n.py").exists(), (
        "services/ui_i18n.py must be removed after settings-domain migration."
    )
    assert (ROOT / "core" / "application" / "settings" / "ui_i18n.py").exists(), (
        "Canonical UI i18n helpers must live under core/application/settings/."
    )


def test_ui_projection_modules_moved_under_core_application_reports():
    """UI projection builders live in core/application/reports."""
    for legacy_name in (
        "ui_projection.py",
        "ui_projection_helpers.py",
        "ui_projection_extras.py",
    ):
        assert not (ROOT / "services" / legacy_name).exists(), (
            f"services/{legacy_name} must be removed after reports-domain migration."
        )
    for canonical_name in (
        "ui_projection.py",
        "ui_projection_helpers.py",
        "ui_projection_extras.py",
    ):
        assert (ROOT / "core" / "application" / "reports" / canonical_name).exists(), (
            f"Canonical UI projection helper must live under core/application/reports/{canonical_name}."
        )


def test_ui_page_registry_moved_under_core_application_reports():
    """UI page registry helpers live in core/application/reports."""
    assert not (ROOT / "services" / "ui_page_registry.py").exists(), (
        "services/ui_page_registry.py must be removed after reports-domain migration."
    )
    assert (ROOT / "core" / "application" / "reports" / "ui_page_registry.py").exists(), (
        "Canonical UI page registry must live under core/application/reports/."
    )


def test_management_service_layer_modules_moved_under_core_application_management():
    """Retired services-layer modules live in core/application/management."""
    moved_files = (
        "command_models.py",
        "query_models.py",
        "errors.py",
        "guards.py",
        "command_services.py",
        "query_services.py",
        "command_service_runtime.py",
        "query_service_runtime.py",
        "management_runtime.py",
        "mcp_connector_services.py",
    )
    for filename in moved_files:
        assert not (ROOT / "services" / filename).exists(), (
            f"services/{filename} must be removed after management-layer migration."
        )
        assert (ROOT / "core" / "application" / "management" / filename).exists(), (
            "Canonical management-layer module must live under "
            f"core/application/management/{filename}."
        )

    user_admin_package = ROOT / "core" / "application" / "auth" / "user_admin"
    for filename in (
        "__init__.py",
        "command_service.py",
        "contracts.py",
        "notifications.py",
        "policies.py",
        "projection.py",
        "query_service.py",
        "repository.py",
    ):
        assert (user_admin_package / filename).exists(), (
            "Canonical user-administration module must live under "
            f"core/application/auth/user_admin/{filename}."
        )
    for retired_name in (
        "user_admin_contracts.py",
        "user_admin_repository.py",
        "user_admin_services.py",
    ):
        assert not (ROOT / "core" / "application" / "management" / retired_name).exists(), (
            "Retired user-admin compatibility module must not remain under "
            f"core/application/management/{retired_name}."
        )


def test_legacy_services_package_removed():
    """The retired root services/ package must be fully removed after migration."""
    assert not (ROOT / "services").exists(), (
        "Retired root services/ package must not exist after management-layer migration."
    )


def test_registry_manifest_models_moved_under_core_application_registry():
    """Registry manifest models live in core/application/registry."""
    moved_files = (
        "agent_manifest.py",
        "skill_manifest.py",
        "tool_manifest.py",
        "workflow_manifest.py",
        "data_contract_manifest.py",
    )
    for filename in moved_files:
        assert not (ROOT / "models" / filename).exists(), (
            f"models/{filename} must be removed after registry-model migration."
        )
        assert (ROOT / "core" / "application" / "registry" / filename).exists(), (
            "Canonical registry manifest model must live under "
            f"core/application/registry/{filename}."
        )


def test_registry_runtime_modules_moved_under_core_application_registry():
    """Registry runtime modules must live under core/application/registry."""
    moved_files = (
        "agent_registry.py",
        "builtin_mcp_tools.py",
        "contract_validation.py",
        "db_store.py",
        "io_resolver.py",
        "prompt_contracts.py",
        "public_contract_projection.py",
        "public_contract_projection_helpers.py",
        "registry_manager.py",
        "skill_registry.py",
        "tool_registry.py",
        "workflow_registry.py",
    )
    for filename in moved_files:
        assert (ROOT / "core" / "application" / "registry" / filename).exists(), (
            "Canonical registry runtime module must live under "
            f"core/application/registry/{filename}."
        )


def test_legacy_registry_root_removed():
    """The retired root registry/ package must be fully removed after migration."""
    assert not (ROOT / "registry").exists(), (
        "Retired root registry/ package must not exist after registry-domain migration."
    )


def test_extension_and_management_models_moved_under_owning_application_domains():
    """Extension/management data models live in owning core/application packages."""
    canonical_homes = {
        "extension_runtime.py": ROOT
        / "core"
        / "application"
        / "extensions"
        / "extension_runtime.py",
        "extension_install.py": ROOT
        / "core"
        / "application"
        / "extensions"
        / "extension_install.py",
        "project_connection.py": ROOT
        / "core"
        / "application"
        / "management"
        / "project_connection.py",
        "mcp_connector.py": ROOT / "core" / "application" / "management" / "mcp_connector.py",
    }
    for filename, canonical in canonical_homes.items():
        assert not (ROOT / "models" / filename).exists(), (
            f"models/{filename} must be removed after ownership-based model migration."
        )
        assert canonical.exists(), f"Canonical model must live under {canonical.relative_to(ROOT)}."


def test_legacy_models_package_removed():
    """The retired root models/ package must be fully removed after migration."""
    assert not (ROOT / "models").exists(), (
        "Retired root models/ package must not exist after application-package migration."
    )


def test_request_schemas_moved_under_core_application_management():
    """Request DTO schemas live in core/application/management."""
    assert not (ROOT / "schemas" / "requests.py").exists(), (
        "schemas/requests.py must be removed after request-schema migration."
    )
    assert (ROOT / "core" / "application" / "management" / "requests.py").exists(), (
        "Canonical request schema module must live under core/application/management/."
    )


def test_legacy_schemas_package_removed():
    """The retired root schemas/ package must be fully removed after migration."""
    assert not (ROOT / "schemas").exists(), (
        "Retired root schemas/ package must not exist after request-schema migration."
    )


def test_external_agent_reference_worker_moved_under_core_application_external_agents():
    """External-agent reference worker lives in core/application/external_agents."""
    assert not (ROOT / "external_agents" / "reference_worker.py").exists(), (
        "external_agents/reference_worker.py must be removed after external-agents migration."
    )
    assert (ROOT / "core" / "application" / "external_agents" / "reference_worker.py").exists(), (
        "Canonical external-agent reference worker must live under "
        "core/application/external_agents/."
    )


def test_legacy_external_agents_package_removed():
    """The retired root external_agents/ package must be fully removed after migration."""
    assert not (ROOT / "external_agents").exists(), (
        "Retired root external_agents/ package must not exist after migration."
    )


def test_trigger_modules_moved_to_canonical_homes():
    """Trigger modules must live under orchestration/runtime canonical packages."""
    assert not (ROOT / "triggers" / "task_envelope.py").exists(), (
        "triggers/task_envelope.py must be removed after trigger-domain migration."
    )
    assert not (ROOT / "triggers" / "schedule_trigger.py").exists(), (
        "triggers/schedule_trigger.py must be removed after trigger-runtime migration."
    )
    assert (ROOT / "core" / "application" / "orchestration" / "task_envelope.py").exists(), (
        "Canonical TaskEnvelope contract must live under core/application/orchestration/."
    )
    assert (
        ROOT / "core" / "infrastructure" / "runtime" / "triggers" / "schedule_trigger.py"
    ).exists(), "Canonical schedule trigger must live under core/infrastructure/runtime/triggers/."


def test_legacy_triggers_package_removed():
    """The retired root triggers/ package must be fully removed after migration."""
    assert not (ROOT / "triggers").exists(), (
        "Retired root triggers/ package must not exist after trigger-domain migration."
    )


def test_flow_player_runtime_moved_under_extension_runtime():
    """Playwright runtime belongs to extension-runtime seam, not workflow domain."""
    assert not (ROOT / "flows" / "player.py").exists(), (
        "flows/player.py must be removed after flow-runtime migration."
    )
    assert not (ROOT / "flows" / "registry" / "example_login_flow.yaml").exists(), (
        "flows/registry/example_login_flow.yaml must be removed after flow-runtime migration."
    )
    assert (
        ROOT / "core" / "infrastructure" / "extension_runtime" / "playwright_flow_player.py"
    ).exists(), (
        "Canonical Playwright flow runtime must live under "
        "core/infrastructure/extension_runtime/playwright_flow_player.py."
    )
    assert (
        ROOT / "core" / "application" / "workflows" / "registry" / "example_login_flow.yaml"
    ).exists(), (
        "Canonical example flow asset must live under "
        "core/application/workflows/registry/example_login_flow.yaml."
    )


def test_legacy_flows_package_removed():
    """The retired root flows/ package must be fully removed after migration."""
    assert not (ROOT / "flows").exists(), (
        "Retired root flows/ package must not exist after flow-runtime migration."
    )


def test_guardrail_modules_moved_under_core_application_orchestration_guardrails():
    """Guardrail runtime modules must live under core/application/orchestration/guardrails."""
    moved_files = ("checker.py", "deterministic.py", "semantic.py")
    for filename in moved_files:
        assert not (ROOT / "guardrails" / filename).exists(), (
            f"guardrails/{filename} must be removed after orchestration guardrail migration."
        )
        assert (
            ROOT / "core" / "application" / "orchestration" / "guardrails" / filename
        ).exists(), (
            "Canonical orchestration guardrail module must live under "
            f"core/application/orchestration/guardrails/{filename}."
        )


def test_legacy_guardrails_root_removed():
    """The retired root guardrails/ package must be fully removed after migration."""
    assert not (ROOT / "guardrails").exists(), (
        "Retired root guardrails/ package must not exist after orchestration migration."
    )


def test_graph_modules_moved_under_core_application_orchestration_graph():
    """Graph builders/nodes must live under core/application/orchestration/graph."""
    moved_files = ("builder.py", "nodes.py", "nodes_step_context.py")
    for filename in moved_files:
        assert not (ROOT / "graph" / filename).exists(), (
            f"graph/{filename} must be removed after orchestration graph migration."
        )
        assert (ROOT / "core" / "application" / "orchestration" / "graph" / filename).exists(), (
            "Canonical orchestration graph module must live under "
            f"core/application/orchestration/graph/{filename}."
        )


def test_legacy_graph_root_removed():
    """The retired root graph/ package must be fully removed after migration."""
    assert not (ROOT / "graph").exists(), (
        "Retired root graph/ package must not exist after orchestration graph migration."
    )


def test_agent_modules_moved_under_core_application_orchestration_agents():
    """Agent runtime modules must live under core/application/orchestration/agents."""
    moved_files = (
        "__init__.py",
        "base.py",
        "behavior_prompt_block.py",
        "common.py",
        "common_runtime_helpers.py",
        "kb_intake_node.py",
        "prompt_agent.py",
        "prompt_builder.py",
    )
    for filename in moved_files:
        assert not (ROOT / "agents" / filename).exists(), (
            f"agents/{filename} must be removed after orchestration agents migration."
        )
        assert (ROOT / "core" / "application" / "orchestration" / "agents" / filename).exists(), (
            "Canonical orchestration agent module must live under "
            f"core/application/orchestration/agents/{filename}."
        )


def test_legacy_agents_root_removed():
    """The retired root agents/ package must be fully removed after migration."""
    assert not (ROOT / "agents").exists(), (
        "Retired root agents/ package must not exist after orchestration migration."
    )


def test_tools_modules_moved_under_core_application_orchestration_tools():
    """Tool runtime modules must live under core/application/orchestration/tools."""
    moved_files = (
        "__init__.py",
        "base.py",
        "integration_tools.py",
        "mcp_rate_limit.py",
        "mcp_tools.py",
        "tool_resolver.py",
    )
    for filename in moved_files:
        assert not (ROOT / "tools" / filename).exists(), (
            f"tools/{filename} must be removed after orchestration tool migration."
        )
        assert (ROOT / "core" / "application" / "orchestration" / "tools" / filename).exists(), (
            "Canonical orchestration tool module must live under "
            f"core/application/orchestration/tools/{filename}."
        )


def test_legacy_tools_root_removed():
    """The retired root tools/ package must be fully removed after migration."""
    assert not (ROOT / "tools").exists(), (
        "Retired root tools/ package must not exist after orchestration migration."
    )


def test_orchestrator_modules_moved_under_core_application_orchestration():
    """Orchestrator runtime modules must live under core/application/orchestration."""
    moved_files = (
        "classifier.py",
        "control_flow_step_node.py",
        "helpers.py",
        "step_hub.py",
        "invoke_workflow_step_node.py",
        "journal.py",
        "parallel_executor_node.py",
        "routing_node.py",
        "state_summarizer.py",
        "supervisor.py",
        "validation_node.py",
    )
    for filename in moved_files:
        assert not (ROOT / "orchestrator" / filename).exists(), (
            f"orchestrator/{filename} must be removed after orchestration-runtime migration."
        )
        assert (ROOT / "core" / "application" / "orchestration" / filename).exists(), (
            "Canonical orchestration runtime module must live under "
            f"core/application/orchestration/{filename}."
        )


def test_legacy_orchestrator_root_removed():
    """The retired root orchestrator/ package must be fully removed after migration."""
    assert not (ROOT / "orchestrator").exists(), (
        "Retired root orchestrator/ package must not exist after orchestration-runtime migration."
    )


def test_extension_modules_live_under_core_application_extensions():
    """Extension-domain modules must live under core/application/extensions."""
    moved_files = (
        "artifact_dataset_plane_v2.py",
        "entitlement_lifecycle_v2.py",
        "extension_descriptor.py",
        "extension_contract_v2.py",
        "extension_install.py",
        "extension_projection_v2.py",
        "install.py",
        "lifecycle.py",
        "order_import_batch.py",
        "order_import_v2.py",
        "remote_runtime_protocol_v2.py",
        "runtime_store.py",
    )
    for filename in moved_files:
        assert not (ROOT / "extension" / filename).exists(), (
            f"extension/{filename} must be removed after extension-domain migration."
        )
        assert (ROOT / "core" / "application" / "extensions" / filename).exists(), (
            f"Canonical extension-domain module must live under core/application/extensions/{filename}."
        )


def test_legacy_extension_root_removed():
    """The retired root extension/ package must be fully removed after migration."""
    assert not (ROOT / "extension").exists(), (
        "Retired root extension/ package must not exist after extension-domain migration."
    )


def test_prompt_assets_moved_under_core_application_orchestration_prompts():
    """Prompt assets must live under core/application/orchestration/prompts."""
    assert not (ROOT / "prompts" / "orchestrator_system.md").exists(), (
        "prompts/orchestrator_system.md must be removed after orchestration-asset migration."
    )
    assert (
        ROOT / "core" / "application" / "orchestration" / "prompts" / "orchestrator_system.md"
    ).exists(), (
        "Canonical orchestrator system prompt must live under core/application/orchestration/prompts/."
    )


def test_legacy_prompts_root_removed():
    """The retired root prompts/ directory must be removed after migration."""
    assert not (ROOT / "prompts").exists(), (
        "Retired root prompts/ directory must not exist after orchestration-asset migration."
    )


def test_legacy_hidden_root_removed():
    """The retired hidden-root directory must be removed after migration."""
    assert not (ROOT / ("_" + _RETIRED_BRAND_LOWER)).exists(), (
        "Retired hidden-root directory must not exist after method-asset migration."
    )


def test_readiness_runtime_moved_under_core_infrastructure_runtime():
    """Readiness checks live in core/infrastructure/runtime."""
    assert not (ROOT / "services" / "readiness_runtime.py").exists(), (
        "services/readiness_runtime.py must be removed after runtime-infrastructure migration."
    )
    assert (ROOT / "core" / "infrastructure" / "runtime" / "readiness_runtime.py").exists(), (
        "Canonical readiness runtime helpers must live under core/infrastructure/runtime/."
    )


def test_watchdog_runtime_moved_under_core_infrastructure_runtime():
    """Watchdog runtime helpers live in core/infrastructure/runtime."""
    assert not (ROOT / "services" / "watchdog_runtime.py").exists(), (
        "services/watchdog_runtime.py must be removed after runtime-infrastructure migration."
    )
    assert (ROOT / "core" / "infrastructure" / "runtime" / "watchdog_runtime.py").exists(), (
        "Canonical watchdog runtime helpers must live under core/infrastructure/runtime/."
    )


def test_graph_resume_runtime_moved_under_core_infrastructure_runtime():
    """Graph-resume runtime helpers live in core/infrastructure/runtime."""
    assert not (ROOT / "services" / "graph_resume_runtime.py").exists(), (
        "services/graph_resume_runtime.py must be removed after runtime-infrastructure migration."
    )
    assert (ROOT / "core" / "infrastructure" / "runtime" / "graph_resume_runtime.py").exists(), (
        "Canonical graph-resume runtime helpers must live under core/infrastructure/runtime/."
    )


def test_state_reader_runtime_moved_under_core_infrastructure_runtime():
    """State-reader runtime helpers live in core/infrastructure/runtime."""
    assert not (ROOT / "services" / "state_reader_runtime.py").exists(), (
        "services/state_reader_runtime.py must be removed after runtime-infrastructure migration."
    )
    assert (ROOT / "core" / "infrastructure" / "runtime" / "state_reader_runtime.py").exists(), (
        "Canonical state-reader runtime helpers must live under core/infrastructure/runtime/."
    )


def test_registry_seed_does_not_ship_test_only_guardrails():
    """Canonical registry seed must not carry disposable QA/test guardrails into fresh DBs."""
    payload = json.loads((ROOT / "db" / "seeds" / "registry_seed.json").read_text())
    offenders = [
        str((item or {}).get("display_name") or "")
        for item in (payload.get("guardrails") or [])
        if "safe to delete" in str((item or {}).get("description") or "").lower()
    ]
    assert not offenders, (
        "registry_seed.json must not ship disposable guardrails into release runtime state: "
        f"{offenders}"
    )


def test_registry_seed_does_not_ship_flow_stewards_own_repository_policy():
    """AC17-R73: four shipped guardrails were this repository's internal policy.

    A fresh installation arrived with eight globally enabled guardrails, seven of them at
    severity `error`. Four described Flow Steward's own development state and nothing about
    the installation running them:

      No Migration Chain           ADR 0012 and this repository's db/migrations layout
      No Tool-Specific Rule Files  the AGENTS.md rule, matching CLAUDE/CURSOR/COPILOT md
      No New Noncanonical Root Files  this repository's top-level buckets, mid-migration
      No Psycopg2 Reintroduction   "while the psycopg3 migration is in progress"

    Someone installing the product to sync a product catalogue received them enabled and
    enforcing, and they disclose the state of our own development. They are removed from the
    shipped seed. The four that remain are about the work a user is doing, not about us.

    This asserts the property rather than the four names, so a new one cannot arrive by a
    different title.
    """
    payload = json.loads((ROOT / "db" / "seeds" / "registry_seed.json").read_text())
    markers = (
        "psycopg2",
        "psycopg3",
        "agents.md",
        "docs/standards",
        "adr 0012",
        "db/migrations",
        "claude",
        "cursor",
        "copilot",
        "windsurf",
        "repository-structure migration",
    )
    offenders: list[str] = []
    for item in payload.get("guardrails") or []:
        blob = json.dumps(item, ensure_ascii=False).lower()
        hit = [marker for marker in markers if marker in blob]
        if hit:
            offenders.append(f"{item.get('display_name')!r} mentions {hit}")

    assert not offenders, (
        "registry_seed.json ships guardrails describing this repository's own development "
        f"policy, which every installation would receive and enforce: {offenders}"
    )


def test_registry_seed_does_not_ship_integer_entity_ids():
    """Canonical registry seed must not carry pre-UUID serial IDs for DB entities."""
    payload = json.loads((ROOT / "db" / "seeds" / "registry_seed.json").read_text())
    offenders: list[str] = []

    def walk(value, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}" if path else str(key))
            return
        if isinstance(value, list):
            for idx, item in enumerate(value):
                walk(item, f"{path}[{idx}]")
            return
        if isinstance(value, bool) or not isinstance(value, int):
            return
        key = path.rsplit(".", 1)[-1].split("[", 1)[0]
        if key == "id" or key.endswith("_id"):
            offenders.append(f"{path}={value}")

    walk(payload)
    assert not offenders, (
        "registry_seed.json still ships integer values for UUID-backed entity IDs: "
        f"{offenders[:20]}"
    )


def test_registry_seed_ships_live_agents_without_a_pinned_route():
    """Shipped agents must leave provider/model to the deployment, not pin one in the seed.

    A built-in API provider is not a portable seed value: the runtime refuses any API
    route without a verified saved provider, and that provider's id is per-install.
    The route belongs to project/account settings.
    """
    payload = json.loads((ROOT / "db" / "seeds" / "registry_seed.json").read_text())
    agents = {
        str(item.get("agent_id") or "").strip(): dict(item)
        for item in (payload.get("agents") or [])
        if isinstance(item, dict)
    }
    # Every seeded agent, not a fixed roster: a hardcoded list let
    # delivery_manager ship with provider "openai" and an empty model, which
    # fails at runtime with "agent override must configure provider and model
    # together" — the exact drift this guard exists to catch.
    for agent_id in sorted(agents):
        cfg = dict((agents.get(agent_id) or {}).get("config") or {})
        provider = str(cfg.get("provider") or "").strip()
        model = str(cfg.get("model") or "").strip()
        assert not provider, (
            f"registry seed drift: '{agent_id}' must ship without a pinned provider/model so the deployment chooses its own route, got {cfg!r}"
        )
        assert not model, (
            f"registry seed drift: '{agent_id}' must ship without a pinned provider/model so the deployment chooses its own route, got {cfg!r}"
        )


# ---------------------------------------------------------------------------
# 11. Database artifacts — retired migrations/ must stay gone after the clean-slate cutover
# ---------------------------------------------------------------------------

_LIVE_DB_ARTIFACT_REFERENCES = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "core" / "application" / "tooling" / "quality" / "check_naming.py",
    ROOT / "core" / "entrypoints" / "cli" / "scripts" / "lint.sh",
    ROOT / "infra" / "tooling" / "sonar-project.properties",
]


def test_legacy_migrations_directory_removed():
    """Root migrations/ stays forbidden; post-1.0 SQL migrations live in db/migrations/."""
    assert not (ROOT / "migrations").exists(), (
        "Retired root migrations/ directory has reappeared. "
        "Post-1.0 SQL migrations belong under db/migrations/ on top of the db/schema/ baseline."
    )


def test_db_migrations_directory_is_the_allowed_migration_home():
    """db/migrations/ is the only allowed migration directory (ADR 0012)."""
    db_migrations = ROOT / "db" / "migrations"
    assert db_migrations.is_dir(), (
        "db/migrations/ must exist as the canonical post-1.0 SQL migration directory."
    )
    # Any files present must be SQL migrations (no per-migration Python files).
    stray_python = sorted(path.name for path in db_migrations.glob("*.py"))
    assert stray_python == [], (
        f"Per-migration Python files are not allowed under db/migrations/: {stray_python}."
    )


def test_init_db_uses_clean_bootstrap_only():
    """core/application/tooling/db/init_db.py must not ship the retired migration runner anymore."""
    source = (ROOT / "core" / "application" / "tooling" / "db" / "init_db.py").read_text()
    forbidden = [
        "schema_migrations",
        "apply_sql_migrations",
        "discover_migration_sources",
        "MigrationSource",
        "LEGACY_CUTOFF_MARKERS",
        "ensure_auth_schema_compat",
        "ensure_project_runtime_schema_compat",
        "ensure_mcp_connector_schema_compat",
        "schema shape checks",
        "ADD COLUMN IF NOT EXISTS",
        "CREATE TABLE IF NOT EXISTS public.",
    ]
    hits = [token for token in forbidden if token in source]
    assert not hits, (
        f"core/application/tooling/db/init_db.py still contains retired migration/backfill pieces: {hits}. "
        "The clean-slate DB flow must rely on db/schema + db/seeds only."
    )


def test_extension_install_store_does_not_create_runtime_tables():
    """DB-backed Extension install runtime must rely on canonical schema, not auto-DDL."""
    source = (ROOT / "core" / "application" / "extensions" / "install.py").read_text()
    forbidden = [
        "CREATE TABLE IF NOT EXISTS",
        "_CREATE_TABLE_SQL",
        "def _ensure_table",
        "cur.execute(_CREATE_TABLE_SQL)",
    ]
    hits = [token for token in forbidden if token in source]
    assert not hits, (
        f"DBExtensionInstallStore still contains runtime table creation logic: {hits}. "
        "Keep extension_installs owned by db/schema/*.sql and the runtime schema contract."
    )


def test_db_upgrade_entrypoint_does_not_run_schema_compat_backfills():
    """Upgrade command applies canonical seeds/state only; schema shape comes from canonical SQL."""
    source = (ROOT / "core" / "application" / "tooling" / "db" / "upgrade_db.py").read_text()
    forbidden = [
        "ensure_auth_schema_compat",
        "ensure_project_runtime_schema_compat",
        "ensure_mcp_connector_schema_compat",
    ]
    hits = [token for token in forbidden if token in source]
    assert not hits, (
        f"core/application/tooling/db/upgrade_db.py still invokes retired schema backfills: {hits}. "
        "Existing environments must be reset/upgraded from the canonical schema path, not patched at runtime."
    )


def test_live_docs_and_tooling_reference_db_schema_and_seeds_not_migrations():
    """Operator-facing docs and tooling should describe db/schema + db/seeds as canonical."""
    bad: dict[str, str] = {}
    for path in _LIVE_DB_ARTIFACT_REFERENCES:
        source = path.read_text()
        # db/migrations/ is the allowed post-1.0 SQL migration directory and
        # public.platform_schema_migrations is its tracking table (ADR 0012).
        # Neutralize those allowed tokens before checking for a bare/root
        # migrations/ reference or the retired standalone-runner concept.
        probe = source.replace("db/migrations", "").replace("platform_schema_migrations", "")
        if "migrations/" in probe or "schema_migrations" in probe or "db/bootstrap" in probe:
            bad[str(path.relative_to(ROOT))] = "retired migration reference"
        elif "db/schema" not in source and "db/seeds" not in source:
            bad[str(path.relative_to(ROOT))] = "missing db/schema/db/seeds reference"
    assert not bad, (
        f"Live docs/tooling are out of sync with the canonical DB artifact contract: {bad}. "
        "Keep operator-facing references on db/schema + db/seeds only."
    )


_CLI_PLATFORM_PACKAGE_ROOTS = (
    ROOT / "core" / "entrypoints" / "cli" / "platform",
    ROOT / "core" / "entrypoints" / "cli" / "tui",
)


def test_cli_platform_packages_do_not_use_command_specific_prompts():
    """Schema-driven CLI input must not drift back to input/getpass calls."""
    forbidden_calls = re.compile(r"\b(?:input|getpass)\s*\(")
    offenders: list[str] = []

    for package_root in _CLI_PLATFORM_PACKAGE_ROOTS:
        if not package_root.exists():
            continue
        for source_path in package_root.rglob("*.py"):
            if forbidden_calls.search(source_path.read_text(encoding="utf-8")):
                offenders.append(str(source_path.relative_to(ROOT)))

    assert offenders == [], (
        "CLI platform packages must use schema-driven input adapters instead of "
        f"command-specific input()/getpass() calls: {offenders}"
    )


def test_release_branch_ships_dataset_registry_not_partial_runtime_leftovers():
    """Datasets are a first-party registry entity; connector execution remains extension-owned."""
    forbidden_paths = (
        ROOT / "core" / "infrastructure" / "datasets",
        ROOT / "core" / "entrypoints" / "api" / "datasets.py",
        ROOT / "tests" / "unit" / "test_dataset_service.py",
        ROOT / "tests" / "security" / "test_dataset_sql_safety.py",
    )
    present = [str(path.relative_to(ROOT)) for path in forbidden_paths if path.exists()]
    assert not present, (
        "Dataset connector/runtime execution must stay behind extension contracts. "
        f"Remove these partial runtime leftovers: {present}"
    )

    router = (ROOT / "assets" / "frontend" / "src" / "app" / "router.tsx").read_text(
        encoding="utf-8"
    )
    shell = (
        ROOT / "assets" / "frontend" / "src" / "components" / "layout" / "AppShell.tsx"
    ).read_text(encoding="utf-8")
    schema = read_canonical_schema_sql()

    assert 'path="datasets" element={<RegistryDatasetsPage />}' in router
    assert 'to: "/registry/datasets"' in shell
    assert "CREATE TABLE public.registry_datasets" in schema


def test_application_datasets_orchestrates_but_does_not_implement_connectors():
    """ADR 0019: the platform orchestrates connection actions, extensions implement them.

    This replaces a blanket ban on `core/application/datasets/`, which commit
    c0ae854 deleted so the package could ship — with no rationale recorded
    anywhere. A green test achieved by removing an assertion proves nothing
    (CLAUDE-T-03).

    The boundary that actually matters is semantic: orchestration here, protocol
    and transport behind extension contracts. A path ban is easy to satisfy by
    moving code elsewhere without changing its nature; this is not.
    """
    package = ROOT / "core" / "application" / "datasets"
    if not package.is_dir():
        return

    # Driver / protocol / transport imports that would mean the platform had
    # started implementing connectors itself instead of invoking an extension.
    forbidden_imports = (
        "psycopg",
        "pymysql",
        "sqlalchemy",
        "pyodbc",
        "cx_Oracle",
        "snowflake",
        "boto3",
        "socket",
        "paramiko",
        "ftplib",
        "smtplib",
        "requests",
        "httpx",
        "urllib.request",
    )
    offenders: list[str] = []
    for module in sorted(package.rglob("*.py")):
        source = module.read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        for name in forbidden_imports:
            if f"import {name}" in code or f"from {name}" in code:
                offenders.append(f"{module.relative_to(ROOT)} imports {name}")

    assert not offenders, (
        "core/application/datasets must orchestrate connection actions, not implement "
        "connector protocols or transport — those stay behind extension contracts "
        f"(ADR 0019). Found: {offenders}"
    )


# ---------------------------------------------------------------------------
# 1.0 removal of built-in source-code, Git and software-delivery capabilities.
# Single negative guard: these capabilities may return only through an
# extension contract, never as built-in core code, schema, seed or UI.
# ---------------------------------------------------------------------------

_REMOVED_CODE_CAPABILITY_PATHS = (
    "core/application/indexing",
    "core/application/memory/graphs_code_backends.py",
    "core/application/search/search_code_intelligence.py",
    "core/application/orchestration/task_prep_runtime.py",
    "core/application/orchestration/task_prep_runner.py",
    "core/application/orchestration/method",
    "core/application/orchestration/worktree_roots.py",
    "core/application/orchestration/hub_node_worktrees.py",
    "core/application/orchestration/hub_node_quality_core.py",
    "core/application/orchestration/git_evidence.py",
    "core/application/orchestration/delivery_handoff_runtime.py",
    "core/application/orchestration/project_runtime_mapping.py",
    "core/application/orchestration/agents/auto_indexer_node.py",
    "core/application/orchestration/agents/branch_setup_node.py",
    "core/application/orchestration/agents/preflight_node.py",
    "core/application/orchestration/agents/prompt_agent_git.py",
    "core/application/orchestration/tools/code_tools.py",
    "core/application/orchestration/tools/git_tools.py",
    "core/application/orchestration/tools/git_read_tools.py",
    "core/application/orchestration/tools/file_tools.py",
    "core/application/orchestration/tools/file_read_tools.py",
    "core/application/orchestration/tools/text_editor_tool.py",
    "core/application/orchestration/tools/shell_tools.py",
    "core/application/orchestration/tools/lint_tools.py",
    "core/application/workflows/quality_policy.py",
    "core/application/tooling/db/code_index_maintenance.py",
    "core/application/tooling/db/self_project_seed.py",
    "core/entrypoints/admin/project_reindex.py",
    "core/entrypoints/mcp/server_code_tools.py",
    "core/entrypoints/mcp/server_task_prep_tool.py",
    "core/infrastructure/db/code_index_store.py",
    "core/infrastructure/db/code_graph_age_store.py",
    "core/infrastructure/db/worktree_store.py",
    "core/infrastructure/db/parallel_merge_store.py",
    "core/infrastructure/runtime/worker/worker_worktrees.py",
    "assets/frontend/src/pages/projects/index-status",
    "assets/frontend/src/pages/projects/detail/project-task-prep-panel.tsx",
)

_REMOVED_CODE_CAPABILITY_TABLES = (
    "platform_code_index_nodes",
    "platform_code_index_edges",
    "platform_code_index_chunks",
    "platform_code_age_snapshots",
    "project_db_schema_snapshots",
    "job_worktrees",
    "job_parallel_merges",
    "quality_check_types",
    "project_policy_checks",
)

_REMOVED_PROJECT_REPOSITORY_COLUMNS = (
    "repo_url",
    "project_root",
    "git_root",
    "container_root",
    "default_branch",
    "docker_compose_path",
    "supported_extensions",
    "exclude_dirs",
    "exclude_files",
    "watch_config",
    "env_snapshot",
    "project_type",
)

_REMOVED_SEED_IDS = {
    "workflows": (
        "bugfix_contextual",
        "incident_response",
        "maintenance",
        "quick_fix",
        "spike_investigation",
        "standard_feature",
        "story_cycle",
    ),
    "agents": (
        "analyst",
        "architect",
        "bugfix_developer",
        "code_review",
        "delivery_manager",
        "developer",
        "doc_generator",
        "incident_developer",
        "kb_writer",
        "pm",
        "qa",
        "refactor_developer",
        "rework_developer",
        "scrum_master",
        "security_scanner",
        "story_verifier",
        "task_verifier",
        "validator",
    ),
    "skills": (
        "analyze_requirements",
        "apply_rework",
        "code_review",
        "create_pr",
        "create_prd",
        "create_stories",
        "design_architecture",
        "fix_bug",
        "generate_docs",
        "handle_incident",
        "implement_story",
        "refactor_code",
        "run_qa",
        "security_scan",
        "update_knowledge_base",
        "verify_story_implementation",
        "verify_task_implementation",
    ),
    "tools": (
        "code_indexer",
        "context7",
        "file_operations",
        "file_read_operations",
        "git_operations",
        "git_read_operations",
        "test_runner",
        "text_editor",
    ),
}

_REMOVED_MCP_TOOLS = (
    "task_prepare_brief",
    "code_search",
    "code_blast_radius",
    "code_file_context",
    "code_project_overview",
    "db_schema",
    "kb_tag_files",
    "project_index_status",
    "job_worktrees",
    "job_parallel_merges",
    "job_stories",
)

# Tokens that only ever named the removed capabilities. Scanned in shipped
# runtime sources (not tests) so a reintroduction fails here, in one place.
_REMOVED_CODE_CAPABILITY_TOKENS = (
    "task_prepare_brief",
    "pending_task_prep",
    "task-prep",
    "prep-task.xml",
    "code_blast_radius",
    "code_file_context",
    "project_index_status",
    "platform_code_index",
    "CodeGraphBackend",
    "build_code_graph_backend",
    "code_index_list_entries",
    "job_worktrees",
    "job_parallel_merges",
    "worktree_policy",
    "project_policy_checks",
    "quality_check_types",
    "/api/auto-index",
    "/reindex",
    "/index-status",
    "/parallel-merges",
    "tree_sitter",
    "sknetwork",
    "FS_PROJECTS_ROOT",
    "FS_TEST_PROJECT_ROOT",
    "FS_AUTO_INDEX",
    "FS_CODE_GRAPH",
    "text_editor_tool",
    "git_read_operations",
    "confirmed_stories",
    "selected_stories",
    "repository_changes",
)

_REMOVED_RUNTIME_DEPENDENCIES = (
    "tree-sitter",
    "tree-sitter-languages",
    "watchdog",
    "numpy",
    "scipy",
    "scikit-network",
    "pymysql",
)


def _shipped_runtime_source_files() -> list[Path]:
    roots = ("core", "assets/frontend/src", "db", "infra/docker", "locales")
    suffixes = {".py", ".ts", ".tsx", ".json", ".sql", ".sh", ".yaml", ".yml", ".po", ".pot"}
    files: list[Path] = []
    for root in roots:
        for path in (ROOT / root).rglob("*"):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if "node_modules" in path.parts or "__pycache__" in path.parts:
                continue
            if ".test." in path.name or path.name.endswith("test-helpers.tsx"):
                continue
            files.append(path)
    return files


def test_removed_code_capability_modules_do_not_exist() -> None:
    present = [rel for rel in _REMOVED_CODE_CAPABILITY_PATHS if (ROOT / rel).exists()]
    assert present == []


def test_canonical_schema_has_no_code_capability_tables_or_repository_columns() -> None:
    schema = read_canonical_schema_sql()
    tables = [
        table
        for table in _REMOVED_CODE_CAPABILITY_TABLES
        if re.search(rf"\b{re.escape(table)}\b", schema)
    ]
    assert tables == []
    projects = re.search(r"CREATE TABLE public\.projects \((.*?)\n\);", schema, re.S)
    assert projects is not None
    columns = [
        column
        for column in _REMOVED_PROJECT_REPOSITORY_COLUMNS
        if re.search(rf"^\s+{column}\s", projects.group(1), re.M)
    ]
    assert columns == []


def test_db_migrations_holds_no_historical_migrations() -> None:
    files = sorted(
        path.name for path in (ROOT / "db" / "migrations").iterdir() if path.name != ".gitkeep"
    )
    assert files == []


def test_registry_seed_has_no_software_delivery_entities() -> None:
    seed = json.loads(REGISTRY_SEED.read_text(encoding="utf-8"))
    id_keys = {
        "workflows": "workflow_id",
        "agents": "agent_id",
        "skills": "skill_id",
        "tools": "tool_id",
    }
    leftovers = {
        section: sorted(
            {str(row.get(id_keys[section]) or "") for row in seed.get(section) or []}
            & set(forbidden)
        )
        for section, forbidden in _REMOVED_SEED_IDS.items()
    }
    assert leftovers == {section: [] for section in _REMOVED_SEED_IDS}
    lookup = json.loads(LOOKUP_SEED.read_text(encoding="utf-8"))
    assert "quality_check_types" not in lookup


def test_mcp_surface_has_no_code_or_task_prep_tools() -> None:
    from core.entrypoints.mcp.server_api_client import MCP_TOOL_ROUTES
    from core.entrypoints.mcp.server_tool_scopes import MCP_TOOL_SCOPES

    assert sorted(set(_REMOVED_MCP_TOOLS) & set(MCP_TOOL_ROUTES)) == []
    assert sorted(set(_REMOVED_MCP_TOOLS) & set(MCP_TOOL_SCOPES)) == []
    scopes = {scope for values in MCP_TOOL_SCOPES.values() for scope in values}
    assert "code.read" not in scopes


def test_shipped_sources_do_not_reference_removed_code_capabilities() -> None:
    hits: list[str] = []
    for path in _shipped_runtime_source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in _REMOVED_CODE_CAPABILITY_TOKENS:
            if token in text:
                hits.append(f"{path.relative_to(ROOT).as_posix()}: {token}")
    assert hits == []


def test_runtime_requirements_drop_code_analysis_dependencies() -> None:
    declared = {
        re.split(r"[=<>\[ ]", line.strip(), maxsplit=1)[0].lower()
        for name in ("requirements-runtime.txt", "requirements-runtime.lock")
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "--"))
    }
    assert sorted(set(_REMOVED_RUNTIME_DEPENDENCIES) & declared) == []


# Ids of the removed software-delivery catalog. Tests must use neutral fixtures
# (writer / draft_summary / weekly_report) so a green suite cannot quietly keep
# exercising or re-seeding the removed built-ins.
_REMOVED_DELIVERY_FIXTURE_IDS = (
    "standard_feature",
    "quick_fix",
    "bugfix_contextual",
    "incident_response",
    "spike_investigation",
    "story_cycle",
    "bugfix_developer",
    "delivery_manager",
    "incident_developer",
    "refactor_developer",
    "rework_developer",
    "scrum_master",
    "security_scanner",
    "story_verifier",
    "task_verifier",
    "doc_generator",
    "analyze_requirements",
    "apply_rework",
    "create_pr",
    "create_prd",
    "create_stories",
    "design_architecture",
    "fix_bug",
    "generate_docs",
    "handle_incident",
    "implement_story",
    "refactor_code",
    "run_qa",
    "security_scan",
    "update_knowledge_base",
    "verify_story_implementation",
    "verify_task_implementation",
    "code_indexer",
    "code_search",
    "code_blast_radius",
    "code_file_context",
    "file_operations",
    "file_read_operations",
    "git_operations",
    "git_read_operations",
    "text_editor",
    "run_shell",
    "context7",
)


def _fixture_source_files() -> list[Path]:
    guard = Path(__file__).resolve()
    files: list[Path] = []
    for root in ("tests", "extensions"):
        for path in (ROOT / root).rglob("*.py"):
            if "__pycache__" in path.parts or path.resolve() == guard:
                continue
            if root == "extensions" and "tests" not in path.parts:
                continue
            files.append(path)
    frontend = ROOT / "assets" / "frontend" / "src"
    files.extend(
        path
        for path in frontend.rglob("*")
        if path.is_file()
        and "node_modules" not in path.parts
        and (".test." in path.name or path.name.endswith("test-helpers.tsx"))
    )
    return files


def _removed_delivery_id_hits(files: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in files:
        found = whole_words_in(
            path.read_text(encoding="utf-8", errors="ignore"), _REMOVED_DELIVERY_FIXTURE_IDS
        )
        hits.extend(
            f"{path.relative_to(ROOT).as_posix()}: {token}"
            for token in _REMOVED_DELIVERY_FIXTURE_IDS
            if token in found
        )
    return hits


def test_shipped_sources_do_not_reference_removed_delivery_ids() -> None:
    assert _removed_delivery_id_hits(_shipped_runtime_source_files()) == []


def test_test_fixtures_do_not_use_removed_delivery_ids() -> None:
    files = _fixture_source_files()
    assert len(files) > 500, "fixture scan must cover the real test tree"
    assert _removed_delivery_id_hits(files) == []


# Removed built-in agent ids are common words, so only quoted literals count; the
# generic `validator` agent type stays. Removed delivery artifact keys are matched
# as whole words.
_REMOVED_AGENT_ID_LITERALS = (
    "developer",
    "qa",
    "analyst",
    "architect",
    "code_review",
    "code_reviewer",
    "pm",
    "kb_writer",
)
_REMOVED_DELIVERY_ARTIFACT_KEYS = (
    "prd",
    "analyst_brief",
    "requirements_doc",
    "qa_verdict",
    "qa_feedback",
    "qa_override",
    "test_results",
    "linter_report",
    "code_index_context",
    "db_schema_context",
    "code_review_report",
    "code_review_verdict",
    "story_verdict",
    "architecture_doc",
    "qa_report",
)


def _removed_delivery_vocabulary_hits(files: list[Path]) -> list[str]:
    literal_tokens = "|".join(re.escape(token) for token in _REMOVED_AGENT_ID_LITERALS)
    key_tokens = "|".join(re.escape(token) for token in _REMOVED_DELIVERY_ARTIFACT_KEYS)
    literal = re.compile(rf"""["'](?:{literal_tokens})["']""")
    keys = re.compile(rf"\b(?:{key_tokens})\b")
    hits: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        found = sorted(set(literal.findall(text)) | set(keys.findall(text)))
        if found:
            hits.append(f"{path.relative_to(ROOT).as_posix()}: {found}")
    return hits


def test_shipped_sources_drop_removed_delivery_agents_and_artifacts() -> None:
    assert _removed_delivery_vocabulary_hits(_shipped_runtime_source_files()) == []


def test_test_fixtures_drop_removed_delivery_agents_and_artifacts() -> None:
    assert _removed_delivery_vocabulary_hits(_fixture_source_files()) == []


# Case-insensitive, separator-aware delivery vocabulary: `_`, `:`, `-`, `.` and spaces
# all count as boundaries, so qa_reviewer, agent:qa, "QA verdict" and PRD are caught
# where the exact-key scans above would miss them.
_REMOVED_DELIVERY_VOCABULARY = re.compile(
    r"(?<![A-Za-z0-9])(?:qa|prd|analysts?|architects?)(?![A-Za-z0-9])"
    r"|code[ _-]?(?:index|indexer|indexing|intelligence|graph)"
    r"|(?<![A-Za-z0-9])(?:edit|editing|write|writing)\s+(?:the\s+)?code\b",
    re.IGNORECASE,
)


def _delivery_vocabulary_hits(files: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        found = sorted({match.group(0) for match in _REMOVED_DELIVERY_VOCABULARY.finditer(text)})
        if found:
            hits.append(f"{path.relative_to(ROOT).as_posix()}: {found}")
    return hits


def test_shipped_sources_drop_delivery_vocabulary() -> None:
    assert _delivery_vocabulary_hits(_shipped_runtime_source_files()) == []


def test_test_fixtures_drop_delivery_vocabulary() -> None:
    assert _delivery_vocabulary_hits(_fixture_source_files()) == []


def test_registry_seed_ships_no_orphaned_derived_records() -> None:
    seed = json.loads(REGISTRY_SEED.read_text(encoding="utf-8"))
    sources = {
        str(row.get(key) or "")
        for section, key in (
            ("tools", "tool_id"),
            ("skills", "skill_id"),
            ("agents", "agent_id"),
            ("workflows", "workflow_id"),
        )
        for row in seed.get(section) or []
    }
    orphans = [
        str(field.get("field_key") or "")
        for field in seed.get("fields") or []
        if str(field.get("created_from_ref") or "").split(":", 1)[0] not in sources
    ]
    contracts = {str(field.get("data_contract_id") or "") for field in seed.get("fields") or []}
    orphan_contracts = [
        str(contract.get("contract_id") or "")
        for contract in seed.get("data_contracts") or []
        if str(contract.get("contract_id") or "") not in contracts
    ]
    assert orphans == []
    assert orphan_contracts == []


def test_stand_seeders_do_not_send_removed_project_fields() -> None:
    """Acceptance and smoke seeders must not post the removed repository fields.

    The API rejects them (422 "Unknown project field(s)"), so a stale seeder only
    fails later inside a disposable stand, far from the change that broke it.
    """
    fields = "|".join(re.escape(field) for field in _REMOVED_PROJECT_REPOSITORY_COLUMNS)
    payload_key = re.compile(rf"""["'](?:{fields})["']\s*:""")
    roots = (ROOT / "tests" / "fixtures", ROOT / "tests" / "support", ROOT / "scripts")
    hits = [
        path.relative_to(ROOT).as_posix()
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".sh", ".json"}
        and payload_key.search(path.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert hits == []
