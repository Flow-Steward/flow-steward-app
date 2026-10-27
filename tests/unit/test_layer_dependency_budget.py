from __future__ import annotations

import ast
from pathlib import Path

from tests.support.source_index import parse_file

INFRASTRUCTURE_ROOT = Path(__file__).resolve().parents[2] / "core" / "infrastructure"
REPO_ROOT = INFRASTRUCTURE_ROOT.parents[1]
# Adapter decomposition can increase the number of files without adding a cycle
# (docker_runtime_support and the API-only OAuth composition adapter are such
# splits). The SCC invariant below is the
# authoritative cycle gate; this remains a secondary shrinking inventory.
INFRASTRUCTURE_APPLICATION_IMPORT_BASELINE = 55
RELEASE_APPLICATION_ADAPTERS = frozenset({"core/infrastructure/release/github.py"})
RELEASE_APPLICATION_IMPORT_SURFACE = frozenset(
    {"core.application.release.catalog.ReleaseDiscoveryError"}
)
INSTALLER_APPLICATION_ADAPTERS = frozenset(
    {
        "core/infrastructure/installer/gateway.py",
        "core/infrastructure/installer/gateway_cli_runtime.py",
        "core/infrastructure/installer/gateway_configuration.py",
        "core/infrastructure/installer/gateway_doctor.py",
        "core/infrastructure/installer/gateway_health.py",
        "core/infrastructure/installer/gateway_host_preflight.py",
        "core/infrastructure/installer/gateway_image_build.py",
        "core/infrastructure/installer/gateway_image_preparation.py",
        "core/infrastructure/installer/gateway_image_provenance.py",
        "core/infrastructure/installer/gateway_image_selection.py",
        "core/infrastructure/installer/gateway_profile_switch.py",
        "core/infrastructure/installer/gateway_runtime.py",
        "core/infrastructure/installer/gateway_support.py",
        "core/infrastructure/installer/gateway_worker_scale.py",
    }
)
# The gateway split retains one existing adapter boundary. Its application
# symbol surface is frozen from 33ddff603, before extraction; new helper paths
# count normally, and the full physical graph remains subject to the SCC gate.
INSTALLER_APPLICATION_IMPORT_SURFACE = frozenset(
    {
        "core.application.installer.contracts.BootstrapVerification",
        "core.application.installer.contracts.ImageCurrency",
        "core.application.installer.contracts.InstallConfig",
        "core.application.installer.contracts.InstallJournal",
        "core.application.installer.contracts.InstallationEvidence",
        "core.application.installer.errors.InstallerFailure",
        "core.application.installer.ports.InstallerLog",
        "core.application.lifecycle.configuration_service.ConfigurationService",
        "core.application.lifecycle.contracts.ComposeTarget",
        "core.application.lifecycle.contracts.DeploymentConfig",
        "core.application.lifecycle.contracts.LifecycleOperationKind",
        "core.application.lifecycle.contracts.profile_owned_environment",
        "core.application.lifecycle.contracts.public_base_url",
        "core.application.lifecycle.contracts.publishes_host_port",
        "core.application.lifecycle.errors.LifecycleError",
        "core.application.readiness.CoreReadinessService",
        "core.application.readiness.ReadinessArea",
        "core.application.readiness.ReadinessStatus",
        "core.application.release.catalog.DEFAULT_RELEASE_REPOSITORY",
        "core.application.release.contracts.ReleaseIdentity",
        "core.application.release.images.required_product_roles",
    }
)
CLONE_APPLICATION_ADAPTERS = frozenset(
    {
        "core/infrastructure/clone/archive_gateway.py",
        "core/infrastructure/clone/capture_policy.py",
        "core/infrastructure/clone/compact_volume_capture.py",
        "core/infrastructure/clone/compact_volume_publication.py",
        "core/infrastructure/clone/envelope.py",
        "core/infrastructure/clone/host_directory_publication.py",
        "core/infrastructure/clone/filesystem_publication.py",
        "core/infrastructure/clone/installation_gateway.py",
        "core/infrastructure/clone/installation_restore_effects.py",
        "core/infrastructure/clone/restore_gateway.py",
        "core/infrastructure/clone/runtime_gateway.py",
        "core/infrastructure/clone/volume_metadata.py",
    }
)


# These are physical splits of existing adapter boundaries, not new permissions
# for infrastructure modules to import arbitrary application services. Freeze
# every imported symbol of the extracted modules and keep the physical SCC
# analysis below unchanged. The inventory still counts each original boundary.
DECOMPOSED_ADAPTER_BOUNDARIES = {
    "core/infrastructure/lifecycle/docker_gateway.py": {
        "core/infrastructure/lifecycle/docker_gateway_contracts.py": frozenset(
            {
                "core.application.lifecycle.contracts.ComposeTarget",
                "core.application.lifecycle.stack_service.StackImageTransition",
                "core.application.lifecycle.stack_service.StackRollbackSnapshot",
                "core.application.lifecycle.stack_service.StackRuntimeState",
            }
        ),
        "core/infrastructure/lifecycle/docker_preservation_gateway.py": frozenset(
            {
                "core.application.lifecycle.preservation_service.UNKNOWN",
            }
        ),
        "core/infrastructure/lifecycle/docker_profile_runtime_state.py": frozenset(
            {
                "core.application.lifecycle.contracts.ComposeTarget",
                "core.application.lifecycle.stack_service.StackGatewayError",
                "core.application.maintenance.contracts.MaintenanceState",
            }
        ),
        "core/infrastructure/lifecycle/docker_rollback_operations.py": frozenset(
            {
                "core.application.lifecycle.contracts.ComposeTarget",
                "core.application.lifecycle.stack_service.StackContainerRollbackState",
                "core.application.lifecycle.stack_service.StackContainerState",
                "core.application.lifecycle.stack_service.StackGatewayError",
                "core.application.lifecycle.stack_service.StackImageTransition",
                "core.application.lifecycle.stack_service.StackRollbackSnapshot",
            }
        ),
        "core/infrastructure/lifecycle/docker_runtime_probes.py": frozenset(
            {
                "core.application.lifecycle.contracts.ComposeTarget",
                "core.application.lifecycle.stack_service.StackGatewayError",
            }
        ),
    },
    "core/infrastructure/lifecycle/configuration_store.py": {
        "core/infrastructure/lifecycle/configuration_files.py": frozenset(
            {
                "core.application.lifecycle.contracts.ComposeTarget",
                "core.application.lifecycle.contracts.DeploymentConfig",
            }
        ),
        "core/infrastructure/lifecycle/configuration_gateway.py": frozenset(
            {
                "core.application.installer.redaction.redact_installer_text",
                "core.application.lifecycle.contracts.ComposeTarget",
                "core.application.lifecycle.contracts.DeploymentConfig",
                "core.application.lifecycle.contracts.publishes_host_port",
                "core.application.lifecycle.errors.CandidateReadinessProbePendingError",
                "core.application.lifecycle.preservation_service.PreservationManifest",
                "core.application.lifecycle.preservation_service.PreservationService",
                "core.application.lifecycle.preservation_service.validate_manifest",
                "core.application.lifecycle.stack_service.LifecycleOperationKind",
                "core.application.lifecycle.stack_service.StackGatewayError",
                "core.application.lifecycle.stack_service.StackRollbackSnapshot",
                "core.application.lifecycle.stack_service.UNKNOWN",
                "core.application.readiness.ReadinessArea",
                "core.application.readiness.ReadinessReport",
                "core.application.readiness.ReadinessStatus",
            }
        ),
    },
    "core/infrastructure/lifecycle/backup_gateway.py": {
        "core/infrastructure/lifecycle/backup_postgres.py": frozenset(
            {
                "core.application.lifecycle.backup_service.BackupGatewayError",
                "core.application.lifecycle.backup_service.BackupLog",
            }
        ),
    },
    "core/infrastructure/runtime/control/scans.py": {
        "core/infrastructure/runtime/control/scans_health.py": frozenset(
            {
                "core.application.jobs.workflow_wait_projection.DURABLE_WAIT_STATUSES",
            }
        ),
    },
    "core/infrastructure/tabular_feed/engine.py": {
        "core/infrastructure/tabular_feed/engine_plane.py": frozenset(
            {
                "core.application.extensions.artifact_dataset_plane_v2.ArtifactDatasetPlane",
            }
        ),
    },
    "core/infrastructure/runtime/http_app_imports.py": {
        "core/infrastructure/runtime/http_app_auth_runtime.py": frozenset(
            {
                "core.application.auth.request_principal_runtime",
            }
        ),
        "core/infrastructure/runtime/http_app_runtime_helpers.py": frozenset(
            {
                "core.application.auth.request_principal_runtime",
            }
        ),
    },
}
PRINCIPAL_ADAPTER_SYMBOLS = {
    "core/infrastructure/runtime/http_app_auth_runtime.py": frozenset(
        {
            "build_request_scope_resolver",
            "normalize_live_principal",
            "optional_actor_username",
            "request_auth_token",
            "request_auth_user_payload",
            "request_principal",
            "require_authenticated_user",
            "resolve_actor_username",
            "roles_intersect",
        }
    ),
    "core/infrastructure/runtime/http_app_runtime_helpers.py": frozenset(
        {
            "normalize_requested_roles",
        }
    ),
}


def _infrastructure_files_importing_application() -> list[str]:
    offenders: list[str] = []
    for path in sorted(INFRASTRUCTURE_ROOT.rglob("*.py")):
        imports_application = False
        for node in parse_file(path).nodes:
            if isinstance(node, ast.Import):
                imports_application = any(
                    alias.name == "core.application" or alias.name.startswith("core.application.")
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                module = str(node.module or "")
                imports_application = module == "core.application" or module.startswith(
                    "core.application."
                )
            if imports_application:
                offenders.append(str(path.relative_to(INFRASTRUCTURE_ROOT.parents[1])))
                break
    return offenders


def _mixed_application_infrastructure_cycles() -> list[list[str]]:
    layer_roots = (REPO_ROOT / "core" / "application", INFRASTRUCTURE_ROOT)
    modules = {
        ".".join(path.with_suffix("").relative_to(REPO_ROOT).parts): path
        for root in layer_roots
        for path in root.rglob("*.py")
    }
    edges: dict[str, set[str]] = {module: set() for module in modules}
    for module, path in modules.items():
        for node in parse_file(path).nodes:
            imported = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [str(node.module or "")]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for candidate in imported:
                resolved = candidate
                while resolved and resolved not in modules:
                    resolved = resolved.rpartition(".")[0]
                if resolved in modules and resolved != module:
                    edges[module].add(resolved)

    index = 0
    indices: dict[str, int] = {}
    low_links: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []

    def visit(module: str) -> None:
        nonlocal index
        indices[module] = low_links[module] = index
        index += 1
        stack.append(module)
        on_stack.add(module)
        for dependency in edges[module]:
            if dependency not in indices:
                visit(dependency)
                low_links[module] = min(low_links[module], low_links[dependency])
            elif dependency in on_stack:
                low_links[module] = min(low_links[module], indices[dependency])
        if low_links[module] != indices[module]:
            return
        component: list[str] = []
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == module:
                break
        if (
            len(component) > 1
            and any(item.startswith("core.application") for item in component)
            and any(item.startswith("core.infrastructure") for item in component)
        ):
            cycles.append(sorted(component))

    for module in modules:
        if module not in indices:
            visit(module)
    return cycles


def _installer_application_import_targets() -> set[str]:
    targets: set[str] = set()
    for relative_path in sorted(INSTALLER_APPLICATION_ADAPTERS):
        source_path = REPO_ROOT / relative_path
        if not source_path.is_file():
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets.update(
                    alias.name
                    for alias in node.names
                    if alias.name == "core.application"
                    or alias.name.startswith("core.application.")
                )
            elif isinstance(node, ast.ImportFrom):
                module = str(node.module or "")
                if module == "core.application" or module.startswith("core.application."):
                    targets.update(f"{module}.{alias.name}" for alias in node.names)
    return targets


def _application_import_targets(relative_paths: frozenset[str]) -> set[str]:
    targets: set[str] = set()
    for relative_path in sorted(relative_paths):
        tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets.update(
                    alias.name
                    for alias in node.names
                    if alias.name == "core.application"
                    or alias.name.startswith("core.application.")
                )
            elif isinstance(node, ast.ImportFrom):
                module = str(node.module or "")
                if module == "core.application" or module.startswith("core.application."):
                    targets.update(f"{module}.{alias.name}" for alias in node.names)
    return targets


def _assert_principal_adapter_symbol_surface() -> None:
    for path, permitted_symbols in PRINCIPAL_ADAPTER_SYMBOLS.items():
        tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        used_symbols = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Name) or node.id != "_request_principal_runtime":
                continue
            parent = parents[node]
            assert isinstance(parent, ast.Attribute), (
                f"Principal adapter {path} must use explicit, frozen attributes"
            )
            assert parent.value is node, (
                f"Principal adapter {path} must use explicit, frozen attributes"
            )
            used_symbols.add(parent.attr)
        assert used_symbols <= permitted_symbols, (
            f"Principal adapter {path} grew its symbol surface: "
            f"{sorted(used_symbols - permitted_symbols)}"
        )


def test_infrastructure_to_application_dependency_budget_can_only_shrink() -> None:
    offenders = _infrastructure_files_importing_application()
    clone_adapters = {path for path in offenders if path.startswith("core/infrastructure/clone/")}
    runtime_adapter = INFRASTRUCTURE_ROOT / "extension_runtime" / "extension_runtime_platform.py"
    application_composition = (
        REPO_ROOT / "core" / "application" / "extensions" / "platform_connection_operations.py"
    )
    assert "core.application" not in runtime_adapter.read_text(encoding="utf-8")
    composition_source = application_composition.read_text(encoding="utf-8")
    assert 'runtime_context["platform_connection_operation"]' in composition_source
    assert 'runtime_context["platform_connection_contract"]' in composition_source
    assert clone_adapters == CLONE_APPLICATION_ADAPTERS
    legacy_offenders = set(offenders) - CLONE_APPLICATION_ADAPTERS
    installer_adapters = legacy_offenders & INSTALLER_APPLICATION_ADAPTERS
    unexpected_installer_targets = (
        _installer_application_import_targets() - INSTALLER_APPLICATION_IMPORT_SURFACE
    )
    assert not unexpected_installer_targets, (
        "Installer adapter application dependency surface grew: "
        f"{sorted(unexpected_installer_targets)}"
    )
    legacy_offenders -= installer_adapters
    if installer_adapters:
        legacy_offenders.add("core/infrastructure/installer/gateway.py")
    release_adapters = legacy_offenders & RELEASE_APPLICATION_ADAPTERS
    unexpected_release_targets = (
        _application_import_targets(RELEASE_APPLICATION_ADAPTERS)
        - RELEASE_APPLICATION_IMPORT_SURFACE
    )
    assert not unexpected_release_targets, (
        f"Release adapter application dependency surface grew: {sorted(unexpected_release_targets)}"
    )
    legacy_offenders -= release_adapters
    for owner, extracted_modules in DECOMPOSED_ADAPTER_BOUNDARIES.items():
        for path, frozen_symbols in extracted_modules.items():
            targets = _application_import_targets(frozenset({path}))
            assert targets <= frozen_symbols, (
                f"Extracted adapter {path} grew application imports: {sorted(targets - frozen_symbols)}"
            )
        extracted_offenders = legacy_offenders.intersection(extracted_modules)
        legacy_offenders -= extracted_offenders
        if extracted_offenders:
            legacy_offenders.add(owner)
    _assert_principal_adapter_symbol_surface()
    assert len(legacy_offenders) <= INFRASTRUCTURE_APPLICATION_IMPORT_BASELINE, (
        "Infrastructure→application dependency budget grew; move composition into a runtime "
        f"layer or reduce the baseline. Current {len(legacy_offenders)} legacy files: "
        f"{sorted(legacy_offenders)}"
    )


def test_clone_composition_lives_in_cli_entrypoint() -> None:
    assert not (INFRASTRUCTURE_ROOT / "clone" / "composition.py").exists()
    composition = REPO_ROOT / "core" / "entrypoints" / "cli" / "clone_composition.py"
    assert composition.exists()
    assert "build_installed_clone_service" in composition.read_text(encoding="utf-8")


def test_clone_host_directory_publication_imports_only_its_entry_contract() -> None:
    assert _application_import_targets(
        frozenset({"core/infrastructure/clone/host_directory_publication.py"})
    ) == {"core.application.clone.contracts.CloneEntry"}


def test_trash_lifecycle_composition_lives_in_api_entrypoint() -> None:
    infrastructure_adapter = INFRASTRUCTURE_ROOT / "runtime" / "http_app_reactions_extensions.py"
    api_composition = REPO_ROOT / "core" / "entrypoints" / "api" / "route_registration.py"

    adapter_source = infrastructure_adapter.read_text(encoding="utf-8")
    # Notification dispatch has its own counted application boundary; this
    # guard prevents trash services from being constructed in infrastructure.
    assert "WorkflowSecretDefaultService" not in adapter_source
    assert "create_workflow_secret_default_repository" not in adapter_source
    composition_source = api_composition.read_text(encoding="utf-8")
    assert "WorkflowSecretDefaultService(" in composition_source
    assert "create_workflow_secret_default_repository(" in composition_source


def test_application_and_infrastructure_module_graph_is_acyclic() -> None:
    assert _mixed_application_infrastructure_cycles() == []


def test_control_process_composition_lives_in_an_entrypoint() -> None:
    old_module = INFRASTRUCTURE_ROOT / "runtime" / "control" / "main.py"
    new_module = REPO_ROOT / "core" / "entrypoints" / "cli" / "control_process.py"
    compose_sources = (
        REPO_ROOT / "infra" / "docker" / "docker-compose.yaml",
        REPO_ROOT / "infra" / "docker" / "docker-compose.prod.yaml",
    )

    assert not old_module.exists()
    assert new_module.exists()
    for compose_path in compose_sources:
        source = compose_path.read_text(encoding="utf-8")
        assert "core.entrypoints.cli.control_process" in source
        assert "core.infrastructure.runtime.control.main" not in source


def test_unused_submission_admission_compatibility_facade_is_absent() -> None:
    assert not (INFRASTRUCTURE_ROOT / "runtime" / "submission_admission_runtime.py").exists()


def test_submission_admission_policy_is_injected_into_infrastructure_runtimes() -> None:
    infrastructure_consumers = (
        INFRASTRUCTURE_ROOT / "runtime" / "control" / "loops.py",
        INFRASTRUCTURE_ROOT / "runtime" / "readiness_runtime.py",
        INFRASTRUCTURE_ROOT / "runtime" / "worker" / "queue.py",
    )
    for path in infrastructure_consumers:
        source = path.read_text(encoding="utf-8")
        assert "core.application.jobs.submission_admission" not in source

    for path in (
        REPO_ROOT / "core" / "entrypoints" / "api" / "app_factory.py",
        REPO_ROOT / "core" / "entrypoints" / "api" / "runtime_wiring.py",
        REPO_ROOT / "core" / "entrypoints" / "cli" / "control_process.py",
        REPO_ROOT / "core" / "entrypoints" / "cli" / "worker_process.py",
    ):
        assert "public_submission_pause_active" in path.read_text(encoding="utf-8")


def test_maintenance_contracts_are_composed_outside_infrastructure_adapters() -> None:
    for path in (
        INFRASTRUCTURE_ROOT / "lifecycle" / "maintenance_gateway.py",
        INFRASTRUCTURE_ROOT / "runtime" / "maintenance_http.py",
    ):
        assert "core.application" not in path.read_text(encoding="utf-8")

    composition = (REPO_ROOT / "core" / "entrypoints" / "cli" / "lifecycle_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "state_model=MaintenanceState" in composition
    assert "lifecycle_error=LifecycleError" in composition


def test_installer_readiness_classification_is_injected_by_cli_composition() -> None:
    probe = INFRASTRUCTURE_ROOT / "installer" / "readiness_probe.py"
    source = probe.read_text(encoding="utf-8")
    assert "core.application" not in source
    assert "installation_classification" in source

    composition = (REPO_ROOT / "core" / "entrypoints" / "cli" / "lifecycle_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "classify_installation(gateway.inspect())" in composition


def test_database_schema_recovery_error_is_application_owned_without_compatibility_shim() -> None:
    application_error_module = REPO_ROOT / "core" / "application" / "lifecycle" / "errors.py"
    infrastructure_error_module = INFRASTRUCTURE_ROOT / "db" / "errors.py"

    assert application_error_module.exists()
    assert "class DatabaseSchemaRecoveryRequiredError" in application_error_module.read_text(
        encoding="utf-8"
    )
    assert not infrastructure_error_module.exists()
    schema_contract_source = (INFRASTRUCTURE_ROOT / "db" / "schema_contract.py").read_text(
        encoding="utf-8"
    )
    composition_source = (INFRASTRUCTURE_ROOT / "runtime" / "entrypoint_db_adapters.py").read_text(
        encoding="utf-8"
    )
    assert "core.application.lifecycle.errors" not in schema_contract_source
    assert "RuntimeSchemaContractViolation" in schema_contract_source
    assert "except RuntimeSchemaContractViolation" in composition_source
    assert "raise DatabaseSchemaRecoveryRequiredError" in composition_source


def test_platform_identity_defaults_are_owned_by_infrastructure_config() -> None:
    identity_module = INFRASTRUCTURE_ROOT / "config" / "platform_identity.py"
    auth_config = REPO_ROOT / "core" / "application" / "auth" / "config.py"

    assert identity_module.exists()
    auth_source = auth_config.read_text(encoding="utf-8")
    assert "from core.infrastructure.config.platform_identity import" in auth_source
    for path in (
        INFRASTRUCTURE_ROOT / "db" / "memory_store_common.py",
        REPO_ROOT / "core" / "application" / "notifications" / "contracts.py",
        INFRASTRUCTURE_ROOT / "db" / "platform_logger_common.py",
        REPO_ROOT / "core" / "application" / "extensions" / "provider_runtime.py",
        INFRASTRUCTURE_ROOT / "db" / "system_settings_store.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "core.application.auth.config" not in source
        assert "core.infrastructure.config.platform_identity" in source


def test_secret_detection_policy_is_owned_by_security_infrastructure() -> None:
    old_module = REPO_ROOT / "core" / "application" / "security" / "secret_detection.py"
    scanner = INFRASTRUCTURE_ROOT / "security" / "scanners" / "secrets.py"
    policy = INFRASTRUCTURE_ROOT / "security" / "secret_detection.py"
    public_projection = REPO_ROOT / "core" / "application" / "jobs" / "public_secret_redaction.py"

    assert not old_module.exists()
    assert policy.exists()
    assert "core.application.security.secret_detection" not in scanner.read_text(encoding="utf-8")
    assert "core.infrastructure.security.secret_detection" in public_projection.read_text(
        encoding="utf-8"
    )


def test_worker_guardrail_composition_lives_in_the_process_entrypoint() -> None:
    worker_bootstrap = INFRASTRUCTURE_ROOT / "runtime" / "worker" / "worker_bootstrap.py"
    process_entrypoint = REPO_ROOT / "core" / "entrypoints" / "cli" / "worker_process.py"
    compose_files = (
        REPO_ROOT / "infra" / "docker" / "docker-compose.yaml",
        REPO_ROOT / "infra" / "docker" / "docker-compose.prod.yaml",
    )

    assert "core.application" not in worker_bootstrap.read_text(encoding="utf-8")
    entrypoint_source = process_entrypoint.read_text(encoding="utf-8")
    assert "ensure_canonical_guardrails_seeded" in entrypoint_source
    for compose_file in compose_files:
        source = compose_file.read_text(encoding="utf-8")
        assert "core.entrypoints.cli.worker_process" in source


def test_queue_naming_is_owned_by_queue_infrastructure() -> None:
    queue_names = INFRASTRUCTURE_ROOT / "runtime" / "worker" / "queue_names.py"
    queue_runtime = INFRASTRUCTURE_ROOT / "runtime" / "worker" / "queue.py"
    queue_broker = INFRASTRUCTURE_ROOT / "runtime" / "worker" / "queue_broker.py"
    worker_delivery = INFRASTRUCTURE_ROOT / "runtime" / "worker" / "worker_delivery.py"
    application_delivery = REPO_ROOT / "core" / "application" / "jobs" / "queue_delivery_runtime.py"

    assert queue_names.exists()
    for path in (queue_runtime, queue_broker, worker_delivery):
        source = path.read_text(encoding="utf-8")
        assert "core.application.jobs.queue_delivery_runtime" not in source
    for path in (queue_broker, worker_delivery):
        assert "core.infrastructure.runtime.worker.queue_names" in path.read_text(encoding="utf-8")
    assert "core.infrastructure.runtime.worker.queue_broker" in queue_runtime.read_text(
        encoding="utf-8"
    )
    assert "core.infrastructure.runtime.worker.queue_names" in application_delivery.read_text(
        encoding="utf-8"
    )


def test_worker_inbound_application_callback_is_injected_by_entrypoint() -> None:
    inbound_polling = INFRASTRUCTURE_ROOT / "runtime" / "worker" / "worker_inbound_polling.py"
    process_entrypoint = REPO_ROOT / "core" / "entrypoints" / "cli" / "worker_process.py"

    assert "core.application" not in inbound_polling.read_text(encoding="utf-8")
    assert "_process_extension_response_envelope" in process_entrypoint.read_text(encoding="utf-8")


def test_extension_runtime_domain_modules_do_not_live_in_database_package() -> None:
    database_package = INFRASTRUCTURE_ROOT / "db"
    runtime_package = INFRASTRUCTURE_ROOT / "extension_runtime"
    domain_modules = {
        "extension_runtime.py",
        "extension_runtime_common.py",
        "extension_runtime_compile.py",
        "extension_runtime_entrypoints.py",
        "extension_runtime_feature_validation.py",
        "extension_runtime_invocation.py",
        "extension_runtime_manifest.py",
        "extension_runtime_platform.py",
        "extension_runtime_ui_manifest.py",
        "extension_runtime_validation.py",
    }

    assert not (
        {path.name for path in database_package.glob("extension_runtime*.py")} & domain_modules
    )
    assert domain_modules.issubset({path.name for path in runtime_package.glob("*.py")})


def test_release_catalog_is_split_from_release_orchestration() -> None:
    release_package = REPO_ROOT / "core" / "application" / "release"
    service = release_package / "service.py"
    catalog = release_package / "catalog.py"
    configuration_store = INFRASTRUCTURE_ROOT / "lifecycle" / "configuration_store.py"

    assert catalog.exists()
    assert len(service.read_text(encoding="utf-8").splitlines()) <= 1_750
    service_source = service.read_text(encoding="utf-8")
    assert "from core.application.release.catalog import" in service_source
    assert "def _load_release_catalog(" not in service_source
    assert "class FilesystemReleaseConfigurationGateway" not in service_source
    assert "class FilesystemReleaseConfigurationGateway" not in configuration_store.read_text(
        encoding="utf-8"
    )
    preservation_source = (
        REPO_ROOT / "core" / "application" / "lifecycle" / "preservation_service.py"
    ).read_text(encoding="utf-8")
    for source in (service_source, preservation_source):
        assert "legacy_baseline_adoption" not in source
        assert "allow_unknown_prior_schema" not in source
    assert "public compatibility export" not in service_source


def test_transform_selector_parsing_is_split_from_transform_execution() -> None:
    orchestration_package = REPO_ROOT / "core" / "application" / "orchestration"
    runtime = orchestration_package / "transform_step_runtime.py"
    selectors = orchestration_package / "transform_selectors.py"

    assert selectors.exists()
    assert len(runtime.read_text(encoding="utf-8").splitlines()) <= 2_050
    runtime_source = runtime.read_text(encoding="utf-8")
    assert "from core.application.orchestration.transform_selectors import" in runtime_source
    assert "def _find_bracket_close(" not in runtime_source


def test_docker_runtime_parsing_is_split_from_lifecycle_gateway() -> None:
    lifecycle_package = INFRASTRUCTURE_ROOT / "lifecycle"
    gateway = lifecycle_package / "docker_gateway.py"
    support = lifecycle_package / "docker_runtime_support.py"

    assert support.exists()
    assert len(gateway.read_text(encoding="utf-8").splitlines()) <= 1_800
    gateway_source = gateway.read_text(encoding="utf-8")
    assert "docker_runtime_support" in gateway_source
    assert "def _json_objects(" not in gateway_source


def test_configuration_artifact_files_are_owned_by_configuration_files() -> None:
    lifecycle_package = INFRASTRUCTURE_ROOT / "lifecycle"
    gateway_source = (lifecycle_package / "docker_gateway.py").read_text(encoding="utf-8")
    files_source = (lifecycle_package / "configuration_files.py").read_text(encoding="utf-8")
    configuration_gateway_source = (lifecycle_package / "configuration_gateway.py").read_text(
        encoding="utf-8"
    )
    assert (
        "from core.infrastructure.lifecycle.configuration_files import"
        in configuration_gateway_source
    )
    artifact_helpers = (
        "_capture_configuration_files",
        "_load_configuration_file_snapshot",
        "_restore_configuration_files",
        "_configuration_files_match",
        "_candidate_tls_bytes",
        "_reverse_proxy_override",
        "_secure_atomic_write_bytes",
        "_secure_unlink",
    )

    for helper in artifact_helpers:
        assert f"def {helper}(" in files_source
        assert f"def {helper}(" not in configuration_gateway_source
        assert f"def {helper}(" not in gateway_source


def test_platform_logger_cycle_edges_are_composed_outside_infrastructure() -> None:
    jobs_mixin = INFRASTRUCTURE_ROOT / "db" / "platform_logger_jobs_mixin.py"
    progress_service = REPO_ROOT / "core" / "application" / "jobs" / "public_progress_service.py"

    assert "core.application.memory.runtime" not in jobs_mixin.read_text(encoding="utf-8")
    assert "workflow_override_runtime" not in progress_service.read_text(encoding="utf-8")
