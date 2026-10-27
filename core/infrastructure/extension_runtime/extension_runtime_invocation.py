# ruff: noqa: F403, F405, RUF022
# mypy: ignore-errors
from __future__ import annotations

import contextlib

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.config.size_limits import clamp_size_limit_bytes
from core.infrastructure.extension_runtime.progress_channel import (
    PROGRESS_FD_ENV,
    ProgressSink,
    close_quietly,
    current_progress_sink,
    drain_progress,
)

from .extension_runtime_common import *  # NOSONAR
from .extension_runtime_manifest import (
    _normalize_command,
    _normalize_entrypoint,
    _normalize_healthcheck,
)
from .extension_runtime_validation import _as_dict
from .process_confinement import (
    confined_command,
    kill_marked_processes,
    kill_process_group,
    new_run_marker,
    remove_scratch,
    run_environment,
    scratch_directory,
)

# Cap extension stdout/stderr while it is being read so a buggy or hostile
# extension cannot stream unbounded output into host memory. Enforced *during*
# capture (the child is killed on overflow), not after a full in-memory buffer.
# Operator-tunable via env (read once at import; clamped to a safe range).
_MAX_EXTENSION_OUTPUT_BYTES = clamp_size_limit_bytes(
    _runtime_env.get("FS_EXTENSION_OUTPUT_MAX_BYTES"),
    default=5 * 1024 * 1024,  # 5 MiB
    minimum=64 * 1024,  # 64 KiB
    maximum=256 * 1024 * 1024,  # 256 MiB
)
_DEFAULT_ENTRYPOINT_TIMEOUT_SECONDS = 30
_MAX_ENTRYPOINT_TIMEOUT_SECONDS = 600
_MAX_POLICY_ENTRYPOINT_TIMEOUT_SECONDS = 7200
_CANONICAL_TIMEOUT_POLICY = {
    "env": "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
    "default_seconds": 300,
    "minimum_seconds": 30,
    "maximum_seconds": 3600,
    "headroom_seconds": 30,
}


def extension_runtime_availability_failure(extension: dict | None) -> dict[str, Any] | None:
    """Return a typed fail-closed envelope for removed or disabled runtimes."""
    if not isinstance(extension, dict) or not extension:
        return {
            "status": "failed",
            "detail": "extension_unavailable",
            "response": {"definitely_no_external_effect": True},
            "errors": [
                {
                    "code": "extension_unavailable",
                    "message": "This extension is unavailable—reinstall it or replace this step.",
                }
            ],
        }
    if extension.get("enabled") is False:
        return {
            "status": "failed",
            "detail": "extension_disabled",
            "response": {"definitely_no_external_effect": True},
            "errors": [
                {
                    "code": "extension_disabled",
                    "message": "This extension is disabled—ask an admin to enable it.",
                }
            ],
        }
    declared_contract = _declared_host_contract(extension)
    if declared_contract != HOST_CONTRACT_VERSION:
        # A release built for another host contract is never invoked "to try":
        # its envelopes would not match what the runtime expects.
        return {
            "status": "failed",
            "detail": "extension_contract_incompatible",
            "response": {"definitely_no_external_effect": True},
            "errors": [
                {
                    "code": "extension_contract_incompatible",
                    "message": (
                        f"This extension was built for host contract '{declared_contract}'; "
                        f"this Flow Steward speaks '{HOST_CONTRACT_VERSION}'. "
                        "Install a release built for it."
                    ),
                }
            ],
        }
    return None


def _declared_host_contract(extension: dict) -> str:
    """The host contract a release declares in ``runtime.contract_version``, if any."""
    for source in (
        extension,
        _as_dict(extension.get("compiled_manifest")),
        _as_dict(extension.get("manifest")),
    ):
        declared = str(_as_dict(source.get("runtime")).get("contract_version") or "").strip()
        if declared:
            return declared
    return ""


def _clamp_entrypoint_timeout(value, *, default: int = _DEFAULT_ENTRYPOINT_TIMEOUT_SECONDS) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = default
    return max(1, min(timeout, _MAX_ENTRYPOINT_TIMEOUT_SECONDS))


def _resolved_entrypoint_timeout(entrypoint: dict, explicit_timeout: float | None) -> int:
    policy = _as_dict(entrypoint.get("timeout_policy"))
    if policy:
        try:
            env_name = str(policy.get("env") or "").strip()
            default = int(policy.get("default_seconds"))
            minimum = int(policy.get("minimum_seconds"))
            maximum = int(policy.get("maximum_seconds"))
            headroom = int(policy.get("headroom_seconds"))
        except (TypeError, ValueError):
            env_name, default, minimum, maximum, headroom = "", 0, 0, 0, 0
        parsed_policy = {
            "env": env_name,
            "default_seconds": default,
            "minimum_seconds": minimum,
            "maximum_seconds": maximum,
            "headroom_seconds": headroom,
        }
        if (
            set(policy) == set(_CANONICAL_TIMEOUT_POLICY)
            and parsed_policy == _CANONICAL_TIMEOUT_POLICY
        ):
            try:
                configured = int(
                    str(
                        _runtime_env.get("FS_EXTENSION_OPERATION_TIMEOUT_SECONDS") or default
                    ).strip()
                )
            except (TypeError, ValueError):
                configured = default
            operation_timeout = max(minimum, min(configured, maximum))
            return min(
                operation_timeout + headroom,
                _MAX_POLICY_ENTRYPOINT_TIMEOUT_SECONDS,
            )
    if explicit_timeout is not None:
        return _clamp_entrypoint_timeout(explicit_timeout)
    return _clamp_entrypoint_timeout(entrypoint.get("timeout_seconds"))


def _extension_command_validation_detail(
    *,
    bundle_root: str,
    command: list[str],
    field_path: str,
) -> str:
    from core.infrastructure.extension_runtime.boundary import (
        validate_extension_runtime_command,
    )

    issues = validate_extension_runtime_command(
        bundle_root=bundle_root,
        command=command,
        field_path=field_path,
    )
    return "; ".join(
        str(issue.get("message") or "").strip()
        for issue in issues
        if str(issue.get("message") or "").strip()
    )


def _is_structured_extension_error(response: dict[str, Any]) -> bool:
    """Return whether a child reported a domain error through the public envelope."""
    if response.get("ok") is not False:
        return False
    error_code = str(response.get("error_code") or "").strip()
    error_message = str(response.get("error") or "").strip()
    errors = response.get("errors")
    if not error_code or not error_message or not isinstance(errors, list) or not errors:
        return False
    for item in errors:
        if not isinstance(item, dict):
            return False
        code = str(item.get("code") or "").strip()
        message = str(item.get("message") or "").strip()
        if not code or not message:
            return False
    first_error = errors[0]
    return (
        str(first_error.get("code") or "").strip() == error_code
        and str(first_error.get("message") or "").strip() == error_message
    )


def _breaker_identity(extension: dict, runtime_context: dict) -> tuple[str, str] | None:
    extension_account_id = str(extension.get("account_id") or "").strip()
    runtime_account_id = str(runtime_context.get("account_id") or "").strip()
    extension_install_id = str(
        extension.get("extension_install_id") or extension.get("install_id") or ""
    ).strip()
    runtime_install_id = str(runtime_context.get("extension_install_id") or "").strip()
    if (
        extension_account_id and runtime_account_id and extension_account_id != runtime_account_id
    ) or (
        extension_install_id and runtime_install_id and extension_install_id != runtime_install_id
    ):
        return None
    return (
        runtime_account_id or extension_account_id,
        runtime_install_id or extension_install_id,
    )


def _run_subprocess_capped(
    command: list,
    *,
    cwd: str | None,
    env: dict,
    timeout: int,
    input_text: str | None = None,
    max_output_bytes: int = _MAX_EXTENSION_OUTPUT_BYTES,
    progress_sink: ProgressSink | None = None,
) -> tuple[int | None, str, str, bool]:
    """Run *command* capturing stdout/stderr with a hard per-stream byte cap.

    Unlike ``subprocess.run(capture_output=True)`` (which buffers the entire
    output in memory before any size check), this drains each pipe on its own
    thread and KILLS the child as soon as either stream exceeds ``max_output_bytes``
    — so an extension that floods stdout cannot OOM the host. Raises
    ``subprocess.TimeoutExpired`` on timeout (child killed first).

    Returns ``(returncode, stdout_text, stderr_text, exceeded)``.
    """
    cap = max(1, int(max_output_bytes))
    # The step that started this run listens for progress on a pipe of its own,
    # so progress never mixes with, or counts against, the captured output.
    progress_fds: tuple[int, int] | None = os.pipe() if progress_sink is not None else None
    if progress_fds is not None:
        env = {**env, PROGRESS_FD_ENV: str(progress_fds[1])}
    # Confinement (isolation phase 1): limits, no new privileges, a process group
    # killed as a whole, and a scratch TMPDIR of its own.
    scratch = scratch_directory()
    marker = new_run_marker()
    env = run_environment(env, scratch, marker)
    try:
        proc = subprocess.Popen(  # nosec B603 — command is operator-installed extension entrypoint
            confined_command(command, timeout_seconds=timeout),
            cwd=cwd or None,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            close_fds=True,
            pass_fds=(progress_fds[1],) if progress_fds is not None else (),
            start_new_session=True,
        )
    except BaseException:
        if progress_fds is not None:
            close_quietly(progress_fds[0])
            close_quietly(progress_fds[1])
        remove_scratch(scratch)
        raise
    if progress_fds is not None:
        # Only the child keeps the write end; the reader sees EOF when it exits.
        close_quietly(progress_fds[1])
    buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    exceeded = threading.Event()

    def _drain(stream, key: str) -> None:
        buf = buffers[key]
        total = 0
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                remaining = cap - len(buf)
                if remaining > 0:
                    buf.extend(chunk[:remaining])
                if total > cap:
                    # Strictly more than the cap was produced: stop accumulating and
                    # kill the child so it cannot keep producing output (bounded
                    # memory + bounded work). Output of exactly ``cap`` bytes is fine.
                    exceeded.set()
                    kill_process_group(proc.pid)
                    with contextlib.suppress(Exception):
                        proc.kill()
                    break
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    def _feed_stdin() -> None:
        # Write stdin on its own thread: a child that never reads stdin would
        # otherwise block a synchronous write once the payload exceeds the OS pipe
        # buffer, and the main thread would never reach the timeout/kill. Killing
        # the child unblocks this write with a broken-pipe error.
        if input_text is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(input_text.encode("utf-8"))
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()

    workers = [
        threading.Thread(target=_drain, args=(proc.stdout, "stdout"), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, "stderr"), daemon=True),
    ]
    if input_text is not None:
        workers.append(threading.Thread(target=_feed_stdin, daemon=True))
    if progress_fds is not None and progress_sink is not None:
        workers.append(
            threading.Thread(
                target=drain_progress, args=(progress_fds[0], progress_sink), daemon=True
            )
        )
    for thread in workers:
        thread.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_group(proc.pid)
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        raise
    finally:
        if proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.kill()
                proc.wait(timeout=5)
        # Whatever the extension started is ended with it, however it ended:
        # its process group, and any descendant that left the group but still
        # carries the run's marker.
        kill_process_group(proc.pid)
        kill_marked_processes(marker)
        remove_scratch(scratch)
    for thread in workers:
        thread.join(timeout=1.0)

    stdout_text = buffers["stdout"].decode("utf-8", errors="replace")
    stderr_text = buffers["stderr"].decode("utf-8", errors="replace")
    return proc.returncode, stdout_text, stderr_text, exceeded.is_set()


def run_extension_health(extension: dict) -> dict:
    from core.infrastructure.db.remote_extension_runtime import (
        is_remote_runtime_enabled,
        remote_health,
    )

    if is_remote_runtime_enabled(extension):
        invocation = remote_health(extension)
        response = _as_dict(invocation.get("response"))
        result_payload = _as_dict(response.get("result"))
        detail = str(
            invocation.get("detail") or result_payload.get("detail") or response.get("detail") or ""
        ).strip()
        return {
            "status": "healthy" if invocation.get("status") == "succeeded" else "failed",
            "detail": detail,
            "command": [],
            "bundle_root": str(extension.get("bundle_root") or "").strip(),
            "timeout_seconds": None,
            "exit_code": 0 if invocation.get("status") == "succeeded" else 2,
            "stdout": "",
            "stderr": "",
            "response": response,
        }

    try:
        from core.application.extensions.invocation_release import InvocationReleaseSelection

        extension = InvocationReleaseSelection.capture(extension).runtime_extension()
    except (OSError, ValueError) as exc:
        return {
            "status": "failed",
            "detail": str(exc),
            "command": [],
            "bundle_root": str(extension.get("bundle_root") or "").strip(),
        }

    health = _normalize_healthcheck(extension.get("healthcheck"))
    command = _normalize_command(health.get("command"))
    bundle_root = str(extension.get("bundle_root") or "").strip()
    if not command:
        return {
            "status": "unknown",
            "detail": "No health command configured",
            "command": [],
            "bundle_root": bundle_root,
        }
    command_error = _extension_command_validation_detail(
        bundle_root=bundle_root,
        command=command,
        field_path="health.command",
    )
    if command_error:
        return {
            "status": "failed",
            "detail": command_error,
            "command": command,
            "bundle_root": bundle_root,
        }
    timeout_seconds = max(1, int(health.get("timeout_seconds") or 10))
    try:
        returncode, stdout_raw, stderr_raw, _exceeded = _run_subprocess_capped(
            command,
            cwd=bundle_root or None,
            timeout=timeout_seconds,
            env=_sandboxed_env(extension),
        )
        stdout = (stdout_raw or "").strip()[:4000]
        stderr = (stderr_raw or "").strip()[:4000]
        return {
            "status": "healthy" if returncode == 0 else "failed",
            "detail": stderr or stdout or f"exit_code={returncode}",
            "command": command,
            "bundle_root": bundle_root,
            "timeout_seconds": timeout_seconds,
            "exit_code": returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "detail": str(exc),
            "command": command,
            "bundle_root": bundle_root,
            "timeout_seconds": timeout_seconds,
        }


def _runtime_payload_operation_id(payload: dict) -> str:
    for key in ("action_id", "operation_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    action = payload.get("action")
    if isinstance(action, str):
        return action.strip()
    action_payload = _as_dict(action)
    return str(action_payload.get("action_id") or action_payload.get("id") or "").strip()


def _external_effect_claim_missing(extension: dict, payload: dict, runtime_context: dict) -> bool:
    operation_id = _runtime_payload_operation_id(payload)
    if not operation_id:
        return False
    from core.application.extensions.external_effects import (
        external_effect_declaration_for_operation,
    )

    declaration = external_effect_declaration_for_operation(extension, operation_id)
    if not declaration:
        return False
    if (
        runtime_context.get("test_mode") is True
        and runtime_context.get("_host_test_mode_suppression") is True
        and str(declaration.get("test_mode_behavior") or "").strip().lower() == "suppress"
    ):
        return False
    return not str(runtime_context.get("external_effect_id") or "").strip()


def run_extension_invocation(
    extension: dict,
    *,
    request_payload: dict | None = None,
    timeout_seconds: float | None = None,
    runtime_context: dict | None = None,
    artifact_inputs: list[dict] | None = None,
    artifact_outputs: list[dict] | None = None,
    dataset_inputs: list[dict] | None = None,
    dataset_outputs: list[dict] | None = None,
) -> dict:
    """Run an extension in an isolated subprocess.

    Isolation guarantees (S1):
    - Separate OS process — extension crash cannot crash the host.
    - Environment filtered to allow-list — no accidental secret leakage.
    - Communication via JSON on stdin/stdout only.

    Governance (S2):
    - Circuit breaker rejects calls when quarantined.
    - Concurrency cap prevents resource exhaustion.

    Audit (S3):
    - ``runtime_context`` can carry ``job_id``, ``workflow_id``,
      ``group_id``, ``step_id``, ``step_execution_id`` for structured audit
      correlation.
    """
    availability_failure = extension_runtime_availability_failure(extension)
    if availability_failure is not None:
        return availability_failure

    try:
        from core.application.extensions.invocation_release import InvocationReleaseSelection

        extension = InvocationReleaseSelection.capture(extension).runtime_extension()
    except (OSError, ValueError) as exc:
        return {
            "status": "failed",
            "detail": str(exc),
            "command": [],
            "bundle_root": str(extension.get("bundle_root") or "").strip(),
            "response": {"definitely_no_external_effect": True},
        }

    entrypoint = _normalize_entrypoint(
        extension.get("entrypoint") or extension.get("entrypoint_jsonb")
    )
    command = _normalize_command(entrypoint.get("command"))
    bundle_root = str(extension.get("bundle_root") or "").strip()
    extension_key = str(
        extension.get("extension_key") or extension.get("extension_id") or ""
    ).strip()
    ctx = _as_dict(runtime_context)
    if not command:
        return {
            "status": "failed",
            "detail": "No entrypoint command configured",
            "command": [],
            "bundle_root": bundle_root,
            "response": {},
        }
    command_error = _extension_command_validation_detail(
        bundle_root=bundle_root,
        command=command,
        field_path="entrypoint.command",
    )
    if command_error:
        return {
            "status": "failed",
            "detail": command_error,
            "command": command,
            "bundle_root": bundle_root,
            "response": {},
        }

    timeout = _resolved_entrypoint_timeout(entrypoint, timeout_seconds)

    breaker_identity = _breaker_identity(extension, ctx) if extension_key else ("", "")
    if breaker_identity is None:
        return {
            "status": "failed",
            "detail": "extension_runtime_identity_mismatch",
            "command": command,
            "bundle_root": bundle_root,
            "timeout_seconds": timeout,
            "response": {"definitely_no_external_effect": True},
            "errors": [
                {
                    "code": "extension_runtime_identity_mismatch",
                    "message": "Extension runtime identity does not match the installed scope",
                }
            ],
        }

    payload = _as_dict(request_payload)
    if _external_effect_claim_missing(extension, payload, ctx):
        return {
            "status": "failed",
            "detail": "external_effect_claim_required",
            "command": command,
            "bundle_root": bundle_root,
            "timeout_seconds": timeout,
            "response": {"definitely_no_external_effect": True},
            "errors": [
                {
                    "code": "external_effect_claim_required",
                    "message": "Declared external-effect actions require a host claim",
                }
            ],
        }

    # S2 — circuit breaker gate. Missing account/install identity is kept in an
    # explicit empty-identity bucket; it can never reset or quarantine a known
    # tenant/install bucket.
    if extension_key:
        account_id, extension_install_id = breaker_identity
        breaker = ExtensionCircuitBreaker.for_extension(
            extension_key,
            account_id=account_id,
            extension_install_id=extension_install_id,
        )
        rejection = breaker.pre_invoke()
        if rejection:
            _log.warning("Invocation rejected: %s", rejection)
            return {
                "status": "failed",
                "detail": rejection,
                "command": command,
                "bundle_root": bundle_root,
                "timeout_seconds": timeout,
                "response": {},
                "quarantined": True,
            }
    else:
        breaker = None

    # The host names its contract on every payload, whichever caller built it
    # (query, action, notification delivery, tool call, direct invoke): the SDK
    # refuses an envelope without it before any effect.
    payload = {**payload, "contract_version": HOST_CONTRACT_VERSION}
    serialized_runtime_context = _subprocess_runtime_context(ctx)
    if serialized_runtime_context:
        payload = {**payload, "runtime_context": serialized_runtime_context}
    if artifact_inputs or artifact_outputs:
        payload = {
            **payload,
            "artifacts": {
                "inputs": list(artifact_inputs or []),
                "outputs": list(artifact_outputs or []),
            },
        }
    if dataset_inputs or dataset_outputs:
        payload = {
            **payload,
            "datasets": {
                "inputs": list(dataset_inputs or []),
                "outputs": list(dataset_outputs or []),
            },
        }
    stdin_text = json.dumps(_json_safe_subprocess_value(payload))
    breaker_outcome: bool | None = False
    try:
        returncode, stdout_raw, stderr_raw, exceeded = _run_subprocess_capped(
            command,
            cwd=bundle_root or None,
            input_text=stdin_text,
            timeout=timeout,
            env=_sandboxed_env(extension),  # S1 — filtered env
            progress_sink=current_progress_sink(),
        )
        if exceeded:
            # Output was capped mid-stream and the child killed; treat as failure
            # rather than parsing a truncated/oversized payload.
            raise ValueError(
                f"Extension output exceeds maximum size ({_MAX_EXTENSION_OUTPUT_BYTES} bytes)"
            )
        stdout_full = (stdout_raw or "").strip()
        stdout = stdout_full[:10000]
        stderr = (stderr_raw or "").strip()[:4000]
        response: dict[str, Any] = {}
        response_is_json_object = False
        if stdout_full:
            try:
                parsed = json.loads(stdout_full)
                if isinstance(parsed, dict):
                    response = parsed
                    response_is_json_object = True
                else:
                    response = {"value": parsed}
            except Exception:
                response = {"raw_stdout": stdout}
        status = (
            "succeeded"
            if returncode == 0 and response_is_json_object and response.get("ok") is True
            else "failed"
        )
        breaker_outcome = status == "succeeded"
        if not breaker_outcome and returncode == 2 and _is_structured_extension_error(response):
            # A valid extension envelope can reject a query or request without
            # indicating that the subprocess/runtime itself is unhealthy. Exit
            # codes other than the domain-failure code remain runtime failures.
            breaker_outcome = None
        detail = stderr or str(response.get("error") or "").strip() or f"exit_code={returncode}"
        result = {
            "status": status,
            "detail": detail if status == "failed" else "",
            "command": command,
            "bundle_root": bundle_root,
            "timeout_seconds": timeout,
            "exit_code": returncode,
            "response": response,
            "stdout": stdout,
            "stderr": stderr,
        }
        # S3 — attach audit context when available
        if ctx:
            result["audit_context"] = {
                k: ctx[k]
                for k in (
                    "job_id",
                    "workflow_id",
                    "group_id",
                    "for_each_id",
                    "step_id",
                    "step_execution_id",
                    "account_id",
                )
                if k in ctx
            }
        return result
    except Exception as exc:
        return {
            "status": "failed",
            "detail": str(exc),
            "command": command,
            "bundle_root": bundle_root,
            "timeout_seconds": timeout,
            "response": {},
        }
    finally:
        if breaker is not None:
            breaker.post_invoke(succeeded=breaker_outcome)


def _declared_operation_ids(extension: dict, key: str, id_field: str) -> set[str]:
    declared_rows = _declared_operation_rows(extension, key)
    normalized: set[str] = set()
    for row_payload in declared_rows:
        value = str(row_payload.get(id_field) or row_payload.get("id") or "").strip()
        if value:
            normalized.add(value)
    return normalized


def _declared_operation_rows(extension: dict, key: str) -> list[dict]:
    declared: list[Any] = []
    direct_rows = extension.get(key)
    if isinstance(direct_rows, list):
        declared.extend(direct_rows)
    compiled_manifest = _as_dict(extension.get("compiled_manifest"))
    compiled_rows = compiled_manifest.get(key)
    if isinstance(compiled_rows, list):
        declared.extend(compiled_rows)

    rows: list[dict] = []
    for row in declared:
        row_payload = _as_dict(row)
        if row_payload:
            rows.append(row_payload)
    return rows


def _declared_operation_row(
    extension: dict,
    *,
    operation_type: str,
    operation_id: str,
) -> dict:
    normalized_operation_id = str(operation_id or "").strip()
    normalized_operation_type = str(operation_type or "").strip().lower()
    if not normalized_operation_id or normalized_operation_type not in {"query", "action"}:
        return {}
    key = "query_definitions" if normalized_operation_type == "query" else "action_definitions"
    id_field = "query_id" if normalized_operation_type == "query" else "action_id"
    for row in _declared_operation_rows(extension, key):
        row_id = str(row.get(id_field) or row.get("id") or "").strip()
        if row_id == normalized_operation_id:
            return row
    return {}


def _operation_handler_mode(
    extension: dict,
    *,
    operation_type: str,
    operation_id: str,
) -> str:
    row = _declared_operation_row(
        extension,
        operation_type=operation_type,
        operation_id=operation_id,
    )
    handler = _as_dict(_as_dict(row).get("handler"))
    mode = str(handler.get("mode") or "extension").strip().lower() or "extension"
    if mode == "core":
        return "platform"
    return mode


__all__ = [
    "extension_runtime_availability_failure",
    "run_extension_health",
    "run_extension_invocation",
    "_declared_operation_ids",
    "_declared_operation_rows",
    "_declared_operation_row",
    "_operation_handler_mode",
]
