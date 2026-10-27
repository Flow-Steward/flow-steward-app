# ruff: noqa: F405, RUF100
# mypy: ignore-errors
import re

from .io_resolver_blocks import *  # noqa: F403,F401  # NOSONAR

_TEMPLATE_SELECTOR_RE = re.compile(r"\{\{\s*+([^{}]++)\}\}")


class IOResolverPreviewHelpersMixin:
    """Contract preview row builders and merge helpers."""

    @staticmethod
    def _state_target_name(target: object) -> str:
        target_name = str(target or "").strip()
        if target_name.startswith(STATE_PREFIX):
            target_name = target_name[len(STATE_PREFIX) :]
        return target_name

    @staticmethod
    def _state_source_name(value: object) -> str:
        """Only the exact set_variable source wrapper denotes a state reference."""
        if not isinstance(value, dict) or set(value) != {"source"}:
            return ""
        source = value.get("source")
        if not isinstance(source, str):
            return ""
        normalized = source.strip()
        return normalized if normalized == "state" or normalized.startswith(STATE_PREFIX) else ""

    @classmethod
    def _infer_state_output_type(cls, value: object, default: str = "json") -> str:
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return "number"
        if isinstance(value, str):
            return "text"
        if isinstance(value, list):
            return "array"
        if isinstance(value, dict):
            source = cls._state_source_name(value)
            if not source or source == "state":
                return "object"
            for variable in (*GLOBAL_STATE_VARS, *DOTTED_PATH_VARS):
                if source == f"state.{variable['name']}":
                    return str(variable["type"])
            return "json"
        return default

    @staticmethod
    def _to_object(value: object) -> dict:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            import json as _json

            try:
                parsed = _json.loads(value)
            except Exception:
                return {}
            return dict(parsed) if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _preview_source_kind(source: str) -> str:
        text = str(source or "").strip()
        if not text:
            return ""
        if text == "literal":
            return "literal"
        if text.startswith(("workflow_data.", "state.workflow_data.")):
            return "previous step output"
        if text.startswith((INPUTS_PREFIX, STATE_INPUTS_PREFIX)):
            return "workflow input"
        if text.startswith(("outputs.", "state.outputs.")):
            return "upstream step output"
        if text.startswith(("artifacts.", "state.artifacts.")):
            return "runtime artifact"
        if text.startswith("set_variable."):
            return "set_variable"
        if text.startswith(("global.", STATE_PREFIX)):
            return "global/runtime state"
        return "mapped variable"

    def _preview_input_row(
        self,
        *,
        name: str,
        input_type: str = "text",
        description: str = "",
        required: bool = True,
        source: str = "",
        available_type_map: dict[str, str] | None = None,
        options: list[str] | None = None,
    ) -> dict:
        clean_type = str(input_type or "text").strip() or "text"
        explicit_source = str(source or "").strip()
        available_map = available_type_map if isinstance(available_type_map, dict) else {}
        if explicit_source == "literal":
            resolved = True
            resolution = "literal"
            suggested_sources: list[str] = []
        else:
            resolution_payload = self._resolve_input_source(
                input_name=str(name or "").strip(),
                expected_type=clean_type,
                explicit_source=explicit_source,
                available_type_map=available_map,
            )
            resolved = bool(resolution_payload.get("resolved"))
            resolution = str(resolution_payload.get("reason") or "").strip()
            if explicit_source:
                explicit_source = str(resolution_payload.get("source") or explicit_source).strip()
            suggested_sources = list(resolution_payload.get("suggested_sources") or [])
        return {
            "name": str(name or "").strip(),
            "type": clean_type,
            "description": str(description or "").strip(),
            "required": bool(required),
            "source": explicit_source,
            "source_kind": self._preview_source_kind(explicit_source),
            "resolved": bool(resolved),
            "resolution": resolution,
            "suggested_sources": suggested_sources,
            "options": [str(item).strip() for item in (options or []) if str(item).strip()],
        }

    def _tool_interface_functions(self, tool: dict) -> list[dict]:
        interface = self._to_object(tool.get("interface"))
        functions = interface.get("functions")
        if isinstance(functions, list) and functions:
            return [dict(item) for item in functions if isinstance(item, dict)]
        parameters = interface.get("parameters")
        returns = interface.get("returns")
        if isinstance(parameters, list) or isinstance(returns, dict):
            return [
                {
                    "name": str(tool.get("tool_id") or "invoke").strip() or "invoke",
                    "description": str(tool.get("description") or "").strip(),
                    "parameters": parameters if isinstance(parameters, list) else [],
                    "returns": returns if isinstance(returns, dict) else {},
                }
            ]
        return []

    def _connector_operation_parameter_contract(
        self,
        *,
        connector_id: str,
        operation_id: str,
        input_name: str,
    ) -> dict:
        normalized_connector_id = str(connector_id or "").strip()
        tool_id = normalized_connector_id.removeprefix(CONNECTOR_PREFIX).strip()
        if not tool_id:
            return {}
        tool = self._get_tool(tool_id) or {}
        if not isinstance(tool, dict):
            return {}
        normalized_operation_id = str(operation_id or "").strip()
        normalized_input_name = str(input_name or "").strip()
        for func in self._tool_interface_functions(tool):
            func_name = str(func.get("name") or func.get("operation_id") or "").strip()
            if normalized_operation_id and func_name != normalized_operation_id:
                continue
            for param in func.get("parameters", []) or []:
                if not isinstance(param, dict):
                    continue
                param_name = str(param.get("name") or "").strip()
                if param_name == normalized_input_name:
                    return dict(param)
        return {}

    @staticmethod
    def _return_contract_rows(raw_returns: object) -> list[dict]:
        if isinstance(raw_returns, list):
            return [dict(item) for item in raw_returns if isinstance(item, dict)]
        if not isinstance(raw_returns, dict):
            return []
        properties = raw_returns.get("properties")
        if isinstance(properties, dict):
            required = raw_returns.get("required")
            required_names = (
                {str(item) for item in required} if isinstance(required, list) else set()
            )
            rows: list[dict] = []
            for key, value in properties.items():
                if not isinstance(value, dict):
                    continue
                row = dict(value)
                row.setdefault("name", str(key))
                row.setdefault("required", str(key) in required_names)
                rows.append(row)
            if rows:
                return rows
        if any(
            key in raw_returns
            for key in ("name", "field_key", "id", "key", "type", "value_type", "required")
        ):
            return [dict(raw_returns)]
        rows: list[dict] = []
        for key, value in raw_returns.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("name", str(key))
                rows.append(row)
            elif value:
                rows.append({"name": str(key), "type": str(value)})
        return rows

    def _tool_operation_return_contract(
        self,
        tool: dict,
        *,
        operation_id: str,
        output_name: str,
    ) -> dict:
        for func in self._tool_interface_functions(tool):
            func_name = str(func.get("name") or func.get("operation_id") or "").strip()
            if operation_id and func_name != operation_id:
                continue
            rows = self._return_contract_rows(func.get("returns"))
            for row in rows:
                row_names = {
                    str(row.get("name") or "").strip(),
                    str(row.get("field_key") or "").strip(),
                    str(row.get("id") or "").strip(),
                    str(row.get("key") or "").strip(),
                }
                if output_name and output_name in row_names:
                    return dict(row)
            if len(rows) == 1:
                row = dict(rows[0])
                row.setdefault("name", output_name)
                return row
        return {}

    def _connector_operation_return_contract(
        self,
        *,
        connector_id: str,
        operation_id: str,
        output_name: str,
        connector_install_id: str = "",
        account_id: str = "",
    ) -> dict:
        normalized_connector_id = str(connector_id or "").strip()
        normalized_operation_id = str(operation_id or "").strip()
        normalized_output_name = str(output_name or "").strip()
        if normalized_connector_id.startswith("extension."):
            from core.application.extensions.operation_support import (
                extension_operation_contract,
            )

            normalized_install_id = str(connector_install_id or "").strip()
            install_contract = self._get_extension_install_contract(
                install_id=normalized_install_id,
                account_id=account_id,
            )
            contract_account_id = str((install_contract or {}).get("account_id") or "").strip()
            contract_extension_id = str((install_contract or {}).get("extension_id") or "").strip()
            if (
                not isinstance(install_contract, dict)
                or str(install_contract.get("install_id") or "").strip() != normalized_install_id
                or not contract_extension_id
                or str(install_contract.get("install_state") or "").strip().lower() != "active"
                or (
                    bool(str(account_id or "").strip())
                    and contract_account_id != str(account_id or "").strip()
                )
            ):
                return {}
            projected_prefix = f"extension.{normalized_install_id}."
            if normalized_install_id and normalized_connector_id.startswith(projected_prefix):
                projected_tool = self._get_tool(normalized_connector_id) or {}
                implementation = self._to_object(projected_tool.get("implementation"))
                projected_account_id = str(projected_tool.get("account_id") or "").strip()
                if (
                    not isinstance(projected_tool, dict)
                    or str(projected_tool.get("source_install_id") or "").strip()
                    != normalized_install_id
                    or str(implementation.get("extension_key") or "").strip()
                    != contract_extension_id
                    or not str(implementation.get("action") or "").strip()
                    or projected_tool.get("enabled") is False
                    or (
                        bool(str(account_id or "").strip())
                        and projected_account_id != str(account_id or "").strip()
                    )
                ):
                    return {}
                return self._tool_operation_return_contract(
                    projected_tool,
                    operation_id=normalized_operation_id,
                    output_name=normalized_output_name,
                )

            extension_id = normalized_connector_id.removeprefix("extension.").strip()
            if contract_extension_id != extension_id:
                return {}
            operation = extension_operation_contract(
                install_contract,
                operation_id=normalized_operation_id,
            )
            if not operation.declared:
                return {}
            for output in operation.outputs:
                if str(output.get("name") or "").strip() == normalized_output_name:
                    return dict(output)
            return {}

        tool_id = normalized_connector_id.removeprefix(CONNECTOR_PREFIX).strip()
        if not tool_id:
            return {}
        tool = self._get_tool(tool_id) or {}
        if not isinstance(tool, dict):
            return {}
        return self._tool_operation_return_contract(
            tool,
            operation_id=normalized_operation_id,
            output_name=normalized_output_name,
        )

    def _state_selector_without_default(self, value: object) -> str:
        text = str(value or "").strip()
        if not text.startswith(STATE_PREFIX) or "|" in text:
            return ""
        return text

    def _template_selector_input_rows(
        self,
        *,
        value: object,
        field_prefix: str,
        description_prefix: str,
        available_type_map: dict[str, str],
    ) -> list[dict]:
        rows: list[dict] = []

        def _walk(raw_value: object, path: str) -> None:
            if isinstance(raw_value, dict):
                for key, child in raw_value.items():
                    child_path = f"{path}.{key}" if path else str(key)
                    _walk(child, child_path)
                return
            if isinstance(raw_value, list):
                for index, child in enumerate(raw_value):
                    child_path = f"{path}[{index}]"
                    _walk(child, child_path)
                return
            if not isinstance(raw_value, str):
                return
            for match in _TEMPLATE_SELECTOR_RE.finditer(raw_value):
                selector = str(match.group(1) or "").strip()
                if not selector or "|" in selector or not selector.startswith(STATE_PREFIX):
                    continue
                rows.append(
                    self._preview_input_row(
                        name=path or field_prefix,
                        input_type=str(available_type_map.get(selector) or "text"),
                        description=f"{description_prefix} {path or field_prefix}".strip(),
                        required=True,
                        source=selector,
                        available_type_map=available_type_map,
                    )
                )

        _walk(value, field_prefix)
        deduped: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (str(row.get("name") or "").strip(), str(row.get("source") or "").strip())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        # A template field can read several state values. Test Run values are
        # keyed by row name, so each distinct selector needs its own identity.
        counts: dict[str, int] = {}
        for row in deduped:
            counts[row["name"]] = counts.get(row["name"], 0) + 1
        used_names: set[str] = set()
        for row in deduped:
            if counts[row["name"]] > 1:
                base_name = f"{row['name']} ({row['source']})"
                candidate = base_name
                suffix = 2
                while candidate in counts or candidate in used_names:
                    candidate = f"{base_name} [{suffix}]"
                    suffix += 1
                row["name"] = candidate
            used_names.add(row["name"])
        return deduped

    def _preview_output_row(
        self,
        *,
        name: str,
        output_type: str = "text",
        description: str = "",
        source: str = "",
        source_kind: str = "",
    ) -> dict:
        return {
            "name": str(name or "").strip(),
            "type": str(output_type or "text").strip() or "text",
            "description": str(description or "").strip(),
            "source": str(source or "").strip(),
            "source_kind": str(source_kind or "").strip(),
        }

    @staticmethod
    def _normalize_contract_item(
        raw: dict,
        *,
        default_required: bool,
        source: str,
        source_id: str,
    ) -> dict | None:
        name = str((raw or {}).get("name") or "").strip()
        if not name:
            return None
        return {
            "name": name,
            "type": str((raw or {}).get("type") or "text").strip() or "text",
            "description": str((raw or {}).get("description") or "").strip(),
            "required": bool((raw or {}).get("required", default_required)),
            "source": source,
            "source_id": str(source_id or "").strip(),
        }

    def _merge_step_input_contract(
        self,
        *,
        agent_id: str,
        skill_id: str,
        block_key: str = "",
        include_runtime_io: bool = True,
    ) -> list[dict]:
        """Build step input contract.

        Canonical mode (include_runtime_io=False) uses skill inputs only.
        Runtime mode additionally merges tool interfaces and agent-level I/O.
        """
        merged: dict[str, dict] = {}
        resolved_block = self._resolve_published_block_contract(
            agent_id=agent_id,
            skill_id=skill_id,
            block_key=block_key,
        )
        resolved_block_key = str((resolved_block or {}).get("block_key") or block_key).strip()

        if isinstance(resolved_block, dict):
            for item in resolved_block.get("inputs") or resolved_block.get("_inputs") or []:
                if not isinstance(item, dict):
                    continue
                normalized = self._normalize_published_block_input_item(
                    item,
                    block_key=resolved_block_key,
                )
                if normalized:
                    merged[normalized["name"]] = normalized
            if not include_runtime_io:
                return list(merged.values())

        if skill_id:
            for item in self._get_skill_inputs(skill_id) or []:
                normalized = self._normalize_contract_item(
                    item,
                    default_required=False,
                    source="skill",
                    source_id=skill_id,
                )
                if normalized:
                    merged[normalized["name"]] = normalized

        if include_runtime_io:
            for tool_row in self._get_agent_tools(agent_id) or []:
                tool_id = str((tool_row or {}).get("tool_id") or "").strip()
                if not tool_id:
                    continue
                tool = self._get_tool(tool_id) or {}
                interface = tool.get("interface") or {}
                for param in interface.get("parameters", []) or []:
                    normalized = self._normalize_contract_item(
                        param,
                        default_required=False,
                        source="tool",
                        source_id=tool_id,
                    )
                    if normalized and normalized["name"] not in merged:
                        merged[normalized["name"]] = normalized
                for func in interface.get("functions", []) or []:
                    for param in (func or {}).get("parameters", []) or []:
                        normalized = self._normalize_contract_item(
                            param,
                            default_required=False,
                            source="tool",
                            source_id=tool_id,
                        )
                        if normalized and normalized["name"] not in merged:
                            merged[normalized["name"]] = normalized

            for item in self._get_agent_inputs(agent_id) or []:
                normalized = self._normalize_contract_item(
                    item,
                    default_required=False,
                    source="agent",
                    source_id=agent_id,
                )
                if not normalized:
                    continue
                existing = merged.get(normalized["name"])
                if not existing:
                    merged[normalized["name"]] = normalized
                    continue
                # Preserve skill-first semantics, but don't lose strictness/description hints.
                if normalized.get("required"):
                    existing["required"] = True
                if not existing.get("description") and normalized.get("description"):
                    existing["description"] = normalized["description"]

        return list(merged.values())

    def _merge_step_output_contract(
        self,
        *,
        agent_id: str,
        skill_id: str,
        block_key: str = "",
        include_runtime_io: bool = True,
        account_id: str | None = None,
    ) -> list[dict]:
        """Build step output contract.

        Canonical mode (include_runtime_io=False) uses the captured-authority resolver.
        Runtime mode additionally merges tool and agent outputs.
        """
        if not include_runtime_io:
            from core.application.registry.public_contract_projection_helpers import (
                _normalize_value_type,
            )
            from core.application.registry.step_output_contract import resolve_step_output_contract

            rows = resolve_step_output_contract(
                self.db,
                agent_id=agent_id,
                skill_id=skill_id,
                block_key=block_key,
                account_id=account_id or "",
            ).rows
            # Editor text aliases predate canonical JSON Schema types. Preserve
            # that presentation without collapsing precise object/array types.
            return [
                {
                    **row,
                    "type": _normalize_value_type(row["type"])
                    if row["type"] == "string"
                    else row["type"],
                }
                for row in rows
            ]
        merged: dict[str, dict] = {}
        resolved_block = self._resolve_published_block_contract(
            agent_id=agent_id,
            skill_id=skill_id,
            block_key=block_key,
            account_id=account_id,
        )
        resolved_block_key = str((resolved_block or {}).get("block_key") or block_key).strip()

        if isinstance(resolved_block, dict):
            for item in resolved_block.get("outputs") or resolved_block.get("_outputs") or []:
                if not isinstance(item, dict):
                    continue
                normalized = self._normalize_published_block_output_item(
                    item,
                    block_key=resolved_block_key,
                )
                if not normalized:
                    continue
                if not bool(normalized.get("exposed_by_default", True)):
                    continue
                merged[normalized["name"]] = normalized
            if not include_runtime_io:
                return list(merged.values())

        if skill_id:
            for item in self._get_skill_outputs(skill_id) or []:
                normalized = self._normalize_contract_item(
                    item,
                    default_required=True,
                    source="skill",
                    source_id=skill_id,
                )
                if normalized:
                    merged[normalized["name"]] = normalized

        if include_runtime_io:
            for tool_row in self._get_agent_tools(agent_id) or []:
                tool_id = str((tool_row or {}).get("tool_id") or "").strip()
                if not tool_id:
                    continue
                tool = self._get_tool(tool_id) or {}
                interface = tool.get("interface") or {}
                returns = interface.get("returns") or {}
                normalized = self._normalize_contract_item(
                    returns,
                    default_required=False,
                    source="tool",
                    source_id=tool_id,
                )
                if normalized and normalized["name"] not in merged:
                    merged[normalized["name"]] = normalized

            for item in self._get_agent_outputs(agent_id) or []:
                normalized = self._normalize_contract_item(
                    item,
                    default_required=False,
                    source="agent",
                    source_id=agent_id,
                )
                if normalized and normalized["name"] not in merged:
                    merged[normalized["name"]] = normalized

        return list(merged.values())

    def _candidate_sources_for_input(
        self,
        *,
        input_name: str,
        expected_type: str,
        available_type_map: dict[str, str],
    ) -> list[str]:
        """Suggest compatible sources for a contract input."""
        candidates: list[str] = []
        needle = f".{input_name}"
        for key in sorted(available_type_map.keys()):
            source_type = str(available_type_map.get(key) or "text").strip() or "text"
            if not self._check_type_compat(expected_type, source_type):
                continue
            if key == input_name or key.endswith(needle):
                candidates.append(key)
        return candidates[:8]

    def _resolve_input_source(
        self,
        *,
        input_name: str,
        expected_type: str,
        explicit_source: str,
        available_type_map: dict[str, str],
    ) -> dict:
        """Resolve explicit or inferred source mapping for one input."""
        source = str(explicit_source or "").strip()
        candidates = self._candidate_sources_for_input(
            input_name=input_name,
            expected_type=expected_type,
            available_type_map=available_type_map,
        )

        if source:
            source_type = str(available_type_map.get(source) or "").strip()
            if not source_type:
                return {
                    "resolved": False,
                    "source": source,
                    "reason": "explicit_source_missing",
                    "suggested_sources": candidates,
                }
            if not self._check_type_compat(expected_type, source_type):
                return {
                    "resolved": False,
                    "source": source,
                    "reason": "explicit_source_type_mismatch",
                    "suggested_sources": candidates,
                }
            return {
                "resolved": True,
                "source": source,
                "reason": "explicit_source",
                "suggested_sources": candidates,
            }

        exact_type = str(available_type_map.get(input_name) or "").strip()
        if exact_type and self._check_type_compat(expected_type, exact_type):
            return {
                "resolved": True,
                "source": input_name,
                "reason": "auto_exact_match",
                "suggested_sources": candidates,
            }

        if len(candidates) == 1:
            return {
                "resolved": True,
                "source": candidates[0],
                "reason": "auto_unique_candidate",
                "suggested_sources": candidates,
            }

        return {
            "resolved": False,
            "source": "",
            "reason": "unresolved",
            "suggested_sources": candidates,
        }

    def _step_contract_groups_from_rows(
        self,
        rows: list[dict],
    ) -> tuple[list[dict], list[dict], list[dict]]:
        locked_required: list[dict] = []
        unresolved_required: list[dict] = []
        optional: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if bool(row.get("required")):
                if bool(row.get("resolved")):
                    locked_required.append({**row, "read_only": True})
                else:
                    unresolved_required.append({**row, "read_only": False})
            else:
                optional.append({**row, "read_only": False})
        return locked_required, unresolved_required, optional


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
