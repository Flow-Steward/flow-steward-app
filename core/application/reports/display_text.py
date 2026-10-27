from __future__ import annotations

import re


def build_display_summary(raw: str | None, max_len: int = 96) -> str:
    """Return a compact single-line title for list/table surfaces.

    The canonical ``task_summary`` stays untouched for runtime execution.
    This helper only derives a shorter user-facing label for grids, cards,
    notifications, and operator consoles.
    """
    text = str(raw or "").replace("\r", "").strip()
    if not text:
        return ""

    lines = [str(line or "").strip() for line in text.split("\n")]
    first_line = next((line for line in lines if line), text)
    first_line = first_line.replace("\t", " ").strip()
    if first_line.startswith("[invoke_workflow:"):
        _tag, separator, remainder = first_line.partition("] ")
        if separator and remainder.strip():
            first_line = remainder.strip()
    for prefix in ("Task Brief:", "Task Request:", "Task Summary:"):
        if first_line.lower().startswith(prefix.lower()):
            first_line = first_line[len(prefix) :].strip()
            break

    if first_line.startswith(("#", "-", "*", "+")):
        first_line = first_line.lstrip("#-*+ ").strip()

    text = " ".join(first_line.split()).strip()
    if not text:
        text = " ".join(str(raw or "").split()).strip()
    if not text:
        return ""

    heading_split = text.split(" ### ")[0].split(" ## ")[0]
    if heading_split:
        text = heading_split.strip()

    titled = re.match(r"^(.{8,100}?):\s+", text)
    if titled:
        text = titled.group(1).strip()

    sentence = re.match(r"^([^.!?]{8,120}[.!?])(?:\s|$)", text)
    if sentence:
        text = sentence.group(1).rstrip(".!?").strip()

    max_len = max(8, int(max_len or 96))
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"
