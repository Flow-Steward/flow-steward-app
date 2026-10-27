"""
Data contract manifest — reusable schema for structured payloads passed between blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DataContractManifest:
    contract_id: str
    version: str
    display_name: str
    description: str
    kind: str
    schema: dict
    enabled: bool = True
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_db_row(cls, row: dict) -> DataContractManifest:
        return cls(
            contract_id=str(row.get("contract_id") or ""),
            version=str(row.get("version") or "1.0.0"),
            display_name=str(row.get("display_name") or ""),
            description=str(row.get("description") or ""),
            kind=str(row.get("kind") or "json"),
            schema=dict(row.get("schema_json") or row.get("schema") or {}),
            enabled=bool(row.get("enabled", True)),
            tags=list(row.get("tags") or []),
        )

    def to_dict(self) -> dict:
        return {
            "contract_id": self.contract_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "kind": self.kind,
            "schema": self.schema,
            "enabled": self.enabled,
            "tags": self.tags,
        }
