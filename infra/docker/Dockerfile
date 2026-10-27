# syntax=docker/dockerfile:1.19

FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS base

WORKDIR /app

RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf '%s\n' 'Binary::apt::APT::Keep-Downloaded-Packages "true";' \
       > /etc/apt/apt.conf.d/keep-cache

RUN --mount=type=cache,id=flow-steward-apt-bookworm-archives,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=flow-steward-apt-bookworm-index,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl

ENV PYTHONPATH=/app

RUN useradd -m -s /bin/bash fs


FROM base AS source-slices

COPY infra/docker/materialize_source_slice.py /usr/local/bin/materialize_source_slice.py
COPY infra/docker/source-slices/ /source-slices/
# Resolve manifests against content-addressed stage inputs, not a later context
# bind view. Only filtered /out/<role> trees reach the final runtime images.
COPY core/ /src/core/
COPY db/ /src/db/
COPY locales/ /src/locales/
COPY VERSION /src/VERSION
RUN mkdir -p /out
RUN set -eu; for role in app worker control compact; do \
      python /usr/local/bin/materialize_source_slice.py materialize \
        --source-root /src \
        --manifest "/source-slices/${role}.json" \
        --output-root "/out/${role}"; \
    done


FROM base AS cli

USER root
COPY --chmod=u=rwX,go=rX infra/docker/sitecustomize.py /usr/local/lib/python3.12/site-packages/sitecustomize.py

RUN --mount=type=cache,id=flow-steward-apt-bookworm-archives,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=flow-steward-apt-bookworm-index,target=/var/lib/apt,sharing=locked \
    install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
       -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && { \
       printf '%s\n' 'Types: deb'; \
       printf '%s\n' 'URIs: https://download.docker.com/linux/debian'; \
       printf 'Suites: %s\n' "$(. /etc/os-release && printf '%s' "$VERSION_CODENAME")"; \
       printf '%s\n' 'Components: stable'; \
       printf 'Architectures: %s\n' "$(dpkg --print-architecture)"; \
       printf '%s\n' 'Signed-By: /etc/apt/keyrings/docker.asc'; \
       } > /etc/apt/sources.list.d/docker.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       docker-ce-cli docker-buildx-plugin docker-compose-plugin git postgresql-client-17

# No git-lfs: it was here because .gitattributes routed bundled extension payloads
# through the lfs filter, so any git call over this checkout invoked it. Core ships
# no extensions and routes nothing through the filter now.
RUN git config --global user.email "agent@flow-steward.local" \
    && git config --global user.name "Flow Steward Agent" \
    && git config --system --add safe.directory /app

COPY --chmod=u=rwX,go=rX requirements-cli.lock .
RUN --mount=type=cache,id=flow-steward-pip-python312,target=/root/.cache/pip,sharing=locked \
    pip install --require-hashes -r requirements-cli.lock

# Keep the retained management runtime physically separate from product/API
# composition. Product commands execute the same canonical Click adapter in
# fs-app; this image owns only shared command contracts and lifecycle code.
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/__init__.py core/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/entrypoints/cli/__init__.py \
    core/entrypoints/cli/lifecycle_click_app.py \
    core/entrypoints/cli/bootstrap_recovery.py \
    core/entrypoints/cli/lifecycle_lock_probe.py \
    core/entrypoints/cli/lifecycle_runtime.py \
    core/entrypoints/cli/maintenance_adapters.py \
    core/entrypoints/cli/clone_composition.py \
    core/entrypoints/cli/compact_platform_runtime.py \
    core/entrypoints/cli/doctor.py \
    core/entrypoints/cli/platform_runtime.py \
    core/entrypoints/cli/secret_input.py \
    core/entrypoints/cli/warm_runtime.py \
    core/entrypoints/cli/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/entrypoints/cli/platform/__init__.py \
    core/entrypoints/cli/platform/click_adapter.py \
    core/entrypoints/cli/platform/command_help.py \
    core/entrypoints/cli/platform/command_parameters.py \
    core/entrypoints/cli/platform/confirmation.py \
    core/entrypoints/cli/platform/errors.py \
    core/entrypoints/cli/platform/execution.py \
    core/entrypoints/cli/platform/help_formatter.py \
    core/entrypoints/cli/platform/input_resolution.py \
    core/entrypoints/cli/platform/interrupts.py \
    core/entrypoints/cli/platform/modes.py \
    core/entrypoints/cli/platform/operation_log.py \
    core/entrypoints/cli/platform/redaction.py \
    core/entrypoints/cli/platform/renderers.py \
    core/entrypoints/cli/platform/result_presentation.py \
    core/entrypoints/cli/platform/service_registry.py \
    core/entrypoints/cli/platform/theme.py \
    core/entrypoints/cli/platform/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/entrypoints/cli/platform/service_bindings/__init__.py \
    core/entrypoints/cli/platform/service_bindings/backup.py \
    core/entrypoints/cli/platform/service_bindings/clone.py \
    core/entrypoints/cli/platform/service_bindings/common.py \
    core/entrypoints/cli/platform/service_bindings/config.py \
    core/entrypoints/cli/platform/service_bindings/database.py \
    core/entrypoints/cli/platform/service_bindings/environment.py \
    core/entrypoints/cli/platform/service_bindings/maintenance.py \
    core/entrypoints/cli/platform/service_bindings/readiness.py \
    core/entrypoints/cli/platform/service_bindings/release.py \
    core/entrypoints/cli/platform/service_bindings/stack.py \
    core/entrypoints/cli/platform/service_bindings/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/entrypoints/cli/tui/__init__.py \
    core/entrypoints/cli/tui/form_app.py \
    core/entrypoints/cli/tui/theme.tcss \
    core/entrypoints/cli/tui/theme_monochrome.tcss \
    core/entrypoints/cli/tui/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/__init__.py core/application/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/auth/config.py core/application/auth/email.py core/application/auth/secrets.py core/application/auth/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/auth/bootstrap_tokens.py core/application/auth/bootstrap_recovery.py core/application/auth/bootstrap_recovery_gateway.py core/application/auth/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/auth/user_admin/contracts.py core/application/auth/user_admin/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/security/__init__.py core/infrastructure/security/secret_codec.py core/infrastructure/security/owner_only_secret.py core/infrastructure/security/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/extensions/identity.py core/application/extensions/scaffold_contracts.py core/application/extensions/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/extensions/artifact_dataset_plane_v2.py \
    core/application/extensions/artifact_dataset_operations.py \
    core/application/extensions/artifact_plane_values.py \
    core/application/extensions/artifact_retention.py \
    core/application/extensions/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/installer/ core/application/installer/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/doctor/ core/application/doctor/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/jobs/schema_metadata.py \
    core/application/jobs/workflow_run_models.py \
    core/application/jobs/workflow_wait_contract.py \
    core/application/jobs/artifact_cleanup_runtime.py \
    core/application/jobs/artifact_cleanup_recovery.py \
    core/application/jobs/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/lifecycle/ core/application/lifecycle/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/monitoring/__init__.py core/application/monitoring/contracts.py core/application/monitoring/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/clone/ core/application/clone/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/maintenance/ core/application/maintenance/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/management/__init__.py core/application/management/errors.py core/application/management/json_safety.py core/application/management/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/management/command_models*.py core/application/management/query_models*.py core/application/management/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/management/project_policy_validation.py core/application/management/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/registry/__init__.py \
    core/application/registry/builder_metadata.py \
    core/application/registry/identifier_keys.py \
    core/application/registry/tool_manifest.py \
    core/application/registry/workflow_contract.py \
    core/application/registry/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/management/command_specs/ core/application/management/command_specs/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/readiness/ core/application/readiness/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/release/ core/application/release/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/security/ core/application/security/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/tooling/__init__.py core/application/tooling/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/tooling/db/ core/application/tooling/db/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/tooling/contracts.py core/application/tooling/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/tooling/extensions/models.py core/application/tooling/extensions/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/tooling/i18n/models.py core/application/tooling/i18n/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/tooling/quality/models.py core/application/tooling/quality/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/tooling/testing/models.py core/application/tooling/testing/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/tooling/ops/ core/application/tooling/ops/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/__init__.py core/infrastructure/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/runtime_env.py core/infrastructure/runtime_containers.py core/infrastructure/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/cli_runtime/ core/infrastructure/cli_runtime/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/clone/ core/infrastructure/clone/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/config/ core/infrastructure/config/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/infrastructure/db/__init__.py \
    core/infrastructure/db/job_activity_store.py \
    core/infrastructure/db/schema_contract.py \
    core/infrastructure/db/schema_contract_catalog.py \
    core/infrastructure/db/schema_state.py \
    core/infrastructure/db/system_settings_store.py \
    core/infrastructure/db/
# Database bootstrap imports storage facades and their explicit dependency closure.
# Keep product/API composition out of the management image.
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/observability_vocabulary.py \
    core/application/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/auth/account_memberships.py \
    core/application/auth/audit.py \
    core/application/auth/password.py \
    core/application/auth/principal.py \
    core/application/auth/resolve.py \
    core/application/auth/scope_contracts.py \
    core/application/auth/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/extensions/release_membership_lock.py \
    core/application/extensions/projected_dataset_bridge.py \
    core/application/extensions/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/jobs/job_lifecycle_repository.py \
    core/application/jobs/public_progress_service.py \
    core/application/jobs/query_scope.py \
    core/application/jobs/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/management/scope.py \
    core/application/management/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/observability/llm_costs.py \
    core/application/observability/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/orchestration/models.py \
    core/application/orchestration/task_envelope.py \
    core/application/orchestration/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/registry/agent_manifest.py \
    core/application/registry/builtin_mcp_tools.py \
    core/application/registry/builtin_standard_tools.py \
    core/application/registry/db_store.py \
    core/application/registry/db_store_agent_contract_mixin.py \
    core/application/registry/db_store_agents_mixin.py \
    core/application/registry/db_store_blocks_mixin.py \
    core/application/registry/db_store_common.py \
    core/application/registry/db_store_connection_mixin.py \
    core/application/registry/db_store_contracts_mixin.py \
    core/application/registry/db_store_dataset_catalog_mixin.py \
    core/application/registry/db_store_datasets_mixin.py \
    core/application/registry/db_store_guardrails_mixin.py \
    core/application/registry/db_store_revisions_mixin.py \
    core/application/registry/db_store_tools_mixin.py \
    core/application/registry/db_store_skills_mixin.py \
    core/application/registry/db_store_workflows_mixin.py \
    core/application/registry/prompt_contracts.py \
    core/application/registry/output_contract_fields.py \
    core/application/registry/step_skill_resolution.py \
    core/application/registry/workflow_definition_rows.py \
    core/application/registry/workflow_lookup.py \
    core/application/registry/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/reports/activity_repository.py \
    core/application/reports/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/application/workflows/execution_graph.py \
    core/application/workflows/workflow_canvas_annotation_contract.py \
    core/application/workflows/workflow_contract_summary.py \
    core/application/workflows/workflow_history_snapshot.py \
    core/application/workflows/workflow_hook_placement.py \
    core/application/workflows/workflow_step_kinds.py \
    core/application/workflows/workflow_step_scope.py \
    core/application/workflows/reaction_scopes.py \
    core/application/workflows/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/infrastructure/db/action_log_store.py \
    core/infrastructure/db/agent_message_store.py \
    core/infrastructure/db/command_audit_store.py \
    core/infrastructure/db/dashboard_rollups_store.py \
    core/infrastructure/db/db_pool.py \
    core/infrastructure/db/decision_override_store.py \
    core/infrastructure/db/event_store.py \
    core/infrastructure/db/extension_release_membership_lock.py \
    core/infrastructure/db/job_admin_store.py \
    core/infrastructure/db/job_attachment_store.py \
    core/infrastructure/db/job_cost_store.py \
    core/infrastructure/db/job_error_store.py \
    core/infrastructure/db/job_queries_store.py \
    core/infrastructure/db/job_scope_contract.py \
    core/infrastructure/db/logger_dashboard_rollups_mixin.py \
    core/infrastructure/db/logger_events_dispatch_common.py \
    core/infrastructure/db/logger_events_dispatch_delivery_attempts_mixin.py \
    core/infrastructure/db/logger_events_dispatch_dispatches_mixin.py \
    core/infrastructure/db/logger_events_dispatch_domain_events_mixin.py \
    core/infrastructure/db/logger_events_dispatch_job_reactions_mixin.py \
    core/infrastructure/db/logger_events_dispatch_mixin.py \
    core/infrastructure/db/logger_events_dispatch_outbox_mixin.py \
    core/infrastructure/db/logger_extensions_accounts_accounts_mixin.py \
    core/infrastructure/db/logger_extensions_accounts_callbacks_mixin.py \
    core/infrastructure/db/logger_extensions_accounts_common.py \
    core/infrastructure/db/logger_extensions_accounts_mixin.py \
    core/infrastructure/db/logger_extensions_accounts_pat_mixin.py \
    core/infrastructure/db/logger_extensions_accounts_query_mixin.py \
    core/infrastructure/db/logger_extensions_accounts_runtime_mixin.py \
    core/infrastructure/db/logger_extensions_accounts_service_tokens_mixin.py \
    core/infrastructure/db/logger_extensions_accounts_sync_mixin.py \
    core/infrastructure/db/logger_lifecycle_projects_mixin.py \
    core/infrastructure/db/logger_policy_projects_common.py \
    core/infrastructure/db/logger_policy_projects_mixin.py \
    core/infrastructure/db/logger_provider_integrations_mixin.py \
    core/infrastructure/db/logger_scheduled_jobs_mixin.py \
    core/infrastructure/db/logger_scheduled_providers_common.py \
    core/infrastructure/db/logger_scheduled_providers_mixin.py \
    core/infrastructure/db/logger_trace_mixin.py \
    core/infrastructure/db/platform_logger.py \
    core/infrastructure/db/platform_logger_action_audit_mixin.py \
    core/infrastructure/db/platform_logger_activity_mixin.py \
    core/infrastructure/db/platform_logger_admin_queries_mixin.py \
    core/infrastructure/db/platform_logger_callback.py \
    core/infrastructure/db/platform_logger_common.py \
    core/infrastructure/db/platform_logger_core_mixin.py \
    core/infrastructure/db/platform_logger_decisions_mixin.py \
    core/infrastructure/db/platform_logger_jobs_mixin.py \
    core/infrastructure/db/platform_logger_scheduled_locks_mixin.py \
    core/infrastructure/db/platform_project_settings_history_mixin.py \
    core/infrastructure/db/platform_stores.py \
    core/infrastructure/db/project_execution_store.py \
    core/infrastructure/db/project_registration_store.py \
    core/infrastructure/db/project_settings_store.py \
    core/infrastructure/db/scheduled_job_lock_store.py \
    core/infrastructure/db/schema_store.py \
    core/infrastructure/db/secret_storage_store.py \
    core/infrastructure/db/step_replay_store.py \
    core/infrastructure/db/
COPY --chmod=u=rwX,go=rX --chown=fs:fs \
    core/infrastructure/security/prompt_safety.py \
    core/infrastructure/security/secret_reference.py \
    core/infrastructure/security/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/projects/connection_repository.py core/application/projects/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/notifications/repository.py core/application/notifications/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/application/workflows/workflow_reaction_scope_repository.py core/application/workflows/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/db/psycopg_runtime/ core/infrastructure/db/psycopg_runtime/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/extension_runtime/package_identity.py core/infrastructure/extension_runtime/object_storage.py core/infrastructure/extension_runtime/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/installer/ core/infrastructure/installer/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/lifecycle/ core/infrastructure/lifecycle/
COPY --chmod=u=rwX,go=rX --chown=fs:fs core/infrastructure/release/ core/infrastructure/release/
COPY --chmod=u=rwX,go=rX --chown=fs:fs db/schema/ db/schema/
COPY --chmod=u=rwX,go=rX --chown=fs:fs db/migrations/ db/migrations/
COPY --chmod=u=rwX,go=rX --chown=fs:fs db/seeds/ db/seeds/
COPY --chmod=u=rwX,go=rX --chown=fs:fs infra/docker/Dockerfile infra/docker/docker-compose.yaml infra/docker/docker-compose.prod.yaml infra/docker/
COPY --chmod=u=rwX,go=rX --chown=fs:fs VERSION VERSION

ARG FS_BUILD_REVISION=unknown
LABEL org.opencontainers.image.revision="${FS_BUILD_REVISION}"

USER fs

ENTRYPOINT ["python", "-m", "core.entrypoints.cli.lifecycle_click_app"]


FROM base AS runtime-dependencies

COPY requirements-runtime.lock .
RUN --mount=type=cache,id=flow-steward-pip-python312,target=/root/.cache/pip,sharing=locked \
    pip install --require-hashes -r requirements-runtime.lock


FROM base AS control-dependencies

COPY requirements-control.lock .
RUN --mount=type=cache,id=flow-steward-pip-python312,target=/root/.cache/pip,sharing=locked \
    pip install --require-hashes -r requirements-control.lock


FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS frontend-base

WORKDIR /app


FROM frontend-base AS frontend-dependencies

COPY assets/frontend/package.json assets/frontend/package-lock.json assets/frontend/
RUN cd assets/frontend && npm ci


FROM frontend-dependencies AS frontend-build

COPY assets/frontend/ assets/frontend/
COPY assets/platform-kit/ assets/platform-kit/
COPY assets/platform-components.css assets/platform-components.css
RUN cd assets/frontend \
    && rm -rf dist \
    && npm run build \
    && test -f dist/index.html


FROM frontend-dependencies AS frontend-development

COPY assets/frontend/ assets/frontend/
COPY assets/platform-kit/ assets/platform-kit/
COPY assets/platform-components.css assets/platform-components.css
COPY core/entrypoints/cli/scripts/start_frontend_dev.sh /usr/local/bin/start_frontend_dev.sh
COPY core/entrypoints/cli/scripts/frontend_shell_freshness.sh /usr/local/lib/frontend_shell_freshness.sh
RUN chmod 0755 /usr/local/bin/start_frontend_dev.sh

ARG BUILD_SHA=unknown
LABEL org.opencontainers.image.revision="${BUILD_SHA}"
ENV FS_BUILD_SHA=${BUILD_SHA}

CMD ["/usr/local/bin/start_frontend_dev.sh"]


FROM runtime-dependencies AS worker-system-tooling

USER root

RUN --mount=type=cache,id=flow-steward-apt-bookworm-archives,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=flow-steward-apt-bookworm-index,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo \
       "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
       https://download.docker.com/linux/debian \
       $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       docker-ce-cli docker-buildx-plugin docker-compose-plugin


FROM worker-system-tooling AS audit-system-tooling

USER root

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends libnss3-tools nodejs git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global user.email "agent@flow-steward.local" \
    && git config --global user.name "Flow Steward Agent" \
    && git config --system --add safe.directory /app

FROM audit-system-tooling AS audit-tooling

USER root

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements-audit.txt .
COPY requirements-semgrep.txt .
COPY requirements-cli.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements-cli.lock \
    && pip install --no-cache-dir -r requirements-audit.txt \
    && python -m pip check

RUN python -m venv /opt/semgrep \
    && /opt/semgrep/bin/pip install --no-cache-dir -r requirements-semgrep.txt \
    && /opt/semgrep/bin/pip check \
    && /opt/semgrep/bin/semgrep --version \
    && printf '%s\n' \
        '#!/bin/sh' \
        'PATH=/opt/semgrep/bin:$PATH' \
        'export PATH' \
        'exec /opt/semgrep/bin/semgrep "$@"' \
        > /usr/local/bin/semgrep \
    && chmod 0755 /usr/local/bin/semgrep

RUN playwright install-deps chromium \
    && playwright install chromium \
    && chmod -R a+rX /ms-playwright

RUN npm install -g \
    eslint@9.38.0 \
    stylelint@16.23.1 \
    stylelint-config-standard@39.0.0 \
    htmlhint@1.7.1


FROM runtime-dependencies AS app-runtime

COPY --chmod=u=rwX,go=rX infra/docker/sitecustomize.py /usr/local/lib/python3.12/site-packages/sitecustomize.py
COPY --chown=fs:fs --from=source-slices /out/app/ /app/
COPY --chmod=u=rwX,go=rX --chown=fs:fs --from=frontend-build /app/assets/frontend/dist assets/frontend/dist
COPY --chmod=u=rwX,go=rX --chown=fs:fs --from=frontend-build /app/assets/platform-kit assets/platform-kit
COPY --chmod=u=rwX,go=rX --chown=fs:fs --from=frontend-build /app/assets/platform-components.css assets/platform-components.css
RUN touch assets/frontend/dist/.vite/manifest.json \
    && pip install --no-cache-dir --no-deps ./core/infrastructure/extension_sdk/public \
    && python -c "import flowsteward_extension_sdk"

ARG BUILD_SHA=unknown
LABEL org.opencontainers.image.revision="${BUILD_SHA}"
ENV FS_BUILD_SHA=${BUILD_SHA}

USER fs

CMD ["/app/core/entrypoints/cli/scripts/start_server.sh"]


FROM worker-system-tooling AS worker-runtime

COPY --chmod=u=rwX,go=rX infra/docker/sitecustomize.py /usr/local/lib/python3.12/site-packages/sitecustomize.py
COPY --chown=fs:fs --from=source-slices /out/worker/ /app/
RUN pip install --no-cache-dir --no-deps ./core/infrastructure/extension_sdk/public \
    && python -c "import flowsteward_extension_sdk"

ARG BUILD_SHA=unknown
LABEL org.opencontainers.image.revision="${BUILD_SHA}"
ENV FS_BUILD_SHA=${BUILD_SHA}

USER fs

CMD ["python", "-m", "core.entrypoints.cli.worker_process"]


FROM control-dependencies AS control-runtime

COPY --chmod=u=rwX,go=rX infra/docker/sitecustomize.py /usr/local/lib/python3.12/site-packages/sitecustomize.py
COPY --chown=fs:fs --from=source-slices /out/control/ /app/

ARG BUILD_SHA=unknown
LABEL org.opencontainers.image.revision="${BUILD_SHA}"
ENV FS_BUILD_SHA=${BUILD_SHA}

USER fs

CMD ["python", "-m", "core.entrypoints.cli.control_process"]


FROM traefik:v3.3@sha256:2cd5cc75530c8d07ae0587c743d23eb30cae2436d07017a5ff78498b1a43d09f AS compact-traefik


FROM base AS compact-runtime

COPY --chmod=u=rwX,go=rX infra/docker/sitecustomize.py /usr/local/lib/python3.12/site-packages/sitecustomize.py

USER root

# The runtime images carry no git: nothing the app, worker or MCP processes run shells
# out to it. git belongs to the stages that operate on a checkout -- cli, audit and dev --
# and to the installer image. This stage also deliberately ships WITHOUT the docker CLI,
# buildx and compose that `worker-system-tooling` carries -- those decide whether the
# worker may drive Docker, which is a capability and attack-surface question, not a
# packaging one.
RUN --mount=type=cache,id=flow-steward-apt-trixie-archives,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=flow-steward-apt-trixie-index,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17

COPY --chmod=u=rwX,go=rX requirements-compact.lock ./
RUN --mount=type=cache,id=flow-steward-pip-python312,target=/root/.cache/pip,sharing=locked \
    pip install --require-hashes -r requirements-compact.lock \
    && python -m pip check

RUN groupadd --gid 10100 fs-artifacts \
    && groupadd --gid 10102 fs-runtime \
    && groupadd --gid 10103 fs-extensions \
    && for identity in app:10001 worker:10002 control:10003 mcp:10004 installer:10005 router:10006 operator:10007; do \
         name="${identity%%:*}"; uid="${identity##*:}"; \
         groupadd --gid "$uid" "fs-$name"; \
         useradd --no-create-home --uid "$uid" --gid "$uid" --shell /usr/sbin/nologin "fs-$name"; \
       done \
    && install -d -o root -g root -m 0755 /data /run/flow-steward-compact

COPY --from=compact-traefik /usr/local/bin/traefik /usr/local/bin/traefik
COPY --from=source-slices /out/compact/ /app/
COPY --chmod=u=rwX,go=rX --from=frontend-build /app/assets/frontend/dist assets/frontend/dist
COPY --chmod=u=rwX,go=rX --from=frontend-build /app/assets/platform-kit assets/platform-kit
COPY --chmod=u=rwX,go=rX --from=frontend-build /app/assets/platform-components.css assets/platform-components.css
COPY --chmod=u=rwX,go=rX VERSION VERSION
COPY --chmod=0755 infra/docker/compact/flow-steward /usr/local/bin/flow-steward
RUN touch assets/frontend/dist/.vite/manifest.json \
    && pip install --no-cache-dir --no-deps ./core/infrastructure/extension_sdk/public \
    && ln -s /data/state/.flow-steward /app/.flow-steward \
    && ln -s /data/state/.flow-steward/runtime /run/flow-steward-runtime \
    && ln -s /data/backups /app/backups \
    && ln -s /data/logs /app/logs \
    && chmod -R a-w /app/core /app/db /app/locales /app/assets \
    && chmod 0755 /usr/local/bin/traefik

ARG BUILD_SHA=unknown
ARG FS_IMAGE_PLATFORM=unknown
LABEL org.opencontainers.image.revision="${BUILD_SHA}" \
      org.opencontainers.image.role="compact-runtime"
ENV FS_BUILD_SHA=${BUILD_SHA} \
    FS_EMBEDDED_IMAGE_REVISION=${BUILD_SHA} \
    FS_EMBEDDED_IMAGE_ROLE=compact-runtime \
    FS_EMBEDDED_IMAGE_PLATFORM=${FS_IMAGE_PLATFORM} \
    FS_DEPLOYMENT_PROFILE=compact \
    FS_COMPACT_DATA_ROOT=/data \
    FS_EXTENSIONS_ROOT=/data/extensions \
    ARTIFACT_LOCAL_ROOT_DIR=/data/artifacts \
    PYTHONPYCACHEPREFIX=/tmp/flow-steward/pycache

ENTRYPOINT ["python", "-m", "core.entrypoints.cli.compact_provider_runtime"]


FROM audit-tooling AS audit

COPY . .
COPY --from=frontend-build /app/assets/frontend/dist /app/assets/frontend/dist
RUN touch assets/frontend/dist/.vite/manifest.json
RUN pip install --no-cache-dir --no-deps ./core/infrastructure/extension_sdk/public \
    && python -c "import flowsteward_extension_sdk" \
    && chown -R fs:fs /app /home/fs
RUN install -d -o fs -g fs -m 0755 /app/assets/frontend/node_modules

ARG BUILD_SHA=unknown
LABEL org.opencontainers.image.revision="${BUILD_SHA}"
ENV FS_BUILD_SHA=${BUILD_SHA}
# lint.sh passes a digest of the files that decide the gate's tools and compares
# this label before every run, so a tool upgrade cannot keep running the old image.
# Declared after the last RUN: a new digest changes metadata only, no layer.
ARG AUDIT_TOOLCHAIN_DIGEST=unknown
LABEL org.flowsteward.audit-toolchain="${AUDIT_TOOLCHAIN_DIGEST}"

USER fs

CMD ["tail", "-f", "/dev/null"]


FROM audit AS dev
CMD ["tail", "-f", "/dev/null"]
