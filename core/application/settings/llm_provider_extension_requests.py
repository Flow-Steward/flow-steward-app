"""Strict transport inputs for account-scoped LLM provider extension workflows."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SaveLlmProviderCredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secrets: dict[str, Any] = Field(default_factory=dict)
    connection_config: dict[str, Any] = Field(default_factory=dict)
    remote_base_url: str = ""


class RefreshLlmProviderModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_config: dict[str, Any] = Field(default_factory=dict)
