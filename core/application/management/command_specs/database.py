"""Database tooling public command specifications."""

from __future__ import annotations

from core.application.management.command_specs.contracts import (
    ChoiceItem,
    CommandSpec,
    CompactSupport,
    FieldSpec,
    FieldType,
    InteractivePolicy,
    OperationType,
)
from core.application.tooling.db.models import (
    DatabaseBootstrapSeedRequest,
    DatabaseGenerateMigrationRequest,
    DatabaseOperationResult,
    DatabaseQueryDiagnosticsRequest,
    DatabaseRestoreParityCaptureRequest,
    DatabaseRestoreParityVerifyRequest,
    DatabaseStatusRequest,
    DatabaseTuneEnvRequest,
    DatabaseUpgradeRequest,
)

_TOKEN_DATABASE_STATUS = "database.status"
_TOKEN_DATABASE_UPGRADE = "database.upgrade"
_TOKEN_DATABASE_BOOTSTRAP_SEED = "database.bootstrap-seed"
_TOKEN_DATABASE_GENERATE_MIGRATION = "database.generate-migration"
_TOKEN_DATABASE_QUERY_DIAGNOSTICS = "database.query-diagnostics"
_TOKEN_DATABASE_TUNE_ENV = "database.tune-env"
_TOKEN_DATABASE_RESTORE_PARITY_CAPTURE = "database.restore-parity-capture"
_TOKEN_DATABASE_RESTORE_PARITY_VERIFY = "database.restore-parity-verify"

_DSN_SECRET_FIELD = FieldSpec(
    key="dsn",
    label="Postgres DSN",
    type=FieldType.SECRET,
    secret=True,
    advanced=True,
    allow_secret_file=True,
    allow_secret_stdin=True,
    description=(
        "Optional DSN override. Use --dsn-file or --dsn-stdin; raw DSNs are never "
        "accepted as ordinary options."
    ),
)

_AUTHOR_FIELD = FieldSpec(
    key="author",
    label="Author",
    type=FieldType.RUNTIME_IDENTIFIER,
    default="system",
    option_name="--author",
)

_STORAGE_FIELD = FieldSpec(
    key="storage",
    label="Storage",
    type=FieldType.SINGLE_SELECT,
    default="ssd",
    choices=(
        ChoiceItem(value="ssd", label="SSD"),
        ChoiceItem(value="hdd", label="HDD"),
    ),
    option_name="--storage",
)


DATABASE_COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        command_id=_TOKEN_DATABASE_STATUS,
        compact_support=CompactSupport.PLATFORM,
        path=("database", "status"),
        summary="Inspect database upgrade status.",
        description=(
            "Inspect canonical schema, seed, and post-1.0 migration status through "
            "typed application tooling."
        ),
        examples=("flow-steward database status",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(_DSN_SECRET_FIELD,),
        input_model=DatabaseStatusRequest,
        result_model=DatabaseOperationResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_DATABASE_STATUS,
        result_renderer_ref=_TOKEN_DATABASE_STATUS,
    ),
    CommandSpec(
        command_id=_TOKEN_DATABASE_UPGRADE,
        compact_support=CompactSupport.PLATFORM,
        path=("database", "upgrade"),
        summary="Apply the canonical database upgrade.",
        description="Run the release-safe canonical schema, seed, and migration upgrade operation.",
        examples=("flow-steward database upgrade --author operator",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            _DSN_SECRET_FIELD,
            _AUTHOR_FIELD,
            FieldSpec(
                key="maintenance_confirmed",
                label="Maintenance confirmed",
                type=FieldType.BOOLEAN,
                default=False,
                option_name="--maintenance-confirmed",
                description=(
                    "Confirm writers are stopped and a verified backup exists for "
                    "maintenance migrations."
                ),
            ),
        ),
        input_model=DatabaseUpgradeRequest,
        result_model=DatabaseOperationResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_DATABASE_UPGRADE,
        result_renderer_ref=_TOKEN_DATABASE_UPGRADE,
    ),
    CommandSpec(
        command_id=_TOKEN_DATABASE_BOOTSTRAP_SEED,
        path=("database", "bootstrap-seed"),
        summary="Apply canonical database seed data.",
        description="Apply checked-in lookup and registry seed snapshots through application tooling.",
        examples=("flow-steward database bootstrap-seed --author operator",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(_DSN_SECRET_FIELD, _AUTHOR_FIELD),
        input_model=DatabaseBootstrapSeedRequest,
        result_model=DatabaseOperationResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_DATABASE_BOOTSTRAP_SEED,
        result_renderer_ref=_TOKEN_DATABASE_BOOTSTRAP_SEED,
    ),
    CommandSpec(
        command_id=_TOKEN_DATABASE_GENERATE_MIGRATION,
        path=("database", "generate-migration"),
        summary="Create a post-1.0 SQL migration file.",
        description="Generate one timestamped SQL-only migration template under the selected repository root.",
        examples=("flow-steward database generate-migration add_status_index",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            FieldSpec(
                key="name",
                label="Name",
                type=FieldType.TEXT,
                required=True,
                positional=True,
                description="Short migration description used to build the filename slug.",
            ),
            FieldSpec(
                key="root",
                label="Repository root",
                type=FieldType.PATH,
                default=".",
                option_name="--root",
                advanced=True,
            ),
        ),
        input_model=DatabaseGenerateMigrationRequest,
        result_model=DatabaseOperationResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_DATABASE_GENERATE_MIGRATION,
        result_renderer_ref=_TOKEN_DATABASE_GENERATE_MIGRATION,
    ),
    CommandSpec(
        command_id=_TOKEN_DATABASE_QUERY_DIAGNOSTICS,
        path=("database", "query-diagnostics"),
        summary="Collect Postgres query diagnostics.",
        description="Read query settings, top statements, and seq-scan hotspots without resetting stats.",
        examples=("flow-steward database query-diagnostics --limit 10 --sort-by mean",),
        operation_type=OperationType.READ,
        interactive_policy=InteractivePolicy.IMMEDIATE,
        fields=(
            _DSN_SECRET_FIELD,
            FieldSpec(
                key="limit",
                label="Limit",
                type=FieldType.INTEGER,
                default=15,
                option_name="--limit",
            ),
            FieldSpec(
                key="sort_by",
                label="Sort by",
                type=FieldType.SINGLE_SELECT,
                default="mean",
                choices=(
                    ChoiceItem(value="mean", label="Mean execution time"),
                    ChoiceItem(value="total", label="Total execution time"),
                    ChoiceItem(value="max", label="Max execution time"),
                    ChoiceItem(value="calls", label="Calls"),
                ),
                option_name="--sort-by",
            ),
            FieldSpec(
                key="min_rows",
                label="Minimum rows",
                type=FieldType.INTEGER,
                default=1000,
                option_name="--min-rows",
            ),
            FieldSpec(
                key="max_query_len",
                label="Max query length",
                type=FieldType.INTEGER,
                default=140,
                option_name="--max-query-len",
            ),
        ),
        input_model=DatabaseQueryDiagnosticsRequest,
        result_model=DatabaseOperationResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_DATABASE_QUERY_DIAGNOSTICS,
        result_renderer_ref=_TOKEN_DATABASE_QUERY_DIAGNOSTICS,
    ),
    CommandSpec(
        command_id=_TOKEN_DATABASE_TUNE_ENV,
        path=("database", "tune-env"),
        summary="Generate a tuned Postgres env block.",
        description="Generate a pgtune-style Postgres environment profile through typed application tooling.",
        examples=("flow-steward database tune-env --memory-gb 8 --max-connections 50",),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            FieldSpec(
                key="memory_gb",
                label="Memory GB",
                type=FieldType.DECIMAL,
                option_name="--memory-gb",
                description="Total RAM to size against. Defaults to detected host memory.",
            ),
            FieldSpec(
                key="max_connections",
                label="Max connections",
                type=FieldType.INTEGER,
                default=100,
                option_name="--max-connections",
            ),
            _STORAGE_FIELD,
        ),
        input_model=DatabaseTuneEnvRequest,
        result_model=DatabaseOperationResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_DATABASE_TUNE_ENV,
        result_renderer_ref=_TOKEN_DATABASE_TUNE_ENV,
    ),
    CommandSpec(
        command_id=_TOKEN_DATABASE_RESTORE_PARITY_CAPTURE,
        compact_support=CompactSupport.PLATFORM,
        path=("database", "restore-parity-capture"),
        summary="Capture restore-parity metadata.",
        description=(
            "Capture parity metadata for backup restore drills; this does not restore "
            "backups or migrate storage."
        ),
        examples=(
            "flow-steward database restore-parity-capture --output backups/restore/baseline.json",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            _DSN_SECRET_FIELD,
            FieldSpec(
                key="label",
                label="Label",
                type=FieldType.TEXT,
                default="baseline",
                option_name="--label",
            ),
            FieldSpec(
                key="output",
                label="Output",
                type=FieldType.PATH,
                required=True,
                option_name="--output",
            ),
        ),
        input_model=DatabaseRestoreParityCaptureRequest,
        result_model=DatabaseOperationResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_DATABASE_RESTORE_PARITY_CAPTURE,
        result_renderer_ref=_TOKEN_DATABASE_RESTORE_PARITY_CAPTURE,
    ),
    CommandSpec(
        command_id=_TOKEN_DATABASE_RESTORE_PARITY_VERIFY,
        compact_support=CompactSupport.PLATFORM,
        path=("database", "restore-parity-verify"),
        summary="Verify restore-parity metadata.",
        description=(
            "Compare current parity metadata with a captured baseline; this does not "
            "perform restore orchestration."
        ),
        examples=(
            "flow-steward database restore-parity-verify --baseline backups/restore/baseline.json",
        ),
        operation_type=OperationType.CHANGE,
        interactive_policy=InteractivePolicy.ALWAYS_REVIEW,
        fields=(
            _DSN_SECRET_FIELD,
            FieldSpec(
                key="label",
                label="Label",
                type=FieldType.TEXT,
                default="restored",
                option_name="--label",
            ),
            FieldSpec(
                key="baseline",
                label="Baseline",
                type=FieldType.PATH,
                required=True,
                option_name="--baseline",
            ),
            FieldSpec(
                key="output",
                label="Output",
                type=FieldType.PATH,
                option_name="--output",
            ),
        ),
        input_model=DatabaseRestoreParityVerifyRequest,
        result_model=DatabaseOperationResult,
        plan_builder_ref=None,
        confirmation_policy_ref=None,
        service_ref=_TOKEN_DATABASE_RESTORE_PARITY_VERIFY,
        result_renderer_ref=_TOKEN_DATABASE_RESTORE_PARITY_VERIFY,
    ),
)
