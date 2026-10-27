"""Typed request contracts for quality tooling operations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class _QualityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QualityLintRequest(_QualityRequest):
    mode: Literal["default", "fast", "full", "audit"] = "default"
    checks: str = ""
    timeout_seconds: int = 3600


class QualityCheckCiExcludesRequest(_QualityRequest):
    path: str = ""
    timeout_seconds: int = 300


class QualityCheckNamingRequest(_QualityRequest):
    base: str = "HEAD~1"
    files: str = ""
    timeout_seconds: int = 300


class QualityReleaseGateRequest(_QualityRequest):
    timeout_seconds: int = 3600


class QualityVerifyRequest(_QualityRequest):
    tier: Literal[1, 2, 3]
    evidence_mode: Literal["off", "report", "reuse"] = "report"
    fingerprint_source: Literal["worktree", "index", "commit"] = "worktree"
    trust: Literal["local"] = "local"
    commit_sha: str = ""
    repository_root: str = "."
    evidence_root: str = "logs/quality/evidence"
    timeout_seconds: int = 7200


class QualitySonarExportIssuesRequest(_QualityRequest):
    host_url: str = "http://localhost:9000"
    project_key: str = "flow-steward-project"
    statuses: str = "OPEN,CONFIRMED,REOPENED"
    severities: str = ""
    types: str = ""
    page_size: int = 500
    output_dir: str = "logs/quality/sonar"
    sonar_token: str = ""
    timeout_seconds: int = 900

    @field_validator("page_size")
    @classmethod
    def normalize_page_size(cls, value: int) -> int:
        return max(1, min(int(value or 1), 500))


class QualitySonarEnsureRulesRequest(_QualityRequest):
    host_url: str = "http://localhost:9000"
    rules: str = ""
    sonar_token: str = ""
    timeout_seconds: int = 900


class QualitySonarGenerateBacklogRequest(_QualityRequest):
    input_csv: str = ""
    input_dir: str = "logs/quality/sonar"
    output: str = "logs/quality/sonar/sonar-dev-backlog.md"
    top_rules: int = 15
    top_files: int = 20
    max_p0: int = 60
    max_p1: int = 40
    timeout_seconds: int = 300
