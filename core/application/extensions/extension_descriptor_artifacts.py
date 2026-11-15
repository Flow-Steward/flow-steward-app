"""The artifacts section of an extension descriptor: what the bundle ships.

Each entry names one packaged workflow, agent, skill, tool, dataset, saved
query, data contract, guardrail or nested extension by its identifier, with a
``ref`` to resolve or an inline ``snapshot``. Workflows must already be in
their final form and agents must not use phases; both are refused here,
before any install.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.application.extensions.extension_contract_policies import (
    ARTIFACT_ENTRY_ID_FIELDS as _ARTIFACT_ENTRY_ID_FIELDS,
)


class ExtensionDescriptorArtifactEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    workflow_id: str | None = None
    agent_id: str | None = None
    skill_id: str | None = None
    tool_id: str | None = None
    contract_id: str | None = None
    data_contract_id: str | None = None
    dataset_id: str | None = None
    query_template_id: str | None = None
    saved_query_id: str | None = None
    extension_id: str | None = None
    id: str | None = None

    ref: str = ""
    publish: bool = True
    snapshot: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_entry(self) -> ExtensionDescriptorArtifactEntry:
        artifact_id = self.artifact_id()
        if not artifact_id:
            raise ValueError("artifact entry must contain an identifier field")
        if not str(self.ref or "").strip() and not self.snapshot:
            raise ValueError("artifact entry requires ref or snapshot")
        return self

    def artifact_id(self) -> str:
        for field in _ARTIFACT_ENTRY_ID_FIELDS:
            value = getattr(self, field, None)
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        return ""


def _refuse_non_final_workflow(entry: ExtensionDescriptorArtifactEntry) -> None:
    """A packaged workflow is the one final definition: no removed shapes, valid hooks.

    The loop Group and the inline For each step are refused here, before any
    install, and so are an Invoke that is both direct and a hook, an invalid
    hook declaration, a hook inside a container and a hook id used twice (ADR 0038).
    """
    from core.application.workflows.workflow_definition_changes import removed_format_issues
    from core.application.workflows.workflow_hook_contract import invoke_config_issues
    from core.application.workflows.workflow_hook_placement import hook_placement_issues

    snapshot = entry.snapshot if isinstance(entry.snapshot, dict) else {}
    issues = [*removed_format_issues(snapshot), *hook_placement_issues(snapshot)]
    seen: set[str] = set()
    for step in snapshot.get("steps") or []:
        if not isinstance(step, dict) or step.get("step_kind") != "invoke_workflow":
            continue
        override = step.get("override") if isinstance(step.get("override"), dict) else {}
        config = override.get("invoke_workflow") if isinstance(override, dict) else None
        if not isinstance(config, dict) or "hook" not in config:
            continue
        issues.extend(invoke_config_issues(config))
        hook_id = config["hook"].get("hook_id") if isinstance(config["hook"], dict) else None
        if isinstance(hook_id, str) and hook_id in seen:
            issues.append(
                {
                    "code": "workflow.hook.id_duplicate",
                    "message": f"Hook id '{hook_id}' is used twice",
                }
            )
        if isinstance(hook_id, str):
            seen.add(hook_id)
    if issues:
        raise ValueError(
            f"Workflow '{entry.artifact_id()}' is not a final workflow definition: "
            + "; ".join(f"{issue['code']}: {issue['message']}" for issue in issues[:5])
        )


class ExtensionDescriptorArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflows: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    agents: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    skills: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    tools: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    datasets: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    saved_queries: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    data_contracts: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    guardrails: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    extensions: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)

    @field_validator("workflows")
    @classmethod
    def _validate_workflow_notes(
        cls, entries: list[ExtensionDescriptorArtifactEntry]
    ) -> list[ExtensionDescriptorArtifactEntry]:
        from core.application.workflows.workflow_canvas_annotation_contract import (
            ANNOTATIONS_SETTING,
            normalize_portable_annotations,
        )

        for entry in entries:
            _refuse_non_final_workflow(entry)
            settings = entry.snapshot.get("settings")
            if not isinstance(settings, dict) or ANNOTATIONS_SETTING not in settings:
                continue
            try:
                notes = normalize_portable_annotations(settings[ANNOTATIONS_SETTING])
            except ValueError as exc:
                raise ValueError(
                    f"Workflow '{entry.artifact_id()}' settings.canvas_annotations: {exc}"
                ) from exc
            entry.snapshot = {
                **entry.snapshot,
                "settings": {**settings, ANNOTATIONS_SETTING: notes},
            }
        return entries

    @field_validator("agents")
    @classmethod
    def _refuse_phase_era_agents(
        cls, entries: list[ExtensionDescriptorArtifactEntry]
    ) -> list[ExtensionDescriptorArtifactEntry]:
        from core.application.registry.agent_manifest import refuse_agent_phases

        for entry in entries:
            try:
                refuse_agent_phases(entry.snapshot)
            except ValueError as exc:
                raise ValueError(f"Agent '{entry.artifact_id()}': {exc}") from exc
        return entries

    @model_validator(mode="after")
    def _validate_not_empty(self) -> ExtensionDescriptorArtifacts:
        total = 0
        for entries in self.sections().values():
            total += len(entries)
        if total == 0:
            raise ValueError("artifacts must contain at least one artifact entry")
        return self

    def sections(self) -> dict[str, list[ExtensionDescriptorArtifactEntry]]:
        return {
            "workflows": self.workflows,
            "agents": self.agents,
            "skills": self.skills,
            "tools": self.tools,
            "datasets": self.datasets,
            "saved_queries": self.saved_queries,
            "data_contracts": self.data_contracts,
            "guardrails": self.guardrails,
            "extensions": self.extensions,
        }
