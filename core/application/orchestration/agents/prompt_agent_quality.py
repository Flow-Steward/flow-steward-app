# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from .prompt_agent_artifacts import *  # noqa: F403,F401  # NOSONAR


class PromptAgentQualityMixin:
    """Quality-gate verdict helpers."""

    @staticmethod
    def _extract_markdown_section(text: str, headings: tuple[str, ...]) -> str:
        """Return the first matching markdown section body, or an empty string."""
        for heading in headings:
            pattern = re.compile(rf"(?ms)^#+\s*{re.escape(heading)}\s*$\n(.*?)(?=^#+\s|\Z)")
            match = pattern.search(text)
            if match:
                body = match.group(1).strip()
                if body:
                    return body
        return ""

    def _extract_verdict(self, text: str, skill: str) -> dict:
        _ = skill
        _ = text
        return {}


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
