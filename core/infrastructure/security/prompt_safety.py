"""Prompt safety helpers for template validation and state minimization."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from core.infrastructure.security.secret_reference import is_secret_reference

# Project config paths that are explicitly safe to expose in prompt templates.
_ALLOWED_PROJECT_CONFIG_PREFIXES = (
    "project_config.project_rules",
    "project_config.conventions",
    "project_config.project_name",
    "project_config.project_id",
    "project_config.operator_default_workflow_id",
)

# Sensitive path families that must never appear in templates/context-inject.
_FORBIDDEN_PROJECT_CONFIG_PREFIXES = (
    "project_config.project_connections",
    "project_config.project_providers",
    "project_config.config",
    "project_config.secrets",
    "project_config.secret_refs",
    "project_config.credentials",
    "project_config.auth",
    "project_config.tokens",
    "project_config.api_keys",
)

_BRACE_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_](?a:\w*)(?:\.[A-Za-z_](?a:\w*))*)\}")
_PROJECT_CONFIG_PATH_RE = re.compile(
    r"project_config(?:\.[A-Za-z_](?a:\w*)|\[['\"][A-Za-z_](?a:\w*)['\"]\])*"
)

_PROMPT_PROJECT_CONFIG_KEYS = frozenset(
    {
        "project_name",
        "project_id",
        "project_rules",
        "conventions",
        "operator_default_workflow_id",
    }
)

_SENSITIVE_VALUE_KEYS = frozenset(
    {
        "project_connections",
        "project_providers",
        "config",
        "raw_config",
        "secret",
        "secrets",
        "secret_ref",
        "secret_refs",
        "credentials",
        "credential",
        "authorization",
        "auth",
        "token",
        "tokens",
        "api_key",
        "api_keys",
        "password",
    }
)
_SENSITIVE_KEY_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "credential",
    "api_key",
    "private_key",
    "dsn",
)
_DIAGNOSTIC_IDENTIFIER_KEYS = frozenset(
    {
        "request_id",
        "x_request_id",
        "openai_request_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "run_id",
        "parent_run_id",
        "correlation_id",
        "cause_correlation_id",
        "message_id",
        "dispatch_id",
        "attempt_id",
        "event_id",
        "error_id",
        "provider_error_code",
        "provider_error_type",
    }
)
_REDACTED_DIAGNOSTIC_ID = "[REDACTED_DIAGNOSTIC_ID]"
_SAFE_DIAGNOSTIC_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_UNSAFE_DIAGNOSTIC_VALUE_RE = re.compile(
    r"""(?ix)
    (
      \b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b
      | \b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://
      | -----BEGIN\ (?:RSA\ |EC\ |DSA\ |OPENSSH\ )?PRIVATE\ KEY-----
      | \bbearer\s+(?a:[\w.~+/=-]){12,}
      | \beyJ(?a:[\w-]){10,}\.(?a:[\w-]){10,}\.(?a:[\w-]){10,}
      | \bghp_(?a:\w){20,}
      | \bgithub_pat_(?a:\w){20,}
      | \bglpat-(?a:[\w-]){20,}
      | \bxoxb-(?a:[\w-]){20,}
      | \bsk-(?a:[\w-]){20,}
      | \bsk_live_(?a:\w){20,}
      | \bSG\.(?a:[\w-]){16,}\.(?a:[\w-]){16,}
      | \bAIza(?a:[\w-]){20,}
      | (?i:api[_-]?key|api[_-]?secret|secret[_-]?key|access[_-]?token|refresh[_-]?token|bearer[_-]?token)\s*[:=]
    )
    """
)
_CANONICAL_PROJECTED_REGISTRY_ID_RE = re.compile(
    r"^extension\."
    r"(?:pi_[0-9a-fA-F]{24}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\."
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,191}$"
)


def normalize_reference_path(path: str) -> str:
    """Normalize dotted/bracket path forms into a canonical dotted path."""
    text = str(path or "").strip()
    if not text:
        return ""
    text = text.replace(" ", "")
    text = re.sub(
        r"\[['\"]?([A-Za-z_](?a:\w*))['\"]?\]",
        lambda match: f".{match.group(1)}",
        text,
    )
    return re.sub(r"\.+", ".", text).strip(".")


def is_forbidden_reference_path(path: str) -> tuple[bool, str]:
    """Return (is_forbidden, reason) for a template/context reference path."""
    normalized = normalize_reference_path(path)
    if not normalized:
        return False, ""
    if normalized == "project_config":
        return True, "Path 'project_config' is forbidden; reference only explicit safe fields."
    if not normalized.startswith("project_config."):
        return False, ""

    for prefix in _FORBIDDEN_PROJECT_CONFIG_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "."):
            return True, f"Path '{normalized}' is forbidden by security policy."

    if _is_allowed_project_config_path(normalized):
        return False, ""

    return (
        True,
        (
            f"Path '{normalized}' is not in the prompt-safe allowlist. "
            "Use project name/rules/conventions fields only."
        ),
    )


def validate_reference_path(path: str) -> None:
    """Raise ValueError when a reference path is forbidden."""
    forbidden, reason = is_forbidden_reference_path(path)
    if forbidden:
        raise ValueError(reason)


def find_forbidden_template_paths(template: str) -> list[str]:
    """Extract forbidden paths referenced by a template string."""
    text = str(template or "")
    candidates: set[str] = set()
    for match in _BRACE_PLACEHOLDER_RE.findall(text):
        candidates.add(normalize_reference_path(match))
    for match in _PROJECT_CONFIG_PATH_RE.findall(text):
        candidates.add(normalize_reference_path(match))

    blocked: list[str] = []
    for candidate in sorted(candidates):
        forbidden, _ = is_forbidden_reference_path(candidate)
        if forbidden:
            blocked.append(candidate)
    return blocked


#: What an agent, an operator reading a job, or a Test Run response sees in place
#: of a credential. Deliberately not a value: the plaintext is resolved only at
#: an authorized connector or tool boundary.
REDACTED_CREDENTIAL_MARKER = "[redacted: credential — resolved at the tool boundary]"


def is_sensitive_name(name: object) -> bool:
    """Whether a field name means "credential" under the one canonical policy.

    Every surface that hides secrets asks this, so there is a single list to
    review and a single place a new fragment has to be added.
    """
    normalized = str(name or "").strip().lower().replace("-", "_")
    if not normalized:
        return False
    if normalized in _SENSITIVE_VALUE_KEYS:
        return True
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def redact_sensitive_structure(value: Any, *, sensitive_keys: frozenset[str] = frozenset()) -> Any:
    """Recursively replace credential-bearing fields with the marker.

    `sensitive_keys` carries names that are sensitive for a reason the name
    itself cannot show — a value whose *source* was a secret, aliased on the way
    in to something innocuous. Sensitivity follows the value, not its current
    spelling.
    """
    # A reference is sensitive whatever key it sits under: the field it came
    # from may be spelled `login`, which no name-based rule would ever flag.
    if is_secret_reference(value):
        return REDACTED_CREDENTIAL_MARKER
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if is_sensitive_name(key) or key in sensitive_keys:
                redacted[key] = REDACTED_CREDENTIAL_MARKER
                continue
            redacted[key] = redact_sensitive_structure(item, sensitive_keys=sensitive_keys)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_structure(item, sensitive_keys=sensitive_keys) for item in value]
    if isinstance(value, tuple):
        return tuple(
            redact_sensitive_structure(item, sensitive_keys=sensitive_keys) for item in value
        )
    return value


def sanitize_project_config_for_prompt(project_config: dict | None) -> dict[str, Any]:
    """Return a prompt-safe project_config projection (data minimization)."""
    source = project_config if isinstance(project_config, dict) else {}
    safe: dict[str, Any] = {}

    for key in _PROMPT_PROJECT_CONFIG_KEYS:
        if key not in source:
            continue
        sanitized = _sanitize_prompt_value(source.get(key))
        if sanitized not in (None, "", [], {}):
            safe[key] = sanitized

    return safe


def find_prompt_guard_violations(
    system_prompt: str,
    user_prompt: str,
    *,
    project_config: dict | None = None,
) -> list[str]:
    """Return prompt-guard violations before sending text to an LLM provider."""
    combined = "\n\n".join(
        part for part in (str(system_prompt or ""), str(user_prompt or "")) if part
    )
    if not combined:
        return []

    violations: list[str] = []
    blocked_paths = find_forbidden_template_paths(combined)
    if blocked_paths:
        violations.append(
            "prompt contains forbidden reference path(s): " + ", ".join(sorted(set(blocked_paths)))
        )

    sensitive_values = _collect_sensitive_project_values(project_config)
    prompt_safe_values = _collect_prompt_safe_project_values(project_config)
    seen: set[str] = set()
    for path, value in sensitive_values:
        if not value or len(value) < 6:
            continue
        if value in prompt_safe_values:
            continue
        if value in combined:
            key = f"{path}:{value[:12]}"
            if key in seen:
                continue
            seen.add(key)
            violations.append(f"prompt contains sensitive value from project_config path '{path}'")

    return violations


def assert_prompt_payload_safe(
    system_prompt: str,
    user_prompt: str,
    *,
    project_config: dict | None = None,
) -> None:
    """Raise ValueError when prompt guard detects sensitive content leakage."""
    violations = find_prompt_guard_violations(
        system_prompt,
        user_prompt,
        project_config=project_config,
    )
    if violations:
        raise ValueError("; ".join(violations))


def redact_text_for_persistence(
    text: str,
    *,
    project_id: int | str | None = None,
    job_id: int | str | None = None,
    context_type: str = "display",
    preserve_raw_on_block: bool = False,
) -> str:
    """Redact secrets/PII before persisting prompt/log payloads."""
    raw = str(text or "")
    if not raw:
        return raw

    try:
        from core.infrastructure.security.integration import scan_outbound

        decision = scan_outbound(
            raw,
            "outbound",
            project_id=project_id,
            job_id=job_id,
            context_type=context_type,
        )
        if decision.action == "block" and not preserve_raw_on_block:
            return "[BLOCKED: security policy]"
        if isinstance(decision.redacted_text, str):
            return decision.redacted_text
    except Exception:
        # The scan could not run: withhold the text rather than persist it half-masked.
        from core.infrastructure.security.integration import SCAN_UNAVAILABLE_PLACEHOLDER

        return SCAN_UNAVAILABLE_PLACEHOLDER

    redacted = raw
    for forbidden in find_forbidden_template_paths(redacted):
        redacted = redacted.replace(forbidden, "[REDACTED_PATH]")
    return re.sub(
        r"\b(?:postgres|mysql|mongodb|redis)://[^\s\"']+",
        "[REDACTED_URI]",
        redacted,
        flags=re.IGNORECASE,
    )


def redact_structure_for_persistence(
    value: Any,
    *,
    project_id: int | str | None = None,
    job_id: int | str | None = None,
    context_type: str = "display",
    preserve_raw_on_block: bool = False,
) -> Any:
    """Recursively redact text fields in nested payloads before persistence."""
    return _redact_structure_for_persistence(
        value,
        project_id=project_id,
        job_id=job_id,
        context_type=context_type,
        preserve_raw_on_block=preserve_raw_on_block,
        current_key="",
    )


def redact_diagnostic_identifier_for_persistence(
    key: str,
    value: Any,
    *,
    project_id: int | str | None = None,
    job_id: int | str | None = None,
    context_type: str = "display",
) -> Any:
    """Preserve safe opaque diagnostic IDs while redacting unsafe values."""
    if not isinstance(value, str):
        return value
    raw = str(value or "").strip()
    if not raw:
        return raw
    if not _is_diagnostic_identifier_key(key):
        return redact_text_for_persistence(
            raw,
            project_id=project_id,
            job_id=job_id,
            context_type=context_type,
        )
    if _is_safe_opaque_diagnostic_identifier(raw):
        return raw
    return _REDACTED_DIAGNOSTIC_ID


def _redact_structure_for_persistence(
    value: Any,
    *,
    project_id: int | str | None,
    job_id: int | str | None,
    context_type: str,
    preserve_raw_on_block: bool,
    current_key: str,
) -> Any:
    if isinstance(value, dict):
        return {
            str(k): _redact_structure_for_persistence(
                v,
                project_id=project_id,
                job_id=job_id,
                context_type=context_type,
                preserve_raw_on_block=preserve_raw_on_block,
                current_key=str(k),
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_structure_for_persistence(
                item,
                project_id=project_id,
                job_id=job_id,
                context_type=context_type,
                preserve_raw_on_block=preserve_raw_on_block,
                current_key=current_key,
            )
            for item in value
        ]
    if isinstance(value, str):
        if is_canonical_structured_registry_identifier(current_key, value):
            return value
        if _is_diagnostic_identifier_key(current_key):
            return redact_diagnostic_identifier_for_persistence(
                current_key,
                value,
                project_id=project_id,
                job_id=job_id,
                context_type=context_type,
            )
        return redact_text_for_persistence(
            value,
            project_id=project_id,
            job_id=job_id,
            context_type=context_type,
            preserve_raw_on_block=preserve_raw_on_block,
        )
    return value


def is_canonical_structured_registry_identifier(key: str, value: Any) -> bool:
    """Recognize host-generated install-scoped IDs only in typed identity fields."""
    if not isinstance(value, str):
        return False
    normalized_key = str(key or "").strip().lower().replace("-", "_").lstrip("_")
    identity_field = normalized_key in {
        "connector_id",
        "selected_workflow",
        "tool_id",
        "workflow",
        "workflow_id",
    } or normalized_key.endswith("_workflow_id")
    return bool(identity_field and _CANONICAL_PROJECTED_REGISTRY_ID_RE.fullmatch(value.strip()))


def restore_canonical_structured_registry_identifiers(original: Any, redacted: Any) -> Any:
    """Restore only canonical typed registry IDs after a whole-payload scan."""
    if isinstance(original, dict) and isinstance(redacted, dict):
        restored = dict(redacted)
        for raw_key, original_value in original.items():
            key = str(raw_key)
            if key not in restored:
                continue
            if is_canonical_structured_registry_identifier(key, original_value):
                restored[key] = original_value
            else:
                restored[key] = restore_canonical_structured_registry_identifiers(
                    original_value,
                    restored[key],
                )
        return restored
    if isinstance(original, list) and isinstance(redacted, list):
        return [
            restore_canonical_structured_registry_identifiers(original_item, redacted_item)
            for original_item, redacted_item in zip(original, redacted, strict=False)
        ] + list(redacted[len(original) :])
    return redacted


def _is_diagnostic_identifier_key(key: str) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    return normalized in _DIAGNOSTIC_IDENTIFIER_KEYS


def _is_safe_opaque_diagnostic_identifier(value: str) -> bool:
    text = str(value or "").strip()
    if not _SAFE_DIAGNOSTIC_IDENTIFIER_RE.fullmatch(text):
        return False
    return _UNSAFE_DIAGNOSTIC_VALUE_RE.search(text) is None


def blocked_reference_examples() -> list[dict[str, str]]:
    """Human-readable blocked path examples for UI hint panels."""
    return [
        {"name": "project_config", "reason": "Whole project_config is forbidden in templates."},
        {
            "name": "project_config.credentials",
            "reason": "Credential payloads cannot be injected into prompt context.",
        },
        {
            "name": "project_config.project_connections",
            "reason": "Connection payloads are blocked from prompt context.",
        },
        {
            "name": "project_config.project_providers",
            "reason": "Provider binding payloads are blocked from prompt context.",
        },
        {
            "name": "project_config.config",
            "reason": "Raw config blob is blocked from prompt context.",
        },
    ]


def _is_allowed_project_config_path(path: str) -> bool:
    for prefix in _ALLOWED_PROJECT_CONFIG_PREFIXES:
        if path == prefix or path.startswith(prefix + "."):
            return True
    return False


def _is_explicitly_forbidden_project_config_path(path: str) -> bool:
    """Return True only for the strict forbidden prefix families."""
    normalized = normalize_reference_path(path)
    if not normalized:
        return False
    normalized = re.sub(r"\[\d+\]", "", normalized)
    for prefix in _FORBIDDEN_PROJECT_CONFIG_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "."):
            return True
    return False


def _is_sensitive_key_name(key: str) -> bool:
    normalized = str(key or "").strip().lower()
    if not normalized:
        return False
    if normalized in _SENSITIVE_VALUE_KEYS:
        return True
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _collect_sensitive_project_values(project_config: dict | None) -> list[tuple[str, str]]:
    source = project_config if isinstance(project_config, dict) else {}
    collected: list[tuple[str, str]] = []
    _walk_sensitive_values(source, path="", inherited_sensitive=False, out=collected)
    return collected


def _collect_prompt_safe_project_values(project_config: dict | None) -> set[str]:
    collected: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if isinstance(value, bool) or value is None:
            return
        text = str(value).strip()
        if len(text) >= 6:
            collected.add(text)

    walk(sanitize_project_config_for_prompt(project_config))
    return collected


def _walk_sensitive_values(
    value: Any,
    *,
    path: str,
    inherited_sensitive: bool,
    out: list[tuple[str, str]],
) -> None:
    if isinstance(value, dict):
        for raw_key, raw_item in value.items():
            key = str(raw_key or "").strip()
            if not key:
                continue
            next_path = f"{path}.{key}" if path else key
            sensitive = inherited_sensitive or _is_sensitive_key_name(key)
            sensitive = sensitive or _is_explicitly_forbidden_project_config_path(
                f"project_config.{next_path}"
            )
            _walk_sensitive_values(
                raw_item,
                path=next_path,
                inherited_sensitive=sensitive,
                out=out,
            )
        return

    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk_sensitive_values(
                item,
                path=f"{path}[{index}]",
                inherited_sensitive=inherited_sensitive,
                out=out,
            )
        return

    if not inherited_sensitive:
        return
    if isinstance(value, bool) or value is None:
        return

    text = str(value).strip()
    if len(text) < 6:
        return
    out.append((path or "project_config", text))


def _sanitize_prompt_value(value: Any):
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key or "")
            key_lower = key_text.strip().lower()
            if not key_lower:
                continue
            if key_lower in _SENSITIVE_VALUE_KEYS:
                continue
            if any(fragment in key_lower for fragment in _SENSITIVE_KEY_FRAGMENTS):
                continue
            nested = _sanitize_prompt_value(item)
            if nested in (None, "", [], {}):
                continue
            result[key_text] = nested
        return result
    if isinstance(value, list):
        cleaned = []
        for item in value:
            nested = _sanitize_prompt_value(item)
            if nested in (None, "", [], {}):
                continue
            cleaned.append(nested)
        return cleaned
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
