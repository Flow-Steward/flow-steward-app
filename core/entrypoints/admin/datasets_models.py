"""Request models for dataset admin routes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DatasetSave(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    dataset_id: str = ""
    display_name: str
    workspace_id: str = ""
    availability_mode: str = "specific_projects_only"
    description: str = ""
    connection_id: str
    read_only: bool = True
    enabled: bool = True
    schema_snapshot_jsonb: dict = Field(default_factory=dict)
    resource_scope_jsonb: dict = Field(default_factory=dict)
    metadata_jsonb: dict = Field(default_factory=dict)
    project_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class DatasetPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    display_name: str | None = None
    workspace_id: str | None = None
    availability_mode: str | None = None
    description: str | None = None
    connection_id: str | None = None
    read_only: bool | None = None
    enabled: bool | None = None
    schema_snapshot_jsonb: dict | None = None
    resource_scope_jsonb: dict | None = None
    metadata_jsonb: dict | None = None
    project_ids: list[str] | None = None
    tags: list[str] | None = None


class DataConnectionSave(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    connection_id: str = ""
    workspace_id: str
    connector_id: str
    display_name: str
    description: str = ""
    environment: str = "dev"
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)
    connection_config_jsonb: dict = Field(default_factory=dict)
    secret_refs_jsonb: dict = Field(default_factory=dict)
    metadata_jsonb: dict = Field(default_factory=dict)


class ProjectDatasetAssignment(BaseModel):
    dataset_ids: list[str] = Field(default_factory=list)


class DatasetProjectAssignment(BaseModel):
    project_ids: list[str] = Field(default_factory=list)


class DatasetQueryTemplateSave(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    query_template_id: str = ""
    dataset_id: str
    display_name: str
    description: str = ""
    query_language: str = "sql"
    query_definition_jsonb: dict = Field(default_factory=dict)
    params_schema_jsonb: dict = Field(default_factory=dict)
    output_schema_jsonb: dict = Field(default_factory=dict)
    validation_result_jsonb: dict = Field(default_factory=dict)
    enabled: bool = True
    version: int = 1


class DatasetQueryTemplateAction(BaseModel):
    project_id: str | None = None
    params: dict = Field(default_factory=dict)
    row_limit: int = 100
