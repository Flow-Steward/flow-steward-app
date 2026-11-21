"""Extension catalog contracts the management CLI needs without the catalog itself.

The CLI image carries command specs and the exit-code mapping, but not the catalog
reader, the installer or their fetch stack; both need these two types.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExtensionCatalogInstallRequest(BaseModel):
    """The exact release selected from the published catalog."""

    model_config = ConfigDict(extra="forbid")

    extension_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=64)
    release_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_browser_authority_impact_hash: str = Field(
        default="", max_length=64, pattern=r"^(?:|[a-f0-9]{64})$"
    )


class ExtensionCatalogError(RuntimeError):
    """The configured extension catalog cannot be served."""

    def __init__(self, message: str, *, code: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    def detail(self) -> dict[str, Any]:
        # `message` is where the frontend HTTP client reads a reason, as it does for
        # the workflow import errors; without it the operator saw only the status.
        return {"error": str(self), "code": self.code, "message": str(self)}
