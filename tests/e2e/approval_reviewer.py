from __future__ import annotations


def _normalized_stage(stage: str) -> str:
    return str(stage or "").strip().lower()


def _min_content_len_for_stage(stage: str, overrides: dict[str, int] | None = None) -> int:
    normalized = _normalized_stage(stage)
    if overrides:
        if stage in overrides:
            return int(overrides[stage])
        lowered_overrides = {str(key).strip().lower(): value for key, value in overrides.items()}
        if normalized in lowered_overrides:
            return int(lowered_overrides[normalized])
    if normalized in {"requirements", "analysis"}:
        return 120
    if normalized in {"architecture", "design"}:
        return 120
    if normalized in {"stories", "story review", "planning"}:
        return 80
    if normalized in {"ready for review", "review", "delivery"}:
        return 40
    return 40


def approval_payload_issue(
    payload: dict | None,
    task_summary: str,
    stage_min_lengths: dict[str, int] | None = None,
) -> str:
    """Return validation error text for approval payload; empty string means acceptable."""
    if not isinstance(payload, dict):
        return "approval payload must be an object."

    stage = str(payload.get("stage") or "").strip()
    message = str(payload.get("message") or "").strip()
    content = str(payload.get("content") or "").strip()
    summary = str(task_summary or "").strip()

    if not stage:
        return "approval payload missing 'stage'."
    if not message or len(message) < 12:
        return "approval payload has an empty/too-short 'message'."
    if not content:
        return "approval payload missing 'content'."

    min_len = _min_content_len_for_stage(stage, overrides=stage_min_lengths)
    if len(content) < min_len:
        return f"approval content too short for stage '{stage}' ({len(content)} < {min_len})."

    if summary and content == summary:
        return "approval content equals task summary (no real review artifact presented)."
    return ""
