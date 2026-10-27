"""Capability candidates shared by workflow discovery consumers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CapabilityCandidate(BaseModel):
    id: str
    type: str
    display_name: str
    composition_role: str = ""
    operation_id: str = ""
    function_name: str = ""
    operation_display_name: str = ""
    operation_summary: str = ""
    why_matched: str = ""
    recommended_use: str = ""
    confidence: float = 0.0
    capability_summary: str = ""
    step_kinds: list[str] = Field(default_factory=list)
    input_names: list[str] = Field(default_factory=list)
    input_contracts: list[dict[str, Any]] = Field(default_factory=list)
    output_names: list[str] = Field(default_factory=list)
    skill_names: list[str] = Field(default_factory=list)
    # Structured eligibility metadata (Create #10): used to EXCLUDE a lexically
    # matching primitive whose role/anti-use-cases are incompatible, and to keep a
    # metadata-matching primitive eligible even when its wording differs.
    semantic_roles: list[str] = Field(default_factory=list)
    anti_use_cases: list[str] = Field(default_factory=list)
