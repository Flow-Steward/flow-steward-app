"""Detached output declarations captured for one Agent execution attempt."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from psycopg import Error as DatabaseError

from core.application.registry.output_contract_fields import (
    OutputContractResolutionError as OutputContractResolutionError,
)
from core.application.registry.output_contract_fields import (
    _canonical,
    published_output_description,
)
from core.application.registry.output_contract_fields import (
    add_output_declaration as _add_declaration,
)
from core.application.registry.public_contract_projection_helpers import (
    _default_schema_for_value_type,
)
from core.application.registry.published_block_selection import (
    applicable_agent_block,
    in_account_scope,
    select_agent_block_key,
)


@dataclass(frozen=True)
class StepOutputContract:
    """An immutable JSON value: callers receive fresh rows, never registry references."""

    encoded: str

    @property
    def rows(self) -> list[dict[str, Any]]:
        return json.loads(self.encoded)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.encoded.encode()).hexdigest()

    def validate(self, artifacts: Any) -> list[str]:
        """Validate declared values; extra diagnostics do not become declarations."""
        if not isinstance(artifacts, dict):
            return ["Agent artifacts must be an object"]
        errors = []
        for row in self.rows:
            name = row["name"]
            value = artifacts.get(name)
            if row["required"] and (name not in artifacts or value is None):
                errors.append(f"Missing required output '{name}'")
            elif row["required"] and isinstance(value, str) and not value.strip():
                # A required text output must say something; an empty list or
                # object is a legitimate value and is validated by its schema.
                errors.append(f"Required output '{name}' is empty")
            elif name in artifacts and value is not None:
                validator = Draft202012Validator(row["contract_schema"])
                try:
                    _canonical(value)
                    valid = validator.is_valid(value)
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    errors.append(f"Invalid type or value for output '{name}'")
        return errors

    @classmethod
    def from_receipt(cls, rows: Any, digest: Any) -> StepOutputContract:
        """Verify canonical declaration content before trusting receipt names."""
        if not isinstance(rows, list):
            raise ValueError("Invalid output declarations")
        names = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "name",
                "type",
                "description",
                "required",
                "source",
                "source_id",
                "contract_schema",
                "response_schema",
            }:
                raise ValueError("Invalid output declaration")
            if not all(
                isinstance(row[key], str) and row[key]
                for key in ("name", "type", "source", "source_id")
            ):
                raise ValueError("Invalid output declaration identity")
            if not isinstance(row["description"], str):
                raise ValueError("Invalid output declaration description")
            if type(row["required"]) is not bool:
                raise ValueError("Invalid output declaration required flag")
            for key in ("contract_schema", "response_schema"):
                if not isinstance(row[key], dict):
                    raise ValueError("Invalid output declaration schema")
                Draft202012Validator.check_schema(row[key])
            names.append(row["name"])
        if names != sorted(set(names)):
            raise ValueError("Invalid output declaration order")
        value = cls(_canonical(rows))
        if not isinstance(digest, str) or value.digest != digest:
            raise ValueError("Output contract digest mismatch")
        return value


def _read(db: Any, method: str, key: str, default: Any) -> Any:
    getter = getattr(db, method, None)
    return getter(key) if callable(getter) else default


def resolve_step_output_contract(
    db: Any,
    *,
    agent_id: str,
    skill_id: str,
    account_id: str,
    block_key: str = "",
    skill_manifest: Any = None,
    agent_manifest: Any = None,
) -> StepOutputContract:
    """Resolve the exposed block, or selected skill plus genuine Agent-own outputs.

    Runtime supplies the already authorized selected SkillManifest. Readiness uses
    the same normalization over current rows, after its existing assignment checks.
    Missing optional adapter methods mean no such declaration source; failed reads
    never mean an empty contract.
    """
    try:
        return _resolve(
            db,
            agent_id=agent_id,
            skill_id=skill_id,
            account_id=account_id,
            block_key=block_key,
            skill_manifest=skill_manifest,
            agent_manifest=agent_manifest,
        )
    except OutputContractResolutionError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        SchemaError,
        RuntimeError,
        OSError,
        DatabaseError,
    ) as exc:
        raise OutputContractResolutionError("Could not resolve Agent output contract") from exc


def _get_block(db: Any, key: str, account_id: str) -> dict | None:
    getter = getattr(db, "get_published_block_contract", None)
    if not callable(getter):
        return None
    # Account filtering is performed on the returned row as well, including
    # minimal adapters whose legacy method has no scope keyword.
    import inspect

    parameters = inspect.signature(getter).parameters
    if "account_id" in parameters or any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
        return getter(key, account_id=account_id)
    return getter(key)


def _list_blocks(db: Any) -> list[dict]:
    getter = getattr(db, "list_published_block_contracts", None)
    return getter(enabled_only=True) if callable(getter) else []


def _resolve_block(
    db: Any, agent_id: str, skill_id: str, account_id: str, block_key: str
) -> dict | None:
    block = (
        applicable_agent_block(
            agent_id=agent_id,
            skill_id=skill_id,
            block_key=block_key,
            account_id=account_id,
            list_blocks=lambda: _list_blocks(db),
            get_block=lambda key: _get_block(db, key, account_id),
        )
        if agent_id or skill_id or block_key
        else None
    )
    if block_key and (
        block is None
        or not block.get("enabled", True)
        or (
            (agent_id or skill_id)
            and not select_agent_block_key(
                [block], agent_id=agent_id, skill_id=skill_id, account_id=account_id
            )
        )
    ):
        raise OutputContractResolutionError("Selected published output block is unavailable")
    return block


def _linked_output_schema(db: Any, row: dict, *, account_id: str) -> dict | None:
    """Resolve linked authoring constraints, excluding proven generated format defaults."""
    contract_id = str(row.get("data_contract_id") or row.get("contract_id") or "").strip()
    if not contract_id:
        return row.get("contract_schema")
    contract = _read(db, "get_data_contract", contract_id, None)
    if (
        not isinstance(contract, dict)
        or not contract.get("enabled", True)
        or not in_account_scope(contract, account_id)
    ):
        raise OutputContractResolutionError("Selected output data contract is unavailable")
    schema = contract.get("schema_json", contract.get("schema"))
    field_type = str(row.get("value_type") or row.get("type") or "").strip()
    field_name = str(row.get("field_key") or row.get("name") or "").strip()
    generated_default = (
        field_type in {"json", "list"}
        and contract.get("projection_mode") == "derived"
        and contract.get("derived_from_kind") == "field_shape"
        and contract.get("derived_from_ref") == field_name
        and contract.get("kind") == field_type
        and schema == _default_schema_for_value_type(field_type)
    )
    if generated_default:
        return None
    if not isinstance(schema, dict):
        raise OutputContractResolutionError("Invalid output data contract schema")
    return schema


def _skill_field_schema(
    db: Any, row: dict, prop: Any, *, account_id: str, legacy_metadata: bool
) -> dict | None:
    linked = _linked_output_schema(db, row, account_id=account_id)
    if linked is not None:
        return linked
    # Only the exact RegistryDBStore-generated whole schema is metadata.
    # Authored/extension schemas and linked Data Contracts remain strict.
    return None if legacy_metadata else prop


def _derived_skill_output_contracts(
    db: Any, block: dict, skill_id: str, skill_manifest: Any
) -> dict[str, str]:
    """Return the selected skill's own output contracts when the block mirrors it.

    A derived agent+skill block reads each output's shape from the shared field
    row, and one field key carries one contract for every skill and workflow that
    uses it. The skill's own output row is the authority for its block.
    """
    source_ref = str(block.get("source_ref") or "").strip()
    if (
        not skill_id
        or block.get("derived_from_kind") != "agent_skill_pair"
        or not source_ref.endswith(f"::{skill_id}")
    ):
        return {}
    rows = (
        getattr(skill_manifest, "expected_outputs", None)
        if skill_manifest is not None
        else _read(db, "get_skill_outputs", skill_id, None)
    )
    if rows is None:
        skill = _read(db, "get_skill", skill_id, {})
        rows = (skill or {}).get("_outputs", []) if isinstance(skill, dict) else []
    return {
        str(row.get("name") or "").strip(): str(row.get("contract_id") or "").strip()
        for row in rows or []
        if isinstance(row, dict) and str(row.get("contract_id") or "").strip()
    }


def _add_block_fields(
    fields: dict[str, dict],
    block: dict,
    db: Any,
    account_id: str,
    skill_contracts: dict[str, str] | None = None,
) -> None:
    for row in block.get("outputs", block.get("_outputs", [])) or []:
        own_contract = (skill_contracts or {}).get(str(row.get("field_key") or "").strip())
        if own_contract:
            row = {**row, "data_contract_id": own_contract}
        if row.get("exposed_by_default", True):
            _add_declaration(
                fields,
                {
                    "name": row.get("field_key"),
                    "type": row.get("value_type", "text"),
                    "description": published_output_description(row),
                    "required": row.get("required_by_default", False),
                    "contract_schema": _linked_output_schema(db, row, account_id=account_id),
                },
                "published_block",
                str(block["block_key"]),
            )


def _add_skill_fields(
    fields: dict[str, dict], db: Any, skill_id: str, skill_manifest: Any, account_id: str
) -> None:
    skill = _read(db, "get_skill", skill_id, {}) if skill_id else {}
    skill = skill if isinstance(skill, dict) else {}
    rows = (
        getattr(skill_manifest, "expected_outputs", None)
        if skill_manifest is not None
        else _read(db, "get_skill_outputs", skill_id, None)
        if skill_id
        else []
    )
    if rows is None:
        rows = skill.get("_outputs", skill.get("expected_outputs", []))
    schema = (
        getattr(skill_manifest, "output_schema", {})
        if skill_manifest is not None
        else skill.get("output_schema", {})
    )
    schema = schema if isinstance(schema, dict) else {}
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise OutputContractResolutionError("Invalid selected skill output schema")
    builder = getattr(db, "_build_contract_schema", None)
    legacy_metadata = (
        callable(builder)
        and bool(skill)
        and schema == skill.get("output_schema")
        and schema == builder(rows or [])
    )
    for row in rows or []:
        name = str(row.get("name") or "").strip()
        prop = _skill_field_schema(
            db, row, properties.get(name), account_id=account_id, legacy_metadata=legacy_metadata
        )
        _add_declaration(
            fields,
            {**row, "required": bool(row.get("required", True)) or name in required},
            "skill",
            skill_id,
            prop,
        )
    for name, prop in properties.items():
        if name not in fields:
            # The authored schema is the declaration. Infer a precise readiness
            # type only when its complete top-level type supports one; unions and
            # compositions retain unknown classification without widening validation.
            _add_declaration(
                fields,
                {
                    "name": name,
                    "type": "json",
                    "description": prop.get("description", ""),
                    "required": name in required,
                },
                "skill",
                skill_id,
                prop,
            )
    if any(name not in properties and name not in fields for name in required):
        raise OutputContractResolutionError("Required output has no declaration")


def _add_agent_fields(fields: dict[str, dict], db: Any, agent_id: str, agent_manifest: Any) -> None:
    own_getter = getattr(db, "get_agent_own_outputs", None)
    own = (
        own_getter(agent_id)
        if agent_id and callable(own_getter)
        else _read(db, "get_agent_outputs", agent_id, [])
        if agent_id
        else []
    )
    for row in own or []:
        if row.get("source") in (None, "", "own"):
            _add_declaration(fields, row, "agent", agent_id)
    agent = _read(db, "get_agent", agent_id, {}) if agent_id and agent_manifest is None else {}
    legacy = (
        getattr(agent_manifest, "outputs", [])
        if agent_manifest is not None
        else (agent or {}).get("outputs", [])
    )
    for name in legacy or []:
        if isinstance(name, str) and name not in fields:
            _add_declaration(
                fields,
                {"name": name, "type": "string", "required": False},
                "agent_manifest",
                agent_id,
            )


def _resolve(
    db: Any,
    *,
    agent_id: str,
    skill_id: str,
    account_id: str,
    block_key: str,
    skill_manifest: Any,
    agent_manifest: Any,
) -> StepOutputContract:
    fields: dict[str, dict] = {}
    block = _resolve_block(db, agent_id, skill_id, account_id, block_key)
    if block is not None:
        _add_block_fields(
            fields,
            block,
            db,
            account_id,
            _derived_skill_output_contracts(db, block, skill_id, skill_manifest),
        )
    else:
        _add_skill_fields(fields, db, skill_id, skill_manifest, account_id)
        _add_agent_fields(fields, db, agent_id, agent_manifest)
    return StepOutputContract(_canonical([fields[key] for key in sorted(fields)]))
