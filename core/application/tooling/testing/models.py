"""Typed request contracts for testing tooling operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class _TestingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _validate_live_auth(*, username: str, password: str, bearer_token: str) -> None:
    has_username = bool(str(username or "").strip())
    has_password = bool(str(password or ""))
    has_bearer = bool(str(bearer_token or "").strip())
    if has_bearer and (has_username or has_password):
        raise ValueError("Choose either username/password or bearer token authentication")
    if has_username != has_password:
        raise ValueError("Both username and password are required for local authentication")
    if not has_bearer and not has_username:
        raise ValueError("Explicit authentication is required")


class TestingSmokeTestRequest(_TestingRequest):
    base_url: str = ""
    project_id: str = ""
    workflow_id: str = ""
    lane: Literal["backend", "ui"] = "backend"
    verify_ssl: bool = False
    timeout_seconds: int = 1800

    @field_validator("timeout_seconds")
    @classmethod
    def normalize_timeout(cls, value: int) -> int:
        return max(1, int(value or 1))


class TestingNightfireRegressionRequest(_TestingRequest):
    lane: Literal["ui", "backend"] = "ui"
    project_id: str = ""
    base_url: str = ""
    verify_ssl: bool = False
    auth_role: Literal["", "admin", "operator", "viewer"] = ""
    auth_username: str = ""
    auth_password: str = ""
    auth_display_name: str = ""
    source_workflow_id: str = ""
    report_path: str = ""
    with_cutover_smoke: bool = False
    with_full_ui: bool = False
    fail_fast: bool = False
    timeout_seconds: int = 7200


class TestingFuzzApiRequest(_TestingRequest):
    base_url: str = ""
    seed: int = 7331
    max_text_size: int = 16_384
    timeout: float = 15.0
    report_path: str = ""
    role: Literal["admin", "operator", "viewer"] = "admin"
    username: str = ""
    password: str = ""
    skip_auth: bool = False
    strict: bool = False

    @field_validator("max_text_size")
    @classmethod
    def normalize_text_size(cls, value: int) -> int:
        return max(256, int(value or 256))


class TestingFuzzMcpRequest(_TestingRequest):
    app_base_url: str = ""
    mcp_base_url: str = ""
    seed: int = 9917
    timeout: float = 15.0
    burst_size: int = 20
    report_path: str = ""
    role: Literal["admin", "operator", "viewer"] = "admin"
    username: str = ""
    password: str = ""
    bearer_token: str = ""
    skip_auth: bool = False
    strict: bool = False


class TestingFuzzUiRequest(_TestingRequest):
    base_url: str = ""
    seed: int = 8123
    timeout: float = 20.0
    report_path: str = ""
    role: Literal["admin", "operator", "viewer"] = "admin"
    username: str = ""
    password: str = ""
    skip_auth: bool = False
    project_id: str = ""
    routes: str = "/login,/dashboard,/projects,/jobs,/settings,/registry,/projects/{project_id}"
    lighthouse_routes: str = "/login,/dashboard,/projects,/jobs,/settings"
    lighthouse_threshold: float = 0.90
    lighthouse_timeout: float = 60.0
    skip_lighthouse: bool = False
    run_chrome_mcp: bool = False
    chrome_mcp_script: str = ""
    strict: bool = False


class TestingCutoverPreflightRequest(_TestingRequest):
    project_id: str
    base_url: str = "http://localhost:8099"
    verify_ssl: bool = False
    timeout_seconds: int = 300

    @field_validator("project_id")
    @classmethod
    def require_project_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("project_id is required")
        return normalized


class TestingCutoverSmokeRequest(TestingCutoverPreflightRequest):
    summary: str = "Phase 5 smoke: summarize the project status"
    timeout_seconds: int = 900
    poll_seconds: int = 5
    username: str = ""
    password: str = ""
    bearer_token: str = ""

    @model_validator(mode="after")
    def validate_authentication(self) -> TestingCutoverSmokeRequest:
        _validate_live_auth(
            username=self.username,
            password=self.password,
            bearer_token=self.bearer_token,
        )
        return self
