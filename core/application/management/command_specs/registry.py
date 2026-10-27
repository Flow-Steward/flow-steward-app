"""Closed aggregate for the first public command specifications."""

from __future__ import annotations

from core.application.management.command_specs.backup import BACKUP_COMMAND_SPECS
from core.application.management.command_specs.clone import CLONE_COMMAND_SPECS
from core.application.management.command_specs.config import CONFIG_COMMAND_SPECS
from core.application.management.command_specs.contracts import CommandSpec
from core.application.management.command_specs.database import DATABASE_COMMAND_SPECS
from core.application.management.command_specs.environment import ENVIRONMENT_COMMAND_SPECS
from core.application.management.command_specs.extensions import EXTENSION_COMMAND_SPECS
from core.application.management.command_specs.field_metadata import apply_field_metadata
from core.application.management.command_specs.jobs import JOB_COMMAND_SPECS
from core.application.management.command_specs.localization import LOCALIZATION_COMMAND_SPECS
from core.application.management.command_specs.maintenance import MAINTENANCE_COMMAND_SPECS
from core.application.management.command_specs.projects import PROJECT_COMMAND_SPECS
from core.application.management.command_specs.proxy import PROXY_COMMAND_SPECS
from core.application.management.command_specs.quality import QUALITY_COMMAND_SPECS
from core.application.management.command_specs.readiness import READINESS_COMMAND_SPECS
from core.application.management.command_specs.release import RELEASE_COMMAND_SPECS
from core.application.management.command_specs.reports import REPORT_COMMAND_SPECS
from core.application.management.command_specs.stack import STACK_COMMAND_SPECS
from core.application.management.command_specs.testing import TESTING_COMMAND_SPECS
from core.application.management.command_specs.users import USER_COMMAND_SPECS
from core.application.management.command_specs.workflows import WORKFLOW_COMMAND_SPECS

PUBLIC_COMMAND_SPECS: tuple[CommandSpec, ...] = apply_field_metadata(
    (
        *READINESS_COMMAND_SPECS,
        *PROXY_COMMAND_SPECS,
        *STACK_COMMAND_SPECS,
        *ENVIRONMENT_COMMAND_SPECS,
        *MAINTENANCE_COMMAND_SPECS,
        *RELEASE_COMMAND_SPECS,
        *BACKUP_COMMAND_SPECS,
        *CLONE_COMMAND_SPECS,
        *CONFIG_COMMAND_SPECS,
        *USER_COMMAND_SPECS,
        *EXTENSION_COMMAND_SPECS,
        *PROJECT_COMMAND_SPECS,
        *WORKFLOW_COMMAND_SPECS,
        *JOB_COMMAND_SPECS,
        *REPORT_COMMAND_SPECS,
        *DATABASE_COMMAND_SPECS,
        *TESTING_COMMAND_SPECS,
        *QUALITY_COMMAND_SPECS,
        *LOCALIZATION_COMMAND_SPECS,
    )
)

ALLOWED_SERVICE_REFS = frozenset(spec.service_ref for spec in PUBLIC_COMMAND_SPECS)
ALLOWED_CHOICE_PROVIDER_REFS = frozenset(
    {"accounts.available", "projects.available", "stack.log-services"}
)
ALLOWED_PLAN_BUILDER_REFS = frozenset(
    {
        "extensions.uninstall.preview",
        "stack.mutation.preview",
        "config.apply.preview",
        "environment.switch.preview",
        "release.upgrade.preview",
        "clone.restore.preview",
    }
)
ALLOWED_CONFIRMATION_POLICY_REFS = frozenset(
    {
        "match:extension_id",
        "match:project_id,repo_root",
        "match:target_installation_id,target_profile,public_host,plan_digest",
    }
)
ALLOWED_RESULT_RENDERER_REFS = frozenset(spec.result_renderer_ref for spec in PUBLIC_COMMAND_SPECS)


def get_command_spec(path: tuple[str, ...]) -> CommandSpec:
    """Return one static public command specification by its canonical path."""
    for spec in PUBLIC_COMMAND_SPECS:
        if spec.path == path:
            return spec
    raise KeyError("Unknown public command path: " + ".".join(path))


def complete_operator_command(command: str) -> str:
    """An operator command as it has to be typed: ./flow-steward, and --confirm exactly
    when the command reviews before it acts.

    W1's "Continue it with" appended --confirm to every recovery command, and told an
    operator to run `backup create --confirm`, which has no such option (AC17-R162).
    Anything that is not a flow-steward command is returned unchanged.
    """
    words = command.strip().split()
    if not words or words[0] not in {"flow-steward", "./flow-steward"}:
        return command
    words[0] = "./flow-steward"
    positional = tuple(word for word in words[1:] if not word.startswith("-"))
    for length in range(len(positional), 0, -1):
        try:
            spec = get_command_spec(positional[:length])
        except KeyError:
            continue
        if spec.interactive_policy.name == "ALWAYS_REVIEW" and "--confirm" not in words:
            words.append("--confirm")
        break
    return " ".join(words)
