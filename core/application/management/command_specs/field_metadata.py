"""Curated reusable guidance for public management command fields."""

from __future__ import annotations

from core.application.management.command_specs.contracts import CommandSpec, FieldType

_MESSAGE_READ_FROM_A_PROTECTED_FILE_OR_STDIN = "Read from a protected file or stdin"

# Values are (description, representative value). Repeated operational inputs use
# one vocabulary across command families; command-specific meanings are overridden
# below. Secret representative values are rendered as safe-entry placeholders.
_COMMON_GUIDANCE: dict[str, tuple[str, str]] = {
    "actor": ("Operator or automation identity recorded in the audit trail.", "release-operator"),
    "app_base_url": (
        "Base URL of the Flow Steward application exercised by the MCP fuzzer.",
        "https://flow-steward.example.com",
    ),
    "auth_display_name": (
        "Display name assigned to the temporary regression-test user.",
        "Nightfire Operator",
    ),
    "auth_password": (
        "Password for the temporary regression-test user.",
        _MESSAGE_READ_FROM_A_PROTECTED_FILE_OR_STDIN,
    ),
    "auth_role": ("Role granted to the temporary regression-test user.", "operator"),
    "auth_username": (
        "Username created for authenticated regression scenarios.",
        "nightfire.operator@example.com",
    ),
    "author": ("Identity recorded as the author of the database operation.", "release-operator"),
    "base": ("Git revision used as the naming comparison baseline.", "origin/main"),
    "base_url": (
        "Base URL of the Flow Steward deployment to inspect or test.",
        "https://flow-steward.example.com",
    ),
    "baseline": (
        "Path to the previously captured restore-parity baseline file.",
        "backups/parity/baseline.json",
    ),
    "bearer_token": (
        "Bearer token used to authenticate MCP fuzz requests.",
        _MESSAGE_READ_FROM_A_PROTECTED_FILE_OR_STDIN,
    ),
    "before_id": ("Return records with identifiers before this pagination cursor.", "12500"),
    "burst_size": ("Number of MCP requests issued in each fuzzing burst.", "20"),
    "checks": (
        "Comma-separated lint checks to run instead of the full selected mode.",
        "ruff,mypy",
    ),
    "chrome_mcp_script": (
        "Path to the Chrome MCP validation script executed after UI fuzzing.",
        "tests/ui/chrome_mcp_validation.py",
    ),
    "code": ("BCP 47-style locale code used for catalog files.", "lt"),
    "comment": (
        "Operator note explaining the approval, rejection, or cancellation.",
        "Validated during release review",
    ),
    "compose_only": ("Run only the Docker Compose portion of the preflight check.", "true"),
    "dsn": (
        "PostgreSQL connection string supplied through an approved secure source.",
        _MESSAGE_READ_FROM_A_PROTECTED_FILE_OR_STDIN,
    ),
    "entry_path": ("Published workflow entry path used to start the task.", "webhook/intake"),
    "extra_state": (
        "JSON object containing public task annotations.",
        '{"priority":"high"}',
    ),
    "fail_fast": ("Stop the regression run after the first failing scenario.", "true"),
    "files": (
        "Comma-separated repository files to check for naming violations.",
        "core/application/jobs/service.py,tests/unit/test_jobs.py",
    ),
    "force": ("Recompile catalogs even when generated outputs appear current.", "true"),
    "host_url": ("Base URL of the SonarQube or SonarCloud API.", "https://sonar.example.com"),
    "input_csv": ("Path to one exported Sonar issues CSV file.", "reports/sonar/issues.csv"),
    "input_dir": ("Directory containing exported Sonar issues CSV files.", "reports/sonar"),
    "job_id": ("Stable identifier of the job to inspect or change.", "job_01JABCDEF1234567890"),
    "lane": ("Named validation lane that determines the scenario set.", "backend"),
    "lighthouse_routes": (
        "Comma-separated routes included in the Lighthouse audit.",
        "/,/projects,/workflows",
    ),
    "lighthouse_threshold": ("Minimum acceptable Lighthouse score from zero to one.", "0.9"),
    "lighthouse_timeout": ("Maximum seconds allowed for each Lighthouse route audit.", "120"),
    "limit": ("Maximum number of records returned by the command.", "100"),
    "maintenance_confirmed": ("Confirm writers are stopped and a verified backup exists.", "true"),
    "max_connections": ("PostgreSQL max_connections value used for tuning recommendations.", "200"),
    "max_p0": ("Maximum number of priority-zero backlog items to generate.", "10"),
    "max_p1": ("Maximum number of priority-one backlog items to generate.", "30"),
    "max_query_len": ("Maximum SQL characters retained in each diagnostic result.", "2000"),
    "max_text_size": (
        "Largest generated text payload, in characters, used by the API fuzzer.",
        "4096",
    ),
    "mcp_base_url": (
        "Base URL of the MCP HTTP endpoint exercised by the fuzzer.",
        "https://flow-steward.example.com/mcp",
    ),
    "memory_gb": ("Host memory in GiB used to calculate PostgreSQL settings.", "16"),
    "min_rows": ("Exclude diagnostic queries affecting fewer than this many rows.", "1000"),
    "name": (
        "Short migration name used to build the timestamped SQL filename.",
        "add_job_priority",
    ),
    "native_label": ("Locale name written in the locale's own language.", "Lietuvių"),
    "newest": ("Return the newest records first instead of chronological order.", "true"),
    "offset": ("Number of matching records to skip before returning results.", "100"),
    "output": ("Path where the generated artifact is written.", "reports/output.json"),
    "output_dir": ("Directory where exported Sonar issue files are written.", "reports/sonar"),
    "packages_only": ("Run only the Python and frontend package preflight checks.", "true"),
    "page_size": ("Number of Sonar issues requested from the API per page.", "500"),
    "params": (
        "JSON object containing report-specific generation parameters.",
        '{"window_days":30}',
    ),
    "password": (
        "Password used by authenticated test requests.",
        _MESSAGE_READ_FROM_A_PROTECTED_FILE_OR_STDIN,
    ),
    "path": ("Repository-relative path checked against CI exclusion rules.", "core/application"),
    "persist": ("Store the generated report so it can be retrieved later.", "true"),
    "poll_seconds": ("Seconds to wait between cutover-smoke status polls.", "2"),
    "producer": ("Limit messages to the component that produced them.", "worker"),
    "project_id": (
        "Project identifier that scopes the operation.",
        "00000000-0000-0000-0000-000000000123",
    ),
    "project_key": ("Sonar project key whose issues are exported.", "flow-steward"),
    "project_name": (
        "Registered project name checked by the release preflight.",
        "Flow Steward App",
    ),
    "report_id": (
        "Stable identifier of a previously persisted report.",
        "report_01JABCDEF1234567890",
    ),
    "report_path": (
        "Path where the test runner writes its structured report.",
        "reports/validation.json",
    ),
    "report_type": ("Kind of operational report to generate.", "job_summary"),
    "role": ("Application role used for authenticated fuzz requests.", "operator"),
    "root": (
        "Repository root containing canonical database migration directories.",
        "/srv/flow-steward",
    ),
    "routes": (
        "Comma-separated application routes included in UI fuzzing.",
        "/projects,/workflows",
    ),
    "rules": ("Comma-separated Sonar rule keys that must be enabled.", "python:S1481,python:S3776"),
    "run_chrome_mcp": ("Run the optional Chrome MCP validation after UI fuzzing.", "true"),
    "seed": ("Deterministic random seed used to reproduce a fuzz run.", "424242"),
    "severities": (
        "Comma-separated Sonar severities included in the export.",
        "BLOCKER,CRITICAL,MAJOR",
    ),
    "since_id": ("Return records with identifiers after this pagination cursor.", "12000"),
    "skill": ("Limit messages to those emitted for one skill.", "summarize"),
    "skip_auth": ("Run fuzz requests without creating an authenticated session.", "true"),
    "skip_lighthouse": ("Skip Lighthouse audits during UI fuzzing.", "true"),
    "sonar_token": (
        "API token used to authenticate with Sonar.",
        _MESSAGE_READ_FROM_A_PROTECTED_FILE_OR_STDIN,
    ),
    "sort_by": ("Metric used to order expensive-query diagnostics.", "total"),
    "source": (
        "Path to the source translation catalog used for bootstrap.",
        "locales/en/LC_MESSAGES/messages.po",
    ),
    "source_workflow_id": (
        "Workflow identifier copied for regression scenarios.",
        "release-report",
    ),
    "stale_heartbeat_seconds": (
        "Age in seconds after which a worker heartbeat is considered stale.",
        "90",
    ),
    "status": ("Comma-separated lifecycle statuses used to filter results.", "running,failed"),
    "statuses": ("Comma-separated Sonar issue statuses included in the export.", "OPEN,CONFIRMED"),
    "step_id": ("Limit messages to one workflow step.", "review_report"),
    "storage": ("Storage profile used when calculating PostgreSQL tuning settings.", "ssd"),
    "strict": ("Return a failing exit status when the fuzzer finds any issue.", "true"),
    "summary": ("Task summary submitted by the cutover smoke test.", "Verify production cutover"),
    "task_id": ("Caller-supplied task identifier used for idempotent submission.", "TASK-1042"),
    "task_source": ("Source system or channel that originated the task.", "cli"),
    "task_summary": (
        "Concise description of the work requested from the workflow.",
        "Summarize the release notes",
    ),
    "test_mode": ("Execute the task with test-mode safeguards enabled.", "true"),
    "thread_id": (
        "Approval thread identifier awaiting an operator decision.",
        "thread_01JABCDEF1234567890",
    ),
    "timeout": ("Maximum seconds allowed for each fuzzing HTTP request.", "10"),
    "timeout_seconds": ("Maximum seconds allowed for the command to complete.", "300"),
    "top_files": ("Number of highest-impact files included in the generated backlog.", "20"),
    "top_rules": ("Number of highest-impact rules included in the generated backlog.", "20"),
    "types": (
        "Comma-separated Sonar issue types included in the export.",
        "BUG,VULNERABILITY,CODE_SMELL",
    ),
    "username": (
        "Username used to establish an authenticated fuzzing session.",
        "fuzz.operator@example.com",
    ),
    "verify_ssl": ("Verify the deployment's TLS certificate for outbound requests.", "true"),
    "version_only": ("Run only the application version preflight check.", "true"),
    "with_cutover_smoke": ("Include the cutover smoke suite in the regression run.", "true"),
    "with_full_ui": ("Include the full browser validation suite in the regression run.", "true"),
    "workflow_id": ("Workflow identifier selected for the submitted task.", "release-report"),
}

_COMMAND_GUIDANCE: dict[str, tuple[str, str]] = {
    "environment.switch.target": (
        "Deployment environment to activate without changing the installed release.",
        "production",
    ),
    "environment.switch.file_storage": (
        "Artifact storage backend to retain or activate with the target environment.",
        "keep",
    ),
    "environment.switch.s3_endpoint": (
        "Credential-free HTTPS endpoint for the target S3-compatible service.",
        "https://objects.example.com",
    ),
    "environment.switch.s3_bucket": (
        "Existing bucket that will serve Flow Steward artifacts.",
        "flow-steward-artifacts",
    ),
    "environment.switch.s3_region": (
        "Region used to construct and sign candidate S3 requests.",
        "eu-west-1",
    ),
    "environment.switch.s3_credentials_file": (
        "Owner-only JSON file from which candidate S3 credentials are imported.",
        ".secrets/artifact-s3.json",
    ),
    "environment.switch.allow_insecure_s3_http": (
        "Permit HTTP only for an explicitly selected development S3 target.",
        "false",
    ),
    "environment.switch.artifact_decision": (
        "Explicit availability decision when the prior backend is non-empty or unknown.",
        "accept_unavailable",
    ),
    "jobs.approve.action": (
        "Approval action recorded when resuming the waiting thread.",
        "approve",
    ),
    "jobs.reject.action": ("Rejection action recorded when closing the waiting thread.", "reject"),
    "database.restore-parity-capture.label": (
        "Human-readable label embedded in the captured parity snapshot.",
        "pre-upgrade",
    ),
    "database.restore-parity-verify.label": (
        "Expected snapshot label used when verifying restore parity.",
        "pre-upgrade",
    ),
    "localization.init-locale.label": ("English display name for the new locale.", "Lithuanian"),
    "quality.lint.mode": ("Quality-gate depth to run.", "fast"),
}


def apply_field_metadata(specs: tuple[CommandSpec, ...]) -> tuple[CommandSpec, ...]:
    """Fill missing public guidance from the curated command-field vocabulary."""
    enriched: list[CommandSpec] = []
    for spec in specs:
        fields = []
        for field in spec.fields:
            guidance = _COMMAND_GUIDANCE.get(
                f"{spec.command_id}.{field.key}", _COMMON_GUIDANCE.get(field.key)
            )
            if guidance is None:
                fields.append(field)
                continue
            description, representative = guidance
            update: dict[str, object] = {}
            if not field.description.strip():
                update["description"] = description
            if not field.examples and not field.placeholder.strip():
                if field.type is FieldType.SECRET:
                    update["placeholder"] = representative
                else:
                    update["examples"] = (representative,)
            fields.append(field.model_copy(update=update) if update else field)
        enriched.append(spec.model_copy(update={"fields": tuple(fields)}))
    return tuple(enriched)
