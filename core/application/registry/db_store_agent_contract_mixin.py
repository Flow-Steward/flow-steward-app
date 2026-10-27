"""Pure contract-row projection shared by the registry agent store."""

from __future__ import annotations

from typing import Any


class RegistryDBStoreAgentContractMixin:
    """Keep contract normalization separate without changing store dispatch."""

    @staticmethod
    def _materialized_contract_source(item: dict[str, Any]) -> str:
        src = str((item or {}).get("source") or "").strip()
        src_id = str((item or {}).get("source_id") or "").strip()
        if src in ("", "own", "agent"):
            return "own"
        if src_id:
            return f"{src}:{src_id}"
        return src

    @classmethod
    def _materialized_contract_rows(
        cls, items: list[dict[str, Any]] | None, *, output_contract: bool = False
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            source = cls._materialized_contract_source(item)
            # Only agent-authored (own) inputs carry their required flag into the
            # workflow contract. Inherited skill/tool inputs — especially tool
            # parameters supplied at call time — must stay optional, otherwise the
            # workflow validator would demand a prior phase produce them. This
            # mirrors the pre-materialization behavior (empty table = no required
            # inherited inputs) while still exposing the field names.
            required = (
                bool(item.get("required", True))
                if output_contract
                else bool(item.get("required", False)) and source == "own"
            )
            rows.append(
                {
                    "name": name,
                    "type": str(item.get("type") or "text") or "text",
                    "description": str(item.get("description") or ""),
                    "required": required,
                    "source": source,
                }
            )
        return rows
