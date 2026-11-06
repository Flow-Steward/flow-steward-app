"""Manifest tool adapter for runtimes without LangChain tooling."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel


class FallbackTool:
    def __init__(
        self,
        *,
        name: str,
        description: str,
        args_schema: type[BaseModel] | dict[str, Any],
        invoke_fn: Callable[..., Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.args_schema = args_schema
        self._invoke_fn = invoke_fn
        self.metadata = dict(metadata or {})

    def invoke(self, payload: dict[str, Any] | None = None) -> Any:
        if isinstance(self.args_schema, dict):
            # Match LangChain's dict-schema path: the MCP server validates its
            # own JSON arguments, including explicit nulls and extra keys.
            return self._invoke_fn(**dict(payload or {}))
        validated = self.args_schema.model_validate(payload or {})
        return self._invoke_fn(**validated.model_dump(exclude_unset=True, exclude_none=True))
