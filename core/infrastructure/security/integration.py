"""SEC-008..011: Trust boundary integration hooks.

Provides ``scan_inbound``, ``scan_outbound``, and ``scan_tool_io``
functions that wrap the SecurityOrchestrator engine
with policy resolution, enforcement mode, and event logging.

Hooks are non-fatal for scanner/runtime faults, but policy-parse failures are
fail-safe in enforce mode (blocked) to prevent silent bypass.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from core.infrastructure.security.config import (
    current_mode,
    enforcement_active,
    policy_error_fail_closed,
    security_enabled,
)
from core.infrastructure.security.engine import SecurityOrchestrator
from core.infrastructure.security.models import EnforcementDecision
from core.infrastructure.security.policy import SecurityPolicy
from core.infrastructure.security.policy_loader import (
    load_global_policy,
    load_project_policy_context,
    merge_policies,
)

logger = logging.getLogger(__name__)


class SecurityPolicyResolutionError(RuntimeError):
    """Raised when the effective security policy cannot be resolved/validated."""


# ---------------------------------------------------------------------------
# Policy resolution (global + project DB-backed overrides)
# ---------------------------------------------------------------------------
# Policies already resolved inside `reuse_resolved_policies()`, keyed by project.
_RESOLVED_POLICIES: ContextVar[dict[str | None, SecurityPolicy] | None] = ContextVar(
    "fs_security_resolved_policies", default=None
)


@contextmanager
def reuse_resolved_policies() -> Iterator[None]:
    """Resolve each project's policy once for the enclosed work, not once per scan.

    AC17-R155: a job detail redacts every leaf of its diagnostics preview, and each
    scan resolved the policy again -- global policy, a deep copy of the default, the
    project override and a system-setting query. One GET ran 3085 SQL statements. The
    enclosed scans now share the policy as it was when the first of them resolved it.
    A resolution that fails is not remembered, so every scan still fails closed.
    """
    token = _RESOLVED_POLICIES.set({})
    try:
        yield
    finally:
        _RESOLVED_POLICIES.reset(token)


def _resolve_policy(
    project_id: str | None = None, project_override: dict | None = None
) -> SecurityPolicy:
    resolved = _RESOLVED_POLICIES.get()
    if resolved is None or project_override is not None:
        return _resolve_policy_now(project_id, project_override)
    key = None if project_id is None else str(project_id)
    policy = resolved.get(key)
    if policy is None:
        policy = _resolve_policy_now(project_id, None)
        resolved[key] = policy
    return policy


def _resolve_policy_now(
    project_id: str | None = None, project_override: dict | None = None
) -> SecurityPolicy:
    """Load and merge policy: global policy + project override.

    Resolution order:
    1. Global effective policy (YAML default + DB global override).
    2. If *project_override* dict is given explicitly, use it.
    3. Otherwise, if *project_id* is given, load project override from DB.
    """
    account_id = None
    if project_id is not None:
        account_id, stored_override = load_project_policy_context(project_id)
        if project_override is None:
            project_override = stored_override
    global_policy = load_global_policy(account_id)

    try:
        return merge_policies(global_policy, project_override)
    except Exception as e:
        raise SecurityPolicyResolutionError("invalid security policy configuration") from e


def _make_allow(text: str) -> EnforcementDecision:
    """Quick allow-through decision."""
    return EnforcementDecision(action="allow", redacted_text=text)


def _policy_is_enabled(policy: SecurityPolicy) -> bool:
    return bool(getattr(policy, "enabled", True))


def _policy_error_mode_label() -> str:
    if enforcement_active():
        return "enforce"
    if policy_error_fail_closed():
        return "monitor_fail_closed"
    return "monitor"


def _record_policy_resolution_error(
    *,
    surface: str,
    project_id: str | None,
    job_id: str | None,
    mode_label: str,
) -> None:
    message = f"security policy resolution failed [surface={surface} mode={mode_label}]"

    try:
        from core.infrastructure.security.event_logger import get_security_logger

        sec_logger = get_security_logger()
        if sec_logger:
            sec_logger.log_policy_resolution_error(
                project_id=project_id,
                job_id=job_id,
                surface=surface,
                mode=mode_label,
                message=message,
            )
    except Exception:
        logger.debug("policy resolution error event logging failed", exc_info=True)

    if job_id is None:
        return

    try:
        bl = _resolve_job_error_logger()
        if bl is None:
            return
        bl.log_job_error(
            str(job_id),
            source="security",
            category="policy",
            error_message="Security policy resolution failed during scan",
            response_excerpt=message,
            context_jsonb={
                "surface": surface,
                "mode": mode_label,
                "project_id": str(project_id) if project_id is not None else None,
                "scan_mode": current_mode(),
                "category": "policy_resolution_error",
            },
        )
    except Exception:
        logger.debug("policy resolution error job logging failed", exc_info=True)


def _resolve_job_error_logger():
    """Best-effort runtime logger for job-level security error records."""
    try:
        from core.infrastructure.db.platform_stores import (
            PlatformJobStore,
            resolve_observability_dsn,
        )
    except Exception:
        return None
    try:
        dsn = resolve_observability_dsn()
        if not dsn:
            return None
        return PlatformJobStore(dsn)
    except Exception:
        return None


def _make_policy_error_decision(
    text: str,
    *,
    surface: str,
    project_id: Any = None,
    job_id: Any = None,
) -> EnforcementDecision:
    """Fail-safe behavior for policy parse errors.

    In enforce mode, block to avoid fail-open bypass. In monitor mode,
    preserve pass-through behavior unless fail-closed flag is enabled.
    """
    in_enforce_mode = enforcement_active()
    fail_closed = in_enforce_mode or policy_error_fail_closed()
    mode_label = _policy_error_mode_label()
    _record_policy_resolution_error(
        surface=surface,
        project_id=project_id,
        job_id=job_id,
        mode_label=mode_label,
    )
    if fail_closed:
        logger.error(
            "security policy resolution failed [surface=%s mode=%s] — blocking payload",
            surface,
            mode_label,
        )
        return EnforcementDecision(
            action="block",
            redacted_text="[BLOCKED: invalid security policy configuration]",
        )
    logger.warning(
        "security policy resolution failed in monitor mode [surface=%s] — passing through",
        surface,
    )
    return _make_allow(text)


SCAN_UNAVAILABLE_PLACEHOLDER = "[REDACTED: security scan unavailable]"


def _withheld(decision: EnforcementDecision) -> EnforcementDecision:
    """A redacting scan that could not run lets none of its text through.

    The policy-error path passes the text on in monitor mode, which never blocks; for a
    surface whose job is to redact what leaves, that handed back the secret it was asked
    to remove. Monitor mode still never blocks here -- it redacts all of it.
    """
    if decision.action == "block":
        return decision
    return EnforcementDecision(action="redact", redacted_text=SCAN_UNAVAILABLE_PLACEHOLDER)


def _maybe_downgrade(decision: EnforcementDecision, original_text: str) -> EnforcementDecision:
    """In monitor mode: never *block*, but still apply redaction.

    This ensures secrets and PII are redacted even in monitor mode.  Only the
    ``block`` action (which would reject the request entirely) is downgraded to
    ``allow``.  The ``redact`` action and its ``redacted_text`` are preserved so
    the data written to persistence is clean regardless of enforcement mode.
    """
    _ = original_text
    if not enforcement_active() and decision.action == "block":
        decision = decision.model_copy(update={"action": "allow"})
    return decision


def _log_decision(
    decision: EnforcementDecision,
    *,
    surface: str,
    project_id: Any = None,
    job_id: Any = None,
    account_id: str | None = None,
) -> None:
    """Best-effort logging of scan decisions."""
    if not decision.findings:
        return
    try:
        from core.infrastructure.security.event_logger import get_security_logger

        sec_logger = get_security_logger()
        if sec_logger:
            sec_logger.log_scan_event(
                findings=decision.findings,
                surface=surface,
                project_id=project_id,
                job_id=job_id,
                scan_report=decision.scan_report,
                account_id=account_id,
            )
    except Exception:
        # Event logger not available yet (pre-SEC-013) — fall back to stdlib
        logger.debug(
            "security scan [surface=%s project=%s account=%s]: %d finding(s), action=%s",
            surface,
            project_id,
            account_id,
            len(decision.findings),
            decision.action,
        )


# ---------------------------------------------------------------------------
# SEC-008: Inbound scanning
# ---------------------------------------------------------------------------
def scan_inbound(
    text: str,
    surface: str = "inbound",
    *,
    project_id: Any = None,
    job_id: Any = None,
    account_id: str | None = None,
    project_override: dict | None = None,
    context_type: str = "prompt",
) -> EnforcementDecision:
    """Scan externally-supplied text before it reaches agent prompts.

    Drop-in replacement for ``sanitize_external_input`` — subsumes the old
    regex approach with the full scanner suite.
    """
    if not security_enabled() or not text:
        return _make_allow(text or "")

    try:
        policy = _resolve_policy(project_id, project_override)
        if not _policy_is_enabled(policy):
            return _make_allow(text)
        orch = SecurityOrchestrator(policy, project_id=project_id or 0)
        decision = orch.scan_input(text, surface, context_type=context_type)
        decision = _maybe_downgrade(decision, text)
        _log_decision(
            decision, surface=surface, project_id=project_id, job_id=job_id, account_id=account_id
        )
        return decision
    except SecurityPolicyResolutionError:
        return _make_policy_error_decision(
            text,
            surface=surface,
            project_id=project_id,
            job_id=job_id,
        )
    except Exception:
        # Treated as the policy error it is in effect: blocked when enforcing, passed in
        # monitor mode unless the fail-closed flag says otherwise.
        logger.exception("scan_inbound failed — applying the policy-error decision")
        return _make_policy_error_decision(
            text,
            surface=surface,
            project_id=project_id,
            job_id=job_id,
        )


# ---------------------------------------------------------------------------
# SEC-009: Outbound scanning
# ---------------------------------------------------------------------------
def scan_outbound(
    text: str,
    surface: str = "outbound",
    *,
    project_id: Any = None,
    job_id: Any = None,
    account_id: str | None = None,
    project_override: dict | None = None,
    context_type: str = "display",
) -> EnforcementDecision:
    """Scan agent output before publish/delivery."""
    if not security_enabled() or not text:
        return _make_allow(text or "")

    try:
        policy = _resolve_policy(project_id, project_override)
        if not _policy_is_enabled(policy):
            return _make_allow(text)
        orch = SecurityOrchestrator(policy, project_id=project_id or 0)
        decision = orch.scan_output(text, surface, context_type=context_type)
        decision = _maybe_downgrade(decision, text)
        _log_decision(
            decision, surface=surface, project_id=project_id, job_id=job_id, account_id=account_id
        )
        return decision
    except SecurityPolicyResolutionError:
        return _withheld(
            _make_policy_error_decision(
                text,
                surface=surface,
                project_id=project_id,
                job_id=job_id,
            )
        )
    except Exception:
        # A failure is not a policy verdict: nothing of the text leaves unredacted.
        logger.exception("scan_outbound failed — withholding the text")
        return _withheld(
            _make_policy_error_decision(
                text,
                surface=surface,
                project_id=project_id,
                job_id=job_id,
            )
        )


# ---------------------------------------------------------------------------
# SEC-010: Tool boundary scanning
# ---------------------------------------------------------------------------
def scan_tool_input(
    tool_args_str: str,
    *,
    tool_name: str = "",
    project_id: str | None = None,
    job_id: str | None = None,
    account_id: str | None = None,
) -> EnforcementDecision:
    """Scan tool arguments before execution."""
    _ = tool_name
    if not security_enabled() or not tool_args_str:
        return _make_allow(tool_args_str or "")

    try:
        policy = _resolve_policy(project_id)
        if not _policy_is_enabled(policy):
            return _make_allow(tool_args_str)
        orch = SecurityOrchestrator(policy, project_id=project_id or "")
        decision = orch.scan_input(tool_args_str, "tool", context_type="shell")
        decision = _maybe_downgrade(decision, tool_args_str)
        _log_decision(
            decision, surface="tool", project_id=project_id, job_id=job_id, account_id=account_id
        )
        return decision
    except SecurityPolicyResolutionError:
        return _make_policy_error_decision(
            tool_args_str,
            surface="tool",
            project_id=project_id,
            job_id=job_id,
        )
    except Exception:
        # Treated as the policy error it is in effect: blocked when enforcing, passed in
        # monitor mode unless the fail-closed flag says otherwise.
        logger.exception("scan_tool_input failed — applying the policy-error decision")
        return _make_policy_error_decision(
            tool_args_str,
            surface="tool",
            project_id=project_id,
            job_id=job_id,
        )


def scan_tool_output(
    tool_result: str,
    *,
    tool_name: str = "",
    project_id: str | None = None,
    job_id: str | None = None,
    account_id: str | None = None,
) -> EnforcementDecision:
    """Scan tool output before returning to agent."""
    _ = tool_name
    if not security_enabled() or not tool_result:
        return _make_allow(tool_result or "")

    try:
        policy = _resolve_policy(project_id)
        if not _policy_is_enabled(policy):
            return _make_allow(tool_result)
        orch = SecurityOrchestrator(policy, project_id=project_id or "")
        decision = orch.scan_output(tool_result, "tool", context_type="display")
        decision = _maybe_downgrade(decision, tool_result)
        _log_decision(
            decision, surface="tool", project_id=project_id, job_id=job_id, account_id=account_id
        )
        return decision
    except SecurityPolicyResolutionError:
        return _withheld(
            _make_policy_error_decision(
                tool_result,
                surface="tool",
                project_id=project_id,
                job_id=job_id,
            )
        )
    except Exception:
        # A failure is not a policy verdict: nothing of the text leaves unredacted.
        logger.exception("scan_tool_output failed — withholding the text")
        return _withheld(
            _make_policy_error_decision(
                tool_result,
                surface="tool",
                project_id=project_id,
                job_id=job_id,
            )
        )
