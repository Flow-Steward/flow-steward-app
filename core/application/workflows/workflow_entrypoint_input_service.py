"""Application-owned input mapping for public workflow entrypoints."""

from __future__ import annotations

from typing import Any

from core.application.registry.workflow_contract_service import WorkflowInterfaceService


class WorkflowEntrypointMappingError(RuntimeError):
    """A caller-supplied mapping cannot be applied to the workflow contract."""

    status_code = 400


def _extract_path(root: Any, path: str) -> Any:
    cursor = root
    for token in [item for item in str(path or "").split(".") if item]:
        if isinstance(cursor, dict):
            cursor = cursor.get(token)
        elif isinstance(cursor, list) and token.isdigit():
            index = int(token)
            if index < 0 or index >= len(cursor):
                return None
            cursor = cursor[index]
        else:
            return None
    return cursor


def _normalize_header_map(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key or "").strip().lower(): item for key, item in value.items()}


class WorkflowEntrypointInputService:
    """Map ingress sources and validate them against the canonical workflow interface."""

    def __init__(self, interface_service: WorkflowInterfaceService | None = None) -> None:
        self._interfaces = interface_service or WorkflowInterfaceService()

    def expected_input_names(self, workflow_id: str) -> set[str]:
        interface = self._interfaces.interface_for_workflow_id(str(workflow_id or "").strip())
        return {
            str(row.name or "").strip()
            for row in (interface.expected_inputs or [])
            if str(row.name or "").strip()
        }

    def build_inputs(
        self,
        *,
        workflow_id: str,
        mapping: dict[str, str],
        sources: dict[str, Any],
        fallback_body: Any,
        file_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        expected_inputs = self.expected_input_names(workflow_id)
        workflow_inputs: dict[str, Any] = {}
        for target, source in mapping.items():
            if target not in expected_inputs:
                raise WorkflowEntrypointMappingError(
                    f"Input mapping target '{target}' is not a declared Expected Input"
                )
            root_name, _, tail = source.partition(".")
            if root_name not in {"body", "headers", "query", "form", "files"}:
                raise WorkflowEntrypointMappingError(f"Unsupported mapping selector '{source}'")
            source_root = sources.get(root_name)
            selector_tail = tail
            if root_name == "headers":
                source_root = _normalize_header_map(source_root)
                selector_tail = tail.lower()
            workflow_inputs[target] = (
                _extract_path(source_root, selector_tail) if selector_tail else source_root
            )
        if isinstance(fallback_body, dict):
            for name in expected_inputs:
                if name not in workflow_inputs and name in fallback_body:
                    workflow_inputs[name] = fallback_body.get(name)
        workflow_inputs["files"] = file_refs
        return workflow_inputs

    def validate_inputs(
        self,
        workflow_id: str,
        workflow_inputs: dict[str, Any],
        *,
        channel: str,
    ) -> Any:
        return self._interfaces.validate_inputs_for_workflow_id(
            workflow_id,
            workflow_inputs,
            channel=channel,
        )


__all__ = ["WorkflowEntrypointInputService", "WorkflowEntrypointMappingError"]
