"""Private no-subcommand controller invoked only by ``./install.sh``."""

from __future__ import annotations

import json
import os
import platform as host_platform
import shutil
import signal
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click

from core.application.installer import (
    InstallationKind,
    InstallConfig,
    InstallRunResult,
    ReleaseIdentity,
)
from core.application.installer.errors import InstallerFailure
from core.application.installer.redaction import redact_installer_text
from core.application.installer.service import InstallerService
from core.application.installer.spec import INSTALLER_SPEC
from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.stack_service import StackGatewayError
from core.application.maintenance.compact_client import CompactMaintenanceClient
from core.application.management.command_specs.contracts import (
    CLIErrorDetail,
    ExecutionResult,
    ExitCode,
    FieldSpec,
    FieldType,
    OperationProgressEvent,
    OperationStepStatus,
)
from core.application.readiness import CoreReadinessService
from core.application.readiness.contracts import ReadinessReport
from core.application.release.catalog import GitHubReleaseDiscovery
from core.application.release.checkout_release import published_release_for_checkout
from core.application.release.environment import release_runtime_environment
from core.application.release.service import (
    OfficialReleaseSelector,
    ReleaseInstallRequest,
    release_platform_for_machine,
)
from core.entrypoints.cli.platform.modes import OutputMode, TerminalFacts, select_mode
from core.entrypoints.cli.platform.renderers import (
    JSONRenderer,
    PlainRenderer,
    RichRenderer,
)
from core.infrastructure.config.installer_config import installer_state_root
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.installer.gateway import DockerInstallerGateway
from core.infrastructure.installer.registry_image_resolver import OfficialReleaseResolver
from core.infrastructure.installer.release_handoff import InstallerReleaseHandoff
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.lifecycle.configuration_store import LifecycleConfigurationStore
from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway
from core.infrastructure.lifecycle.state_store import LifecycleStateStore
from core.infrastructure.release.github import GitHubJSONFetcher
from core.infrastructure.release.github_credentials import GitHubReleaseCredentialLoader
from core.infrastructure.release.local_checkout import LocalCheckoutReleaseSource

_ROOT = Path(__file__).resolve().parents[3]
_STATE_DIRECTORY_NAME = ".flow-steward"


def _field_option(field: FieldSpec) -> click.Option:
    option_name = field.option_name or f"--{field.key.replace('_', '-')}"
    kwargs: dict[str, Any] = {"default": None, "help": field.description}
    if field.type in {FieldType.SINGLE_SELECT, FieldType.MULTI_SELECT}:
        kwargs["type"] = click.Choice([choice.value for choice in field.choices])
        kwargs["multiple"] = field.type is FieldType.MULTI_SELECT
    elif field.type is FieldType.INTEGER:
        kwargs["type"] = int
    elif field.type is FieldType.PATH:
        kwargs["type"] = click.Path(path_type=str)
    elif field.type is FieldType.BOOLEAN:
        kwargs["is_flag"] = True
    else:
        kwargs["type"] = str
    # The field key, not the option name, is the value's name: `--setting` fills
    # operator_settings.
    return click.Option([option_name, field.key], **kwargs)


def _params() -> list[click.Parameter]:
    return [
        click.Option(["--json"], is_flag=True, help="Emit one JSON document."),
        click.Option(["--plain"], is_flag=True, help="Use line-oriented output (default)."),
        click.Option(["--tui"], is_flag=True, help="Opt in to full-screen forms and rich output."),
        click.Option(["--no-color"], is_flag=True, help="Disable color."),
        click.Option(["--verbose"], is_flag=True, help="Stream redacted technical output."),
        click.Option(["--non-interactive"], is_flag=True, help="Never prompt."),
        click.Option(["--confirm"], is_flag=True, help="Confirm the reviewed installation plan."),
        *(_field_option(field) for field in INSTALLER_SPEC.fields),
    ]


def _callback(**values: Any) -> None:
    json_requested = bool(values.pop("json"))
    plain_requested = bool(values.pop("plain"))
    tui_requested = bool(values.pop("tui"))
    no_color = bool(values.pop("no_color"))
    verbose = bool(values.pop("verbose"))
    non_interactive = bool(values.pop("non_interactive")) or json_requested
    confirmed = bool(values.pop("confirm"))
    terminal = _terminal_facts()
    decision = select_mode(
        terminal=terminal,
        json_requested=json_requested,
        plain_requested=plain_requested,
        tui_requested=tui_requested,
        non_interactive=non_interactive,
        no_color_requested=no_color,
        textual_eligible=True,
    )
    non_interactive = non_interactive or not decision.can_prompt
    renderer = _installer_renderer(
        json_requested=json_requested,
        plain_requested=not tui_requested
        or plain_requested
        or decision.output_mode is OutputMode.PLAIN,
        color_enabled=decision.color_enabled,
        animation_enabled=decision.animation_enabled,
    )
    try:
        service = _create_service(stream_output=verbose or not json_requested)
        classification = service.inspect()
    except Exception as exc:
        log_path = _write_emergency_log(exc)
        _render_failure(
            renderer,
            "installer_initialization_failed",
            f"{type(exc).__name__}: {exc}",
            ExitCode.INTERNAL,
            hint=f"Correct filesystem/runtime access and rerun ./install.sh. Log: {log_path}",
            retryable=False,
            log_path=log_path,
        )
    supplied = {key: value for key, value in values.items() if value not in (None, ())}
    explicit_fields = frozenset(supplied)
    needs_review = classification.kind is InstallationKind.FRESH
    if not needs_review:
        # Resume the installation that is actually on disk rather than the spec's
        # defaults; explicit command-line values still override it.
        supplied = _recorded_installation_values(_ROOT) | supplied
    # Guided answers are not host paths pre-mounted by the bootstrap launcher.
    path_supplied_keys = set(supplied)
    if not needs_review or non_interactive:
        normalized = _with_defaults(supplied, supplied_keys=set(supplied))
    else:
        normalized, form_confirmed = _collect_interactive(
            supplied,
            renderer=renderer,
            decision=decision,
            terminal=terminal,
        )
        confirmed = confirmed or form_confirmed
    try:
        _validate_container_paths(normalized, supplied_keys=path_supplied_keys)
        config = InstallConfig.model_validate(normalized)
    except Exception as exc:
        _render_failure(renderer, "validation_failed", str(exc), ExitCode.VALIDATION)
    if needs_review and not confirmed:
        if decision.can_prompt and renderer.confirm_review(INSTALLER_SPEC, config.model_dump()):
            confirmed = True
        else:
            _render_failure(
                renderer,
                "cancelled",
                # AC17-R78: naming the flag is the whole message. A non-interactive caller
                # reading "requires explicit confirmation" has no way to know what to add.
                "Installation requires explicit confirmation. Rerun with --confirm, or run "
                "interactively to confirm at the prompt.",
                ExitCode.INTERRUPTED,
            )
    assert confirmed or not needs_review

    target_release: ReleaseIdentity | None = None
    handoff_result: InstallRunResult | None = None
    if config.environment == "production":
        try:
            target_release = _prepare_official_release(config, discover=needs_review)
            if target_release is not None and not needs_review:
                release_state = InstallerStateStore(
                    installer_state_root(_ROOT),
                    lifecycle_state_root=_ROOT / _STATE_DIRECTORY_NAME,
                )
                if (
                    release_state.load_installed_release() is None
                    and release_state.load_release_handoff() is not None
                ):
                    handoff_result = InstallerReleaseHandoff(
                        repo_root=_ROOT,
                        state=release_state,
                        source_repository=str(
                            _runtime_env.get(
                                "FS_RELEASE_REPOSITORY", "Flow-Steward/flow-steward-app"
                            )
                        ),
                        source_registries=str(_runtime_env.get("FS_RELEASE_REGISTRIES") or ""),
                    ).install_exact(
                        target_release,
                        config=config,
                        explicit_fields=explicit_fields,
                        timeout_seconds=3600,
                    )
            if handoff_result is None:
                # The first service was read-only classification. Recreate it after exact
                # image resolution so its gateway sees only the verified release inputs.
                service = _create_service(stream_output=verbose or not json_requested)
        except LifecycleError as exc:
            _render_failure(
                renderer,
                exc.code,
                exc.cause,
                exc.exit_code,
                hint=exc.remediation,
                retryable=exc.retryable,
                log_path=exc.log_path,
            )
        except Exception as exc:
            _render_failure(
                renderer,
                "official_release_unavailable",
                str(exc),
                ExitCode.DEPENDENCY,
                hint="Restore GitHub/registry access and rerun the same installation.",
                retryable=True,
            )

    if handoff_result is not None:
        result = handoff_result
    else:
        if config.environment == "production" and not json_requested:
            _announce_install_mode(target_release)
        with renderer.operation_progress() as progress:
            try:
                service.progress_sink = lambda step_id, status, detail: progress(
                    OperationProgressEvent(
                        step_id=step_id,
                        status=_progress_status(status),
                        label=detail,
                        detail=detail,
                    )
                )
                result = service.install(
                    config,
                    explicit_fields=explicit_fields,
                    target_release=target_release,
                )
            except InstallerFailure as exc:
                _render_failure(
                    renderer,
                    exc.code,
                    exc.cause,
                    ExitCode(exc.exit_code),
                    hint=f"{exc.remediation} Log: {exc.log_path or '(not created)'}",
                    retryable=exc.retryable,
                    log_path=exc.log_path,
                )
            except Exception as exc:
                log_path = _write_emergency_log(exc)
                _render_failure(
                    renderer,
                    "installer_initialization_failed",
                    f"{type(exc).__name__}: {exc}",
                    ExitCode.INTERNAL,
                    hint=f"Correct filesystem/runtime access and rerun ./install.sh. Log: {log_path}",
                    retryable=False,
                    log_path=log_path,
                )
    if result.status == "already_installed" and not json_requested:
        _render_already_installed(result)
        _announce_changes(result)
        # AC17-R50: this path re-issues an expired setup token, and used to return here
        # without ever printing the replacement. The operator was told "Flow Steward is
        # already installed" and nothing else, while the link they were holding had just
        # been invalidated -- a dead end replaced by silence. The announcement is a no-op
        # unless bootstrap is still required and a live token exists.
        _announce_setup_link(result)
        return
    execution = ExecutionResult(
        result=result,
        public_result=result.model_dump(mode="json"),
        operation_id=result.operation_id,
        log_path=result.log_path,
    )
    renderer.render_result(INSTALLER_SPEC, execution)
    if not json_requested:
        _announce_update(result)
        _announce_changes(result)
    _announce_setup_link(result)


def _announce_update(result: InstallRunResult) -> None:
    """Say in a sentence that the running version changed, and from what to what.

    AC17-R141: the defect was an update reported and not applied with nothing on the
    screen to say otherwise, so the opposite has to be said plainly too. The fields
    are in the JSON result for machines; this is the line an operator reads.
    """
    if not result.updated_to_revision:
        return
    click.echo()
    if not result.updated_from_revision:
        click.echo(
            f"Updated to {result.updated_to_revision}; the runtime was not running, so the "
            "revision it replaced is not known."
        )
        return
    click.echo(f"Updated from {result.updated_from_revision} to {result.updated_to_revision}.")


def _announce_changes(result: InstallRunResult) -> None:
    """R157/R159 acceptance: what the run changed was written to the log only."""
    for change in result.changes:
        click.echo(change)


def _announce_setup_link(result: InstallRunResult) -> None:
    """Put the one-time setup link on stderr, never in the serialized result.

    The operator needs a link that works by following installer output alone, but
    `./install.sh --json > result.json` must not capture a live setup token. stderr
    reaches the terminal in both output modes and is not captured by that redirect.
    """
    if not result.bootstrap_setup_url:
        return
    click.echo(
        "\n".join(
            (
                "",
                "=" * 60,
                "NEXT STEP: CREATE THE FIRST ADMINISTRATOR",
                "",
                "Open this link in your browser:",
                "",
                result.bootstrap_setup_url,
                "",
                "This one-time link contains a secret setup token.",
                "Do not share it.",
                "=" * 60,
                "",
            )
        ),
        err=True,
    )


def _render_already_installed(result: InstallRunResult) -> None:
    click.echo("Flow Steward is already installed.")
    click.echo()
    # AC17-R162: "the running images were built from another revision, and left in
    # place" reached the JSON only, and a rollback that did nothing read as a success.
    for warning in result.warnings:
        click.echo(f"Warning: {warning}")
    if result.warnings:
        click.echo()
    if result.bootstrap_instruction:
        click.echo(result.bootstrap_instruction)
        # AC17-R93: the serialized result carried bootstrap_url while the human-readable
        # render printed only the prose, so an instruction ending "open the resulting URL"
        # never showed which URL. The token-bearing link is announced separately on
        # stderr; this bare URL holds no secret. Printing both would be redundant.
        if result.bootstrap_url and not result.bootstrap_setup_url:
            click.echo(f"  {result.bootstrap_url}")
        click.echo()
    click.echo("Manage this installation with:")
    click.echo("  ./flow-steward status")
    click.echo("  ./flow-steward doctor")
    if result.log_path:
        click.echo(f"Log: {result.log_path}")


def _installer_renderer(
    *, json_requested: bool, plain_requested: bool, color_enabled: bool, animation_enabled: bool
) -> Any:
    if json_requested:
        return JSONRenderer()
    if plain_requested:
        return PlainRenderer()
    return RichRenderer(color_enabled=color_enabled, animation_enabled=animation_enabled)


installer_command = click.Command(
    name="install.sh",
    help=INSTALLER_SPEC.description,
    callback=_callback,
    params=_params(),
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _collect_interactive(
    supplied: dict[str, Any], *, renderer: Any, decision: Any, terminal: TerminalFacts
) -> tuple[dict[str, Any], bool]:
    if decision.output_mode is OutputMode.PLAIN:
        values = renderer.collect_missing(INSTALLER_SPEC.fields, supplied, include_optional=True)
        return _with_defaults(values, supplied_keys=set(supplied)), False
    from core.entrypoints.cli.tui.form_app import CommandFormApp, FormOutcome

    def choices(_provider: str) -> tuple[Any, ...]:
        return ()

    outcome = CommandFormApp(
        spec=INSTALLER_SPEC,
        initial_values=supplied,
        choice_loader=choices,
        terminal_size=(terminal.columns, terminal.rows),
        color_enabled=decision.color_enabled,
        animation_enabled=decision.animation_enabled,
    ).run() or FormOutcome(status="cancelled")
    if outcome.status == "plain_fallback":
        values = renderer.collect_missing(INSTALLER_SPEC.fields, supplied, include_optional=True)
        return _with_defaults(values, supplied_keys=set(supplied)), False
    if outcome.status != "confirmed":
        _render_failure(renderer, "cancelled", "Installation cancelled.", ExitCode.INTERRUPTED)
    return _with_defaults(outcome.values, supplied_keys=set(supplied)), True


def _recorded_installation_values(root: Path) -> dict[str, Any]:
    """Recover the configuration an existing installation was installed with.

    AC17-R93: on a non-fresh installation the installer skipped the review form and
    fell straight through to the spec defaults, so a bare rerun silently proposed
    `scalable`/`development` for an installation recorded as `compact`/`production`
    and then refused its own combination as invalid. That made the R88 remediation
    ("Run ./install.sh instead") impossible to follow literally.

    Prefer the canonical active deployment record so a bare rerun cannot reset ports,
    ingress, storage, or TLS to installer defaults. Anything supplied on the command
    line still wins over the recorded values.
    """
    try:
        active = LifecycleConfigurationStore(
            root,
            state_root=installer_state_root(root),
        ).read_active()
    except (OSError, ValueError):
        active = None
    if active is not None:
        return active.model_dump(mode="python")
    for name, environment in ((".env.prod", "production"), (".env", "development")):
        path = root / name
        if not path.is_file():
            continue
        recorded: dict[str, Any] = {"environment": environment}
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        for line in content.splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key == "FS_DEPLOYMENT_PROFILE":
                profile = value.strip().strip('"').strip("'")
                if profile in {"compact", "scalable"}:
                    recorded["deployment_profile"] = profile
                break
        return recorded
    return {}


def _with_defaults(
    values: dict[str, Any], *, supplied_keys: set[str] | None = None
) -> dict[str, Any]:
    normalized = dict(values)
    explicit = supplied_keys if supplied_keys is not None else set(normalized)
    file_storage_supplied = "file_storage" in (explicit)
    if "tls_mode" in explicit and "ingress_mode" not in explicit:
        normalized["ingress_mode"] = {
            "reverse_proxy": "external_proxy",
            "external_resolver": "external_traefik",
            "external_certificate": "external_traefik",
            "platform_managed": "platform",
        }.get(str(normalized.get("tls_mode")), "bundled_traefik")
    for field in INSTALLER_SPEC.fields:
        if field.key not in normalized and field.default is not None:
            default = field.default
            normalized[field.key] = list(default) if isinstance(default, tuple) else default
    # The spec default "bundled" is the Scalable default; Compact defaults to the local
    # filesystem. Only that default is replaced: an answered local_fs or external_s3
    # (with its S3 settings) is kept, and --file-storage bundled stays explicit.
    if (
        normalized.get("deployment_profile") == "compact"
        and not file_storage_supplied
        and normalized.get("file_storage") == "bundled"
    ):
        normalized["file_storage"] = "local_fs"
    if normalized.get("environment") == "production":
        normalized["development_tools"] = ()
    return normalized


def _validate_container_paths(values: dict[str, Any], *, supplied_keys: set[str]) -> None:
    tls_mode = str(values.get("tls_mode") or "local")
    for key in ("certificate_path", "private_key_path"):
        raw = str(values.get(key) or "")
        if not raw:
            continue
        raw_path = Path(raw).expanduser()
        resolved = raw_path if raw_path.is_absolute() else _ROOT / raw_path
        outside_repo = not resolved.resolve().is_relative_to(_ROOT)
        if outside_repo and not raw_path.is_absolute():
            raise ValueError(
                "Relative installer file paths must stay inside the repository; use an absolute "
                "./install.sh option for an external input file."
            )
        if tls_mode != "provided" and outside_repo:
            raise ValueError("Generated TLS output paths must stay inside the repository.")
        if tls_mode == "provided" and key not in supplied_keys and outside_repo:
            raise ValueError(
                "Guided provided-TLS files must be under the repository; "
                "pass external absolute paths as ./install.sh options so they can be mounted read-only."
            )
    credentials = str(values.get("s3_credentials_path") or "")
    if values.get("file_storage") == "external_s3" and credentials:
        raw_path = Path(credentials).expanduser()
        resolved = raw_path if raw_path.is_absolute() else _ROOT / raw_path
        outside_repo = not resolved.resolve().is_relative_to(_ROOT)
        if outside_repo and not raw_path.is_absolute():
            raise ValueError(
                "Relative installer file paths must stay inside the repository; use an absolute "
                "./install.sh option for an external input file."
            )
        if "s3_credentials_path" not in supplied_keys and outside_repo:
            raise ValueError(
                "Guided S3 credentials must be under the repository; pass an external absolute "
                "path as a ./install.sh option so it can be mounted read-only."
            )


def _create_service(*, progress_sink: Any = None, stream_output: bool = False) -> InstallerService:
    state = InstallerStateStore(
        installer_state_root(_ROOT), lifecycle_state_root=_ROOT / _STATE_DIRECTORY_NAME
    )
    configuration_service = ConfigurationService(
        store=LifecycleConfigurationStore(_ROOT, state_root=state.root)
    )
    technical_output_sink: Callable[[str], None] | None = None
    if stream_output:

        def emit_technical_output(line: str) -> None:
            click.echo(redact_installer_text(line), err=True)

        technical_output_sink = emit_technical_output
    gateway = DockerInstallerGateway(
        repo_root=_ROOT,
        state_store=state,
        technical_output_sink=technical_output_sink,
        configuration_service=configuration_service,
    )

    def installer_readiness(timeout_seconds: float = 60.0) -> dict[str, Any]:
        active = configuration_service.read_active(required=False)
        if active is None:
            return {}
        return gateway.probe_cli_readiness(active, timeout_seconds=timeout_seconds)

    def bounded_readiness(timeout_seconds: float) -> ReadinessReport:
        return CoreReadinessService(probe=lambda: installer_readiness(timeout_seconds)).check()

    return InstallerService(
        gateway=gateway,
        state=state,
        project_identity=str(_runtime_env.get("FS_COMPOSE_PROJECT_NAME") or "flow-steward"),
        progress_sink=progress_sink,
        readiness_service=CoreReadinessService(
            probe=installer_readiness,
        ),
        readiness_reprobe=bounded_readiness,
    )


def _prepare_official_release(
    config: InstallConfig, *, discover: bool = True
) -> ReleaseIdentity | None:
    """Choose official images or a source build for this installation.

    Official handoffs and identities stay official. Without installer state, a fresh
    install of a clean checkout exactly at its published ``v<VERSION>`` tag installs
    that release's official images; any other checkout builds local sources.
    """
    state = InstallerStateStore(installer_state_root(_ROOT))
    journal = state.load_release_handoff()
    if (
        journal is None
        and state.load_installed_release() is None
        and state.load_pending_release() is None
    ):
        return _prepare_fresh_install(config, state=state, discover=discover)
    _runtime_env["FS_INSTALL_MODE"] = "official"
    if journal is not None:
        if journal.release_repository:
            _runtime_env["FS_RELEASE_REPOSITORY"] = journal.release_repository
        if journal.release_registries:
            _runtime_env["FS_RELEASE_REGISTRIES"] = journal.release_registries
    repository = str(_runtime_env.get("FS_RELEASE_REPOSITORY", "Flow-Steward/flow-steward-app"))
    resolver = OfficialReleaseResolver()
    platform = release_platform_for_machine(host_platform.machine())
    if discover:
        coordinates = LocalCheckoutReleaseSource(
            repo_root=_ROOT,
            repository=repository,
        ).exact()
        release = resolver.resolve_coordinates(
            coordinates,
            profile=config.deployment_profile,
            platform=platform,
        )
    else:

        class NoRemoteDiscovery:
            def __init__(self, release_repository: str) -> None:
                self.repository = release_repository

            def latest(self) -> Any:
                raise AssertionError("install.sh must not discover a remote release")

            def for_version(self, _version: str) -> Any:
                raise AssertionError("install.sh must not discover a remote release")

        installed = state.load_installed_release()
        pending = state.load_pending_release()
        if installed is None and journal is None and pending is not None:
            coordinates = LocalCheckoutReleaseSource(
                repo_root=_ROOT,
                repository=repository,
            ).exact()
            if (
                str(coordinates.version),
                coordinates.tag,
                coordinates.commit_sha,
            ) != (
                pending.version,
                pending.tag,
                pending.commit_sha,
            ):
                raise LifecycleError.stable_failure(
                    code="production_checkout_pending_mismatch",
                    cause="The current checkout does not match the pending production release.",
                    remediation=(
                        f"Check out {pending.tag} at {pending.commit_sha}, then rerun ./install.sh."
                    ),
                    step="production_checkout",
                )
        release = OfficialReleaseSelector(
            discovery=NoRemoteDiscovery(repository),
            state=state,
            resolver=resolver,
            profile=config.deployment_profile,
            platform=platform,
        ).prepare_install(ReleaseInstallRequest(discover_latest=False))
    _runtime_env.update(release_runtime_environment(release))
    return release


def _prepare_fresh_install(
    config: InstallConfig, *, state: InstallerStateStore, discover: bool
) -> ReleaseIdentity | None:
    """Fresh install: a published release tag pulls its images, anything else builds."""
    repository = str(_runtime_env.get("FS_RELEASE_REPOSITORY", "Flow-Steward/flow-steward-app"))
    # A resumed source installation (no fresh review) keeps building its sources.
    checkout = (
        LocalCheckoutReleaseSource(repo_root=_ROOT, repository=repository).probe()
        if discover
        else None
    )
    if checkout is None:
        _runtime_env["FS_INSTALL_MODE"] = "source"
        return None
    _runtime_env["FS_INSTALL_MODE"] = "official"
    release = published_release_for_checkout(
        OfficialReleaseSelector(
            discovery=GitHubReleaseDiscovery(
                repository=checkout.repository,
                fetch_json=GitHubJSONFetcher(GitHubReleaseCredentialLoader(state.root).load).fetch,
            ),
            state=state,
            resolver=OfficialReleaseResolver(),
            profile=config.deployment_profile,
            platform=release_platform_for_machine(host_platform.machine()),
        ),
        checkout,
    )
    _runtime_env.update(release_runtime_environment(release))
    return release


def _announce_install_mode(target_release: ReleaseIdentity | None) -> None:
    """Say which images this run installs before the long preparation starts."""
    if target_release is None:
        click.echo("Flow Steward installer: building product images from this checkout.", err=True)
        return
    click.echo(
        f"Flow Steward installer: installing official release {target_release.tag} "
        f"from {target_release.registry} (no local product image build).",
        err=True,
    )


def _compact_maintenance_client(
    *,
    project_root: Path,
    state_root: Path,
    config: DeploymentConfig,
    docker_host_project_root: Path | None = None,
) -> CompactMaintenanceClient:
    configuration = ConfigurationService(
        store=LifecycleConfigurationStore(project_root, state_root=state_root),
    )
    target = configuration.derive_target(config)
    gateway = DockerLifecycleGateway(
        project_root=project_root, docker_host_project_root=docker_host_project_root
    )
    return CompactMaintenanceClient(
        lambda operation: gateway.compact_maintenance_operation(target, operation),
        mutation_state=LifecycleStateStore(
            project_root, installation_lock_path=state_root / "install.lock"
        ),
    )


def run_pending_release_handoff() -> int:
    """Private stable-bootstrap target; never exposed as a public CLI mutation."""
    state = InstallerStateStore(
        installer_state_root(_ROOT), lifecycle_state_root=_ROOT / _STATE_DIRECTORY_NAME
    )
    pending = state.load_pending_release()
    journal = state.load_release_handoff()
    if pending is None:
        click.echo(
            json.dumps(
                {
                    "error": {
                        "code": "release_pending_state_missing",
                        "message": "The private release handoff has no pending target.",
                    }
                },
                sort_keys=True,
            )
        )
        return int(ExitCode.STABLE_FAILURE)
    if journal is None or journal.target != pending or journal.phase != "checkout_applied":
        click.echo(
            json.dumps(
                {
                    "error": {
                        "code": "release_handoff_state_mismatch",
                        "message": "The private release handoff journal does not match the pending target.",
                    }
                },
                sort_keys=True,
            )
        )
        return int(ExitCode.STABLE_FAILURE)
    installed = state.load_installed_release()
    if (installed is not None and installed != pending) or (
        state.load_release_transition_binding() is not None
    ):
        active = LifecycleConfigurationStore(_ROOT, state_root=state.root).read_active()
        try:
            if pending.profile == "compact":
                if active is None or active.deployment_profile != "compact":
                    raise ValueError("Installed Compact configuration is unavailable")
                host_root = str(_runtime_env.get("FS_DOCKER_HOST_PROJECT_ROOT") or "").strip()
                maintenance_enabled = (
                    _compact_maintenance_client(
                        project_root=_ROOT,
                        state_root=state.root,
                        config=active,
                        docker_host_project_root=Path(host_root) if host_root else None,
                    )
                    .status()
                    .status
                    == "enabled"
                )
            else:
                maintenance_path = state.root / "runtime/maintenance-state.json"
                maintenance = json.loads(maintenance_path.read_text(encoding="utf-8"))
                maintenance_enabled = (
                    isinstance(maintenance, dict) and maintenance.get("phase") == "enabled"
                )
        except (OSError, ValueError, StackGatewayError, LifecycleError):
            maintenance_enabled = False
        if not maintenance_enabled:
            click.echo(
                json.dumps(
                    {
                        "error": {
                            "code": "release_maintenance_required",
                            "message": "Release upgrade requires maintenance to remain enabled.",
                        }
                    },
                    sort_keys=True,
                )
            )
            return int(ExitCode.STABLE_FAILURE)
        if active is None or active.deployment_profile != pending.profile:
            click.echo(
                json.dumps(
                    {
                        "error": {
                            "code": "release_install_config_mismatch",
                            "message": "Active deployment profile does not match the pending release.",
                        }
                    },
                    sort_keys=True,
                )
            )
            return int(ExitCode.STABLE_FAILURE)
    recorded = (
        journal.install_config.model_dump(mode="python")
        if journal.install_config is not None
        else _recorded_installation_values(_ROOT)
    )
    recorded.setdefault("environment", "production")
    recorded.setdefault("deployment_profile", pending.profile)
    try:
        config = InstallConfig.model_validate(_with_defaults(recorded, supplied_keys=set(recorded)))
    except Exception as exc:
        click.echo(
            json.dumps(
                {
                    "error": {
                        "code": "release_install_config_invalid",
                        "message": redact_installer_text(str(exc)),
                    }
                },
                sort_keys=True,
            )
        )
        return int(ExitCode.STABLE_FAILURE)
    if config.deployment_profile != pending.profile or config.environment != "production":
        click.echo(
            json.dumps(
                {
                    "error": {
                        "code": "release_install_config_mismatch",
                        "message": "Recorded installation configuration does not match the pending release.",
                    }
                },
                sort_keys=True,
            )
        )
        return int(ExitCode.STABLE_FAILURE)
    try:
        result = _create_service(stream_output=False).install(
            config,
            explicit_fields=frozenset(journal.explicit_fields),
            target_release=pending,
        )
    except InstallerFailure as exc:
        click.echo(
            json.dumps(
                {
                    "error": {
                        "code": exc.code,
                        "message": redact_installer_text(exc.cause),
                        "hint": redact_installer_text(exc.remediation),
                        "retryable": exc.retryable,
                    },
                    "log_path": exc.log_path,
                },
                sort_keys=True,
            )
        )
        return int(exc.exit_code)
    if result.bootstrap_setup_url:
        state.save_release_handoff_secret(result.bootstrap_setup_url)
    click.echo(json.dumps({"result": result.model_dump(mode="json")}, sort_keys=True))
    return 0


def _render_failure(
    renderer: Any,
    code: str,
    message: str,
    exit_code: ExitCode,
    *,
    hint: str | None = None,
    retryable: bool = False,
    log_path: str | None = None,
) -> None:
    result = ExecutionResult(
        error=CLIErrorDetail(
            code=code,
            message=redact_installer_text(message),
            hint=redact_installer_text(hint) if hint else None,
            retryable=retryable,
        ),
        exit_code=exit_code,
        log_path=log_path,
    )
    renderer.render_result(INSTALLER_SPEC, result)
    raise click.exceptions.Exit(int(exit_code))


def _write_emergency_log(exc: Exception) -> str:
    fd, path = tempfile.mkstemp(prefix="flow-steward-installer-", suffix=".log")
    try:
        os.fchmod(fd, 0o600)
        message = redact_installer_text(f"{type(exc).__name__}: {exc}\n")
        os.write(fd, message.encode("utf-8", errors="replace"))
    finally:
        os.close(fd)
    return path


def _progress_status(value: str) -> OperationStepStatus:
    return {
        "running": OperationStepStatus.RUNNING,
        "succeeded": OperationStepStatus.SUCCEEDED,
        "skipped": OperationStepStatus.SKIPPED,
        "failed": OperationStepStatus.FAILED,
    }.get(value, OperationStepStatus.RUNNING)


def _terminal_facts() -> TerminalFacts:
    size = shutil.get_terminal_size((80, 24))
    return TerminalFacts(
        stdin_isatty=sys.stdin.isatty(),
        stdout_isatty=sys.stdout.isatty(),
        columns=size.columns,
        rows=size.lines,
        term=str(_runtime_env.get("TERM") or ""),
        no_color_env="NO_COLOR" in _runtime_env,
    )


def main(argv: list[str] | None = None) -> int:
    with _installer_signal_handlers():
        try:
            installer_command.main(
                args=list(sys.argv[1:] if argv is None else argv),
                prog_name="install.sh",
                standalone_mode=True,
            )
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else 0
    return 0


@contextmanager
def _installer_signal_handlers():
    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    previous = {
        process_signal: signal.getsignal(process_signal)
        for process_signal in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        for process_signal in previous:
            signal.signal(process_signal, interrupt)
        yield
    finally:
        for process_signal, handler in previous.items():
            signal.signal(process_signal, handler)


if __name__ == "__main__":
    raise SystemExit(main())
