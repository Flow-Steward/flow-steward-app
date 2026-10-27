"""Validate HTTP safety limits and Collect step configuration values."""

from __future__ import annotations

from urllib.parse import urlparse

from core.application.registry.contract_validation_common import (
    _is_state_path,
    _issue,
)

INLINE_HTTP_RESPONSE_MAX_BYTES = 5 * 1024 * 1024


ARTIFACT_HTTP_RESPONSE_DEFAULT_BYTES = 100 * 1024 * 1024


ARTIFACT_HTTP_RESPONSE_MAX_BYTES = 200 * 1024 * 1024


def _http_auth_ref_uses_process_env(auth_ref: str, *, auth_mode: str) -> bool:
    ref = str(auth_ref or "").strip()
    if not ref:
        return False
    candidates = [ref]
    if str(auth_mode or "").strip().lower() == "basic":
        left, sep, right = ref.partition(":")
        candidates = [left, right] if sep else [left]
    return any(str(candidate or "").strip().lower().startswith("env:") for candidate in candidates)


def _http_response_mode(config: dict) -> str:
    mode = str(config.get("response_mode") or "inline").strip().lower()
    return mode if mode in {"inline", "artifact"} else ""


def _http_max_response_bytes_error(config: dict, *, mode: str) -> str:
    raw = config.get("max_response_bytes")
    if raw in (None, ""):
        return ""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return "http_request max_response_bytes must be a positive integer"
    ceiling = (
        ARTIFACT_HTTP_RESPONSE_MAX_BYTES if mode == "artifact" else INLINE_HTTP_RESPONSE_MAX_BYTES
    )
    if raw <= 0:
        return "http_request max_response_bytes must be positive"
    if raw > ceiling:
        return f"http_request max_response_bytes exceeds the {mode} ceiling"
    return ""


def _http_url_has_template_in_scheme_or_authority(value: str) -> bool:
    text = str(value or "").strip()
    scheme_idx = text.find("://")
    if scheme_idx < 0:
        return "{{" in text
    authority = text[scheme_idx + 3 :].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return "{{" in text[:scheme_idx] or "{{" in authority


def _http_auth_url_static_origin_error(url_template: str) -> str:
    if _http_url_has_template_in_scheme_or_authority(url_template):
        return "authenticated http_request url scheme and authority must be static"
    parsed = urlparse(str(url_template or "").strip())
    if str(parsed.scheme or "").lower() != "https":
        return "authenticated http_request url must use https"
    if parsed.username or parsed.password:
        return "authenticated http_request url must not contain userinfo"
    if not parsed.hostname:
        return "authenticated http_request url requires a literal host"
    return ""


COLLECT_MODE_APPEND = "append"
COLLECT_MODE_SET_KEY = "set_key"
COLLECT_MODES = (COLLECT_MODE_APPEND, COLLECT_MODE_SET_KEY)


def _validate_collect_step_config(config: dict, step_path: str) -> list[dict]:
    """Collect adds one value to an existing array or one literal key of an object.

    ``target`` and ``source`` are state paths; ``key`` (set_key only) is a
    literal string or ``{"source": "<state path>"}`` whose value is used as one
    literal dictionary key (ADR 0038).
    """
    errors: list[dict] = []
    path = f"{step_path}.override.collect"
    target = str(config.get("target") or "").strip()
    if not target:
        errors.append(
            _issue(
                code="workflow.step_collect_target_missing",
                message="Collect needs the variable it adds to (target)",
                path=f"{path}.target",
            )
        )
    elif not _is_state_path(target):
        errors.append(
            _issue(
                code="workflow.step_collect_target_invalid",
                message="Collect target must be a state path such as state.emails",
                path=f"{path}.target",
            )
        )
    source = config.get("source")
    if source in (None, ""):
        errors.append(
            _issue(
                code="workflow.step_collect_source_missing",
                message="Collect needs the value it adds (source)",
                path=f"{path}.source",
            )
        )
    elif not _is_state_path(source):
        errors.append(
            _issue(
                code="workflow.step_collect_source_invalid",
                message="Collect source must be a state path such as state.order.email",
                path=f"{path}.source",
            )
        )
    mode = config.get("mode", COLLECT_MODE_APPEND)
    if mode not in COLLECT_MODES:
        errors.append(
            _issue(
                code="workflow.step_collect_mode_invalid",
                message="Collect mode must be append or set_key",
                path=f"{path}.mode",
            )
        )
    key = config.get("key")
    if mode == COLLECT_MODE_SET_KEY:
        literal = isinstance(key, str) and key != ""
        selected = (
            isinstance(key, dict) and set(key) == {"source"} and _is_state_path(key.get("source"))
        )
        if not literal and not selected:
            errors.append(
                _issue(
                    code="workflow.step_collect_key_invalid",
                    message=(
                        "Collect set_key needs a key: a literal text, or {source: state path} "
                        "whose value becomes one literal key"
                    ),
                    path=f"{path}.key",
                )
            )
    elif key is not None:
        errors.append(
            _issue(
                code="workflow.step_collect_key_unexpected",
                message="Collect key is used only in set_key mode",
                path=f"{path}.key",
            )
        )
    return errors
