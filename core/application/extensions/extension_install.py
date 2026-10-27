"""Extension Install model — account-level extension availability.

An ``extension_install`` represents that an extension is available at the
account/workspace level. Project-scoped activation uses ``ProjectConnection``.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExtensionInstall(BaseModel):
    """Account-level extension availability record."""

    model_config = ConfigDict(extra="forbid")

    extension_id: str
    account_id: str
    version: str = "1.0.0"
    kind: str = ""  # notification_provider | task_provider | tool_provider
    display_name: str = ""
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    installed_at: float = Field(default_factory=time.time)
