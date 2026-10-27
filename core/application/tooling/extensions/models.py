"""Transport-safe extension tooling request and result contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExtensionBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extension_id: str
    root: str | Path = ""
    out_dir: str | Path = "dist/extensions"
    version: str = ""
    runtime_target: str = ""
    with_wheels: bool = False
    platforms: str = ""


class ExtensionBundleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    extension_id: str
    exit_code: int
    output: str = ""


class ExtensionDependencyLockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extension_id: str
    root: str | Path = ""
    input: str | Path = ""
    platforms: str = ""


class ExtensionDependencyLockResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    extension_id: str
    exit_code: int
    output: str = ""


class ExtensionTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extension_id: str
    root: str | Path = ""
    skip_validate: bool = False
    skip_smoke: bool = False
    skip_pytest: bool = False
    allow_no_pytest: bool = False
    #: Names of environment variables to hand this run, taken from the caller's
    #: own environment. Ephemeral input for one invocation — never product
    #: configuration, and never a value carried in the request.
    test_env: tuple[str, ...] = ()
    #: Pytest targets relative to the bundle (`tests/test_a.py` or a node id). A
    #: selection is a focused run and stops at the first failure; without one the
    #: whole `tests/test_*.py` suite runs to the end.
    test_paths: tuple[str, ...] = ()


class ExtensionTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    extension_id: str
    exit_code: int
    output: str = ""


class ExtensionValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extension_id: str
    root: str | Path = ""
    health: bool = False
    marketplace: bool = False


class ExtensionValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    extension_id: str
    exit_code: int
    output: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
