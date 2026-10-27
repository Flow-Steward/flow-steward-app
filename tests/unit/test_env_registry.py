from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application.tooling.ops.generate_env_catalog import (
    render_env_catalog_markdown,
)
from core.infrastructure.config import env_registry
from core.infrastructure.config.env_audit import (
    _scan_env_reads,
    build_env_audit,
)
from core.infrastructure.config.env_registry import (
    StackEnvSelection,
    auth_default_expectations,
    build_installer_env,
    build_lifecycle_env,
    image_pin_keys,
    required_env_keys_for_selection,
    secret_env_keys_for_selection,
    selected_groups,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("field", ("graph_storage", "vector_search"))
def test_registry_rejects_retired_graph_backend_selection(field: str) -> None:
    values = {
        "environment": "production",
        "graph_storage": "postgres_age",
        "vector_search": "pgvector",
        "file_storage": "external_s3",
    }
    values[field] = "neo" + "4j"

    _raises_input_39_1 = StackEnvSelection(**values)
    with pytest.raises(ValueError, match=f"{field} must be"):
        selected_groups(_raises_input_39_1)


def test_registry_selects_expected_groups_for_production_stack() -> None:
    selection = StackEnvSelection(
        environment="production",
        graph_storage="postgres_age",
        vector_search="pgvector",
        file_storage="external_s3",
    )

    groups = set(selected_groups(selection))
    required = set(required_env_keys_for_selection(selection))
    secrets = set(secret_env_keys_for_selection(selection))

    assert "core_runtime" in groups
    assert "artifact_storage_external_s3" in groups
    assert "production_images" in groups
    assert "quality_tools" not in groups
    assert "ARTIFACT_STORAGE_BACKEND" in required
    assert "ARTIFACT_S3_BUCKET" in required
    assert "ENCRYPTION_KEY" in required
    assert "ENCRYPTION_KEY" in secrets
    assert "FS_SECRET_KEY" in secrets
    assert "POSTGRES_PASSWORD" in secrets
    assert set(image_pin_keys()) == {
        "FS_APP_IMAGE",
        "FS_WORKER_IMAGE",
        "FS_CONTROL_IMAGE",
        "MCP_IMAGE",
        "TRAEFIK_IMAGE",
        "POSTGRES_IMAGE",
        "RABBITMQ_IMAGE",
    }
    assert "FS_RELEASE_REPOSITORY" not in image_pin_keys()


@pytest.mark.parametrize("existing", ({}, {"FS_FRONTEND_IMAGE": ""}))
def test_production_installer_does_not_select_development_frontend_image(
    existing: dict[str, str],
) -> None:
    rendered = build_installer_env(
        selection=StackEnvSelection(environment="production", file_storage="local_fs"),
        existing=existing,
        secret_factory=lambda: "generated-secret",
    )

    assert "FS_FRONTEND_IMAGE" not in rendered


@pytest.mark.parametrize("selected", ["", "sha256:" + "a" * 64])
def test_compact_environment_regeneration_does_not_invent_optional_mcp_selection(
    selected: str,
) -> None:
    selection = StackEnvSelection(environment="production", file_storage="local_fs")
    existing = {"MCP_IMAGE": selected} if selected else {}
    rendered = build_installer_env(
        selection=selection,
        existing=existing,
        secret_factory=lambda: "generated-secret",
        deployment_profile="compact",
    )
    regenerated = build_lifecycle_env(
        selection=selection, existing=rendered, deployment_profile="compact"
    )
    for values in (rendered, regenerated):
        if selected:
            assert values["MCP_IMAGE"] == selected
        else:
            assert "MCP_IMAGE" not in values


def test_development_installer_keeps_frontend_image_default() -> None:
    rendered = build_installer_env(
        selection=StackEnvSelection(environment="local_development", file_storage="local_fs"),
        existing={},
        secret_factory=lambda: "generated-secret",
    )

    assert rendered["FS_FRONTEND_IMAGE"] == env_registry.registry_default_value(
        "FS_FRONTEND_IMAGE", environment="local_development"
    )


def test_service_selection_preserves_explicit_local_filesystem_storage() -> None:
    selection = env_registry.selection_from_services(
        stack_environment="production",
        required_services=("fs-app", "fs-worker"),
        file_storage="local_fs",
    )

    required = set(required_env_keys_for_selection(selection))
    assert selection.file_storage == "local_fs"
    assert "ARTIFACT_LOCAL_ROOT_DIR" in required
    assert "ARTIFACT_S3_BUCKET" not in required


def test_lifecycle_environment_preserves_candidate_compose_provenance() -> None:
    """Config validation must render the same immutable candidate pins it is running."""
    selection = StackEnvSelection(
        environment="production",
        graph_storage="postgres_age",
        vector_search="pgvector",
        file_storage="external_s3",
    )
    existing = build_installer_env(
        selection=selection,
        existing={
            "ARTIFACT_S3_ACCESS_KEY": "candidate-access",
            "ARTIFACT_S3_SECRET_KEY": "candidate-secret",  # pragma: allowlist secret
        },
        secret_factory=lambda: "generated-secret",
    )
    existing.update(
        {
            "FS_BUILD_SHA": "b1c4c60fa9173936e8a13feec8c917ab70194ab6",  # pragma: allowlist secret
            "FS_CLI_IMAGE": "sha256:cli-candidate",
            "FS_CLI_IMAGE_REVISION": "c" * 40,
            "FS_APP_IMAGE_REVISION": "a" * 40,
            "FS_APP_IMAGE_DIGEST": "sha256:app-candidate",
            "FS_WORKER_IMAGE_REVISION": "b" * 40,
            "FS_CONTROL_IMAGE_REVISION": "d" * 40,
            "MCP_IMAGE_DIGEST": "sha256:mcp-candidate",
            "MCP_IMAGE_REVISION": "e" * 40,
            "FS_FRONTEND_IMAGE": "sha256:frontend-candidate",
            "FS_FRONTEND_IMAGE_REVISION": "f" * 40,
            "FS_WORKER_MCP_BEARER_TOKEN": "candidate-worker-token",
            "FS_CANDIDATE_STATE_DIR": "/run/flow-steward-candidate-state",
            "FS_CANDIDATE_PROD_ENV": "/app/.env.prod",
            "AWS_CA_BUNDLE": "/app/extensions/minio-ca.pem",
        }
    )

    rendered = build_lifecycle_env(selection=selection, existing=existing)

    assert rendered["FS_BUILD_SHA"] == (  # pragma: allowlist secret
        "b1c4c60fa9173936e8a13feec8c917ab70194ab6"  # pragma: allowlist secret
    )
    assert rendered["FS_CLI_IMAGE"] == "sha256:cli-candidate"
    assert rendered["FS_CLI_IMAGE_REVISION"] == "c" * 40
    assert rendered["FS_APP_IMAGE_REVISION"] == "a" * 40
    assert rendered["FS_APP_IMAGE_DIGEST"] == "sha256:app-candidate"
    assert rendered["FS_WORKER_IMAGE_REVISION"] == "b" * 40
    assert rendered["FS_CONTROL_IMAGE_REVISION"] == "d" * 40
    assert rendered["MCP_IMAGE_DIGEST"] == "sha256:mcp-candidate"
    assert rendered["MCP_IMAGE_REVISION"] == "e" * 40
    assert rendered["FS_FRONTEND_IMAGE"] == "sha256:frontend-candidate"
    assert rendered["FS_FRONTEND_IMAGE_REVISION"] == "f" * 40
    assert rendered["FS_WORKER_MCP_BEARER_TOKEN"] == "candidate-worker-token"
    assert rendered["FS_CANDIDATE_STATE_DIR"] == "/run/flow-steward-candidate-state"
    assert rendered["FS_CANDIDATE_PROD_ENV"] == "/app/.env.prod"
    assert rendered["AWS_CA_BUNDLE"] == "/app/extensions/minio-ca.pem"


def test_local_fs_lifecycle_environment_requires_no_s3_credentials() -> None:
    selection = StackEnvSelection(
        environment="production",
        graph_storage="postgres_age",
        vector_search="pgvector",
        file_storage="local_fs",
    )
    existing = build_installer_env(
        selection=selection,
        existing={},
        secret_factory=lambda: "generated-secret",
    )
    existing["FS_BACKUP_ENCRYPTION_KEY"] = "backup-secret"

    rendered = build_lifecycle_env(selection=selection, existing=existing)

    assert rendered["ARTIFACT_STORAGE_BACKEND"] == "local_fs"
    assert rendered["ARTIFACT_LOCAL_ROOT_DIR"] == "/data/artifacts"
    assert "ARTIFACT_S3_ACCESS_KEY" not in rendered
    assert "ARTIFACT_S3_SECRET_KEY" not in rendered


@pytest.mark.parametrize("profile", ("compact", "scalable"))
def test_config_apply_round_trip_preserves_custom_release_sources(
    tmp_path: Path,
    profile: str,
) -> None:
    from core.application.lifecycle.contracts import DeploymentConfig
    from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore

    selection = StackEnvSelection(
        environment="production",
        graph_storage="postgres_age",
        vector_search="pgvector",
        file_storage="local_fs",
    )
    existing = build_installer_env(
        selection=selection,
        existing={
            "FS_RELEASE_REPOSITORY": "acme/flow-steward",
            "FS_RELEASE_REGISTRIES": "registry.example/acme,mirror.example/acme",
        },
        secret_factory=lambda: "generated-secret",
    )
    existing["FS_BACKUP_ENCRYPTION_KEY"] = "backup-secret"
    config = DeploymentConfig(
        environment="production",
        deployment_profile=profile,
        file_storage="local_fs",
    )
    store = LifecycleConfigurationStore(tmp_path)
    managed = store.build_environment(config, existing)
    env_path = store.env_path(config)

    store.write_runtime_environment(env_path, managed)
    round_trip = store.read_env_values(env_path)

    assert round_trip["FS_RELEASE_REPOSITORY"] == "acme/flow-steward"
    assert round_trip["FS_RELEASE_REGISTRIES"] == ("registry.example/acme,mirror.example/acme")


def test_registry_selects_expected_groups_for_local_quality_stack() -> None:
    selection = StackEnvSelection(
        environment="local_development",
        graph_storage="postgres_age",
        vector_search="pgvector",
        file_storage="bundled_seaweedfs",
        test_runner=True,
        audit_tools=True,
        quality_tools=True,
    )

    groups = set(selected_groups(selection))

    assert "local_development" in groups
    assert "artifact_storage_bundled_seaweedfs" in groups
    assert "test_tools" in groups
    assert "audit_tools" in groups
    assert "quality_tools" in groups
    assert "production_images" not in groups
    assert auth_default_expectations("local_development")["FS_AUTH_COOKIE_SECURE"] == "0"


def test_env_audit_reports_documented_and_used_variables() -> None:
    rows = {row["variable_name"]: row for row in build_env_audit(repo_root=ROOT)}

    attachment = rows["FS_ALLOWED_ATTACHMENT_MIMES"]
    assert any(".env.example" in source for source in attachment["documented_sources"])
    assert "core_attachment_policy" in attachment["conditional_groups"]
    assert attachment["owning_subsystem"] == "attachments"
    assert "core/infrastructure/config/runtime_config.py" in attachment["code_read_locations"]

    key_prefix = rows["ARTIFACT_KEY_PREFIX"]
    assert key_prefix["documented_sources"] == []
    assert any(
        "artifact_storage_factory.py" in location for location in key_prefix["code_read_locations"]
    )
    assert "used but undocumented" in key_prefix["notes"]

    public_base_url = rows["FS_PUBLIC_BASE_URL"]
    assert any(
        "core/application/auth/serve_auth_runtime.py" in location
        for location in public_base_url["code_read_locations"]
    )

    fs_env_file = rows["FS_ENV_FILE"]
    assert any(
        "core/entrypoints/cli/scripts/docker_compose_env.sh" in location
        for location in fs_env_file["code_read_locations"]
    )

    postgres_host = rows["POSTGRES_HOST"]
    assert any(
        "core/infrastructure/db/workflow_knowledge_store_common.py" in location
        for location in postgres_host["code_read_locations"]
    )

    platform_image = rows["FS_APP_IMAGE"]
    assert any(
        "infra/docker/docker-compose.prod.yaml" in location
        for location in platform_image["code_read_locations"]
    )

    postgres_image = rows["POSTGRES_IMAGE"]
    assert any(
        "infra/docker/docker-compose.prod.yaml" in location
        for location in postgres_image["code_read_locations"]
    )

    db_pool_wait_timeout = rows["FS_DB_POOL_WAIT_TIMEOUT"]
    assert any(
        "core/infrastructure/db/db_pool.py" in location
        for location in db_pool_wait_timeout["code_read_locations"]
    )

    openapi_public = rows["FS_OPENAPI_PUBLIC_ENABLED"]
    assert openapi_public["owning_subsystem"] == "api"
    assert openapi_public["conditional_groups"] == ["openapi_docs"]
    assert any(
        "core/infrastructure/config/openapi_docs_config.py" in location
        for location in openapi_public["code_read_locations"]
    )

    docs_internal = rows["FS_DOCS_INTERNAL_ENABLED"]
    assert docs_internal["owning_subsystem"] == "api"
    assert any(
        "core/infrastructure/config/openapi_docs_config.py" in location
        for location in docs_internal["code_read_locations"]
    )


def test_provider_api_keys_are_not_read_from_environment() -> None:
    rows = {row["variable_name"]: row for row in build_env_audit(repo_root=ROOT)}

    assert "GEMINI_API_KEY" not in rows
    assert "GOOGLE_API_KEY" not in rows


def test_env_audit_artifact_stays_in_sync_with_generator() -> None:
    generated = build_env_audit(repo_root=ROOT)
    artifact = Path(ROOT) / "docs/configuration/flow-steward-env-audit.json"

    assert artifact.exists()
    assert artifact.read_text(encoding="utf-8").strip()
    assert json.loads(artifact.read_text(encoding="utf-8")) == generated


def test_env_catalog_artifact_stays_in_sync_with_generator() -> None:
    generated = render_env_catalog_markdown(ROOT)
    artifact = Path(ROOT) / "docs/configuration/flow-steward-env-catalog.md"

    assert artifact.exists()
    assert artifact.read_text(encoding="utf-8").strip()
    assert artifact.read_text(encoding="utf-8") == generated


def test_every_lifecycle_required_durable_secret_is_actually_generated() -> None:
    """A secret the installer demands must be a secret the installer creates.

    validate_lifecycle_configuration names FS_BACKUP_ENCRYPTION_KEY directly in
    required_secrets, and the manifest declares it installer_managed and
    doctor_required — but generation is gated by a separate allowlist it was
    missing from. A deployment built from the documented flow therefore failed
    the doctor on a secret no step produced.
    """
    selection = env_registry.StackEnvSelection(environment="production")
    generated = set(env_registry.generated_secret_env_keys_for_selection(selection))
    assert "FS_BACKUP_ENCRYPTION_KEY" in generated
    assert "ENCRYPTION_KEY" in generated, "the sibling key it is declared alongside"


def test_doctor_required_installer_managed_secrets_are_generated() -> None:
    """Guards the class of defect, not just the one instance found.

    Anything the doctor demands, that the manifest says the installer manages
    and that is a leaf secret rather than a derived value, has to be generated —
    otherwise the shipped install produces a deployment its own doctor rejects.
    """
    selection = env_registry.StackEnvSelection(environment="production")
    required = set(env_registry.required_env_keys_for_selection(selection))
    secrets = set(env_registry.secret_env_keys_for_selection(selection))
    generated = set(env_registry.generated_secret_env_keys_for_selection(selection))
    specs = env_registry.env_spec_map()

    ungenerated = sorted(
        name
        for name in required & secrets
        if name not in generated
        and getattr(specs.get(name), "installer_managed", False)
        # derived values (DSNs, URLs) are composed from leaf secrets, not generated
        and not name.endswith(("_DSN", "_URI", "_URL"))
        # operator-supplied external credentials are outside the installer's gift
        and not name.startswith("ARTIFACT_S3_")
    )
    assert not ungenerated, (
        f"the doctor requires these installer-managed secrets but nothing generates them: "
        f"{ungenerated}"
    )


def _literal_env_defaults(root: Path, *, digits_only: bool = False) -> dict[str, dict[str, str]]:
    """Every literal fallback in core, as {variable: {file: default}}.

    Deliberately not "the default", singular: a variable may be read in more than one
    place, and the premise that there is one value to compare against only holds if
    those places agree.

    An empty default is not a competing value. Several readers use `""` to mean "not
    configured" and branch on it -- `FS_COMPOSE_PROJECT_NAME` decides whether to pass
    `-p` at all -- which is the absence of a default rather than a different one.
    """
    import re

    value = r"\d+" if digits_only else r"[^\"]*"
    readers_source = (
        r"_env_int|_env_flag|env_flag|_runtime_env\.get|runtime_env\.get"
        r"|os\.environ\.get|environ\.get|env\.get"
    )
    patterns = [re.compile(rf'(?:{readers_source})\(\s*"([A-Z][A-Z0-9_]*)"\s*,\s*"({value})"\s*\)')]
    if not digits_only:
        # A default does not always arrive as a quoted string. Two other shapes carry
        # one, and both were hiding a divergence when this guard saw only the first:
        # a bare Python boolean passed to _env_flag, and `get(NAME) or "value"`.
        patterns.append(
            re.compile(r'(?:_env_flag|env_flag)\(\s*"([A-Z][A-Z0-9_]*)"\s*,\s*(True|False)\s*\)')
        )
        patterns.append(
            re.compile(
                r"(?:_runtime_env|runtime_env|os\.environ|environ|env)\.get"
                r'\(\s*"([A-Z][A-Z0-9_]*)"\s*\)\s*or\s*"([^"]+)"'
            )
        )
    readers: dict[str, dict[str, str]] = {}
    for path in sorted((root / "core").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for name, default in pattern.findall(text):
                if not digits_only and not str(default).strip():
                    continue
                readers.setdefault(name, {})[str(path.relative_to(root))] = str(default)
    return readers


def _same_setting(values: set[str]) -> bool:
    """True when every spelling means the same thing.

    `1`, `true` and `True` are one answer; so are `0`, `false` and `False`. Treating
    them as different would bury the divergences that change behaviour under ones that
    do not.
    """

    def normalized(value: str) -> str:
        low = value.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return "on"
        if low in {"0", "false", "no", "off"}:
            return "off"
        return low

    return len({normalized(value) for value in values}) == 1


def _integer_env_defaults(root: Path) -> dict[str, dict[str, str]]:
    return _literal_env_defaults(root, digits_only=True)


def test_one_variable_is_read_with_one_default_everywhere() -> None:
    """The premise the next guard rests on, asserted instead of assumed.

    AC17-R116: aligning the control loops' defaults left the manifest documenting
    numbers nothing used, and fixing that compared the manifest against a single
    reader. FS_WORKFLOW_KNOWLEDGE_SYNC_INTERVAL_SECONDS has two -- the control loop
    compiled 300, readiness compiled 120 -- so the comparison went green while a
    reader still disagreed. Green from the wrong side, at the level of the premise
    rather than the assertion, and the review session found it.

    Where two readers disagree, one of them is describing a product that does not
    exist: whichever value the deployment actually carries, both see it.
    """
    root = Path(__file__).resolve().parents[2]
    readers = _integer_env_defaults(root)
    assert len(readers) >= 20, "no integer environment defaults found; this guard is vacuous"

    divergent = {
        name: sites for name, sites in sorted(readers.items()) if len(set(sites.values())) > 1
    }
    assert not divergent, (
        "the same variable is read with different compiled-in defaults, so at least one "
        f"reader describes a product that does not exist: {divergent}"
    )


def test_documented_defaults_match_the_ones_the_code_falls_back_to() -> None:
    """A declared default the product does not use is worse than none at all.

    AC17-R116: aligning the control loops' compiled-in defaults with what Compose
    actually sets left the manifest -- the declared contract the env gate walks --
    describing ten numbers that no longer existed in either place. That is the same
    trap the alignment was fixing, one layer along, and I made it: the review session
    found the first of the ten.

    Scoped to variables the installer does not manage, and deliberately. For a managed
    variable the manifest default is what the installer writes into the env file, which
    may legitimately differ from what the code falls back to when nobody wrote
    anything. For an unmanaged one there is nothing between the reader and the code, so
    a different number in the manifest is simply wrong.

    That exclusion rests on a label nothing verifies, and the review session found the
    hole on the very example I used: ARTIFACT_STORAGE_BACKEND is marked
    installer_managed and no installer code writes it, so it sits in the excluded half
    with nothing checking it -- while its three sources disagree three ways. Closing
    that is a guard over the label itself rather than over this comparison, and it is
    the review session's open finding, not this one. Named here so the next reader
    knows the boundary is assumed rather than proven.

    Integers only: booleans are spelled `1` in the manifest and `True` beside the
    reader, and turning that into a finding would bury the ones that change behaviour
    under cosmetics.
    """
    import re

    root = Path(__file__).resolve().parents[2]
    manifest = {
        entry["name"]: entry
        for entry in json.loads(
            (root / "core/infrastructure/config/flow_steward_env_manifest.json").read_text(
                encoding="utf-8"
            )
        )["variables"]
    }
    source = (root / "core/infrastructure/runtime/control/config.py").read_text(encoding="utf-8")
    code_defaults = dict(re.findall(r'_env_int\(\s*"([A-Z_]+)"\s*,\s*"(\d+)"\s*\)', source))
    assert len(code_defaults) >= 10, "no integer defaults found; this guard would be vacuous"

    divergent = {
        name: (str(manifest[name].get("default")), expected)
        for name, expected in sorted(code_defaults.items())
        if name in manifest
        and not manifest[name].get("installer_managed")
        and str(manifest[name].get("default")) != expected
    }
    assert not divergent, (
        "the manifest documents defaults the code does not use, as "
        f"{{name: (documented, actual)}}: {divergent}"
    )


def test_a_declared_variable_is_read_with_one_default_everywhere() -> None:
    """AC17-R129, section B of the image self-sufficiency task.

    The integer guard beside this one could not see string or boolean divergence, and
    three were hiding there. A connectivity probe defaulted the artifact transport off
    while every other reader and the manifest default it on, so the probe tested
    something the runtime does not do. Artifact cleanup defaulted to a different
    bucket name than the storage factory creates, so with the variable unset it swept
    a bucket nothing writes to -- a live install confirms the running name is the
    factory's. And a preflight tool defaulted the base URL to http where the manifest
    declares https.

    Scoped to variables the manifest declares, and that boundary is the point rather
    than a convenience: the manifest is where this product states its environment
    contract, so a variable absent from it has no contract for readers to agree with.
    RABBITMQ_HOST is exactly that case -- two readers, `rabbitmq` and `localhost`,
    neither matching the `rabbitmq-fs` every compose file sets -- and it is left to
    the wider manifest-coverage question rather than being aligned on a guess here.
    """
    import json

    root = Path(__file__).resolve().parents[2]
    declared = {
        entry["name"]
        for entry in json.loads(
            (root / "core/infrastructure/config/flow_steward_env_manifest.json").read_text(
                encoding="utf-8"
            )
        )["variables"]
    }
    readers = _literal_env_defaults(root)
    assert len(readers) >= 40, "no literal environment defaults found; this guard is vacuous"

    divergent = {
        name: sites
        for name, sites in sorted(readers.items())
        if name in declared and not _same_setting(set(sites.values()))
    }
    assert not divergent, (
        "a variable the manifest declares is read with different compiled-in defaults, "
        f"so at least one reader describes a product that does not exist: {divergent}"
    )


@pytest.mark.parametrize(
    "reader",
    (
        "_env_int",
        "_env_float",
        "_env_str",
        "_env_csv_set",
        "_env_flag",
        "_env_bool",
        "_read_int_env",
        "_read_float_env",
        "_bounded_int",
        "os.getenv",
        "os.environ.get",
        "getenv",
        "environ.get",
        "runtime_env.get",
        "runtime_env.getenv",
        "_runtime_env.get",
    ),
)
def test_env_audit_discovers_known_multiline_readers(tmp_path: Path, reader: str) -> None:
    source = tmp_path / "core" / "config.py"
    source.parent.mkdir()
    source.write_text(
        "import os\nfrom os import getenv, environ\n"
        "from core.infrastructure.runtime_env import runtime_env\n"
        "from core.infrastructure.config.runtime_env import runtime_env as _runtime_env\n"
        + f'{reader}(\n    "FIXTURE_MULTILINE_READ",\n    None,\n)\n',
        encoding="utf-8",
    )
    assert _scan_env_reads(tmp_path) == {"FIXTURE_MULTILINE_READ": [{"location": "core/config.py"}]}


def test_env_audit_resolves_only_module_scalar_name_constants(tmp_path: Path) -> None:
    source = tmp_path / "core" / "config.py"
    source.parent.mkdir()
    source.write_text(
        "from core.infrastructure.runtime_env import runtime_env as _runtime_env\n"
        'RETRY_ENV = "FIXTURE_RETRY_ATTEMPTS"\n'
        'DELAY_ENV: str = "FIXTURE_RETRY_DELAY"\n'
        '_env_int(RETRY_ENV, 3)\n_runtime_env.get(\n    DELAY_ENV, "1"\n)\n'
        '_runtime_env.get("FIXTURE_SUBSCRIPT")\n'
        'os.environ["FIXTURE_OS_SUBSCRIPT"]\n'
        '_runtime_env["FIXTURE_RUNTIME_SUBSCRIPT"]\n'
        'UNUSED = "FIXTURE_UNUSED_STRING"\n'
        'DYNAMIC_ENV = "FIXTURE_" + suffix\n_env_int(DYNAMIC_ENV, 0)\n'
        'def local_reader():\n    LOCAL_ENV = "FIXTURE_LOCAL_CONSTANT"\n'
        "    _runtime_env.get(LOCAL_ENV)\n",
        encoding="utf-8",
    )
    assert set(_scan_env_reads(tmp_path)) == {
        "FIXTURE_RETRY_ATTEMPTS",
        "FIXTURE_RETRY_DELAY",
        "FIXTURE_SUBSCRIPT",
        "FIXTURE_OS_SUBSCRIPT",
        "FIXTURE_RUNTIME_SUBSCRIPT",
    }


def test_env_audit_distinguishes_environment_copy_from_unrelated_dicts(tmp_path: Path) -> None:
    source = tmp_path / "core" / "config.py"
    source.parent.mkdir()
    source.write_text(
        "from core.infrastructure.runtime_env import runtime_env as _runtime_env\n"
        "def artifact(environ=None):\n"
        "    env = environ or dict(_runtime_env)\n"
        '    env.get("FIXTURE_ARTIFACT_KEY_PREFIX")\n'
        "def unrelated():\n"
        '    env = {"FIXTURE_DICTIONARY": 1}\n'
        '    env.get("FIXTURE_DICTIONARY")\n'
        '    payload = {}\n    payload.get("FIXTURE_PAYLOAD")\n'
        '    obj.getenv("FIXTURE_UNRELATED_GETENV")\n'
        '    _runtime_env.get(prefix + "FIXTURE_DYNAMIC")\n'
        '# os.getenv("FIXTURE_COMMENT")\n'
        "example = 'os.getenv(\"FIXTURE_QUOTED_EXAMPLE\")'\n",
        encoding="utf-8",
    )
    assert _scan_env_reads(tmp_path) == {
        "FIXTURE_ARTIFACT_KEY_PREFIX": [{"location": "core/config.py"}]
    }


def test_env_audit_preserves_maps_shell_yaml_and_location_dedup(tmp_path: Path) -> None:
    source = tmp_path / "core" / "config.py"
    source.parent.mkdir()
    source.write_text(
        'import os\nENV_OPTIONS = {"FIXTURE_MAP": {"FIXTURE_NESTED_MAP": "x"}}\n'
        'os.getenv("FIXTURE_DEDUP")\nos.getenv("FIXTURE_DEDUP")\n',
        encoding="utf-8",
    )
    (source.parent / "run.sh").write_text(
        "${FIXTURE_SHELL:-default} $FIXTURE_SHELL\n", encoding="utf-8"
    )
    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / "compose.yaml").write_text("${FIXTURE_YAML:?required}\n", encoding="utf-8")
    assert _scan_env_reads(tmp_path) == {
        "FIXTURE_MAP": [{"location": "core/config.py"}],
        "FIXTURE_NESTED_MAP": [{"location": "core/config.py"}],
        "FIXTURE_DEDUP": [{"location": "core/config.py"}],
        "FIXTURE_SHELL": [{"location": "core/run.sh"}],
        "FIXTURE_YAML": [{"location": "infra/compose.yaml"}],
    }


def test_env_audit_excludes_exact_release_ledger_but_keeps_executable_yaml(tmp_path: Path) -> None:
    release = tmp_path / "infra" / "tooling" / "release"
    release.mkdir(parents=True)
    ledger = release / "ledger.yaml"
    provenance = "${FIXTURE_LEDGER_ONLY:?historic} ${FIXTURE_SHARED:?historic}\n"
    ledger.write_text(provenance, encoding="utf-8")
    (release / "runner.yaml").write_text("${FIXTURE_SHARED:?current}\n", encoding="utf-8")
    assert _scan_env_reads(tmp_path) == {
        "FIXTURE_SHARED": [{"location": "infra/tooling/release/runner.yaml"}]
    }
    assert ledger.read_text(encoding="utf-8") == provenance


def test_env_audit_excludes_reassigned_or_shadowed_bindings(tmp_path: Path) -> None:
    source = tmp_path / "core" / "config.py"
    source.parent.mkdir()
    source.write_text(
        "from core.infrastructure.config.runtime_env import runtime_env as _runtime_env\n"
        'SAFE_ENV = "FIXTURE_SAFE_CONSTANT"\n_env_int(SAFE_ENV, 1)\n'
        'REASSIGNED_ENV = "FIXTURE_REASSIGNED_CONSTANT"\n'
        "REASSIGNED_ENV = prefix + suffix\n_env_int(REASSIGNED_ENV, 1)\n"
        'PARAM_ENV = "FIXTURE_PARAMETER_SHADOW"\n'
        'LOCAL_ENV = "FIXTURE_LOCAL_SHADOW"\n'
        "def parameter(PARAM_ENV):\n    _env_str(PARAM_ENV)\n"
        "def local():\n    LOCAL_ENV = prefix + suffix\n    _env_str(LOCAL_ENV)\n"
        "def dict_shadow():\n    _runtime_env = {}\n"
        '    _runtime_env.get("FIXTURE_RUNTIME_DICT_SHADOW")\n'
        "def parameter_shadow(_runtime_env):\n"
        '    _runtime_env.get("FIXTURE_RUNTIME_PARAMETER_SHADOW")\n'
        "def rebound_copy():\n    env = dict(_runtime_env)\n"
        '    env = {}\n    env.get("FIXTURE_REBOUND_COPY")\n'
        'def live():\n    _runtime_env.get("FIXTURE_LIVE_IMPORTED")\n',
        encoding="utf-8",
    )
    assert set(_scan_env_reads(tmp_path)) == {"FIXTURE_SAFE_CONSTANT", "FIXTURE_LIVE_IMPORTED"}


@pytest.mark.parametrize(
    "module", ("core.infrastructure.runtime_env", "core.infrastructure.config.runtime_env")
)
def test_env_audit_discovers_function_local_runtime_imports(tmp_path: Path, module: str) -> None:
    source = tmp_path / "core" / "config.py"
    source.parent.mkdir()
    source.write_text(
        f"def cli():\n    from {module} import runtime_env as local_env\n"
        '    local_env.get("FIXTURE_LOCAL_RUNTIME_IMPORT")\n',
        encoding="utf-8",
    )
    assert _scan_env_reads(tmp_path) == {
        "FIXTURE_LOCAL_RUNTIME_IMPORT": [{"location": "core/config.py"}]
    }


def test_env_audit_discovers_unambiguous_local_os_imports(tmp_path: Path) -> None:
    source = tmp_path / "core" / "config.py"
    source.parent.mkdir()
    source.write_text(
        "def local():\n    import os\n    from os import getenv\n"
        '    os.getenv("FIXTURE_LOCAL_OS_GETENV")\n'
        '    os.environ.get("FIXTURE_LOCAL_OS_MAPPING")\n'
        '    os.environ["FIXTURE_LOCAL_OS_SUBSCRIPT"]\n'
        '    getenv("FIXTURE_LOCAL_BARE_GETENV")\n'
        'def shadow(os, getenv):\n    os.getenv("FIXTURE_PARAMETER_OS")\n'
        '    getenv("FIXTURE_PARAMETER_GETENV")\n'
        "def rebound():\n    import os\n    from os import getenv\n"
        "    os = obj\n    getenv = other\n"
        '    os.getenv("FIXTURE_REBOUND_OS")\n    getenv("FIXTURE_REBOUND_GETENV")\n',
        encoding="utf-8",
    )
    assert set(_scan_env_reads(tmp_path)) == {
        "FIXTURE_LOCAL_OS_GETENV",
        "FIXTURE_LOCAL_OS_MAPPING",
        "FIXTURE_LOCAL_OS_SUBSCRIPT",
        "FIXTURE_LOCAL_BARE_GETENV",
    }


def test_env_audit_scans_only_known_literal_storage_probe_sources(tmp_path: Path) -> None:
    owner = "core/infrastructure/lifecycle/docker_runtime_support.py"
    source = tmp_path / owner
    source.parent.mkdir(parents=True)
    source.write_text(
        'def _bundled_client_prelude():\n    return \'import os\\nos.environ.get("AWS_SESSION_TOKEN")\\nos.environ.get("ARTIFACT_S3_ACCESS_KEY")\\nos.environ.get("ARTIFACT_S3_SECRET_KEY")\\nos.environ["ARTIFACT_S3_ENDPOINT_URL"]\\nos.environ.get("ARTIFACT_S3_REGION")\\nos.environ.get("ARTIFACT_S3_USE_SSL", "true")\\nos.environ["ARTIFACT_S3_BUCKET"]\\n# os.getenv("FIXTURE_EMITTED_COMMENT")\\nexample = \\\'os.getenv("FIXTURE_EMITTED_QUOTED_EXAMPLE")\\\'\\n\'\n\ndef local_storage_write_probe_script():\n    return \'import os\\nos.environ["ARTIFACT_LOCAL_ROOT_DIR"]\\n\'\n\ndef ordinary_example_factory():\n    return \'import os\\nos.getenv("FIXTURE_ORDINARY_QUOTED_SOURCE")\\n\'\n\nexample = \'import os\\nos.getenv("FIXTURE_ORDINARY_QUOTED_SOURCE")\\n\'\n# os.getenv("FIXTURE_OUTER_COMMENT")\n',
        encoding="utf-8",
    )
    # The same factory name in an unrelated owner is not executable-source provenance.
    (source.parent / "examples.py").write_text(
        'def _bundled_client_prelude():\n    return \'import os\\nos.environ.get("AWS_SESSION_TOKEN")\\nos.environ.get("ARTIFACT_S3_ACCESS_KEY")\\nos.environ.get("ARTIFACT_S3_SECRET_KEY")\\nos.environ["ARTIFACT_S3_ENDPOINT_URL"]\\nos.environ.get("ARTIFACT_S3_REGION")\\nos.environ.get("ARTIFACT_S3_USE_SSL", "true")\\nos.environ["ARTIFACT_S3_BUCKET"]\\n# os.getenv("FIXTURE_EMITTED_COMMENT")\\nexample = \\\'os.getenv("FIXTURE_EMITTED_QUOTED_EXAMPLE")\\\'\\n\'\n\ndef local_storage_write_probe_script():\n    return \'import os\\nos.environ["ARTIFACT_LOCAL_ROOT_DIR"]\\n\'\n\ndef ordinary_example_factory():\n    return \'import os\\nos.getenv("FIXTURE_ORDINARY_QUOTED_SOURCE")\\n\'\n\nexample = \'import os\\nos.getenv("FIXTURE_ORDINARY_QUOTED_SOURCE")\\n\'\n# os.getenv("FIXTURE_OUTER_COMMENT")\n',
        encoding="utf-8",
    )
    assert _scan_env_reads(tmp_path) == {
        name: [{"location": owner}]
        for name in (
            "AWS_SESSION_TOKEN",
            "ARTIFACT_S3_ACCESS_KEY",
            "ARTIFACT_S3_SECRET_KEY",
            "ARTIFACT_S3_ENDPOINT_URL",
            "ARTIFACT_S3_REGION",
            "ARTIFACT_S3_USE_SSL",
            "ARTIFACT_S3_BUCKET",
            "ARTIFACT_LOCAL_ROOT_DIR",
        )
    }


def test_env_audit_rescan_follows_edited_source_content(tmp_path: Path) -> None:
    """Per-file scan results are reused by content, so an edit is never served stale."""
    source = tmp_path / "core" / "config.py"
    source.parent.mkdir()
    first = 'import os\nos.getenv("FIXTURE_FIRST_READ")\n'
    source.write_text(first, encoding="utf-8")
    assert _scan_env_reads(tmp_path) == {"FIXTURE_FIRST_READ": [{"location": "core/config.py"}]}

    source.write_text('import os\nos.getenv("FIXTURE_EDITED_READ")\n', encoding="utf-8")
    assert _scan_env_reads(tmp_path) == {"FIXTURE_EDITED_READ": [{"location": "core/config.py"}]}

    source.write_text("VALUE = 1\n", encoding="utf-8")
    assert _scan_env_reads(tmp_path) == {}

    source.write_text(first, encoding="utf-8")
    assert _scan_env_reads(tmp_path) == {"FIXTURE_FIRST_READ": [{"location": "core/config.py"}]}


def test_env_audit_reads_identifiers_the_parser_normalizes(tmp_path: Path) -> None:
    """The token prefilter must not skip reads spelled with NFKC-equivalent identifiers."""
    source = tmp_path / "core" / "config.py"
    source.parent.mkdir()
    source.write_text(
        "import os\n"
        '\uff4f\uff53.\uff47\uff45\uff54\uff45\uff4e\uff56("FIXTURE_NFKC_READ")\n'
        '\uff25\uff2e\uff36_DEFAULTS = {"FIXTURE_NFKC_MAP": "1"}\n',
        encoding="utf-8",
    )
    assert _scan_env_reads(tmp_path) == {
        "FIXTURE_NFKC_READ": [{"location": "core/config.py"}],
        "FIXTURE_NFKC_MAP": [{"location": "core/config.py"}],
    }


def test_env_audit_preserves_real_storage_probe_reader_locations() -> None:
    reads = _scan_env_reads(ROOT)
    owner = {"location": "core/infrastructure/lifecycle/docker_runtime_support.py"}
    for name in (
        "AWS_SESSION_TOKEN",
        "ARTIFACT_S3_ACCESS_KEY",
        "ARTIFACT_S3_SECRET_KEY",
        "ARTIFACT_S3_ENDPOINT_URL",
        "ARTIFACT_S3_REGION",
        "ARTIFACT_S3_USE_SSL",
        "ARTIFACT_S3_BUCKET",
        "ARTIFACT_LOCAL_ROOT_DIR",
    ):
        assert owner in reads.get(name, []), name
